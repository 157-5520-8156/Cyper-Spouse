from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.interaction_act_acceptance_runtime import (
    InteractionActAtomicRecorder,
    InteractionActProposalAuthorityReader,
)
from companion_daemon.world_v2.interaction_act_events import (
    InteractionActProposalRecordedPayload,
    canonical_interaction_act_change_hash,
    canonical_interaction_act_mutation_hash,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.interaction_act_proposal_compiler import (
    InteractionActProposalCompiler,
    InteractionActProposalCompilerError,
)
from companion_daemon.world_v2.interaction_act_reducers import reduce_interaction_act
from companion_daemon.world_v2.interaction_act_runtime import (
    InteractionActRoleOutput,
    ObservedInteractionActSource,
    interaction_act_conversation_ref,
    materialize_interaction_act_mutation,
)
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
from companion_daemon.world_v2.schemas import (
    Action,
    ClaimLease,
    CommitResult,
    CommittedWorldEventRef,
    ExecutionReceipt,
    ExpressionBeatLifecycleEntry,
    ExpressionBeatProjection,
    ExpressionPlanLifecycleEntry,
    ExpressionPlanProjection,
    Observation,
    ProjectionCursor,
    StoredMessagePayloadProjection,
    WorldEvent,
)

from test_proposal_audit import _digest, _result


NOW = datetime(2026, 8, 11, 14, 0, tzinfo=UTC)
WORLD = "world:interaction-act-compiler"
OBSERVATION_EVENT = "event:observation:interaction-act"
USER = "user:primary"
COMPANION = "actor:companion"
MESSAGE = "我有一本旧书，下次见面带给你。"


def _event(*, text: str = MESSAGE) -> WorldEvent:
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:interaction-act",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-act-compiler",
        causation_id="qq:primary:message:1",
        correlation_id="qq:primary:message:1",
        source="platform:qq",
        source_event_id="qq:primary:message:1",
        actor=USER,
        channel="qq",
        payload_ref="ingress:qq:primary:message:1",
        payload_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        received_at=NOW,
    )
    payload = observation.model_dump(mode="json")
    idempotency_key = domain_idempotency_key(
        event_type="ObservationRecorded",
        world_id=WORLD,
        payload=payload,
    )
    assert idempotency_key is not None
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=OBSERVATION_EVENT,
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=USER,
        source="platform:qq",
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )


def _record_decision(
    ledger: WorldLedger,
    *,
    include_act: bool = True,
    source_text: str = MESSAGE,
    source_text_span: str = "下次见面带给你",
    operation: str = "declare",
    status_code: str = "等待下轮继续",
    interaction_act_ref: str | None = None,
    act_kind: str = "提出后续交接",
    subject_ref: str = USER,
    counterparty_refs: tuple[str, ...] = (COMPANION,),
    object_ref: str | None = None,
    object_label: str | None = "一本旧书",
    source_scope: str = "current_message",
    target_id: str = "interaction-act:candidate:opening",
    expected_entity_revision: int | None = 0,
) -> tuple[DecisionProposal, ProjectionCursor, WorldEvent]:
    source_event = _event(text=source_text)
    head = ledger.project()
    ledger.commit(
        [source_event],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    source_commit = ledger.lookup_event_commit(source_event.event_id)
    assert source_commit is not None
    evidence = ProposalEvidenceRef(
        ref_id=source_event.event_id,
        evidence_kind="committed_world_event",
        source_world_revision=source_commit[1].world_revision,
        immutable_hash="sha256:" + source_event.payload_hash,
    )
    changes: tuple[TypedChange, ...] = ()
    if include_act:
        changes = (
            TypedChange(
                change_id="change:interaction-act:opening",
                kind="interaction_act",
                target_id=target_id,
                expected_entity_revision=expected_entity_revision,
                transition=operation,
                evidence_refs=(source_event.event_id,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="interaction_act.v1",
                    value={
                        "interaction_act_ref": interaction_act_ref,
                        "act_kind": act_kind,
                        "subject_ref": subject_ref,
                        "counterparty_refs": list(counterparty_refs),
                        "object_ref": object_ref,
                        "object_label": object_label,
                        "source_scope": source_scope,
                        "source_text_span": source_text_span,
                        "status_code": status_code,
                    },
                ),
            ),
        )
    proposal = DecisionProposal(
        proposal_id="proposal:interaction-act:opening",
        trigger_ref=source_event.event_id,
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(evidence,),
        proposed_changes=changes,
        action_intents=(),
        confidence=8000,
        brief_rationale="角色明确识别了一个需要跨轮保留的互动动作。",
        behavior_tendency="按自己的判断回应",
        stance="清楚",
        display_strategy="不把外部结果当成已完成",
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
            world_id=WORLD,
            trigger_ref=source_event.event_id,
            logical_time=NOW,
            created_at=NOW,
            actor=COMPANION,
            source="test:character-interior",
            trace_id="trace:interaction-act-compiler",
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
            expected_ledger_sequence=head.ledger_sequence,
        ),
    )
    return proposal, recorded.cursor, source_event


class _CapturingLedger:
    def __init__(self, delegate: WorldLedger) -> None:
        self.world_id = delegate.world_id
        self._delegate = delegate
        self.recorded: tuple[WorldEvent, ...] = ()

    @property
    def delegate(self) -> WorldLedger:
        return self._delegate

    def project_at(self, cursor: ProjectionCursor):
        return self._delegate.project_at(cursor)

    def lookup_event_commit(self, event_id: str):
        return self._delegate.lookup_event_commit(event_id)

    def commit_at_cursor(self, events, *, expected_cursor, commit_id=None):
        del commit_id
        self.recorded = tuple(events)
        return CommitResult(
            world_revision=expected_cursor.world_revision,
            deliberation_revision=expected_cursor.deliberation_revision + 1,
            ledger_sequence=expected_cursor.ledger_sequence + len(self.recorded),
            event_ids=tuple(item.event_id for item in self.recorded),
        )


class _ProofLedger:
    def __init__(
        self,
        delegate: WorldLedger,
        *,
        audit_cursor: ProjectionCursor,
        current,
        proof_commits: dict[str, tuple[WorldEvent, CommitResult]] | None = None,
    ) -> None:
        self.world_id = delegate.world_id
        self._delegate = delegate
        self._audit_cursor = audit_cursor
        self._current = current
        self._proof_commits = proof_commits or {}
        self.recorded: tuple[WorldEvent, ...] = ()

    def project_at(self, cursor: ProjectionCursor):
        current_cursor = ProjectionCursor(
            world_revision=self._current.world_revision,
            deliberation_revision=self._current.deliberation_revision,
            ledger_sequence=self._current.ledger_sequence,
        )
        if cursor == current_cursor:
            return self._current
        if cursor == self._audit_cursor:
            return self._delegate.project_at(cursor)
        raise AssertionError(f"unexpected cursor: {cursor}")

    def lookup_event_commit(self, event_id: str):
        return self._proof_commits.get(event_id) or self._delegate.lookup_event_commit(
            event_id
        )

    def commit_at_cursor(self, events, *, expected_cursor, commit_id=None):
        del commit_id
        self.recorded = tuple(events)
        return CommitResult(
            world_revision=expected_cursor.world_revision,
            deliberation_revision=expected_cursor.deliberation_revision + 1,
            ledger_sequence=expected_cursor.ledger_sequence + len(self.recorded),
            event_ids=tuple(item.event_id for item in self.recorded),
        )


def _opening_act(*, subject_ref: str, counterparty_ref: str):
    conversation_ref = interaction_act_conversation_ref(
        world_id=WORLD,
        channel="qq",
        participant_refs=(subject_ref, counterparty_ref),
    )
    source = ObservedInteractionActSource(
        world_id=WORLD,
        conversation_ref=conversation_ref,
        source_event_ref="event:opening-authority:" + subject_ref,
        source_world_revision=1,
        source_payload_hash="9" * 64,
        source_actor_ref=subject_ref,
        source_text="我们之后继续这个安排。",
    )
    opening = materialize_interaction_act_mutation(
        authored=InteractionActRoleOutput(
            contract="interaction-act-role-output.2",
            source_text_span="继续这个安排",
            operation="declare",
            status_code="等待下轮继续",
            interaction_act_ref=None,
            act_kind="协调后续安排",
            subject_ref=subject_ref,
            counterparty_refs=(counterparty_ref,),
            object_ref=None,
            object_label=None,
        ),
        source=source,
        current=(),
        logical_time=NOW,
    )
    current, _ = reduce_interaction_act(
        (),
        (),
        opening,
        logical_time=NOW,
        accepted_event_ref="event:interaction-act-transition-accepted:opening",
    )
    return current[0]


def _proof_event(*, event_id: str, event_type: str, payload: dict[str, object]):
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="test:interaction-act-proof",
        source="test:interaction-act-proof",
        trace_id="trace:interaction-act-proof",
        causation_id=OBSERVATION_EVENT,
        correlation_id="message:proof",
        idempotency_key="identity:" + event_id,
        payload=payload,
    )


def _delivered_proof_projection(
    base,
    *,
    proposal_id: str,
    current_act,
    text: str,
):
    acceptance_id = "acceptance:interaction-act-expression"
    plan_id = "plan:interaction-act-expression"
    beat_id = "beat:interaction-act-expression"
    action_id = "action:interaction-act-expression"
    receipt_id = "receipt:interaction-act-expression"
    payload_ref = "payload:interaction-act-expression"
    payload_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    message = MessagePayloadMaterial(
        payload_ref=payload_ref,
        payload_hash=payload_hash,
        text=text,
        content_type="text/plain",
        storage_kind="inline_text",
        privacy_class="private",
    )
    stored_event = _proof_event(
        event_id="event:interaction-act-message-stored",
        event_type="MessagePayloadStored",
        payload=MessagePayloadStoredPayload(
            acceptance_id=acceptance_id,
            proposal_id=proposal_id,
            message=message,
        ).model_dump(mode="json"),
    )
    plan_event = _proof_event(
        event_id="event:interaction-act-plan-accepted",
        event_type="ExpressionPlanAccepted",
        payload=ExpressionPlanAcceptedPayload(
            acceptance_id=acceptance_id,
            proposal_id=proposal_id,
            expression_change_id="change:expression",
            plan_id=plan_id,
        ).model_dump(mode="json"),
    )
    beat_material = ExpressionBeatMaterial(
        plan_id=plan_id,
        beat_id=beat_id,
        payload=message,
        cancel_policy="cancel-before-dispatch",
        reconsider_policy="reconsider-on-new-observation",
        merge_policy="never",
    )
    beat_event = _proof_event(
        event_id="event:interaction-act-beat-authorized",
        event_type="ExpressionBeatAuthorized",
        payload=ExpressionBeatAuthorizedPayload(
            acceptance_id=acceptance_id,
            proposal_id=proposal_id,
            expression_change_id="change:expression",
            beat=beat_material,
        ).model_dump(mode="json"),
    )
    authorized = Action(
        schema_version="world-v2.1",
        action_id=action_id,
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-act-proof",
        causation_id=proposal_id,
        correlation_id="message:proof",
        kind="reply",
        layer="external_action",
        intent_ref="intent:interaction-act-expression",
        actor=COMPANION,
        target=USER,
        payload_ref=payload_ref,
        payload_hash=payload_hash,
        expression_plan_id=plan_id,
        expression_beat_id=beat_id,
        idempotency_key=action_id,
        budget_reservation_id="reservation:interaction-act-expression",
        state="authorized",
        recovery_policy="effect_once",
    )
    action_event = _proof_event(
        event_id="event:interaction-act-action-authorized",
        event_type="ActionAuthorized",
        payload={"action": authorized.model_dump(mode="json")},
    )
    receipt = ExecutionReceipt(
        receipt_id=receipt_id,
        result_id="result:interaction-act-expression",
        action_id=action_id,
        provider="qq",
        provider_ref="qq-message:interaction-act",
        source_event_id="provider-event:interaction-act",
        receipt_kind="terminal",
        observed_state="delivered",
        is_terminal=True,
        cost_actual=0,
        received_at=NOW,
        raw_payload_hash="raw:interaction-act",
    )
    receipt_event = _proof_event(
        event_id="event:interaction-act-receipt",
        event_type="ExecutionReceiptRecorded",
        payload={"receipt": receipt.model_dump(mode="json")},
    )
    lease = ClaimLease(
        owner_id="action-pump:test",
        attempt_id="attempt:interaction-act-expression",
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
    )
    delivered = authorized.model_copy(update={"state": "delivered", "claim_lease": lease})
    plan = ExpressionPlanProjection(
        acceptance_id=acceptance_id,
        proposal_id=proposal_id,
        expression_change_id="change:expression",
        plan_id=plan_id,
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
                receipt_id=receipt_id,
                terminal_action_state="delivered",
            ),
        ),
    )
    beat = ExpressionBeatProjection(
        acceptance_id=acceptance_id,
        proposal_id=proposal_id,
        expression_change_id="change:expression",
        plan_id=plan_id,
        beat_id=beat_id,
        payload_ref=payload_ref,
        payload_hash=payload_hash,
        action_id=action_id,
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
                receipt_id=receipt_id,
                terminal_action_state="delivered",
            ),
        ),
    )
    stored = StoredMessagePayloadProjection(
        acceptance_id=acceptance_id,
        proposal_id=proposal_id,
        payload_ref=payload_ref,
        payload_hash=payload_hash,
        text=text,
        content_type="text/plain",
        event_ref=stored_event.event_id,
        event_payload_hash=stored_event.payload_hash,
    )
    proof_events = (stored_event, plan_event, beat_event, action_event, receipt_event)
    committed_refs = tuple(
        CommittedWorldEventRef(
            event_id=event.event_id,
            event_type=event.event_type,
            world_revision=base.world_revision + index,
            payload_hash=event.payload_hash,
            logical_time=event.logical_time,
        )
        for index, event in enumerate(proof_events, start=1)
    )
    projection = base.model_copy(
        update={
            "world_revision": base.world_revision + len(proof_events),
            "ledger_sequence": base.ledger_sequence + len(proof_events),
            "committed_world_event_refs": (
                *base.committed_world_event_refs,
                *committed_refs,
            ),
            "interaction_acts": (current_act,),
            "stored_message_payloads": (stored,),
            "expression_plans": (plan,),
            "expression_beats": (beat,),
            "actions": (delivered,),
            "pending_actions": (),
            "execution_receipts": (receipt,),
        }
    )
    commits = {
        event.event_id: (
            event,
            CommitResult(
                world_revision=base.world_revision + index,
                deliberation_revision=base.deliberation_revision,
                ledger_sequence=base.ledger_sequence + index,
                event_ids=(event.event_id,),
            ),
        )
        for index, event in enumerate(proof_events, start=1)
    }
    return projection, commits


def _fixture(
    *,
    include_act: bool = True,
    source_text: str = MESSAGE,
    source_text_span: str = "下次见面带给你",
):
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    proposal, audit_cursor, source_event = _record_decision(
        ledger,
        include_act=include_act,
        source_text=source_text,
        source_text_span=source_text_span,
    )
    wrapped = _CapturingLedger(ledger)
    return wrapped, proposal, audit_cursor, source_event


def test_observed_role_act_compiles_generic_subject_object_and_status_without_outcome() -> None:
    ledger, proposal, audit_cursor, source_event = _fixture()

    result = InteractionActProposalCompiler(ledger=ledger).record_rebased(
        world_id=WORLD,
        audit_cursor=audit_cursor,
        current_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "candidate_recorded"
    assert len(ledger.recorded) == 1
    assert ledger.recorded[0].event_type == "InteractionActProposalRecorded"
    compiled = InteractionActProposalRecordedPayload.model_validate_json(
        ledger.recorded[0].payload_json,
        strict=True,
    )
    mutation = compiled.mutation
    assert compiled.proposal_id.startswith("proposal:interaction-act-compiled:")
    assert result.source_proposal_id == proposal.proposal_id
    assert result.typed_proposal_id == compiled.proposal_id
    assert compiled.proposal_hash == proposal.proposal_hash
    assert compiled.change_id == "change:interaction-act:opening"
    assert compiled.accepted_change_hash == canonical_interaction_act_change_hash(
        proposal.proposed_changes[0]
    )
    assert compiled.observed_source_text == MESSAGE
    assert mutation.operation == "declare"
    assert mutation.act_after.subject_ref == USER
    assert mutation.act_after.counterparty_refs == (COMPANION,)
    assert mutation.act_after.act_kind == "提出后续交接"
    assert mutation.act_after.participant_statuses[0].actor_ref == USER
    assert mutation.act_after.participant_statuses[0].status_code == "等待下轮继续"
    assert mutation.act_after.external_outcome == "not_established"
    assert mutation.act_after.object_descriptor is not None
    assert mutation.act_after.object_descriptor.object_label == "一本旧书"
    assert mutation.source_ref.source_event_ref == source_event.event_id
    assert mutation.source_ref.source_actor_ref == USER


def test_change_hash_binds_target_transition_and_evidence_coordinates() -> None:
    _, proposal, _, _ = _fixture()
    change = proposal.proposed_changes[0]
    expected = canonical_interaction_act_change_hash(change)

    assert canonical_interaction_act_change_hash(
        change.model_copy(update={"target_id": "interaction-act:candidate:forged"})
    ) != expected
    assert canonical_interaction_act_change_hash(
        change.model_copy(update={"transition": "revise"})
    ) != expected
    assert canonical_interaction_act_change_hash(
        change.model_copy(update={"evidence_refs": ("event:forged",)})
    ) != expected


def test_candidate_commits_through_real_world_ledger_domain_identity() -> None:
    capturing, proposal, audit_cursor, _ = _fixture()

    result = InteractionActProposalCompiler(ledger=capturing.delegate).record_rebased(
        world_id=WORLD,
        audit_cursor=audit_cursor,
        current_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "candidate_recorded"
    assert result.typed_proposal_event_ref is not None
    located = capturing.delegate.lookup_event_commit(result.typed_proposal_event_ref)
    assert located is not None
    event, _ = located
    expected_key = domain_idempotency_key(
        event_type=event.event_type,
        world_id=event.world_id,
        payload=event.payload(),
    )
    assert expected_key is not None
    assert event.idempotency_key == expected_key
    projection = capturing.delegate.project()
    assert len(projection.interaction_act_proposals) == 1
    assert projection.interaction_act_proposals[0].proposal_id == result.typed_proposal_id


def test_real_ledger_pins_compiled_proposal_and_commits_accepted_transition() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD,
        accepted_batch_issuer=issuer,
    )
    proposal, audit_cursor, _ = _record_decision(ledger)
    compiled = InteractionActProposalCompiler(ledger=ledger).record_rebased(
        world_id=WORLD,
        audit_cursor=audit_cursor,
        current_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )
    assert compiled.typed_proposal_event_ref is not None
    assert compiled.acceptance_cursor is not None
    reader = InteractionActProposalAuthorityReader(ledger=ledger)
    handle = reader.pin(
        world_id=WORLD,
        cursor=compiled.acceptance_cursor,
        proposal_event_ref=compiled.typed_proposal_event_ref,
    )
    batch = InteractionActAtomicRecorder(
        proposal_reader=reader,
        batch_issuer=issuer,
    ).prepare_batch(
        handle=handle,
        actor="worker:interaction-act-acceptance",
        source="test:interaction-act-vertical",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-act-compiler",
        correlation_id="qq:primary:message:1",
    )

    commit = ledger.commit_accepted(
        batch,
        expected_cursor=compiled.acceptance_cursor,
    )

    assert len(commit.event_ids) == 2
    projected = ledger.project()
    assert len(projected.interaction_acts) == 1
    assert projected.interaction_acts[0].participant_statuses[0].status_code == (
        "等待下轮继续"
    )
    assert projected.interaction_act_proposals == ()


@pytest.mark.parametrize(
    "forgery",
    (
        "observed_source_text",
        "conversation_ref",
        "object_ref",
        "interaction_act_id",
        "transition_id",
        "typed_proposal_id",
    ),
)
def test_direct_forged_typed_proposal_cannot_bypass_compiler_identity_closure(
    forgery: str,
) -> None:
    capturing, proposal, audit_cursor, _ = _fixture()
    InteractionActProposalCompiler(ledger=capturing).record_rebased(
        world_id=WORLD,
        audit_cursor=audit_cursor,
        current_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )
    original_event = capturing.recorded[0]
    typed = InteractionActProposalRecordedPayload.model_validate_json(
        original_event.payload_json,
        strict=True,
    )
    mutation = typed.mutation
    if forgery == "observed_source_text":
        typed = typed.model_copy(
            update={"observed_source_text": "伪造消息里也有下次见面带给你"}
        )
    elif forgery == "typed_proposal_id":
        typed = typed.model_copy(
            update={"proposal_id": "proposal:interaction-act:direct-forgery"}
        )
    else:
        after = mutation.act_after
        if forgery == "conversation_ref":
            after = after.model_copy(
                update={
                    "conversation_ref": "conversation:interaction-act:sha256:"
                    + "3" * 64
                }
            )
        elif forgery == "object_ref":
            assert after.object_descriptor is not None
            after = after.model_copy(
                update={
                    "object_descriptor": after.object_descriptor.model_copy(
                        update={"object_ref": "interaction-object:sha256:" + "0" * 64}
                    )
                }
            )
        elif forgery == "interaction_act_id":
            after = after.model_copy(
                update={"interaction_act_id": "interaction-act:sha256:" + "1" * 64}
            )
        else:
            forged_transition_id = "interaction-act-transition:sha256:" + "2" * 64
            after = after.model_copy(
                update={"origin_transition_id": forged_transition_id}
            )
            mutation = mutation.model_copy(
                update={"transition_id": forged_transition_id}
            )
        mutation = mutation.model_copy(update={"act_after": after})
        typed = typed.model_copy(
            update={
                "mutation": mutation,
                "mutation_payload_hash": canonical_interaction_act_mutation_hash(
                    mutation
                ),
            }
        )
    payload = typed.model_dump(mode="json")
    identity = domain_idempotency_key(
        event_type="InteractionActProposalRecorded",
        world_id=WORLD,
        payload=payload,
    )
    assert identity is not None
    forged_event = WorldEvent.from_payload(
        schema_version=original_event.schema_version,
        event_id=f"event:interaction-act-proposal:direct-forgery:{forgery}",
        world_id=original_event.world_id,
        event_type=original_event.event_type,
        logical_time=original_event.logical_time,
        created_at=original_event.created_at,
        actor=original_event.actor,
        source=original_event.source,
        trace_id=original_event.trace_id,
        causation_id=original_event.causation_id,
        correlation_id=original_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )

    with pytest.raises(ValueError, match="interaction act"):
        capturing.delegate.commit_at_cursor(
            (forged_event,),
            expected_cursor=audit_cursor,
        )


def test_observed_revision_binds_current_head_and_updates_only_source_actor_status() -> None:
    current_act = _opening_act(subject_ref=COMPANION, counterparty_ref=USER)
    issuer = AcceptedLedgerBatchIssuer()
    delegate = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    source_text = "我现在确认这个安排。"
    proposal, audit_cursor, _ = _record_decision(
        delegate,
        source_text=source_text,
        source_text_span=source_text,
        operation="revise",
        status_code="我会在下一轮继续",
        interaction_act_ref=current_act.interaction_act_id,
        act_kind=current_act.act_kind,
        subject_ref=current_act.subject_ref,
        counterparty_refs=current_act.counterparty_refs,
        object_ref=None,
        object_label=None,
        target_id=current_act.interaction_act_id,
        expected_entity_revision=None,
    )
    projection = delegate.project_at(audit_cursor).model_copy(
        update={"interaction_acts": (current_act,)}
    )
    ledger = _ProofLedger(
        delegate,
        audit_cursor=audit_cursor,
        current=projection,
    )

    result = InteractionActProposalCompiler(ledger=ledger).record_rebased(
        world_id=WORLD,
        audit_cursor=audit_cursor,
        current_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "candidate_recorded"
    compiled = InteractionActProposalRecordedPayload.model_validate_json(
        ledger.recorded[0].payload_json,
        strict=True,
    )
    assert compiled.mutation.operation == "revise"
    assert compiled.mutation.expected_entity_revision == current_act.entity_revision
    assert compiled.mutation.act_before == current_act
    statuses = {
        item.actor_ref: item.status_code
        for item in compiled.mutation.act_after.participant_statuses
    }
    assert statuses == {
        COMPANION: "等待下轮继续",
        USER: "我会在下一轮继续",
    }
    assert compiled.mutation.act_after.conversation_ref == current_act.conversation_ref


@pytest.mark.parametrize(
    ("coordinate", "value"),
    (
        ("subject_ref", USER),
        ("counterparty_refs", ("actor:other",)),
        ("act_kind", "另一个动作语义"),
        ("object_ref", "interaction-object:sha256:" + "4" * 64),
    ),
)
def test_revision_rejects_changed_immutable_coordinates(
    coordinate: str,
    value: object,
) -> None:
    current_act = _opening_act(subject_ref=COMPANION, counterparty_ref=USER)
    kwargs: dict[str, object] = {
        "source_text": "我会接着处理。",
        "source_text_span": "我会接着处理",
        "operation": "revise",
        "status_code": "我会接着处理",
        "interaction_act_ref": current_act.interaction_act_id,
        "act_kind": current_act.act_kind,
        "subject_ref": current_act.subject_ref,
        "counterparty_refs": current_act.counterparty_refs,
        "object_ref": None,
        "object_label": None,
        "target_id": current_act.interaction_act_id,
        "expected_entity_revision": None,
    }
    kwargs[coordinate] = value
    if coordinate == "subject_ref":
        kwargs["counterparty_refs"] = (COMPANION,)
    issuer = AcceptedLedgerBatchIssuer()
    delegate = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    proposal, audit_cursor, _ = _record_decision(delegate, **kwargs)
    projection = delegate.project_at(audit_cursor).model_copy(
        update={"interaction_acts": (current_act,)}
    )
    ledger = _ProofLedger(
        delegate,
        audit_cursor=audit_cursor,
        current=projection,
    )

    with pytest.raises(
        InteractionActProposalCompilerError,
        match="immutable_coordinates_changed",
    ):
        InteractionActProposalCompiler(ledger=ledger).record_rebased(
            world_id=WORLD,
            audit_cursor=audit_cursor,
            current_cursor=audit_cursor,
            proposal_id=proposal.proposal_id,
        )


def test_delivered_expression_acceptance_requires_exact_terminal_receipt_chain() -> None:
    current_act = _opening_act(subject_ref=USER, counterparty_ref=COMPANION)
    issuer = AcceptedLedgerBatchIssuer()
    delegate = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    delivered_text = "好呀，你寄给我吧。"
    proposal, audit_cursor, _ = _record_decision(
        delegate,
        source_text="那本书我愿意交给你。",
        source_text_span=delivered_text,
        operation="revise",
        status_code="我愿意接着处理",
        interaction_act_ref=current_act.interaction_act_id,
        act_kind=current_act.act_kind,
        subject_ref=current_act.subject_ref,
        counterparty_refs=current_act.counterparty_refs,
        object_ref=None,
        object_label=None,
        source_scope="delivered_expression",
        target_id=current_act.interaction_act_id,
        expected_entity_revision=current_act.entity_revision,
    )
    base = delegate.project_at(audit_cursor)
    projection, commits = _delivered_proof_projection(
        base,
        proposal_id=proposal.proposal_id,
        current_act=current_act,
        text=delivered_text,
    )
    current_cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )
    ledger = _ProofLedger(
        delegate,
        audit_cursor=audit_cursor,
        current=projection,
        proof_commits=commits,
    )

    result = InteractionActProposalCompiler(ledger=ledger).record_rebased(
        world_id=WORLD,
        audit_cursor=audit_cursor,
        current_cursor=current_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "candidate_recorded"
    compiled = InteractionActProposalRecordedPayload.model_validate_json(
        ledger.recorded[0].payload_json,
        strict=True,
    )
    source_ref = compiled.mutation.source_ref
    assert source_ref.authority_kind == "delivered_expression"
    assert source_ref.source_event_ref == "event:interaction-act-message-stored"
    assert source_ref.delivery_proof is not None
    proof = source_ref.delivery_proof
    assert proof.expression_plan_event_ref == "event:interaction-act-plan-accepted"
    assert proof.expression_beat_event_ref == "event:interaction-act-beat-authorized"
    assert proof.stored_payload_event_ref == "event:interaction-act-message-stored"
    assert proof.action_id == "action:interaction-act-expression"
    assert proof.action_target_ref == USER
    assert proof.action_event_ref == "event:interaction-act-action-authorized"
    assert proof.receipt_id == "receipt:interaction-act-expression"
    assert proof.receipt_event_ref == "event:interaction-act-receipt"
    for event_ref, payload_hash in (
        (proof.expression_plan_event_ref, proof.expression_plan_event_payload_hash),
        (proof.expression_beat_event_ref, proof.expression_beat_event_payload_hash),
        (proof.stored_payload_event_ref, proof.stored_payload_event_payload_hash),
        (proof.action_event_ref, proof.action_event_payload_hash),
        (proof.receipt_event_ref, proof.receipt_payload_hash),
    ):
        assert commits[event_ref][0].payload_hash == payload_hash
    assert compiled.observed_source_text is None
    statuses = {
        item.actor_ref: item.status_code
        for item in compiled.mutation.act_after.participant_statuses
    }
    assert statuses == {USER: "等待下轮继续", COMPANION: "我愿意接着处理"}
    assert compiled.mutation.act_after.external_outcome == "not_established"
    assert compiled.mutation.act_after.conversation_ref == current_act.conversation_ref

    missing_receipt_projection = projection.model_copy(
        update={"execution_receipts": ()}
    )
    missing_receipt_ledger = _ProofLedger(
        delegate,
        audit_cursor=audit_cursor,
        current=missing_receipt_projection,
        proof_commits=commits,
    )
    with pytest.raises(
        InteractionActProposalCompilerError,
        match="delivered_receipt_not_exact",
    ):
        InteractionActProposalCompiler(
            ledger=missing_receipt_ledger
        ).record_rebased(
            world_id=WORLD,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal.proposal_id,
        )


def test_compiler_omission_is_no_change_and_writes_nothing() -> None:
    ledger, proposal, audit_cursor, _ = _fixture(include_act=False)

    result = InteractionActProposalCompiler(ledger=ledger).record_rebased(
        world_id=WORLD,
        audit_cursor=audit_cursor,
        current_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )

    assert result.status == "no_change"
    assert ledger.recorded == ()


def test_compiler_rejects_ambiguous_role_selected_source_span() -> None:
    ledger, proposal, audit_cursor, _ = _fixture(
        source_text="我有一本旧书，一本旧书，下次见面带给你。",
        source_text_span="一本旧书",
    )

    with pytest.raises(
        InteractionActProposalCompilerError,
        match="source_text_span_invalid",
    ):
        InteractionActProposalCompiler(ledger=ledger).record_rebased(
            world_id=WORLD,
            audit_cursor=audit_cursor,
            current_cursor=audit_cursor,
            proposal_id=proposal.proposal_id,
        )


def test_observed_source_preserves_valid_message_beyond_four_thousand_chars() -> None:
    long_message = "前" * 4_500 + "我有一本旧书，下次见面带给你。"
    ledger, proposal, audit_cursor, _ = _fixture(source_text=long_message)

    InteractionActProposalCompiler(ledger=ledger).record_rebased(
        world_id=WORLD,
        audit_cursor=audit_cursor,
        current_cursor=audit_cursor,
        proposal_id=proposal.proposal_id,
    )

    recorded = InteractionActProposalRecordedPayload.model_validate_json(
        ledger.recorded[0].payload_json,
        strict=True,
    )
    assert recorded.observed_source_text == long_message
    assert len(recorded.observed_source_text) > 4_096
