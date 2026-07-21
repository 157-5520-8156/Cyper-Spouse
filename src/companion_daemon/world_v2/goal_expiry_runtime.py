"""Pure mechanical Goal-expiry event construction for ``WorldRuntime.advance``.

DORMANT in effect — this mechanical lane only fires on existing Goal heads,
and the Goal authority has no producer, so no production world has ever built
an expiry event.  Before wiring a Goal producer, read the Producer-First
Authority rule in CONTEXT.md.
"""

from __future__ import annotations

from .event_identity import domain_idempotency_key
from .goal_authority_events import (
    V2GoalExpiredPayload,
    v2_goal_expiry_hash,
    v2_goal_expiry_id,
)
from .goal_authority_reducers import (
    V2_GOAL_EXPIRY_POLICY_DIGEST,
    V2_GOAL_EXPIRY_POLICY_VERSION,
    V2_GOAL_POLICY_REFS,
)
from .goal_situation_schemas import (
    ClockCauseAuthority,
    V2GoalExpiredTerminalReason,
    V2GoalOrigin,
    V2GoalProjection,
    v2_goal_semantic_fingerprint,
)
from .schemas import ClockObservation, ClockTransitionProjection, WorldEvent


def build_due_goal_expiry_events(
    *,
    world_id: str,
    goals: tuple[V2GoalProjection, ...],
    clock: ClockObservation,
    clock_transition: ClockTransitionProjection,
) -> list[WorldEvent]:
    """Select eligible heads by installed policy and build them in stable order."""

    return [
        build_goal_expiry_event(
            world_id=world_id,
            goal=goal,
            clock=clock,
            clock_transition=clock_transition,
        )
        for goal in sorted(goals, key=lambda item: item.goal_id)
        if goal.values.status in {"active", "paused", "blocked"}
        and goal.values.due_window is not None
        and goal.values.due_window.ends_at <= clock_transition.logical_time_to
    ]


def build_goal_expiry_event(
    *,
    world_id: str,
    goal: V2GoalProjection,
    clock: ClockObservation,
    clock_transition: ClockTransitionProjection,
) -> WorldEvent:
    """Build the one canonical expiry event for a due, non-terminal Goal head."""

    if clock.world_id != world_id or (
        clock.logical_time_from != clock_transition.logical_time_from
        or clock.logical_time_to != clock_transition.logical_time_to
    ):
        raise ValueError("Goal expiry Clock authority does not match its observation")

    expiry_id = v2_goal_expiry_id(
        world_id=world_id,
        goal_id=goal.goal_id,
        expected_entity_revision=goal.entity_revision,
        clock_event_ref=clock_transition.clock_event_ref,
        policy_digest=V2_GOAL_EXPIRY_POLICY_DIGEST,
    )
    event_id = f"event:{expiry_id}"
    change_id = f"change:{expiry_id}"
    transition_id = f"transition:{expiry_id}"
    origin = V2GoalOrigin(
        change_id=change_id,
        transition_id=transition_id,
        policy_refs=V2_GOAL_POLICY_REFS,
        accepted_event_ref=event_id,
    )
    terminal = V2GoalExpiredTerminalReason(
        due_window=goal.values.due_window,
        clock_projection_ref=clock_transition.clock_event_ref,
        policy_digest=V2_GOAL_EXPIRY_POLICY_DIGEST,
        privacy_class=goal.values.privacy_class,
    )
    values = goal.values.model_copy(
        update={"status": "expired", "blockers": (), "terminal_reason": terminal}
    )
    after = goal.model_copy(
        update={
            "entity_revision": goal.entity_revision + 1,
            "semantic_fingerprint": v2_goal_semantic_fingerprint(
                goal_id=goal.goal_id,
                actor_ref=goal.actor_ref,
                values=values,
                policy_refs=origin.policy_refs,
            ),
            "values": values,
            "origin": origin,
            "updated_at": clock.logical_time_to,
            "closed_at": clock.logical_time_to,
        }
    )
    cause = ClockCauseAuthority(
        clock_event_ref=clock_transition.clock_event_ref,
        clock_world_revision=clock_transition.computed_world_revision,
        clock_payload_hash=clock_transition.payload_hash,
        logical_time_from=clock_transition.logical_time_from,
        logical_time_to=clock_transition.logical_time_to,
        policy_version=clock_transition.installed_policy_version,
        policy_digest=clock_transition.installed_policy_digest,
    )
    raw_payload = {
        "operation": "expire",
        "authority_lane": "clock_runtime",
        "world_id": world_id,
        "expiry_id": expiry_id,
        "change_id": change_id,
        "transition_id": transition_id,
        "expected_entity_revision": goal.entity_revision,
        "evaluated_world_revision": clock_transition.computed_world_revision,
        "policy_refs": V2_GOAL_POLICY_REFS,
        "goal_before": goal,
        "goal_after": after,
        "cause_authority": cause,
        "terminal_reason": terminal,
        "removed_blocker_fingerprints": tuple(
            sorted(item.blocker_semantic_hash for item in goal.values.blockers)
        ),
        "policy_version": V2_GOAL_EXPIRY_POLICY_VERSION,
        "policy_digest": V2_GOAL_EXPIRY_POLICY_DIGEST,
        "mechanical_change_hash": "0" * 64,
    }
    raw_payload["mechanical_change_hash"] = v2_goal_expiry_hash(raw_payload)
    payload = V2GoalExpiredPayload.model_validate(raw_payload).model_dump(mode="json")
    return WorldEvent.from_payload(
        schema_version=clock.schema_version,
        event_id=event_id,
        world_id=world_id,
        event_type="V2GoalExpired",
        logical_time=clock.logical_time_to,
        created_at=clock.created_at,
        actor="system:goal-clock",
        source="scheduler",
        trace_id=clock.trace_id,
        causation_id=clock_transition.clock_event_ref,
        correlation_id=clock.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="V2GoalExpired", world_id=world_id, payload=payload
        )
        or expiry_id,
        payload=payload,
    )
