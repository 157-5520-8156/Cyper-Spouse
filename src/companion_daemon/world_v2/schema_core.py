"""Cycle-free primitive schema contracts shared by World v2 domains."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _contains_naive_datetime(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is None or value.utcoffset() is None
    if isinstance(value, dict):
        return any(_contains_naive_datetime(item) for item in value.values())
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_naive_datetime(item) for item in value)
    return False


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="after")
    def datetimes_are_timezone_aware(self) -> FrozenModel:
        for name in type(self).model_fields:
            if _contains_naive_datetime(getattr(self, name)):
                raise ValueError(f"{name} must contain only timezone-aware datetimes")
        return self


PrivacyClass = Literal["public", "shareable", "personal", "private", "withhold"]


class EvidenceRef(FrozenModel):
    ref_id: str = Field(min_length=1)
    evidence_type: Literal[
        "committed_fact",
        "committed_experience",
        "committed_world_event",
        "settled_world_event",
        "settled_external_result",
        "observed_message",
        "active_plan",
        "operator_observation",
        "clock_observation",
    ]
    claim_purpose: Literal[
        "current_fact",
        "past_experience",
        "future_plan",
        "private_hypothesis",
        "action_authorization",
        "conversation_continuity",
    ]
    source_world_revision: int | None = Field(default=None, ge=1)
    immutable_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def committed_world_evidence_is_revision_pinned(self) -> EvidenceRef:
        if self.evidence_type in {
            "committed_world_event",
            "settled_world_event",
            "committed_fact",
            "committed_experience",
        }:
            if self.source_world_revision is None or self.immutable_hash is None:
                raise ValueError("world-event evidence requires revision and immutable hash")
        return self
