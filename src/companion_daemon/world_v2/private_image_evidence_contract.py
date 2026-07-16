"""Recipient-scoped visual evidence for the P3 private-media lane.

P0/P2 ``ImageEvidenceDeclared`` deliberately admits only public/shareable
facts.  This separate wire avoids widening that contract: it records a
concrete, already-committed visual slice whose *reading* is restricted to one
recipient during a later P3 authorization.  It is evidence, never a prompt,
an invitation, or authorization to render or deliver a picture.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .image_evidence_contract import (
    CharacterMediaEvidenceV1,
    DECLARABLE_SOURCE_EVENT_TYPES,
)
from .schema_core import FrozenModel


RecipientScopedEvidenceVisibility = Literal["personal", "private"]
_ALLOWED_EVIDENCE_KEYS = frozenset({
    "visibility", "summary", "outcome", "location", "activity", "participants",
    "objects", "environment", "existing_media", "requires_readable_text",
})


class RecipientScopedImageEvidenceV1(FrozenModel):
    """One private visual slice.  Fields intentionally mirror the public wire.

    This is a new type instead of relaxing ``ImageEvidenceV1`` so replay of
    v1/v2 snapshots cannot acquire recipient-scoped facts by decoder upgrade.
    """

    visibility: RecipientScopedEvidenceVisibility
    summary: str | None = Field(default=None, max_length=480)
    outcome: str | None = Field(default=None, max_length=480)
    location: dict[str, object] | None = None
    activity: dict[str, object] | None = None
    participants: tuple[dict[str, object], ...] = Field(default=(), max_length=32)
    objects: tuple[dict[str, object], ...] = Field(default=(), max_length=32)
    environment: dict[str, object] | None = None
    existing_media: tuple[dict[str, object], ...] = Field(default=(), max_length=16)
    requires_readable_text: Literal[False] = False
    character_media: CharacterMediaEvidenceV1 | None = None

    @model_validator(mode="after")
    def contains_a_concrete_visual_slice(self) -> "RecipientScopedImageEvidenceV1":
        if not any((self.location, self.activity, self.participants, self.objects, self.environment, self.existing_media)):
            raise ValueError("recipient-scoped image evidence requires a concrete visual slice")
        return self

    def planner_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json", exclude_none=True).items()
            if key in _ALLOWED_EVIDENCE_KEYS
        }


class RecipientScopedImageEvidenceDeclaredPayload(FrozenModel):
    """Bind private visual evidence to one committed source and one recipient."""

    source_event_ref: str = Field(min_length=1, max_length=512)
    source_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_type: str = Field(min_length=1, max_length=128)
    source_privacy_ceiling: RecipientScopedEvidenceVisibility
    recipient_ref: str = Field(min_length=1, max_length=512)
    image_evidence: RecipientScopedImageEvidenceV1
    declared_at: datetime

    @model_validator(mode="after")
    def source_and_evidence_are_exactly_recipient_scoped(self) -> "RecipientScopedImageEvidenceDeclaredPayload":
        if self.source_event_type not in DECLARABLE_SOURCE_EVENT_TYPES:
            raise ValueError("recipient-scoped image evidence declaration source event type is unsupported")
        if self.image_evidence.visibility != self.source_privacy_ceiling:
            raise ValueError("recipient-scoped image evidence visibility must equal source privacy")
        return self


RECIPIENT_SCOPED_IMAGE_EVIDENCE_PAYLOAD_MODELS = {
    "RecipientScopedImageEvidenceDeclared": RecipientScopedImageEvidenceDeclaredPayload,
}


__all__ = [
    "RECIPIENT_SCOPED_IMAGE_EVIDENCE_PAYLOAD_MODELS",
    "RecipientScopedEvidenceVisibility",
    "RecipientScopedImageEvidenceDeclaredPayload",
    "RecipientScopedImageEvidenceV1",
]
