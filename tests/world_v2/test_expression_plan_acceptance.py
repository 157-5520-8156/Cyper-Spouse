from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.batch_invariants import validate_commit_batch
from companion_daemon.world_v2.expression_plan_acceptance import (
    ExpressionPlanAcceptanceError,
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from companion_daemon.world_v2.expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from companion_daemon.world_v2.proposal_audit_schemas import ProposalAuditProjection, canonical_json
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import BudgetAccount, CommittedWorldEventRef, ProjectionCursor


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
WORLD = "world:multi-expression"


def _hash(value: str) -> str:
    return "sha256:" + sha256(value.encode()).hexdigest()


def _proposal() -> DecisionProposal:
    first, second = "先接住你这句话。", "然后我想认真说：我在听。"
    first_hash, second_hash = _hash(first), _hash(second)
    return DecisionProposal(
        proposal_id="proposal:expression:multi:1",
        trigger_ref="event:observation:1",
        evaluated_world_revision=4,
        schema_registry_version="world-v2-proposals.1",
        evidence_refs=(ProposalEvidenceRef(ref_id="event:observation:1", evidence_kind="observed_message", source_world_revision=4, immutable_hash=_hash("source")),),
        proposed_changes=(TypedChange(
            change_id="change:expression:multi:1", kind="expression_plan_transition",
            target_id="plan:expression:multi:1", transition="accept", evidence_refs=("event:observation:1",),
            payload=CanonicalTypedPayload.from_value(payload_schema="expression_plan_transition.v1", value={
                "plan_id": "plan:expression:multi:1", "overall_intent": "respond in two natural beats",
                "ordering_policy": "dependencies", "terminal_policy": "settle_after_terminal_beats",
                "beat_drafts": [
                    {"beat_id": "beat:expression:1", "inline_text": first, "materialized_payload_ref": "payload:expression:1", "payload_hash": first_hash, "content_type": "text/plain", "dependency_beat_ids": [], "delay_window": None, "cancel_policy": "cancel-before-dispatch", "reconsider_policy": "reconsider-on-new-observation", "merge_policy": "never"},
                    {"beat_id": "beat:expression:2", "inline_text": second, "materialized_payload_ref": "payload:expression:2", "payload_hash": second_hash, "content_type": "text/plain", "dependency_beat_ids": ["beat:expression:1"], "delay_window": {"not_before": (NOW + timedelta(seconds=4)).isoformat(), "expires_at": (NOW + timedelta(minutes=2)).isoformat()}, "cancel_policy": "cancel-before-dispatch", "reconsider_policy": "reconsider-on-new-observation", "merge_policy": "merge-if-reconsidered"},
                ],
            }),
        ),),
        action_intents=(
            ProposalActionIntent(intent_id="intent:expression:1", kind="reply", layer="external_action", target="user:primary", payload_ref="payload:expression:1", payload_hash=first_hash, causal_change_id="change:expression:multi:1", beat_ref="beat:expression:1"),
            ProposalActionIntent(intent_id="intent:expression:2", kind="followup", layer="external_action", target="user:primary", payload_ref="payload:expression:2", payload_hash=second_hash, causal_change_id="change:expression:multi:1", beat_ref="beat:expression:2", dependencies=("intent:expression:1",), due_window=(NOW + timedelta(seconds=4), NOW + timedelta(minutes=2))),
        ),
        confidence=8000, brief_rationale="Two beats leave room for the user to interrupt.",
        appraisals=(), affect_tendencies=(), drives=("continue_conversation",), conflicts=(),
        behavior_tendency="engage", stance="warm", display_strategy="paced", conversation_thread_changes=(),
    )


def _audit() -> ProposalAuditProjection:
    proposal = _proposal()
    return ProposalAuditProjection(
        proposal_id=proposal.proposal_id, proposal_kind="decision", model_result_ref="model-result:1",
        deliberation_result_id="deliberation:1", model_call_id="model-call:1", attempt_id="attempt:1",
        capsule_id="a" * 64, trigger_ref=proposal.trigger_ref, evaluated_world_revision=4,
        proposal_json=canonical_json(proposal.model_dump(mode="json")), proposal_hash=proposal.proposal_hash,
        event_ref="event:proposal:1", event_payload_hash="b" * 64,
    )


def _policy() -> ExpressionPlanBudgetPolicy:
    return ExpressionPlanBudgetPolicy(account_id="account:chat:1", amount_limit_per_action=100, actor="agent:companion", allowed_targets=("user:primary",), recovery_policy="effect_once")


def _material():
    return derive_expression_plan_material(
        audit=_audit(), cursor=ProjectionCursor(world_revision=4, deliberation_revision=2, ledger_sequence=7),
        world_id=WORLD, policy=_policy(), account=BudgetAccount(account_id="account:chat:1", category="chat", window_id="window:1", limit=1000),
        logical_time=NOW, created_at=NOW, trace_id="trace:1", correlation_id="correlation:1",
    )


def test_accepted_expression_plan_materializes_all_beats_actions_dependencies_and_delay() -> None:
    material = _material()
    assert len(material.beats) == 2
    assert material.beats[1].action.dependencies == (material.beats[0].action.action_id,)
    assert material.beats[1].action.not_before == NOW + timedelta(seconds=4)
    issuer = AcceptedLedgerBatchIssuer()
    handle = ExpressionPlanAtomicRecorder(batch_issuer=issuer).prepare_batch(acceptance_id="acceptance:expression:multi:1", material=material, actor="agent:companion", source="test")
    events, _ = issuer.verify(handle=handle, world_id=WORLD, expected_cursor=material.cursor)
    assert tuple(event.event_type for event in events) == (
        "AcceptanceRecorded", "MessagePayloadStored", "MessagePayloadStored", "ExpressionPlanAccepted",
        "ExpressionBeatAuthorized", "BudgetReserved", "ActionAuthorized",
        "ExpressionBeatAuthorized", "BudgetReserved", "ActionAuthorized",
    )
    validate_commit_batch(events, expected_world_revision=4, accepted_manifest_v3_authorized=True)
    state = ReducerState(
        proposal_audits=(_audit(),), budget_accounts=(BudgetAccount(account_id="account:chat:1", category="chat", window_id="window:1", limit=1000),),
        committed_world_event_refs=tuple(CommittedWorldEventRef(event_id=f"event:prior:{index}", event_type="WorldStarted", world_revision=index + 1, payload_hash="c" * 64, logical_time=NOW) for index in range(4)),
    )
    for event in events:
        state = reduce_event(state, event)
    assert tuple(beat.beat_id for beat in state.expression_beats) == ("beat:expression:1", "beat:expression:2")
    assert tuple(action.action_id for action in state.pending_actions) == tuple(item.action.action_id for item in material.beats)


def test_rejects_intent_layer_or_delay_binding_that_does_not_match_frozen_beat() -> None:
    proposal = _proposal()
    bad_layer = proposal.model_copy(update={"action_intents": (proposal.action_intents[0].model_copy(update={"layer": "media_action"}), *proposal.action_intents[1:])})
    audit = _audit().model_copy(update={"proposal_json": canonical_json(bad_layer.model_dump(mode="json")), "proposal_hash": bad_layer.proposal_hash})
    with pytest.raises(ExpressionPlanAcceptanceError, match="beat_binding_invalid"):
        derive_expression_plan_material(
            audit=audit, cursor=ProjectionCursor(world_revision=4, deliberation_revision=2, ledger_sequence=7), world_id=WORLD,
            policy=_policy(), account=BudgetAccount(account_id="account:chat:1", category="chat", window_id="window:1", limit=1000), logical_time=NOW, created_at=NOW, trace_id="trace:1", correlation_id="correlation:1",
        )
    bad_delay = proposal.model_copy(update={"action_intents": (proposal.action_intents[0], proposal.action_intents[1].model_copy(update={"due_window": None}))})
    audit = _audit().model_copy(update={"proposal_json": canonical_json(bad_delay.model_dump(mode="json")), "proposal_hash": bad_delay.proposal_hash})
    with pytest.raises(ExpressionPlanAcceptanceError, match="delay_binding_invalid"):
        derive_expression_plan_material(
            audit=audit, cursor=ProjectionCursor(world_revision=4, deliberation_revision=2, ledger_sequence=7), world_id=WORLD,
            policy=_policy(), account=BudgetAccount(account_id="account:chat:1", category="chat", window_id="window:1", limit=1000), logical_time=NOW, created_at=NOW, trace_id="trace:1", correlation_id="correlation:1",
        )
