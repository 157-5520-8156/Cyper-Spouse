"""Closed authority for one live external-perception V2 delivery."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .external_perception_events import external_perception_value_hash
from .schema_core import FrozenModel


EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION = "external-perception-acceptance.1"
EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_VERSION = "external-perception-acceptance-policy.1"
EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_DIGEST = external_perception_value_hash(
    {
        "contract": EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_VERSION,
        "requires_live_mode": True,
        "requires_model_exposure_permission": True,
        "requires_durable_snapshot_permission": True,
        "writes": (
            "ModelResultRecorded",
            "ExternalSignalSnapshotAdopted",
            "ExternalPerceptionRecorded",
        ),
        "downstream_wake": False,
    }
)


def canonical_external_perception_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION)
    return external_perception_value_hash(material)


class ExternalPerceptionAcceptedEffect(FrozenModel):
    ordinal: int = Field(ge=0, le=24)
    role: Literal["model_result", "signal_snapshot", "external_perception"]
    event_id: str = Field(min_length=1, max_length=512)
    event_type: Literal[
        "ModelResultRecorded",
        "ExternalSignalSnapshotAdopted",
        "ExternalPerceptionRecorded",
    ]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def role_matches_event(self) -> ExternalPerceptionAcceptedEffect:
        expected = {
            "model_result": "ModelResultRecorded",
            "signal_snapshot": "ExternalSignalSnapshotAdopted",
            "external_perception": "ExternalPerceptionRecorded",
        }[self.role]
        if self.event_type != expected:
            raise ValueError("external perception effect role changed event type")
        return self


class ExternalPerceptionAcceptanceManifest(FrozenModel):
    manifest_version: Literal["external-perception-acceptance.1"] = (
        EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION
    )
    status: Literal["accepted"] = "accepted"
    acceptance_id: str = Field(min_length=1, max_length=512)
    attention_attempt_id: str = Field(min_length=1, max_length=512)
    window_id: str = Field(min_length=1, max_length=512)
    candidate_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    evaluated_deliberation_revision: int = Field(ge=0)
    evaluated_ledger_sequence: int = Field(ge=0)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effects: tuple[ExternalPerceptionAcceptedEffect, ...] = Field(min_length=3, max_length=25)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def effects_and_self_hash_are_closed(self) -> ExternalPerceptionAcceptanceManifest:
        if tuple(item.ordinal for item in self.effects) != tuple(range(len(self.effects))):
            raise ValueError("external perception effects must be ordered and contiguous")
        if len({item.event_id for item in self.effects}) != len(self.effects):
            raise ValueError("external perception effect ids must be unique")
        if tuple(item.role for item in self.effects).count("model_result") != 1:
            raise ValueError("external perception delivery requires one model result")
        snapshots = tuple(item for item in self.effects if item.role == "signal_snapshot")
        perceptions = tuple(item for item in self.effects if item.role == "external_perception")
        if not snapshots or len(snapshots) != len(perceptions):
            raise ValueError("external perception snapshots and perceptions must pair")
        if self.manifest_hash != canonical_external_perception_manifest_hash(
            self.model_dump(mode="json")
        ):
            raise ValueError("external perception acceptance manifest hash is invalid")
        return self


def build_external_perception_acceptance_manifest(
    **values: object,
) -> ExternalPerceptionAcceptanceManifest:
    material = {
        "manifest_version": EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION,
        "status": "accepted",
        **values,
    }
    material["manifest_hash"] = canonical_external_perception_manifest_hash(material)
    return ExternalPerceptionAcceptanceManifest.model_validate(material, strict=True)


__all__ = [
    "EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION",
    "EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_DIGEST",
    "EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_VERSION",
    "ExternalPerceptionAcceptanceManifest",
    "ExternalPerceptionAcceptedEffect",
    "build_external_perception_acceptance_manifest",
    "canonical_external_perception_manifest_hash",
]
