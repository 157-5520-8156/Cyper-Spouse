"""Model-owned, turn-local private state attached to an expression audit.

The state is deliberately small and semantically open.  It records what the
role model says was salient before it authored the visible expression, but it
does not authorize a World mutation, a factual claim, or any particular social
move.  Durable inner life continues to use the existing Affect,
PrivateImpression, Thread, Commitment, and Memory acceptance paths.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .schema_core import FrozenModel


class PrivateTurnState(FrozenModel):
    """One concise decision summary, not hidden reasoning or a behavior menu."""

    contract: Literal["private-turn-state.1"] = "private-turn-state.1"
    inner_state_summary: str = Field(min_length=1, max_length=480)
    attended_source_refs: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("inner_state_summary")
    @classmethod
    def summary_has_semantic_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("private turn state summary must contain non-whitespace content")
        return value

    @model_validator(mode="after")
    def attended_refs_are_unique(self) -> "PrivateTurnState":
        if len(self.attended_source_refs) != len(set(self.attended_source_refs)):
            raise ValueError("private turn state source refs must be unique")
        return self


def validate_private_turn_state_sources(
    state: PrivateTurnState | None,
    *,
    allowed_source_refs: set[str],
) -> None:
    """Keep attention claims pinned without turning them into fact authority."""

    if state is None:
        return
    outside = set(state.attended_source_refs) - allowed_source_refs
    if outside:
        # Never render the model-authored ref into an exception: this boundary
        # is surfaced in ordinary reliability logs and correction metadata.
        raise ValueError("private turn state contains an unpinned attention source")


__all__ = [
    "PrivateTurnState",
    "validate_private_turn_state_sources",
]
