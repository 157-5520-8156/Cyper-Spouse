"""Compile an audited decision into a delivery-bound InteractionBid proposal."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .event_identity import domain_idempotency_key
from .interaction_bid_events import (
    InteractionBidProposalRecordedPayload,
    interaction_bid_mutation_hash,
)
from .ledger import LedgerPort
from .schema_core import EvidenceRef, FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


_CONTRACT = "interaction-bid-proposal-compiler.1"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class InteractionBidProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"interaction_bid_proposal_compiler.{code}"
        super().__init__(self.code)


class InteractionBidProposalCompilation(FrozenModel):
    status: Literal["candidate_recorded"]
    source_proposal_id: str
    typed_proposal_id: str
    commit: CommitResult


class InteractionBidProposalCompiler:
    """The only bridge from generic audited deliberation to a private bid."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self._reader = DecisionProposalAuthorityReader(ledger=ledger)

    def record(self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str) -> InteractionBidProposalCompilation:
        authority = self._reader.read(self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id))
        change, source_event, source_commit, trigger = self._verify(authority=authority, cursor=cursor)
        raw = change.payload.value()
        identity = _digest({"contract": _CONTRACT, "source_proposal_event": authority.audit.event_ref, "source_change": change.change_id})
        typed_id = f"proposal:interaction-bid-compiled:{identity}"
        evidence = EvidenceRef(
            ref_id=source_event.event_id, evidence_type="committed_world_event",
            claim_purpose="conversation_continuity", source_world_revision=source_commit.world_revision,
            immutable_hash=source_event.payload_hash,
        )
        material: dict[str, object] = {
            "interaction_bid_proposal_id": typed_id,
            "decision_proposal_id": authority.proposal.proposal_id,
            "change_id": change.change_id,
            "bid_id": raw["bid_id"],
            "evaluated_world_revision": cursor.world_revision,
            "delivery_id": source_event.payload()["delivery"]["delivery_id"],
            "delivery_event_ref": source_event.event_id,
            "delivery_event_payload_hash": source_event.payload_hash,
            "deliberation_trigger_id": trigger.trigger_id,
            "goal": raw["goal"], "hoped_response": raw["hoped_response"],
            "pressure_bp": raw["pressure"], "audience_ref": raw["audience"],
            "due_at": (
                datetime.fromisoformat(raw["due"].replace("Z", "+00:00"))
                if isinstance(raw.get("due"), str) else raw.get("due")
            ), "evidence_refs": (evidence,),
            "confidence_bp": authority.proposal.confidence,
        }
        material["proposed_change_hash"] = interaction_bid_mutation_hash(material)
        payload = InteractionBidProposalRecordedPayload.model_validate(material)
        event_payload = payload.model_dump(mode="json")
        key = domain_idempotency_key(event_type="InteractionBidProposalRecorded", world_id=world_id, payload=event_payload)
        if key is None:
            raise InteractionBidProposalCompilerError("proposal_identity_missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1", event_id=f"event:interaction-bid-proposal-compiled:{identity}",
            world_id=world_id, event_type="InteractionBidProposalRecorded",
            logical_time=source_event.logical_time, created_at=source_event.created_at,
            actor="worker:interaction-bid-proposal-compiler", source=_CONTRACT,
            trace_id=source_event.trace_id, causation_id=authority.audit.event_ref,
            correlation_id=source_event.correlation_id, idempotency_key=key, payload=event_payload,
        )
        commit = self._ledger.commit(
            [event], expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            commit_id="commit:interaction-bid-proposal-compiler:" + _digest({"cursor": cursor.model_dump(mode="json"), "proposal": typed_id}),
        )
        return InteractionBidProposalCompilation(status="candidate_recorded", source_proposal_id=proposal_id, typed_proposal_id=typed_id, commit=commit)

    def _verify(self, *, authority, cursor: ProjectionCursor):
        changes = tuple(item for item in authority.proposal.proposed_changes if item.kind == "interaction_bid_transition")
        if len(changes) != 1 or len(authority.proposal.proposed_changes) != 1:
            raise InteractionBidProposalCompilerError("change_count_invalid")
        change = changes[0]
        if change.transition != "open":
            raise InteractionBidProposalCompilerError("transition_invalid")
        raw = change.payload.value()
        if change.target_id != raw.get("bid_id") or change.expected_entity_revision != 0:
            raise InteractionBidProposalCompilerError("bid_identity_invalid")
        source_event = self._event(authority.audit.trigger_ref)
        if source_event.event_type != "MediaDeliveryShared":
            raise InteractionBidProposalCompilerError("source_not_delivered_media")
        source_commit = self._ledger.lookup_event_commit(source_event.event_id)
        assert source_commit is not None
        event, commit = source_commit
        delivery = event.payload().get("delivery")
        if not isinstance(delivery, dict) or not isinstance(delivery.get("delivery_id"), str):
            raise InteractionBidProposalCompilerError("delivery_payload_invalid")
        projection = self._ledger.project_at(cursor)
        if not any(item.delivery_id == delivery["delivery_id"] for item in projection.media_deliveries):
            raise InteractionBidProposalCompilerError("delivery_not_projected")
        trigger = next((item for item in projection.trigger_processes if item.process_kind == "media_delivery_interaction" and item.state == "claimed" and item.claim_lease is not None and item.source_evidence_ref == source_event.event_id), None)
        if trigger is None:
            raise InteractionBidProposalCompilerError("source_trigger_not_claimed")
        if len(change.evidence_refs) != 1 or tuple(change.evidence_refs) != (source_event.event_id,):
            raise InteractionBidProposalCompilerError("change_evidence_invalid")
        source = next((item for item in authority.proposal.evidence_refs if item.ref_id == source_event.event_id), None)
        if (
            source is None or source.evidence_kind != "committed_world_event"
            or source.source_world_revision != commit.world_revision
            or source.immutable_hash.removeprefix("sha256:") != source_event.payload_hash
        ):
            raise InteractionBidProposalCompilerError("source_evidence_invalid")
        if any(item.bid_id == raw["bid_id"] for item in projection.interaction_bids):
            raise InteractionBidProposalCompilerError("bid_exists")
        # Model output may not create a bid after the source trigger is stale.
        if authority.proposal.evaluated_world_revision != cursor.world_revision:
            raise InteractionBidProposalCompilerError("proposal_stale")
        return change, source_event, commit, trigger

    def _event(self, event_id: str) -> WorldEvent:
        located = self._ledger.lookup_event_commit(event_id)
        if located is None:
            raise InteractionBidProposalCompilerError("source_event_missing")
        return located[0]


__all__ = ["InteractionBidProposalCompilation", "InteractionBidProposalCompiler", "InteractionBidProposalCompilerError"]
