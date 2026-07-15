from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.deliberation import (
    Deliberation,
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import (
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import Observation, WorldEvent


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:pinned-turn"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _InvalidModel:
    async def propose(self, _request: ModelInput) -> ModelOutput:
        return ModelOutput(
            model_id="test-main",
            model_version="test.1",
            raw_proposal={},
            input_tokens=1,
            output_tokens=1,
        )


class _InvalidQuick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(
            model_id="test-quick",
            model_version="test.1",
            raw_proposal={},
            input_tokens=1,
            output_tokens=1,
        )


def _observation() -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id="observation:pinned-turn:1",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:pinned-turn",
        causation_id="cause:pinned-turn",
        correlation_id="correlation:pinned-turn",
        source="test",
        source_event_id="message:pinned-turn:1",
        actor="user:primary",
        channel="test",
        payload_ref="payload:pinned-turn:1",
        payload_hash="sha256:" + "a" * 64,
        received_at=NOW,
    )


def _world_started() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:pinned-turn:world-started",
        world_id=WORLD,
        event_type="WorldStarted",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:pinned-turn:start",
        causation_id="cause:pinned-turn:start",
        correlation_id="correlation:pinned-turn:start",
        idempotency_key="world-started:pinned-turn",
        payload={},
    )


@pytest.mark.asyncio
async def test_runtime_audits_one_cursor_pinned_turn_without_authorizing_effects() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    capsules: ContextCapsuleCompiler = context_capsule_compiler_from_ledger(ledger=ledger)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=capsules,
        deliberation=Deliberation(
            router=_Router(), main_model=_InvalidModel(), quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
    )
    runtime = WorldRuntime(world_id=WORLD, ledger=ledger, pinned_turn=turn)

    first = await runtime.ingest(_observation())
    duplicate = await runtime.ingest(_observation())

    assert first == duplicate
    projection = ledger.project()
    assert projection.world_revision == 2
    assert projection.deliberation_revision == 2
    assert len(projection.model_result_audits) == 2
    assert projection.proposal_audits == ()
