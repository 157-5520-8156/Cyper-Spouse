"""Closed accepted-manifest contract for a source-bound outcome settlement."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


OUTCOME_ACCEPTANCE_MANIFEST_VERSION = "outcome-acceptance.1"


def canonical_outcome_acceptance_value_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def canonical_outcome_acceptance_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", OUTCOME_ACCEPTANCE_MANIFEST_VERSION)
    return canonical_outcome_acceptance_value_hash(material)


class OutcomeAcceptanceManifest(FrozenModel):
    """One exact proposal, settlement and NPC-appraisal continuation."""

    manifest_version: Literal["outcome-acceptance.1"] = OUTCOME_ACCEPTANCE_MANIFEST_VERSION
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deliberation_trigger_id: str = Field(min_length=1, max_length=256)
    settlement_event_id: str = Field(min_length=1, max_length=512)
    settlement_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    npc_appraisal_trigger_id: str = Field(min_length=1, max_length=256)
    npc_appraisal_trigger_event_id: str = Field(min_length=1, max_length=512)
    npc_appraisal_trigger_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def self_hash_is_exact(self) -> "OutcomeAcceptanceManifest":
        if self.manifest_hash != canonical_outcome_acceptance_manifest_hash(self.model_dump(mode="json")):
            raise ValueError("outcome acceptance manifest hash is invalid")
        return self


def build_outcome_acceptance_manifest(**values: object) -> OutcomeAcceptanceManifest:
    material = {"manifest_version": OUTCOME_ACCEPTANCE_MANIFEST_VERSION, "status": "accepted", **values}
    material["manifest_hash"] = canonical_outcome_acceptance_manifest_hash(material)
    return OutcomeAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "OUTCOME_ACCEPTANCE_MANIFEST_VERSION",
    "OutcomeAcceptanceManifest",
    "build_outcome_acceptance_manifest",
    "canonical_outcome_acceptance_manifest_hash",
    "canonical_outcome_acceptance_value_hash",
]
