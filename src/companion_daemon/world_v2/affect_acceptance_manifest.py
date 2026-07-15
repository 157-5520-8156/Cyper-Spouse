"""Closed accepted-manifest value for one persisted Affect proposal.

The Affect vertical is intentionally separate from the inert generic accepted
effect compiler.  It binds one already typed Affect proposal to exactly one
accepted affect mutation; callers cannot provide an arbitrary event sequence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


AFFECT_ACCEPTANCE_MANIFEST_VERSION = "affect-acceptance.1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def canonical_affect_acceptance_value_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def canonical_affect_acceptance_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", AFFECT_ACCEPTANCE_MANIFEST_VERSION)
    return canonical_affect_acceptance_value_hash(material)


class AffectAcceptanceManifest(FrozenModel):
    """A self-hashing, complete authority record for one accepted Affect proposal."""

    manifest_version: Literal["affect-acceptance.1"] = AFFECT_ACCEPTANCE_MANIFEST_VERSION
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutation_event_id: str = Field(min_length=1, max_length=512)
    mutation_event_type: Literal[
        "AffectEpisodeOpened",
        "AffectEpisodeUpdated",
        "AffectEpisodeResolved",
        "AffectEpisodeSuperseded",
        "AffectBaselineAdjusted",
    ]
    mutation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def self_hash_is_exact(self) -> AffectAcceptanceManifest:
        expected = canonical_affect_acceptance_manifest_hash(self.model_dump(mode="json"))
        if self.manifest_hash != expected:
            raise ValueError("affect acceptance manifest hash is invalid")
        return self


def build_affect_acceptance_manifest(**values: object) -> AffectAcceptanceManifest:
    material = {
        "manifest_version": AFFECT_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_affect_acceptance_manifest_hash(material)
    return AffectAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "AFFECT_ACCEPTANCE_MANIFEST_VERSION",
    "AffectAcceptanceManifest",
    "build_affect_acceptance_manifest",
    "canonical_affect_acceptance_manifest_hash",
    "canonical_affect_acceptance_value_hash",
]
