"""Durable terminal audit for a bounded media-selection decline."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


class MediaSelectionCandidateRevision(FrozenModel):
    candidate_id: str = Field(min_length=1, max_length=512)
    entity_revision: int = Field(ge=1)


class MediaSelectionAttemptRecordedPayload(FrozenModel):
    """One terminal model attempt scoped to an exact logical-time candidate set.

    A later logical time or candidate revision produces a different attempt,
    so this is a replay guard rather than a permanent behavioural rule.
    """

    attempt_id: str = Field(min_length=1, max_length=256)
    candidates: tuple[MediaSelectionCandidateRevision, ...] = Field(
        min_length=1, max_length=32
    )
    outcome: Literal["declined", "invalid"]
    model: str = Field(min_length=1, max_length=256)
    raw_output_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    normalized_output_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    failure_code: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def canonical_candidates(self) -> "MediaSelectionAttemptRecordedPayload":
        keys = tuple((item.candidate_id, item.entity_revision) for item in self.candidates)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("media selection decline candidates must be sorted and unique")
        if self.outcome == "declined":
            if self.normalized_output_hash is None or self.failure_code is not None:
                raise ValueError("declined media selection attempt has invalid outcome evidence")
        elif self.normalized_output_hash is not None or self.failure_code is None:
            raise ValueError("invalid media selection attempt has invalid failure evidence")
        return self


def media_selection_attempt_id(
    *, world_id: str, logical_time: datetime,
    candidates: tuple[MediaSelectionCandidateRevision, ...],
) -> str:
    """Bind one attempt to its world, clock and exact candidate revisions."""

    material = {
        "world_id": world_id,
        "logical_time": logical_time.isoformat(),
        "candidates": [
            [item.candidate_id, item.entity_revision] for item in candidates
        ],
    }
    return "media-selection:" + hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "MediaSelectionCandidateRevision",
    "MediaSelectionAttemptRecordedPayload",
    "media_selection_attempt_id",
]
