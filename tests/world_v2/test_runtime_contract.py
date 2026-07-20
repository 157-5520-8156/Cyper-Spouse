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
from companion_daemon.world_v2.errors import IdempotencyConflict
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.projection import (
    AuthenticatedProjectionPrincipal,
    ProjectionAuthority,
    ProjectionCapabilityIssuer,
    ProjectionGrant,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def operator_authority() -> ProjectionAuthority:
    return ProjectionAuthority(
        grants=(
            ProjectionGrant(
                world_id="world-v2-test",
                viewer_id="operator:test",
                viewer_kind="dashboard_operator",
                permissions=frozenset({"projection:debug_refs"}),
                redaction_policy="operator-default-v1",
            ),
        )
    )


class StaticPrincipalVerifier:
    def __init__(self, principal_id: str) -> None:
        self._principal_id = principal_id

    def authenticate(self, credential: object) -> AuthenticatedProjectionPrincipal:
        if credential is not TEST_CREDENTIAL:
            raise PermissionError("invalid test credential")
        return AuthenticatedProjectionPrincipal(
            principal_id=self._principal_id,
            world_id="world-v2-test",
            authentication_context="test-fixture",
        )


TEST_CREDENTIAL = object()


class AmbiguousClockCommitLedger(WorldLedger):
    """Simulate a competing writer committing before this caller sees success."""

    def __init__(self, *, world_id: str) -> None:
        super().__init__(world_id=world_id)
        self._fail_clock_once = True

    def commit(self, events, **kwargs):
        result = super().commit(events, **kwargs)
        if self._fail_clock_once and events[0].event_type == "ClockAdvanced":
            self._fail_clock_once = False
            raise IdempotencyConflict("simulated ambiguous concurrent commit")
        return result


def bind_operator(access: ProjectionAuthority, request: ProjectionRequest) -> ProjectionRequest:
    return ProjectionCapabilityIssuer(
        authority=access,
        principal_verifier=StaticPrincipalVerifier("operator:test"),
    ).bind(request, credential=TEST_CREDENTIAL)


def observation(*, logical_time: datetime = NOW) -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id="obs-http-message-1",
        world_id="world-v2-test",
        logical_time=logical_time,
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
    access = operator_authority()
    runtime = WorldRuntime.in_memory(world_id="world-v2-test", projection_authority=access)
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
        bind_operator(
            access,
            ProjectionRequest(
                schema_version="world-v2.1",
                request_id="projection-request-1",
                world_id="world-v2-test",
                viewer_kind="dashboard_operator",
                viewer_id="operator:test",
                permissions=frozenset({"projection:debug_refs"}),
                trace_id="trace-project-1",
                include_debug_refs=True,
                redaction_policy="operator-default-v1",
            ),
        )
    )
    assert projection.world_revision == 1
    assert projection.view.debug_observation_refs == ("obs-http-message-1",)
    assert len(projection.projection_hash) == 64


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
    access = ProjectionAuthority(
        grants=(
            ProjectionGrant(
                world_id="world-v2-test",
                viewer_id="room:test",
                viewer_kind="room_renderer",
                permissions=frozenset(),
                redaction_policy="room-public-v1",
            ),
        )
    )
    runtime = WorldRuntime.in_memory(world_id="world-v2-test", projection_authority=access)
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

    first = await runtime.advance(clock)
    await runtime.ingest(observation(logical_time=NOW.replace(hour=13)))
    delayed_duplicate = await runtime.advance(clock)
    assert first == delayed_duplicate
    assert first.committed_world_revision == 1
    assert runtime.project(
        ProjectionCapabilityIssuer(
            authority=access,
            principal_verifier=StaticPrincipalVerifier("room:test"),
        ).bind(
            ProjectionRequest(
                schema_version="world-v2.1",
                request_id="projection-clock",
                world_id="world-v2-test",
                viewer_kind="room_renderer",
                viewer_id="room:test",
                permissions=frozenset(),
                trace_id="trace-projection-clock",
                redaction_policy="room-public-v1",
            ),
            credential=TEST_CREDENTIAL,
        )
    ).logical_time == NOW.replace(hour=13)

    conflicting_tick = clock.model_copy(update={"reason": "manual_tick"})
    with pytest.raises(IdempotencyConflict, match="different content"):
        await runtime.advance(conflicting_tick)

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
async def test_clock_advance_joins_a_commit_that_won_the_identity_race() -> None:
    ledger = AmbiguousClockCommitLedger(world_id="world-v2-test")
    runtime = WorldRuntime(world_id="world-v2-test", ledger=ledger)
    clock = ClockObservation(
        schema_version="world-v2.1",
        tick_id="tick-raced",
        world_id="world-v2-test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace-clock-raced",
        causation_id="scheduler:tick-raced",
        correlation_id="scheduler:day-raced",
        logical_time_from=NOW,
        logical_time_to=NOW.replace(hour=13),
        reason="scheduled_tick",
    )

    outcome = await runtime.advance(clock)

    assert outcome.committed_world_revision == 1
    assert outcome.ledger_sequence == 1
    assert ledger.project().world_revision == 1


@pytest.mark.asyncio
async def test_settle_defers_unknown_action_result_for_reconciliation() -> None:
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

    outcome = await runtime.settle(result)
    assert outcome.status == "deferred"


def test_runtime_rejects_a_ledger_bound_to_another_world() -> None:
    from companion_daemon.world_v2.ledger import WorldLedger

    with pytest.raises(ValueError, match="ledger.*another world"):
        WorldRuntime(
            world_id="world-v2-test",
            ledger=WorldLedger.in_memory(world_id="different-world"),
        )
