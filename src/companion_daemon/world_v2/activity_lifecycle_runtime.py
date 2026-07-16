"""Durable proposal and accepted-effect lane for Life Ecology activities."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .activity_lifecycle_acceptance_manifest import (
    build_activity_lifecycle_acceptance_manifest,
    canonical_activity_lifecycle_acceptance_value_hash,
)
from .activity_lifecycle_contract import ActivityLifecycleProposalRecordedPayload
from .activity_lifecycle_proposal import ActivityLifecycleProposal
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .schemas import CommitResult, ProjectionCursor, WorldEvent


_CONTRACT = "activity-lifecycle-runtime.1"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class ActivityLifecycleRuntimeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"activity_lifecycle_runtime.{code}"
        super().__init__(self.code)


def proposal_event_id(*, proposal_id: str) -> str:
    return "event:activity-lifecycle-proposal:" + _digest(
        {"contract": _CONTRACT, "proposal_id": proposal_id}
    )


def acceptance_event_id(*, world_id: str, proposal_id: str, change_id: str) -> str:
    return "event:activity-lifecycle-acceptance:" + _digest(
        {"world_id": world_id, "proposal_id": proposal_id, "change_id": change_id}
    )


def effect_event_id(*, world_id: str, proposal_id: str, change_id: str) -> str:
    return "event:activity-lifecycle-effect:" + _digest(
        {"world_id": world_id, "proposal_id": proposal_id, "change_id": change_id}
    )


class ActivityLifecycleProposalRecord:
    """Result of appending one typed proposal audit at an exact cursor."""

    __slots__ = ("proposal", "proposal_event_ref", "proposal_event_payload_hash", "commit")

    def __init__(self, *, proposal: ActivityLifecycleProposal, proposal_event_ref: str,
                 proposal_event_payload_hash: str, commit: CommitResult) -> None:
        self.proposal = proposal
        self.proposal_event_ref = proposal_event_ref
        self.proposal_event_payload_hash = proposal_event_payload_hash
        self.commit = commit


class ActivityLifecycleProposalRecorder:
    """Append only a reducer-verified proposal; it cannot accept an effect."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    def record(
        self,
        *,
        cursor: ProjectionCursor,
        proposal: ActivityLifecycleProposal,
        actor: str,
        source: str,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> ActivityLifecycleProposalRecord:
        if (
            proposal.evaluated_world_revision != cursor.world_revision
            or proposal.evaluated_deliberation_revision != cursor.deliberation_revision
            or proposal.evaluated_ledger_sequence != cursor.ledger_sequence
        ):
            raise ActivityLifecycleRuntimeError("proposal_cursor_mismatch")
        projection = self._ledger.project_at(cursor)
        if projection.logical_time is None:
            raise ActivityLifecycleRuntimeError("logical_time_missing")
        event_id = proposal_event_id(proposal_id=proposal.proposal_id)
        payload = proposal.model_dump(mode="json")
        key = domain_idempotency_key(
            event_type="ActivityLifecycleProposalRecorded", world_id=self._ledger.world_id, payload=payload
        )
        if key is None:
            raise ActivityLifecycleRuntimeError("proposal_identity_missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type="ActivityLifecycleProposalRecorded",
            logical_time=projection.logical_time,
            created_at=created_at,
            actor=actor,
            source=source,
            trace_id=trace_id,
            causation_id=proposal.wake_event_ref,
            correlation_id=correlation_id,
            idempotency_key=key,
            payload=payload,
        )
        commit = self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=cursor,
            commit_id="commit:activity-lifecycle-proposal:" + _digest(
                {"cursor": cursor.model_dump(mode="json"), "proposal_id": proposal.proposal_id}
            ),
        )
        return ActivityLifecycleProposalRecord(
            proposal=proposal,
            proposal_event_ref=event.event_id,
            proposal_event_payload_hash=event.payload_hash,
            commit=commit,
        )


class _PinnedActivityLifecycleProposal:
    __slots__ = ("__proposal", "__event", "__cursor", "__issuer")

    def __init__(self, *, proposal: ActivityLifecycleProposalRecordedPayload, event: WorldEvent,
                 cursor: ProjectionCursor, issuer: object) -> None:
        self.__proposal = proposal
        self.__event = event
        self.__cursor = cursor
        self.__issuer = issuer

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self) -> object:
        raise TypeError("pinned activity lifecycle proposal cannot be serialized")


class ActivityLifecycleProposalAuthorityReader:
    """Pin a persisted proposal and re-check replay-local source authority."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self._issuer = object()

    def owns(self, handle: _PinnedActivityLifecycleProposal) -> bool:
        return type(handle) is _PinnedActivityLifecycleProposal and handle.issued_by(self._issuer)

    def pin(
        self, *, cursor: ProjectionCursor, proposal_event_ref: str
    ) -> _PinnedActivityLifecycleProposal:
        projection = self._ledger.project_at(cursor)
        located = self._ledger.lookup_event_commit(proposal_event_ref)
        if located is None:
            raise ActivityLifecycleRuntimeError("proposal_event_missing")
        event, commit = located
        if (
            event.world_id != self._ledger.world_id
            or event.event_type != "ActivityLifecycleProposalRecorded"
            or commit.world_revision != cursor.world_revision
            or commit.deliberation_revision != cursor.deliberation_revision
            or commit.ledger_sequence != cursor.ledger_sequence
        ):
            raise ActivityLifecycleRuntimeError("proposal_event_unavailable")
        proposal = ActivityLifecycleProposalRecordedPayload.model_validate_json(event.payload_json)
        if (
            proposal.evaluated_world_revision != cursor.world_revision
            or proposal.evaluated_deliberation_revision + 1 != cursor.deliberation_revision
            or proposal.evaluated_ledger_sequence + 1 != cursor.ledger_sequence
        ):
            raise ActivityLifecycleRuntimeError("proposal_stale")
        if proposal.proposal_id not in projection.proposal_ids:
            raise ActivityLifecycleRuntimeError("proposal_not_persisted")
        if not any(
            item.trigger_id == proposal.ecology_trigger_id
            and item.process_kind == "life_ecology"
            and item.state == "claimed"
            and item.claim_lease is not None
            and item.source_evidence_ref == proposal.wake_event_ref
            for item in projection.trigger_processes
        ):
            raise ActivityLifecycleRuntimeError("ecology_trigger_not_claimed")
        plan = next((item for item in projection.plans if item.plan_id == proposal.plan_id), None)
        if plan is None or plan.entity_revision != proposal.expected_plan_revision:
            raise ActivityLifecycleRuntimeError("plan_not_current")
        return _PinnedActivityLifecycleProposal(
            proposal=proposal, event=event, cursor=cursor, issuer=self._issuer
        )


class ActivityLifecycleAtomicRecorder:
    """Create the only accepted batch that may materialize a scheduler transition."""

    def __init__(self, *, reader: ActivityLifecycleProposalAuthorityReader,
                 batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self._reader = reader
        self._batch_issuer = batch_issuer

    def prepare_batch(
        self,
        *,
        handle: _PinnedActivityLifecycleProposal,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> AcceptedLedgerBatchHandle:
        if not self._reader.owns(handle):
            raise ActivityLifecycleRuntimeError("proposal_handle_untrusted")
        proposal = object.__getattribute__(handle, "_PinnedActivityLifecycleProposal__proposal")
        proposal_event = object.__getattribute__(handle, "_PinnedActivityLifecycleProposal__event")
        cursor = object.__getattribute__(handle, "_PinnedActivityLifecycleProposal__cursor")
        if proposal.evaluated_world_revision != cursor.world_revision:
            raise ActivityLifecycleRuntimeError("proposal_stale")
        acceptance_id = "acceptance:activity-lifecycle:" + _digest(
            {"world_id": proposal_event.world_id, "proposal_id": proposal.proposal_id,
             "change_id": proposal.change_id}
        )
        accepted_event_id = acceptance_event_id(
            world_id=proposal_event.world_id, proposal_id=proposal.proposal_id, change_id=proposal.change_id
        )
        mutation_event_id = effect_event_id(
            world_id=proposal_event.world_id, proposal_id=proposal.proposal_id, change_id=proposal.change_id
        )
        effect_payload = {
            "change_id": proposal.change_id,
            "transition_id": proposal.transition_id,
            "expected_entity_revision": proposal.expected_plan_revision,
            "evidence_refs": [item.model_dump(mode="json") for item in proposal.evidence_refs],
            "policy_refs": ("policy:activity-lifecycle.1",),
            "plan_id": proposal.plan_id,
            "transitioned_at": logical_time.isoformat(),
            "reason_ref": proposal_event.event_id,
            "acceptance_id": acceptance_id,
            "activity_lifecycle_proposal_id": proposal.proposal_id,
            "accepted_change_hash": proposal.proposed_change_hash,
        }
        manifest = build_activity_lifecycle_acceptance_manifest(
            acceptance_id=acceptance_id,
            acceptance_event_ref=accepted_event_id,
            proposal_id=proposal.proposal_id,
            proposal_event_ref=proposal_event.event_id,
            proposal_event_payload_hash=proposal_event.payload_hash,
            evaluated_world_revision=cursor.world_revision,
            accepted_change_id=proposal.change_id,
            accepted_change_hash=proposal.proposed_change_hash,
            ecology_trigger_id=proposal.ecology_trigger_id,
            wake_event_ref=proposal.wake_event_ref,
            wake_event_payload_hash=proposal.wake_event_payload_hash,
            catalog_version=proposal.catalog_version,
            catalog_hash=proposal.catalog_hash,
            opening_token=proposal.opening_token,
            effect_event_id=mutation_event_id,
            effect_event_type=proposal.effect_event_type,
            effect_event_payload_hash=canonical_activity_lifecycle_acceptance_value_hash(effect_payload),
            policy_digest=proposal.policy_digest,
        )
        acceptance_payload = manifest.model_dump(mode="json")
        identities = (
            domain_idempotency_key(event_type="AcceptanceRecorded", world_id=proposal_event.world_id,
                                   payload=acceptance_payload),
            domain_idempotency_key(event_type=proposal.effect_event_type, world_id=proposal_event.world_id,
                                   payload=effect_payload),
        )
        if any(item is None for item in identities):
            raise ActivityLifecycleRuntimeError("effect_identity_missing")
        common = {
            "schema_version": "world-v2.1", "world_id": proposal_event.world_id,
            "logical_time": logical_time, "created_at": created_at, "actor": actor,
            "source": source, "trace_id": trace_id, "correlation_id": correlation_id,
        }
        events = (
            WorldEvent.from_payload(**common, event_id=accepted_event_id,
                                    event_type="AcceptanceRecorded", causation_id=proposal_event.event_id,
                                    idempotency_key=identities[0], payload=acceptance_payload),
            WorldEvent.from_payload(**common, event_id=mutation_event_id,
                                    event_type=proposal.effect_event_type, causation_id=accepted_event_id,
                                    idempotency_key=identities[1], payload=effect_payload),
        )
        if events[1].payload_hash != manifest.effect_event_payload_hash:
            raise ActivityLifecycleRuntimeError("effect_hash_mismatch")
        return self._batch_issuer.issue(
            world_id=proposal_event.world_id,
            expected_cursor=cursor,
            events=events,
            manifest_hash=manifest.manifest_hash,
            registry_digest=proposal.policy_digest,
            commit_id="commit:activity-lifecycle-acceptance:" + _digest(
                {"cursor": cursor.model_dump(mode="json"), "manifest_hash": manifest.manifest_hash}
            ),
        )


class ActivityLifecycleAcceptanceRuntime:
    """Production-facing pin/accept API over the accepted-ledger capability."""

    def __init__(self, *, ledger: LedgerPort, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.ledger = ledger
        self._reader = ActivityLifecycleProposalAuthorityReader(ledger=ledger)
        self._recorder = ActivityLifecycleAtomicRecorder(reader=self._reader, batch_issuer=batch_issuer)

    def pin_proposal(self, *, cursor: ProjectionCursor,
                     proposal_event_ref: str) -> _PinnedActivityLifecycleProposal:
        return self._reader.pin(cursor=cursor, proposal_event_ref=proposal_event_ref)

    def accept(
        self, *, handle: _PinnedActivityLifecycleProposal, actor: str, source: str,
        logical_time: datetime, created_at: datetime, trace_id: str, correlation_id: str,
    ) -> CommitResult:
        cursor = object.__getattribute__(handle, "_PinnedActivityLifecycleProposal__cursor")
        batch = self._recorder.prepare_batch(
            handle=handle, actor=actor, source=source, logical_time=logical_time,
            created_at=created_at, trace_id=trace_id, correlation_id=correlation_id,
        )
        return self.ledger.commit_accepted(batch, expected_cursor=cursor)


__all__ = [
    "ActivityLifecycleAcceptanceRuntime",
    "ActivityLifecycleAtomicRecorder",
    "ActivityLifecycleProposalAuthorityReader",
    "ActivityLifecycleProposalRecord",
    "ActivityLifecycleProposalRecorder",
    "ActivityLifecycleRuntimeError",
    "acceptance_event_id",
    "effect_event_id",
    "proposal_event_id",
]
