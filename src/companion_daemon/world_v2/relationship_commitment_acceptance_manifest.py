"""Closed accepted manifest for one explicit relationship commitment."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


RELATIONSHIP_COMMITMENT_ACCEPTANCE_MANIFEST_VERSION = (
    "relationship-commitment-acceptance.1"
)
RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_VERSION = (
    "relationship-commitment-acceptance-policy.1"
)


def canonical_relationship_commitment_acceptance_value_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST = (
    canonical_relationship_commitment_acceptance_value_hash(
        {
            "contract": RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_VERSION,
            "mutation_event_types": ("RelationshipCommitmentAccepted",),
            "transition_kinds": ("commitment",),
            "authorship": "typed_character_output_only",
            "requires_trigger_completion": False,
        }
    )
)


def canonical_relationship_commitment_acceptance_manifest_hash(
    value: dict[str, object],
) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault(
        "manifest_version", RELATIONSHIP_COMMITMENT_ACCEPTANCE_MANIFEST_VERSION
    )
    return canonical_relationship_commitment_acceptance_value_hash(material)


class RelationshipCommitmentAcceptanceManifest(FrozenModel):
    """Self-hashing authority record for one committed stage transition."""

    manifest_version: Literal["relationship-commitment-acceptance.1"] = (
        RELATIONSHIP_COMMITMENT_ACCEPTANCE_MANIFEST_VERSION
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
    mutation_event_type: Literal["RelationshipCommitmentAccepted"]
    mutation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def self_hash_is_exact(self) -> RelationshipCommitmentAcceptanceManifest:
        if self.policy_digest != RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST:
            raise ValueError(
                "relationship commitment acceptance policy digest is not installed"
            )
        if self.manifest_hash != canonical_relationship_commitment_acceptance_manifest_hash(
            self.model_dump(mode="json")
        ):
            raise ValueError("relationship commitment acceptance manifest hash is invalid")
        return self


def build_relationship_commitment_acceptance_manifest(
    **values: object,
) -> RelationshipCommitmentAcceptanceManifest:
    material = {
        "manifest_version": RELATIONSHIP_COMMITMENT_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_relationship_commitment_acceptance_manifest_hash(
        material
    )
    return RelationshipCommitmentAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "RELATIONSHIP_COMMITMENT_ACCEPTANCE_MANIFEST_VERSION",
    "RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_DIGEST",
    "RELATIONSHIP_COMMITMENT_ACCEPTANCE_POLICY_VERSION",
    "RelationshipCommitmentAcceptanceManifest",
    "build_relationship_commitment_acceptance_manifest",
    "canonical_relationship_commitment_acceptance_manifest_hash",
    "canonical_relationship_commitment_acceptance_value_hash",
]
