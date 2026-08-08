from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.expression_plan_acceptance import (
    ExpressionPlanBudgetPolicy,
)
from companion_daemon.world_v2.proactive_action import ProactiveActionRuntime
from companion_daemon.world_v2.social_initiative import (
    SocialInitiativeCompiler,
    SocialInitiativeContextPolicy,
    SocialInitiativePolicy,
)
from companion_daemon.world_v2.schemas import WorldEvent


NOW = datetime(2026, 7, 17, 14, 0, tzinfo=UTC)
COMPANION_EXPERIENCE_STIMULUS = {
    "experience": {"values": {"participant_refs": ["actor:companion"]}}
}


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
        world_id="world:social-context-test",
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
        trigger_processes=(),
        model_result_audits=(),
        world_occurrences=(),
        threads=(),
        commitments=(),
        thread_transitions=(),
        commitment_transitions=(),
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
        actor_ref="actor:companion",
        policy=SocialInitiativePolicy(
            spontaneous_idle_seconds=1_800,
            spontaneous_expiry_seconds=43_200,
        ),
    ), projection, committed


@pytest.mark.asyncio
async def test_open_situation_consideration_recovers_before_contact_cooldown() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    source = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:experience:persisted-open",
        world_id=projection.world_id,
        event_type="ExperienceCommitted",
        logical_time=NOW + timedelta(minutes=10),
        created_at=NOW + timedelta(minutes=10),
        actor="actor:companion",
        source="test",
        trace_id="trace:persisted-open",
        causation_id="cause:persisted-open",
        correlation_id="conversation:persisted-open",
        idempotency_key="experience:persisted-open",
        payload=COMPANION_EXPERIENCE_STIMULUS,
    )
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]  # noqa: SLF001
        (source, SimpleNamespace(world_revision=2))
        if event_id == source.event_id
        else None
    )
    consideration_id = "consideration:social-initiative:" + "a" * 64
    projection.committed_world_event_refs = (
        SimpleNamespace(
            event_id=source.event_id,
            event_type=source.event_type,
            logical_time=source.logical_time,
            world_revision=2,
        ),
    )
    projection.actions = (
        SimpleNamespace(
            kind="proactive_message",
            state="delivered",
            logical_time=projection.logical_time - timedelta(minutes=1),
        ),
    )
    projection.trigger_processes = (
        SimpleNamespace(
            trigger_id="trigger:proactive:persisted-open",
            trigger_ref="proactive-consideration:" + consideration_id,
            process_kind="proactive_action_deliberation",
            source_evidence_ref=source.event_id,
            state="open",
            runtime_outcome_ref=None,
        ),
    )

    opportunity = await compiler.next_opportunity(projection)

    assert opportunity is not None
    assert opportunity.consideration_id == consideration_id
    assert opportunity.source_kind == "situation_change"
    assert opportunity.source_event_ref == source.event_id
    assert opportunity.cadence_reason_codes == ("recovery:persisted_process",)


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
    assert (
        await compiler.next_opportunity(
            projection,
            excluded_consideration_ids=frozenset({opportunity.consideration_id}),
        )
        is None
    )
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
async def test_semantic_situation_change_gets_one_recorded_jittered_consideration() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    occurred_at = NOW + timedelta(minutes=30)
    stimulus = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:experience:shared",
        world_id="world:social-context-test",
        event_type="ExperienceCommitted",
        logical_time=occurred_at,
        created_at=occurred_at,
        actor="actor:companion",
        source="test",
        trace_id="trace:situation-change",
        causation_id="cause:situation-change",
        correlation_id="conversation:situation-change",
        idempotency_key="experience:shared",
        payload=COMPANION_EXPERIENCE_STIMULUS,
    )
    original_lookup = compiler._ledger.lookup_event_commit  # noqa: SLF001
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]
        (stimulus, SimpleNamespace(world_revision=2))
        if event_id == stimulus.event_id
        else original_lookup(event_id)
    )
    projection.committed_world_event_refs = (
        SimpleNamespace(
            event_id=stimulus.event_id,
            event_type=stimulus.event_type,
            logical_time=stimulus.logical_time,
            world_revision=2,
        ),
    )
    projection.logical_time = occurred_at + timedelta(minutes=3)
    draws: list[dict[str, object]] = []
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **kwargs: (
            draws.append(kwargs)
            or SimpleNamespace(
                selected_candidate_ref="delay:120",
                draw_id="draw:situation-change",
            )
        )
    )

    opportunity = await compiler.next_opportunity(projection)

    assert opportunity is not None
    assert opportunity.source_kind == "situation_change"
    assert opportunity.source_event_ref == stimulus.event_id
    assert opportunity.stimulus_event_refs == (stimulus.event_id,)
    assert draws[0]["candidate_refs"] == ("delay:120", "delay:900", "delay:2700")


@pytest.mark.parametrize("visibility", ["private", "public", "shareable"])
@pytest.mark.asyncio
async def test_npc_only_occurrence_never_becomes_protagonist_stimulus_or_recovery(
    visibility: str,
) -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    occurred_at = NOW + timedelta(minutes=10)
    stimulus = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:occurrence:npc-only:{visibility}",
        world_id=projection.world_id,
        event_type="WorldOccurrenceSettled",
        logical_time=occurred_at,
        created_at=occurred_at,
        actor="worker:world-v2:npc-ecology",
        source="test",
        trace_id="trace:npc-only-stimulus",
        causation_id="cause:npc-only-stimulus",
        correlation_id="conversation:npc-only-stimulus",
        idempotency_key=f"occurrence:npc-only:{visibility}",
        payload={"occurrence_id": f"occurrence:npc-only:{visibility}"},
    )
    stimulus_ref = SimpleNamespace(
        event_id=stimulus.event_id,
        event_type=stimulus.event_type,
        logical_time=stimulus.logical_time,
        world_revision=2,
    )
    projection.committed_world_event_refs = (stimulus_ref,)
    projection.world_occurrences = (
        SimpleNamespace(
            occurrence_id=f"occurrence:npc-only:{visibility}",
            participant_refs=("npc:roommate",),
            visibility=visibility,
            status="settled",
            settlement_event_ref=stimulus.event_id,
        ),
    )
    projection.logical_time = occurred_at + timedelta(minutes=3)
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]  # noqa: SLF001
        (stimulus, SimpleNamespace(world_revision=2))
        if event_id == stimulus.event_id
        else None
    )
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **_kwargs: SimpleNamespace(
            selected_candidate_ref="delay:120",
            draw_id="draw:npc-only-stimulus",
        )
    )

    assert await compiler.next_opportunity(projection) is None

    # Historical open processes remain in replay, but recovery cannot turn an
    # NPC-private source into protagonist capability authority.
    projection.trigger_processes = (
        SimpleNamespace(
            trigger_id="trigger:proactive:npc-only",
            trigger_ref=(
                "proactive-consideration:consideration:social-initiative:"
                + "a" * 64
            ),
            process_kind="proactive_action_deliberation",
            source_evidence_ref=stimulus.event_id,
            state="open",
            runtime_outcome_ref=None,
        ),
    )

    assert await compiler.next_opportunity(projection) is None


@pytest.mark.asyncio
async def test_protagonist_participation_authorizes_occurrence_stimulus() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    occurred_at = NOW + timedelta(minutes=10)
    stimulus = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:occurrence:shared-with-protagonist",
        world_id=projection.world_id,
        event_type="WorldOccurrenceSettled",
        logical_time=occurred_at,
        created_at=occurred_at,
        actor="worker:world-v2:life-aftermath",
        source="test",
        trace_id="trace:shared-stimulus",
        causation_id="cause:shared-stimulus",
        correlation_id="conversation:shared-stimulus",
        idempotency_key="occurrence:shared-with-protagonist",
        payload={"occurrence_id": "occurrence:shared-with-protagonist"},
    )
    projection.committed_world_event_refs = (
        SimpleNamespace(
            event_id=stimulus.event_id,
            event_type=stimulus.event_type,
            logical_time=stimulus.logical_time,
            world_revision=2,
        ),
    )
    projection.world_occurrences = (
        SimpleNamespace(
            occurrence_id="occurrence:shared-with-protagonist",
            participant_refs=("actor:companion", "npc:roommate"),
            visibility="private",
            status="settled",
            settlement_event_ref=stimulus.event_id,
        ),
    )
    projection.logical_time = occurred_at + timedelta(minutes=3)
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]  # noqa: SLF001
        (stimulus, SimpleNamespace(world_revision=2))
        if event_id == stimulus.event_id
        else None
    )
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **_kwargs: SimpleNamespace(
            selected_candidate_ref="delay:120",
            draw_id="draw:shared-stimulus",
        )
    )

    opportunity = await compiler.next_opportunity(projection)

    assert opportunity is not None
    assert opportunity.source_kind == "situation_change"
    assert opportunity.stimulus_event_refs == (stimulus.event_id,)


@pytest.mark.asyncio
async def test_explicit_perception_is_observable_only_to_its_bound_actor() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    occurred_at = NOW + timedelta(minutes=10)
    perceptions = tuple(
        WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:perception:{actor_ref}",
            world_id=projection.world_id,
            event_type="ExternalPerceptionRecorded",
            logical_time=occurred_at + timedelta(seconds=offset),
            created_at=occurred_at + timedelta(seconds=offset),
            actor="worker:world-v2:external-perception",
            source="test",
            trace_id="trace:perception-stimulus",
            causation_id="cause:perception-stimulus",
            correlation_id="conversation:perception-stimulus",
            idempotency_key=f"perception:{actor_ref}",
            payload={"actor_ref": actor_ref},
        )
        for offset, actor_ref in enumerate(("npc:roommate", "actor:companion"))
    )
    projection.committed_world_event_refs = tuple(
        SimpleNamespace(
            event_id=item.event_id,
            event_type=item.event_type,
            logical_time=item.logical_time,
            world_revision=index,
        )
        for index, item in enumerate(perceptions, start=2)
    )
    projection.logical_time = occurred_at + timedelta(minutes=3)
    stored = {item.event_id: item for item in perceptions}
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]  # noqa: SLF001
        (stored[event_id], SimpleNamespace(world_revision=2))
        if event_id in stored
        else None
    )
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **_kwargs: SimpleNamespace(
            selected_candidate_ref="delay:120",
            draw_id="draw:perception-stimulus",
        )
    )

    opportunity = await compiler.next_opportunity(projection)

    assert opportunity is not None
    assert opportunity.source_event_ref == perceptions[1].event_id
    assert opportunity.stimulus_event_refs == (perceptions[1].event_id,)


@pytest.mark.asyncio
async def test_failed_situation_consideration_retains_its_stimulus_on_retry() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    occurred_at = NOW + timedelta(minutes=30)
    stimulus = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:experience:retry",
        world_id="world:social-context-test",
        event_type="ExperienceCommitted",
        logical_time=occurred_at,
        created_at=occurred_at,
        actor="actor:companion",
        source="test",
        trace_id="trace:situation-retry",
        causation_id="cause:situation-retry",
        correlation_id="conversation:situation-retry",
        idempotency_key="experience:retry",
        payload=COMPANION_EXPERIENCE_STIMULUS,
    )
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]  # noqa: SLF001
        (stimulus, SimpleNamespace(world_revision=2))
        if event_id == stimulus.event_id
        else None
    )
    stimulus_ref = SimpleNamespace(
        event_id=stimulus.event_id,
        event_type=stimulus.event_type,
        logical_time=stimulus.logical_time,
        world_revision=2,
    )
    projection.committed_world_event_refs = (stimulus_ref,)
    projection.logical_time = occurred_at + timedelta(minutes=3)
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **_kwargs: SimpleNamespace(
            selected_candidate_ref="delay:120",
            draw_id="draw:situation-retry",
        )
    )
    first = await compiler.next_opportunity(projection)
    assert first is not None

    failure_ref = SimpleNamespace(
        event_id="event:model-result:situation-retry",
        event_type="ModelResultRecorded",
        logical_time=occurred_at + timedelta(minutes=4),
        world_revision=4,
    )
    projection.committed_world_event_refs = (stimulus_ref, failure_ref)
    projection.model_result_audits = (
        SimpleNamespace(
            model_result_ref="model-result:situation-retry",
            proposal_hash=None,
            event_ref=failure_ref.event_id,
            evaluated_world_revision=2,
        ),
    )
    projection.trigger_processes = (
        SimpleNamespace(
            process_kind="proactive_action_deliberation",
            state="terminal",
            trigger_ref="proactive-consideration:" + first.consideration_id,
            runtime_outcome_ref=(
                "proactive:deliberation-failed:model-result:situation-retry"
            ),
            source_evidence_ref=stimulus.event_id,
            claim_lease=SimpleNamespace(acquired_at=failure_ref.logical_time),
        ),
        SimpleNamespace(
            process_kind="proactive_action_deliberation",
            state="terminal",
            trigger_ref="proactive-consideration:later-success",
            runtime_outcome_ref="proactive:model-silent:model-result:later",
            source_evidence_ref=stimulus.event_id,
            claim_lease=None,
        ),
    )
    late_stimulus_ref = SimpleNamespace(
        event_id="event:experience:late-in-window",
        event_type="ExperienceCommitted",
        logical_time=occurred_at + timedelta(minutes=5),
        world_revision=5,
    )
    old_same_time_stimulus_ref = SimpleNamespace(
        event_id="event:experience:before-message",
        event_type="ExperienceCommitted",
        logical_time=stimulus.logical_time,
        world_revision=1,
    )
    projection.committed_world_event_refs = (
        old_same_time_stimulus_ref,
        stimulus_ref,
        failure_ref,
        late_stimulus_ref,
    )
    projection.logical_time = occurred_at + timedelta(minutes=15)

    retry = await compiler._failed_consideration_retry(projection)  # noqa: SLF001

    assert retry is not None
    assert retry.source_kind == "situation_change"
    assert retry.stimulus_event_refs == (stimulus.event_id,)
    assert retry.cadence_reason_codes == ("technical_failure:retry",)

    projection.message_observations = (
        *projection.message_observations,
        SimpleNamespace(observation_id="message:during-call", world_revision=3),
    )
    assert await compiler._failed_consideration_retry(projection) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_technical_retry_precedes_cooldown_from_another_successful_contact() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    occurred_at = NOW + timedelta(minutes=30)
    stimulus = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:experience:retry-before-contact-cooldown",
        world_id="world:social-context-test",
        event_type="ExperienceCommitted",
        logical_time=occurred_at,
        created_at=occurred_at,
        actor="actor:companion",
        source="test",
        trace_id="trace:retry-before-contact-cooldown",
        causation_id="cause:retry-before-contact-cooldown",
        correlation_id="conversation:retry-before-contact-cooldown",
        idempotency_key="experience:retry-before-contact-cooldown",
        payload=COMPANION_EXPERIENCE_STIMULUS,
    )
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]  # noqa: SLF001
        (stimulus, SimpleNamespace(world_revision=2))
        if event_id == stimulus.event_id
        else None
    )
    stimulus_ref = SimpleNamespace(
        event_id=stimulus.event_id,
        event_type=stimulus.event_type,
        logical_time=stimulus.logical_time,
        world_revision=2,
    )
    projection.committed_world_event_refs = (stimulus_ref,)
    projection.logical_time = occurred_at + timedelta(minutes=3)
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **_kwargs: SimpleNamespace(
            selected_candidate_ref="delay:120",
            draw_id="draw:retry-before-contact-cooldown",
        )
    )
    failed_opportunity = await compiler.next_opportunity(projection)
    assert failed_opportunity is not None

    failed_at = occurred_at + timedelta(minutes=4)
    failure_ref = SimpleNamespace(
        event_id="event:model-result:retry-before-contact-cooldown",
        event_type="ModelResultRecorded",
        logical_time=failed_at,
        world_revision=4,
    )
    projection.committed_world_event_refs = (stimulus_ref, failure_ref)
    projection.model_result_audits = (
        SimpleNamespace(
            model_result_ref="model-result:retry-before-contact-cooldown",
            proposal_hash=None,
            event_ref=failure_ref.event_id,
            evaluated_world_revision=2,
        ),
    )
    projection.trigger_processes = (
        SimpleNamespace(
            process_kind="proactive_action_deliberation",
            state="terminal",
            trigger_ref=(
                "proactive-consideration:" + failed_opportunity.consideration_id
            ),
            runtime_outcome_ref=(
                "proactive:deliberation-failed:"
                "model-result:retry-before-contact-cooldown"
            ),
            source_evidence_ref=stimulus.event_id,
            claim_lease=SimpleNamespace(acquired_at=failed_at),
        ),
    )
    # Another consideration successfully contacted the user two minutes before
    # this failure's ten-minute retry deadline. That ordinary contact starts a
    # cadence cooldown, but it does not semantically settle or supersede the
    # failed consideration.
    projection.actions = (
        SimpleNamespace(
            kind="proactive_message",
            state="delivered",
            logical_time=failed_at + timedelta(minutes=8),
        ),
    )
    projection.logical_time = failed_at + timedelta(minutes=10)

    retry = await compiler.next_opportunity(projection)

    assert retry is not None
    assert retry.consideration_id == failed_opportunity.consideration_id
    assert retry.cadence_reason_codes == ("technical_failure:retry",)

    # Only a newer user Observation invalidates the old pinned social context.
    projection.message_observations = (
        *projection.message_observations,
        SimpleNamespace(observation_id="message:new-context", world_revision=5),
    )
    assert await compiler.next_opportunity(projection) is None


@pytest.mark.asyncio
async def test_not_due_retry_does_not_starve_an_independent_due_situation() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    first_at = NOW + timedelta(minutes=10)
    second_at = first_at + timedelta(minutes=11)
    first_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:experience:failed-situation",
        world_id=projection.world_id,
        event_type="ExperienceCommitted",
        logical_time=first_at,
        created_at=first_at,
        actor="actor:companion",
        source="test",
        trace_id="trace:retry-independent-situation",
        causation_id="cause:retry-independent-situation",
        correlation_id="conversation:retry-independent-situation",
        idempotency_key="experience:failed-situation",
        payload=COMPANION_EXPERIENCE_STIMULUS,
    )
    second_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:experience:independent-situation",
        world_id=projection.world_id,
        event_type="ExperienceCommitted",
        logical_time=second_at,
        created_at=second_at,
        actor="actor:companion",
        source="test",
        trace_id="trace:retry-independent-situation",
        causation_id=first_event.event_id,
        correlation_id="conversation:retry-independent-situation",
        idempotency_key="experience:independent-situation",
        payload=COMPANION_EXPERIENCE_STIMULUS,
    )
    stored = {item.event_id: item for item in (first_event, second_event)}
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]  # noqa: SLF001
        (stored[event_id], SimpleNamespace(world_revision=2))
        if event_id in stored
        else None
    )
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **_kwargs: SimpleNamespace(
            selected_candidate_ref="delay:120",
            draw_id="draw:retry-independent-situation",
        )
    )
    first_ref = SimpleNamespace(
        event_id=first_event.event_id,
        event_type=first_event.event_type,
        logical_time=first_event.logical_time,
        world_revision=2,
    )
    projection.committed_world_event_refs = (first_ref,)
    projection.logical_time = first_at + timedelta(minutes=3)
    first = await compiler.next_opportunity(projection)
    assert first is not None

    failed_at = first_at + timedelta(minutes=5)
    failure_ref = SimpleNamespace(
        event_id="event:model-result:failed-situation",
        event_type="ModelResultRecorded",
        logical_time=failed_at,
        world_revision=4,
    )
    second_ref = SimpleNamespace(
        event_id=second_event.event_id,
        event_type=second_event.event_type,
        logical_time=second_event.logical_time,
        world_revision=5,
    )
    trigger_ref = "proactive-consideration:" + first.consideration_id
    failed_trigger_id = ProactiveActionRuntime._trigger_id_for_world(  # noqa: SLF001
        world_id=projection.world_id,
        consideration_id=first.consideration_id,
        retry_ordinal=0,
    )
    projection.trigger_processes = (
        SimpleNamespace(
            trigger_id=failed_trigger_id,
            process_kind=ProactiveActionRuntime.PROCESS_KIND,
            state="terminal",
            trigger_ref=trigger_ref,
            runtime_outcome_ref="proactive:deliberation-failed:model-result:failed-situation",
            source_evidence_ref=first_event.event_id,
            claim_lease=SimpleNamespace(acquired_at=failed_at),
        ),
    )
    projection.completed_trigger_ids = (failed_trigger_id,)
    projection.model_result_audits = (
        SimpleNamespace(
            attempt_id=ProactiveActionRuntime._model_attempt_id(  # noqa: SLF001
                consideration_id=first.consideration_id,
                retry_ordinal=0,
            ),
            model_result_ref="model-result:failed-situation",
            proposal_hash=None,
            parent_model_call_id=None,
            audit_json='{"status":"recovery_failed","failure_code":"quick_invalid_output"}',
            event_ref=failure_ref.event_id,
            evaluated_world_revision=2,
        ),
    )
    projection.committed_world_event_refs = (first_ref, failure_ref, second_ref)
    projection.logical_time = second_at + timedelta(minutes=2)

    runtime = ProactiveActionRuntime(
        ledger=compiler._ledger,  # noqa: SLF001
        turn=SimpleNamespace(),
        batch_issuer=AcceptedLedgerBatchIssuer(),
        policy=ExpressionPlanBudgetPolicy(
            account_id="account:proactive",
            amount_limit_per_action=10,
            actor="actor:companion",
            allowed_targets=("user:primary",),
            recovery_policy="effect_once",
            category="proactive",
        ),
        owner_id="worker:test-social-alternate",
        social_initiative=compiler,
    )
    opened = []

    async def capture_open(**kwargs):  # type: ignore[no-untyped-def]
        opened.append(kwargs["opportunity"])

    runtime._open = capture_open  # type: ignore[method-assign]  # noqa: SLF001

    result = await runtime.drain_one()

    assert result.status == "opened"
    assert len(opened) == 1
    assert opened[0].source_event_ref == second_event.event_id
    assert opened[0].consideration_id != first.consideration_id


@pytest.mark.asyncio
async def test_not_due_situation_retry_does_not_occupy_the_ambient_cadence() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    failed_situation = SimpleNamespace(
        consideration_id="consideration:failed-situation",
        source_kind="situation_change",
    )
    ambient = SimpleNamespace(
        consideration_id="consideration:ambient-next-epoch",
        source_kind="ambient_presence",
    )

    async def no_pending(  # type: ignore[no-untyped-def]
        _projection, *, excluded_consideration_ids
    ):
        del _projection, excluded_consideration_ids
        return None

    async def failed_retry(_projection):  # type: ignore[no-untyped-def]
        del _projection
        return failed_situation

    async def no_situation(  # type: ignore[no-untyped-def]
        _projection, _logical_time, *, excluded_consideration_ids
    ):
        del _projection, _logical_time, excluded_consideration_ids
        return None

    async def ambient_opportunity(_projection, _logical_time):  # type: ignore[no-untyped-def]
        del _projection, _logical_time
        return ambient

    compiler._pending_consideration = no_pending  # type: ignore[method-assign]  # noqa: SLF001
    compiler._failed_consideration_retry = failed_retry  # type: ignore[method-assign]  # noqa: SLF001
    compiler._situation_change = no_situation  # type: ignore[method-assign]  # noqa: SLF001
    compiler._spontaneous_contact = ambient_opportunity  # type: ignore[method-assign]  # noqa: SLF001

    opportunity = await compiler.next_opportunity(
        projection,
        excluded_consideration_ids=frozenset({failed_situation.consideration_id}),
    )

    assert opportunity is ambient


@pytest.mark.asyncio
async def test_successful_retry_terminally_settles_the_failed_consideration() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    occurred_at = NOW + timedelta(minutes=30)
    stimulus = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:experience:settled-retry",
        world_id="world:social-context-test",
        event_type="ExperienceCommitted",
        logical_time=occurred_at,
        created_at=occurred_at,
        actor="actor:companion",
        source="test",
        trace_id="trace:settled-retry",
        causation_id="cause:settled-retry",
        correlation_id="conversation:settled-retry",
        idempotency_key="experience:settled-retry",
        payload=COMPANION_EXPERIENCE_STIMULUS,
    )
    stimulus_ref = SimpleNamespace(
        event_id=stimulus.event_id,
        event_type=stimulus.event_type,
        logical_time=stimulus.logical_time,
        world_revision=2,
    )
    failure_ref = SimpleNamespace(
        event_id="event:model-result:settled-retry",
        event_type="ModelResultRecorded",
        logical_time=occurred_at + timedelta(minutes=4),
        world_revision=4,
    )
    consideration_ref = "proactive-consideration:consideration:settled-retry"
    projection.logical_time = occurred_at + timedelta(minutes=20)
    projection.committed_world_event_refs = (stimulus_ref, failure_ref)
    projection.model_result_audits = (
        SimpleNamespace(
            model_result_ref="model-result:settled-retry",
            proposal_hash=None,
            event_ref=failure_ref.event_id,
            evaluated_world_revision=2,
        ),
    )
    projection.trigger_processes = (
        SimpleNamespace(
            process_kind="proactive_action_deliberation",
            state="terminal",
            trigger_ref=consideration_ref,
            runtime_outcome_ref=(
                "proactive:deliberation-failed:model-result:settled-retry"
            ),
            source_evidence_ref=stimulus.event_id,
            claim_lease=SimpleNamespace(acquired_at=failure_ref.logical_time),
        ),
        SimpleNamespace(
            process_kind="proactive_action_deliberation",
            state="terminal",
            trigger_ref=consideration_ref,
            runtime_outcome_ref="proactive:silent",
            source_evidence_ref=stimulus.event_id,
            claim_lease=SimpleNamespace(
                acquired_at=failure_ref.logical_time + timedelta(minutes=10)
            ),
        ),
    )
    compiler._ledger.lookup_event_commit = lambda event_id: (  # type: ignore[attr-defined]  # noqa: SLF001
        (stimulus, SimpleNamespace(world_revision=2))
        if event_id == stimulus.event_id
        else None
    )

    retry = await compiler._failed_consideration_retry(projection)  # noqa: SLF001

    assert retry is None


@pytest.mark.asyncio
async def test_each_situation_window_survives_a_newer_window_until_considered() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    first_at = NOW + timedelta(minutes=10)
    second_at = first_at + timedelta(minutes=11)
    events = tuple(
        WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id="world:social-context-test",
            event_type="ExperienceCommitted",
            logical_time=at,
            created_at=at,
            actor="actor:companion",
            source="test",
            trace_id="trace:situation-windows",
            causation_id="cause:situation-windows",
            correlation_id="conversation:situation-windows",
            idempotency_key=event_id,
            payload=COMPANION_EXPERIENCE_STIMULUS,
        )
        for event_id, at in (
            ("event:experience:first-window", first_at),
            ("event:experience:second-window", second_at),
        )
    )
    projection.committed_world_event_refs = tuple(
        SimpleNamespace(
            event_id=item.event_id,
            event_type=item.event_type,
            logical_time=item.logical_time,
            world_revision=index,
        )
        for index, item in enumerate(events, start=2)
    )
    projection.logical_time = first_at + timedelta(minutes=50)
    compiler._ledger.lookup_event_commit = lambda event_id: next(  # type: ignore[attr-defined]  # noqa: SLF001
        (
            (item, SimpleNamespace(world_revision=index))
            for index, item in enumerate(events, start=2)
            if item.event_id == event_id
        ),
        None,
    )
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **kwargs: SimpleNamespace(
            selected_candidate_ref=(
                "delay:2700" if kwargs["seed_instant"] == first_at else "delay:120"
            ),
            draw_id="draw:situation-window",
        )
    )
    newer = await compiler.next_opportunity(projection)
    assert newer is not None
    assert newer.source_event_ref == events[1].event_id

    older = await compiler.next_opportunity(
        projection,
        excluded_consideration_ids=frozenset({newer.consideration_id}),
    )
    assert older is not None
    assert older.source_event_ref == events[0].event_id

    projection.trigger_processes = (
        SimpleNamespace(
            process_kind="proactive_action_deliberation",
            trigger_ref="proactive-consideration:" + newer.consideration_id,
            state="terminal",
            runtime_outcome_ref="proactive:silent",
        ),
    )
    terminal_fallback = await compiler.next_opportunity(projection)
    assert terminal_fallback is not None
    assert terminal_fallback.source_event_ref == events[0].event_id


@pytest.mark.asyncio
async def test_new_stimulus_in_a_settled_window_reuses_draw_but_gets_a_new_epoch() -> None:
    compiler, projection, _committed = _compiler_fixture(receptive=True)
    anchor_at = NOW + timedelta(minutes=10)
    events = tuple(
        WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id="world:social-context-test",
            event_type="ExperienceCommitted",
            logical_time=at,
            created_at=at,
            actor="actor:companion",
            source="test",
            trace_id="trace:same-window",
            causation_id="cause:same-window",
            correlation_id="conversation:same-window",
            idempotency_key=event_id,
            payload=COMPANION_EXPERIENCE_STIMULUS,
        )
        for event_id, at in (
            ("event:experience:window-anchor", anchor_at),
            ("event:experience:window-append", anchor_at + timedelta(minutes=5)),
        )
    )
    projection.committed_world_event_refs = (
        SimpleNamespace(
            event_id=events[0].event_id,
            event_type=events[0].event_type,
            logical_time=events[0].logical_time,
            world_revision=2,
        ),
    )
    projection.logical_time = anchor_at + timedelta(minutes=3)
    compiler._ledger.lookup_event_commit = lambda event_id: next(  # type: ignore[attr-defined]  # noqa: SLF001
        (
            (item, SimpleNamespace(world_revision=index))
            for index, item in enumerate(events, start=2)
            if item.event_id == event_id
        ),
        None,
    )
    draw_calls: list[str] = []
    compiler._random = SimpleNamespace(  # noqa: SLF001
        draw=lambda **kwargs: (
            draw_calls.append(kwargs["attempt_id"])
            or SimpleNamespace(
                selected_candidate_ref="delay:120",
                draw_id="draw:same-window",
            )
        )
    )

    first = await compiler.next_opportunity(projection)
    assert first is not None
    projection.trigger_processes = (
        SimpleNamespace(
            process_kind="proactive_action_deliberation",
            trigger_ref="proactive-consideration:" + first.consideration_id,
            state="terminal",
            runtime_outcome_ref="proactive:silent",
        ),
    )
    projection.committed_world_event_refs = tuple(
        SimpleNamespace(
            event_id=item.event_id,
            event_type=item.event_type,
            logical_time=item.logical_time,
            world_revision=index,
        )
        for index, item in enumerate(events, start=2)
    )
    projection.logical_time = anchor_at + timedelta(minutes=6)
    appended = await compiler.next_opportunity(projection)

    assert appended is not None
    assert appended.consideration_id != first.consideration_id
    assert appended.stimulus_event_refs == tuple(item.event_id for item in events)
    assert len(set(draw_calls)) == 1


@pytest.mark.asyncio
async def test_response_expectation_never_opens_a_standalone_proactive_opportunity() -> None:
    """Expectations inform cognition; they are not an authority to chase a reply."""

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
        actor_ref="actor:companion",
        policy=SocialInitiativePolicy(
            spontaneous_idle_seconds=1_800,
            spontaneous_expiry_seconds=43_200,
        ),
    )

    assert await compiler.next_opportunity(projection) is None
