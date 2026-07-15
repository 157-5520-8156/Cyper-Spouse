from __future__ import annotations

from datetime import timedelta

import pytest

from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    ClockObservation,
    DueWindow,
    EvidenceRef,
    OutcomeObservation,
    WorldOccurrenceProjection,
)
from test_life_projection import (
    LIFE_TIME,
    WORLD_ID,
    commit,
    event,
    model_hash,
    mutation,
    seed_through_proposal,
)
from companion_daemon.world_v2.ledger import WorldLedger


def _clock(*, tick_id: str, target) -> ClockObservation:
    return ClockObservation(
        schema_version="world-v2.1",
        tick_id=tick_id,
        world_id=WORLD_ID,
        logical_time=target,
        created_at=target,
        trace_id=f"trace:{tick_id}",
        causation_id=f"scheduler:{tick_id}",
        correlation_id="correlation:occurrence-clock",
        logical_time_from=LIFE_TIME,
        logical_time_to=target,
        reason="scheduled_tick",
    )


def _commit_occurrence(
    ledger: WorldLedger,
    *,
    occurrence_id: str,
    opens_at,
    closes_at,
    precondition_refs: tuple[str, ...],
) -> None:
    plan = next(item for item in ledger.project().plans if item.plan_id == "plan-tea")
    evidence_ref = EvidenceRef(
        ref_id=plan.plan_id,
        evidence_type="active_plan",
        claim_purpose="future_plan",
        immutable_hash=model_hash(plan),
    ).model_dump(mode="json")
    occurrence = WorldOccurrenceProjection(
        occurrence_id=occurrence_id,
        entity_revision=1,
        trigger_ref=f"trigger:{occurrence_id}",
        participant_refs=("npc:lin",),
        location_ref="room:kitchen",
        time_window=DueWindow(opens_at=opens_at, closes_at=closes_at),
        precondition_refs=precondition_refs,
        candidate_outcome_refs=(f"result:{occurrence_id}",),
        visibility="private",
        status="committed",
    )
    commit(
        ledger,
        [
            event(
                f"{occurrence_id}:committed",
                "WorldOccurrenceCommitted",
                {
                    **mutation(
                        f"{occurrence_id}:committed",
                        expected_revision=0,
                        evidence_refs=[evidence_ref],
                    ),
                    "occurrence": occurrence.model_dump(mode="json"),
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_clock_activates_a_committed_occurrence_with_verifiable_preconditions() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    _commit_occurrence(
        ledger,
        occurrence_id="occurrence:clock-activate",
        opens_at=LIFE_TIME,
        closes_at=LIFE_TIME + timedelta(minutes=10),
        precondition_refs=("plan:plan-tea",),
    )

    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    clock = _clock(
        tick_id="occurrence-clock-activate",
        target=LIFE_TIME + timedelta(minutes=1),
    )
    first = await runtime.advance(clock)

    occurrence = next(
        item
        for item in ledger.project().world_occurrences
        if item.occurrence_id == "occurrence:clock-activate"
    )
    assert occurrence.status == "active"
    assert occurrence.satisfied_precondition_refs == ("plan:plan-tea",)
    activation_ref = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_type == "WorldOccurrenceActivated"
        and item.event_id != "occurrence-activated"
    )
    activated = ledger.lookup_event_commit(activation_ref.event_id)
    assert activated is not None
    assert activated[0].event_type == "WorldOccurrenceActivated"
    assert activated[0].payload()["evidence_refs"][0]["ref_id"] == (
        "event:trigger:clock:occurrence-clock-activate"
    )
    projection_after_first = ledger.project()
    assert await runtime.advance(clock) == first
    assert ledger.project() == projection_after_first


@pytest.mark.asyncio
async def test_clock_expires_an_unactivated_occurrence_after_its_window_closes() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    _commit_occurrence(
        ledger,
        occurrence_id="occurrence:clock-expire",
        opens_at=LIFE_TIME,
        closes_at=LIFE_TIME + timedelta(minutes=1),
        precondition_refs=("plan:plan-tea",),
    )

    await WorldRuntime(world_id=WORLD_ID, ledger=ledger).advance(
        _clock(
            tick_id="occurrence-clock-expire",
            target=LIFE_TIME + timedelta(minutes=2),
        )
    )

    occurrence = next(
        item
        for item in ledger.project().world_occurrences
        if item.occurrence_id == "occurrence:clock-expire"
    )
    assert occurrence.status == "expired"
    assert occurrence.terminal_reason_ref == (
        "clock-expired:event:trigger:clock:occurrence-clock-expire"
    )


@pytest.mark.asyncio
async def test_clock_does_not_activate_an_occurrence_with_an_unverifiable_precondition() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    _commit_occurrence(
        ledger,
        occurrence_id="occurrence:clock-unverifiable",
        opens_at=LIFE_TIME,
        closes_at=LIFE_TIME + timedelta(minutes=10),
        precondition_refs=("condition:unverified",),
    )

    await WorldRuntime(world_id=WORLD_ID, ledger=ledger).advance(
        _clock(
            tick_id="occurrence-clock-unverifiable",
            target=LIFE_TIME + timedelta(minutes=1),
        )
    )

    occurrence = next(
        item
        for item in ledger.project().world_occurrences
        if item.occurrence_id == "occurrence:clock-unverifiable"
    )
    assert occurrence.status == "committed"
    assert not any(
        item.event_type == "WorldOccurrenceActivated"
        and item.event_id != "occurrence-activated"
        for item in ledger.project().committed_world_event_refs
    )


@pytest.mark.asyncio
async def test_runtime_records_a_source_bound_outcome_observation_for_an_active_occurrence() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    _commit_occurrence(
        ledger,
        occurrence_id="occurrence:outcome-observed",
        opens_at=LIFE_TIME,
        closes_at=LIFE_TIME + timedelta(minutes=10),
        precondition_refs=("plan:plan-tea",),
    )
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    target = LIFE_TIME + timedelta(minutes=1)
    await runtime.advance(_clock(tick_id="outcome-observed-open", target=target))
    observation = OutcomeObservation(
        schema_version="world-v2.1",
        observation_id="outcome-observation:tea-ready",
        world_id=WORLD_ID,
        logical_time=target,
        created_at=target,
        trace_id="trace:outcome-observation",
        causation_id="sensor:tea-ready",
        correlation_id="correlation:outcome-observation",
        occurrence_id="occurrence:outcome-observed",
        source_kind="clock_plan_precondition",
        source_refs=("plan-tea",),
        observed_payload_ref="sensor-payload:tea-ready",
        observed_payload_hash="a" * 64,
        observed_at=target,
        confidence_bp=9_000,
    )

    first = await runtime.record_outcome_observation(observation)

    projected = ledger.project()
    occurrence = next(
        item
        for item in projected.world_occurrences
        if item.occurrence_id == "occurrence:outcome-observed"
    )
    assert occurrence.entity_revision == 3
    assert occurrence.observation_refs == ("outcome-observation:tea-ready",)
    recorded_ref = next(
        item
        for item in projected.committed_world_event_refs
        if item.event_type == "OutcomeObservationRecorded"
        and item.event_id != "outcome-observed"
    )
    recorded = ledger.lookup_event_commit(recorded_ref.event_id)
    assert recorded is not None
    assert recorded[0].payload()["evidence_refs"] == [
        {
            "claim_purpose": "current_fact",
            "evidence_type": "active_plan",
            "immutable_hash": model_hash(projected.plans[0]),
            "ref_id": "plan-tea",
            "source_world_revision": None,
        }
    ]
    outcome_trigger = next(
        item
        for item in projected.trigger_processes
        if item.process_kind == "outcome_deliberation"
    )
    assert outcome_trigger.source_evidence_ref == recorded_ref.event_id
    assert outcome_trigger.state == "open"
    assert await runtime.record_outcome_observation(observation) == first
    assert ledger.project() == projected


@pytest.mark.asyncio
async def test_runtime_rejects_an_outcome_observation_with_an_unrelated_plan_source() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    _commit_occurrence(
        ledger,
        occurrence_id="occurrence:outcome-source-rejected",
        opens_at=LIFE_TIME,
        closes_at=LIFE_TIME + timedelta(minutes=10),
        precondition_refs=("plan:plan-tea",),
    )
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    target = LIFE_TIME + timedelta(minutes=1)
    await runtime.advance(_clock(tick_id="outcome-source-rejected-open", target=target))
    before = ledger.project()

    with pytest.raises(ValueError, match="not an occurrence precondition"):
        await runtime.record_outcome_observation(
            OutcomeObservation(
                schema_version="world-v2.1",
                observation_id="outcome-observation:unrelated-plan",
                world_id=WORLD_ID,
                logical_time=target,
                created_at=target,
                trace_id="trace:unrelated-plan",
                causation_id="sensor:unrelated-plan",
                correlation_id="correlation:unrelated-plan",
                occurrence_id="occurrence:outcome-source-rejected",
                source_kind="clock_plan_precondition",
                source_refs=("plan:not-a-precondition",),
                observed_payload_ref="sensor-payload:unrelated-plan",
                observed_payload_hash="b" * 64,
                observed_at=target,
                confidence_bp=9_000,
            )
        )

    assert ledger.project() == before
