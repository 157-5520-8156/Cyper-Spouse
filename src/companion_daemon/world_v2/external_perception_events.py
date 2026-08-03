"""Frozen V2 contracts for reality the companion actually encountered.

External signals remain sidecar claims.  These contracts begin only after a
live Character-attention result selected an exact revision through an offered
channel.  They preserve evidence and the companion's own fallible reading;
they contain no downstream behaviour or Affect instruction.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal, Protocol

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .proposal_audit_schemas import ModelResultRecordedPayload
from .schema_core import FrozenModel
from .schemas import ProjectionCursor


_HASH = r"^[0-9a-f]{64}$"


def canonical_external_perception_json(value: object) -> str:
    return json.dumps(
        to_jsonable_python(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def external_perception_value_hash(value: object) -> str:
    return hashlib.sha256(canonical_external_perception_json(value).encode()).hexdigest()


class FrozenExternalSignalSnapshot(FrozenModel):
    """The complete licensed evidence bytes exposed to one attention turn."""

    snapshot_ref: str = Field(min_length=1, max_length=512)
    signal_revision_ref: str = Field(min_length=1, max_length=512)
    source_id: str = Field(min_length=1, max_length=512)
    upstream_publisher_ref: str = Field(min_length=1, max_length=512)
    upstream_item_id: str = Field(min_length=1, max_length=1_024)
    source_policy_revision: str = Field(min_length=1, max_length=256)
    source_payload_hash: str = Field(pattern=_HASH)
    normalized_hash: str = Field(pattern=_HASH)
    headline: str = Field(min_length=1, max_length=1_000)
    licensed_summary: str = Field(default="", max_length=8_000)
    canonical_url: str | None = Field(default=None, max_length=4_096)
    occurred_at: datetime | None = None
    published_at: datetime | None = None
    observed_at: datetime
    expires_at: datetime
    correction_lineage_refs: tuple[str, ...] = Field(default=(), max_length=64)
    model_visible_material_json: str = Field(min_length=2, max_length=32_768)
    model_visible_material_hash: str = Field(pattern=_HASH)
    may_expose_to_character_model: bool
    may_quote: bool
    may_freeze_durable_snapshot: bool

    @model_validator(mode="after")
    def exact_material_and_time_are_closed(self) -> FrozenExternalSignalSnapshot:
        for value in (
            self.occurred_at,
            self.published_at,
            self.observed_at,
            self.expires_at,
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("external signal snapshot times must be timezone-aware")
        try:
            decoded = json.loads(self.model_visible_material_json)
        except json.JSONDecodeError as exc:
            raise ValueError("model-visible external material is invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("model-visible external material must be a JSON object")
        if canonical_external_perception_json(decoded) != self.model_visible_material_json:
            raise ValueError("model-visible external material must be canonical JSON")
        # Bind the exact licensed bytes shown to the model, not a decoded
        # structure that another encoder could serialize differently.
        actual = hashlib.sha256(self.model_visible_material_json.encode()).hexdigest()
        if actual != self.model_visible_material_hash:
            raise ValueError("model-visible external material hash is invalid")
        if self.expires_at <= self.observed_at:
            raise ValueError("adoptable external signal snapshot is already expired")
        if len(self.correction_lineage_refs) != len(set(self.correction_lineage_refs)):
            raise ValueError("external signal correction lineage must be unique")
        return self


class ExternalPerceptionChannelProof(FrozenModel):
    """A frozen access proof selected by the companion, not proof of belief."""

    channel_ref: str = Field(min_length=1, max_length=512)
    channel_kind: str = Field(min_length=1, max_length=128)
    proof_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    proof_hash: str = Field(pattern=_HASH)
    access_summary: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def proof_refs_are_unique(self) -> ExternalPerceptionChannelProof:
        if len(self.proof_refs) != len(set(self.proof_refs)):
            raise ValueError("external perception channel proof refs must be unique")
        return self


class ExternalPerceptionSelection(FrozenModel):
    """One role-authored selection from a frozen live attention window."""

    perception_id: str = Field(min_length=1, max_length=512)
    candidate_ref: str = Field(min_length=1, max_length=512)
    snapshot: FrozenExternalSignalSnapshot
    channel: ExternalPerceptionChannelProof
    subjective_summary: str = Field(min_length=1, max_length=2_000)
    epistemic_notes: str = Field(min_length=1, max_length=2_000)
    attended_context_refs: tuple[str, ...] = Field(default=(), max_length=64)
    privacy_class: Literal["public", "shareable", "personal", "private", "withhold"]

    @model_validator(mode="after")
    def refs_are_unique(self) -> ExternalPerceptionSelection:
        if len(self.attended_context_refs) != len(set(self.attended_context_refs)):
            raise ValueError("attended Context refs must be unique")
        return self


class ExternalPerceptionLiveDelivery(FrozenModel):
    """Producer input for one fresh live attention result.

    Shadow attempts cannot be upgraded into this value.  A future live Hub
    creates a fresh attempt at the then-current World cursor.
    """

    world_id: str = Field(min_length=1, max_length=512)
    deployment_mode_revision: str = Field(min_length=1, max_length=256)
    attention_attempt_id: str = Field(min_length=1, max_length=512)
    window_id: str = Field(min_length=1, max_length=512)
    candidate_snapshot_hash: str = Field(pattern=_HASH)
    pinned_cursor: ProjectionCursor
    actor_ref: str = Field(min_length=1, max_length=512)
    encountered_world_time: datetime
    observed_wall_time: datetime
    attention_model_result: ModelResultRecordedPayload
    selections: tuple[ExternalPerceptionSelection, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def result_is_bound_to_this_attempt(self) -> ExternalPerceptionLiveDelivery:
        for value in (self.encountered_world_time, self.observed_wall_time):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("external perception delivery times must be timezone-aware")
        result = self.attention_model_result
        if (
            result.attempt_id != self.attention_attempt_id
            or result.trigger_ref != self.attention_attempt_id
            or result.evaluated_world_revision != self.pinned_cursor.world_revision
            or result.capsule_id != self.candidate_snapshot_hash
            or result.proposal_hash is None
        ):
            raise ValueError("attention model result changed its pinned delivery identity")
        identities = tuple(item.perception_id for item in self.selections)
        revisions = tuple(item.snapshot.signal_revision_ref for item in self.selections)
        snapshots = tuple(item.snapshot.snapshot_ref for item in self.selections)
        if (
            len(identities) != len(set(identities))
            or len(revisions) != len(set(revisions))
            or len(snapshots) != len(set(snapshots))
        ):
            raise ValueError("external perception selections must be unique")
        if any(item.snapshot.expires_at <= self.observed_wall_time for item in self.selections):
            raise ValueError("external perception delivery contains expired evidence")
        return self


class ExternalSignalSnapshotAdoptedPayload(FrozenModel):
    acceptance_id: str = Field(min_length=1, max_length=512)
    attention_attempt_id: str = Field(min_length=1, max_length=512)
    model_result_event_ref: str = Field(min_length=1, max_length=512)
    model_result_event_payload_hash: str = Field(pattern=_HASH)
    snapshot: FrozenExternalSignalSnapshot


class ExternalPerceptionRecordedPayload(FrozenModel):
    acceptance_id: str = Field(min_length=1, max_length=512)
    perception_id: str = Field(min_length=1, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=512)
    attention_attempt_id: str = Field(min_length=1, max_length=512)
    window_id: str = Field(min_length=1, max_length=512)
    pinned_cursor: ProjectionCursor
    encountered_world_time: datetime
    observed_wall_time: datetime
    candidate_ref: str = Field(min_length=1, max_length=512)
    candidate_snapshot_hash: str = Field(pattern=_HASH)
    snapshot_ref: str = Field(min_length=1, max_length=512)
    snapshot_event_ref: str = Field(min_length=1, max_length=512)
    snapshot_event_payload_hash: str = Field(pattern=_HASH)
    channel: ExternalPerceptionChannelProof
    subjective_summary: str = Field(min_length=1, max_length=2_000)
    epistemic_notes: str = Field(min_length=1, max_length=2_000)
    attended_context_refs: tuple[str, ...] = Field(default=(), max_length=64)
    attention_model_result_ref: str = Field(min_length=1, max_length=256)
    attention_model_event_ref: str = Field(min_length=1, max_length=512)
    attention_model_event_payload_hash: str = Field(pattern=_HASH)
    privacy_class: Literal["public", "shareable", "personal", "private", "withhold"]


class ExternalSignalSnapshotProjection(FrozenExternalSignalSnapshot):
    acceptance_id: str = Field(min_length=1, max_length=512)
    attention_attempt_id: str = Field(min_length=1, max_length=512)
    model_result_event_ref: str = Field(min_length=1, max_length=512)
    model_result_event_payload_hash: str = Field(pattern=_HASH)
    accepted_event_ref: str = Field(min_length=1, max_length=512)
    accepted_event_payload_hash: str = Field(pattern=_HASH)
    accepted_world_revision: int = Field(ge=1)


class ExternalPerceptionProjection(ExternalPerceptionRecordedPayload):
    accepted_event_ref: str = Field(min_length=1, max_length=512)
    accepted_event_payload_hash: str = Field(pattern=_HASH)
    accepted_world_revision: int = Field(ge=1)


class ExternalPerceptionLifeInfluenceView(FrozenModel):
    influence_id: str = Field(min_length=1, max_length=512)
    source_event_ref: str = Field(min_length=1, max_length=512)
    source_event_payload_hash: str = Field(pattern=_HASH)
    source_world_revision: int = Field(ge=1)
    perception_id: str = Field(min_length=1, max_length=512)
    signal_revision_ref: str = Field(min_length=1, max_length=512)
    source_id: str = Field(min_length=1, max_length=512)
    headline: str = Field(default="", max_length=1_000)
    licensed_summary: str = Field(default="", max_length=8_000)
    may_quote: bool
    source_uncertainty: str = Field(min_length=1, max_length=2_000)
    correction_lineage_refs: tuple[str, ...]
    channel: ExternalPerceptionChannelProof
    subjective_summary: str = Field(min_length=1, max_length=2_000)
    attended_context_refs: tuple[str, ...]
    behavior_suggestion: None = None


class _ExternalPerceptionProjectionView(Protocol):
    external_signal_snapshots: tuple[ExternalSignalSnapshotProjection, ...]
    external_perceptions: tuple[ExternalPerceptionProjection, ...]


def compile_external_perception_life_influences(
    projection: _ExternalPerceptionProjectionView,
) -> tuple[ExternalPerceptionLifeInfluenceView, ...]:
    """Expose sourced perceptions as neutral Context, never as a behavior proposal."""

    snapshots = {item.snapshot_ref: item for item in projection.external_signal_snapshots}
    influences: list[ExternalPerceptionLifeInfluenceView] = []
    for perception in projection.external_perceptions:
        snapshot = snapshots.get(perception.snapshot_ref)
        if snapshot is None or snapshot.accepted_event_ref != perception.snapshot_event_ref:
            raise ValueError("external perception projection lost its adopted snapshot")
        influences.append(
            ExternalPerceptionLifeInfluenceView(
                influence_id=f"life-influence:{perception.perception_id}",
                source_event_ref=perception.accepted_event_ref,
                source_event_payload_hash=perception.accepted_event_payload_hash,
                source_world_revision=perception.accepted_world_revision,
                perception_id=perception.perception_id,
                signal_revision_ref=snapshot.signal_revision_ref,
                source_id=snapshot.source_id,
                headline=(snapshot.headline if snapshot.may_quote else ""),
                licensed_summary=(snapshot.licensed_summary if snapshot.may_quote else ""),
                may_quote=snapshot.may_quote,
                source_uncertainty=perception.epistemic_notes,
                correction_lineage_refs=snapshot.correction_lineage_refs,
                channel=perception.channel,
                subjective_summary=perception.subjective_summary,
                attended_context_refs=perception.attended_context_refs,
            )
        )
    return tuple(influences)


EXTERNAL_PERCEPTION_PAYLOAD_MODELS = {
    "ExternalSignalSnapshotAdopted": ExternalSignalSnapshotAdoptedPayload,
    "ExternalPerceptionRecorded": ExternalPerceptionRecordedPayload,
}


__all__ = [
    "EXTERNAL_PERCEPTION_PAYLOAD_MODELS",
    "ExternalPerceptionChannelProof",
    "ExternalPerceptionLifeInfluenceView",
    "ExternalPerceptionLiveDelivery",
    "ExternalPerceptionProjection",
    "ExternalPerceptionRecordedPayload",
    "ExternalPerceptionSelection",
    "ExternalSignalSnapshotAdoptedPayload",
    "ExternalSignalSnapshotProjection",
    "FrozenExternalSignalSnapshot",
    "canonical_external_perception_json",
    "compile_external_perception_life_influences",
    "external_perception_value_hash",
]
