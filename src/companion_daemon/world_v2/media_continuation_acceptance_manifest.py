"""Self-hashing proof for one accepted Media v2 continuation."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION = "media-continuation-acceptance.1"


def canonical_media_continuation_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def canonical_media_continuation_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    return canonical_media_continuation_hash(material)


def media_continuation_event_identity(
    *, event_type: str, world_id: str, payload: dict[str, object]
) -> str:
    return "world-v2:media-continuation:" + canonical_media_continuation_hash(
        {"event_type": event_type, "world_id": world_id, "payload": payload}
    )


class MediaContinuationAcceptanceManifest(FrozenModel):
    manifest_version: Literal["media-continuation-acceptance.1"] = (
        MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION
    )
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1, max_length=256)
    acceptance_event_ref: str = Field(min_length=1, max_length=512)
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    continuation_step: Literal["plan_to_render", "render_to_inspect"]
    trigger_id: str = Field(min_length=1, max_length=256)
    source_evidence_ref: str = Field(min_length=1, max_length=512)
    source_evidence_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_action_id: str = Field(min_length=1, max_length=256)
    authorized_action_kind: Literal["media_render", "media_inspection"]
    authorized_intent_ref: str = Field(min_length=1, max_length=512)
    authorized_payload_ref: str = Field(min_length=1, max_length=512)
    authorized_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    claim_event_ref: str = Field(min_length=1, max_length=512)
    claim_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation_event_ref: str = Field(min_length=1, max_length=512)
    reservation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_event_ref: str = Field(min_length=1, max_length=512)
    action_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_event_ref: str = Field(min_length=1, max_length=512)
    completion_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_self_hash(self) -> "MediaContinuationAcceptanceManifest":
        if self.manifest_hash != canonical_media_continuation_manifest_hash(
            self.model_dump(mode="json")
        ):
            raise ValueError("media continuation acceptance manifest hash is invalid")
        return self


def build_media_continuation_acceptance_manifest(
    **values: object,
) -> MediaContinuationAcceptanceManifest:
    material = {
        "manifest_version": MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_media_continuation_manifest_hash(material)
    return MediaContinuationAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION",
    "MediaContinuationAcceptanceManifest",
    "build_media_continuation_acceptance_manifest",
    "canonical_media_continuation_hash",
    "canonical_media_continuation_manifest_hash",
    "media_continuation_event_identity",
]
