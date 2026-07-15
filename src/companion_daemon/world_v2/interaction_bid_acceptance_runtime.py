"""Atomic accepted-effect capability for delivered-media InteractionBid proposals."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .interaction_bid_acceptance_manifest import (
    build_interaction_bid_acceptance_manifest,
    canonical_interaction_bid_value_hash,
)
from .interaction_bid_events import InteractionBidOpenedPayload
from .ledger import LedgerPort, WorldLedger
from .schemas import (
    CommitResult,
    InteractionBidOrigin,
    InteractionBidProjection,
    ProjectionCursor,
    WorldEvent,
)
from .sqlite_ledger import SQLiteWorldLedger


INTERACTION_BID_ACCEPTANCE_POLICY_VERSION = "interaction-bid-acceptance-policy.1"
INTERACTION_BID_ACCEPTANCE_POLICY_DIGEST = hashlib.sha256(json.dumps({
    "contract": INTERACTION_BID_ACCEPTANCE_POLICY_VERSION,
    "source": "MediaDeliveryShared", "effect": "InteractionBidOpened",
}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class InteractionBidAcceptanceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"interaction_bid_acceptance.{code}"
        super().__init__(self.code)


class PinnedInteractionBidProposalHandle:
    __slots__ = ("__proposal", "__proposal_event", "__cursor", "__issuer")
    def __init__(self, *, proposal: object, proposal_event: WorldEvent, cursor: ProjectionCursor, issuer: object) -> None:
        self.__proposal, self.__proposal_event, self.__cursor, self.__issuer = proposal, proposal_event, cursor, issuer
    def issued_by(self, issuer: object) -> bool: return self.__issuer is issuer
    def __reduce__(self):
        raise TypeError("pinned interaction bid handles cannot be serialized")

    def __copy__(self):
        raise TypeError("pinned interaction bid handles cannot be copied")

    def __deepcopy__(self, memo):
        del memo
        raise TypeError("pinned interaction bid handles cannot be copied")


class InteractionBidProposalAuthorityReader:
    __slots__ = ("__ledger", "__issuer")
    def __init__(self, *, ledger: LedgerPort) -> None:
        self.__ledger, self.__issuer = ledger, object()
    def owns(self, handle: PinnedInteractionBidProposalHandle) -> bool:
        return type(handle) is PinnedInteractionBidProposalHandle and handle.issued_by(self.__issuer)
    def pin(self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str) -> PinnedInteractionBidProposalHandle:
        if world_id != self.__ledger.world_id:
            raise InteractionBidAcceptanceError("authority_world_mismatch")
        projection = self.__ledger.project_at(cursor)
        proposal = next((item for item in projection.interaction_bid_proposals if item.interaction_bid_proposal_id == proposal_id), None)
        if proposal is None or proposal.evaluated_world_revision != cursor.world_revision:
            raise InteractionBidAcceptanceError("proposal_not_current")
        trigger = next((item for item in projection.trigger_processes if item.trigger_id == proposal.deliberation_trigger_id), None)
        source = self.__ledger.lookup_event_commit(proposal.delivery_event_ref)
        if (
            trigger is None or trigger.process_kind != "media_delivery_interaction" or trigger.state != "claimed" or trigger.claim_lease is None
            or trigger.source_evidence_ref != proposal.delivery_event_ref
            or source is None or source[0].event_type != "MediaDeliveryShared" or source[0].payload_hash != proposal.delivery_event_payload_hash
            or source[1].world_revision > cursor.world_revision
        ):
            raise InteractionBidAcceptanceError("delivery_source_unavailable")
        identity = _digest({"contract": "interaction-bid-proposal-compiler.1", "source_proposal_event": self._audit_ref(projection, proposal.decision_proposal_id), "source_change": proposal.change_id})
        proposal_event = self.__ledger.lookup_event_commit(f"event:interaction-bid-proposal-compiled:{identity}")
        if proposal_event is None or proposal_event[0].event_type != "InteractionBidProposalRecorded":
            raise InteractionBidAcceptanceError("proposal_event_missing")
        if proposal_event[0].payload() != proposal.model_dump(mode="json"):
            raise InteractionBidAcceptanceError("proposal_event_mismatch")
        return PinnedInteractionBidProposalHandle(proposal=proposal, proposal_event=proposal_event[0], cursor=cursor, issuer=self.__issuer)
    @staticmethod
    def _audit_ref(projection, proposal_id: str) -> str:
        audit = next((item for item in projection.proposal_audits if item.proposal_id == proposal_id), None)
        if audit is None:
            raise InteractionBidAcceptanceError("source_audit_missing")
        return audit.event_ref


class InteractionBidAtomicRecorder:
    __slots__ = ("__reader", "__batch_issuer")
    def __init__(self, *, proposal_reader: InteractionBidProposalAuthorityReader, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.__reader, self.__batch_issuer = proposal_reader, batch_issuer
    def prepare_batch(self, *, handle: PinnedInteractionBidProposalHandle, actor: str, source: str, logical_time: datetime, created_at: datetime, trace_id: str, correlation_id: str) -> AcceptedLedgerBatchHandle:
        if not self.__reader.owns(handle):
            raise InteractionBidAcceptanceError("proposal_handle_untrusted")
        proposal = object.__getattribute__(handle, "_PinnedInteractionBidProposalHandle__proposal")
        proposal_event = object.__getattribute__(handle, "_PinnedInteractionBidProposalHandle__proposal_event")
        cursor = object.__getattribute__(handle, "_PinnedInteractionBidProposalHandle__cursor")
        if proposal.evaluated_world_revision != cursor.world_revision:
            raise InteractionBidAcceptanceError("proposal_stale")
        acceptance_id = "acceptance:interaction-bid:" + _digest({"world_id": proposal_event.world_id, "proposal_id": proposal.interaction_bid_proposal_id, "change_id": proposal.change_id})
        acceptance_event_id = "event:interaction-bid-acceptance:" + _digest({"world_id": proposal_event.world_id, "proposal_id": proposal.interaction_bid_proposal_id})
        bid_event_id = "event:interaction-bid-opened:" + _digest({"world_id": proposal_event.world_id, "bid_id": proposal.bid_id})
        bid = InteractionBidProjection(
            bid_id=proposal.bid_id, delivery_id=proposal.delivery_id, delivery_event_ref=proposal.delivery_event_ref,
            delivery_event_payload_hash=proposal.delivery_event_payload_hash, deliberation_trigger_id=proposal.deliberation_trigger_id,
            goal=proposal.goal, hoped_response=proposal.hoped_response, pressure_bp=proposal.pressure_bp,
            audience_ref=proposal.audience_ref, due_at=proposal.due_at, evidence_refs=proposal.evidence_refs,
            opened_at=logical_time, origin=InteractionBidOrigin(
                acceptance_id=acceptance_id, proposal_id=proposal.interaction_bid_proposal_id, change_id=proposal.change_id,
                transition_id="transition:interaction-bid:" + _digest({"proposal": proposal.interaction_bid_proposal_id}),
                evaluated_world_revision=cursor.world_revision, policy_refs=("policy:interaction-bid-v1",),
            ),
        )
        opened_payload = InteractionBidOpenedPayload(
            change_id=proposal.change_id, transition_id=bid.origin.transition_id, acceptance_id=acceptance_id,
            proposal_id=proposal.interaction_bid_proposal_id, evaluated_world_revision=cursor.world_revision,
            accepted_change_hash=proposal.proposed_change_hash, bid=bid,
        ).model_dump(mode="json")
        manifest = build_interaction_bid_acceptance_manifest(
            acceptance_id=acceptance_id, proposal_id=proposal.interaction_bid_proposal_id,
            proposal_event_ref=proposal_event.event_id, proposal_event_payload_hash=proposal_event.payload_hash,
            evaluated_world_revision=cursor.world_revision, accepted_change_id=proposal.change_id,
            accepted_change_hash=proposal.proposed_change_hash, delivery_id=proposal.delivery_id,
            delivery_event_ref=proposal.delivery_event_ref, delivery_event_payload_hash=proposal.delivery_event_payload_hash,
            deliberation_trigger_id=proposal.deliberation_trigger_id, bid_event_id=bid_event_id,
            bid_payload_hash=canonical_interaction_bid_value_hash(opened_payload),
            policy_digest=INTERACTION_BID_ACCEPTANCE_POLICY_DIGEST,
        )
        common = {"schema_version": "world-v2.1", "world_id": proposal_event.world_id, "logical_time": logical_time, "created_at": created_at, "actor": actor, "source": source, "trace_id": trace_id, "correlation_id": correlation_id}
        acceptance_payload = manifest.model_dump(mode="json")
        keys = tuple(domain_idempotency_key(event_type=kind, world_id=proposal_event.world_id, payload=payload) for kind, payload in (("AcceptanceRecorded", acceptance_payload), ("InteractionBidOpened", opened_payload)))
        if any(key is None for key in keys):
            raise InteractionBidAcceptanceError("event_identity_missing")
        events = (
            WorldEvent.from_payload(**common, event_id=acceptance_event_id, event_type="AcceptanceRecorded", causation_id=proposal_event.event_id, idempotency_key=keys[0], payload=acceptance_payload),
            WorldEvent.from_payload(**common, event_id=bid_event_id, event_type="InteractionBidOpened", causation_id=acceptance_event_id, idempotency_key=keys[1], payload=opened_payload),
        )
        return self.__batch_issuer.issue(world_id=proposal_event.world_id, expected_cursor=cursor, events=events, manifest_hash=manifest.manifest_hash, registry_digest=INTERACTION_BID_ACCEPTANCE_POLICY_DIGEST, commit_id="commit:interaction-bid-acceptance:" + _digest({"cursor": cursor.model_dump(mode="json"), "manifest": manifest.manifest_hash}))


class InteractionBidAcceptanceRuntime:
    __slots__ = ("ledger", "_reader", "_recorder")
    def __init__(self, *, ledger: LedgerPort, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        self.ledger = ledger
        self._reader = InteractionBidProposalAuthorityReader(ledger=ledger)
        self._recorder = InteractionBidAtomicRecorder(
            proposal_reader=self._reader, batch_issuer=batch_issuer
        )
    @classmethod
    def in_memory(cls, *, world_id: str):
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer),
            batch_issuer=issuer,
        )
    @classmethod
    def open(cls, *, path: Path, world_id: str):
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=SQLiteWorldLedger(path=path, world_id=world_id, accepted_batch_issuer=issuer),
            batch_issuer=issuer,
        )
    def pin_proposal(self, *, cursor: ProjectionCursor, proposal_id: str) -> PinnedInteractionBidProposalHandle:
        return self._reader.pin(world_id=self.ledger.world_id, cursor=cursor, proposal_id=proposal_id)
    def accept(self, *, handle: PinnedInteractionBidProposalHandle, actor: str, source: str, logical_time: datetime, created_at: datetime, trace_id: str, correlation_id: str) -> CommitResult:
        cursor = object.__getattribute__(handle, "_PinnedInteractionBidProposalHandle__cursor")
        return self.ledger.commit_accepted(self._recorder.prepare_batch(handle=handle, actor=actor, source=source, logical_time=logical_time, created_at=created_at, trace_id=trace_id, correlation_id=correlation_id), expected_cursor=cursor)
    def accept_runtime_owned(self, *, handle: PinnedInteractionBidProposalHandle, actor: str, source: str) -> CommitResult:
        event = object.__getattribute__(handle, "_PinnedInteractionBidProposalHandle__proposal_event")
        return self.accept(handle=handle, actor=actor, source=source, logical_time=event.logical_time, created_at=event.created_at, trace_id=event.trace_id, correlation_id=event.correlation_id)


__all__ = ["INTERACTION_BID_ACCEPTANCE_POLICY_DIGEST", "InteractionBidAcceptanceError", "InteractionBidAcceptanceRuntime", "InteractionBidAtomicRecorder", "InteractionBidProposalAuthorityReader", "PinnedInteractionBidProposalHandle"]
