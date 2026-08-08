"""Exact ledger coordinates shared by CharacterInterior purpose ports."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from ..schema_core import FrozenModel
from ..schemas import ProjectionCursor


class InteriorPurposeContext(FrozenModel):
    """Exact committed coordinates for one private semantic purpose."""

    inner_turn_ref: str = Field(min_length=1, max_length=512)
    trigger_ref: str = Field(min_length=1, max_length=512)
    cursor: ProjectionCursor
    logical_time: datetime
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("logical_time")
    @classmethod
    def logical_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("interior purpose logical time must be timezone-aware")
        return value

    @field_validator("source_refs")
    @classmethod
    def sources_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("interior purpose source refs must be nonempty and unique")
        return value


__all__ = ["InteriorPurposeContext"]
