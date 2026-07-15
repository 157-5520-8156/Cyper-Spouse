from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest

from companion_daemon.world_v2.minimal_reply_acceptance import (
    MinimalReplyAcceptanceError,
    ReplyBudgetPolicy,
    derive_minimal_reply_material,
)
from companion_daemon.world_v2.minimal_reply_manifest import build_minimal_reply_manifest
from companion_daemon.world_v2.minimal_reply_atomic_recorder import MinimalReplyAtomicRecorder
from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.batch_invariants import validate_commit_batch
from companion_daemon.world_v2.proposal_audit_schemas import (
    ProposalAuditProjection,
    canonical_json,
)
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    MinimalProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    CommittedWorldEventRef,
    ProjectionCursor,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:minimal-reply"


def _proposal(*, text: str = "我明白了，刚刚确实没有接住。") -> MinimalProposal:
    payload_hash = "sha256:" + sha256(text.encode()).hexdigest()
    return MinimalProposal(
        proposal_id="proposal:minimal-reply:1",
        trigger_ref="event:observation:1",
        evaluated_world_revision=4,
        schema_registry_version="world-v2-proposals.1",
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id="event:observation:1",
                evidence_kind="observed_message",
                source_world_revision=4,
                immutable_hash="sha256:" + "a" * 64,
            ),
        ),
        proposed_changes=(
            TypedChange(
                change_id="change:expression:1",
                kind="expression_plan_transition",
                target_id="plan:reply:1",
                transition="accept",
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="expression_plan_transition.v1",
                    value={
                        "plan_id": "plan:reply:1",
                        "overall_intent": "reply",
                        "ordering_policy": "dependencies",
                        "terminal_policy": "settle",
                        "beat_drafts": [
                            {
                                "beat_id": "beat:reply:1",
                                "inline_text": text,
                                "materialized_payload_ref": "payload:reply:1",
                                "payload_hash": payload_hash,
                                "content_type": "text/plain",
                                "dependency_beat_ids": [],
                                "delay_window": None,
                                "cancel_policy": "cancel-before-dispatch",
                                "reconsider_policy": "reconsider-on-new-observation",
                                "merge_policy": "never",
                            }
                        ],
                    },
                ),
            ),
        ),
        action_intents=(
            ProposalActionIntent(
                intent_id="intent:reply:1",
                kind="reply",
                layer="external_action",
                target="user:primary",
                payload_ref="payload:reply:1",
                payload_hash=payload_hash,
                causal_change_id="change:expression:1",
                beat_ref="beat:reply:1",
            ),
        ),
        confidence=7000,
        brief_rationale="Acknowledge the missed emotion without claiming world facts.",
        source_model_result="model-result:final:1",
        response_text=text,
        stance="acknowledge_briefly",
    )


def _audit(proposal: MinimalProposal | None = None) -> ProposalAuditProjection:
    value = proposal or _proposal()
    return ProposalAuditProjection(
        proposal_id=value.proposal_id,
        proposal_kind="minimal",
        model_result_ref="model-result:final:1",
        deliberation_result_id="deliberation:1",
        model_call_id="model-call:1",
        attempt_id="attempt:1",
        capsule_id="b" * 64,
        trigger_ref=value.trigger_ref,
        evaluated_world_revision=value.evaluated_world_revision,
        proposal_json=canonical_json(value.model_dump(mode="json")),
        proposal_hash=value.proposal_hash,
        event_ref="event:proposal:1",
        event_payload_hash="c" * 64,
    )


def _policy() -> ReplyBudgetPolicy:
    return ReplyBudgetPolicy(
        account_id="account:chat:1",
        amount_limit=100,
        actor="agent:companion",
        target="user:primary",
        recovery_policy="effect_once",
    )


def _account(*, limit: int = 1_000) -> BudgetAccount:
    return BudgetAccount(
        account_id="account:chat:1", category="chat", window_id="window:1", limit=limit
    )


def test_derives_reply_action_and_reservation_only_from_audited_minimal_proposal() -> None:
    material = derive_minimal_reply_material(
        audit=_audit(),
        cursor=ProjectionCursor(world_revision=4, deliberation_revision=2, ledger_sequence=7),
        world_id=WORLD,
        policy=_policy(),
        account=_account(),
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:1",
        correlation_id="correlation:1",
    )

    assert material.action.kind == "reply"
    assert material.reservation.category == "chat"
    assert material.action.payload_hash == material.beat.payload.payload_hash
    assert material.action.budget_reservation_id == material.reservation.reservation_id


def test_rejects_stale_or_unaffordable_reply_without_deriving_an_action() -> None:
    with pytest.raises(MinimalReplyAcceptanceError, match="stale_revision"):
        derive_minimal_reply_material(
            audit=_audit(),
            cursor=ProjectionCursor(world_revision=5, deliberation_revision=2, ledger_sequence=7),
            world_id=WORLD,
            policy=_policy(),
            account=_account(),
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:1",
            correlation_id="correlation:1",
        )


def test_manifest_binds_every_reply_side_effect_to_its_audited_material() -> None:
    material = derive_minimal_reply_material(
        audit=_audit(),
        cursor=ProjectionCursor(world_revision=4, deliberation_revision=2, ledger_sequence=7),
        world_id=WORLD,
        policy=_policy(),
        account=_account(),
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:1",
        correlation_id="correlation:1",
    )
    manifest = build_minimal_reply_manifest(acceptance_id="acceptance:reply:1", material=material)

    assert manifest.action_id == material.action.action_id
    assert manifest.reservation_id == material.reservation.reservation_id
    assert manifest.message_payload_hash == material.beat.payload.payload_hash
    with pytest.raises(MinimalReplyAcceptanceError, match="budget_unavailable"):
        derive_minimal_reply_material(
            audit=_audit(),
            cursor=ProjectionCursor(world_revision=4, deliberation_revision=2, ledger_sequence=7),
            world_id=WORLD,
            policy=_policy(),
            account=_account(limit=10),
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:1",
            correlation_id="correlation:1",
        )


def test_rejects_a_model_selected_target_that_conflicts_with_composition_policy() -> None:
    proposal = _proposal().model_copy(
        update={
            "action_intents": (
                _proposal().action_intents[0].model_copy(update={"target": "user:other"}),
            )
        }
    )
    with pytest.raises(MinimalReplyAcceptanceError, match="policy_target_mismatch"):
        derive_minimal_reply_material(
            audit=_audit(proposal),
            cursor=ProjectionCursor(world_revision=4, deliberation_revision=2, ledger_sequence=7),
            world_id=WORLD,
            policy=_policy(),
            account=_account(),
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:1",
            correlation_id="correlation:1",
        )


def test_recorder_materializes_a_closed_reply_batch_that_reduces_to_dispatchable_state() -> None:
    material = derive_minimal_reply_material(
        audit=_audit(),
        cursor=ProjectionCursor(world_revision=4, deliberation_revision=2, ledger_sequence=7),
        world_id=WORLD,
        policy=_policy(),
        account=_account(),
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:1",
        correlation_id="correlation:1",
    )
    issuer = AcceptedLedgerBatchIssuer()
    handle = MinimalReplyAtomicRecorder(batch_issuer=issuer).prepare_batch(
        acceptance_id="acceptance:reply:1",
        material=material,
        actor="agent:companion",
        source="minimal-reply-test",
    )
    events, _ = issuer.verify(handle=handle, world_id=WORLD, expected_cursor=material.cursor)
    assert tuple(event.event_type for event in events) == (
        "AcceptanceRecorded",
        "MessagePayloadStored",
        "ExpressionPlanAccepted",
        "ExpressionBeatAuthorized",
        "BudgetReserved",
        "ActionAuthorized",
    )
    validate_commit_batch(events, expected_world_revision=4, accepted_manifest_v3_authorized=True)

    state = ReducerState(
        proposal_audits=(_audit(),),
        budget_accounts=(_account(),),
        committed_world_event_refs=tuple(
            CommittedWorldEventRef(
                event_id=f"event:prior:{index}",
                event_type="WorldStarted",
                world_revision=index + 1,
                payload_hash="a" * 64,
                logical_time=NOW,
            )
            for index in range(4)
        ),
    )
    for event in events:
        state = reduce_event(state, event)

    assert state.stored_message_payloads[0].text == "我明白了，刚刚确实没有接住。"
    assert state.expression_beats[0].beat_id == "beat:reply:1"
    assert state.pending_actions == (material.action,)
