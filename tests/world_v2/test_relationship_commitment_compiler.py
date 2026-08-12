from __future__ import annotations

from datetime import timedelta
import hashlib
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.minimal_reply_acceptance import (
    ExpressionBeatMaterial,
    MessagePayloadMaterial,
)
from companion_daemon.world_v2.minimal_reply_events import (
    ExpressionBeatAuthorizedPayload,
    ExpressionPlanAcceptedPayload,
    MessagePayloadStoredPayload,
)
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
from companion_daemon.world_v2.relationship_commitment_acceptance_runtime import (
    relationship_commitment_mutation_event_id,
)
from companion_daemon.world_v2.relationship_events import (
    RelationshipCommitmentAcceptedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.relationship_proposal_compiler import (
    RelationshipProposalCompiler,
    RelationshipProposalCompilerError,
)
from companion_daemon.world_v2.relationship_trigger import (
    relationship_continuity_trigger_id,
)
from companion_daemon.world_v2.relationship_reducers import relationship_primary_id
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import (
    AcceptanceDecisionRef,
    Action,
    ClaimLease,
    CommitResult,
    CommittedWorldEventRef,
    ExecutionReceipt,
    ExpressionBeatLifecycleEntry,
    ExpressionBeatProjection,
    ExpressionPlanLifecycleEntry,
    ExpressionPlanProjection,
    ProjectionCursor,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    StoredMessagePayloadProjection,
    TriggerProcess,
    WorldEvent,
)

from test_appraisal_authority import NOW, WORLD_ID, prepare_claimed_interaction
from test_character_interior_inbound_relationship import (
    _record_relationship_decision,
)
from test_proposal_audit import _digest, _result


TEXT = "好呀，那说好了，你是我朋友了。"
SPAN = "你是我朋友了"
PLAN_ID = "plan:relationship-commitment-expression"
BEAT_ID = "beat:relationship-commitment-expression"
ACTION_ID = "action:relationship-commitment-expression"
RECEIPT_ID = "receipt:relationship-commitment-expression"


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="test:relationship-commitment",
        source="test:relationship-commitment",
        trace_id="trace:relationship-commitment",
        causation_id="event:observation",
        correlation_id="correlation:relationship-commitment",
        idempotency_key=f"identity:{event_id}",
        payload=payload,
    )


def _authorized_action(
    *, proposal_id: str, payload_hash: str, target: str = "user:test"
) -> Action:
    return Action(
        schema_version="world-v2.1",
        action_id=ACTION_ID,
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:relationship-commitment",
        causation_id=proposal_id,
        correlation_id="correlation:relationship-commitment",
        kind="reply",
        layer="external_action",
        intent_ref=f"{proposal_id}:intent:reply",
        actor="agent:companion",
        target=target,
        payload_ref="payload:relationship-commitment-expression",
        payload_hash=payload_hash,
        expression_plan_id=PLAN_ID,
        expression_beat_id=BEAT_ID,
        idempotency_key=ACTION_ID,
        budget_reservation_id="reservation:relationship-commitment-expression",
        state="authorized",
        recovery_policy="effect_once",
    )


def _proof_projection(
    base,
    *,
    proposal_id: str,
    source_event: WorldEvent,
    text: str = TEXT,
    stored_event_text: str | None = None,
    action_target: str = "user:test",
):
    acceptance_id = "acceptance:relationship-commitment-expression"
    payload_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    message = MessagePayloadMaterial(
        payload_ref="payload:relationship-commitment-expression",
        payload_hash=payload_hash,
        text=text,
        content_type="text/plain",
        storage_kind="inline_text",
        privacy_class="private",
    )
    event_text = stored_event_text if stored_event_text is not None else text
    event_message = MessagePayloadMaterial(
        payload_ref="payload:relationship-commitment-expression",
        payload_hash="sha256:" + hashlib.sha256(event_text.encode("utf-8")).hexdigest(),
        text=event_text,
        content_type="text/plain",
        storage_kind="inline_text",
        privacy_class="private",
    )
    stored_event = _event(
        "event:relationship-commitment-message-stored",
        "MessagePayloadStored",
        MessagePayloadStoredPayload(
            acceptance_id=acceptance_id,
            proposal_id=proposal_id,
            message=event_message,
        ).model_dump(mode="json"),
    )
    plan_event = _event(
        "event:relationship-commitment-plan-accepted",
        "ExpressionPlanAccepted",
        ExpressionPlanAcceptedPayload(
            acceptance_id=acceptance_id,
            proposal_id=proposal_id,
            expression_change_id="change:relationship-commitment-expression",
            plan_id=PLAN_ID,
        ).model_dump(mode="json"),
    )
    beat_material = ExpressionBeatMaterial(
        plan_id=PLAN_ID,
        beat_id=BEAT_ID,
        payload=message,
        cancel_policy="cancel-before-dispatch",
        reconsider_policy="reconsider-on-new-observation",
        merge_policy="never",
    )
    beat_event = _event(
        "event:relationship-commitment-beat-authorized",
        "ExpressionBeatAuthorized",
        ExpressionBeatAuthorizedPayload(
            acceptance_id=acceptance_id,
            proposal_id=proposal_id,
            expression_change_id="change:relationship-commitment-expression",
            beat=beat_material,
        ).model_dump(mode="json"),
    )
    authorized = _authorized_action(
        proposal_id=proposal_id,
        payload_hash=payload_hash,
        target=action_target,
    )
    action_event = _event(
        "event:relationship-commitment-action-authorized",
        "ActionAuthorized",
        {"action": authorized.model_dump(mode="json")},
    )
    receipt = ExecutionReceipt(
        receipt_id=RECEIPT_ID,
        result_id="result:relationship-commitment-expression",
        action_id=ACTION_ID,
        provider="qq",
        provider_ref="qq-message:relationship-commitment",
        source_event_id="provider-event:relationship-commitment",
        receipt_kind="terminal",
        observed_state="delivered",
        is_terminal=True,
        cost_actual=0,
        received_at=NOW,
        raw_payload_hash="raw:relationship-commitment",
    )
    receipt_event = _event(
        "event:relationship-commitment-receipt",
        "ExecutionReceiptRecorded",
        {"receipt": receipt.model_dump(mode="json")},
    )
    lease = ClaimLease(
        owner_id="action-pump:test",
        attempt_id="attempt:relationship-commitment-expression",
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
    )
    delivered = authorized.model_copy(update={"state": "delivered", "claim_lease": lease})
    plan = ExpressionPlanProjection(
        acceptance_id=acceptance_id,
        proposal_id=proposal_id,
        expression_change_id="change:relationship-commitment-expression",
        plan_id=PLAN_ID,
        event_ref=plan_event.event_id,
        event_payload_hash=plan_event.payload_hash,
        state="completed",
        history=(
            ExpressionPlanLifecycleEntry(
                state="authorized",
                event_ref=plan_event.event_id,
                event_payload_hash=plan_event.payload_hash,
            ),
            ExpressionPlanLifecycleEntry(
                state="completed",
                event_ref=receipt_event.event_id,
                event_payload_hash=receipt_event.payload_hash,
                receipt_id=RECEIPT_ID,
                terminal_action_state="delivered",
            ),
        ),
    )
    beat = ExpressionBeatProjection(
        acceptance_id=acceptance_id,
        proposal_id=proposal_id,
        expression_change_id="change:relationship-commitment-expression",
        plan_id=PLAN_ID,
        beat_id=BEAT_ID,
        payload_ref=authorized.payload_ref,
        payload_hash=payload_hash,
        action_id=ACTION_ID,
        cancel_policy="cancel-before-dispatch",
        reconsider_policy="reconsider-on-new-observation",
        merge_policy="never",
        event_ref=beat_event.event_id,
        event_payload_hash=beat_event.payload_hash,
        state="settled",
        history=(
            ExpressionBeatLifecycleEntry(
                state="authorized",
                event_ref=beat_event.event_id,
                event_payload_hash=beat_event.payload_hash,
            ),
            ExpressionBeatLifecycleEntry(
                state="settled",
                event_ref=receipt_event.event_id,
                event_payload_hash=receipt_event.payload_hash,
                receipt_id=RECEIPT_ID,
                terminal_action_state="delivered",
            ),
        ),
    )
    stored = StoredMessagePayloadProjection(
        acceptance_id=acceptance_id,
        proposal_id=proposal_id,
        payload_ref=authorized.payload_ref,
        payload_hash=payload_hash,
        text=text,
        content_type="text/plain",
        event_ref=stored_event.event_id,
        event_payload_hash=stored_event.payload_hash,
    )
    process = TriggerProcess(
        trigger_id=relationship_continuity_trigger_id(
            world_id=WORLD_ID,
            observation_event_id=source_event.event_id,
        ),
        trigger_ref=f"relationship-continuity:{source_event.event_id}",
        process_kind="relationship_deliberation",
        source_evidence_ref=source_event.event_id,
        state="claimed",
        claim_lease=ClaimLease(
            owner_id="worker:relationship",
            attempt_id="attempt:relationship",
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
        ),
        attempt_ids=("attempt:relationship",),
    )
    proof_events = (stored_event, plan_event, beat_event, action_event, receipt_event)
    start_revision = base.world_revision
    committed = tuple(
        CommittedWorldEventRef(
            event_id=event.event_id,
            event_type=event.event_type,
            world_revision=start_revision + index,
            payload_hash=event.payload_hash,
            logical_time=event.logical_time,
        )
        for index, event in enumerate(proof_events, start=1)
    )
    projection = base.model_copy(
        update={
            "world_revision": start_revision + len(proof_events),
            "ledger_sequence": base.ledger_sequence + len(proof_events),
            "committed_world_event_refs": (*base.committed_world_event_refs, *committed),
            "stored_message_payloads": (stored,),
            "expression_plans": (plan,),
            "expression_beats": (beat,),
            "actions": (delivered,),
            "pending_actions": (),
            "execution_receipts": (receipt,),
            "trigger_processes": (*base.trigger_processes, process),
        }
    )
    commits = {
        event.event_id: (
            event,
            CommitResult(
                world_revision=start_revision + index,
                deliberation_revision=base.deliberation_revision,
                ledger_sequence=base.ledger_sequence + index,
                event_ids=(event.event_id,),
            ),
        )
        for index, event in enumerate(proof_events, start=1)
    }
    return projection, commits


class _CompilerLedger:
    def __init__(self, delegate, *, audit_cursor: ProjectionCursor, current, proof_commits):
        self.world_id = delegate.world_id
        self._delegate = delegate
        self._audit_cursor = audit_cursor
        self._current = current
        self._proof_commits = proof_commits
        self.recorded: tuple[WorldEvent, ...] = ()

    def project(self):
        return self._current

    def project_at(self, cursor: ProjectionCursor):
        if cursor == self._audit_cursor:
            return self._delegate.project_at(cursor)
        if (
            cursor.world_revision == self._current.world_revision
            and cursor.deliberation_revision == self._current.deliberation_revision
            and cursor.ledger_sequence == self._current.ledger_sequence
        ):
            return self._current
        raise AssertionError(f"unexpected cursor: {cursor}")

    def lookup_event_commit(self, event_id: str):
        return self._proof_commits.get(event_id) or self._delegate.lookup_event_commit(event_id)

    def commit_at_cursor(self, events, *, expected_cursor, commit_id=None):
        del commit_id
        assert expected_cursor.world_revision == self._current.world_revision
        self.recorded = tuple(events)
        return CommitResult(
            world_revision=expected_cursor.world_revision,
            deliberation_revision=expected_cursor.deliberation_revision + 1,
            ledger_sequence=expected_cursor.ledger_sequence + len(self.recorded),
            event_ids=tuple(item.event_id for item in self.recorded),
        )

    def without_delivered_action(self) -> None:
        self._current = self._current.model_copy(
            update={"execution_receipts": (), "actions": ()}
        )


def _record_relationship_commitment_decision(
    ledger: WorldLedger,
    *,
    visible_text_span: str,
):
    source_event = ledger.lookup_event_commit("message-event:1")[0]
    evidence = ProposalEvidenceRef(
        ref_id=source_event.event_id,
        evidence_kind="committed_world_event",
        source_world_revision=ledger.lookup_event_commit(source_event.event_id)[
            1
        ].world_revision,
        immutable_hash="sha256:" + source_event.payload_hash,
    )
    proposal = DecisionProposal(
        proposal_id="proposal:inbound-relationship:1",
        trigger_ref=source_event.event_id,
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(evidence,),
        proposed_changes=(
            TypedChange(
                change_id="change:inbound-relationship-commitment:1",
                kind="relationship_commitment",
                target_id="relationship-commitment:user:test:friend",
                transition="commit",
                evidence_refs=(source_event.event_id,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="relationship_commitment.v1",
                    value={
                        "subject_ref": "user:test",
                        "target_stage": "friend",
                        "commitment_code": "mutual_friendship",
                        "persistence": "durable",
                        "visible_text_span": visible_text_span,
                    },
                ),
            ),
        ),
        action_intents=(),
        confidence=7600,
        brief_rationale="角色在本次互动里形成了自己的关系承诺。",
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
            source="test:relationship-commitment",
            trace_id="trace:relationship-commitment",
            causation_id=source_event.event_id,
            correlation_id="correlation:relationship-commitment",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
            expected_ledger_sequence=head.ledger_sequence,
        ),
    )
    return proposal, recorded.cursor, source_event


def _compiler_fixture(
    *,
    include_commitment: bool = True,
    text: str = TEXT,
    visible_text_span: str = SPAN,
    target_stage: str = "friend",
    subject_ref: str = "user:test",
    stored_event_text: str | None = None,
    delivery_target: str | None = None,
):
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    prepare_claimed_interaction(ledger, reply_target=delivery_target)
    if visible_text_span == SPAN:
        proposal, audit_cursor, source_event = _record_relationship_decision(
            ledger,
            include_signal=False,
            include_commitment=include_commitment,
            commitment_target_stage=target_stage,
            commitment_subject_ref=subject_ref,
        )
    else:
        if not include_commitment or target_stage != "friend" or subject_ref != "user:test":
            raise AssertionError("custom visible span fixture only supports one commitment")
        proposal, audit_cursor, source_event = (
            _record_relationship_commitment_decision(
                ledger,
                visible_text_span=visible_text_span,
            )
        )
    base = ledger.project_at(audit_cursor)
    current, commits = _proof_projection(
        base,
        proposal_id=proposal.proposal_id,
        source_event=source_event,
        text=text,
        stored_event_text=stored_event_text,
        action_target=delivery_target or "user:test",
    )
    wrapped = _CompilerLedger(
        ledger,
        audit_cursor=audit_cursor,
        current=current,
        proof_commits=commits,
    )
    current_cursor = ProjectionCursor(
        world_revision=current.world_revision,
        deliberation_revision=current.deliberation_revision,
        ledger_sequence=current.ledger_sequence,
    )
    return wrapped, proposal, audit_cursor, current_cursor


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _compiled_commitment_proposal_event(
    *, delivery_target: str = "user:test"
) -> tuple[_CompilerLedger, object, ProjectionCursor, WorldEvent]:
    ledger, source_proposal, audit_cursor, current_cursor = _compiler_fixture(
        delivery_target=delivery_target,
    )
    result = RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=current_cursor,
        proposal_id=source_proposal.proposal_id,
    )
    assert result.status == "candidate_recorded"
    assert len(ledger.recorded) == 1
    return ledger, source_proposal, audit_cursor, ledger.recorded[0]


def _reducer_state_with_delivery(
    ledger: _CompilerLedger,
    *,
    delivery_projection=None,
) -> ReducerState:
    current = delivery_projection or ledger._current
    source_state = ledger._delegate._state
    return source_state.model_copy(
        update={
            "committed_world_event_refs": current.committed_world_event_refs,
            "stored_message_payloads": current.stored_message_payloads,
            "expression_plans": current.expression_plans,
            "expression_beats": current.expression_beats,
            "actions": current.actions,
            "pending_actions": current.pending_actions,
            "execution_receipts": current.execution_receipts,
            "trigger_processes": current.trigger_processes,
        }
    )


def _rewrite_compiled_commitment_event(
    event: WorldEvent,
    payload: RelationshipCommitmentAcceptedPayload,
    *,
    suffix: str,
) -> WorldEvent:
    raw_payload = payload.model_dump(mode="json")
    raw_payload["accepted_change_hash"] = relationship_mutation_hash(raw_payload)
    rewritten_payload = RelationshipCommitmentAcceptedPayload.model_validate_json(
        _canonical_json(raw_payload)
    )
    proposal = RelationshipProposalProjection.model_validate_json(event.payload_json)
    rewritten_proposal = proposal.model_copy(
        update={
            "proposed_change_hash": rewritten_payload.accepted_change_hash,
            "proposed_mutation": RelationshipProposedMutation(
                event_type="RelationshipCommitmentAccepted",
                payload_json=_canonical_json(rewritten_payload.model_dump(mode="json")),
            ),
        }
    )
    return WorldEvent.from_payload(
        schema_version=event.schema_version,
        event_id=f"{event.event_id}:{suffix}",
        world_id=event.world_id,
        event_type=event.event_type,
        logical_time=event.logical_time,
        created_at=event.created_at,
        actor=event.actor,
        source=event.source,
        trace_id=event.trace_id,
        causation_id=event.causation_id,
        correlation_id=event.correlation_id,
        idempotency_key=f"{event.idempotency_key}:{suffix}",
        payload=rewritten_proposal.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "coordinate",
    ("target_stage", "commitment_code", "visible_text_span"),
)
def test_public_reducer_rejects_commitment_semantics_changed_from_typed_source(
    coordinate: str,
) -> None:
    ledger, _source, _audit_cursor, proposal_event = (
        _compiled_commitment_proposal_event()
    )
    proposal = RelationshipProposalProjection.model_validate_json(
        proposal_event.payload_json
    )
    payload = RelationshipCommitmentAcceptedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    commitment_update: dict[str, object] = {}
    payload_update: dict[str, object] = {}
    if coordinate == "target_stage":
        payload_update["stage_after"] = "acquaintance"
        commitment_update["committed_stage"] = "acquaintance"
    elif coordinate == "commitment_code":
        commitment_update["commitment_code"] = "forged_friendship_commitment"
    else:
        # Still exact-once in the delivered text, but not the span the role selected.
        commitment_update["visible_text_span"] = "好呀"
    crafted = payload.model_copy(
        update={
            **payload_update,
            "commitment": payload.commitment.model_copy(update=commitment_update),
        }
    )
    crafted_event = _rewrite_compiled_commitment_event(
        proposal_event,
        crafted,
        suffix=f"forged-{coordinate}",
    )

    with pytest.raises(ValueError):
        reduce_event(_reducer_state_with_delivery(ledger), crafted_event)


def test_public_reducer_rejects_overlapping_delivered_visible_span() -> None:
    ledger, source_proposal, audit_cursor, _current_cursor = _compiler_fixture(
        text="aaa",
        visible_text_span="aa",
    )
    source_audit = next(
        item
        for item in ledger._delegate.project_at(audit_cursor).proposal_audits
        if item.proposal_id == source_proposal.proposal_id
    )
    source_change = source_proposal.proposed_changes[0]
    _baseline_ledger, _source, _baseline_cursor, proposal_event = (
        _compiled_commitment_proposal_event()
    )
    proof_event_hashes = {
        item.event_id: item.payload_hash
        for item in ledger._current.committed_world_event_refs
    }
    proposal = RelationshipProposalProjection.model_validate_json(
        proposal_event.payload_json
    )
    payload = RelationshipCommitmentAcceptedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    crafted_proof = payload.commitment.delivery_proof.model_copy(
        update={
            "message_payload_hash": "sha256:"
            + hashlib.sha256(b"aaa").hexdigest(),
            "stored_payload_event_hash": proof_event_hashes[
                "event:relationship-commitment-message-stored"
            ],
            "beat_event_payload_hash": proof_event_hashes[
                "event:relationship-commitment-beat-authorized"
            ],
            "action_event_payload_hash": proof_event_hashes[
                "event:relationship-commitment-action-authorized"
            ],
        }
    )
    crafted = payload.model_copy(
        update={
            "commitment": payload.commitment.model_copy(
                update={
                    "visible_text_span": "aa",
                    "delivery_proof": crafted_proof,
                }
            )
        }
    )
    crafted_event = _rewrite_compiled_commitment_event(
        proposal_event,
        crafted,
        suffix="overlapping-visible-span",
    )
    crafted_proposal = RelationshipProposalProjection.model_validate_json(
        crafted_event.payload_json
    )
    assert crafted_proposal.source_audit is not None
    crafted_proposal = crafted_proposal.model_copy(
        update={
            "source_audit": crafted_proposal.source_audit.model_copy(
                update={
                    "proposal_event_ref": source_audit.event_ref,
                    "proposal_event_payload_hash": source_audit.event_payload_hash,
                    "model_result_ref": source_audit.model_result_ref,
                    "capsule_id": source_audit.capsule_id,
                    "change_id": source_change.change_id,
                    "change_payload_hash": source_change.payload.payload_hash,
                }
            )
        }
    )
    crafted_event = WorldEvent.from_payload(
        schema_version=crafted_event.schema_version,
        event_id=f"{crafted_event.event_id}:source-bound",
        world_id=crafted_event.world_id,
        event_type=crafted_event.event_type,
        logical_time=crafted_event.logical_time,
        created_at=crafted_event.created_at,
        actor=crafted_event.actor,
        source=crafted_event.source,
        trace_id=crafted_event.trace_id,
        causation_id=crafted_event.causation_id,
        correlation_id=crafted_event.correlation_id,
        idempotency_key=f"{crafted_event.idempotency_key}:source-bound",
        payload=crafted_proposal.model_dump(mode="json"),
    )

    with pytest.raises(
        ValueError,
        match="relationship commitment delivered expression is not terminal",
    ):
        reduce_event(
            _reducer_state_with_delivery(ledger),
            crafted_event,
        )


def test_public_reducer_rejects_commitment_subject_not_bound_to_observation_actor() -> None:
    ledger, _source, _audit_cursor, proposal_event = (
        _compiled_commitment_proposal_event()
    )
    proposal = RelationshipProposalProjection.model_validate_json(
        proposal_event.payload_json
    )
    payload = RelationshipCommitmentAcceptedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    forged_subject = "user:forged"
    forged_relationship_id = relationship_primary_id(subject_ref=forged_subject)
    crafted = payload.model_copy(
        update={
            "relationship_id": forged_relationship_id,
            "subject_ref": forged_subject,
            "commitment": payload.commitment.model_copy(
                update={
                    "commitment_id": "relationship-commitment:forged-subject",
                    "relationship_id": forged_relationship_id,
                    "subject_ref": forged_subject,
                }
            ),
        }
    )
    crafted_event = _rewrite_compiled_commitment_event(
        proposal_event,
        crafted,
        suffix="forged-subject",
    )

    with pytest.raises(ValueError):
        reduce_event(_reducer_state_with_delivery(ledger), crafted_event)


def test_public_reducer_rejects_commitment_action_target_not_bound_to_observation_reply_target() -> None:
    expected_target = "conversation:qq:c2c:expected"
    forged_target = "conversation:qq:c2c:forged"
    ledger, source_proposal, audit_cursor, proposal_event = (
        _compiled_commitment_proposal_event(delivery_target=expected_target)
    )
    base = ledger._delegate.project_at(audit_cursor)
    source_event = ledger._delegate.lookup_event_commit("message-event:1")[0]
    forged_delivery, _forged_commits = _proof_projection(
        base,
        proposal_id=source_proposal.proposal_id,
        source_event=source_event,
        action_target=forged_target,
    )
    forged_action_event_hash = next(
        item.payload_hash
        for item in forged_delivery.committed_world_event_refs
        if item.event_id == "event:relationship-commitment-action-authorized"
    )
    proposal = RelationshipProposalProjection.model_validate_json(
        proposal_event.payload_json
    )
    payload = RelationshipCommitmentAcceptedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    crafted_proof = payload.commitment.delivery_proof.model_copy(
        update={
            "action_target_ref": forged_target,
            "action_event_payload_hash": forged_action_event_hash,
        }
    )
    crafted = payload.model_copy(
        update={
            "commitment": payload.commitment.model_copy(
                update={"delivery_proof": crafted_proof}
            )
        }
    )
    crafted_event = _rewrite_compiled_commitment_event(
        proposal_event,
        crafted,
        suffix="forged-action-target",
    )

    with pytest.raises(ValueError):
        reduce_event(
            _reducer_state_with_delivery(
                ledger,
                delivery_projection=forged_delivery,
            ),
            crafted_event,
        )


def test_record_commitment_rebased_requires_delivered_expression_and_records_typed_candidate() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture()

    result = RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=current_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "candidate_recorded"
    assert len(ledger.recorded) == 1
    typed = RelationshipProposalProjection.model_validate_json(ledger.recorded[0].payload_json)
    assert typed.transition_kind == "commitment"
    mutation = RelationshipCommitmentAcceptedPayload.model_validate_json(
        typed.proposed_mutation.payload_json
    )
    assert mutation.subject_ref == "user:test"
    assert mutation.stage_before == "stranger"
    assert mutation.stage_after == "friend"
    assert mutation.expected_entity_revision == 0
    assert mutation.commitment.commitment_code == "mutual_friendship"
    assert mutation.commitment.visible_text_span == SPAN
    assert mutation.commitment.delivery_proof.expression_proposal_id == proposal.proposal_id
    assert mutation.commitment.delivery_proof.expression_plan_id == PLAN_ID
    assert mutation.commitment.delivery_proof.expression_beat_id == BEAT_ID
    assert mutation.commitment.delivery_proof.action_id == ACTION_ID
    assert mutation.commitment.delivery_proof.receipt_id == RECEIPT_ID
    assert mutation.commitment.origin.accepted_event_ref == (
        relationship_commitment_mutation_event_id(
            world_id=WORLD_ID,
            proposal_id=typed.proposal_id,
            transition_id=typed.transition_id,
        )
    )


def test_record_commitment_rebased_binds_qq_target_separately_from_subject() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture(
        delivery_target="conversation:qq:c2c:openid"
    )

    result = RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=current_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "candidate_recorded"
    typed = RelationshipProposalProjection.model_validate_json(ledger.recorded[0].payload_json)
    mutation = RelationshipCommitmentAcceptedPayload.model_validate_json(
        typed.proposed_mutation.payload_json
    )
    assert mutation.subject_ref == "user:test"
    assert mutation.commitment.delivery_proof.action_id == ACTION_ID
    assert mutation.commitment.delivery_proof.action_target_ref == (
        "conversation:qq:c2c:openid"
    )


def test_record_commitment_rebased_recovers_accepted_descendant_effect_once() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture()
    compiler = RelationshipProposalCompiler(ledger=ledger)
    first = compiler.record_commitment_rebased(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=current_cursor,
        proposal_id=proposal.proposal_id,
    )
    assert first.commit is not None
    proposal_event = ledger.recorded[0]
    typed = RelationshipProposalProjection.model_validate_json(proposal_event.payload_json)
    mutation = RelationshipCommitmentAcceptedPayload.model_validate_json(
        typed.proposed_mutation.payload_json
    )
    acceptance_event = _event(
        "event:relationship-commitment-accepted",
        "AcceptanceRecorded",
        {"proposal_event_ref": proposal_event.event_id},
    )
    acceptance_commit = CommitResult(
        world_revision=current_cursor.world_revision,
        deliberation_revision=current_cursor.deliberation_revision + 2,
        ledger_sequence=current_cursor.ledger_sequence + 2,
        event_ids=(acceptance_event.event_id,),
    )
    ledger._proof_commits.update(
        {
            proposal_event.event_id: (proposal_event, first.commit),
            acceptance_event.event_id: (acceptance_event, acceptance_commit),
        }
    )
    ledger._current = ledger._current.model_copy(
        update={
            "deliberation_revision": acceptance_commit.deliberation_revision,
            "ledger_sequence": acceptance_commit.ledger_sequence,
            "acceptance_decisions": (
                AcceptanceDecisionRef(
                    proposal_id=typed.proposal_id,
                    evaluated_world_revision=typed.evaluated_world_revision,
                    acceptance_id=mutation.acceptance_id,
                    status="accepted",
                    accepted_change_id=typed.change_id,
                    accepted_change_hash=typed.proposed_change_hash,
                    manifest_version="relationship-commitment-acceptance.1",
                    manifest_hash="a" * 64,
                    acceptance_event_ref=acceptance_event.event_id,
                    acceptance_event_payload_hash=acceptance_event.payload_hash,
                ),
            ),
        }
    )
    ledger.recorded = ()
    replay_cursor = ProjectionCursor(
        world_revision=ledger._current.world_revision,
        deliberation_revision=ledger._current.deliberation_revision,
        ledger_sequence=ledger._current.ledger_sequence,
    )

    recovered = compiler.record_commitment_rebased(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=replay_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert recovered.typed_proposal_id == typed.proposal_id
    assert recovered.commit == first.commit
    assert ledger.recorded == ()


def test_record_commitment_rebased_does_nothing_without_typed_commitment() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture(
        include_commitment=False
    )

    result = RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
        world_id=WORLD_ID,
        audit_cursor=audit_cursor,
        current_cursor=current_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "no_change"
    assert ledger.recorded == ()


def test_record_commitment_rebased_fails_closed_without_delivered_receipt() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture()
    ledger.without_delivered_action()

    with pytest.raises(
        RelationshipProposalCompilerError,
        match="commitment_expression_not_delivered",
    ):
        RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal.proposal_id,
        )


def test_record_commitment_rebased_requires_visible_span_exactly_once() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture(
        text=f"{SPAN}，是真的，{SPAN}。"
    )

    with pytest.raises(
        RelationshipProposalCompilerError,
        match="commitment_visible_span_not_exact",
    ):
        RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal.proposal_id,
        )


def test_record_commitment_rebased_counts_overlapping_visible_span_occurrences() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture(
        text="aaa",
        visible_text_span="aa",
    )

    with pytest.raises(
        RelationshipProposalCompilerError,
        match="commitment_visible_span_not_exact",
    ):
        RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal.proposal_id,
        )


def test_record_commitment_rebased_rejects_uninstalled_stage_skip() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture(
        target_stage="close_friend"
    )

    with pytest.raises(
        RelationshipProposalCompilerError,
        match="commitment_stage_transition_not_installed",
    ):
        RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal.proposal_id,
        )


def test_record_commitment_rebased_rejects_unbound_subject() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture(
        subject_ref="user:forged"
    )

    with pytest.raises(
        RelationshipProposalCompilerError,
        match="subject_not_bound_to_source",
    ):
        RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal.proposal_id,
        )


def test_record_commitment_rebased_rejects_stored_text_not_bound_to_event() -> None:
    ledger, proposal, audit_cursor, current_cursor = _compiler_fixture(
        stored_event_text="这条持久事件里没有那句关系承诺。"
    )

    with pytest.raises(
        RelationshipProposalCompilerError,
        match="commitment_expression_authority_mismatch",
    ):
        RelationshipProposalCompiler(ledger=ledger).record_commitment_rebased(
            world_id=WORLD_ID,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal.proposal_id,
        )
