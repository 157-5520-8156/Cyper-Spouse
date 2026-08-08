"""Replay-only payload codecs for immutable events from retired LifeAuthorRuntime."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel
from .schemas import ProjectionCursor


class LifeAuthorDecisionRecordedPayload(FrozenModel):
    decision_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    wake_event_ref: str = Field(min_length=1)
    wake_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    wake_world_revision: int = Field(ge=1)
    draw_event_ref: str = Field(min_length=1)
    draw_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    draw_world_revision: int = Field(ge=1)
    candidate_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["no_op", "select"]
    selected_candidate_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=256)
    raw_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_identity_version: Literal["life-author-context.1"] | None = None
    context_capsule_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_model_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_snapshot_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_cursor: ProjectionCursor | None = None

    @model_validator(mode="after")
    def selection_is_bound_to_the_drawn_candidate(self) -> "LifeAuthorDecisionRecordedPayload":
        if self.decision == "select":
            if self.selected_candidate_token != self.candidate_token:
                raise ValueError("life author decision selected a different candidate")
        elif self.selected_candidate_token is not None:
            raise ValueError("life author no-op cannot select a candidate")
        context_identity = (
            self.context_identity_version,
            self.context_capsule_id,
            self.context_model_content_hash,
            self.context_snapshot_hash,
            self.context_cursor,
        )
        if any(item is not None for item in context_identity) and any(
            item is None for item in context_identity
        ):
            raise ValueError("life author Context identity must be complete")
        return self


class LifeAvailabilitySnapshotRecordedPayload(FrozenModel):
    """Exact reviewed availability recorded by the historical planner."""

    snapshot_id: str = Field(min_length=1, max_length=256)
    wake_event_ref: str = Field(min_length=1)
    wake_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    wake_world_revision: int = Field(ge=1)
    candidate_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_actor_ref: str = Field(min_length=1)
    availability_scope: Literal["current_presence", "reviewed_future_slot"] = (
        "current_presence"
    )
    location_ref: str | None = Field(default=None, min_length=1)
    participant_refs: tuple[str, ...] = ()
    availability_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def participants_are_registered_npc_refs(self) -> "LifeAvailabilitySnapshotRecordedPayload":
        if self.participant_refs != tuple(sorted(set(self.participant_refs))):
            raise ValueError("availability snapshot participants must be unique and canonical")
        if any(not item.startswith("npc:") for item in self.participant_refs):
            raise ValueError("life author availability snapshot accepts registered NPC refs only")
        return self


__all__ = [
    "LifeAuthorDecisionRecordedPayload",
    "LifeAvailabilitySnapshotRecordedPayload",
]
