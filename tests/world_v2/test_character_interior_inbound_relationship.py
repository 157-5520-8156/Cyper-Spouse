from __future__ import annotations

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.character_interior.inbound_relationship import (
    InboundRelationshipSignalWorker,
)
from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.proposal_audit import (
    ProposalAuditContext,
    ProposalAuditRecorder,
)
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.relationship_acceptance_runtime import (
    RelationshipAcceptanceRuntime,
)
from companion_daemon.world_v2.relationship_proposal_compiler import (
    RelationshipProposalCompiler,
    RelationshipProposalCompilerError,
)
from companion_daemon.world_v2.schemas import (
    Observation,
    ProjectionCursor,
    RelationshipProposalProjection,
)
from companion_daemon.world_v2.runtime import WorldRuntime
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


def _cursor(ledger) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _record_relationship_decision(ledger, *, include_signal: bool = True):
    source_event = ledger.lookup_event_commit("message-event:1")[0]
    evidence = ProposalEvidenceRef(
        ref_id=source_event.event_id,
        evidence_kind="committed_world_event",
        source_world_revision=ledger.lookup_event_commit(source_event.event_id)[
            1
        ].world_revision,
        immutable_hash="sha256:" + source_event.payload_hash,
    )
    changes: tuple[TypedChange, ...] = ()
    if include_signal:
        changes = (
            TypedChange(
                change_id="change:inbound-relationship:1",
                kind="relationship_signal",
                target_id="relationship:user:test",
                transition="suggest",
                expected_entity_revision=0,
                evidence_refs=(source_event.event_id,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="relationship_signal.v1",
                    value={
                        "subject_ref": "user:test",
                        "signal_code": "她觉得这次互动让彼此更愿意认真接住对方",
                        "confidence_bp": 7600,
                        "persistence": "durable",
                        "rationale_code": "这是角色对本次真实互动形成的关系理解",
                        "suggested_deltas": {
                            "trust_bp": 120,
                            "closeness_bp": 160,
                            "respect_bp": 40,
                            "reliability_bp": 80,
                            "mutuality_bp": 130,
                            "repair_confidence_bp": 20,
                        },
                    },
                ),
            ),
        )
    proposal = DecisionProposal(
        proposal_id="proposal:inbound-relationship:1",
        trigger_ref=source_event.event_id,
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(evidence,),
        proposed_changes=changes,
        action_intents=(),
        confidence=7600,
        brief_rationale="角色在同一次入站思考里形成了自己的关系理解。",
        behavior_tendency="自然接住",
        stance="愿意靠近",
        display_strategy="不刻意宣告",
        timing_choice="silent",
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
    recorded = ProposalAuditRecorder(ledger=ledger).record(
        result,
        ProposalAuditContext(
            world_id=WORLD_ID,
            trigger_ref=source_event.event_id,
            logical_time=NOW,
            created_at=NOW,
            actor="agent:companion",
            source="test:character-interior",
            trace_id="trace:inbound-relationship",
            causation_id=source_event.event_id,
            correlation_id="correlation:inbound-relationship",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
            expected_ledger_sequence=head.ledger_sequence,
        ),
    )
    return proposal, recorded.cursor, source_event


def _worker(
    *,
    ledger,
    issuer,
    compiler: RelationshipProposalCompiler | None = None,
    acceptance: RelationshipAcceptanceRuntime | None = None,
) -> InboundRelationshipSignalWorker:
    return InboundRelationshipSignalWorker(
        ledger=ledger,
        compiler=compiler or RelationshipProposalCompiler(ledger=ledger),
        acceptance=acceptance
        or RelationshipAcceptanceRuntime(
            ledger=ledger,
            batch_issuer=issuer,
        ),
        owner_id="worker:character-interior-relationship",
    )


class _InjectedCompilerFailure(RelationshipProposalCompiler):
    def record_rebased(self, **_kwargs):
        raise RelationshipProposalCompilerError("injected_technical_failure")


class _InjectedAcceptanceFailure(RelationshipAcceptanceRuntime):
    def accept_runtime_owned(self, **_kwargs):
        raise RuntimeError("injected acceptance failure")


@pytest.mark.asyncio
async def test_one_authored_inbound_relationship_signal_settles_once_without_a_model_call() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    prepare_claimed_interaction(ledger)
    proposal, audit_cursor, source_event = _record_relationship_decision(ledger)
    model_results_before = ledger.project().model_result_audits

    first = await _worker(ledger=ledger, issuer=issuer).process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=_cursor(ledger),
        proposal_id=proposal.proposal_id,
        source_event=source_event,
    )
    settled = ledger.project()

    assert first.status == "accepted"
    assert len(settled.relationship_signals) == 1
    assert settled.relationship_signals[0].subject_ref == "user:test"
    assert settled.model_result_audits == model_results_before
    process = next(
        item
        for item in settled.trigger_processes
        if item.trigger_id == first.trigger_id
    )
    assert process.state == "terminal"
    assert process.runtime_outcome_ref is not None
    assert process.runtime_outcome_ref.endswith(":accepted")

    second = await _worker(ledger=ledger, issuer=issuer).process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=_cursor(ledger),
        proposal_id=proposal.proposal_id,
        source_event=source_event,
    )

    assert second.status == "accepted"
    assert second.replayed is True
    assert ledger.project() == settled


@pytest.mark.asyncio
async def test_omitted_relationship_signal_neither_opens_work_nor_infers_a_change() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    prepare_claimed_interaction(ledger)
    proposal, audit_cursor, source_event = _record_relationship_decision(
        ledger,
        include_signal=False,
    )
    before = ledger.project()

    result = await _worker(ledger=ledger, issuer=issuer).process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=_cursor(ledger),
        proposal_id=proposal.proposal_id,
        source_event=source_event,
    )

    assert result.status == "no_change"
    assert result.trigger_id is None
    assert ledger.project() == before
    assert ledger.project().relationship_signals == ()
    assert not any(
        item.process_kind == "relationship_deliberation"
        for item in ledger.project().trigger_processes
    )


@pytest.mark.asyncio
async def test_world_runtime_settles_relationship_even_when_the_same_turn_has_no_appraisal() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    prepare_claimed_interaction(ledger)
    proposal, audit_cursor, source_event = _record_relationship_decision(ledger)
    worker = _worker(ledger=ledger, issuer=issuer)
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        inbound_state_owner="worker:interaction-appraisal",
        inbound_relationship_worker=worker,
    )

    deferred = await runtime._settle_unified_inbound_state(  # noqa: SLF001
        observation=Observation.model_validate_json(source_event.payload_json),
        observation_event=source_event,
        audit_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )

    projection = ledger.project()
    assert deferred == ()
    assert len(projection.relationship_signals) == 1
    assert next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "interaction_appraisal"
    ).state == "terminal"


@pytest.mark.asyncio
async def test_authored_signal_rebases_after_same_turn_world_effects_without_reauthoring() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    prepare_claimed_interaction(ledger)
    proposal, audit_cursor, source_event = _record_relationship_decision(ledger)
    audited_world_revision = audit_cursor.world_revision
    model_results_before = ledger.project().model_result_audits
    commit(
        ledger,
        [
            event(
                "message-event:same-turn-world-effect",
                "ObservationRecorded",
                message_payload("message:same-turn-world-effect"),
            )
        ],
    )

    result = await _worker(ledger=ledger, issuer=issuer).process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=_cursor(ledger),
        proposal_id=proposal.proposal_id,
        source_event=source_event,
    )

    assert result.status == "accepted"
    assert result.compile_commit is not None
    proposal_event = next(
        ledger.lookup_event_commit(event_id)[0]
        for event_id in result.compile_commit.event_ids
        if ledger.lookup_event_commit(event_id)[0].event_type == "ProposalRecorded"
    )
    typed = RelationshipProposalProjection.model_validate_json(
        proposal_event.payload_json
    )
    assert typed.evaluated_world_revision > audited_world_revision
    assert typed.source_audit is not None
    assert typed.source_audit.proposal_event_ref == ledger.project().proposal_audits[0].event_ref
    assert ledger.project().model_result_audits == model_results_before


@pytest.mark.asyncio
async def test_restart_recovers_a_claimed_signal_after_technical_failure_without_no_change(
    tmp_path,
) -> None:
    path = tmp_path / "inbound-relationship-recovery.sqlite3"
    first_issuer = AcceptedLedgerBatchIssuer()
    first = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=first_issuer,
    )
    prepare_claimed_interaction(first)
    proposal, audit_cursor, source_event = _record_relationship_decision(first)

    with pytest.raises(
        RelationshipProposalCompilerError,
        match="injected_technical_failure",
    ):
        await _worker(
            ledger=first,
            issuer=first_issuer,
            compiler=_InjectedCompilerFailure(ledger=first),
        ).process(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=_cursor(first),
            proposal_id=proposal.proposal_id,
            source_event=source_event,
        )

    failed = first.project()
    relationship_process = next(
        item
        for item in failed.trigger_processes
        if item.process_kind == "relationship_deliberation"
    )
    assert relationship_process.state == "claimed"
    assert relationship_process.runtime_outcome_ref is None
    assert failed.relationship_signals == ()
    first.close()

    reopened_issuer = AcceptedLedgerBatchIssuer()
    reopened = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=reopened_issuer,
    )
    try:
        worker = _worker(
            ledger=reopened,
            issuer=reopened_issuer,
        )
        runtime = WorldRuntime(
            world_id=WORLD_ID,
            ledger=reopened,
            inbound_relationship_worker=worker,
        )
        recovered = await runtime.drain_background_once()

        projection = reopened.project()
        assert recovered is not None
        assert recovered.status == "accepted"
        assert len(projection.relationship_signals) == 1
        assert next(
            item
            for item in projection.trigger_processes
            if item.trigger_id == recovered.trigger_id
        ).state == "terminal"
        assert reopened.rebuild() == projection
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_acceptance_survives_completion_crash_and_retry_is_effect_once(
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    prepare_claimed_interaction(ledger)
    proposal, audit_cursor, source_event = _record_relationship_decision(ledger)
    original_commit_at_cursor = ledger.commit_at_cursor
    failed_once = False

    def fail_first_completion(events, *, expected_cursor, commit_id=None):
        nonlocal failed_once
        if (
            not failed_once
            and len(events) == 1
            and events[0].event_type == "TriggerProcessCompleted"
        ):
            failed_once = True
            raise RuntimeError("injected completion crash")
        return original_commit_at_cursor(
            events,
            expected_cursor=expected_cursor,
            commit_id=commit_id,
        )

    monkeypatch.setattr(ledger, "commit_at_cursor", fail_first_completion)
    with pytest.raises(RuntimeError, match="injected completion crash"):
        await _worker(ledger=ledger, issuer=issuer).process(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=_cursor(ledger),
            proposal_id=proposal.proposal_id,
            source_event=source_event,
        )

    interrupted = ledger.project()
    assert len(interrupted.relationship_signals) == 1
    process = next(
        item
        for item in interrupted.trigger_processes
        if item.process_kind == "relationship_deliberation"
    )
    assert process.state == "claimed"
    accepted_event_ids = tuple(
        item.origin.accepted_event_ref for item in interrupted.relationship_signals
    )

    recovered = await _worker(ledger=ledger, issuer=issuer).process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=_cursor(ledger),
        proposal_id=proposal.proposal_id,
        source_event=source_event,
    )

    projection = ledger.project()
    assert recovered.status == "accepted"
    assert tuple(
        item.origin.accepted_event_ref for item in projection.relationship_signals
    ) == accepted_event_ids
    assert len(projection.relationship_signals) == 1
    assert next(
        item
        for item in projection.trigger_processes
        if item.trigger_id == recovered.trigger_id
    ).state == "terminal"


@pytest.mark.asyncio
async def test_pending_compiled_proposal_is_reused_after_acceptance_failure() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    prepare_claimed_interaction(ledger)
    proposal, audit_cursor, source_event = _record_relationship_decision(ledger)

    with pytest.raises(RuntimeError, match="injected acceptance failure"):
        await _worker(
            ledger=ledger,
            issuer=issuer,
            acceptance=_InjectedAcceptanceFailure(
                ledger=ledger,
                batch_issuer=issuer,
            ),
        ).process(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=_cursor(ledger),
            proposal_id=proposal.proposal_id,
            source_event=source_event,
        )

    interrupted = ledger.project()
    assert len(interrupted.relationship_proposals) == 1
    typed_proposal_id = interrupted.relationship_proposals[0].proposal_id
    assert interrupted.relationship_signals == ()

    recovered = await _worker(ledger=ledger, issuer=issuer).process(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=_cursor(ledger),
        proposal_id=proposal.proposal_id,
        source_event=source_event,
    )

    assert recovered.status == "accepted"
    assert recovered.typed_proposal_id == typed_proposal_id
    assert len(ledger.project().relationship_signals) == 1
    assert ledger.project().relationship_proposals == ()
