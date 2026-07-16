"""Short-lived, source-bound authority for visible physical state.

This is intentionally a narrow fact module.  It records only observable,
time-bounded cues that a committed life event can support; it is neither a
health record, an arousal model, nor an image-prompt vocabulary.  The state is
useful precisely because a historical resolver can distinguish an absent state
from an explicit clear/counter-evidence state.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from .appearance_state import APPEARANCE_SOURCE_EVENT_TYPES
from .schema_core import FrozenModel, PrivacyClass


VisiblePhysicalCueId = Literal[
    "perspiration",
    "flush",
    "recovering_breath",
    "damp_hair",
    "rain_damp_fabric",
    "sleepy_face",
    "posture_fatigue",
    "muscle_tension",
]
VisiblePhysicalCueIntensity = Literal["light", "moderate", "marked"]
VisiblePhysicalNegativeCueId = Literal[
    "dry",
    "dry_hair",
    "settled_breathing",
    "clear_complexion",
    "rested_posture",
    "relaxed_muscles",
]
VisiblePhysicalRegion = Literal[
    "face",
    "hair",
    "neck",
    "shoulder",
    "chest",
    "back",
    "arm",
    "hand",
    "leg",
    "clothing",
]

MAX_VISIBLE_PHYSICAL_STATE_LIFETIME = timedelta(hours=4)

_COUNTER_EVIDENCE: dict[VisiblePhysicalNegativeCueId, frozenset[VisiblePhysicalCueId]] = {
    "dry": frozenset({"perspiration", "rain_damp_fabric"}),
    "dry_hair": frozenset({"damp_hair"}),
    "settled_breathing": frozenset({"recovering_breath"}),
    "clear_complexion": frozenset({"flush"}),
    "rested_posture": frozenset({"sleepy_face", "posture_fatigue"}),
    "relaxed_muscles": frozenset({"muscle_tension"}),
}


class VisiblePhysicalCue(FrozenModel):
    """One positive, observable cue with bounded intensity and region."""

    cue_id: VisiblePhysicalCueId
    intensity: VisiblePhysicalCueIntensity
    visible_regions: tuple[VisiblePhysicalRegion, ...] = Field(min_length=1, max_length=8)
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def has_unique_regions(self) -> "VisiblePhysicalCue":
        if len(self.visible_regions) != len(set(self.visible_regions)):
            raise ValueError("visible physical cue regions must be unique")
        return self


class VisiblePhysicalNegativeCue(FrozenModel):
    """Structured counter-evidence; free-text negation is deliberately absent."""

    cue_id: VisiblePhysicalNegativeCueId
    visible_regions: tuple[VisiblePhysicalRegion, ...] = Field(min_length=1, max_length=8)
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def has_unique_regions(self) -> "VisiblePhysicalNegativeCue":
        if len(self.visible_regions) != len(set(self.visible_regions)):
            raise ValueError("visible physical negative cue regions must be unique")
        return self


class VisiblePhysicalStateProjection(FrozenModel):
    """Immutable state version whose validity is always short and explicit."""

    physical_state_id: str = Field(min_length=1, max_length=256)
    subject_ref: str = Field(min_length=1, max_length=256)
    entity_revision: int = Field(ge=1)
    source_event_ref: str = Field(min_length=1, max_length=512)
    source_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_type: str = Field(min_length=1, max_length=128)
    valid_from: datetime
    valid_until: datetime
    visibility: PrivacyClass
    positive_cues: tuple[VisiblePhysicalCue, ...] = Field(max_length=8)
    negative_cues: tuple[VisiblePhysicalNegativeCue, ...] = Field(max_length=6)

    @model_validator(mode="after")
    def is_short_lived_source_bound_state(self) -> "VisiblePhysicalStateProjection":
        if self.source_event_type not in APPEARANCE_SOURCE_EVENT_TYPES:
            raise ValueError("visible physical state source event type is unsupported")
        if not self.positive_cues and not self.negative_cues:
            raise ValueError("visible physical state requires positive or negative evidence")
        if self.valid_until <= self.valid_from:
            raise ValueError("visible physical state validity interval is invalid")
        if self.valid_until - self.valid_from > MAX_VISIBLE_PHYSICAL_STATE_LIFETIME:
            raise ValueError("visible physical state exceeds maximum lifetime")
        positive_ids = tuple(cue.cue_id for cue in self.positive_cues)
        negative_ids = tuple(cue.cue_id for cue in self.negative_cues)
        if len(positive_ids) != len(set(positive_ids)):
            raise ValueError("visible physical positive cue ids must be unique")
        if len(negative_ids) != len(set(negative_ids)):
            raise ValueError("visible physical negative cue ids must be unique")
        for negative in self.negative_cues:
            for positive in self.positive_cues:
                if (
                    positive.cue_id in _COUNTER_EVIDENCE[negative.cue_id]
                    and set(positive.visible_regions) & set(negative.visible_regions)
                ):
                    raise ValueError("visible physical positive and negative cues conflict")
        return self

    @property
    def has_positive_cues(self) -> bool:
        """A clear/negative-only state is meaningful but not a positive basis."""

        return bool(self.positive_cues)


class VisiblePhysicalStateRecordedPayload(FrozenModel):
    state: VisiblePhysicalStateProjection


class VisiblePhysicalStateRecordCommand(FrozenModel):
    """Host request without forgeable source coordinates or revision fields."""

    command_id: str = Field(min_length=1, max_length=256)
    source_event_ref: str = Field(min_length=1, max_length=512)
    subject_ref: str = Field(min_length=1, max_length=256)
    positive_cues: tuple[VisiblePhysicalCue, ...] = Field(max_length=8)
    negative_cues: tuple[VisiblePhysicalNegativeCue, ...] = Field(max_length=6)
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def contains_evidence(self) -> "VisiblePhysicalStateRecordCommand":
        if not self.positive_cues and not self.negative_cues:
            raise ValueError("visible physical state requires positive or negative evidence")
        return self


def visible_physical_state_at(
    states: tuple[VisiblePhysicalStateProjection, ...],
    *,
    subject_ref: str,
    at_logical_time: datetime,
) -> VisiblePhysicalStateProjection | None:
    """Resolve exactly the version active at a historical logical time."""

    matches = tuple(
        state
        for state in states
        if state.subject_ref == subject_ref
        and state.valid_from <= at_logical_time < state.valid_until
    )
    if not matches:
        return None
    return max(matches, key=lambda state: (state.valid_from, state.entity_revision))


VISIBLE_PHYSICAL_STATE_PAYLOAD_MODELS = {
    "VisiblePhysicalStateRecorded": VisiblePhysicalStateRecordedPayload,
}


__all__ = [
    "MAX_VISIBLE_PHYSICAL_STATE_LIFETIME",
    "VISIBLE_PHYSICAL_STATE_PAYLOAD_MODELS",
    "VisiblePhysicalCue",
    "VisiblePhysicalCueId",
    "VisiblePhysicalCueIntensity",
    "VisiblePhysicalNegativeCue",
    "VisiblePhysicalNegativeCueId",
    "VisiblePhysicalRegion",
    "VisiblePhysicalStateProjection",
    "VisiblePhysicalStateRecordCommand",
    "VisiblePhysicalStateRecordedPayload",
    "visible_physical_state_at",
]
