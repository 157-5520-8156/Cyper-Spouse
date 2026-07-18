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


def test_context_changes_spontaneous_wait_and_act_hold_weight_without_hard_scenarios() -> None:
    policy = SocialInitiativePolicy(
        spontaneous_idle_seconds=1_800,
        spontaneous_expiry_seconds=43_200,
    )
    compiler = SocialInitiativeContextPolicy(policy=policy)
    receptive = SimpleNamespace(
        relationship_states=(
            SimpleNamespace(
                variables=SimpleNamespace(closeness_bp=8_000, mutuality_bp=8_000)
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
                variables=SimpleNamespace(closeness_bp=1_000, mutuality_bp=1_000)
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
        logical_time=NOW.replace(hour=2),
    )

    assert receptive_profile.not_before_seconds == 1_260
    assert guarded_profile.not_before_seconds == 4_590
    assert receptive_profile.candidate_weights["act"] == 8_000
    assert receptive_profile.candidate_weights["hold"] == 4_000
    assert guarded_profile.candidate_weights["act"] == 4_500
    assert guarded_profile.candidate_weights["hold"] == 13_500
    assert receptive_profile.reason_codes == (
        "relationship:close",
        "affect:approach",
        "activity:available",
        "daypart:day",
    )
    assert guarded_profile.reason_codes == (
        "relationship:distant",
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
        logical_time=NOW + timedelta(seconds=1_260),
        actions=(),
        expression_plan_manifests=(),
        message_observations=(
            SimpleNamespace(observation_id="message:source", world_revision=1),
        ),
        committed_world_event_refs=(),
        relationship_states=(
            (
                SimpleNamespace(
                    variables=SimpleNamespace(closeness_bp=8_000, mutuality_bp=8_000)
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
async def test_receptive_context_reaches_draw_before_plain_idle_window() -> None:
    receptive, projection, receptive_commits = _compiler_fixture(receptive=True)
    ordinary, ordinary_projection, ordinary_commits = _compiler_fixture(receptive=False)

    await receptive.next_opportunity(projection)
    await ordinary.next_opportunity(ordinary_projection)

    assert [event.event_type for event in receptive_commits] == ["RandomDrawRecorded"]
    assert ordinary_commits == []


@pytest.mark.asyncio
async def test_unchanged_context_reuses_one_act_hold_draw_across_scheduler_ticks() -> None:
    compiler, projection, committed = _compiler_fixture(receptive=True)

    first = await compiler.next_opportunity(projection)
    projection.logical_time += timedelta(seconds=15)
    second = await compiler.next_opportunity(projection)

    assert (first is None) == (second is None)
    assert [event.event_type for event in committed] == ["RandomDrawRecorded"]


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
