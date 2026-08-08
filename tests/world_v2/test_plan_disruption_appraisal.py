"""Plan disruption appraisal: one durable inner-feeling opportunity per abandoned plan."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanRuntime,
    ActivityPlanTransitionCommand,
)
from companion_daemon.world_v2.batch_invariants import plan_disruption_appraisal_trigger_identity
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.plan_disruption_appraisal_trigger import (
    PlanDisruptionAppraisalTriggerOpener,
    plan_disruption_opportunity,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import DueWindow, Observation, TriggerProcess, WorldEvent


WORLD_ID = "world:plan-disruption"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _seed_event(event_id: str, event_type: str, payload: dict[str, object], *, at: datetime) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=at,
        created_at=at,
        actor="system:test",
        source="test",
        trace_id="trace:plan-disruption-fixture",
        causation_id=event_id,
        correlation_id="correlation:plan-disruption",
        idempotency_key=event_id,
        payload=payload,
    )


async def _abandoned_plan_world(
):
    """One in-memory world with a clock, one observed message, and appraisal lanes."""

    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    ledger.commit(
        (_seed_event("event:start", "WorldStarted", {}, at=NOW - timedelta(seconds=1)),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    projection = ledger.project()
    ledger.commit(
        (
            _seed_event(
                "event:clock",
                "ClockAdvanced",
                {
                    "logical_time_from": (NOW - timedelta(seconds=1)).isoformat(),
                    "logical_time_to": NOW.isoformat(),
                },
                at=NOW,
            ),
        ),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
    )
    await runtime.ingest(
        Observation(
            schema_version="world-v2.1",
            observation_id="observation:source",
            world_id=WORLD_ID,
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:plan-disruption-fixture",
            causation_id="inbound:1",
            correlation_id="correlation:plan-disruption",
            source="test",
            source_event_id="message:1",
            actor="user:primary",
            channel="test",
            payload_ref="payload:source",
            payload_hash="a" * 64,
            text="周三读书会你还去吗？",
            received_at=NOW,
            reply_context={"target": "user:primary"},
        )
    )
    return runtime, ledger, None, None


def _plan_and_abandon(
    ledger,
    *,
    plan_id: str = "plan:literature",
    activity_id: str = "activity:literature",
    future_window: bool = False,
) -> str:
    """Create one companion-owned plan, abandon it, and return the abandonment event id."""

    plans = ActivityPlanRuntime(ledger=ledger, owner_actor_ref="agent:companion")
    plans.plan(
        ActivityPlanCommand(
            command_id=f"command:{plan_id}",
            world_id=WORLD_ID,
            source_observation_id="observation:source",
            plan_id=plan_id,
            activity_id=activity_id,
            activity_kind="literature_meetup",
            importance_bp=6_000,
            participant_refs=("friend:wenjing",),
            scheduled_window=(
                DueWindow(opens_at=NOW + timedelta(days=2), closes_at=NOW + timedelta(days=2, hours=2))
                if future_window
                else None
            ),
        ),
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:plan",
        causation_id="cause:plan",
        correlation_id="correlation:plan",
    )
    plans.transition(
        ActivityPlanTransitionCommand(
            command_id=f"command:{plan_id}:abandon",
            world_id=WORLD_ID,
            source_observation_id="observation:source",
            plan_id=plan_id,
            operation="abandon",
        ),
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:plan",
        causation_id="cause:abandon",
        correlation_id="correlation:plan",
    )
    plan = next(item for item in ledger.project().plans if item.plan_id == plan_id)
    assert plan.status == "abandoned" and plan.authority_origin is not None
    return plan.authority_origin.accepted_event_ref


# --- projection layer -------------------------------------------------------


@pytest.mark.asyncio
async def test_opportunity_binds_the_abandoned_plan_facts() -> None:
    _runtime, ledger, _worker, _turn = await _abandoned_plan_world()
    abandoned_ref = _plan_and_abandon(ledger, future_window=True)

    opportunity = plan_disruption_opportunity(ledger.project())

    assert opportunity is not None
    assert opportunity.source_evidence_ref == abandoned_ref
    assert opportunity.plan_id == "plan:literature"
    assert opportunity.activity_kind == "literature_meetup"
    assert opportunity.was_future_plan is True
    assert opportunity.participant_refs == ("friend:wenjing",)
    assert opportunity.trigger_id == plan_disruption_appraisal_trigger_identity(
        WORLD_ID, abandoned_ref
    )
    # Pure derivation: repeated evaluation of the same projection is identical.
    assert opportunity == plan_disruption_opportunity(ledger.project())


@pytest.mark.asyncio
async def test_only_the_latest_abandonment_can_open() -> None:
    _runtime, ledger, _worker, _turn = await _abandoned_plan_world()
    _plan_and_abandon(ledger, plan_id="plan:first", activity_id="activity:first")
    second_ref = _plan_and_abandon(ledger, plan_id="plan:second", activity_id="activity:second")
    opener = PlanDisruptionAppraisalTriggerOpener(ledger=ledger, owner_id="worker:appraisal")

    opportunity = plan_disruption_opportunity(ledger.project())
    assert opportunity is not None and opportunity.source_evidence_ref == second_ref

    assert await opener.open_once() is not None
    # The stale first abandonment never resurfaces once a newer one anchored.
    assert plan_disruption_opportunity(ledger.project()) is None
    assert await opener.open_once() is None


@pytest.mark.asyncio
async def test_opener_is_idempotent_per_abandonment_anchor() -> None:
    _runtime, ledger, _worker, _turn = await _abandoned_plan_world()
    abandoned_ref = _plan_and_abandon(ledger)
    opener = PlanDisruptionAppraisalTriggerOpener(ledger=ledger, owner_id="worker:appraisal")

    trigger_id = await opener.open_once()
    duplicate = await opener.open_once()

    assert trigger_id is not None and duplicate is None
    process = next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "plan_disruption_appraisal"
    )
    assert process.trigger_id == trigger_id
    assert process.state == "open"
    assert process.source_evidence_ref == abandoned_ref
    assert process.trigger_ref == f"plan-disruption:{abandoned_ref}"


# --- reducer layer ----------------------------------------------------------


def _opened_event(process: TriggerProcess) -> WorldEvent:
    payload = {"process": process.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessOpened", world_id=WORLD_ID, payload=payload
    )
    assert identity is not None
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:plan-disruption-test:opened:" + _digest(process.trigger_id),
        world_id=WORLD_ID,
        event_type="TriggerProcessOpened",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:appraisal",
        source="test",
        trace_id="trace:plan-disruption-test",
        causation_id="test",
        correlation_id="test",
        idempotency_key=identity,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_reducer_rejects_a_trigger_bound_to_a_non_abandonment_event() -> None:
    _runtime, ledger, _worker, _turn = await _abandoned_plan_world()
    _plan_and_abandon(ledger)
    observation_ref = next(
        item.event_id
        for item in ledger.project().committed_world_event_refs
        if item.event_type == "ObservationRecorded"
    )
    projection = ledger.project()
    forged = TriggerProcess(
        trigger_id=plan_disruption_appraisal_trigger_identity(WORLD_ID, observation_ref),
        trigger_ref=f"plan-disruption:{observation_ref}",
        process_kind="plan_disruption_appraisal",
        source_evidence_ref=observation_ref,
        state="open",
    )

    with pytest.raises(ValueError, match="requires a committed plan abandonment"):
        ledger.commit(
            (_opened_event(forged),),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )

@pytest.mark.asyncio
async def test_reducer_rejects_a_trigger_with_a_forged_identity() -> None:
    _runtime, ledger, _worker, _turn = await _abandoned_plan_world()
    abandoned_ref = _plan_and_abandon(ledger)
    projection = ledger.project()
    forged = TriggerProcess(
        trigger_id="trigger:plan-disruption-appraisal:" + "0" * 64,
        trigger_ref=f"plan-disruption:{abandoned_ref}",
        process_kind="plan_disruption_appraisal",
        source_evidence_ref=abandoned_ref,
        state="open",
    )

    with pytest.raises(ValueError, match="identity is not deterministic"):
        ledger.commit(
            (_opened_event(forged),),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
