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
from typing import Literal

from pydantic import Field, model_validator

from .event_identity import domain_idempotency_key
from .life_events import ActivityPlannedPayload, ActivityTransitionPayload
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
    supersedes_plan_id: str | None = Field(default=None, min_length=1, max_length=512)

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
            supersedes_plan_id=command.supersedes_plan_id,
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

    def transition(
        self,
        command: "ActivityPlanTransitionCommand",
        *,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Move an existing plan through its durable lifecycle.

        This is intentionally source-bound.  A platform or scheduler may ask
        for a transition but cannot manufacture a reason/evidence reference or
        skip the entity revision CAS checked by the reducer.
        """
        if command.world_id != self._ledger.world_id:
            raise ValueError("activity transition command belongs to another world")
        projection = self._ledger.project()
        if projection.logical_time is None or logical_time != projection.logical_time:
            raise ValueError("activity transition must be pinned to the current logical clock")
        plan = next((item for item in projection.plans if item.plan_id == command.plan_id), None)
        if plan is None:
            raise ValueError("activity transition plan is unavailable")
        observation = next(
            (item for item in projection.message_observations if item.observation_id == command.source_observation_id),
            None,
        )
        if observation is None:
            raise ValueError("activity transition source observation is unavailable")
        evidence = EvidenceRef(
            ref_id=observation.observation_id,
            evidence_type="observed_message",
            claim_purpose="future_plan",
            source_world_revision=observation.world_revision,
            immutable_hash=observation.event_payload_hash,
        )
        event_type = {
            "start": "ActivityStarted", "pause": "ActivityPaused", "resume": "ActivityResumed",
            "complete": "ActivityCompleted", "abandon": "ActivityAbandoned",
        }[command.operation]
        payload = ActivityTransitionPayload(
            change_id="change:activity-transition:" + _digest([command.command_id, command.plan_id]),
            transition_id="transition:activity-transition:" + _digest([command.command_id, command.operation]),
            expected_entity_revision=plan.entity_revision,
            evidence_refs=(evidence,), policy_refs=command.policy_refs,
            plan_id=plan.plan_id, transitioned_at=logical_time, reason_ref=evidence.ref_id,
        ).model_dump(mode="json")
        event = self._event(
            event_type=event_type, command_id=command.command_id, payload=payload,
            logical_time=logical_time, created_at=created_at, trace_id=trace_id,
            causation_id=causation_id, correlation_id=correlation_id,
        )
        return self._commit_idempotently(event=event, projection=projection, command_id=command.command_id)

    def replace(
        self,
        command: ActivityPlanCommand,
        *,
        predecessor_plan_id: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> CommitResult:
        """Atomically abandon a live plan and install its explicit successor.

        No ``ActivityCompleted`` or Experience event is emitted: a substituted
        plan is a change of intent, not a fabricated lived result.
        """
        if command.supersedes_plan_id != predecessor_plan_id:
            raise ValueError("replacement plan must name its exact predecessor")
        if command.world_id != self._ledger.world_id:
            raise ValueError("replacement plan command belongs to another world")
        projection = self._ledger.project()
        if projection.logical_time is None or logical_time != projection.logical_time:
            raise ValueError("replacement plan must be pinned to the current logical clock")
        predecessor = next((item for item in projection.plans if item.plan_id == predecessor_plan_id), None)
        if predecessor is None:
            raise ValueError("replacement predecessor is unavailable")
        if predecessor.status not in {"planned", "active", "paused"}:
            raise ValueError("replacement predecessor is already terminal")
        observation = next((item for item in projection.message_observations if item.observation_id == command.source_observation_id), None)
        if observation is None:
            raise ValueError("replacement source observation is unavailable")
        evidence = EvidenceRef(ref_id=observation.observation_id, evidence_type="observed_message",
            claim_purpose="future_plan", source_world_revision=observation.world_revision,
            immutable_hash=observation.event_payload_hash)
        abandon = ActivityTransitionPayload(
            change_id="change:activity-replacement:" + _digest([command.command_id, predecessor.plan_id, "abandon"]),
            transition_id="transition:activity-replacement:" + _digest([command.command_id, predecessor.plan_id, "abandon"]),
            expected_entity_revision=predecessor.entity_revision, evidence_refs=(evidence,),
            policy_refs=command.policy_refs, plan_id=predecessor.plan_id,
            transitioned_at=logical_time, reason_ref=evidence.ref_id,
        ).model_dump(mode="json")
        successor = PlanStateProjection(
            plan_id=command.plan_id, activity_id=command.activity_id, entity_revision=1,
            activity_kind=command.activity_kind, evidence_refs=(evidence,), status="planned",
            importance_bp=command.importance_bp, scheduled_window=command.scheduled_window,
            participant_refs=command.participant_refs, location_ref=command.location_ref,
            privacy_class=command.privacy_class, owner_actor_ref=self._owner_actor_ref,
            supersedes_plan_id=predecessor.plan_id,
        )
        planned = ActivityPlannedPayload(
            change_id="change:activity-replacement:" + _digest([command.command_id, successor.plan_id]),
            transition_id="transition:activity-replacement:" + _digest([command.command_id, successor.activity_id]),
            expected_entity_revision=0, evidence_refs=(evidence,), policy_refs=command.policy_refs,
            plan=successor,
        ).model_dump(mode="json")
        events = (
            self._event(event_type="ActivityAbandoned", command_id=command.command_id + ":abandon", payload=abandon,
                logical_time=logical_time, created_at=created_at, trace_id=trace_id, causation_id=causation_id, correlation_id=correlation_id),
            self._event(event_type="ActivityPlanned", command_id=command.command_id + ":successor", payload=planned,
                logical_time=logical_time, created_at=created_at, trace_id=trace_id, causation_id=causation_id, correlation_id=correlation_id),
        )
        existing = self._ledger.lookup_event_commit(events[0].event_id)
        if existing is not None:
            persisted, commit = existing
            if persisted != events[0]:
                raise ValueError("replacement command has conflicting durable content")
            return commit
        cursor = ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)
        return self._ledger.commit_at_cursor(events, expected_cursor=cursor,
            commit_id="activity-replacement:" + _digest([command.world_id, command.command_id]))

    def _event(self, *, event_type: str, command_id: str, payload: dict[str, object], logical_time: datetime, created_at: datetime, trace_id: str, causation_id: str, correlation_id: str) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1", event_id="event:activity:" + _digest([self._ledger.world_id, event_type, command_id, payload]),
            world_id=self._ledger.world_id, event_type=event_type, logical_time=logical_time, created_at=created_at,
            actor=self._owner_actor_ref, source=self._source, trace_id=trace_id, causation_id=causation_id,
            correlation_id=correlation_id, idempotency_key=domain_idempotency_key(event_type=event_type, world_id=self._ledger.world_id, payload=payload) or "activity:" + _digest([command_id, event_type]), payload=payload)

    def _commit_idempotently(self, *, event: WorldEvent, projection, command_id: str) -> CommitResult:
        existing = self._ledger.lookup_event_commit(event.event_id)
        if existing is not None:
            persisted, commit = existing
            if persisted != event:
                raise ValueError("activity transition command has conflicting durable content")
            return commit
        cursor = ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)
        return self._ledger.commit_at_cursor((event,), expected_cursor=cursor,
            commit_id="activity-transition:" + _digest([self._ledger.world_id, command_id]))


class ActivityPlanTransitionCommand(FrozenModel):
    command_id: str = Field(min_length=1, max_length=256)
    world_id: str = Field(min_length=1, max_length=256)
    source_observation_id: str = Field(min_length=1, max_length=512)
    plan_id: str = Field(min_length=1, max_length=512)
    operation: Literal["start", "pause", "resume", "complete", "abandon"]
    policy_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def policies_are_canonical(self) -> "ActivityPlanTransitionCommand":
        if tuple(sorted(set(self.policy_refs))) != self.policy_refs:
            raise ValueError("activity transition policies must be sorted and unique")
        return self


__all__ = ["ActivityPlanCommand", "ActivityPlanRuntime", "ActivityPlanTransitionCommand"]
