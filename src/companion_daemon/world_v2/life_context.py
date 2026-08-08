"""Source-bound context contracts shared by current life-world boundaries.

This module carries no character decision loop.  It only exposes the exact
compiler-issued capsule view that a current authority may consume.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Protocol


class LifeContextCapsule(Protocol):
    capsule_id: str
    snapshot_hash: str
    world_revision: int
    deliberation_revision: int
    ledger_sequence: int
    logical_time: datetime | None
    model_content_json: str


class LifeContextCapsuleHandle(Protocol):
    @property
    def capsule(self) -> LifeContextCapsule: ...


class LifeContextCapsuleCompiler(Protocol):
    def compile_for_deliberation(self, query) -> LifeContextCapsuleHandle: ...  # type: ignore[no-untyped-def]


def compile_life_decision_context(capsule: LifeContextCapsule) -> dict[str, object]:
    """Return the compiler-issued bounded view without a second truth path."""

    decoded = json.loads(capsule.model_content_json)
    if not isinstance(decoded, dict):
        raise ValueError("life Context Capsule must decode to an object")
    return decoded


__all__ = [
    "LifeContextCapsule",
    "LifeContextCapsuleCompiler",
    "LifeContextCapsuleHandle",
    "compile_life_decision_context",
]
