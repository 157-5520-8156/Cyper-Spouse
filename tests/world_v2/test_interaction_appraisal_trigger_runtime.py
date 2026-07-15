from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from companion_daemon.world_v2.appraisal_proposal_compiler import AppraisalProposalCompiler
from companion_daemon.world_v2.appraisal_proposal_worker import AppraisalProposalWorker
from companion_daemon.world_v2.deliberation import Deliberation, ModelInput, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import context_capsule_compiler_from_ledger
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.proposal_envelope import DecisionProposal
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import Observation, WorldEvent


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:interaction-appraisal-runtime"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="background", router_version="test.1")


class _NoChangeDecisionModel:
    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.calls += 1
        proposal = DecisionProposal(
            proposal_id="proposal:interaction-appraisal:no-change",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(),
            proposed_changes=(),
            action_intents=(),
            confidence=4000,
            brief_rationale="No durable relational interpretation is warranted.",
            affect_decision="no_change",
            behavior_tendency="observe",
            stance="wait",
            display_strategy="withhold",
        )
        return ModelOutput(
            model_id="test-no-change", model_version="test.1", raw_proposal=proposal.model_dump(mode="json")
        )

    async def recover(self, request: ModelInput, _failure: str) -> ModelOutput:
        return await self.propose(request)


def _world_started() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-appraisal:world-started",
        world_id=WORLD,
        event_type="WorldStarted",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:start",
        causation_id="cause:start",
        correlation_id="correlation:start",
        idempotency_key="world-started:interaction-appraisal",
        payload={},
    )


def _observation() -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id="observation:interaction-appraisal:1",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-appraisal",
        causation_id="cause:interaction-appraisal",
        correlation_id="correlation:interaction-appraisal",
        source="test",
        source_event_id="message:interaction-appraisal:1",
        actor="user:primary",
        channel="test",
        payload_ref="payload:interaction-appraisal:1",
        payload_hash="sha256:" + "a" * 64,
        text="我今天只是讲了件小事。",
        received_at=NOW,
        reply_context={"target": "user:primary"},
    )


@pytest.mark.asyncio
async def test_background_interaction_appraisal_terminates_no_change_without_repeating_the_model() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    ingress = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        interaction_appraisal_owner="worker:appraisal",
    )
    await ingress.ingest(_observation())

    model = _NoChangeDecisionModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(router=_Router(), main_model=model, quick_recovery=model),
        companion_actor_ref="agent:companion",
    )
    acceptance = AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    worker = AppraisalProposalWorker(
        compiler=AppraisalProposalCompiler(ledger=ledger),
        acceptance=acceptance,
        actor="worker:appraisal",
    )
    background = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        interaction_appraisal_owner="worker:appraisal",
        appraisal_worker=worker,
        interaction_appraisal_turn=turn,
    )

    result = await background.drain_background_once()

    assert result is not None
    assert result.status == "processed"
    assert result.work_status == "no_change"
    assert ledger.project().trigger_processes[0].state == "terminal"
    assert model.calls == 1
    idle = await background.drain_background_once()
    assert idle is not None and idle.status == "idle"
    assert model.calls == 1
