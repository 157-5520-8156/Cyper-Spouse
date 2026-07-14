from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from companion_daemon.world_v2 import ClockObservation, WorldRuntime
from companion_daemon.world_v2.batch_invariants import validate_commit_batch
from companion_daemon.world_v2.clock_authority import (
    CLOCK_EXPLICIT_POLICY_DIGEST,
    CLOCK_EXPLICIT_POLICY_VERSION,
    append_clock_transition,
)
from companion_daemon.world_v2.errors import IdempotencyConflict
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.goal_expiry_runtime import build_due_goal_expiry_events
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import CommitResult
from companion_daemon.world_v2.schemas import (
    AffectComponentProjection,
    AffectDecayProfileProjection,
    AffectEpisodeProjection,
    AffectOrigin,
    AppraisalMeaningRef,
    EvidenceRef,
    affect_decay_config_digest,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from test_goal_authority_v16 import (
    blocker,
    goal_projection,
    internal_cause,
    record_accept_open_goal,
)


WORLD_ID = "world:goal-integration"
NOW = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
OPEN_TIME = datetime(2026, 7, 15, 17, 0, tzinfo=UTC)


class RecordingLedger(WorldLedger):
    def __init__(self, *, world_id: str) -> None:
        super().__init__(world_id=world_id)
        self.last_commit_events = ()

    def commit(self, events, **kwargs):
        result = super().commit(events, **kwargs)
        self.last_commit_events = tuple(events)
        return result


class AmbiguousGoalExpiryLedger(RecordingLedger):
    def __init__(self, *, world_id: str) -> None:
        super().__init__(world_id=world_id)
        self.raise_after_tick: str | None = None

    def commit(self, events, **kwargs):
        result = super().commit(events, **kwargs)
        if self.raise_after_tick is not None and events[0].event_id == self.raise_after_tick:
            self.raise_after_tick = None
            raise IdempotencyConflict("simulated ambiguous Goal-expiry commit")
        return result


class RejectingGoalExpiryLedger(WorldLedger):
    def __init__(self, *, world_id: str) -> None:
        super().__init__(world_id=world_id)
        self.reject_once = True

    def commit(self, events, **kwargs):
        if self.reject_once and any(event.event_type == "V2GoalExpired" for event in events):
            self.reject_once = False
            raise ValueError("simulated atomic batch rejection")
        return super().commit(events, **kwargs)


class AmbiguousRecoveryLedger(WorldLedger):
    def __init__(self, *, world_id: str) -> None:
        super().__init__(world_id=world_id)
        self.raise_after_recovery = False

    def commit(self, events, **kwargs):
        result = super().commit(events, **kwargs)
        if self.raise_after_recovery and events[0].event_type == "V2GoalExpired":
            self.raise_after_recovery = False
            raise IdempotencyConflict("simulated ambiguous recovery commit")
        return result


class StaticProjectionLedger:
    """Ledger boundary double for testing Runtime enumeration of legal Goal heads."""

    blocks_event_loop = False

    def __init__(self, projection) -> None:
        self.world_id = WORLD_ID
        self.projection = projection
        self.committed_events = ()

    def project(self):
        return self.projection

    def project_at(self, cursor):
        raise AssertionError("advance must not request a historical projection")

    def lookup_event_commit(self, event_id):
        return None

    def commit(self, events, **kwargs):
        validate_commit_batch(
            events, expected_world_revision=kwargs["expected_world_revision"]
        )
        self.committed_events = tuple(events)
        return CommitResult(
            world_revision=self.projection.world_revision + len(events),
            deliberation_revision=self.projection.deliberation_revision,
            ledger_sequence=self.projection.ledger_sequence + len(events),
            event_ids=tuple(event.event_id for event in events),
        )


def clock(
    *,
    tick_id: str = "goal-expiry-1",
    logical_time_from: datetime = OPEN_TIME,
    logical_time_to: datetime = NOW,
    policy_version: str | None = None,
    policy_digest: str | None = None,
) -> ClockObservation:
    return ClockObservation(
        schema_version="world-v2.1",
        tick_id=tick_id,
        world_id=WORLD_ID,
        logical_time=logical_time_to,
        created_at=logical_time_to,
        trace_id=f"trace:{tick_id}",
        causation_id=f"scheduler:{tick_id}",
        correlation_id="scheduler:goal-expiry",
        logical_time_from=logical_time_from,
        logical_time_to=logical_time_to,
        reason="scheduled_tick",
        policy_version=policy_version,
        policy_digest=policy_digest,
    )


def clock_event(value: ClockObservation) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version=value.schema_version,
        event_id=f"event:trigger:clock:{value.tick_id}",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=value.logical_time_to,
        created_at=value.created_at,
        actor="system:clock",
        source="scheduler",
        trace_id=value.trace_id,
        causation_id=value.causation_id,
        correlation_id=value.correlation_id,
        idempotency_key=f"clock:{value.tick_id}",
        payload=value.model_dump(mode="json"),
    )


async def bootstrap_goal_clock(runtime: WorldRuntime) -> None:
    await runtime.advance(
        clock(
            tick_id="goal-open-clock",
            logical_time_from=OPEN_TIME - timedelta(minutes=1),
            logical_time_to=OPEN_TIME,
        )
    )


@pytest.mark.asyncio
async def test_advance_expires_a_due_active_goal_in_the_clock_commit() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:due",
        event_id="event:goal:due:opened",
    )
    before = ledger.project()

    outcome = await runtime.advance(clock())

    after = ledger.project()
    goal = next(item for item in after.goals if item.goal_id == "goal:due")
    assert goal.values.status == "expired"
    assert goal.closed_at == NOW
    assert goal.entity_revision == 2
    assert after.goal_transitions[-1].operation == "expire"
    assert outcome.committed_world_revision == before.world_revision + 2


@pytest.mark.asyncio
async def test_advance_expires_due_goals_in_stable_goal_id_order_only() -> None:
    ledger = RecordingLedger(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:z-last",
        event_id="event:goal:z-last:opened",
    )
    record_accept_open_goal(
        ledger,
        goal_id="goal:a-first",
        event_id="event:goal:a-first:opened",
    )
    before = ledger.project()

    target_clock = clock()
    first = await runtime.advance(target_clock)

    event_types = tuple(event.event_type for event in ledger.last_commit_events)
    assert event_types == ("ClockAdvanced", "V2GoalExpired", "V2GoalExpired")
    expired_goal_ids = tuple(
        json.loads(event.payload_json)["goal_after"]["goal_id"]
        for event in ledger.last_commit_events[1:]
    )
    assert expired_goal_ids == ("goal:a-first", "goal:z-last")
    payloads = tuple(
        json.loads(event.payload_json) for event in ledger.last_commit_events[1:]
    )
    assert all(
        payload["cause_authority"]
        == {
            "kind": "clock",
            "clock_event_ref": f"event:trigger:clock:{target_clock.tick_id}",
            "clock_world_revision": before.world_revision + 1,
            "clock_payload_hash": ledger.last_commit_events[0].payload_hash,
            "logical_time_from": OPEN_TIME.isoformat().replace("+00:00", "Z"),
            "logical_time_to": NOW.isoformat().replace("+00:00", "Z"),
            "policy_version": before.clock_transition_history[-1].installed_policy_version,
            "policy_digest": before.clock_transition_history[-1].installed_policy_digest,
        }
        for payload in payloads
    )
    after_first = ledger.project()
    assert await runtime.advance(target_clock) == first
    assert ledger.project() == after_first


@pytest.mark.asyncio
async def test_a_not_due_goal_is_caught_up_by_the_next_clock() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:later",
        event_id="event:goal:later:opened",
    )
    opened_revision = ledger.project().world_revision

    await runtime.advance(
        clock(
            tick_id="before-due",
            logical_time_from=OPEN_TIME,
            logical_time_to=OPEN_TIME + timedelta(minutes=30),
        )
    )
    not_due = ledger.project()
    assert not_due.goals[0].values.status == "active"
    assert not_due.world_revision == opened_revision + 1

    await runtime.advance(
        clock(
            tick_id="at-due",
            logical_time_from=OPEN_TIME + timedelta(minutes=30),
            logical_time_to=NOW,
        )
    )
    caught_up = ledger.project()
    assert caught_up.goals[0].values.status == "expired"
    assert caught_up.world_revision == opened_revision + 3


@pytest.mark.asyncio
async def test_goal_expiry_binds_an_explicit_installed_clock_policy() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:explicit-clock-policy",
        event_id="event:goal:explicit-clock-policy:opened",
    )

    await runtime.advance(
        clock(
            tick_id="explicit-clock-policy",
            policy_version=CLOCK_EXPLICIT_POLICY_VERSION,
            policy_digest=CLOCK_EXPLICIT_POLICY_DIGEST,
        )
    )

    projection = ledger.project()
    latest_clock = projection.clock_transition_history[-1]
    expiry = projection.goal_transitions[-1]
    assert latest_clock.installed_policy_version == CLOCK_EXPLICIT_POLICY_VERSION
    assert latest_clock.installed_policy_digest == CLOCK_EXPLICIT_POLICY_DIGEST
    assert expiry.cause_authority.policy_version == CLOCK_EXPLICIT_POLICY_VERSION
    assert expiry.cause_authority.policy_digest == CLOCK_EXPLICIT_POLICY_DIGEST


@pytest.mark.asyncio
async def test_goal_expiry_identity_is_scoped_by_world() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:world-scoped",
        event_id="event:goal:world-scoped:opened",
    )
    before = ledger.project()
    target = clock(tick_id="world-scoped")
    tick = clock_event(target)
    pending = append_clock_transition(
        before.clock_transition_history,
        event=tick,
        current_logical_time=before.logical_time,
        computed_world_revision=before.world_revision + 1,
    )[-1]

    first = build_due_goal_expiry_events(
        world_id=WORLD_ID,
        goals=before.goals,
        clock=target,
        clock_transition=pending,
    )[0]
    other_world = "world:goal-integration:other"
    second = build_due_goal_expiry_events(
        world_id=other_world,
        goals=before.goals,
        clock=target.model_copy(update={"world_id": other_world}),
        clock_transition=pending,
    )[0]

    assert first.event_id != second.event_id
    assert json.loads(first.payload_json)["expiry_id"] != json.loads(
        second.payload_json
    )["expiry_id"]


@pytest.mark.asyncio
async def test_due_goal_expiry_and_affect_decay_share_one_clock_commit() -> None:
    seed = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_runtime = WorldRuntime(world_id=WORLD_ID, ledger=seed)
    await bootstrap_goal_clock(seed_runtime)
    record_accept_open_goal(
        seed,
        goal_id="goal:with-affect",
        event_id="event:goal:with-affect:opened",
    )
    meaning = AppraisalMeaningRef(
        appraisal_id="appraisal:runtime",
        hypothesis_id="hypothesis:runtime",
        source_cluster_ref="cluster:runtime",
        accepted_change_id="change:appraisal:runtime",
        accepted_transition_id="transition:appraisal:runtime",
    )
    profile = AffectDecayProfileProjection(
        half_life_seconds=3_600,
        floor_bp=300,
        delay_seconds=0,
        config_version="affect-decay.1",
        config_digest=affect_decay_config_digest(
            kind="exponential_half_life",
            half_life_seconds=3_600,
            floor_bp=300,
            delay_seconds=0,
            config_version="affect-decay.1",
        ),
    )
    episode = AffectEpisodeProjection(
        episode_id="affect:runtime",
        entity_revision=1,
        origin=AffectOrigin(
            change_id="change:affect:runtime",
            transition_id="transition:affect:runtime",
            policy_refs=("policy:affect-v1",),
            matrix_catalog_version="affect-matrix.1",
            accepted_event_ref="event:affect:runtime",
        ),
        components=(
            AffectComponentProjection(
                component_id="component:hurt:runtime",
                dimension="hurt",
                source_cluster_ref="cluster:runtime",
                appraisal_refs=(meaning,),
                intensity_bp=4_200,
                decay_anchor_intensity_bp=4_200,
                opened_at=OPEN_TIME,
                decay_anchor_at=OPEN_TIME,
                decay_not_before=OPEN_TIME,
                last_stimulus_at=OPEN_TIME,
                last_updated_at=OPEN_TIME,
                decay_profile=profile,
                residue_bp=500,
            ),
        ),
        evidence_refs=(
            EvidenceRef(
                ref_id="observation:runtime-affect",
                evidence_type="observed_message",
                claim_purpose="private_hypothesis",
            ),
        ),
        opened_at=OPEN_TIME,
        updated_at=OPEN_TIME,
        status="active",
    )
    ledger = StaticProjectionLedger(
        seed.project().model_copy(update={"affect_episodes": (episode,)})
    )
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)

    await runtime.advance(clock(tick_id="goal-and-affect"))

    assert tuple(event.event_type for event in ledger.committed_events) == (
        "ClockAdvanced",
        "V2GoalExpired",
        "AffectEpisodeDecayed",
    )
    expiry_payload = json.loads(ledger.committed_events[1].payload_json)
    decay_payload = json.loads(ledger.committed_events[2].payload_json)
    assert expiry_payload["cause_authority"]["clock_event_ref"] == (
        "event:trigger:clock:goal-and-affect"
    )
    assert datetime.fromisoformat(decay_payload["to_logical_time"]) == NOW
    seed_projection = seed.project()
    state = ReducerState(
        logical_time=seed_projection.logical_time,
        committed_world_event_refs=seed_projection.committed_world_event_refs,
        clock_transition_history=seed_projection.clock_transition_history,
        goals=seed_projection.goals,
        goal_transitions=seed_projection.goal_transitions,
        affect_episodes=(episode,),
    )
    for event in ledger.committed_events:
        state = reduce_event(state, event)
    assert state.goals[0].values.status == "expired"
    assert state.affect_episodes[0].entity_revision == 2
    assert state.affect_episodes[0].components[0].intensity_bp < 4_200


@pytest.mark.asyncio
async def test_a_terminal_goal_is_not_expired_again_on_a_later_clock() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:once",
        event_id="event:goal:once:opened",
    )
    await runtime.advance(clock())
    expired = ledger.project().goals[0]

    await runtime.advance(
        clock(
            tick_id="after-terminal",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
        )
    )

    after = ledger.project().goals[0]
    assert after == expired


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("paused", "blocked"))
async def test_advance_expires_paused_and_blocked_goal_heads(status: str) -> None:
    seed = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_runtime = WorldRuntime(world_id=WORLD_ID, ledger=seed)
    await bootstrap_goal_clock(seed_runtime)
    opened = record_accept_open_goal(
        seed,
        goal_id=f"goal:{status}",
        event_id=f"event:goal:{status}:opened",
    )
    blockers = ()
    if status == "blocked":
        cause = internal_cause(
            evaluated_world_revision=seed.project().world_revision,
            logical_time=OPEN_TIME,
            trigger_ref="trigger:blocked-runtime",
            decision_slot="goal-governance:blocked-runtime",
        )
        blockers = (
            blocker(
                blocker_id="blocker:runtime",
                blocker_class="resource_constraint",
                basis=cause.basis,
                text="I cannot finish this before the deadline.",
            ),
        )
    head = goal_projection(
        revision=opened.entity_revision + 1,
        values=opened.values.model_copy(update={"status": status, "blockers": blockers}),
        event_ref=f"event:goal:{status}:current",
        updated_at=OPEN_TIME,
        goal_id=opened.goal_id,
        opened_at=opened.opened_at,
    )
    ledger = StaticProjectionLedger(
        seed.project().model_copy(update={"goals": (head,)})
    )
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)

    await runtime.advance(clock(tick_id=f"expire-{status}"))

    assert tuple(event.event_type for event in ledger.committed_events) == (
        "ClockAdvanced",
        "V2GoalExpired",
    )
    payload = json.loads(ledger.committed_events[1].payload_json)
    assert payload["goal_before"]["values"]["status"] == status
    assert payload["goal_after"]["values"]["status"] == "expired"
    assert payload["goal_after"]["values"]["blockers"] == []
    assert payload["removed_blocker_fingerprints"] == [
        item.blocker_semantic_hash for item in blockers
    ]


@pytest.mark.asyncio
async def test_goal_expiry_joins_an_ambiguous_atomic_clock_commit() -> None:
    ledger = AmbiguousGoalExpiryLedger(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:raced",
        event_id="event:goal:raced:opened",
    )
    target = clock(tick_id="raced-expiry")
    ledger.raise_after_tick = f"event:trigger:clock:{target.tick_id}"

    outcome = await runtime.advance(target)

    projection = ledger.project()
    assert projection.goals[0].values.status == "expired"
    assert tuple(item.operation for item in projection.goal_transitions).count("expire") == 1
    assert outcome.ledger_sequence == projection.ledger_sequence


@pytest.mark.asyncio
async def test_retry_of_latest_clock_recovers_only_missing_due_goals() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:a-already-expired",
        event_id="event:goal:a-already-expired:opened",
    )
    record_accept_open_goal(
        ledger,
        goal_id="goal:z-missing-expiry",
        event_id="event:goal:z-missing-expiry:opened",
    )
    before = ledger.project()
    target = clock(tick_id="partial-expiry")
    tick = clock_event(target)
    pending_clock = append_clock_transition(
        before.clock_transition_history,
        event=tick,
        current_logical_time=before.logical_time,
        computed_world_revision=before.world_revision + 1,
    )[-1]
    expiries = build_due_goal_expiry_events(
        world_id=WORLD_ID,
        goals=before.goals,
        clock=target,
        clock_transition=pending_clock,
    )
    ledger.commit(
        [tick, expiries[0]],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )

    outcome = await runtime.advance(target)

    recovered = ledger.project()
    assert tuple(goal.values.status for goal in recovered.goals) == (
        "expired",
        "expired",
    )
    assert tuple(item.operation for item in recovered.goal_transitions).count("expire") == 2
    cause = recovered.goal_transitions[-1].cause_authority
    assert cause.clock_event_ref == tick.event_id
    assert cause.clock_world_revision == pending_clock.computed_world_revision
    assert cause.clock_payload_hash == tick.payload_hash
    assert cause.logical_time_from == pending_clock.logical_time_from
    assert cause.logical_time_to == pending_clock.logical_time_to
    assert cause.policy_version == pending_clock.installed_policy_version
    assert cause.policy_digest == pending_clock.installed_policy_digest
    assert outcome.committed_world_revision == before.world_revision + 3


@pytest.mark.asyncio
async def test_concurrent_missing_expiry_recovery_joins_the_winning_commit() -> None:
    ledger = AmbiguousRecoveryLedger(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:recovery-race",
        event_id="event:goal:recovery-race:opened",
    )
    before = ledger.project()
    target = clock(tick_id="recovery-race")
    tick = clock_event(target)
    clock_commit = ledger.commit(
        [tick],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    ledger.raise_after_recovery = True

    outcome = await runtime.advance(target)

    after = ledger.project()
    assert after.goals[0].values.status == "expired"
    assert after.world_revision == clock_commit.world_revision + 1
    assert outcome.committed_world_revision == after.world_revision
    assert outcome.ledger_sequence == after.ledger_sequence


@pytest.mark.asyncio
async def test_rejected_clock_expiry_batch_leaves_no_partial_clock() -> None:
    ledger = RejectingGoalExpiryLedger(world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:atomic",
        event_id="event:goal:atomic:opened",
    )
    before = ledger.project()
    target = clock(tick_id="atomic-rejection")

    with pytest.raises(ValueError, match="atomic batch rejection"):
        await runtime.advance(target)
    assert ledger.project() == before

    await runtime.advance(target)
    after = ledger.project()
    assert after.goals[0].values.status == "expired"
    assert after.world_revision == before.world_revision + 2


@pytest.mark.asyncio
async def test_goal_expiry_survives_sqlite_runtime_reopen(tmp_path) -> None:
    path = tmp_path / "goal-expiry-runtime.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await bootstrap_goal_clock(runtime)
    record_accept_open_goal(
        ledger,
        goal_id="goal:sqlite",
        event_id="event:goal:sqlite:opened",
    )
    await runtime.advance(clock(tick_id="sqlite-expiry"))
    expected = ledger.project()
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    rebuilt = reopened.project()
    assert rebuilt.goals == expected.goals
    assert rebuilt.goal_transitions == expected.goal_transitions
    assert rebuilt.clock_transition_history == expected.clock_transition_history
    reopened.close()
