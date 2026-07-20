"""Closed accepted-manifest value for a relationship slow-variable adjustment.

Relationship signals and their later slow-variable adjustments deliberately
use different acceptance contracts.  A signal is an immutable observation;
an adjustment is a policy-mediated, replayable state transition.  Keeping the
manifest separate prevents an adjustment worker from accidentally reusing the
signal lane as a general relationship event writer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION = (
    "relationship-adjustment-acceptance.1"
)


def canonical_relationship_adjustment_acceptance_value_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def canonical_relationship_adjustment_acceptance_manifest_hash(
    value: dict[str, object],
) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault(
        "manifest_version", RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION
    )
    return canonical_relationship_adjustment_acceptance_value_hash(material)


class RelationshipAdjustmentAcceptanceManifest(FrozenModel):
    """Self-hashing authority record for one accepted ``adjust`` transition."""

    manifest_version: Literal["relationship-adjustment-acceptance.1"] = (
        RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION
    )
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutation_event_id: str = Field(min_length=1, max_length=512)
    mutation_event_type: Literal["RelationshipSlowVariableAdjusted"]
    mutation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def self_hash_is_exact(self) -> "RelationshipAdjustmentAcceptanceManifest":
        if self.manifest_hash != canonical_relationship_adjustment_acceptance_manifest_hash(
            self.model_dump(mode="json")
        ):
            raise ValueError("relationship adjustment acceptance manifest hash is invalid")
        return self


def build_relationship_adjustment_acceptance_manifest(
    **values: object,
) -> RelationshipAdjustmentAcceptanceManifest:
    material = {
        "manifest_version": RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_relationship_adjustment_acceptance_manifest_hash(
        material
    )
    return RelationshipAdjustmentAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION",
    "RelationshipAdjustmentAcceptanceManifest",
    "build_relationship_adjustment_acceptance_manifest",
    "canonical_relationship_adjustment_acceptance_manifest_hash",
    "canonical_relationship_adjustment_acceptance_value_hash",
]
