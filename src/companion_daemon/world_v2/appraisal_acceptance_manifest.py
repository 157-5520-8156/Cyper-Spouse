"""Closed accepted-manifest value for one persisted Appraisal proposal.

This is intentionally a narrow production seam.  It does not attempt to turn
the old generic ``DecisionProposal`` compiler into a second authority path;
instead it binds the already typed, source-provenanced appraisal proposal to
its one mutation and its claimed-trigger completion.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


APPRAISAL_ACCEPTANCE_MANIFEST_VERSION = "appraisal-acceptance.1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def canonical_appraisal_acceptance_value_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def canonical_appraisal_acceptance_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", APPRAISAL_ACCEPTANCE_MANIFEST_VERSION)
    return canonical_appraisal_acceptance_value_hash(material)


class AppraisalAcceptanceManifest(FrozenModel):
    """A self-hashing, complete authority record for one accepted appraisal."""

    manifest_version: Literal["appraisal-acceptance.1"] = APPRAISAL_ACCEPTANCE_MANIFEST_VERSION
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger_id: str = Field(min_length=1, max_length=256)
    mutation_event_id: str = Field(min_length=1, max_length=512)
    mutation_event_type: Literal[
        "AppraisalAccepted", "AppraisalContradicted", "AppraisalSuperseded"
    ]
    mutation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_event_id: str = Field(min_length=1, max_length=512)
    completion_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def self_hash_is_exact(self) -> AppraisalAcceptanceManifest:
        expected = canonical_appraisal_acceptance_manifest_hash(self.model_dump(mode="json"))
        if self.manifest_hash != expected:
            raise ValueError("appraisal acceptance manifest hash is invalid")
        return self


def build_appraisal_acceptance_manifest(**values: object) -> AppraisalAcceptanceManifest:
    material = {
        "manifest_version": APPRAISAL_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_appraisal_acceptance_manifest_hash(material)
    return AppraisalAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "APPRAISAL_ACCEPTANCE_MANIFEST_VERSION",
    "AppraisalAcceptanceManifest",
    "build_appraisal_acceptance_manifest",
    "canonical_appraisal_acceptance_manifest_hash",
    "canonical_appraisal_acceptance_value_hash",
]
