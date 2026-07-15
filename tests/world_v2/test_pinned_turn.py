from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.advisory_compiler import AdvisoryAdapterInput, AdvisoryCompiler
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
from companion_daemon.world_v2.schemas import Observation, ProjectionCursor, WorldEvent
from companion_daemon.world_v2.matrix_catalog import (
    CandidateDistribution,
    ClassificationCandidate,
    default_matrix_catalog,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:pinned-turn"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _InvalidModel:
    def __init__(self) -> None:
        self.request: ModelInput | None = None

    async def propose(self, _request: ModelInput) -> ModelOutput:
        self.request = _request
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


class _EmotionAdvice:
    adapter_id = "emotion"
    version = "test.1"

    def __init__(self) -> None:
        self.received: AdvisoryAdapterInput | None = None

    async def classify(self, request: AdvisoryAdapterInput) -> tuple[CandidateDistribution, ...]:
        self.received = request
        return (
            CandidateDistribution(
                catalog_version="world-v2-matrix-1",
                field_id="appraisal.negative",
                candidates=(
                    ClassificationCandidate(
                        value="disappointment",
                        weight=7100,
                        confidence=7800,
                        producer="emotion@test.1",
                        source_refs=(request.trigger_ref,),
                        expires_at=request.expires_at,
                    ),
                ),
                produced_at=request.logical_time,
            ),
        )


class _InvalidAdvice(_EmotionAdvice):
    async def classify(self, request: AdvisoryAdapterInput) -> tuple[CandidateDistribution, ...]:
        output = (await super().classify(request))[0]
        return (output.model_copy(update={"field_id": "unknown.advisory.field"}),)


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
        text="我好像有点失望，你刚刚没怎么接住我。",
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


@pytest.mark.asyncio
async def test_pinned_turn_passes_source_bound_advisory_candidates_to_deliberation() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    model = _InvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(), main_model=model, quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
        advisory_compiler=AdvisoryCompiler(
            catalog=default_matrix_catalog(),
            adapters=(advice := _EmotionAdvice(),),
            authority_key=b"pinned-turn-advisory-test-authority-key",
        ),
    )
    runtime = WorldRuntime(world_id=WORLD, ledger=ledger, pinned_turn=turn)

    await runtime.ingest(_observation())

    assert model.request is not None
    content = model.request.model_content_json
    assert '"kind":"appraisal.negative"' in content
    assert '"value":"disappointment"' in content
    assert '"source_refs":["event:trigger:observation:test:message:pinned-turn:1"]' in content
    assert advice.received is not None
    assert advice.received.trigger["text"] == "我好像有点失望，你刚刚没怎么接住我。"
    projection = ledger.project()
    assert projection.world_revision == 2
    assert projection.deliberation_revision == 2


@pytest.mark.asyncio
async def test_invalid_advisory_fails_open_without_blocking_deliberation() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    model = _InvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(), main_model=model, quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
        advisory_compiler=AdvisoryCompiler(
            catalog=default_matrix_catalog(),
            adapters=(_InvalidAdvice(),),
            authority_key=b"pinned-turn-advisory-test-authority-key",
        ),
    )

    await WorldRuntime(world_id=WORLD, ledger=ledger, pinned_turn=turn).ingest(_observation())

    assert model.request is not None
    assert '"advisories":{"availability":"unavailable"' in model.request.model_content_json
    assert '"disappointment"' not in model.request.model_content_json


@pytest.mark.asyncio
async def test_pinned_turn_rejects_observation_not_equal_to_committed_event_payload() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    runtime = WorldRuntime(world_id=WORLD, ledger=ledger)
    observation = _observation()
    await runtime.ingest(observation)
    event_id = "event:trigger:observation:test:message:pinned-turn:1"
    stored = ledger.lookup_event_commit(event_id)
    assert stored is not None
    event, commit = stored
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(), main_model=_InvalidModel(), quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
    )

    with pytest.raises(ValueError, match="does not match its committed authority"):
        await turn.audit_observation(
            observation=observation.model_copy(update={"payload_ref": "forged:payload"}),
            observation_event=event,
            cursor=ProjectionCursor(
                world_revision=commit.world_revision,
                deliberation_revision=commit.deliberation_revision,
                ledger_sequence=commit.ledger_sequence,
            ),
        )
