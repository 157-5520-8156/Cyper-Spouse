"""Source-bound application command for creating one lived-world activity plan.

This is deliberately a small *application* seam, rather than a convenience
writer exposed to a platform.  Schedulers or offline fixtures can ask for a
plan with one source Observation, while this module owns the evidence pin,
revision-zero creation invariant, event identity and cursor CAS.  Callers
cannot provide an ``EvidenceRef`` or an arbitrary event image.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import Field, model_validator

from .event_identity import domain_idempotency_key
from .life_events import ActivityPlannedPayload
from .schema_core import FrozenModel, PrivacyClass
from .schemas import CommitResult, DueWindow, EvidenceRef, PlanStateProjection, ProjectionCursor, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ActivityPlanCommand(FrozenModel):
    """A scheduler-owned plan request bound to an existing observed message."""

    command_id: str = Field(min_length=1, max_length=256)
    world_id: str = Field(min_length=1, max_length=256)
    source_observation_id: str = Field(min_length=1, max_length=512)
    plan_id: str = Field(min_length=1, max_length=512)
    activity_id: str = Field(min_length=1, max_length=512)
    activity_kind: str = Field(min_length=1, max_length=256)
    importance_bp: int = Field(ge=0, le=10_000)
    location_ref: str | None = Field(default=None, min_length=1, max_length=512)
    participant_refs: tuple[str, ...] = Field(default=(), max_length=32)
    scheduled_window: DueWindow | None = None
    privacy_class: PrivacyClass = "private"
    policy_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def ids_are_distinct_and_participants_canonical(self) -> "ActivityPlanCommand":
        if self.plan_id == self.activity_id:
            raise ValueError("activity plan and activity identities must differ")
        if tuple(sorted(set(self.participant_refs))) != self.participant_refs:
            raise ValueError("activity plan participants must be sorted and unique")
        if tuple(sorted(set(self.policy_refs))) != self.policy_refs:
            raise ValueError("activity plan policies must be sorted and unique")
        return self


class ActivityPlanRuntime:
    """Create one revision-one ActivityPlanned event from authoritative ingress.

    The sole interface is :meth:`plan`.  Its implementation hides projection
    lookup, exact observed-message provenance, event construction and CAS so
    hosts never need ledger write access.
    """

    def __init__(self, *, ledger, owner_actor_ref: str, source: str = "world-v2:activity-plan") -> None:
        if not owner_actor_ref:
            raise ValueError("activity plan runtime needs an owner actor")
        self._ledger = ledger
        self._owner_actor_ref = owner_actor_ref
        self._source = source

    def plan(
        self,
        command: ActivityPlanCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> CommitResult:
        if command.world_id != self._ledger.world_id:
            raise ValueError("activity plan command belongs to another world")
        projection = self._ledger.project()
        if projection.logical_time is None or logical_time != projection.logical_time:
            raise ValueError("activity plan must be pinned to the current logical clock")
        observation = next(
            (item for item in projection.message_observations if item.observation_id == command.source_observation_id),
            None,
        )
        if observation is None:
            raise ValueError("activity plan source observation is unavailable")
        evidence = EvidenceRef(
            ref_id=observation.observation_id,
            evidence_type="observed_message",
            claim_purpose="future_plan",
            source_world_revision=observation.world_revision,
            immutable_hash=observation.event_payload_hash,
        )
        plan = PlanStateProjection(
            plan_id=command.plan_id,
            activity_id=command.activity_id,
            entity_revision=1,
            activity_kind=command.activity_kind,
            evidence_refs=(evidence,),
            status="planned",
            importance_bp=command.importance_bp,
            scheduled_window=command.scheduled_window,
            participant_refs=command.participant_refs,
            location_ref=command.location_ref,
            privacy_class=command.privacy_class,
            owner_actor_ref=self._owner_actor_ref,
        )
        payload = ActivityPlannedPayload(
            change_id="change:activity-plan:" + _digest([command.command_id, command.plan_id]),
            transition_id="transition:activity-plan:" + _digest([command.command_id, command.activity_id]),
            expected_entity_revision=0,
            evidence_refs=(evidence,),
            policy_refs=command.policy_refs,
            plan=plan,
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:activity-plan:" + _digest([command.world_id, payload]),
            world_id=command.world_id,
            event_type="ActivityPlanned",
            logical_time=logical_time,
            created_at=created_at,
            actor=self._owner_actor_ref,
            source=self._source,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="ActivityPlanned", world_id=command.world_id, payload=payload
                )
                or "activity-plan:" + _digest([command.world_id, command.command_id])
            ),
            payload=payload,
        )
        existing = self._ledger.lookup_event_commit(event.event_id)
        if existing is not None:
            persisted, commit = existing
            if persisted != event:
                raise ValueError("activity plan command has conflicting durable content")
            return commit
        if any(item.plan_id == command.plan_id for item in projection.plans):
            raise ValueError("activity plan already exists")
        if any(item.activity_id == command.activity_id for item in projection.plans):
            raise ValueError("activity identity already exists")
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        return self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=cursor,
            commit_id="activity-plan:" + _digest([command.world_id, command.command_id]),
        )


__all__ = ["ActivityPlanCommand", "ActivityPlanRuntime"]
