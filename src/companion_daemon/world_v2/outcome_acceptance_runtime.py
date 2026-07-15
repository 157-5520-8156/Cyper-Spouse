"""Closed production acceptance lane for compiled lived-world outcomes.

An Outcome proposal is deliberately not a host-writable settlement plan.  This
module pins the persisted compiler record at one cursor, re-verifies its
claimed observation trigger, and materializes the only accepted batch the
reducer understands: acceptance, settlement, then the NPC appraisal trigger.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .batch_invariants import appraisal_trigger_identity
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort, WorldLedger
from .life_events import OutcomeProposalRecordedPayload
from .outcome_acceptance_manifest import (
    build_outcome_acceptance_manifest,
    canonical_outcome_acceptance_value_hash,
)
from .schemas import CommitResult, ProjectionCursor, TriggerProcess, WorldEvent
from .sqlite_ledger import SQLiteWorldLedger


OUTCOME_ACCEPTANCE_POLICY_VERSION = "outcome-acceptance-policy.1"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


OUTCOME_ACCEPTANCE_POLICY_DIGEST = _digest(
    {
        "contract": OUTCOME_ACCEPTANCE_POLICY_VERSION,
        "events": (
            "AcceptanceRecorded",
            "WorldOccurrenceSettled",
            "TriggerProcessOpened:npc_world_appraisal",
        ),
        "requires_claimed_source_trigger": True,
    }
)


class OutcomeAcceptanceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"outcome_acceptance.{code}"
        super().__init__(self.code)


class PinnedOutcomeProposalAuthorityHandle:
    """Unserializable proof of one exact compiled Outcome proposal."""

    __slots__ = ("__proposal", "__proposal_event", "__trigger", "__cursor", "__issuer")

    def __init__(self, *, proposal: object, proposal_event: WorldEvent, trigger: TriggerProcess,
                 cursor: ProjectionCursor, issuer: object) -> None:
        self.__proposal = proposal
        self.__proposal_event = proposal_event
        self.__trigger = trigger
        self.__cursor = cursor
        self.__issuer = issuer

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self) -> object:
        raise TypeError("pinned Outcome proposal handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("pinned Outcome proposal handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("pinned Outcome proposal handles cannot be copied")


class OutcomeProposalAuthorityReader:
    """Deep read seam for compiler-owned, source-bound Outcome proposals."""

    __slots__ = ("__ledger", "__issuer")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self.__ledger = ledger
        self.__issuer = object()

    def owns(self, handle: PinnedOutcomeProposalAuthorityHandle) -> bool:
        return type(handle) is PinnedOutcomeProposalAuthorityHandle and handle.issued_by(self.__issuer)

    def pin(self, *, world_id: str, cursor: ProjectionCursor,
            proposal_id: str) -> PinnedOutcomeProposalAuthorityHandle:
        if world_id != self.__ledger.world_id:
            raise OutcomeAcceptanceError("authority_world_mismatch")
        projection = self.__ledger.project_at(cursor)
        proposal = next(
            (item for item in projection.outcome_proposals if item.outcome_proposal_id == proposal_id),
            None,
        )
        if proposal is None:
            raise OutcomeAcceptanceError("proposal_not_persisted")
        if proposal.evaluated_world_revision != cursor.world_revision:
            raise OutcomeAcceptanceError("proposal_stale")
        if not proposal.deliberation_trigger_id or not proposal.source_observation_id:
            raise OutcomeAcceptanceError("proposal_not_source_bound")
        trigger = next(
            (item for item in projection.trigger_processes if item.trigger_id == proposal.deliberation_trigger_id),
            None,
        )
        source_event_id = f"event:outcome-observation:{proposal.source_observation_id}"
        if (
            trigger is None
            or trigger.process_kind != "outcome_deliberation"
            or trigger.state != "claimed"
            or trigger.claim_lease is None
            or trigger.source_evidence_ref != source_event_id
        ):
            raise OutcomeAcceptanceError("source_trigger_not_claimed")
        source = self.__ledger.lookup_event_commit(source_event_id)
        if (
            source is None
            or source[0].event_type != "OutcomeObservationRecorded"
            or source[1].world_revision > cursor.world_revision
            or source[0].payload().get("observation", {}).get("occurrence_id") != proposal.occurrence_id
        ):
            raise OutcomeAcceptanceError("source_observation_unavailable")
        proposal_event = self._proposal_event(proposal=proposal, projection=projection)
        if proposal_event is None or proposal_event.event_type != "OutcomeProposalRecorded":
            raise OutcomeAcceptanceError("proposal_event_missing")
        # Event JSON preserves its original datetime spelling while a projection
        # normalizes UTC to ``Z``.  Rehydrate the event through the exact typed
        # contract before comparing semantic authority.
        recorded = OutcomeProposalRecordedPayload.model_validate_json(proposal_event.payload_json)
        if recorded.model_dump(mode="json") != proposal.model_dump(mode="json"):
            raise OutcomeAcceptanceError("proposal_event_mismatch")
        return PinnedOutcomeProposalAuthorityHandle(
            proposal=proposal,
            proposal_event=proposal_event,
            trigger=trigger,
            cursor=cursor,
            issuer=self.__issuer,
        )

    def _proposal_event(self, *, proposal, projection) -> WorldEvent | None:
        # Compiler event identity is derived from the immutable generic audit,
        # never from an unverified projection DTO.
        audit = next(
            (item for item in projection.proposal_audits if item.proposal_id == proposal.decision_proposal_id),
            None,
        )
        if audit is None:
            return None
        identity = _digest(
            {
                "contract": "outcome-proposal-compiler.1",
                "source_proposal_event": audit.event_ref,
                "source_change": proposal.change_id,
            }
        )
        located = self.__ledger.lookup_event_commit(f"event:outcome-proposal-compiled:{identity}")
        return located[0] if located is not None else None


def outcome_acceptance_event_id(*, world_id: str, proposal_id: str, change_id: str) -> str:
    return "event:outcome-acceptance:" + _digest(
        {"world_id": world_id, "proposal_id": proposal_id, "change_id": change_id}
    )


def outcome_settlement_event_id(*, world_id: str, proposal_id: str, change_id: str) -> str:
    return "event:outcome-settlement:" + _digest(
        {"world_id": world_id, "proposal_id": proposal_id, "change_id": change_id}
    )


def _npc_trigger_open_event_id(*, world_id: str, trigger_id: str) -> str:
    return "event:npc-world-appraisal-trigger-opened:" + _digest(
        {"world_id": world_id, "trigger_id": trigger_id}
    )


class OutcomeAtomicRecorder:
    """The sole materializer for accepted Outcome settlement batches."""

    __slots__ = ("__reader", "__batch_issuer")

    def __init__(self, *, proposal_reader: OutcomeProposalAuthorityReader,
                 batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.__reader = proposal_reader
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self, *, handle: PinnedOutcomeProposalAuthorityHandle, actor: str, source: str,
        logical_time: datetime, created_at: datetime, trace_id: str, correlation_id: str,
    ) -> AcceptedLedgerBatchHandle:
        if not self.__reader.owns(handle):
            raise OutcomeAcceptanceError("proposal_handle_untrusted")
        proposal = object.__getattribute__(handle, "_PinnedOutcomeProposalAuthorityHandle__proposal")
        proposal_event = object.__getattribute__(handle, "_PinnedOutcomeProposalAuthorityHandle__proposal_event")
        trigger = object.__getattribute__(handle, "_PinnedOutcomeProposalAuthorityHandle__trigger")
        cursor = object.__getattribute__(handle, "_PinnedOutcomeProposalAuthorityHandle__cursor")
        if proposal.evaluated_world_revision != cursor.world_revision:
            raise OutcomeAcceptanceError("proposal_stale")
        if logical_time >= proposal.expires_at:
            raise OutcomeAcceptanceError("proposal_expired")
        acceptance_id = "acceptance:outcome:" + _digest(
            {"world_id": proposal_event.world_id, "proposal_id": proposal.outcome_proposal_id,
             "change_id": proposal.change_id}
        )
        acceptance_event_id = outcome_acceptance_event_id(
            world_id=proposal_event.world_id, proposal_id=proposal.outcome_proposal_id,
            change_id=proposal.change_id,
        )
        settlement_event_id = outcome_settlement_event_id(
            world_id=proposal_event.world_id, proposal_id=proposal.outcome_proposal_id,
            change_id=proposal.change_id,
        )
        npc_trigger_id = appraisal_trigger_identity(proposal.occurrence_id, proposal.proposed_result_id)
        settlement_payload = {
            "change_id": proposal.change_id,
            "transition_id": "transition:outcome-settlement:" + _digest(
                {"proposal_id": proposal.outcome_proposal_id, "change_id": proposal.change_id}
            ),
            "expected_entity_revision": proposal.evaluated_entity_revision,
            "evidence_refs": [item.model_dump(mode="json") for item in proposal.evidence_refs],
            "policy_refs": ("policy:outcome-v1",),
            "acceptance_id": acceptance_id,
            "evaluated_world_revision": cursor.world_revision,
            "accepted_change_hash": proposal.proposed_change_hash,
            "occurrence_id": proposal.occurrence_id,
            "outcome_proposal_id": proposal.outcome_proposal_id,
            "candidate_result_ref": proposal.candidate_result_ref,
            "result_id": proposal.proposed_result_id,
            "observation_refs": proposal.observation_refs,
            "result_payload_ref": proposal.proposed_result_payload_ref,
            "result_payload_hash": proposal.proposed_result_payload_hash,
            "settled_at": logical_time.isoformat(),
            "appraisal_trigger_ref": npc_trigger_id,
        }
        trigger_process = TriggerProcess(
            trigger_id=npc_trigger_id,
            trigger_ref=npc_trigger_id,
            process_kind="npc_world_appraisal",
            source_evidence_ref=settlement_event_id,
            state="open",
        )
        trigger_payload = {"process": trigger_process.model_dump(mode="json")}
        manifest = build_outcome_acceptance_manifest(
            acceptance_id=acceptance_id,
            proposal_id=proposal.outcome_proposal_id,
            proposal_event_ref=proposal_event.event_id,
            proposal_event_payload_hash=proposal_event.payload_hash,
            evaluated_world_revision=cursor.world_revision,
            accepted_change_id=proposal.change_id,
            accepted_change_hash=proposal.proposed_change_hash,
            deliberation_trigger_id=trigger.trigger_id,
            settlement_event_id=settlement_event_id,
            settlement_payload_hash=canonical_outcome_acceptance_value_hash(settlement_payload),
            npc_appraisal_trigger_id=npc_trigger_id,
            npc_appraisal_trigger_event_id=_npc_trigger_open_event_id(
                world_id=proposal_event.world_id, trigger_id=npc_trigger_id
            ),
            npc_appraisal_trigger_payload_hash=canonical_outcome_acceptance_value_hash(trigger_payload),
            policy_digest=OUTCOME_ACCEPTANCE_POLICY_DIGEST,
        )
        common = {
            "schema_version": "world-v2.1", "world_id": proposal_event.world_id,
            "logical_time": logical_time, "created_at": created_at, "actor": actor,
            "source": source, "trace_id": trace_id, "correlation_id": correlation_id,
        }
        acceptance_payload = manifest.model_dump(mode="json")
        identities = (
            domain_idempotency_key(event_type="AcceptanceRecorded", world_id=proposal_event.world_id,
                                   payload=acceptance_payload),
            domain_idempotency_key(event_type="WorldOccurrenceSettled", world_id=proposal_event.world_id,
                                   payload=settlement_payload),
            domain_idempotency_key(event_type="TriggerProcessOpened", world_id=proposal_event.world_id,
                                   payload=trigger_payload),
        )
        if any(identity is None for identity in identities):
            raise OutcomeAcceptanceError("event_identity_missing")
        events = (
            WorldEvent.from_payload(**common, event_id=acceptance_event_id,
                event_type="AcceptanceRecorded", causation_id=proposal_event.event_id,
                idempotency_key=identities[0], payload=acceptance_payload),
            WorldEvent.from_payload(**common, event_id=settlement_event_id,
                event_type="WorldOccurrenceSettled", causation_id=acceptance_event_id,
                idempotency_key=identities[1], payload=settlement_payload),
            WorldEvent.from_payload(**common, event_id=manifest.npc_appraisal_trigger_event_id,
                event_type="TriggerProcessOpened", causation_id=settlement_event_id,
                idempotency_key=identities[2], payload=trigger_payload),
        )
        if events[1].payload_hash != manifest.settlement_payload_hash or (
            events[2].payload_hash != manifest.npc_appraisal_trigger_payload_hash
        ):
            raise OutcomeAcceptanceError("effect_hash_mismatch")
        return self.__batch_issuer.issue(
            world_id=proposal_event.world_id, expected_cursor=cursor, events=events,
            manifest_hash=manifest.manifest_hash, registry_digest=OUTCOME_ACCEPTANCE_POLICY_DIGEST,
            commit_id="commit:outcome-acceptance:" + _digest(
                {"world_id": proposal_event.world_id, "cursor": cursor.model_dump(mode="json"),
                 "manifest_hash": manifest.manifest_hash,
                 "events": tuple(event.model_dump(mode="json") for event in events)}
            ),
        )


class OutcomeAcceptanceRuntime:
    """Composition root for the isolated production Outcome acceptance lane."""

    __slots__ = ("ledger", "_reader", "_recorder")

    def __init__(self, *, ledger: LedgerPort, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.ledger = ledger
        self._reader = OutcomeProposalAuthorityReader(ledger=ledger)
        self._recorder = OutcomeAtomicRecorder(proposal_reader=self._reader, batch_issuer=batch_issuer)

    @classmethod
    def in_memory(cls, *, world_id: str) -> "OutcomeAcceptanceRuntime":
        issuer = AcceptedLedgerBatchIssuer()
        return cls(ledger=WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer),
                   batch_issuer=issuer)

    @classmethod
    def open(cls, *, path: Path, world_id: str) -> "OutcomeAcceptanceRuntime":
        issuer = AcceptedLedgerBatchIssuer()
        return cls(ledger=SQLiteWorldLedger(path=path, world_id=world_id,
                                            accepted_batch_issuer=issuer), batch_issuer=issuer)

    def close(self) -> None:
        close = getattr(self.ledger, "close", None)
        if close is not None:
            close()

    def pin_proposal(self, *, cursor: ProjectionCursor,
                     proposal_id: str) -> PinnedOutcomeProposalAuthorityHandle:
        return self._reader.pin(world_id=self.ledger.world_id, cursor=cursor, proposal_id=proposal_id)

    def accept(self, *, handle: PinnedOutcomeProposalAuthorityHandle, actor: str, source: str,
               logical_time: datetime, created_at: datetime, trace_id: str,
               correlation_id: str) -> CommitResult:
        cursor = object.__getattribute__(handle, "_PinnedOutcomeProposalAuthorityHandle__cursor")
        batch = self._recorder.prepare_batch(
            handle=handle, actor=actor, source=source, logical_time=logical_time,
            created_at=created_at, trace_id=trace_id, correlation_id=correlation_id,
        )
        return self.ledger.commit_accepted(batch, expected_cursor=cursor)

    def accept_runtime_owned(self, *, handle: PinnedOutcomeProposalAuthorityHandle,
                             actor: str, source: str) -> CommitResult:
        proposal_event = object.__getattribute__(handle, "_PinnedOutcomeProposalAuthorityHandle__proposal_event")
        return self.accept(handle=handle, actor=actor, source=source,
                           logical_time=proposal_event.logical_time,
                           created_at=proposal_event.created_at,
                           trace_id=proposal_event.trace_id,
                           correlation_id=proposal_event.correlation_id)


__all__ = [
    "OUTCOME_ACCEPTANCE_POLICY_DIGEST", "OUTCOME_ACCEPTANCE_POLICY_VERSION",
    "OutcomeAcceptanceError", "OutcomeAcceptanceRuntime", "OutcomeAtomicRecorder",
    "OutcomeProposalAuthorityReader", "PinnedOutcomeProposalAuthorityHandle",
    "outcome_acceptance_event_id", "outcome_settlement_event_id",
]
