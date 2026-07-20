"""Proposal → Acceptance → Action vertical for normal Media v2 continuations."""

from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass
import hashlib
import json

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .media_continuation_acceptance_manifest import (
    build_media_continuation_acceptance_manifest,
    canonical_media_continuation_hash,
    media_continuation_event_identity,
)
from .media_execution_runtime import MediaExecutionRuntime
from .media_v2 import (
    MediaPlanRecordedPayload,
    MediaRenderArtifactRecordedPayload,
    artifact_continuation_trigger_id,
    continuation_trigger_id,
    media_digest,
)
from .proposal_envelope import (
    CanonicalTypedPayload,
    ContinuationProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from .schemas import (
    Action,
    ClaimLease,
    CommitResult,
    ProjectionCursor,
    ProviderMediaGrantBinding,
    TriggerProcess,
    WorldEvent,
)


_REGISTRY_DIGEST = hashlib.sha256(b"media-continuation-acceptance.1").hexdigest()


class MediaContinuationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MediaContinuationActionPolicy:
    actor: str
    owner_id: str
    grant: ProviderMediaGrantBinding
    account_id: str
    amount_limit: int

    def __post_init__(self) -> None:
        if not self.actor or not self.owner_id or not self.account_id or self.amount_limit < 0:
            raise ValueError("media continuation Action policy is invalid")


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _event_id(role: str, stable: str) -> str:
    return "event:media-continuation:" + role + ":" + media_digest(
        {"role": role, "stable": stable}
    )


def _identity(event_type: str, world_id: str, payload: dict[str, object]) -> str:
    identity = domain_idempotency_key(
        event_type=event_type, world_id=world_id, payload=payload
    )
    if identity is None:
        return media_continuation_event_identity(
            event_type=event_type, world_id=world_id, payload=payload
        )
    return identity


def _proposal_id(
    *, world_id: str, trigger: TriggerProcess, source_hash: str,
    evaluated_world_revision: int,
) -> str:
    return "proposal:media-continuation:" + media_digest(
        {
            "world_id": world_id,
            "trigger_id": trigger.trigger_id,
            "source_evidence_ref": trigger.source_evidence_ref,
            "source_hash": source_hash,
            "evaluated_world_revision": evaluated_world_revision,
        }
    )


class MediaContinuationRuntime:
    """Persist and accept deterministic continuations without provider work."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        execution: MediaExecutionRuntime,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        self._ledger = ledger
        self._execution = execution
        self._issuer = batch_issuer

    def propose(
        self,
        *,
        trigger_id: str,
        actor: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> tuple[ContinuationProposal, CommitResult | None]:
        projection = self._ledger.project()
        trigger = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id), None
        )
        if trigger is None or trigger.process_kind != "media_continuation":
            raise MediaContinuationError("media continuation trigger is unavailable")
        source_event, source_revision = self._exact_source(trigger)
        proposal = self._build_proposal(
            projection=projection,
            trigger=trigger,
            source_event=source_event,
            source_revision=source_revision,
        )
        proposal_event_id = _event_id("proposal", proposal.proposal_id)
        existing = self._ledger.lookup_event_commit(proposal_event_id)
        if existing is not None:
            recorded = ContinuationProposal.model_validate_json(existing[0].payload_json)
            if recorded != proposal:
                raise MediaContinuationError("deterministic continuation proposal bytes diverged")
            return recorded, None
        if trigger.state != "open":
            raise MediaContinuationError("new continuation proposal requires an open trigger")
        payload = proposal.model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=proposal_event_id,
            event_type="ProposalRecorded",
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=logical_time,
            actor=actor,
            source="world-v2:media-continuation",
            trace_id=trace_id,
            causation_id=source_event.event_id,
            correlation_id=correlation_id,
            idempotency_key=_identity("ProposalRecorded", self._ledger.world_id, payload),
            payload=payload,
        )
        commit = self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=_cursor(projection),
            commit_id="commit:media-continuation-proposal:"
            + media_digest({"proposal": proposal.proposal_id}),
        )
        return proposal, commit

    def accept(
        self,
        *,
        trigger_id: str,
        actor: str,
        owner_id: str,
        grant: ProviderMediaGrantBinding,
        account_id: str,
        amount_limit: int,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> Action:
        projection = self._ledger.project()
        trigger = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id), None
        )
        if trigger is None or trigger.process_kind != "media_continuation":
            raise MediaContinuationError("media continuation trigger is unavailable")
        source_event, source_revision = self._exact_source(trigger)
        joined = self._join_accepted_action(trigger=trigger, projection=projection)
        if joined is not None:
            return joined
        if trigger.state != "open":
            raise MediaContinuationError("non-open continuation lacks accepted completion proof")
        proposal_id = _proposal_id(
            world_id=self._ledger.world_id,
            trigger=trigger,
            source_hash=source_event.payload_hash,
            evaluated_world_revision=projection.world_revision,
        )
        proposal_event_id = _event_id("proposal", proposal_id)
        located = self._ledger.lookup_event_commit(proposal_event_id)
        if located is None:
            raise MediaContinuationError("continuation Acceptance requires recorded Proposal")
        proposal_event, _proposal_commit = located
        proposal = ContinuationProposal.model_validate_json(proposal_event.payload_json)
        if proposal.evaluated_world_revision != projection.world_revision:
            raise MediaContinuationError("continuation Proposal is stale")
        self._validate_proposal(
            proposal=proposal,
            trigger=trigger,
            source_event=source_event,
            source_revision=source_revision,
        )
        action_kind = proposal.action_intents[0].kind
        existing = next(
            (
                item
                for item in projection.actions
                if item.kind == action_kind
                and item.intent_ref == proposal.proposed_changes[0].target_id
            ),
            None,
        )
        if existing is not None:
            raise MediaContinuationError(
                "media Action exists without this continuation Acceptance authority"
            )
        target_id = proposal.proposed_changes[0].target_id
        if proposal.continuation_step == "plan_to_render":
            reservation, action = self._execution.prepare_render_authorization(
                plan_id=target_id, actor=actor, grant=grant, account_id=account_id,
                amount_limit=amount_limit, logical_time=logical_time,
                trace_id=trace_id, correlation_id=correlation_id,
            )
        else:
            reservation, action = self._execution.prepare_inspection_authorization(
                artifact_id=target_id, actor=actor, grant=grant, account_id=account_id,
                amount_limit=amount_limit, logical_time=logical_time,
                trace_id=trace_id, correlation_id=correlation_id,
            )
        intent = proposal.action_intents[0]
        if (
            action.kind != intent.kind
            or action.target != intent.target
            or action.payload_ref != intent.payload_ref
            or action.payload_hash != intent.payload_hash
        ):
            raise MediaContinuationError("materialized Action diverges from continuation intent")

        attempt_id = "attempt:media-continuation:" + media_digest(
            {"trigger": trigger.trigger_id, "proposal": proposal.proposal_id}
        )
        lease = ClaimLease(
            owner_id=owner_id,
            attempt_id=attempt_id,
            acquired_at=logical_time,
            expires_at=logical_time + timedelta(minutes=5),
        )
        claimed = trigger.model_copy(
            update={"state": "claimed", "claim_lease": lease, "attempt_ids": (*trigger.attempt_ids, attempt_id)}
        )
        claim_payload = {"process": claimed.model_dump(mode="json")}
        reservation_payload = {"reservation": reservation.model_dump(mode="json")}
        action_payload = {"action": action.model_dump(mode="json")}
        completion_payload = {
            "trigger_id": trigger.trigger_id,
            "owner_id": owner_id,
            "attempt_id": attempt_id,
            "completed_at": logical_time.isoformat(),
            "runtime_outcome_ref": action.action_id,
        }
        acceptance_id = "acceptance:media-continuation:" + media_digest(
            {"proposal": proposal.proposal_id, "change": proposal.proposed_changes[0].change_id}
        )
        event_ids = {
            "acceptance": _event_id("acceptance", acceptance_id),
            "claim": _event_id("claim", attempt_id),
            "reservation": _event_id("reservation", reservation.reservation_id),
            "action": _event_id("action", action.action_id),
            "completion": _event_id("completion", trigger.trigger_id),
        }
        manifest = build_media_continuation_acceptance_manifest(
            acceptance_id=acceptance_id,
            acceptance_event_ref=event_ids["acceptance"],
            proposal_id=proposal.proposal_id,
            proposal_event_ref=proposal_event.event_id,
            proposal_event_payload_hash=proposal_event.payload_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            continuation_step=proposal.continuation_step,
            trigger_id=trigger.trigger_id,
            source_evidence_ref=source_event.event_id,
            source_evidence_payload_hash=source_event.payload_hash,
            accepted_change_id=proposal.proposed_changes[0].change_id,
            accepted_change_hash=proposal.proposed_changes[0].payload.payload_hash.removeprefix("sha256:"),
            authorized_action_id=action.action_id,
            authorized_action_kind=action.kind,
            authorized_intent_ref=action.intent_ref,
            authorized_payload_ref=action.payload_ref,
            authorized_payload_hash=action.payload_hash,
            claim_event_ref=event_ids["claim"],
            claim_payload_hash=canonical_media_continuation_hash(claim_payload),
            reservation_event_ref=event_ids["reservation"],
            reservation_payload_hash=canonical_media_continuation_hash(reservation_payload),
            action_event_ref=event_ids["action"],
            action_payload_hash=canonical_media_continuation_hash(action_payload),
            completion_event_ref=event_ids["completion"],
            completion_payload_hash=canonical_media_continuation_hash(completion_payload),
        )
        common = dict(
            schema_version="world-v2.1", world_id=self._ledger.world_id,
            logical_time=logical_time, created_at=logical_time, actor=actor,
            source="world-v2:media-continuation", trace_id=trace_id,
            correlation_id=correlation_id,
        )
        definitions = (
            ("AcceptanceRecorded", manifest.model_dump(mode="json"), "acceptance", proposal_event.event_id),
            ("TriggerProcessClaimed", claim_payload, "claim", event_ids["acceptance"]),
            ("BudgetReserved", reservation_payload, "reservation", event_ids["claim"]),
            ("ActionAuthorized", action_payload, "action", event_ids["reservation"]),
            ("TriggerProcessCompleted", completion_payload, "completion", event_ids["action"]),
        )
        events = tuple(
            WorldEvent.from_payload(
                **common,
                event_id=event_ids[role], event_type=event_type,
                causation_id=causation_id,
                idempotency_key=_identity(event_type, self._ledger.world_id, payload),
                payload=payload,
            )
            for event_type, payload, role, causation_id in definitions
        )
        batch = self._issuer.issue(
            world_id=self._ledger.world_id,
            expected_cursor=_cursor(projection),
            events=events,
            manifest_hash=manifest.manifest_hash,
            registry_digest=_REGISTRY_DIGEST,
            commit_id="commit:media-continuation-acceptance:"
            + media_digest({"manifest": manifest.manifest_hash}),
        )
        self._ledger.commit_accepted(batch, expected_cursor=_cursor(projection))
        return action

    def _exact_source(self, trigger: TriggerProcess) -> tuple[WorldEvent, int]:
        if trigger.source_evidence_ref is None:
            raise MediaContinuationError("continuation trigger has no source evidence")
        located = self._ledger.lookup_event_commit(trigger.source_evidence_ref)
        if located is None:
            raise MediaContinuationError("continuation source event is unavailable")
        event, commit = located
        if event.world_id != self._ledger.world_id:
            raise MediaContinuationError("continuation source belongs to another world")
        return event, commit.world_revision

    def _build_proposal(self, *, projection, trigger, source_event, source_revision) -> ContinuationProposal:
        if source_event.event_type == "MediaPlanRecorded":
            result = MediaPlanRecordedPayload.model_validate_json(source_event.payload_json)
            plan = next((item for item in projection.media_plans if item.plan_id == result.plan.plan_id), None)
            if plan is None or plan != result.plan or trigger.trigger_id != continuation_trigger_id(plan):
                raise MediaContinuationError("plan continuation lacks exact settled plan evidence")
            step, target_id, artifact_ref, payload_ref, payload_hash, action_kind, action_target = (
                "plan_to_render", plan.plan_id, None, plan.plan_payload_ref,
                plan.plan_payload_hash, "media_render", "provider:media-renderer",
            )
            opportunity_ref, plan_ref = plan.opportunity_id, plan.plan_id
            evidence_kind = "settled_world_event"
        elif source_event.event_type == "MediaRenderArtifactRecorded":
            result = MediaRenderArtifactRecordedPayload.model_validate_json(source_event.payload_json)
            artifact = next(
                (item for item in projection.media_artifacts if item.artifact_id == result.artifact.artifact_id), None
            )
            plan = next(
                (item for item in projection.media_plans if item.plan_id == result.artifact.plan_id), None
            )
            if (
                artifact is None or artifact != result.artifact or plan is None
                or trigger.trigger_id != artifact_continuation_trigger_id(artifact)
            ):
                raise MediaContinuationError("inspection continuation lacks exact settled artifact evidence")
            step, target_id, artifact_ref, payload_ref, payload_hash, action_kind, action_target = (
                "render_to_inspect", artifact.artifact_id, artifact.artifact_id,
                artifact.artifact_ref, artifact.artifact_hash,
                "media_inspection", "provider:media-inspector",
            )
            opportunity_ref, plan_ref = plan.opportunity_id, plan.plan_id
            evidence_kind = "settled_external_result"
        else:
            raise MediaContinuationError("unsupported media continuation source")
        proposal_id = _proposal_id(
            world_id=self._ledger.world_id, trigger=trigger,
            source_hash=source_event.payload_hash,
            evaluated_world_revision=projection.world_revision,
        )
        evidence = ProposalEvidenceRef(
            ref_id=source_event.event_id,
            evidence_kind=evidence_kind,
            source_world_revision=source_revision,
            immutable_hash="sha256:" + source_event.payload_hash,
        )
        workflow_step_id = "media-step:" + media_digest(
            {"trigger": trigger.trigger_id, "step": step}
        )
        typed_payload = CanonicalTypedPayload.from_value(
            payload_schema="media_continuation.v1",
            value={
                "workflow_step_id": workflow_step_id,
                "opportunity_ref": opportunity_ref,
                "plan_ref": plan_ref,
                "artifact_ref": artifact_ref,
                "inspection_ref": None,
                "next_action_payload_hash": payload_hash,
            },
        )
        change_id = "change:media-continuation:" + media_digest(
            {"proposal": proposal_id, "step": step}
        )
        return ContinuationProposal(
            proposal_id=proposal_id,
            trigger_ref=trigger.trigger_id,
            evaluated_world_revision=projection.world_revision,
            evidence_refs=(evidence,),
            proposed_changes=(TypedChange(
                change_id=change_id, kind="media_continuation", target_id=target_id,
                transition=step, evidence_refs=(source_event.event_id,), payload=typed_payload,
            ),),
            action_intents=(ProposalActionIntent(
                intent_id="intent:media-continuation:" + media_digest({"change": change_id}),
                kind=action_kind, layer="media_action", target=action_target,
                payload_ref=payload_ref, payload_hash=payload_hash,
                causal_change_id=change_id,
            ),),
            confidence=10_000,
            brief_rationale="Continue exact settled Media v2 work without changing semantics.",
            workflow_kind="media_continuation",
            upstream_result_refs=(source_event.event_id,),
            continuation_step=step,
        )

    def _validate_proposal(self, *, proposal, trigger, source_event, source_revision) -> None:
        rebuilt = self._build_proposal(
            projection=self._ledger.project(), trigger=trigger,
            source_event=source_event, source_revision=source_revision,
        )
        if proposal != rebuilt:
            raise MediaContinuationError("recorded continuation Proposal no longer matches authority")

    def _join_accepted_action(self, *, trigger: TriggerProcess, projection) -> Action | None:
        if trigger.state != "terminal":
            return None
        from .media_continuation_acceptance_manifest import (
            MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION,
            MediaContinuationAcceptanceManifest,
        )

        proposal_ids = {
            item.proposal_id
            for item in projection.proposal_revisions
            if item.trigger_ref == trigger.trigger_id
        }
        decisions = tuple(
            item for item in projection.acceptance_decisions
            if item.proposal_id in proposal_ids and item.status == "accepted"
        )
        if len(decisions) != 1 or decisions[0].acceptance_id is None:
            raise MediaContinuationError("completed continuation lacks one Acceptance decision")
        located = self._ledger.lookup_event_commit(
            _event_id("acceptance", decisions[0].acceptance_id)
        )
        if located is not None:
            raw = located[0].payload()
            if (
                raw.get("manifest_version") != MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION
                or raw.get("trigger_id") != trigger.trigger_id
            ):
                raise MediaContinuationError("accepted continuation manifest identity diverged")
            manifest = MediaContinuationAcceptanceManifest.model_validate(raw, strict=True)
            action_located = self._ledger.lookup_event_commit(manifest.action_event_ref)
            if action_located is None or action_located[0].payload_hash != manifest.action_payload_hash:
                raise MediaContinuationError("accepted continuation Action proof is unavailable")
            action = Action.model_validate_json(
                json.dumps(
                    action_located[0].payload().get("action"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            projected = next(
                (item for item in projection.actions if item.action_id == action.action_id), None
            )
            if projected is None:
                raise MediaContinuationError("accepted continuation Action is absent from projection")
            return projected
        raise MediaContinuationError("completed continuation lacks Acceptance manifest")


class MediaContinuationWorker:
    """Drain one deterministic normal continuation; reruns join durable identity."""

    def __init__(
        self, *, runtime: MediaContinuationRuntime, ledger: LedgerPort,
        render_policy: MediaContinuationActionPolicy | None = None,
        inspection_policy: MediaContinuationActionPolicy | None = None,
    ) -> None:
        if (render_policy is None) != (inspection_policy is None):
            raise ValueError("render and inspection continuation policies must be injected together")
        self._runtime, self._ledger = runtime, ledger
        self._render_policy, self._inspection_policy = render_policy, inspection_policy

    def drain_once(
        self, *, actor: str | None = None, owner_id: str | None = None,
        grant: ProviderMediaGrantBinding | None = None,
        account_id: str | None = None, amount_limit: int | None = None, logical_time: datetime,
        trace_id: str, correlation_id: str,
    ) -> str | None:
        projection = self._ledger.project()
        trigger = next(
            (
                item for item in projection.trigger_processes
                if item.process_kind == "media_continuation" and item.state == "open"
            ),
            None,
        )
        if trigger is None:
            return None
        source = self._ledger.lookup_event_commit(trigger.source_evidence_ref or "")
        step = (
            "plan_to_render"
            if source is not None and source[0].event_type == "MediaPlanRecorded"
            else "render_to_inspect"
        )
        policy = self._render_policy if step == "plan_to_render" else self._inspection_policy
        if policy is not None:
            actor, owner_id, grant, account_id, amount_limit = (
                policy.actor, policy.owner_id, policy.grant,
                policy.account_id, policy.amount_limit,
            )
        if (
            actor is None or owner_id is None or grant is None
            or account_id is None or amount_limit is None
        ):
            raise MediaContinuationError("media continuation worker lacks Action policy")
        self._runtime.propose(
            trigger_id=trigger.trigger_id, actor=actor, logical_time=logical_time,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        action = self._runtime.accept(
            trigger_id=trigger.trigger_id, actor=actor, owner_id=owner_id, grant=grant,
            account_id=account_id, amount_limit=amount_limit, logical_time=logical_time,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        return action.action_id


__all__ = [
    "MediaContinuationActionPolicy", "MediaContinuationError",
    "MediaContinuationRuntime", "MediaContinuationWorker",
]
