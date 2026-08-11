"""Self-hashing accepted manifest for one interaction-act status mutation."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .interaction_act_events import (
    InteractionActAcceptedPayload,
    canonical_interaction_act_accepted_payload_hash,
)
from .schema_core import FrozenModel, canonicalize_json_value


INTERACTION_ACT_ACCEPTANCE_MANIFEST_VERSION = "interaction-act-acceptance.1"
INTERACTION_ACT_ACCEPTANCE_POLICY_VERSION = "interaction-act-acceptance-policy.2"


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST = _canonical_hash(
    {
        "contract": INTERACTION_ACT_ACCEPTANCE_POLICY_VERSION,
        "source_event_type": "InteractionActProposalRecorded",
        "effect_event_type": "InteractionActTransitionAccepted",
        "operations": ("declare", "revise"),
        "status_authority": "per_source_actor",
        "global_status": "forbidden",
        "external_outcome": "not_established",
    }
)


def canonical_interaction_act_acceptance_manifest_hash(
    value: InteractionActAcceptanceManifest | dict[str, object],
) -> str:
    material = (
        value.model_dump(mode="json")
        if isinstance(value, InteractionActAcceptanceManifest)
        else dict(value)
    )
    material.pop("manifest_hash", None)
    material.setdefault(
        "manifest_version",
        INTERACTION_ACT_ACCEPTANCE_MANIFEST_VERSION,
    )
    encoded = json.dumps(
        canonicalize_json_value(material),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class InteractionActAcceptanceManifest(FrozenModel):
    manifest_version: Literal["interaction-act-acceptance.1"] = (
        INTERACTION_ACT_ACCEPTANCE_MANIFEST_VERSION
    )
    status: Literal["accepted"] = "accepted"
    world_id: str = Field(min_length=1, max_length=256)
    acceptance_id: str = Field(min_length=1, max_length=256)
    source_proposal_event_ref: str = Field(min_length=1, max_length=512)
    source_proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    mutation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect_event_id: str = Field(
        pattern=r"^event:interaction-act-transition-accepted:[0-9a-f]{64}$"
    )
    effect_event_type: Literal["InteractionActTransitionAccepted"]
    effect_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def self_hash_is_exact(self) -> InteractionActAcceptanceManifest:
        if self.policy_digest != INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST:
            raise ValueError("interaction act acceptance policy digest is not installed")
        if self.manifest_hash != canonical_interaction_act_acceptance_manifest_hash(self):
            raise ValueError("interaction act acceptance manifest hash is invalid")
        return self


def build_interaction_act_acceptance_manifest(
    *,
    accepted_payload: InteractionActAcceptedPayload,
    policy_digest: str,
) -> InteractionActAcceptanceManifest:
    material: dict[str, object] = {
        "manifest_version": INTERACTION_ACT_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        "world_id": accepted_payload.world_id,
        "acceptance_id": accepted_payload.acceptance_id,
        "source_proposal_event_ref": accepted_payload.source_proposal_event_ref,
        "source_proposal_event_payload_hash": (
            accepted_payload.source_proposal_event_payload_hash
        ),
        "proposal_id": accepted_payload.proposal_id,
        "proposal_hash": accepted_payload.proposal_hash,
        "accepted_change_id": accepted_payload.change_id,
        "accepted_change_hash": accepted_payload.accepted_change_hash,
        "evaluated_world_revision": accepted_payload.evaluated_world_revision,
        "mutation_payload_hash": accepted_payload.mutation_payload_hash,
        "effect_event_id": accepted_payload.accepted_event_ref,
        "effect_event_type": "InteractionActTransitionAccepted",
        "effect_payload_hash": canonical_interaction_act_accepted_payload_hash(
            accepted_payload
        ),
        "policy_digest": policy_digest,
    }
    material["manifest_hash"] = canonical_interaction_act_acceptance_manifest_hash(
        material
    )
    return InteractionActAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "INTERACTION_ACT_ACCEPTANCE_MANIFEST_VERSION",
    "INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST",
    "INTERACTION_ACT_ACCEPTANCE_POLICY_VERSION",
    "InteractionActAcceptanceManifest",
    "build_interaction_act_acceptance_manifest",
    "canonical_interaction_act_acceptance_manifest_hash",
]
