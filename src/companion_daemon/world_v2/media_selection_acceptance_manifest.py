"""Self-hashing acceptance records for source-bound media selections.

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
"""The closed, byte-compatible P1 public-media acceptance contract."""

MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION = "media-selection-acceptance.2"
"""The P3 recipient-scoped acceptance contract.

This is deliberately a separate wire version rather than optional fields on
``.1``.  Existing P1 manifest JSON (and therefore its self-hash) must remain
exactly stable while P3 binds the private authorization material.
"""

MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS = frozenset(
    {
        MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION,
        MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION,
    }
)


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


class MediaSelectionAcceptanceManifestV2(FrozenModel):
    """P3 acceptance proof for one recipient-scoped media selection.

    The ordinary selection/effect coordinates intentionally match ``.1``.
    P3 adds the three independently derived authority digests and the exact
    V3 image snapshot schema.  They are mandatory, self-hashed wire material
    rather than advisory metadata.
    """

    manifest_version: Literal["media-selection-acceptance.2"] = (
        MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION
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
    p3_authorization_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    relationship_context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    private_basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_schema_version: Literal["world-image-event-snapshot-v3"]
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_self_hash(self) -> "MediaSelectionAcceptanceManifestV2":
        if self.manifest_hash != canonical_media_selection_manifest_hash(self.model_dump(mode="json")):
            raise ValueError("media selection acceptance manifest hash is invalid")
        return self


MediaSelectionAcceptanceManifestAny = (
    MediaSelectionAcceptanceManifest | MediaSelectionAcceptanceManifestV2
)


def parse_media_selection_acceptance_manifest(
    value: dict[str, object],
) -> MediaSelectionAcceptanceManifestAny:
    """Decode one installed media-selection acceptance wire contract.

    The ``.1`` branch continues to call its original model directly.  This
    avoids silently accepting P3 fields in historical public-media manifests.
    """

    version = value.get("manifest_version", MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION)
    if version == MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION:
        return MediaSelectionAcceptanceManifest.model_validate(value, strict=True)
    if version == MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION:
        return MediaSelectionAcceptanceManifestV2.model_validate(value, strict=True)
    raise ValueError("media selection acceptance manifest version is unsupported")


def build_media_selection_acceptance_manifest_v2(
    **values: object,
) -> MediaSelectionAcceptanceManifestV2:
    material = {
        "manifest_version": MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_media_selection_manifest_hash(material)
    return MediaSelectionAcceptanceManifestV2.model_validate(material, strict=True)


__all__ = [
    "MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION",
    "MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION",
    "MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS",
    "MediaSelectionAcceptanceManifest",
    "MediaSelectionAcceptanceManifestAny",
    "MediaSelectionAcceptanceManifestV2",
    "build_media_selection_acceptance_manifest",
    "build_media_selection_acceptance_manifest_v2",
    "canonical_media_selection_manifest_hash",
    "canonical_media_selection_value_hash",
    "parse_media_selection_acceptance_manifest",
]
