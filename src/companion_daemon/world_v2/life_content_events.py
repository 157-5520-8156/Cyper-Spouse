"""Closed ledger payload for authorizing lived-world content reads."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .schema_core import FrozenModel, PrivacyClass


class LifeContentRecordedPayload(FrozenModel):
    """Bind exact sidecar bytes to an already committed life authority."""

    content_id: str = Field(min_length=1)
    content_kind: Literal["occurrence_result", "experience_summary"]
    content_ref: str = Field(min_length=1)
    content_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    privacy_class: PrivacyClass
    source_kind: Literal["occurrence_settlement", "experience"]
    source_event_ref: str = Field(min_length=1)
    source_world_revision: int = Field(ge=1)
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_entity_id: str = Field(min_length=1)
    source_entity_revision: int = Field(ge=1)


LIFE_CONTENT_PAYLOAD_MODELS = {"LifeContentRecorded": LifeContentRecordedPayload}


__all__ = ["LIFE_CONTENT_PAYLOAD_MODELS", "LifeContentRecordedPayload"]
