"""Application-owned lifecycle for a durable ``reply_later`` responsibility.

The legacy social-task queue treated a delayed reply as an implementation
detail.  World v2 treats it as a private commitment whose only fulfillment is
the exact terminal receipt of one scheduled Action.  This module is the small
write seam that keeps platform hosts from constructing proposals, acceptance
records, or commitment after-images themselves.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from pydantic import Field, model_validator

from .commitment_events import (
    CommitmentChangedPayload,
    CommitmentClockTransitionPayload,
    commitment_mutation_hash,
)
from .commitment_reducers import COMMITMENT_DEADLINE_POLICY_DIGEST, COMMITMENT_DEADLINE_POLICY_VERSION
from .event_identity import domain_idempotency_key
from .schema_core import FrozenModel
from .schemas import (
    Action, BudgetReservation, CommitmentFulfillmentContract, CommitmentOrigin,
    CommitmentProjection, CommitmentProposedMutation, CommitmentProposalProjection,
    CommitmentValues, EvidenceRef, Observation, ProjectionCursor, WorldEvent,
    commitment_semantic_fingerprint,
)


POLICY_REFS = ("policy:commitment-v1",)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class ReplyLaterCommand(FrozenModel):
    command_id: str = Field(min_length=1, max_length=256)
    world_id: str = Field(min_length=1, max_length=256)
    source_observation_id: str = Field(min_length=1, max_length=512)
    commitment_id: str = Field(min_length=1, max_length=512)
    action_id: str = Field(min_length=1, max_length=512)
    target: str = Field(min_length=1, max_length=512)
    payload_ref: str = Field(min_length=1, max_length=1024)
    payload_hash: str = Field(min_length=64, max_length=64)
    content_ref: str = Field(min_length=1, max_length=1024)
    content_hash: str = Field(min_length=64, max_length=64)
    due_opens_at: datetime
    due_closes_at: datetime
    importance_bp: int = Field(ge=0, le=10_000)
    budget_account_id: str = Field(min_length=1, max_length=512)
    budget_amount: int = Field(ge=0)
    recovery_policy: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def stable_lifecycle_is_valid(self) -> "ReplyLaterCommand":
        if self.commitment_id == self.action_id:
            raise ValueError("reply-later commitment and Action identities must differ")
        if self.due_opens_at.tzinfo is None or self.due_closes_at.tzinfo is None:
            raise ValueError("reply-later due window must be timezone-aware")
        if self.due_closes_at <= self.due_opens_at:
            raise ValueError("reply-later due window must move forward")
        return self


class _LegacyReplyLaterAuthority:
    __slots__ = ()


_LEGACY_REPLY_LATER_AUTHORITY = _LegacyReplyLaterAuthority()


class DeferredReplyRuntime:
    """Settle deferred responsibilities; legacy command authoring is isolated.

    Production authoring goes through ``SocialActionWorker``.  The opt-in
    command seam exists only so archived fixtures/migrations can replay old
    authority without becoming a second live decision path.
    """

    def __init__(self, *, ledger, actor: str = "actor:companion",
                 source: str = "world-v2:reply-later") -> None:
        if not actor:
            raise ValueError("reply-later runtime needs a companion actor")
        self._ledger, self._actor, self._source = ledger, actor, source

    def _defer_legacy(self, command: ReplyLaterCommand, *, authority: _LegacyReplyLaterAuthority,
                      logical_time: datetime, created_at: datetime, trace_id: str,
                      causation_id: str, correlation_id: str):
        if authority is not _LEGACY_REPLY_LATER_AUTHORITY:
            raise ValueError("reply_later.migration_authority_required")
        if command.world_id != self._ledger.world_id:
            raise ValueError("reply-later command belongs to another world")
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            raise ValueError("reply-later command must pin the current logical clock")
        existing = next((item for item in projection.commitments if item.commitment_id == command.commitment_id), None)
        if existing is not None:
            event_id = self._event_id("open", command.command_id)
            located = self._ledger.lookup_event_commit(event_id)
            if located is None:
                raise ValueError("reply-later commitment identity is already occupied")
            return located[1]
        if any(item.action_id == command.action_id for item in projection.actions):
            raise ValueError("reply-later Action identity is already occupied")
        source = next((item for item in projection.message_observations
                       if item.observation_id == command.source_observation_id), None)
        if source is None:
            raise ValueError("reply-later source observation is unavailable")
        if logical_time >= command.due_closes_at:
            raise ValueError("reply-later deadline is already closed")
        account = next((item for item in projection.budget_accounts
                        if item.account_id == command.budget_account_id), None)
        if account is None or account.category != "chat":
            raise ValueError("reply-later requires an active chat budget account")
        evidence = EvidenceRef(ref_id=source.observation_id, evidence_type="observed_message",
            claim_purpose="conversation_continuity", source_world_revision=source.world_revision,
            immutable_hash=source.event_payload_hash)
        reservation_id = "reservation:reply-later:" + _digest([command.world_id, command.command_id])
        action = Action(schema_version="world-v2.1", action_id=command.action_id,
            world_id=command.world_id, logical_time=logical_time, created_at=created_at,
            trace_id=trace_id, causation_id=causation_id, correlation_id=correlation_id,
            kind="followup", layer="external_action", intent_ref=command.commitment_id,
            actor=self._actor, target=command.target, payload_ref=command.payload_ref,
            payload_hash=command.payload_hash,
            idempotency_key="reply-later:" + _digest([command.world_id, command.command_id]),
            not_before=command.due_opens_at, expires_at=command.due_closes_at,
            budget_reservation_id=reservation_id, state="authorized", recovery_policy=command.recovery_policy)
        acceptance_id = "acceptance:reply-later:" + _digest([command.world_id, command.command_id])
        proposal_id = "proposal:reply-later:" + _digest([command.world_id, command.command_id])
        change_id = "change:reply-later:" + _digest([command.world_id, command.commitment_id])
        transition_id = "transition:reply-later:" + _digest([command.world_id, command.commitment_id])
        mutation_event_id = self._event_id("open", command.command_id)
        contract = CommitmentFulfillmentContract(contract_kind="execution_receipt",
            evidence_type="settled_external_result", expected_action_id=action.action_id,
            expected_action_payload_hash=action.payload_hash, expected_result_status="delivered",
            contract_version="commitment-fulfillment-contract.1")
        values = CommitmentValues(subject_ref=source.observation_id, content_ref=command.content_ref,
            content_hash=command.content_hash, anchor_evidence_refs=(evidence,), source_evidence_refs=(evidence,),
            importance_bp=command.importance_bp, due_window={"opens_at": command.due_opens_at, "closes_at": command.due_closes_at},
            persistence_level="session", fulfillment_contract=contract, privacy_class="private", status="open")
        after = CommitmentProjection(commitment_id=command.commitment_id, entity_revision=1,
            semantic_fingerprint=commitment_semantic_fingerprint(owner_ref=self._actor, subject_ref=source.observation_id,
                content_ref=command.content_ref, content_hash=command.content_hash, anchor_evidence_refs=(evidence,),
                fulfillment_contract=contract, policy_refs=POLICY_REFS), values=values,
            origin=CommitmentOrigin(change_id=change_id, transition_id=transition_id, policy_refs=POLICY_REFS,
                accepted_event_ref=mutation_event_id), opened_at=logical_time, updated_at=logical_time)
        raw = {"change_id": change_id, "transition_id": transition_id, "expected_entity_revision": 0,
            "evidence_refs": (evidence,), "policy_refs": POLICY_REFS, "acceptance_id": acceptance_id,
            "proposal_id": proposal_id, "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64, "operation": "open", "commitment_before": None,
            "commitment_after": after}
        raw["accepted_change_hash"] = commitment_mutation_hash(raw)
        payload = CommitmentChangedPayload.model_validate(raw)
        proposal = CommitmentProposalProjection(proposal_id=proposal_id, proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:commitment.1", transition_kind="open", change_id=change_id,
            transition_id=transition_id, evaluated_world_revision=projection.world_revision,
            expected_entity_revision=0, proposed_change_hash=payload.accepted_change_hash,
            evidence_refs=(evidence,), policy_refs=POLICY_REFS,
            proposed_mutation=CommitmentProposedMutation(event_type="PrivateCommitmentOpened",
                payload_json=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
        common = dict(schema_version="world-v2.1", world_id=command.world_id, logical_time=logical_time,
            created_at=created_at, actor=self._actor, source=self._source, trace_id=trace_id,
            correlation_id=correlation_id)
        proposal_event = WorldEvent.from_payload(**common, event_id=self._event_id("proposal", command.command_id),
            event_type="ProposalRecorded", causation_id=causation_id,
            idempotency_key=domain_idempotency_key(event_type="ProposalRecorded", world_id=command.world_id,
                payload=proposal.model_dump(mode="json")) or proposal_id, payload=proposal.model_dump(mode="json"))
        cursor = ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence)
        persisted_proposal = self._ledger.lookup_event_commit(proposal_event.event_id)
        if persisted_proposal is None:
            self._ledger.commit_at_cursor((proposal_event,), expected_cursor=cursor,
                commit_id="reply-later:proposal:" + _digest([command.world_id, command.command_id]))
        elif persisted_proposal[0] != proposal_event:
            raise ValueError("reply-later proposal identity has conflicting durable content")
        current = self._ledger.project()
        acceptance = {"acceptance_id": acceptance_id, "status": "accepted", "proposal_id": proposal_id,
            "evaluated_world_revision": projection.world_revision, "accepted_change_id": change_id,
            "accepted_change_hash": payload.accepted_change_hash}
        acceptance_event = WorldEvent.from_payload(**common, event_id=self._event_id("acceptance", command.command_id),
            event_type="AcceptanceRecorded", causation_id=proposal_event.event_id,
            idempotency_key=domain_idempotency_key(event_type="AcceptanceRecorded", world_id=command.world_id, payload=acceptance) or acceptance_id,
            payload=acceptance)
        mutation_event = WorldEvent.from_payload(**common, event_id=mutation_event_id,
            event_type="PrivateCommitmentOpened", causation_id=acceptance_event.event_id,
            idempotency_key=domain_idempotency_key(event_type="PrivateCommitmentOpened", world_id=command.world_id,
                payload=payload.model_dump(mode="json")) or command.commitment_id, payload=payload.model_dump(mode="json"))
        reservation = BudgetReservation(reservation_id=reservation_id, account_id=account.account_id,
            action_id=action.action_id, category="chat", amount_limit=command.budget_amount)
        reserve_event = WorldEvent.from_payload(**common, event_id=self._event_id("reservation", command.command_id),
            event_type="BudgetReserved", causation_id=mutation_event.event_id,
            idempotency_key="reply-later:reservation:" + _digest([command.world_id, command.command_id]),
            payload={"reservation": reservation.model_dump(mode="json")})
        action_event = WorldEvent.from_payload(**common, event_id=self._event_id("action", command.command_id),
            event_type="ActionAuthorized", causation_id=reserve_event.event_id,
            idempotency_key="reply-later:action:" + _digest([command.world_id, command.command_id]),
            payload={"action": action.model_dump(mode="json")})
        cursor = ProjectionCursor(world_revision=current.world_revision, deliberation_revision=current.deliberation_revision,
            ledger_sequence=current.ledger_sequence)
        existing_acceptance = self._ledger.lookup_event_commit(acceptance_event.event_id)
        if existing_acceptance is not None:
            if existing_acceptance[0] != acceptance_event:
                raise ValueError("reply-later acceptance identity has conflicting durable content")
            action_commit = self._ledger.lookup_event_commit(action_event.event_id)
            if action_commit is None or action_commit[0] != action_event:
                raise RuntimeError("reply-later accepted batch is incomplete")
            return action_commit[1]
        return self._ledger.commit_at_cursor((acceptance_event, mutation_event, reserve_event, action_event),
            expected_cursor=cursor, commit_id="reply-later:accept:" + _digest([command.world_id, command.command_id]))

    def _event_id(self, role: str, command_id: str) -> str:
        return "event:reply-later:" + role + ":" + _digest([self._ledger.world_id, command_id])

    def clock_events(self, *, projection, clock_event: WorldEvent) -> tuple[WorldEvent, ...]:
        """Derive deterministic due/break transitions after a committed clock tick."""
        logical_time = clock_event.logical_time
        clock_evidence = EvidenceRef(
            ref_id=f"clock:{logical_time.isoformat()}", evidence_type="clock_observation",
            claim_purpose="conversation_continuity",
        )
        events: list[WorldEvent] = []
        for commitment in projection.commitments:
            if commitment.values.status not in {"open", "due"}:
                continue
            operations: tuple[str, ...]
            if commitment.values.status == "open" and logical_time >= commitment.values.due_window.opens_at:
                operations = ("due", "break") if logical_time >= commitment.values.due_window.closes_at else ("due",)
            elif commitment.values.status == "due" and logical_time >= commitment.values.due_window.closes_at:
                operations = ("break",)
            else:
                continue
            current = commitment
            for operation in operations:
                next_values = current.values.model_copy(update={
                    "source_evidence_refs": (*current.values.source_evidence_refs, clock_evidence),
                    "status": "due" if operation == "due" else "broken",
                    "settlement_evidence_ref": None if operation == "due" else clock_evidence.ref_id,
                    "settlement_reason_code": None if operation == "due" else "deadline_elapsed",
                })
                role = f"clock-{operation}:{clock_event.event_id}"
                event_id = "event:reply-later:" + role + ":" + _digest([self._ledger.world_id, current.commitment_id])
                after = current.model_copy(update={
                    "entity_revision": current.entity_revision + 1, "values": next_values,
                    "updated_at": logical_time,
                    "origin": CommitmentOrigin(authority_mode="mechanical_clock",
                        change_id="change:reply-later:" + _digest([role, current.commitment_id]),
                        transition_id="transition:reply-later:" + _digest([role, current.commitment_id]),
                        policy_refs=current.origin.policy_refs, accepted_event_ref=event_id),
                })
                payload = CommitmentClockTransitionPayload(
                    change_id=after.origin.change_id, transition_id=after.origin.transition_id,
                    operation=operation, expected_entity_revision=current.entity_revision,
                    commitment_before=current, commitment_after=after, clock_evidence_ref=clock_evidence,
                    clock_event_ref=clock_event.event_id, clock_event_payload_hash=clock_event.payload_hash,
                    policy_version=COMMITMENT_DEADLINE_POLICY_VERSION,
                    policy_digest=COMMITMENT_DEADLINE_POLICY_DIGEST,
                ).model_dump(mode="json")
                events.append(WorldEvent.from_payload(schema_version="world-v2.1", event_id=event_id,
                    world_id=self._ledger.world_id, event_type=("PrivateCommitmentDue" if operation == "due" else "PrivateCommitmentDeadlineBroken"),
                    logical_time=logical_time, created_at=clock_event.created_at, actor="system:reply-later-clock",
                    source=self._source, trace_id=clock_event.trace_id, causation_id=clock_event.event_id,
                    correlation_id=clock_event.correlation_id,
                    idempotency_key=domain_idempotency_key(event_type=("PrivateCommitmentDue" if operation == "due" else "PrivateCommitmentDeadlineBroken"), world_id=self._ledger.world_id, payload=payload) or event_id,
                    payload=payload))
                current = after
        return tuple(events)

    def settle_terminal_action(self, *, action_id: str, logical_time: datetime, created_at: datetime,
                                trace_id: str, causation_id: str, correlation_id: str):
        """Close every active reply-later commitment bound to one terminal receipt.

        Delivered receipts fulfill; failed/cancelled/expired receipts break;
        unknown receipts release rather than silently leaving a promise open.
        """
        projection = self._ledger.project()
        action = next((item for item in projection.actions if item.action_id == action_id), None)
        if action is None or action.state not in {"delivered", "failed", "cancelled", "expired", "unknown"}:
            return None
        receipt = next((item for item in reversed(projection.execution_receipts) if item.action_id == action_id and item.is_terminal), None)
        if receipt is None:
            return None
        target = next((item for item in projection.commitments if item.values.status in {"open", "due"}
            and item.values.fulfillment_contract.contract_kind == "execution_receipt"
            and item.values.fulfillment_contract.expected_action_id == action_id), None)
        if target is None:
            return None
        operation = "fulfill" if action.state == "delivered" else ("break" if action.state in {"failed", "cancelled", "expired"} else "release")
        reason = "evidence_satisfied" if operation == "fulfill" else ("authoritative_failure" if operation == "break" else "precondition_failed")
        if operation == "release":
            receipt_event_id = (
                f"event:trigger:settlement:{receipt.provider}:{receipt.source_event_id}:execution-receipt"
            )
            receipt_event = next(
                (item for item in projection.committed_world_event_refs if item.event_id == receipt_event_id),
                None,
            )
            if receipt_event is None:
                raise RuntimeError("reply-later terminal receipt has no committed evidence event")
            evidence = EvidenceRef(ref_id=receipt_event.event_id, evidence_type="committed_world_event",
                claim_purpose="conversation_continuity", source_world_revision=receipt_event.world_revision,
                immutable_hash=receipt_event.payload_hash)
        else:
            evidence = EvidenceRef(ref_id=receipt.receipt_id, evidence_type="settled_external_result",
                claim_purpose="conversation_continuity", immutable_hash=_digest(receipt.model_dump(mode="json")))
        return self._record_terminal_transition(
            projection=projection,
            target=target,
            operation=operation,
            reason=reason,
            evidence=evidence,
            settlement_key=receipt.receipt_id,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )

    def release_interrupted_action(
        self,
        *,
        action_id: str,
        observation_event_id: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ):
        """Release an interrupted deferred promise from exact committed input.

        This is a recovery seam, not a social decision seam: the Action must
        already be durably cancelled and the later Observation must already be
        committed.  Replaying it joins the same terminal transition.
        """
        projection = self._ledger.project()
        action = next((item for item in projection.actions if item.action_id == action_id), None)
        target = next(
            (
                item
                for item in projection.commitments
                if item.values.status in {"open", "due"}
                and item.values.fulfillment_contract.contract_kind == "execution_receipt"
                and item.values.fulfillment_contract.expected_action_id == action_id
            ),
            None,
        )
        if target is None:
            return None
        if action is None or action.state != "cancelled":
            raise ValueError("interrupted commitment release requires its cancelled Action")
        located = self._ledger.lookup_event_commit(observation_event_id)
        if located is None or located[0].event_type != "ObservationRecorded":
            raise ValueError("interrupted commitment release requires a committed Observation")
        observation = Observation.model_validate_json(located[0].payload_json)
        source = next(
            (
                item
                for item in projection.message_observations
                if item.observation_id == observation.observation_id
            ),
            None,
        )
        if (
            source is None
            or source.event_payload_hash != located[0].payload_hash
            or source.world_revision != located[1].world_revision
        ):
            raise ValueError("interrupted commitment release source binding is invalid")
        evidence = EvidenceRef(
            ref_id=source.observation_id,
            evidence_type="observed_message",
            claim_purpose="conversation_continuity",
            source_world_revision=source.world_revision,
            immutable_hash=source.event_payload_hash,
        )
        return self._record_terminal_transition(
            projection=projection,
            target=target,
            operation="release",
            reason="user_withdrew",
            evidence=evidence,
            settlement_key=observation_event_id,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )

    def _record_terminal_transition(
        self,
        *,
        projection,
        target: CommitmentProjection,
        operation: str,
        reason: str,
        evidence: EvidenceRef,
        settlement_key: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ):
        proposal_id = "proposal:reply-later-terminal:" + _digest([self._ledger.world_id, target.commitment_id, settlement_key])
        acceptance_id = "acceptance:reply-later-terminal:" + _digest([self._ledger.world_id, target.commitment_id, settlement_key])
        change_id = "change:reply-later-terminal:" + _digest([target.commitment_id, settlement_key])
        transition_id = "transition:reply-later-terminal:" + _digest([target.commitment_id, settlement_key])
        mutation_event_id = "event:reply-later:terminal:" + _digest([self._ledger.world_id, target.commitment_id, settlement_key])
        values = target.values.model_copy(update={"source_evidence_refs": (*target.values.source_evidence_refs, evidence),
            "status": {"fulfill": "fulfilled", "break": "broken", "release": "released"}[operation],
            "settlement_evidence_ref": evidence.ref_id, "settlement_reason_code": reason})
        after = target.model_copy(update={"entity_revision": target.entity_revision + 1, "values": values,
            "updated_at": logical_time, "origin": CommitmentOrigin(change_id=change_id, transition_id=transition_id,
                policy_refs=target.origin.policy_refs, accepted_event_ref=mutation_event_id)})
        raw = {"change_id": change_id, "transition_id": transition_id, "expected_entity_revision": target.entity_revision,
            "evidence_refs": (*target.values.source_evidence_refs, evidence), "policy_refs": target.origin.policy_refs,
            "acceptance_id": acceptance_id, "proposal_id": proposal_id, "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64, "operation": operation, "commitment_before": target, "commitment_after": after}
        raw["accepted_change_hash"] = commitment_mutation_hash(raw)
        payload = CommitmentChangedPayload.model_validate(raw)
        proposal = CommitmentProposalProjection(proposal_id=proposal_id, proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:commitment.1", transition_kind=operation, change_id=change_id,
            transition_id=transition_id, evaluated_world_revision=projection.world_revision,
            expected_entity_revision=target.entity_revision, proposed_change_hash=payload.accepted_change_hash,
            evidence_refs=payload.evidence_refs, policy_refs=target.origin.policy_refs,
            proposed_mutation=CommitmentProposedMutation(event_type={"fulfill": "PrivateCommitmentFulfilled", "break": "PrivateCommitmentBroken", "release": "PrivateCommitmentReleased"}[operation],
                payload_json=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
        common = dict(schema_version="world-v2.1", world_id=self._ledger.world_id, logical_time=logical_time,
            created_at=created_at, actor=self._actor, source=self._source, trace_id=trace_id, correlation_id=correlation_id)
        proposal_event = WorldEvent.from_payload(**common, event_id="event:reply-later:terminal-proposal:" + _digest([proposal_id]),
            event_type="ProposalRecorded", causation_id=causation_id,
            idempotency_key=domain_idempotency_key(event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=proposal.model_dump(mode="json")) or proposal_id,
            payload=proposal.model_dump(mode="json"))
        cursor = ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)
        prior = self._ledger.lookup_event_commit(proposal_event.event_id)
        if prior is None:
            self._ledger.commit_at_cursor((proposal_event,), expected_cursor=cursor, commit_id="reply-later:terminal-proposal:" + _digest([proposal_id]))
        elif prior[0] != proposal_event:
            raise ValueError("reply-later terminal proposal conflicts with durable content")
        current = self._ledger.project()
        acceptance = {"acceptance_id": acceptance_id, "status": "accepted", "proposal_id": proposal_id,
            "evaluated_world_revision": projection.world_revision, "accepted_change_id": change_id, "accepted_change_hash": payload.accepted_change_hash}
        acceptance_event = WorldEvent.from_payload(**common, event_id="event:reply-later:terminal-acceptance:" + _digest([proposal_id]),
            event_type="AcceptanceRecorded", causation_id=proposal_event.event_id,
            idempotency_key=domain_idempotency_key(event_type="AcceptanceRecorded", world_id=self._ledger.world_id, payload=acceptance) or acceptance_id, payload=acceptance)
        mutation_event = WorldEvent.from_payload(**common, event_id=mutation_event_id,
            event_type=proposal.proposed_mutation.event_type, causation_id=acceptance_event.event_id,
            idempotency_key=domain_idempotency_key(event_type=proposal.proposed_mutation.event_type, world_id=self._ledger.world_id, payload=payload.model_dump(mode="json")) or mutation_event_id,
            payload=payload.model_dump(mode="json"))
        prior = self._ledger.lookup_event_commit(acceptance_event.event_id)
        if prior is not None:
            return prior[1]
        cursor = ProjectionCursor(world_revision=current.world_revision, deliberation_revision=current.deliberation_revision, ledger_sequence=current.ledger_sequence)
        return self._ledger.commit_at_cursor((acceptance_event, mutation_event), expected_cursor=cursor,
            commit_id="reply-later:terminal:" + _digest([proposal_id]))


__all__ = ["DeferredReplyRuntime", "ReplyLaterCommand"]
