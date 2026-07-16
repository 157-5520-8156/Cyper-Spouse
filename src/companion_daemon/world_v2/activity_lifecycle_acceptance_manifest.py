"""Closed authority record for one accepted life-ecology activity transition.

The activity-lifecycle lane must not reuse a user-observed activity command as
evidence for a scheduler wake.  This manifest is the narrow, self-hashing
record that binds the independently audited lifecycle proposal to the one
accepted activity effect selected from its frozen opening catalog.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION = "activity-lifecycle-acceptance.1"

ActivityLifecycleEffectEventType = Literal[
    "ActivityStarted",
    "ActivityPaused",
    "ActivityResumed",
    "ActivityCompleted",
    "ActivityAbandoned",
]


def canonical_activity_lifecycle_acceptance_value_hash(value: object) -> str:
    """Hash JSON material with the canonical representation used by the ledger."""

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def canonical_activity_lifecycle_acceptance_manifest_hash(value: dict[str, object]) -> str:
    """Derive the manifest hash while excluding the self-referential field."""

    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION)
    return canonical_activity_lifecycle_acceptance_value_hash(material)


class ActivityLifecycleAcceptanceManifest(FrozenModel):
    """Complete authority binding for one accepted life-ecology activity effect."""

    manifest_version: Literal["activity-lifecycle-acceptance.1"] = (
        ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION
    )
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1, max_length=256)
    acceptance_event_ref: str = Field(min_length=1, max_length=512)
    acceptance_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ecology_trigger_id: str = Field(min_length=1, max_length=256)
    wake_event_ref: str = Field(min_length=1, max_length=512)
    wake_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=256)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    opening_token: str = Field(min_length=1, max_length=512)
    effect_event_id: str = Field(min_length=1, max_length=512)
    effect_event_type: ActivityLifecycleEffectEventType
    effect_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def self_hash_is_exact(self) -> "ActivityLifecycleAcceptanceManifest":
        expected = canonical_activity_lifecycle_acceptance_manifest_hash(
            self.model_dump(mode="json")
        )
        if self.manifest_hash != expected:
            raise ValueError("activity lifecycle acceptance manifest hash is invalid")
        return self


def build_activity_lifecycle_acceptance_manifest(
    **values: object,
) -> ActivityLifecycleAcceptanceManifest:
    """Build a closed, self-hashing manifest without caller-supplied hash authority."""

    material = {
        "manifest_version": ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_activity_lifecycle_acceptance_manifest_hash(material)
    return ActivityLifecycleAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION",
    "ActivityLifecycleAcceptanceManifest",
    "ActivityLifecycleEffectEventType",
    "build_activity_lifecycle_acceptance_manifest",
    "canonical_activity_lifecycle_acceptance_manifest_hash",
    "canonical_activity_lifecycle_acceptance_value_hash",
]
