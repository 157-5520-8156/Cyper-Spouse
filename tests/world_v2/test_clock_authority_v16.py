from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.clock_authority import (
    CLOCK_AUTHORITY_POLICY_DIGEST,
    CLOCK_AUTHORITY_POLICY_VERSION,
    resolve_latest_clock,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, WorldEvent


NOW = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)


def clock_event(
    *,
    event_id: str = "event:clock:1",
    origin: datetime = NOW,
    target: datetime = NOW + timedelta(minutes=1),
    extra: dict[str, object] | None = None,
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id="world:test",
        event_type="ClockAdvanced",
        logical_time=target,
        created_at=target,
        actor="clock:runtime",
        source="clock_runtime",
        trace_id=f"trace:{event_id}",
        causation_id=f"cause:{event_id}",
        correlation_id=f"correlation:{event_id}",
        idempotency_key=f"idempotency:{event_id}",
        payload={
            "logical_time_from": origin.isoformat(),
            "logical_time_to": target.isoformat(),
            **(extra or {}),
        },
    )


def state_at_now() -> ReducerState:
    return ReducerState(
        logical_time=NOW,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id="event:world:start",
                event_type="WorldStarted",
                world_revision=1,
                payload_hash="a" * 64,
                logical_time=NOW,
            ),
        ),
    )


def test_clock_advanced_freezes_computed_authority_projection() -> None:
    assert (
        CLOCK_AUTHORITY_POLICY_DIGEST
        == "777eb12b5de741f9f4f64e69d87ad5d7f9ea38aabe5d0e90a979160e4cea71e5"
    )
    event = clock_event()
    reduced = reduce_event(state_at_now(), event)

    assert reduced.logical_time == NOW + timedelta(minutes=1)
    assert len(reduced.clock_transition_history) == 1
    frozen = reduced.clock_transition_history[0]
    assert frozen.clock_event_ref == event.event_id
    assert frozen.computed_world_revision == 2
    assert frozen.payload_hash == event.payload_hash
    assert frozen.logical_time_from == NOW
    assert frozen.logical_time_to == reduced.logical_time
    assert frozen.installed_policy_version == CLOCK_AUTHORITY_POLICY_VERSION
    assert frozen.installed_policy_digest == CLOCK_AUTHORITY_POLICY_DIGEST
    assert resolve_latest_clock(
        reduced.clock_transition_history,
        current_logical_time=reduced.logical_time,
    ) == frozen


def test_clock_payload_cannot_override_computed_authority_fields() -> None:
    forged = clock_event(
        extra={
            "computed_world_revision": 999,
            "clock_event_ref": "event:forged",
            "payload_hash": "f" * 64,
            "installed_policy_version": "attacker-policy.1",
            "installed_policy_digest": "f" * 64,
        }
    )
    with pytest.raises(ValueError):
        reduce_event(state_at_now(), forged)


def test_first_clock_rejects_naive_payload_timestamps() -> None:
    event = clock_event(
        origin=NOW,
        target=NOW + timedelta(minutes=1),
        extra={"logical_time_from": "2026-07-15T18:00:00"},
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        reduce_event(ReducerState(), event)


@pytest.mark.parametrize("attack", ("policy", "current_time", "revision", "event_ref"))
def test_latest_clock_resolver_fails_closed_on_tampered_history(attack: str) -> None:
    reduced = reduce_event(state_at_now(), clock_event())
    frozen = reduced.clock_transition_history[0]
    history = reduced.clock_transition_history
    current = reduced.logical_time
    if attack == "policy":
        history = (
            frozen.model_copy(update={"installed_policy_digest": "f" * 64}),
        )
    elif attack == "current_time":
        current = NOW + timedelta(minutes=2)
    elif attack == "revision":
        history = (
            frozen,
            frozen.model_copy(
                update={
                    "clock_event_ref": "event:clock:2",
                    "computed_world_revision": frozen.computed_world_revision,
                }
            ),
        )
    else:
        history = (
            frozen,
            frozen.model_copy(update={"computed_world_revision": 3}),
        )
    with pytest.raises(ValueError, match="clock|Clock"):
        resolve_latest_clock(history, current_logical_time=current)


def test_latest_clock_rejects_invalid_earlier_policy_and_overlap() -> None:
    first = reduce_event(state_at_now(), clock_event())
    second_time = NOW + timedelta(minutes=2)
    second = reduce_event(
        first,
        clock_event(
            event_id="event:clock:2",
            origin=first.logical_time,
            target=second_time,
        ),
    )
    earlier, latest = second.clock_transition_history
    wrong_policy = (
        earlier.model_copy(update={"installed_policy_digest": "f" * 64}),
        latest,
    )
    with pytest.raises(ValueError, match="invalid authority entry"):
        resolve_latest_clock(wrong_policy, current_logical_time=second_time)

    overlap = (
        earlier,
        latest.model_copy(
            update={"logical_time_from": earlier.logical_time_to - timedelta(seconds=1)}
        ),
    )
    with pytest.raises(ValueError, match="overlaps"):
        resolve_latest_clock(overlap, current_logical_time=second_time)


def test_clock_history_is_only_part_of_v16_semantics() -> None:
    reduced = reduce_event(state_at_now(), clock_event())
    legacy = reduced.semantic_payload(
        world_id="world:test",
        world_revision=2,
        reducer_bundle_version="world-v2-reducers.15",
    )
    current = reduced.semantic_payload(
        world_id="world:test",
        world_revision=2,
        reducer_bundle_version="world-v2-reducers.16",
    )
    assert "clock_transition_history" not in legacy
    assert current["clock_transition_history"] == (
        reduced.clock_transition_history[0].model_dump(mode="json"),
    )


def test_later_observation_temporarily_stales_clock_until_next_tick() -> None:
    reduced = reduce_event(state_at_now(), clock_event())
    observed_at = NOW + timedelta(minutes=5)
    observation = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:after-clock",
        world_id="world:test",
        event_type="ObservationRecorded",
        logical_time=observed_at,
        created_at=observed_at,
        actor="actor:user",
        source="test",
        trace_id="trace:observation:after-clock",
        causation_id="cause:observation:after-clock",
        correlation_id="correlation:observation:after-clock",
        idempotency_key="idempotency:observation:after-clock",
        payload={"observation_id": "observation:after-clock"},
    )
    after_observation = reduce_event(reduced, observation)
    assert after_observation.logical_time == observed_at
    with pytest.raises(ValueError, match="not installed or current"):
        resolve_latest_clock(
            after_observation.clock_transition_history,
            current_logical_time=after_observation.logical_time,
        )

    recovered_at = observed_at + timedelta(minutes=1)
    recovered = reduce_event(
        after_observation,
        clock_event(
            event_id="event:clock:recover-after-observation",
            origin=observed_at,
            target=recovered_at,
        ),
    )
    latest = resolve_latest_clock(
        recovered.clock_transition_history,
        current_logical_time=recovered.logical_time,
    )
    assert latest.logical_time_from == observed_at
    assert latest.logical_time_to == recovered_at
