from __future__ import annotations

from datetime import timedelta

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.affect_acceptance_runtime import AffectAcceptanceRuntime
from companion_daemon.world_v2.affect_proposal_compiler import (
    AffectProposalCompilation,
    AffectProposalCompiler,
)
from companion_daemon.world_v2.appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from companion_daemon.world_v2.appraisal_proposal_compiler import AppraisalProposalCompiler
from companion_daemon.world_v2.appraisal_proposal_worker import AppraisalProposalWorker
from companion_daemon.world_v2.batch_invariants import interaction_appraisal_trigger_identity
from companion_daemon.world_v2.deliberation import DeliberationResult, ModelResultAudit
from companion_daemon.world_v2.immediate_emotion_proposal_worker import (
    ImmediateEmotionProposalWorker,
)
from companion_daemon.world_v2.interaction_appraisal_trigger_runtime import (
    InteractionAppraisalTriggerRuntime,
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
    AffectBaselineProjection,
    AffectProposalProjection,
    ClaimLease,
    ClockObservation,
    EvidenceRef,
    ProjectionCursor,
    TriggerProcess,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.runtime import WorldRuntime

from test_appraisal_authority import (
    NOW,
    WORLD_ID,
    commit,
    event,
    message_payload,
    prepare_claimed_interaction,
)
from test_proposal_audit import _digest, _result


def _additional_claimed_interaction(ledger, *, sequence: int, at=NOW):
    observation_id = f"message:{sequence}"
    payload = message_payload(observation_id)
    payload.update(
        {
            "logical_time": at.isoformat(),
            "created_at": at.isoformat(),
            "received_at": at.isoformat(),
        }
    )
    commit(
        ledger,
        [
            event(
                f"message-event:{sequence}",
                "ObservationRecorded",
                payload,
                at=at,
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
                at=at,
            )
        ],
    )
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:interaction-appraisal",
                attempt_id=f"attempt:interaction:{sequence}",
                acquired_at=at,
                expires_at=at + timedelta(minutes=2),
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
                at=at,
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
    proposal_kind: str = "immediate-emotion",
    component_deltas: list[dict[str, object]] | None = None,
    component_targets: list[dict[str, object]] | None = None,
    at=NOW,
):
    if component_deltas is not None and component_targets is not None:
        raise ValueError("choose legacy deltas or target intensities, not both")
    if sequence == 1:
        _observation, _trigger, evidence = prepare_claimed_interaction(ledger)
    else:
        evidence = _additional_claimed_interaction(ledger, sequence=sequence, at=at)
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
                **(
                    {"component_targets": component_targets}
                    if component_targets is not None
                    else {
                        "component_deltas": component_deltas
                        if component_deltas is not None
                        else [{"name": "hurt", "value": 4200}]
                    }
                ),
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
        proposal_id=f"proposal:{proposal_kind}:{sequence}",
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
            logical_time=at,
            created_at=at,
            actor="agent:companion",
            source="test:immediate-emotion",
            trace_id="trace:immediate-emotion",
            causation_id="cause:immediate-emotion",
            correlation_id="correlation:immediate-emotion",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
            expected_ledger_sequence=head.ledger_sequence,
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


class _CurrentProjectionBaselineOverlay:
    """Test port that simulates one baseline commit after the audit cursor."""

    def __init__(self, ledger, *, cursor: ProjectionCursor, baseline_bp: int) -> None:
        self._ledger = ledger
        self._cursor = cursor
        self._baseline = AffectBaselineProjection(
            dimension="hurt",
            baseline_bp=baseline_bp,
            calibration_revision=3,
            policy_version="affect-baseline-calibration.1",
            last_calibrated_at=NOW,
            calibrated_through=NOW,
            last_calibration_basis_hash="d" * 64,
        )

    @property
    def world_id(self):
        return self._ledger.world_id

    def project_at(self, cursor):
        projection = self._ledger.project_at(cursor)
        if cursor == self._cursor:
            return projection.model_copy(update={"affect_baselines": (self._baseline,)})
        return projection

    def __getattr__(self, name):
        return getattr(self._ledger, name)


class _BoundChangedAffectCompiler(AffectProposalCompiler):
    def record_rebased(
        self,
        *,
        world_id,
        audit_cursor,
        current_cursor,
        proposal_id,
    ):
        del world_id, audit_cursor, current_cursor
        return AffectProposalCompilation(
            status="no_change",
            source_proposal_id=proposal_id,
            source_proposal_event_ref="event:proposal-audit:bound-changed",
            skip_reason="affect_proposal_compiler.target_lower_bound_changed_after_pin",
        )


class _UnusedPinnedTurn:
    async def audit_observation(self, **_kwargs):
        raise AssertionError("the existing cursor-bound decision audit must be reused")


def test_immediate_worker_marks_a_bound_change_for_fresh_affect_consideration() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    proposal, audit_cursor = _record_combined_emotion_proposal(
        ledger,
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 2000}],
    )
    worker = ImmediateEmotionProposalWorker(
        appraisal_worker=AppraisalProposalWorker(
            compiler=AppraisalProposalCompiler(ledger=ledger),
            acceptance=AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
            actor="worker:immediate-appraisal",
        ),
        affect_compiler=_BoundChangedAffectCompiler(ledger=ledger),
        affect_acceptance=AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:immediate-affect",
    )

    result = worker.process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "appraisal_only"
    assert result.requires_fresh_affect_consideration is True
    assert result.affect_skip_reason == (
        "affect_proposal_compiler.target_lower_bound_changed_after_pin"
    )
    assert len(ledger.project().appraisals) == 1
    assert ledger.project().affect_episodes == ()


@pytest.mark.asyncio
async def test_runtime_opens_fresh_affect_trigger_when_the_pinned_bound_changed() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    _record_combined_emotion_proposal(
        ledger,
        proposal_kind="interaction-appraisal",
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 2000}],
    )
    appraisal_worker = AppraisalProposalWorker(
        compiler=AppraisalProposalCompiler(ledger=ledger),
        acceptance=AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:immediate-appraisal",
    )
    immediate_worker = ImmediateEmotionProposalWorker(
        appraisal_worker=appraisal_worker,
        affect_compiler=_BoundChangedAffectCompiler(ledger=ledger),
        affect_acceptance=AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:immediate-affect",
    )
    runtime = InteractionAppraisalTriggerRuntime(
        ledger=ledger,
        pinned_turn=_UnusedPinnedTurn(),  # type: ignore[arg-type]
        worker=appraisal_worker,
        owner_id="worker:interaction-appraisal",
        affect_owner_id="worker:affect",
        immediate_emotion_worker=immediate_worker,
    )

    result = await runtime.drain_one()
    projection = ledger.project()

    assert result.work_status == "accepted"
    assert len(projection.appraisals) == 1
    assert projection.affect_episodes == ()
    assert any(
        process.process_kind == "affect_deliberation" and process.state != "terminal"
        for process in projection.trigger_processes
    )


def test_bound_change_after_pin_preserves_appraisal_and_requests_fresh_affect_consideration() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    proposal, audit_cursor = _record_combined_emotion_proposal(
        ledger,
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 2000}],
    )
    appraisal = AppraisalProposalWorker(
        compiler=AppraisalProposalCompiler(ledger=ledger),
        acceptance=AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:immediate-appraisal",
    ).process(
        world_id=WORLD_ID,
        cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )
    assert appraisal.status == "accepted"
    head = ledger.project()
    current_cursor = ProjectionCursor(
        world_revision=head.world_revision,
        deliberation_revision=head.deliberation_revision,
        ledger_sequence=head.ledger_sequence,
    )
    compiler = AffectProposalCompiler(
        ledger=_CurrentProjectionBaselineOverlay(
            ledger,
            cursor=current_cursor,
            baseline_bp=4500,
        )
    )

    result = compiler.record_rebased(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=current_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "no_change"
    assert result.skip_reason == "affect_proposal_compiler.target_lower_bound_changed_after_pin"
    assert len(ledger.project().appraisals) == 1
    assert ledger.project().affect_episodes == ()


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


def test_target_intensity_replaces_the_same_causal_component_instead_of_accumulating() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    worker = _worker(ledger=ledger, issuer=issuer)
    first, first_cursor = _record_combined_emotion_proposal(
        ledger,
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 5000}],
    )
    worker.process(
        world_id=WORLD_ID,
        audit_cursor=first_cursor,
        proposal_id=first.proposal_id,
    )
    second, second_cursor = _record_combined_emotion_proposal(
        ledger,
        sequence=2,
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 6000}],
    )

    result = worker.process(
        world_id=WORLD_ID,
        audit_cursor=second_cursor,
        proposal_id=second.proposal_id,
    )

    assert result.status == "accepted"
    assert ledger.project().affect_episodes[0].components[0].intensity_bp == 6000


def test_repeated_same_target_intensity_does_not_stack() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    worker = _worker(ledger=ledger, issuer=issuer)
    first, first_cursor = _record_combined_emotion_proposal(
        ledger,
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 3000}],
    )
    worker.process(
        world_id=WORLD_ID,
        audit_cursor=first_cursor,
        proposal_id=first.proposal_id,
    )
    second, second_cursor = _record_combined_emotion_proposal(
        ledger,
        sequence=2,
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 3000}],
    )

    result = worker.process(
        world_id=WORLD_ID,
        audit_cursor=second_cursor,
        proposal_id=second.proposal_id,
    )

    projection = ledger.project()
    assert result.status == "accepted"
    assert projection.affect_episodes[0].entity_revision == 2
    assert projection.affect_episodes[0].components[0].intensity_bp == 3000


@pytest.mark.asyncio
async def test_target_intensity_is_applied_after_materializing_decay() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    worker = _worker(ledger=ledger, issuer=issuer)
    first, first_cursor = _record_combined_emotion_proposal(
        ledger,
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 8000}],
    )
    worker.process(
        world_id=WORLD_ID,
        audit_cursor=first_cursor,
        proposal_id=first.proposal_id,
    )
    later = NOW + timedelta(minutes=10)
    await WorldRuntime(world_id=WORLD_ID, ledger=ledger).advance(
        ClockObservation(
            schema_version="world-v2.1",
            tick_id="affect-target-after-decay:1",
            world_id=WORLD_ID,
            logical_time=NOW,
            created_at=later,
            trace_id="trace:affect-target-after-decay",
            causation_id="scheduler:affect-target-after-decay",
            correlation_id="correlation:affect-target-after-decay",
            logical_time_from=NOW,
            logical_time_to=later,
            reason="scheduled_tick",
        )
    )
    decayed = ledger.project().affect_episodes[0].components[0].intensity_bp
    assert 6000 < decayed < 8000
    second, second_cursor = _record_combined_emotion_proposal(
        ledger,
        sequence=2,
        at=later,
        component_targets=[{"dimension": "hurt", "target_intensity_bp": 6000}],
    )

    result = worker.process(
        world_id=WORLD_ID,
        audit_cursor=second_cursor,
        proposal_id=second.proposal_id,
    )

    projection = ledger.project()
    assert result.status == "accepted"
    assert len(projection.affect_episodes) == 1
    assert projection.affect_episodes[0].components[0].intensity_bp == 6000


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
        assert reopened.rebuild() == projection
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
