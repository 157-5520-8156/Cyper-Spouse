"""Closed accepted-manifest for one source-bound delivered-media Thread change."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION = "media-delivery-thread-acceptance.1"


def canonical_media_thread_value_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def canonical_media_thread_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION)
    return canonical_media_thread_value_hash(material)


class MediaDeliveryThreadAcceptanceManifest(FrozenModel):
    manifest_version: Literal["media-delivery-thread-acceptance.1"] = (
        MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION
    )
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
    thread_event_id: str = Field(min_length=1)
    thread_event_type: Literal["MediaDeliveryThreadOpened", "MediaDeliveryThreadUpdated"]
    thread_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_self_hash(self) -> "MediaDeliveryThreadAcceptanceManifest":
        if self.manifest_hash != canonical_media_thread_manifest_hash(self.model_dump(mode="json")):
            raise ValueError("media delivery thread acceptance manifest hash is invalid")
        return self


def build_media_thread_acceptance_manifest(
    **values: object,
) -> MediaDeliveryThreadAcceptanceManifest:
    material = {
        "manifest_version": MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_media_thread_manifest_hash(material)
    return MediaDeliveryThreadAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION",
    "MediaDeliveryThreadAcceptanceManifest",
    "build_media_thread_acceptance_manifest",
    "canonical_media_thread_manifest_hash",
    "canonical_media_thread_value_hash",
]
