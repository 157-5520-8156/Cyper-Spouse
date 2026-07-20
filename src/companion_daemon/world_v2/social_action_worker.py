"""Recovery-safe worker for model-reachable deferred social actions."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Literal

from pydantic import Field

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .errors import ConcurrencyConflict
from .event_identity import domain_idempotency_key
from .expression_plan_acceptance import ExpressionPlanAcceptanceError
from .pinned_turn import PinnedTurnCompiler
from .proposal_audit_schemas import ProposalAuditProjection
from .proposal_envelope import DecisionProposal, MinimalProposal, validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import ClaimLease, Observation, ProjectionCursor, TriggerProcess, WorldEvent
from .social_action_acceptance import (
    SocialDeferredPolicy,
    derive_social_deferred_material,
)
from .social_action_atomic_recorder import SocialDeferredAtomicRecorder
from .deferred_thread_proposal import DeferredThreadProposalCompiler


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class SocialActionRunResult(FrozenModel):
    status: Literal[
        "idle", "deferred", "no_reply", "reply_now_proposed", "budget_exhausted",
        "stale", "unavailable", "duplicate",
    ]
    proposal_id: str | None = Field(default=None, min_length=1)
    action_id: str | None = Field(default=None, min_length=1)
    commitment_id: str | None = Field(default=None, min_length=1)
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)


class SocialActionWorker:
    """Audit a pinned observation, then accept only a source-bound defer."""

    def __init__(
        self,
        *,
        ledger,
        pinned_turn: PinnedTurnCompiler | None,
        batch_issuer: AcceptedLedgerBatchIssuer,
        policy: SocialDeferredPolicy,
        actor: str = "actor:companion",
        source: str = "world-v2:social-action-worker",
    ) -> None:
        if not actor or not source:
            raise ValueError("social action worker authority metadata is required")
        self._ledger = ledger
        self._turn = pinned_turn
        self._recorder = SocialDeferredAtomicRecorder(batch_issuer=batch_issuer)
        self._threads = DeferredThreadProposalCompiler(ledger=ledger)
        self._policy = policy
        self._actor = actor
        self._source = source

    @property
    def ledger(self):
        return self._ledger

    async def drain_one(self) -> SocialActionRunResult:
        """Process one durable message Observation not yet socially decided."""
        projection = self._ledger.project()
        decided_sources = {
            item.source_evidence_ref
            for item in projection.trigger_processes
            if item.process_kind == "social_action_deliberation"
            and item.state == "terminal"
            and item.source_evidence_ref is not None
        }
        for source in projection.message_observations:
            if source.actor == self._actor:
                continue
            observation = self._source_observation(
                observation_id=source.observation_id, projection=projection
            )
            if observation is None or observation[1].event_id in decided_sources:
                continue
            # Production reuses the already-audited main reply proposal.  A
            # missing/failed main audit is not work for this lane and must not
            # consume one background unit forever on every scheduler pass.
            if (
                self._turn is None
                and (
                    (audit := self._existing_audit(
                        projection=projection, trigger_ref=observation[1].event_id
                    )) is None
                    or self._proposal_choice(
                        validate_proposal_envelope(json.loads(audit.proposal_json))
                    ) != "defer"
                )
            ):
                continue
            return await self.run_observation(source.observation_id)
        return SocialActionRunResult(status="idle")

    async def run_observation(self, observation_id: str) -> SocialActionRunResult:
        projection = self._ledger.project()
        source = self._source_observation(observation_id=observation_id, projection=projection)
        if source is None:
            return SocialActionRunResult(status="unavailable", reason_code="social_action.source_unavailable")
        observation, observation_event = source
        audit = self._existing_audit(projection=projection, trigger_ref=observation_event.event_id)
        if audit is None:
            cursor = ProjectionCursor(world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence)
            if self._turn is None:
                return SocialActionRunResult(
                    status="unavailable", reason_code="social_action.shared_audit_unavailable"
                )
            try:
                await self._turn.audit_observation(
                    observation=observation,
                    observation_event=observation_event,
                    cursor=cursor,
                )
            except ConcurrencyConflict:
                return SocialActionRunResult(status="stale", reason_code="social_action.cursor_stale")
            projection = self._ledger.project()
            audit = self._existing_audit(projection=projection, trigger_ref=observation_event.event_id)
        if audit is None:
            return SocialActionRunResult(status="unavailable", reason_code="social_action.model_terminal_failure")
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        if not (
            isinstance(proposal, MinimalProposal)
            and (
                proposal.brief_rationale.startswith("social_action:")
                or proposal.proposal_id.startswith("proposal:chat-reply:")
            )
            or isinstance(proposal, DecisionProposal)
            and proposal.proposal_id.startswith("proposal:expression:")
        ):
            return SocialActionRunResult(status="unavailable", proposal_id=audit.proposal_id,
                reason_code="social_action.proposal_family_mismatch")
        choice = self._proposal_choice(proposal)
        if choice == "no_reply":
            self._record_terminal_decision(
                proposal=proposal, outcome="no_reply", observation_event=observation_event
            )
            return SocialActionRunResult(status="no_reply", proposal_id=proposal.proposal_id)
        if choice == "reply_now":
            self._record_terminal_decision(
                proposal=proposal, outcome="reply_now", observation_event=observation_event
            )
            return SocialActionRunResult(status="reply_now_proposed", proposal_id=proposal.proposal_id)
        if (
            len(proposal.action_intents) != 1
            or proposal.action_intents[0].kind != "followup"
        ):
            return SocialActionRunResult(status="unavailable", proposal_id=proposal.proposal_id,
                reason_code="social_action.unsupported_choice")
        projection = self._ledger.project()
        existing = next(
            (item for item in projection.actions if item.intent_ref.startswith(proposal.proposal_id + ":")),
            None,
        )
        if existing is not None:
            self._record_terminal_decision(
                proposal=proposal, outcome="accepted_defer", observation_event=observation_event
            )
            commitment = next((item for item in projection.commitments
                if item.values.fulfillment_contract.expected_action_id == existing.action_id), None)
            return SocialActionRunResult(status="duplicate", proposal_id=proposal.proposal_id,
                action_id=existing.action_id,
                commitment_id=commitment.commitment_id if commitment is not None else None)
        # Any intervening world change makes Acceptance stale.  In particular,
        # a newer user message must be reconsidered rather than authorizing old prose.
        if audit.evaluated_world_revision != projection.world_revision:
            self._record_terminal_decision(
                proposal=proposal, outcome="stale_reconsider", observation_event=observation_event
            )
            return SocialActionRunResult(status="stale", proposal_id=proposal.proposal_id,
                reason_code="social_action.new_world_evidence")
        source_ref = next(item for item in projection.message_observations
            if item.observation_id == observation_id)
        initial_cursor = ProjectionCursor(world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence)
        thread_payload, _thread_cursor = self._threads.record(
            audit=audit, cursor=initial_cursor, source_observation=source_ref,
            source_event=observation_event,
        )
        projection = self._ledger.project()
        account = next((item for item in projection.budget_accounts
            if item.account_id == self._policy.expression.account_id), None)
        if account is None:
            self._record_terminal_decision(
                proposal=proposal, outcome="budget_exhausted", observation_event=observation_event
            )
            return SocialActionRunResult(status="budget_exhausted", proposal_id=proposal.proposal_id,
                reason_code="social_action.chat_budget_unavailable")
        cursor = ProjectionCursor(world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence)
        acceptance_id = "acceptance:social-deferred:" + _digest({
            "world_id": self._ledger.world_id, "proposal_id": proposal.proposal_id,
            "proposal_hash": proposal.proposal_hash, "policy_digest": self._policy.digest,
        })
        try:
            material = derive_social_deferred_material(
                acceptance_id=acceptance_id,
                audit=audit,
                cursor=cursor,
                world_id=self._ledger.world_id,
                policy=self._policy,
                account=account,
                source_observation=source_ref,
                source_observation_event_ref=observation_event.event_id,
                logical_time=projection.logical_time or observation.logical_time,
                created_at=observation.created_at,
                trace_id=observation.trace_id,
                correlation_id=observation.correlation_id,
                thread_payload=thread_payload,
            )
        except ExpressionPlanAcceptanceError as exc:
            if exc.code == "expression_plan_acceptance.budget_unavailable":
                self._record_terminal_decision(
                    proposal=proposal, outcome="budget_exhausted",
                    observation_event=observation_event
                )
                return SocialActionRunResult(status="budget_exhausted", proposal_id=proposal.proposal_id,
                    reason_code="social_action.chat_budget_exhausted")
            raise
        handle = self._recorder.prepare_batch(material=material, actor=self._actor, source=self._source)
        try:
            self._ledger.commit_accepted(handle, expected_cursor=cursor)
        except ConcurrencyConflict:
            raced = self._ledger.project()
            existing = next(
                (item for item in raced.actions
                 if item.intent_ref.startswith(proposal.proposal_id + ":")),
                None,
            )
            if existing is not None:
                self._record_terminal_decision(
                    proposal=proposal, outcome="accepted_defer",
                    observation_event=observation_event
                )
                commitment = next(
                    (item for item in raced.commitments
                     if item.values.fulfillment_contract.expected_action_id == existing.action_id),
                    None,
                )
                return SocialActionRunResult(
                    status="duplicate", proposal_id=proposal.proposal_id,
                    action_id=existing.action_id,
                    commitment_id=commitment.commitment_id if commitment is not None else None,
                )
            self._record_terminal_decision(
                proposal=proposal, outcome="stale_reconsider",
                observation_event=observation_event
            )
            return SocialActionRunResult(status="stale", proposal_id=proposal.proposal_id,
                reason_code="social_action.acceptance_race")
        self._record_terminal_decision(
            proposal=proposal, outcome="accepted_defer", observation_event=observation_event
        )
        return SocialActionRunResult(
            status="deferred",
            proposal_id=proposal.proposal_id,
            action_id=material.expression.beats[0].action.action_id,
            commitment_id=material.commitment_payload.commitment_after.commitment_id,
        )

    def _record_terminal_decision(
        self, *, proposal: MinimalProposal | DecisionProposal,
        outcome: Literal[
            "accepted_defer", "stale_reconsider", "budget_exhausted", "no_reply", "reply_now"
        ],
        observation_event: WorldEvent,
    ) -> None:
        trigger_id = "trigger:social-action:" + _digest({
            "world_id": self._ledger.world_id,
            "source_event_ref": observation_event.event_id,
            "proposal_id": proposal.proposal_id,
        })
        projection = self._ledger.project()
        existing = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id), None
        )
        if existing is not None:
            if existing.state != "terminal" or existing.runtime_outcome_ref != (
                f"social-action-decision:{outcome}:{proposal.proposal_id}"
            ):
                raise ValueError("social action decision trigger is incomplete or conflicting")
            return
        at = projection.logical_time or observation_event.logical_time
        attempt_id = "attempt:social-action:" + _digest([trigger_id, proposal.proposal_hash])
        open_process = TriggerProcess(
            trigger_id=trigger_id,
            trigger_ref=f"social-action:{proposal.proposal_id}",
            process_kind="social_action_deliberation",
            source_evidence_ref=observation_event.event_id,
            state="open",
        )
        claimed = open_process.model_copy(update={
            "state": "claimed",
            "claim_lease": ClaimLease(owner_id=self._actor, attempt_id=attempt_id,
                acquired_at=at, expires_at=at + timedelta(minutes=2)),
            "attempt_ids": (attempt_id,),
        })
        completion_payload = {
            "trigger_id": trigger_id,
            "owner_id": self._actor,
            "attempt_id": attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": f"social-action-decision:{outcome}:{proposal.proposal_id}",
        }
        raw = (
            ("TriggerProcessOpened", {"process": open_process.model_dump(mode="json")}),
            ("TriggerProcessClaimed", {"process": claimed.model_dump(mode="json")}),
            ("TriggerProcessCompleted", completion_payload),
        )
        common = dict(schema_version="world-v2.1", world_id=self._ledger.world_id,
            logical_time=at, created_at=observation_event.created_at, actor=self._actor,
            source=self._source, trace_id=observation_event.trace_id,
            correlation_id=observation_event.correlation_id)
        events: list[WorldEvent] = []
        for index, (event_type, payload) in enumerate(raw):
            identity = domain_idempotency_key(
                event_type=event_type, world_id=self._ledger.world_id, payload=payload
            ) or f"world-v2:social-action-decision:{event_type}:" + _digest(
                [self._ledger.world_id, trigger_id, attempt_id]
            )
            events.append(WorldEvent.from_payload(
                **common,
                event_id=f"event:social-action:{event_type.lower()}:" + _digest([trigger_id, attempt_id]),
                event_type=event_type,
                causation_id=(observation_event.event_id if index == 0 else events[-1].event_id),
                idempotency_key=identity,
                payload=payload,
            ))
        cursor = ProjectionCursor(world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence)
        self._ledger.commit_at_cursor(
            tuple(events), expected_cursor=cursor,
            commit_id="commit:social-action-decision:" + _digest([trigger_id, attempt_id]),
        )

    def _source_observation(self, *, observation_id: str, projection) -> tuple[Observation, WorldEvent] | None:
        for ref in reversed(projection.committed_world_event_refs):
            if ref.event_type != "ObservationRecorded":
                continue
            located = self._ledger.lookup_event_commit(ref.event_id)
            if located is None:
                continue
            try:
                observation = Observation.model_validate_json(located[0].payload_json)
            except ValueError:
                continue
            if observation.observation_id == observation_id:
                return observation, located[0]
        return None

    @staticmethod
    def _proposal_choice(proposal: MinimalProposal | DecisionProposal) -> str:
        if isinstance(proposal, DecisionProposal):
            return {"now": "reply_now", "later": "defer", "silent": "no_reply"}[
                proposal.timing_choice
            ]
        if not proposal.proposed_changes:
            return "no_reply"
        return "defer" if proposal.action_intents[0].kind == "followup" else "reply_now"

    @staticmethod
    def _existing_audit(*, projection, trigger_ref: str) -> ProposalAuditProjection | None:
        matches = []
        for audit in projection.proposal_audits:
            if audit.trigger_ref != trigger_ref:
                continue
            try:
                proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(proposal, MinimalProposal)
                and (
                    proposal.brief_rationale.startswith("social_action:")
                    or proposal.proposal_id.startswith("proposal:chat-reply:")
                )
                or isinstance(proposal, DecisionProposal)
                and proposal.proposal_id.startswith("proposal:expression:")
            ):
                matches.append(audit)
        if len(matches) > 1:
            raise ValueError("social action has duplicate proposal audit authority")
        return matches[0] if matches else None


__all__ = ["SocialActionRunResult", "SocialActionWorker"]
