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
    "objects", "environment", "existing_media", "requires_readable_text",
})


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
    existing_media: tuple[dict[str, object], ...] = Field(default=(), max_length=16)
    requires_readable_text: Literal[False] = False

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
    "DECLARABLE_SOURCE_EVENT_TYPES",
    "IMAGE_EVIDENCE_PAYLOAD_MODELS",
    "ImageEvidenceDeclaredPayload",
    "ImageEvidenceV1",
]
