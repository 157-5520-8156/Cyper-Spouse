"""Regression pins for the interaction-appraisal inline ledger hot path.

Production forensics (2026-07-20): the immediate-emotion inline phase spent
minutes per turn because ``observation_events_at`` re-verified its boundary
commit by replaying the whole ledger from genesis, and the affect rebase
re-read its audit cursor through another full-history replay.  These tests
pin the access *shape* through the ledger's own counters instead of wall
clocks, so they stay deterministic under load.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.clock_authority import append_clock_transition
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import ObservationEventLocator
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    Observation,
    ProjectionCursor,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
WORLD = "world:inline-appraisal-perf"


def _observation(marker: str) -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id=f"observation:perf:{marker}",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id=f"trace:perf:{marker}",
        causation_id=f"cause:perf:{marker}",
        correlation_id=f"correlation:perf:{marker}",
        source="perf-test",
        source_event_id=f"message:perf:{marker}",
        actor="user:perf",
        channel="test",
        payload_ref=f"payload:perf:{marker}",
        payload_hash="sha256:" + "a" * 64,
        text="小事一件。",
        received_at=NOW,
        reply_context={"target": "user:perf"},
    )


def _observation_event(observation: Observation) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:trigger:observation:{observation.source}:{observation.source_event_id}",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded",
            world_id=WORLD,
            payload=observation.model_dump(mode="json"),
        )
        or f"observation:{observation.source}:{observation.source_event_id}",
        payload=observation.model_dump(mode="json"),
    )


def _plain_event(event_id: str, observation_id: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="perf-test",
        trace_id=f"trace:{event_id}",
        causation_id=f"cause:{event_id}",
        correlation_id=f"correlation:{event_id}",
        idempotency_key=event_id,
        payload={"observation_id": observation_id},
    )


def _cursor(committed) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=committed.world_revision,
        deliberation_revision=committed.deliberation_revision,
        ledger_sequence=committed.ledger_sequence,
    )


def test_observation_events_at_verifies_boundary_without_history_replay(tmp_path) -> None:
    """The same-turn appraisal source read must stay O(commit), not O(history)."""

    ledger = SQLiteWorldLedger(path=tmp_path / "boundary.sqlite3", world_id=WORLD)
    try:
        ledger.commit(
            [_plain_event("event:perf:prefix", "obs:perf:prefix")],
            expected_world_revision=0,
            expected_deliberation_revision=0,
            commit_id="commit:perf:prefix",
        )
        observation = _observation("boundary")
        event = _observation_event(observation)
        committed = ledger.commit(
            [event],
            expected_world_revision=1,
            expected_deliberation_revision=0,
            commit_id="commit:perf:boundary",
        )
        locator = ObservationEventLocator.for_message(
            world_id=WORLD,
            observation_id=observation.observation_id,
            source=observation.source,
            source_event_id=observation.source_event_id,
        )
        before = ledger.performance_counters()

        located = ledger.observation_events_at((locator,), cursor=_cursor(committed))

        assert len(located) == 1
        assert located[0].event == event
        after = ledger.performance_counters()
        assert after.total_replay_calls == before.total_replay_calls
        assert after.historical_replay_calls == before.historical_replay_calls
    finally:
        ledger.close()


def test_idempotent_commit_retry_does_not_replay_history(tmp_path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "retry.sqlite3", world_id=WORLD)
    try:
        ledger.commit(
            [_plain_event("event:perf:first", "obs:perf:first")],
            expected_world_revision=0,
            expected_deliberation_revision=0,
            commit_id="commit:perf:first",
        )
        events = [_plain_event("event:perf:second", "obs:perf:second")]
        committed = ledger.commit(
            events,
            expected_world_revision=1,
            expected_deliberation_revision=0,
            commit_id="commit:perf:second",
        )
        before = ledger.performance_counters()

        retried = ledger.commit(
            events,
            expected_world_revision=1,
            expected_deliberation_revision=0,
            commit_id="commit:perf:second",
        )

        assert retried == committed
        after = ledger.performance_counters()
        assert after.total_replay_calls == before.total_replay_calls
    finally:
        ledger.close()


def test_project_at_recent_pre_commit_head_is_served_from_memory(tmp_path) -> None:
    """An audit cursor is typically the head from a few commits ago.

    The commit path must remember the projection it just displaced so a
    same-turn ``project_at`` (appraisal pin, affect rebase) never replays the
    ledger from genesis, and the remembered value must be byte-identical to
    an independent replay of that cursor.
    """

    ledger = SQLiteWorldLedger(path=tmp_path / "pre-commit-head.sqlite3", world_id=WORLD)
    try:
        first = ledger.commit(
            [_plain_event("event:perf:head-1", "obs:perf:head-1")],
            expected_world_revision=0,
            expected_deliberation_revision=0,
            commit_id="commit:perf:head-1",
        )
        audit_cursor = _cursor(first)
        assert ledger.project().world_revision == 1
        ledger.commit(
            [_plain_event("event:perf:head-2", "obs:perf:head-2")],
            expected_world_revision=1,
            expected_deliberation_revision=0,
            commit_id="commit:perf:head-2",
        )
        before = ledger.performance_counters()

        remembered = ledger.project_at(audit_cursor)

        after = ledger.performance_counters()
        assert after.historical_replay_calls == before.historical_replay_calls
        assert after.total_replay_calls == before.total_replay_calls
        assert after.historical_projection_hits == before.historical_projection_hits + 1
        assert remembered.observation_refs == ("obs:perf:head-1",)

        # Independent verification: a fresh adapter replaying the same cursor
        # must produce the exact same projection (same semantic hash contract).
        independent = SQLiteWorldLedger(
            path=tmp_path / "pre-commit-head.sqlite3", world_id=WORLD
        )
        try:
            assert independent.project_at(audit_cursor) == remembered
        finally:
            independent.close()
    finally:
        ledger.close()


def _clock_event(
    *,
    event_id: str,
    origin: datetime,
    target: datetime,
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
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
        },
    )


def _state_with_clock_history(length: int) -> ReducerState:
    state = ReducerState(
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
    for index in range(length):
        state = reduce_event(
            state,
            _clock_event(
                event_id=f"event:clock:{index}",
                origin=state.logical_time,
                target=state.logical_time + timedelta(minutes=1),
            ),
        )
    return state


def test_clock_advanced_incremental_validation_matches_full_validation() -> None:
    """The reducer's prefix-validated append must accept and reject exactly
    like the full-history validation it replaces."""

    state = _state_with_clock_history(4)
    event = _clock_event(
        event_id="event:clock:next",
        origin=state.logical_time,
        target=state.logical_time + timedelta(minutes=5),
    )
    revision = len(state.committed_world_event_refs) + 1

    full = append_clock_transition(
        state.clock_transition_history,
        event=event,
        current_logical_time=state.logical_time,
        computed_world_revision=revision,
    )
    incremental = append_clock_transition(
        state.clock_transition_history,
        event=event,
        current_logical_time=state.logical_time,
        computed_world_revision=revision,
        prefix_validated=True,
    )
    assert incremental == full

    rejections = (
        # Backwards interval.
        dict(
            event=_clock_event(
                event_id="event:clock:backwards",
                origin=state.logical_time,
                target=state.logical_time - timedelta(minutes=1),
            ),
            current_logical_time=state.logical_time,
            computed_world_revision=revision,
        ),
        # From does not match the current logical time.
        dict(
            event=_clock_event(
                event_id="event:clock:gap",
                origin=state.logical_time + timedelta(minutes=1),
                target=state.logical_time + timedelta(minutes=2),
            ),
            current_logical_time=state.logical_time,
            computed_world_revision=revision,
        ),
        # World revision does not advance.
        dict(
            event=event,
            current_logical_time=state.logical_time,
            computed_world_revision=state.clock_transition_history[-1].computed_world_revision,
        ),
        # Duplicate clock event ref.
        dict(
            event=_clock_event(
                event_id=state.clock_transition_history[-1].clock_event_ref,
                origin=state.logical_time,
                target=state.logical_time + timedelta(minutes=5),
            ),
            current_logical_time=state.logical_time,
            computed_world_revision=revision,
        ),
        # History already ahead of the supplied logical time.
        dict(
            event=event,
            current_logical_time=state.logical_time - timedelta(minutes=2),
            computed_world_revision=revision,
        ),
    )
    for kwargs in rejections:
        with pytest.raises(ValueError):
            append_clock_transition(state.clock_transition_history, **kwargs)
        with pytest.raises(ValueError):
            append_clock_transition(
                state.clock_transition_history, prefix_validated=True, **kwargs
            )


def test_clock_advanced_reducer_still_rejects_invalid_advance() -> None:
    state = _state_with_clock_history(2)
    stale = _clock_event(
        event_id="event:clock:stale",
        origin=state.logical_time - timedelta(minutes=1),
        target=state.logical_time + timedelta(minutes=1),
    )
    with pytest.raises(ValueError):
        reduce_event(state, stale)

    advanced = reduce_event(
        state,
        _clock_event(
            event_id="event:clock:ok",
            origin=state.logical_time,
            target=state.logical_time + timedelta(minutes=1),
        ),
    )
    assert advanced.clock_transition_history[-1].clock_event_ref == "event:clock:ok"
