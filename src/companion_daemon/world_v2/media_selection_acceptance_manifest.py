"""Self-hashing acceptance record for one P1 public media selection.

The manifest names every materialized effect but intentionally excludes its
own ``manifest_hash`` from the digest.  This keeps the ledger event hash and
the manifest's proof non-circular while binding the opportunity, reservation,
and planning Action to one persisted deliberation proposal.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION = "media-selection-acceptance.1"


def canonical_media_selection_value_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def canonical_media_selection_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION)
    return canonical_media_selection_value_hash(material)


class MediaSelectionAcceptanceManifest(FrozenModel):
    manifest_version: Literal["media-selection-acceptance.1"] = (
        MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION
    )
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1, max_length=256)
    acceptance_event_ref: str = Field(min_length=1, max_length=512)
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(min_length=1, max_length=256)
    expected_candidate_revision: int = Field(ge=1)
    candidate_authority_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    opportunity_event_id: str = Field(min_length=1, max_length=512)
    opportunity_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    opportunity_id: str = Field(min_length=1, max_length=256)
    snapshot_ref: str = Field(min_length=1, max_length=512)
    snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reservation_event_id: str = Field(min_length=1, max_length=512)
    reservation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_event_id: str = Field(min_length=1, max_length=512)
    action_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_self_hash(self) -> "MediaSelectionAcceptanceManifest":
        if self.manifest_hash != canonical_media_selection_manifest_hash(self.model_dump(mode="json")):
            raise ValueError("media selection acceptance manifest hash is invalid")
        return self


def build_media_selection_acceptance_manifest(
    **values: object,
) -> MediaSelectionAcceptanceManifest:
    material = {
        "manifest_version": MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_media_selection_manifest_hash(material)
    return MediaSelectionAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION",
    "MediaSelectionAcceptanceManifest",
    "build_media_selection_acceptance_manifest",
    "canonical_media_selection_manifest_hash",
    "canonical_media_selection_value_hash",
]
