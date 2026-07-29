"""Source-bound declaration contract for planner-readable visual evidence.

An image evidence declaration is a narrow, accepted assertion that an already
committed life event carries a displayable visual slice.  It is deliberately
not a prompt, a new world event, or authority to render/deliver media.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel, PrivacyClass


DECLARABLE_SOURCE_EVENT_TYPES = frozenset({
    "ActivityStarted",
    "ActivityResumed",
    "ActivityCompleted",
    "WorldOccurrenceSettled",
    "ExperienceCommitted",
    "FactCommitted",
    "FactCorrected",
    "FactCommitMaterializedV2",
})
_ALLOWED_EVIDENCE_KEYS = frozenset({
    "visibility", "summary", "outcome", "location", "activity", "participants",
    "objects", "environment", "situational_context", "existing_media",
    "requires_readable_text",
})

CharacterCaptureCapability = Literal[
    "character_front_camera",
    "character_rear_camera",
    "mirror",
    "timer_fixed",
    "requested_helper",
    "known_companion",
]
NonSensitiveBodyRegion = Literal[
    "hair",
    "ear",
    "neck",
    "shoulder",
    "upper_arm",
    "forearm",
    "wrist",
    "hand",
    "ankle",
    "foot",
]


class CharacterBodyDetailGroundingV1(FrozenModel):
    """One event-bound, non-sensitive body-detail subject and its visible object."""

    body_region: NonSensitiveBodyRegion
    object_ref: str = Field(min_length=1, max_length=512)


class CharacterMediaEvidenceV1(FrozenModel):
    """P2-only facts that can later support a bounded character-media candidate.

    This is source evidence, not a shot instruction.  The fact binder owns
    deciding whether a specific character-media kind is eligible.
    """

    character_ref: str = Field(min_length=1, max_length=512)
    present: Literal[True]
    capture_capabilities: tuple[CharacterCaptureCapability, ...] = Field(min_length=1, max_length=6)
    body_detail: CharacterBodyDetailGroundingV1 | None = None

    @model_validator(mode="after")
    def capture_capabilities_are_unique(self) -> "CharacterMediaEvidenceV1":
        if len(set(self.capture_capabilities)) != len(self.capture_capabilities):
            raise ValueError("character media capture capabilities must be unique")
        if self.body_detail is not None and not {
            "character_front_camera", "character_rear_camera",
        }.intersection(self.capture_capabilities):
            raise ValueError("character media body detail requires front/rear capture")
        return self


class MediaSituationalContextV1(FrozenModel):
    """Source-declared life coordinates that keep a picture in its real phase.

    These values describe the already-current environment.  They are neither
    a scene menu nor an instruction to render or share anything.
    """

    season: Literal["spring", "summer", "autumn", "winter"]
    academic_phase: str | None = Field(default=None, min_length=1, max_length=64)
    academic_year: int | None = Field(default=None, ge=1, le=12)
    calendar_context_tags: tuple[str, ...] = Field(default=(), max_length=16)
    current_residence_context_tags: tuple[str, ...] = Field(
        default=(), max_length=4
    )
    life_arc_context_tags: tuple[str, ...] = Field(default=(), max_length=32)
    active_life_arc_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_event_refs: tuple[str, ...] = Field(min_length=1, max_length=17)

    @model_validator(mode="after")
    def context_coordinates_are_canonical(self) -> "MediaSituationalContextV1":
        groups = (
            (self.calendar_context_tags, ("calendar:", "academic:")),
            (self.current_residence_context_tags, ("residence:",)),
            (self.life_arc_context_tags, ("life_arc:", "narrative:", "work:", "travel:")),
        )
        for values, prefixes in groups:
            if values != tuple(sorted(set(values))):
                raise ValueError("media situational context tags must be sorted and unique")
            if any(not item.startswith(prefixes) for item in values):
                raise ValueError("media situational context tag has an invalid namespace")
        if self.active_life_arc_ids != tuple(sorted(set(self.active_life_arc_ids))):
            raise ValueError("media active Life Arc ids must be sorted and unique")
        if self.source_event_refs != tuple(sorted(set(self.source_event_refs))):
            raise ValueError(
                "media situational context source refs must be sorted and unique"
            )
        return self


class ImageEvidenceV1(FrozenModel):
    """A typed envelope; leaf-level planner checks remain fail-closed downstream."""

    visibility: Literal["public", "shareable"]
    summary: str | None = Field(default=None, max_length=480)
    outcome: str | None = Field(default=None, max_length=480)
    location: dict[str, object] | None = None
    activity: dict[str, object] | None = None
    participants: tuple[dict[str, object], ...] = Field(default=(), max_length=32)
    objects: tuple[dict[str, object], ...] = Field(default=(), max_length=32)
    environment: dict[str, object] | None = None
    situational_context: MediaSituationalContextV1 | None = None
    existing_media: tuple[dict[str, object], ...] = Field(default=(), max_length=16)
    requires_readable_text: Literal[False] = False
    character_media: CharacterMediaEvidenceV1 | None = None

    @model_validator(mode="after")
    def contains_a_concrete_visual_slice(self) -> "ImageEvidenceV1":
        if not any((self.location, self.activity, self.participants, self.objects, self.environment, self.existing_media)):
            raise ValueError("image evidence requires a concrete visual slice")
        return self

    def planner_payload(self) -> dict[str, object]:
        """Return only explicitly present values for the snapshot compiler."""

        return {
            key: value
            for key, value in self.model_dump(mode="json", exclude_none=True).items()
            if key in _ALLOWED_EVIDENCE_KEYS
        }


class ImageEvidenceDeclaredPayload(FrozenModel):
    """Bind one public/shareable visual slice to one immutable life event."""

    source_event_ref: str = Field(min_length=1, max_length=512)
    source_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_type: str = Field(min_length=1, max_length=128)
    source_privacy_ceiling: PrivacyClass
    image_evidence: ImageEvidenceV1
    declared_at: datetime

    @model_validator(mode="after")
    def source_is_supported_and_not_more_private_than_its_anchor(self) -> "ImageEvidenceDeclaredPayload":
        if self.source_event_type not in DECLARABLE_SOURCE_EVENT_TYPES:
            raise ValueError("image evidence declaration source event type is unsupported")
        if self.source_privacy_ceiling not in {"public", "shareable"}:
            raise ValueError("image evidence declaration source must be public or shareable")
        if self.image_evidence.visibility == "shareable" and self.source_privacy_ceiling != "shareable":
            raise ValueError("image evidence visibility exceeds its source privacy")
        return self


IMAGE_EVIDENCE_PAYLOAD_MODELS = {
    "ImageEvidenceDeclared": ImageEvidenceDeclaredPayload,
}


__all__ = [
    "CharacterBodyDetailGroundingV1",
    "CharacterCaptureCapability",
    "CharacterMediaEvidenceV1",
    "DECLARABLE_SOURCE_EVENT_TYPES",
    "IMAGE_EVIDENCE_PAYLOAD_MODELS",
    "ImageEvidenceDeclaredPayload",
    "ImageEvidenceV1",
    "MediaSituationalContextV1",
    "NonSensitiveBodyRegion",
]
