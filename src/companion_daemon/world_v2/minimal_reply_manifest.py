"""Durable, closed manifest for the isolated minimal-reply acceptance lane."""

from __future__ import annotations

import hashlib
import json

from pydantic import Field, model_validator

from .minimal_reply_acceptance import MinimalReplyAcceptanceMaterial
from .schema_core import FrozenModel


MINIMAL_REPLY_MANIFEST_VERSION = "minimal-reply-acceptance.1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def canonical_minimal_reply_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", MINIMAL_REPLY_MANIFEST_VERSION)
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


class MinimalReplyManifest(FrozenModel):
    manifest_version: str = MINIMAL_REPLY_MANIFEST_VERSION
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expression_change_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    beat_id: str = Field(min_length=1)
    message_payload_ref: str = Field(min_length=1)
    message_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reservation_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def manifest_is_self_bound(self) -> MinimalReplyManifest:
        if self.manifest_version != MINIMAL_REPLY_MANIFEST_VERSION:
            raise ValueError("minimal reply manifest version is unsupported")
        expected = canonical_minimal_reply_manifest_hash(self.model_dump(mode="json"))
        if self.manifest_hash != expected:
            raise ValueError("minimal reply manifest hash is invalid")
        return self


def build_minimal_reply_manifest(
    *, acceptance_id: str, material: MinimalReplyAcceptanceMaterial
) -> MinimalReplyManifest:
    values: dict[str, object] = {
        "manifest_version": MINIMAL_REPLY_MANIFEST_VERSION,
        "acceptance_id": acceptance_id,
        "proposal_id": material.proposal_id,
        "proposal_event_ref": material.proposal_event_ref,
        "proposal_event_payload_hash": material.proposal_event_payload_hash,
        "proposal_hash": material.proposal_hash,
        "evaluated_world_revision": material.cursor.world_revision,
        "policy_digest": material.policy_digest,
        "expression_change_id": material.expression_change_id,
        "intent_id": material.intent_id,
        "intent_hash": material.intent_hash,
        "plan_id": material.beat.plan_id,
        "beat_id": material.beat.beat_id,
        "message_payload_ref": material.beat.payload.payload_ref,
        "message_payload_hash": material.beat.payload.payload_hash,
        "reservation_id": material.reservation.reservation_id,
        "action_id": material.action.action_id,
    }
    values["manifest_hash"] = canonical_minimal_reply_manifest_hash(values)
    return MinimalReplyManifest.model_validate(values, strict=True)


__all__ = [
    "MINIMAL_REPLY_MANIFEST_VERSION",
    "MinimalReplyManifest",
    "build_minimal_reply_manifest",
    "canonical_minimal_reply_manifest_hash",
]
