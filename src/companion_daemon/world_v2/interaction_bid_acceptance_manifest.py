"""Closed accepted-manifest contract for one delivered-media interaction bid."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION = "interaction-bid-acceptance.1"


def canonical_interaction_bid_value_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical_interaction_bid_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION)
    return canonical_interaction_bid_value_hash(material)


class InteractionBidAcceptanceManifest(FrozenModel):
    manifest_version: Literal["interaction-bid-acceptance.1"] = INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_id: str = Field(min_length=1)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    delivery_id: str = Field(min_length=1)
    delivery_event_ref: str = Field(min_length=1)
    delivery_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deliberation_trigger_id: str = Field(min_length=1)
    bid_event_id: str = Field(min_length=1)
    bid_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_self_hash(self) -> "InteractionBidAcceptanceManifest":
        if self.manifest_hash != canonical_interaction_bid_manifest_hash(self.model_dump(mode="json")):
            raise ValueError("interaction bid acceptance manifest hash is invalid")
        return self


def build_interaction_bid_acceptance_manifest(**values: object) -> InteractionBidAcceptanceManifest:
    material = {"manifest_version": INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION, "status": "accepted", **values}
    material["manifest_hash"] = canonical_interaction_bid_manifest_hash(material)
    return InteractionBidAcceptanceManifest.model_validate(material, strict=True)


__all__ = ["INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION", "InteractionBidAcceptanceManifest", "build_interaction_bid_acceptance_manifest", "canonical_interaction_bid_manifest_hash", "canonical_interaction_bid_value_hash"]
