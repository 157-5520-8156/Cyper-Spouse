"""Dedicated, source-bound authority records for post-delivery interaction bids.

An interaction bid is intentionally a small *private social intention*, not a
message, thread, or side effect.  It can only be born from the immutable
``MediaDeliveryShared`` event; previews and transport receipts are not usable
substitutes.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schema_core import FrozenModel
from .schemas import EvidenceRef, InteractionBidProjection


class InteractionBidProposalRecordedPayload(FrozenModel):
    interaction_bid_proposal_id: str = Field(min_length=1)
    decision_proposal_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    bid_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    delivery_id: str = Field(min_length=1)
    delivery_event_ref: str = Field(min_length=1)
    delivery_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deliberation_trigger_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    hoped_response: str = Field(min_length=1)
    pressure_bp: int = Field(ge=0, le=10_000)
    audience_ref: str = Field(min_length=1)
    due_at: datetime | None = None
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=0, le=10_000)
    proposed_change_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def hash_binds_every_bid_value(self) -> "InteractionBidProposalRecordedPayload":
        if self.proposed_change_hash != interaction_bid_mutation_hash(self):
            raise ValueError("interaction bid proposed change hash does not match fields")
        return self


class InteractionBidOpenedPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)
    bid: InteractionBidProjection

    @model_validator(mode="after")
    def opened_bid_matches_acceptance(self) -> "InteractionBidOpenedPayload":
        if self.accepted_change_hash != interaction_bid_mutation_hash(self):
            raise ValueError("interaction bid accepted change hash does not match fields")
        origin = self.bid.origin
        if (
            origin.change_id != self.change_id
            or origin.transition_id != self.transition_id
            or origin.acceptance_id != self.acceptance_id
            or origin.proposal_id != self.proposal_id
            or origin.evaluated_world_revision != self.evaluated_world_revision
        ):
            raise ValueError("interaction bid origin does not match accepted authority")
        return self


def interaction_bid_mutation_hash(
    payload: (
        InteractionBidProposalRecordedPayload
        | InteractionBidOpenedPayload
        | Mapping[str, Any]
    ),
) -> str:
    raw = (
        payload.model_dump(mode="json") if isinstance(payload, FrozenModel)
        else to_jsonable_python(dict(payload))
    )
    bid = raw.get("bid")
    if isinstance(bid, dict):
        raw = {**raw, **bid}
    material = {
        "change_id": raw.get("change_id"),
        "bid_id": raw.get("bid_id"),
        "delivery_id": raw.get("delivery_id"),
        "delivery_event_ref": raw.get("delivery_event_ref"),
        "delivery_event_payload_hash": raw.get("delivery_event_payload_hash"),
        "deliberation_trigger_id": raw.get("deliberation_trigger_id"),
        "goal": raw.get("goal"),
        "hoped_response": raw.get("hoped_response"),
        "pressure_bp": raw.get("pressure_bp"),
        "audience_ref": raw.get("audience_ref"),
        "due_at": raw.get("due_at"),
        "evidence_refs": raw.get("evidence_refs"),
    }
    encoded = json.dumps(material, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


INTERACTION_BID_PAYLOAD_MODELS = {
    "InteractionBidProposalRecorded": InteractionBidProposalRecordedPayload,
    "InteractionBidOpened": InteractionBidOpenedPayload,
}


__all__ = [
    "INTERACTION_BID_PAYLOAD_MODELS", "InteractionBidOpenedPayload",
    "InteractionBidProposalRecordedPayload", "interaction_bid_mutation_hash",
]
