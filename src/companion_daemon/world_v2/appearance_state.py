"""Sparse, source-bound visible appearance authority.

This module intentionally records only facts that have become relevant to a
visible world state.  It is neither a wardrobe inventory nor a prompt schema:
attribute aspects remain open-ended, while every published value is tied to a
committed source event and a logical validity interval.
"""

from __future__ import annotations

from datetime import datetime
from pydantic import Field, model_validator

from .schema_core import FrozenModel, PrivacyClass


APPEARANCE_SOURCE_EVENT_TYPES = frozenset(
    {
        "ActivityStarted",
        "ActivityResumed",
        "ActivityCompleted",
        "WorldOccurrenceSettled",
        "ExperienceCommitted",
        "FactCommitted",
        "FactCorrected",
        "FactCommitMaterializedV2",
    }
)
_PRIVACY_ORDER = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}


class VisibleAppearanceAttribute(FrozenModel):
    """One factual visible attribute, without forcing a wardrobe taxonomy."""

    aspect: str = Field(min_length=1, max_length=96)
    description: str = Field(min_length=1, max_length=480)


class AppearanceStateProjection(FrozenModel):
    """An immutable sparse state version, valid over one logical interval."""

    appearance_state_id: str = Field(min_length=1, max_length=256)
    subject_ref: str = Field(min_length=1, max_length=256)
    entity_revision: int = Field(ge=1)
    source_event_ref: str = Field(min_length=1, max_length=512)
    source_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_type: str = Field(min_length=1, max_length=128)
    valid_from: datetime
    valid_until: datetime | None = None
    visibility: PrivacyClass
    visible_attributes: tuple[VisibleAppearanceAttribute, ...] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def is_sparse_source_bound_and_time_bounded(self) -> "AppearanceStateProjection":
        if self.source_event_type not in APPEARANCE_SOURCE_EVENT_TYPES:
            raise ValueError("appearance state source event type is unsupported")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("appearance state validity interval is invalid")
        aspects = tuple(item.aspect for item in self.visible_attributes)
        if len(aspects) != len(set(aspects)):
            raise ValueError("appearance state attribute aspects must be unique")
        return self


class AppearanceStateRecordedPayload(FrozenModel):
    state: AppearanceStateProjection


class AppearanceStateRecordCommand(FrozenModel):
    """Request a state record; source coordinates are derived by the runtime."""

    command_id: str = Field(min_length=1, max_length=256)
    source_event_ref: str = Field(min_length=1, max_length=512)
    subject_ref: str = Field(min_length=1, max_length=256)
    visibility: PrivacyClass
    visible_attributes: tuple[VisibleAppearanceAttribute, ...] = Field(min_length=1, max_length=24)
    valid_until: datetime | None = None


def appearance_state_at(
    states: tuple[AppearanceStateProjection, ...],
    *,
    subject_ref: str,
    at_logical_time: datetime,
) -> AppearanceStateProjection | None:
    """Return the one source-bound version active at a historical clock time."""

    matches = tuple(
        state
        for state in states
        if state.subject_ref == subject_ref
        and state.valid_from <= at_logical_time
        and (state.valid_until is None or at_logical_time < state.valid_until)
    )
    if not matches:
        return None
    return max(matches, key=lambda state: (state.valid_from, state.entity_revision))


def privacy_is_no_broader_than(
    *, visibility: PrivacyClass, source_visibility: PrivacyClass
) -> bool:
    return _PRIVACY_ORDER[visibility] >= _PRIVACY_ORDER[source_visibility]


APPEARANCE_STATE_PAYLOAD_MODELS = {
    "AppearanceStateRecorded": AppearanceStateRecordedPayload,
}


__all__ = [
    "APPEARANCE_SOURCE_EVENT_TYPES",
    "APPEARANCE_STATE_PAYLOAD_MODELS",
    "AppearanceStateProjection",
    "AppearanceStateRecordCommand",
    "AppearanceStateRecordedPayload",
    "VisibleAppearanceAttribute",
    "appearance_state_at",
    "privacy_is_no_broader_than",
]
