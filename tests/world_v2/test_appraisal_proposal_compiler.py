from __future__ import annotations

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from companion_daemon.world_v2.appraisal_proposal_compiler import AppraisalProposalCompiler
from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.schemas import ProjectionCursor

from test_appraisal_authority import NOW, WORLD_ID, prepare_claimed_interaction
from test_proposal_audit import _digest, _result


def test_compiler_records_and_accepts_a_source_bound_appraisal() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    _, trigger, evidence = prepare_claimed_interaction(ledger)
    base = _result()
    change = TypedChange(
        change_id="change:decision:appraisal:1",
        kind="appraisal_transition",
        target_id="appraisal:ignored-model-hint",
        transition="activate",
        expected_entity_revision=0,
        evidence_refs=(evidence.ref_id,),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="appraisal_transition.v1",
            value={
                "appraisal_id": "appraisal:ignored-model-hint",
                "meaning_candidates": [
                    {"meaning": "disappointment", "confidence": 7200},
                    {"meaning": "misunderstanding", "confidence": 2800},
                ],
                "attribution": "user",
                "severity": 6500,
                "confidence": 7800,
                "expiry": None,
            },
        ),
    )
    proposal = DecisionProposal(
        proposal_id="proposal:generic-appraisal:1",
        trigger_ref="message-event:1",
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=evidence.ref_id,
                evidence_kind="observed_message",
                source_world_revision=evidence.source_world_revision,
                immutable_hash="sha256:" + str(evidence.immutable_hash),
            ),
        ),
        proposed_changes=(change,),
        action_intents=(),
        confidence=8000,
        brief_rationale="The user may feel dismissed, but it remains fallible.",
        behavior_tendency="hold_space",
        stance="attend",
        display_strategy="partial_disclosure",
    )
    audit = base.audit
    result = DeliberationResult(
        result_id="deliberation:"
        + _digest(
            {
                "capsule_id": base.capsule_id,
                "proposal_hash": proposal.proposal_hash,
                "attempt_audits": [audit.model_dump(mode="json")],
            }
        ),
        capsule_id=base.capsule_id,
        proposal=proposal,
        audit=audit,
        attempt_audits=(audit,),
    )
    head = ledger.project()
    recorded = ProposalAuditRecorder(ledger=ledger).record(
        result,
        ProposalAuditContext(
            world_id=WORLD_ID,
            trigger_ref="message-event:1",
            logical_time=NOW,
            created_at=NOW,
            actor="agent:companion",
            source="test",
            trace_id="trace:generic-appraisal",
            causation_id="cause:generic-appraisal",
            correlation_id="correlation:generic-appraisal",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
        ),
    )

    compilation = AppraisalProposalCompiler(ledger=ledger).record(
        world_id=WORLD_ID, cursor=recorded.cursor, proposal_id=proposal.proposal_id
    )

    assert compilation.status == "candidate_recorded"
    assert compilation.commit is not None
    typed = ledger.project().appraisal_proposals[0]
    assert typed.change_id == change.change_id
    assert typed.trigger_id == trigger.trigger_id
    runtime = AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    accepted = runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(
            cursor=ProjectionCursor(
                world_revision=compilation.commit.world_revision,
                deliberation_revision=compilation.commit.deliberation_revision,
                ledger_sequence=compilation.commit.ledger_sequence,
            ),
            proposal_id=typed.proposal_id,
        ),
        actor="worker:interaction-appraisal",
        source="test:appraisal-acceptance",
    )

    projection = ledger.project()
    assert accepted.world_revision == projection.world_revision
    assert projection.appraisals[0].origin.change_id == change.change_id
    assert projection.appraisals[0].hypotheses[0].meaning == "disappointment"
    assert projection.trigger_processes[0].state == "terminal"
