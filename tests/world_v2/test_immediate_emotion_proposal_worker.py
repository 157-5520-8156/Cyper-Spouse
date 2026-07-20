from __future__ import annotations

from datetime import timedelta

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.affect_acceptance_runtime import AffectAcceptanceRuntime
from companion_daemon.world_v2.affect_proposal_compiler import AffectProposalCompiler
from companion_daemon.world_v2.appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from companion_daemon.world_v2.appraisal_proposal_compiler import AppraisalProposalCompiler
from companion_daemon.world_v2.appraisal_proposal_worker import AppraisalProposalWorker
from companion_daemon.world_v2.batch_invariants import interaction_appraisal_trigger_identity
from companion_daemon.world_v2.deliberation import DeliberationResult, ModelResultAudit
from companion_daemon.world_v2.immediate_emotion_proposal_worker import (
    ImmediateEmotionProposalWorker,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.schemas import (
    AffectProposalProjection,
    ClaimLease,
    EvidenceRef,
    TriggerProcess,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger

from test_appraisal_authority import (
    NOW,
    WORLD_ID,
    commit,
    event,
    message_payload,
    prepare_claimed_interaction,
)
from test_proposal_audit import _digest, _result


def _additional_claimed_interaction(ledger, *, sequence: int):
    observation_id = f"message:{sequence}"
    commit(
        ledger,
        [
            event(
                f"message-event:{sequence}",
                "ObservationRecorded",
                message_payload(observation_id),
            )
        ],
    )
    opened = TriggerProcess(
        trigger_id=interaction_appraisal_trigger_identity(WORLD_ID, observation_id),
        trigger_ref=f"interaction:{observation_id}",
        process_kind="interaction_appraisal",
        source_evidence_ref=observation_id,
        state="open",
    )
    commit(
        ledger,
        [
            event(
                f"interaction-trigger-opened:{sequence}",
                "TriggerProcessOpened",
                {"process": opened.model_dump(mode="json")},
            )
        ],
    )
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:interaction-appraisal",
                attempt_id=f"attempt:interaction:{sequence}",
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=2),
            ),
            "attempt_ids": (f"attempt:interaction:{sequence}",),
        }
    )
    commit(
        ledger,
        [
            event(
                f"interaction-trigger-claimed:{sequence}",
                "TriggerProcessClaimed",
                {"process": claimed.model_dump(mode="json")},
            )
        ],
    )
    message = next(
        item for item in ledger.project().message_observations if item.observation_id == observation_id
    )
    return EvidenceRef(
        ref_id=observation_id,
        evidence_type="observed_message",
        claim_purpose="private_hypothesis",
        source_world_revision=message.world_revision,
        immutable_hash=message.event_payload_hash,
    )


def _record_combined_emotion_proposal(
    ledger,
    *,
    sequence: int = 1,
    component_deltas: list[dict[str, object]] | None = None,
):
    if sequence == 1:
        _observation, _trigger, evidence = prepare_claimed_interaction(ledger)
    else:
        evidence = _additional_claimed_interaction(ledger, sequence=sequence)
    appraisal_change = TypedChange(
        change_id=f"change:immediate-emotion:appraisal:{sequence}",
        kind="appraisal_transition",
        target_id="appraisal:model-hint",
        transition="activate",
        expected_entity_revision=0,
        evidence_refs=(evidence.ref_id,),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="appraisal_transition.v1",
            value={
                "appraisal_id": "appraisal:model-hint",
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
    affect_change = TypedChange(
        change_id=f"change:immediate-emotion:affect:{sequence}",
        kind="affect_transition",
        target_id="affect:model-hint",
        transition="open",
        expected_entity_revision=0,
        evidence_refs=(evidence.ref_id,),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="affect_transition.v1",
            value={
                "episode_id": "affect:model-hint",
                "appraisal_change_refs": [appraisal_change.change_id],
                "component_deltas": component_deltas
                if component_deltas is not None
                else [{"name": "hurt", "value": 4200}],
                "decay_config": {
                    "object_ref": "policy:decay:standard",
                    "schema_version": "affect-decay.1",
                    "payload_hash": "sha256:" + "a" * 64,
                },
                "residue_config": {
                    "object_ref": "policy:residue:standard",
                    "schema_version": "affect-residue.1",
                    "payload_hash": "sha256:" + "b" * 64,
                },
            },
        ),
    )
    proposal = DecisionProposal(
        proposal_id=f"proposal:immediate-emotion:{sequence}",
        trigger_ref=f"message-event:{sequence}",
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=evidence.ref_id,
                evidence_kind="observed_message",
                source_world_revision=evidence.source_world_revision,
                immutable_hash="sha256:" + str(evidence.immutable_hash),
            ),
        ),
        proposed_changes=(appraisal_change, affect_change),
        action_intents=(),
        confidence=7900,
        brief_rationale="One bounded inference proposes meaning and its immediate residual affect.",
        affect_decision="propose",
        behavior_tendency="hold_space",
        stance="care_despite_hurt",
        display_strategy="partial_disclosure",
    )
    base = _result()
    if sequence == 1:
        audit = base.audit
    else:
        response_hash = _digest({"response": sequence})
        model_call_id = f"model-call:immediate-emotion:{sequence}"
        audit = ModelResultAudit(
            **{
                **base.audit.model_dump(mode="python"),
                "model_call_id": model_call_id,
                "model_result_ref": "model-result:"
                + _digest(
                    {"model_call_id": model_call_id, "response_hash": response_hash}
                ),
                "attempt_id": f"attempt:immediate-emotion:{sequence}",
                "response_hash": response_hash,
            }
        )
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
            trigger_ref=proposal.trigger_ref,
            logical_time=NOW,
            created_at=NOW,
            actor="agent:companion",
            source="test:immediate-emotion",
            trace_id="trace:immediate-emotion",
            causation_id="cause:immediate-emotion",
            correlation_id="correlation:immediate-emotion",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
        ),
    )
    return proposal, recorded.cursor


def _worker(*, ledger, issuer):
    return ImmediateEmotionProposalWorker(
        appraisal_worker=AppraisalProposalWorker(
            compiler=AppraisalProposalCompiler(ledger=ledger),
            acceptance=AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
            actor="worker:immediate-appraisal",
        ),
        affect_compiler=AffectProposalCompiler(ledger=ledger),
        affect_acceptance=AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:immediate-affect",
    )


def test_one_audited_emotion_proposal_accepts_appraisal_then_rebased_affect_without_model_call() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    proposal, audit_cursor = _record_combined_emotion_proposal(ledger)
    model_results_before = ledger.project().model_result_audits

    result = _worker(ledger=ledger, issuer=issuer).process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )

    projection = ledger.project()
    assert result.status == "accepted"
    assert result.source_proposal_id == proposal.proposal_id
    assert len(projection.appraisals) == 1
    assert projection.appraisals[0].origin.change_id == "change:immediate-emotion:appraisal:1"
    assert len(projection.affect_episodes) == 1
    assert projection.affect_episodes[0].components[0].dimension == "hurt"
    assert projection.affect_episodes[0].components[0].intensity_bp == 4200
    assert projection.model_result_audits == model_results_before

    joined = _worker(ledger=ledger, issuer=issuer).process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )
    assert joined.status == "accepted"
    assert ledger.project() == projection


def test_restart_after_appraisal_acceptance_reuses_original_audit_and_completes_affect(
    tmp_path,
) -> None:
    path = tmp_path / "immediate-emotion-recovery.sqlite3"
    first_issuer = AcceptedLedgerBatchIssuer()
    first = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=first_issuer,
    )
    proposal, audit_cursor = _record_combined_emotion_proposal(first)
    appraisal = AppraisalProposalWorker(
        compiler=AppraisalProposalCompiler(ledger=first),
        acceptance=AppraisalAcceptanceRuntime(ledger=first, batch_issuer=first_issuer),
        actor="worker:immediate-appraisal",
    ).process(
        world_id=WORLD_ID,
        cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )
    assert appraisal.status == "accepted"
    original_audit = next(
        item for item in first.project().proposal_audits if item.proposal_id == proposal.proposal_id
    )
    assert first.project().affect_episodes == ()
    first.close()

    reopened_issuer = AcceptedLedgerBatchIssuer()
    reopened = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=reopened_issuer,
    )
    try:
        result = _worker(ledger=reopened, issuer=reopened_issuer).process(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            proposal_id=proposal.proposal_id,
        )
        projection = reopened.project()

        assert result.status == "accepted"
        assert len(projection.model_result_audits) == 1
        assert len(projection.appraisals) == 1
        assert len(projection.affect_episodes) == 1
        assert result.affect_compile_commit is not None
        proposal_event = next(
            reopened.lookup_event_commit(event_id)[0]
            for event_id in result.affect_compile_commit.event_ids
            if reopened.lookup_event_commit(event_id)[0].event_type == "ProposalRecorded"
        )
        typed = AffectProposalProjection.model_validate_json(proposal_event.payload_json)
        assert typed.evaluated_world_revision > original_audit.evaluated_world_revision
        assert typed.source_audit is not None
        assert typed.source_audit.proposal_event_ref == original_audit.event_ref
        assert typed.source_audit.proposal_event_payload_hash == original_audit.event_payload_hash
        assert typed.source_audit.model_result_ref == original_audit.model_result_ref
        assert typed.source_audit.capsule_id == original_audit.capsule_id
        assert typed.source_audit.change_id == "change:immediate-emotion:affect:1"
    finally:
        reopened.close()


def test_second_same_cluster_dimension_merges_into_existing_episode_without_another_model_call() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    worker = _worker(ledger=ledger, issuer=issuer)
    first, first_cursor = _record_combined_emotion_proposal(ledger)
    worker.process(
        world_id=WORLD_ID,
        audit_cursor=first_cursor,
        proposal_id=first.proposal_id,
    )
    second, second_cursor = _record_combined_emotion_proposal(ledger, sequence=2)
    model_results_before = ledger.project().model_result_audits

    result = worker.process(
        world_id=WORLD_ID,
        audit_cursor=second_cursor,
        proposal_id=second.proposal_id,
    )

    projection = ledger.project()
    assert result.status == "accepted"
    assert len(projection.affect_episodes) == 1
    assert projection.affect_episodes[0].entity_revision == 2
    assert projection.affect_episodes[0].components[0].intensity_bp == 8400
    assert projection.model_result_audits == model_results_before

    joined = worker.process(
        world_id=WORLD_ID,
        audit_cursor=second_cursor,
        proposal_id=second.proposal_id,
    )
    assert joined.status == "accepted"
    assert ledger.project() == projection


def test_restart_after_second_appraisal_recovers_the_same_merge_update(tmp_path) -> None:
    path = tmp_path / "immediate-emotion-merge-recovery.sqlite3"
    first_issuer = AcceptedLedgerBatchIssuer()
    first = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=first_issuer,
    )
    worker = _worker(ledger=first, issuer=first_issuer)
    initial, initial_cursor = _record_combined_emotion_proposal(first)
    worker.process(
        world_id=WORLD_ID,
        audit_cursor=initial_cursor,
        proposal_id=initial.proposal_id,
    )
    second, second_cursor = _record_combined_emotion_proposal(first, sequence=2)
    appraisal = AppraisalProposalWorker(
        compiler=AppraisalProposalCompiler(ledger=first),
        acceptance=AppraisalAcceptanceRuntime(ledger=first, batch_issuer=first_issuer),
        actor="worker:immediate-appraisal",
    ).process(
        world_id=WORLD_ID,
        cursor=second_cursor,
        proposal_id=second.proposal_id,
    )
    assert appraisal.status == "accepted"
    assert first.project().affect_episodes[0].entity_revision == 1
    first.close()

    reopened_issuer = AcceptedLedgerBatchIssuer()
    reopened = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=reopened_issuer,
    )
    try:
        result = _worker(ledger=reopened, issuer=reopened_issuer).process(
            world_id=WORLD_ID,
            audit_cursor=second_cursor,
            proposal_id=second.proposal_id,
        )
        projection = reopened.project()
        assert result.status == "accepted"
        assert len(projection.affect_episodes) == 1
        assert projection.affect_episodes[0].entity_revision == 2
        assert projection.affect_episodes[0].components[0].intensity_bp == 8400
        assert result.affect_compile_commit is not None
        proposal_event = next(
            reopened.lookup_event_commit(event_id)[0]
            for event_id in result.affect_compile_commit.event_ids
            if reopened.lookup_event_commit(event_id)[0].event_type == "ProposalRecorded"
        )
        typed = AffectProposalProjection.model_validate_json(proposal_event.payload_json)
        assert typed.transition_kind == "update"
        assert typed.proposed_mutation.event_type == "AffectEpisodeUpdated"

        joined = _worker(ledger=reopened, issuer=reopened_issuer).process(
            world_id=WORLD_ID,
            audit_cursor=second_cursor,
            proposal_id=second.proposal_id,
        )
        assert joined.status == "accepted"
        assert reopened.project() == projection
    finally:
        reopened.close()


def test_merge_update_adds_a_new_dimension_to_the_selected_existing_episode() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    worker = _worker(ledger=ledger, issuer=issuer)
    first, first_cursor = _record_combined_emotion_proposal(ledger)
    worker.process(
        world_id=WORLD_ID,
        audit_cursor=first_cursor,
        proposal_id=first.proposal_id,
    )
    second, second_cursor = _record_combined_emotion_proposal(
        ledger,
        sequence=2,
        component_deltas=[
            {"name": "hurt", "value": 1800},
            {"name": "anger", "value": 2400},
        ],
    )

    result = worker.process(
        world_id=WORLD_ID,
        audit_cursor=second_cursor,
        proposal_id=second.proposal_id,
    )

    projection = ledger.project()
    assert result.status == "accepted"
    assert len(projection.affect_episodes) == 1
    assert projection.affect_episodes[0].entity_revision == 2
    assert {
        item.dimension: item.intensity_bp for item in projection.affect_episodes[0].components
    } == {"hurt": 6000, "anger": 2400}


def test_true_multi_episode_merge_conflict_fails_soft_after_appraisal() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    worker = _worker(ledger=ledger, issuer=issuer)
    first, first_cursor = _record_combined_emotion_proposal(ledger)
    worker.process(
        world_id=WORLD_ID,
        audit_cursor=first_cursor,
        proposal_id=first.proposal_id,
    )
    second, second_cursor = _record_combined_emotion_proposal(
        ledger,
        sequence=2,
        component_deltas=[{"name": "anger", "value": 2400}],
    )
    worker.process(
        world_id=WORLD_ID,
        audit_cursor=second_cursor,
        proposal_id=second.proposal_id,
    )
    assert len(ledger.project().affect_episodes) == 2
    third, third_cursor = _record_combined_emotion_proposal(
        ledger,
        sequence=3,
        component_deltas=[
            {"name": "hurt", "value": 1200},
            {"name": "anger", "value": 1600},
        ],
    )

    result = worker.process(
        world_id=WORLD_ID,
        audit_cursor=third_cursor,
        proposal_id=third.proposal_id,
    )

    projection = ledger.project()
    assert result.status == "appraisal_only"
    assert len(projection.appraisals) == 3
    assert len(projection.affect_episodes) == 2
    assert result.typed_affect_proposal_id is None
    assert (
        result.affect_skip_reason
        == "affect_proposal_compiler.merge_target_ambiguous"
    )

    joined = worker.process(
        world_id=WORLD_ID,
        audit_cursor=third_cursor,
        proposal_id=third.proposal_id,
    )
    assert joined.status == "appraisal_only"
    assert joined.affect_skip_reason == result.affect_skip_reason
    assert ledger.project() == projection
