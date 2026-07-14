from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from companion_daemon.world_v2.errors import IdempotencyConflict

from companion_daemon.world_v2 import Observation, ProjectionRequest, WorldRuntime


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
