from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from companion_daemon.world_v2.appraisal_proposal_compiler import AppraisalProposalCompiler
from companion_daemon.world_v2.appraisal_proposal_worker import AppraisalProposalWorker
from companion_daemon.world_v2.deliberation import Deliberation, ModelInput, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import ContextRelevanceScope, context_capsule_compiler_from_ledger
from companion_daemon.world_v2.npc_world_appraisal_trigger_runtime import NpcWorldAppraisalTriggerRuntime
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    TypedChange,
)
from companion_daemon.world_v2.settled_world_appraisal_turn import SettledWorldAppraisalTurn
from test_life_projection import WORLD_ID, commit, seed_through_proposal, settlement_batch


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="background", router_version="test.1")


class _NoChangeModel:
    def __init__(self) -> None:
        self.requests: list[ModelInput] = []

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.requests.append(request)
        proposal = DecisionProposal(
            proposal_id="proposal:npc-world:no-change",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(),
            proposed_changes=(),
            action_intents=(),
            confidence=4000,
            brief_rationale="The settled occurrence does not warrant a durable appraisal.",
            affect_decision="no_change",
            behavior_tendency="observe",
            stance="wait",
            display_strategy="withhold",
        )
        return ModelOutput(
            model_id="test-npc-no-change",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )

    async def recover(self, request: ModelInput, _failure: str) -> ModelOutput:
        return await self.propose(request)


class _WorldAppraisalModel(_NoChangeModel):
    async def propose(self, request: ModelInput) -> ModelOutput:
        self.requests.append(request)
        source = request.trigger_evidence[0]
        proposal = DecisionProposal(
            proposal_id="proposal:npc-world:appraisal",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(source,),
            proposed_changes=(
                TypedChange(
                    change_id="change:npc-world:appraisal",
                    kind="appraisal_transition",
                    target_id="appraisal:npc-world:model-hint",
                    transition="activate",
                    expected_entity_revision=0,
                    evidence_refs=(source.ref_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="appraisal_transition.v1",
                        value={
                            "appraisal_id": "appraisal:npc-world:model-hint",
                            "meaning_candidates": [
                                {"meaning": "goal_progress", "confidence": 7000},
                                {"meaning": "uncertainty", "confidence": 3000},
                            ],
                            "attribution": "situation",
                            "severity": 4200,
                            "confidence": 7000,
                            "expiry": None,
                        },
                    ),
                ),
            ),
            action_intents=(),
            confidence=7000,
            brief_rationale="The settled event may matter, but its interpretation remains fallible.",
            behavior_tendency="reflect",
            stance="attend",
            display_strategy="withhold",
        )
        return ModelOutput(
            model_id="test-npc-appraisal",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )


@pytest.mark.asyncio
async def test_settled_npc_event_is_audited_as_world_authority_and_terminates_no_change() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())

    model = _NoChangeModel()
    turn = SettledWorldAppraisalTurn(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(
            ledger=ledger,
            relevance_scope=ContextRelevanceScope(actor_ref="actor:companion"),
        ),
        deliberation=Deliberation(router=_Router(), main_model=model, quick_recovery=model),
        companion_actor_ref="actor:companion",
    )
    worker = AppraisalProposalWorker(
        compiler=AppraisalProposalCompiler(ledger=ledger),
        acceptance=AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:appraisal",
    )

    result = await NpcWorldAppraisalTriggerRuntime(
        ledger=ledger,
        turn=turn,
        worker=worker,
        owner_id="worker:appraisal",
    ).drain_one()

    assert result.status == "processed"
    assert result.work_status == "no_change"
    assert len(model.requests) == 1
    request = model.requests[0]
    assert request.trigger_ref == "occurrence-settled"
    assert request.trigger_message is None
    assert request.trigger_evidence[0].evidence_kind == "settled_world_event"
    assert json.loads(request.model_content_json)["slices"]["world_life"]["items"][0]["value"][
        "occurrence_id"
    ] == "occurrence-tea"
    process = ledger.project().trigger_processes[0]
    assert process.process_kind == "npc_world_appraisal"
    assert process.state == "terminal"


@pytest.mark.asyncio
async def test_settled_npc_event_can_create_a_source_bound_companion_appraisal() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())

    model = _WorldAppraisalModel()
    turn = SettledWorldAppraisalTurn(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(
            ledger=ledger,
            relevance_scope=ContextRelevanceScope(actor_ref="actor:companion"),
        ),
        deliberation=Deliberation(router=_Router(), main_model=model, quick_recovery=model),
        companion_actor_ref="actor:companion",
    )
    worker = AppraisalProposalWorker(
        compiler=AppraisalProposalCompiler(
            ledger=ledger, world_appraisal_subject_ref="actor:companion"
        ),
        acceptance=AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:appraisal",
    )

    result = await NpcWorldAppraisalTriggerRuntime(
        ledger=ledger,
        turn=turn,
        worker=worker,
        owner_id="worker:appraisal",
    ).drain_one()

    assert result.status == "processed"
    assert result.work_status == "accepted"
    appraisal = ledger.project().appraisals[0]
    assert appraisal.subject_ref == "actor:companion"
    assert appraisal.evidence_refs[0].ref_id == "occurrence-settled"
    assert appraisal.evidence_refs[0].evidence_type == "settled_world_event"
    assert ledger.project().trigger_processes[0].state == "terminal"
