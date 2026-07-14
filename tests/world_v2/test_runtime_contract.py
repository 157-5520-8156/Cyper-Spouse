from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2 import (
    ClockObservation,
    ExternalObservation,
    Observation,
    ProjectionRequest,
    WorldRuntime,
)
from companion_daemon.world_v2.errors import IdempotencyConflict, UnknownAction


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def observation() -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id="obs-http-message-1",
        world_id="world-v2-test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace-1",
        causation_id="http:message-1",
        correlation_id="conversation-1",
        source="http",
        source_event_id="message-1",
        actor="user:geoff",
        channel="test-http",
        payload_ref="inline:test-payload-1",
        payload_hash="sha256:test-payload-1",
        received_at=NOW,
    )


@pytest.mark.asyncio
async def test_duplicate_ingest_joins_one_trigger_and_advances_world_once() -> None:
    runtime = WorldRuntime.in_memory(world_id="world-v2-test")
    incoming = observation()

    first, duplicate = await asyncio.gather(
        runtime.ingest(incoming),
        runtime.ingest(incoming),
    )

    assert first == duplicate
    assert first.status == "observed_only"
    assert first.trigger_id == "trigger:observation:http:message-1"
    assert first.committed_world_revision == 1

    projection = runtime.project(
        ProjectionRequest(
            schema_version="world-v2.1",
            request_id="projection-request-1",
            viewer_kind="operator_debug",
            viewer_id="operator:test",
            permissions=frozenset({"world:debug"}),
            trace_id="trace-project-1",
            include_debug_refs=True,
            redaction_policy="operator_debug",
        )
    )
    assert projection.world_revision == 1
    assert projection.debug_observation_refs == ("obs-http-message-1",)
    assert len(projection.semantic_hash) == 64


@pytest.mark.asyncio
async def test_same_source_event_with_different_payload_is_an_idempotency_conflict() -> None:
    runtime = WorldRuntime.in_memory(world_id="world-v2-test")
    incoming = observation()
    await runtime.ingest(incoming)

    conflicting = incoming.model_copy(
        update={
            "observation_id": "obs-http-message-conflict",
            "payload_ref": "inline:different",
            "payload_hash": "sha256:different",
        }
    )
    with pytest.raises(IdempotencyConflict):
        await runtime.ingest(conflicting)


@pytest.mark.asyncio
async def test_clock_advance_is_effect_once_and_rejects_time_reversal() -> None:
    runtime = WorldRuntime.in_memory(world_id="world-v2-test")
    clock = ClockObservation(
        schema_version="world-v2.1",
        tick_id="tick-1",
        world_id="world-v2-test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace-clock-1",
        causation_id="scheduler:tick-1",
        correlation_id="scheduler:day-1",
        logical_time_from=NOW,
        logical_time_to=NOW.replace(hour=13),
        reason="scheduled_tick",
    )

    first, duplicate = await runtime.advance(clock), await runtime.advance(clock)
    assert first == duplicate
    assert first.committed_world_revision == 1
    assert runtime.project(
        ProjectionRequest(
            schema_version="world-v2.1",
            request_id="projection-clock",
            viewer_kind="operator_debug",
            viewer_id="operator:test",
            permissions=frozenset(),
            trace_id="trace-projection-clock",
            redaction_policy="operator_debug",
        )
    ).logical_time == NOW.replace(hour=13)

    reversed_clock = clock.model_copy(
        update={
            "tick_id": "tick-reversed",
            "logical_time_from": NOW.replace(hour=13),
            "logical_time_to": NOW,
        }
    )
    with pytest.raises(ValueError, match="move backwards"):
        await runtime.advance(reversed_clock)


@pytest.mark.asyncio
async def test_settle_rejects_result_for_unknown_action() -> None:
    runtime = WorldRuntime.in_memory(world_id="world-v2-test")
    result = ExternalObservation(
        schema_version="world-v2.1",
        result_id="result-1",
        world_id="world-v2-test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace-result-1",
        causation_id="provider:receipt-1",
        correlation_id="conversation-1",
        kind="execution_receipt",
        source="test-provider",
        source_event_id="receipt-1",
        action_id="missing-action",
        idempotency_key="missing-action:receipt-1",
        status="delivered",
        provider_ref="provider-message-1",
        artifact_refs=(),
        cost_actual=0,
        observed_at=NOW,
        raw_payload_hash="sha256:receipt-1",
    )

    with pytest.raises(UnknownAction):
        await runtime.settle(result)


def test_runtime_rejects_a_ledger_bound_to_another_world() -> None:
    from companion_daemon.world_v2.ledger import WorldLedger

    with pytest.raises(ValueError, match="ledger.*another world"):
        WorldRuntime(
            world_id="world-v2-test",
            ledger=WorldLedger.in_memory(world_id="different-world"),
        )
