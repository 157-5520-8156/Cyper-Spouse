from __future__ import annotations

from datetime import timedelta

import pytest

from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.outcome_candidate_reader import OutcomeCandidateReader
from companion_daemon.world_v2.outcome_proposal_compiler import (
    OutcomeProposalCompiler,
    OutcomeProposalCompilerError,
)
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    ClaimLease,
    DueWindow,
    EvidenceRef,
    OutcomeCandidateDescriptor,
    OutcomeObservation,
    WorldOccurrenceProjection,
)

from test_life_projection import LIFE_TIME, WORLD_ID, commit, event, model_hash, mutation, seed_through_proposal
from test_occurrence_clock_continuation import _clock
from test_proposal_audit import _digest, _result
from companion_daemon.world_v2.ledger import WorldLedger


async def _prepare_claimed_outcome(*, include_content: bool = True):
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    store = InMemoryImmutableLifeContentStore()
    seed_through_proposal(ledger)
    plan = next(item for item in ledger.project().plans if item.plan_id == "plan-tea")
    candidate_text = "茶已经泡好，林把杯子推到桌边，等着对方先尝一口。"
    content_ref = "content:outcome:tea-ready"
    content_hash = life_content_payload_hash(candidate_text)
    if include_content:
        store.put_if_absent(
            StoredLifeContent(
                content_ref=content_ref,
                content_kind="outcome_candidate",
                content_payload_hash=content_hash,
                text=candidate_text,
            )
        )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:compiler-outcome",
        entity_revision=1,
        trigger_ref="trigger:compiler-outcome",
        participant_refs=("npc:lin",),
        location_ref="room:kitchen",
        time_window=DueWindow(
            opens_at=LIFE_TIME,
            closes_at=LIFE_TIME + timedelta(minutes=10),
        ),
        precondition_refs=("plan:plan-tea",),
        candidate_outcome_refs=("candidate:tea-ready",),
        candidate_outcomes=(
            OutcomeCandidateDescriptor(
                candidate_result_ref="candidate:tea-ready",
                result_id="result:tea-ready",
                result_payload_ref="payload:tea-ready",
                result_payload_hash="sha256:" + "e" * 64,
                privacy_class="private",
                content_ref=content_ref,
                content_payload_hash=content_hash,
            ),
        ),
        visibility="private",
        status="committed",
    )
    plan_evidence = EvidenceRef(
        ref_id=plan.plan_id,
        evidence_type="active_plan",
        claim_purpose="future_plan",
        immutable_hash=model_hash(plan),
    ).model_dump(mode="json")
    commit(
        ledger,
        [
            event(
                "outcome-compiler:committed",
                "WorldOccurrenceCommitted",
                {
                    **mutation(
                        "outcome-compiler:committed",
                        expected_revision=0,
                        evidence_refs=[plan_evidence],
                    ),
                    "occurrence": occurrence.model_dump(mode="json"),
                },
            )
        ],
    )
    target = LIFE_TIME + timedelta(minutes=1)
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    await runtime.advance(_clock(tick_id="outcome-compiler:open", target=target))
    await runtime.record_outcome_observation(
        OutcomeObservation(
            schema_version="world-v2.1",
            observation_id="outcome-observation:compiler-tea-ready",
            world_id=WORLD_ID,
            logical_time=target,
            created_at=target,
            trace_id="trace:outcome-compiler",
            causation_id="sensor:outcome-compiler",
            correlation_id="correlation:outcome-compiler",
            occurrence_id=occurrence.occurrence_id,
            source_kind="clock_plan_precondition",
            source_refs=("plan-tea",),
            observed_payload_ref="sensor-payload:tea-ready",
            observed_payload_hash="a" * 64,
            observed_at=target,
            confidence_bp=9_000,
        )
    )
    trigger = next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "outcome_deliberation"
    )
    claimed = trigger.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:outcome",
                attempt_id="attempt:outcome:1",
                acquired_at=target,
                expires_at=target + timedelta(minutes=2),
            ),
            "attempt_ids": ("attempt:outcome:1",),
        }
    )
    commit(
        ledger,
        [
            event(
                "outcome-compiler:claimed",
                "TriggerProcessClaimed",
                {"process": claimed.model_dump(mode="json")},
                at=target,
            )
        ],
    )
    source_event_id = "event:outcome-observation:outcome-observation:compiler-tea-ready"
    located = ledger.lookup_event_commit(source_event_id)
    assert located is not None
    source_event, source_commit = located
    return ledger, store, target, claimed, source_event, source_commit


def _audited_proposal(*, ledger, target, source_event, source_commit, result_payload_hash=None):
    payload_hash = result_payload_hash or "sha256:" + "e" * 64
    observation_id = "outcome-observation:compiler-tea-ready"
    change = TypedChange(
        change_id="change:generic-outcome:1",
        kind="outcome_settlement",
        target_id="occurrence:compiler-outcome",
        transition="settle",
        expected_entity_revision=3,
        evidence_refs=(source_event.event_id,),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="outcome_settlement.v1",
            value={
                "outcome_proposal_id": "model-hint:ignored",
                "candidate_result_ref": "candidate:tea-ready",
                "result_id": "result:tea-ready",
                "entity_id": "occurrence:compiler-outcome",
                "entity_revision": 3,
                "observations": [
                    {
                        "ref_id": observation_id,
                        "source_world_revision": source_commit.world_revision,
                        "immutable_hash": "sha256:" + source_event.payload_hash,
                    }
                ],
                "result_payload": {
                    "object_ref": "payload:tea-ready",
                    "schema_version": "outcome-result.1",
                    "payload_hash": payload_hash,
                },
            },
        ),
    )
    proposal = DecisionProposal(
        proposal_id="proposal:generic-outcome:1",
        trigger_ref=source_event.event_id,
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=source_event.event_id,
                evidence_kind="committed_world_event",
                source_world_revision=source_commit.world_revision,
                immutable_hash="sha256:" + source_event.payload_hash,
            ),
        ),
        proposed_changes=(change,),
        action_intents=(),
        confidence=8_400,
        brief_rationale="The observed precondition makes this one frozen result appropriate.",
        behavior_tendency="continue_life",
        stance="settle_verified_outcome",
        display_strategy="private",
    )
    base = _result()
    result = DeliberationResult(
        result_id="deliberation:"
        + _digest(
            {
                "capsule_id": base.capsule_id,
                "proposal_hash": proposal.proposal_hash,
                "attempt_audits": [base.audit.model_dump(mode="json")],
            }
        ),
        capsule_id=base.capsule_id,
        proposal=proposal,
        audit=base.audit,
        attempt_audits=(base.audit,),
    )
    head = ledger.project()
    return proposal, ProposalAuditRecorder(ledger=ledger).record(
        result,
        ProposalAuditContext(
            world_id=WORLD_ID,
            trigger_ref=source_event.event_id,
            logical_time=target,
            created_at=target,
            actor="agent:companion",
            source="test",
            trace_id="trace:generic-outcome",
            causation_id="cause:generic-outcome",
            correlation_id="correlation:generic-outcome",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
        ),
    )


@pytest.mark.asyncio
async def test_compiler_records_a_source_bound_outcome_candidate() -> None:
    ledger, store, target, claimed, source_event, source_commit = await _prepare_claimed_outcome()
    proposal, recorded = _audited_proposal(
        ledger=ledger,
        target=target,
        source_event=source_event,
        source_commit=source_commit,
    )

    compiler = OutcomeProposalCompiler(
        ledger=ledger, candidate_reader=OutcomeCandidateReader(store=store)
    )
    verified = compiler.verify_input(
        world_id=WORLD_ID, cursor=recorded.cursor, proposal_id=proposal.proposal_id
    )
    compiled = compiler.record(
        world_id=WORLD_ID, cursor=recorded.cursor, proposal_id=proposal.proposal_id
    )

    assert verified.occurrence_id == "occurrence:compiler-outcome"
    assert verified.deliberation_trigger_id == claimed.trigger_id
    assert compiled.typed_proposal_id == ledger.project().outcome_proposals[-1].outcome_proposal_id
    typed = ledger.project().outcome_proposals[-1]
    assert typed.decision_proposal_id == proposal.proposal_id
    assert typed.source_observation_id == "outcome-observation:compiler-tea-ready"
    assert typed.precondition_refs == ("plan:plan-tea",)


@pytest.mark.asyncio
async def test_compiler_rejects_a_mismatched_result_payload_binding() -> None:
    ledger, store, target, _, source_event, source_commit = await _prepare_claimed_outcome()
    proposal, recorded = _audited_proposal(
        ledger=ledger,
        target=target,
        source_event=source_event,
        source_commit=source_commit,
        result_payload_hash="sha256:" + "d" * 64,
    )

    with pytest.raises(OutcomeProposalCompilerError, match="candidate_result_mismatch"):
        OutcomeProposalCompiler(
            ledger=ledger, candidate_reader=OutcomeCandidateReader(store=store)
        ).verify_input(world_id=WORLD_ID, cursor=recorded.cursor, proposal_id=proposal.proposal_id)


@pytest.mark.asyncio
async def test_compiler_fails_closed_when_candidate_sidecar_is_missing() -> None:
    ledger, store, target, _, source_event, source_commit = await _prepare_claimed_outcome(
        include_content=False
    )
    proposal, recorded = _audited_proposal(
        ledger=ledger,
        target=target,
        source_event=source_event,
        source_commit=source_commit,
    )

    with pytest.raises(OutcomeProposalCompilerError, match="candidate_content_unavailable"):
        OutcomeProposalCompiler(
            ledger=ledger, candidate_reader=OutcomeCandidateReader(store=store)
        ).verify_input(world_id=WORLD_ID, cursor=recorded.cursor, proposal_id=proposal.proposal_id)
