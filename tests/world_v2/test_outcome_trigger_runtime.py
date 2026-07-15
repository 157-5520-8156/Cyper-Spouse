from __future__ import annotations

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.context_capsule import ContextCapsuleBudgetPolicy, SliceBudget
from companion_daemon.world_v2.deliberation import Deliberation, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.ledger_context_resolver import context_capsule_compiler_from_ledger
from companion_daemon.world_v2.outcome_acceptance_runtime import OutcomeAcceptanceRuntime
from companion_daemon.world_v2.outcome_candidate_reader import OutcomeCandidateReader
from companion_daemon.world_v2.outcome_deliberation_turn import OutcomeDeliberationTurn
from companion_daemon.world_v2.outcome_proposal_compiler import OutcomeProposalCompiler
from companion_daemon.world_v2.outcome_proposal_worker import OutcomeProposalWorker
from companion_daemon.world_v2.outcome_trigger_runtime import OutcomeTriggerRuntime
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    TypedChange,
)
from test_outcome_proposal_compiler import _prepare_claimed_outcome


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="background", router_version="test.1")


class _MustNotRunModel:
    async def propose(self, _request) -> ModelOutput:
        raise AssertionError("sidecar-missing outcomes must not call deliberation")

    async def recover(self, _request, _failure: str) -> ModelOutput:
        raise AssertionError("sidecar-missing outcomes must not use recovery")


class _SettlingOutcomeModel:
    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, request) -> ModelOutput:
        self.calls += 1
        source = request.trigger_evidence[0]
        proposal = DecisionProposal(
            proposal_id="proposal:outcome-trigger:settle",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(source,),
            proposed_changes=(
                TypedChange(
                    change_id="change:outcome-trigger:settle",
                    kind="outcome_settlement",
                    target_id="occurrence:compiler-outcome",
                    transition="settle",
                    expected_entity_revision=3,
                    evidence_refs=(source.ref_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="outcome_settlement.v1",
                        value={
                            "outcome_proposal_id": "proposal:outcome-trigger:model-hint",
                            "candidate_result_ref": "candidate:tea-ready",
                            "result_id": "result:tea-ready",
                            "entity_id": "occurrence:compiler-outcome",
                            "entity_revision": 3,
                            "observations": [{
                                "ref_id": "outcome-observation:compiler-tea-ready",
                                "source_world_revision": source.source_world_revision,
                                "immutable_hash": source.immutable_hash,
                            }],
                            "result_payload": {
                                "object_ref": "payload:tea-ready",
                                "schema_version": "outcome-result.1",
                                "payload_hash": "sha256:" + "e" * 64,
                            },
                        },
                    ),
                ),
            ),
            action_intents=(), confidence=8_300,
            brief_rationale="The observed plan precondition confirms the frozen tea result.",
            behavior_tendency="continue_life", stance="settle_verified_outcome",
            display_strategy="withhold",
        )
        return ModelOutput(model_id="test-outcome", model_version="v1", raw_proposal=proposal.model_dump(mode="json"))

    async def recover(self, request, _failure: str) -> ModelOutput:
        return await self.propose(request)


@pytest.mark.asyncio
async def test_missing_outcome_candidate_sidecar_is_terminal_without_a_model_call() -> None:
    ledger, store, _target, _claimed, _source_event, _source_commit = await _prepare_claimed_outcome(
        include_content=False
    )
    reader = OutcomeCandidateReader(store=store)
    turn = OutcomeDeliberationTurn(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(
            ledger=ledger,
            policy=ContextCapsuleBudgetPolicy(
                hard_max_characters=100_000,
                current_situation=SliceBudget(max_items=1, max_fields=256, max_characters=20_000),
            ),
        ),
        deliberation=Deliberation(
            router=_Router(), main_model=_MustNotRunModel(), quick_recovery=_MustNotRunModel()
        ),
        candidate_reader=reader,
        companion_actor_ref="actor:companion",
    )
    issuer = AcceptedLedgerBatchIssuer()
    worker = OutcomeProposalWorker(
        compiler=OutcomeProposalCompiler(ledger=ledger, candidate_reader=reader),
        acceptance=OutcomeAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:outcome",
    )

    result = await OutcomeTriggerRuntime(
        ledger=ledger, turn=turn, worker=worker, owner_id="worker:outcome"
    ).drain_one()

    assert result.status == "processed"
    assert result.work_status == "candidate_content_unavailable"
    process = next(item for item in ledger.project().trigger_processes if item.process_kind == "outcome_deliberation")
    assert process.state == "terminal"


@pytest.mark.asyncio
async def test_outcome_worker_audits_compiles_accepts_and_opens_npc_follow_up() -> None:
    ledger, store, _target, _claimed, _source_event, _source_commit = await _prepare_claimed_outcome()
    issuer = AcceptedLedgerBatchIssuer()
    # The fixture creates a plain ledger for compiler-only tests.  Production
    # composition passes this exact issuer at construction; install it here to
    # exercise the accepted-batch capability end-to-end.
    ledger._accepted_batch_issuer = issuer  # noqa: SLF001
    reader = OutcomeCandidateReader(store=store)
    model = _SettlingOutcomeModel()
    turn = OutcomeDeliberationTurn(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(
            ledger=ledger,
            policy=ContextCapsuleBudgetPolicy(
                hard_max_characters=100_000,
                current_situation=SliceBudget(max_items=1, max_fields=256, max_characters=20_000),
            ),
        ),
        deliberation=Deliberation(router=_Router(), main_model=model, quick_recovery=model),
        candidate_reader=reader,
        companion_actor_ref="actor:companion",
    )
    worker = OutcomeProposalWorker(
        compiler=OutcomeProposalCompiler(ledger=ledger, candidate_reader=reader),
        acceptance=OutcomeAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:outcome",
    )

    result = await OutcomeTriggerRuntime(
        ledger=ledger, turn=turn, worker=worker, owner_id="worker:outcome"
    ).drain_one()

    assert result.status == "processed"
    assert model.calls == 1
    assert result.work_status == "accepted"
    projection = ledger.project()
    outcome = next(item for item in projection.world_occurrences if item.occurrence_id == "occurrence:compiler-outcome")
    assert outcome.status == "settled"
    assert any(item.process_kind == "npc_world_appraisal" and item.state == "open" for item in projection.trigger_processes)
    assert next(item for item in projection.trigger_processes if item.process_kind == "outcome_deliberation").state == "terminal"
