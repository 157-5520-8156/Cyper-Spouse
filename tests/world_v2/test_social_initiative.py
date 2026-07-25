from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.social_initiative import (
    SocialInitiativeCompiler,
    SocialInitiativeContextPolicy,
    SocialInitiativePolicy,
)
from companion_daemon.world_v2.schemas import WorldEvent


NOW = datetime(2026, 7, 17, 14, 0, tzinfo=UTC)


def test_context_changes_relationship_aware_consideration_band_without_deciding_speech() -> None:
    policy = SocialInitiativePolicy(
        spontaneous_idle_seconds=1_800,
        spontaneous_expiry_seconds=43_200,
    )
    compiler = SocialInitiativeContextPolicy(policy=policy)
    receptive = SimpleNamespace(
        relationship_states=(
            SimpleNamespace(
                stage="close_friend",
                variables=SimpleNamespace(
                    trust_bp=8_000,
                    closeness_bp=8_000,
                    respect_bp=8_000,
                    reliability_bp=8_000,
                    mutuality_bp=8_000,
                    repair_confidence_bp=8_000,
                ),
            ),
        ),
        affect_episodes=(
            SimpleNamespace(
                status="active",
                components=(SimpleNamespace(dimension="warmth", intensity_bp=8_000),),
            ),
        ),
        plans=(),
    )
    guarded = SimpleNamespace(
        relationship_states=(
            SimpleNamespace(
                stage="acquaintance",
                variables=SimpleNamespace(
                    trust_bp=1_000,
                    closeness_bp=1_000,
                    respect_bp=1_000,
                    reliability_bp=1_000,
                    mutuality_bp=1_000,
                    repair_confidence_bp=1_000,
                ),
            ),
        ),
        affect_episodes=(
            SimpleNamespace(
                status="active",
                components=(SimpleNamespace(dimension="anger", intensity_bp=8_000),),
            ),
        ),
        plans=(SimpleNamespace(status="active"),),
    )

    receptive_profile = compiler.compile(projection=receptive, logical_time=NOW)
    guarded_profile = compiler.compile(
        projection=guarded,
        logical_time=NOW.replace(hour=18),
    )

    assert receptive_profile.consideration_band_seconds == (3_600, 7_200)
    assert guarded_profile.consideration_band_seconds == (10_800, 21_600)
    assert receptive_profile.delay_candidates_seconds == (3_600, 5_400, 7_200)
    assert guarded_profile.delay_candidates_seconds == (10_800, 16_200, 21_600)
    assert receptive_profile.reason_codes == (
        "relationship:close_friend",
        "affect:approach",
        "activity:available",
        "daypart:day",
    )
    assert guarded_profile.reason_codes == (
        "relationship:acquaintance",
        "affect:guarded",
        "activity:engaged",
        "daypart:overnight",
    )


def _compiler_fixture(*, receptive: bool):
    source = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:message:source",
        world_id="world:social-context-test",
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:social-context",
        causation_id="cause:social-context",
        correlation_id="conversation:social-context",
        idempotency_key="observation:message:source",
        payload={"observation_id": "message:source", "text": "source"},
    )
    stored = {source.event_id: source}
    committed = []
    projection = SimpleNamespace(
        world_revision=1,
        deliberation_revision=0,
        ledger_sequence=1,
        logical_time=NOW + timedelta(minutes=90),
        actions=(),
        expression_plan_manifests=(),
        message_observations=(
            SimpleNamespace(observation_id="message:source", world_revision=1),
        ),
        committed_world_event_refs=(),
        relationship_states=(
            (
                SimpleNamespace(
                    stage="close_friend",
                    variables=SimpleNamespace(
                        trust_bp=8_000,
                        closeness_bp=8_000,
                        respect_bp=8_000,
                        reliability_bp=8_000,
                        mutuality_bp=8_000,
                        repair_confidence_bp=8_000,
                    ),
                ),
            )
            if receptive
            else ()
        ),
        affect_episodes=(
            (
                SimpleNamespace(
                    status="active",
                    components=(
                        SimpleNamespace(dimension="warmth", intensity_bp=8_000),
                    ),
                ),
            )
            if receptive
            else ()
        ),
        plans=(),
    )

    def commit_at_cursor(events, *, expected_cursor, commit_id):  # type: ignore[no-untyped-def]
        del expected_cursor, commit_id
        committed.extend(events)
        stored.update({event.event_id: event for event in events})

    ledger = SimpleNamespace(
        world_id="world:social-context-test",
        blocks_event_loop=False,
        project=lambda: projection,
        lookup_event_commit=lambda event_id: (
            (stored[event_id], SimpleNamespace(world_revision=1))
            if event_id in stored
            else None
        ),
        commit_at_cursor=commit_at_cursor,
    )
    return SocialInitiativeCompiler(
        ledger=ledger,
        policy=SocialInitiativePolicy(
            spontaneous_idle_seconds=1_800,
            spontaneous_expiry_seconds=43_200,
        ),
    ), projection, committed


@pytest.mark.asyncio
async def test_consideration_draw_selects_only_a_delay_and_never_act_or_hold() -> None:
    receptive, projection, receptive_commits = _compiler_fixture(receptive=True)
    draws: list[dict[str, object]] = []

    def draw(**kwargs):  # type: ignore[no-untyped-def]
        draws.append(kwargs)
        return SimpleNamespace(
            selected_candidate_ref="delay:5400",
            draw_id="draw:test-delay",
        )

    receptive._random = SimpleNamespace(draw=draw)  # noqa: SLF001
    opportunity = await receptive.next_opportunity(projection)

    assert opportunity is not None
    assert draws[0]["candidate_refs"] == ("delay:3600", "delay:5400", "delay:7200")
    assert "act" not in draws[0]["candidate_refs"]
    assert "hold" not in draws[0]["candidate_refs"]
    del receptive_commits


@pytest.mark.asyncio
async def test_unchanged_context_reuses_one_delay_draw_across_scheduler_ticks() -> None:
    compiler, projection, committed = _compiler_fixture(receptive=True)

    first = await compiler.next_opportunity(projection)
    projection.logical_time += timedelta(seconds=15)
    second = await compiler.next_opportunity(projection)

    assert (first is None) == (second is None)
    assert [event.event_type for event in committed] == ["RandomDrawRecorded"]


@pytest.mark.asyncio
async def test_each_due_epoch_reaches_the_model_owned_opportunity() -> None:
    """Cadence may decide when to consider, never whether the character may speak."""

    compiler, projection, committed = _compiler_fixture(receptive=True)
    draws: list[dict[str, object]] = []

    def draw(**kwargs):  # type: ignore[no-untyped-def]
        draws.append(kwargs)
        return SimpleNamespace(
            selected_candidate_ref="delay:3600", draw_id="draw:test-delay"
        )

    compiler._random = SimpleNamespace(draw=draw)  # noqa: SLF001 - deterministic seam

    opportunity = await compiler.next_opportunity(projection)

    assert opportunity is not None
    assert opportunity.source_kind == "spontaneous_contact"
    assert opportunity.consideration_epoch == 0
    assert draws and draws[0]["candidate_refs"] == (
        "delay:3600",
        "delay:5400",
        "delay:7200",
    )
    assert str(draws[0]["attempt_id"]).startswith("social-initiative:")
    del committed


@pytest.mark.asyncio
async def test_completed_consideration_is_not_returned_again_in_the_same_epoch() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **_kwargs: SimpleNamespace(
            selected_candidate_ref="delay:3600",
            draw_id="draw:completed-epoch",
        )
    )
    first = await compiler.next_opportunity(projection)
    assert first is not None
    projection.trigger_processes = (
        SimpleNamespace(
            process_kind="proactive_action_deliberation",
            trigger_ref="proactive-consideration:" + first.consideration_id,
            source_evidence_ref=first.source_event_ref,
            state="terminal",
            runtime_outcome_ref="proactive:silent",
        ),
    )

    assert await compiler.next_opportunity(projection) is None


@pytest.mark.asyncio
async def test_expired_message_context_opens_ambient_model_consideration_from_clock() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    clock_at = NOW + timedelta(hours=13)
    clock = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:clock:ambient",
        world_id="world:social-context-test",
        event_type="ClockAdvanced",
        logical_time=clock_at,
        created_at=clock_at,
        actor="system:clock",
        source="test",
        trace_id="trace:ambient",
        causation_id="cause:ambient",
        correlation_id="conversation:ambient",
        idempotency_key="clock:ambient",
        payload={
            "logical_time_from": NOW.isoformat(),
            "logical_time_to": clock_at.isoformat(),
        },
    )
    projection.logical_time = clock_at
    projection.committed_world_event_refs = (
        SimpleNamespace(
            event_id=clock.event_id,
            event_type=clock.event_type,
            logical_time=clock.logical_time,
            world_revision=2,
        ),
    )
    original_lookup = compiler._ledger.lookup_event_commit  # noqa: SLF001
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]
        (clock, SimpleNamespace(world_revision=2))
        if event_id == clock.event_id
        else original_lookup(event_id)
    )
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **_kwargs: SimpleNamespace(
            selected_candidate_ref="delay:3600",
            draw_id="draw:ambient-delay",
        )
    )

    opportunity = await compiler.next_opportunity(projection)

    assert opportunity is not None
    assert opportunity.source_kind == "ambient_presence"
    assert opportunity.source_event_ref == clock.event_id
    assert opportunity.consideration_epoch >= 1


@pytest.mark.asyncio
async def test_unrelated_later_inbound_does_not_cancel_response_gap_opportunity() -> None:
    """A new message is not semantic proof that the earlier thought is finished."""

    source = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:expression:acceptance",
        world_id="world:response-gap-context-test",
        event_type="ExpressionPlanAccepted",
        logical_time=NOW,
        created_at=NOW,
        actor="actor:companion",
        source="test",
        trace_id="trace:response-gap",
        causation_id="cause:response-gap",
        correlation_id="conversation:response-gap",
        idempotency_key="expression:acceptance",
        payload={},
    )
    logical_time = NOW + timedelta(minutes=2)
    action = SimpleNamespace(
        action_id="action:source",
        state="delivered",
        kind="reply",
        logical_time=NOW,
    )
    manifest = SimpleNamespace(
        plan_id="plan:source",
        acceptance_event_ref=source.event_id,
        recorded_at_world_revision=1,
        response_expectation=SimpleNamespace(
            source_beat_id="beat:source",
            not_before=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            delivery_requirement="provider_accepted_or_delivered",
        ),
        beats=(SimpleNamespace(beat_id="beat:source", action=action),),
    )
    projection = SimpleNamespace(
        logical_time=logical_time,
        actions=(action,),
        expression_plan_manifests=(manifest,),
        expression_plans=(SimpleNamespace(plan_id="plan:source", state="authorized"),),
        execution_receipts=(SimpleNamespace(action_id=action.action_id, observed_state="delivered"),),
        message_observations=(
            SimpleNamespace(observation_id="message:source", world_revision=1),
            SimpleNamespace(observation_id="message:unrelated", world_revision=2),
        ),
        committed_world_event_refs=(),
        world_revision=2,
        relationship_states=(),
        affect_episodes=(),
        plans=(),
        trigger_processes=(
            SimpleNamespace(
                process_kind="proactive_action_deliberation",
                trigger_ref="proactive-consideration:failed-idle",
                source_evidence_ref="event:old-idle",
                state="terminal",
                runtime_outcome_ref=(
                    "proactive:deliberation-failed:model-result:old-idle"
                ),
            ),
        ),
    )
    ledger = SimpleNamespace(
        world_id="world:response-gap-context-test",
        blocks_event_loop=False,
        lookup_event_commit=lambda event_id: (
            (source, SimpleNamespace(world_revision=1))
            if event_id == source.event_id
            else None
        ),
    )
    compiler = SocialInitiativeCompiler(
        ledger=ledger,
        policy=SocialInitiativePolicy(
            spontaneous_idle_seconds=1_800,
            spontaneous_expiry_seconds=43_200,
        ),
    )

    opportunity = await compiler.next_opportunity(projection)

    assert opportunity is not None
    assert opportunity.source_kind == "response_gap"
    projection.message_observations += (
        SimpleNamespace(observation_id="message:new-context", world_revision=3),
    )
    reconsidered = await compiler.next_opportunity(projection)
    assert reconsidered is not None
    assert reconsidered.consideration_id != opportunity.consideration_id
