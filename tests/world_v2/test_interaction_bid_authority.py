from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.interaction_bid_acceptance_runtime import InteractionBidAcceptanceRuntime
from companion_daemon.world_v2.interaction_bid_proposal_compiler import (
    InteractionBidProposalCompiler,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.media_delivery_interaction import media_delivery_interaction_trigger_event
from companion_daemon.world_v2.media_v2 import (
    MediaArtifact, MediaAutomaticDeliveryApproval, MediaDeliveryShared,
    MediaPlan, MediaInspectionRecord, MediaOpportunity, media_delivery_action_id,
    media_delivery_id, media_delivery_reservation_id, media_payload_hash, planning_request_id,
)
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload, DecisionProposal, ProposalEvidenceRef, TypedChange,
)
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import (
    Action, ClaimLease, ExecutionReceipt, MediaDeliveryApprovalBinding,
    ProjectionCursor, WorldEvent,
)

from test_proposal_audit import _digest, _result


NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)
WORLD = "world:interaction-bid"


def _event(event_type: str, payload: dict[str, object], suffix: str) -> WorldEvent:
    key = domain_idempotency_key(event_type=event_type, world_id=WORLD, payload=payload)
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=f"event:interaction-bid:{suffix}",
        event_type=event_type, world_id=WORLD, logical_time=NOW, created_at=NOW,
        actor="test:worker", source="test", trace_id="trace:interaction-bid",
        causation_id="cause:interaction-bid", correlation_id="correlation:interaction-bid",
        idempotency_key=key or f"test:{suffix}", payload=payload,
    )


def _prepared_ledger() -> tuple[WorldLedger, WorldEvent, int]:
    opportunity = MediaOpportunity(
        opportunity_id="opportunity:1", candidate_id="candidate:1", family="life_share",
        delivery_mode="automatic", privacy_ceiling="personal", event_snapshot_ref="sidecar:snapshot:1",
        event_snapshot_hash="sha256:" + "1" * 64, source_event_refs=("event:source",),
        catalog_version="catalog.1", recipient_ref="user:1", expires_at=NOW + timedelta(hours=1),
    )
    plan = MediaPlan(
        plan_id="plan:1", planning_request_id=planning_request_id(opportunity.opportunity_id),
        opportunity_id=opportunity.opportunity_id, event_snapshot_hash=opportunity.event_snapshot_hash,
        family="life_share", planner_version="planner.1", schema_version="plan.1",
        plan_payload_ref="sidecar:plan", plan_payload_hash=media_payload_hash('{"plan":1}'), frozen_at=NOW,
    )
    artifact = MediaArtifact(artifact_id="artifact:1", plan_id=plan.plan_id, render_action_id="action:render", artifact_ref="sidecar:artifact", artifact_hash="sha256:" + "a" * 64, attempts=1)
    inspection = MediaInspectionRecord(inspection_id="inspection:1", plan_id=plan.plan_id, artifact_id=artifact.artifact_id, inspection_action_id="action:inspection", passed=True, reason_code="passed", inspection_payload_ref="sidecar:inspection", inspection_payload_hash="sha256:" + "2" * 64)
    approval = MediaAutomaticDeliveryApproval(approval_id="approval:1", entity_revision=1, plan_id=plan.plan_id, inspection_id=inspection.inspection_id, artifact_id=artifact.artifact_id, artifact_hash=artifact.artifact_hash, sample_hash=artifact.artifact_hash, recipient_ref="user:1", operator_ref="operator:1", family="life_share", approved_at=NOW, expires_at=NOW + timedelta(hours=1))
    action_id = media_delivery_action_id(world_id=WORLD, approval_id=approval.approval_id, approval_revision=1)
    action = Action(schema_version="world-v2.1", action_id=action_id, world_id=WORLD, logical_time=NOW, created_at=NOW, trace_id="trace:interaction-bid", causation_id="cause", correlation_id="correlation:interaction-bid", kind="media_delivery", layer="external_action", intent_ref=inspection.inspection_id, actor="companion", target="user:1", payload_ref=artifact.artifact_ref, payload_hash=artifact.artifact_hash, media_delivery_approval=MediaDeliveryApprovalBinding(approval_id=approval.approval_id, approval_revision=1), idempotency_key="media-delivery:test", budget_reservation_id=media_delivery_reservation_id(world_id=WORLD, approval_id=approval.approval_id, approval_revision=1), claim_lease=ClaimLease(owner_id="worker", attempt_id="attempt:1", acquired_at=NOW, expires_at=NOW + timedelta(minutes=1)), state="delivered", recovery_policy="effect_once")
    receipt = ExecutionReceipt(receipt_id="receipt:1", result_id="result:1", action_id=action_id, provider="platform", provider_ref="platform:1", source_event_id="provider:1", receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0, received_at=NOW, raw_payload_hash="sha256:" + "3" * 64)
    delivery = MediaDeliveryShared(delivery_id=media_delivery_id(action_id=action_id, receipt_id=receipt.receipt_id), approval_id=approval.approval_id, approval_revision=1, plan_id=plan.plan_id, inspection_id=inspection.inspection_id, artifact_id=artifact.artifact_id, artifact_hash=artifact.artifact_hash, recipient_ref="user:1", action_id=action_id, receipt_id=receipt.receipt_id)
    ledger = WorldLedger.in_memory(world_id=WORLD)
    # The media delivery itself is covered by its own lifecycle suite.  Seed its
    # already-validated preconditions so this test exercises the bid lane only.
    ledger._state = ReducerState(media_opportunities=(opportunity,), media_plans=(plan,), media_artifacts=(artifact,), media_inspections=(inspection,), media_delivery_approvals=(approval,), actions=(action,), execution_receipts=(receipt,))  # type: ignore[attr-defined]
    source = _event("MediaDeliveryShared", {"delivery": delivery.model_dump(mode="json")}, "delivery")
    trigger = media_delivery_interaction_trigger_event(source_event=source)
    ledger.commit([source, trigger], expected_world_revision=0, expected_deliberation_revision=0)
    process = ledger.project().trigger_processes[0]
    claimed = process.model_copy(update={"state": "claimed", "claim_lease": ClaimLease(owner_id="worker:interaction", attempt_id="attempt:interaction:1", acquired_at=NOW, expires_at=NOW + timedelta(minutes=2)), "attempt_ids": ("attempt:interaction:1",)})
    ledger.commit([_event("TriggerProcessClaimed", {"process": claimed.model_dump(mode="json")}, "claimed")], expected_world_revision=1, expected_deliberation_revision=1)
    located = ledger.lookup_event_commit(source.event_id)
    assert located is not None
    return ledger, source, located[1].world_revision


def _audit(ledger: WorldLedger, source: WorldEvent, source_revision: int):
    change = TypedChange(change_id="change:interaction-bid:1", kind="interaction_bid_transition", target_id="bid:1", transition="open", expected_entity_revision=0, evidence_refs=(source.event_id,), payload=CanonicalTypedPayload.from_value(payload_schema="interaction_bid_transition.v1", value={"bid_id": "bid:1", "goal": "invite_reply", "hoped_response": "user_comments_on_photo", "pressure": 1200, "audience": "user:1", "due": None}))
    proposal = DecisionProposal(proposal_id="proposal:interaction-bid:1", trigger_ref=source.event_id, evaluated_world_revision=ledger.project().world_revision, evidence_refs=(ProposalEvidenceRef(ref_id=source.event_id, evidence_kind="committed_world_event", source_world_revision=source_revision, immutable_hash="sha256:" + source.payload_hash),), proposed_changes=(change,), action_intents=(), confidence=7600, brief_rationale="A delivered image can invite a low-pressure response.", behavior_tendency="offer", stance="invite", display_strategy="private")
    base = _result()
    result = DeliberationResult(result_id="deliberation:" + _digest({"capsule_id": base.capsule_id, "proposal_hash": proposal.proposal_hash, "attempt_audits": [base.audit.model_dump(mode="json")]}), capsule_id=base.capsule_id, proposal=proposal, audit=base.audit, attempt_audits=(base.audit,))
    head = ledger.project()
    recorded = ProposalAuditRecorder(ledger=ledger).record(result, ProposalAuditContext(world_id=WORLD, trigger_ref=source.event_id, logical_time=NOW, created_at=NOW, actor="agent:companion", source="test", trace_id="trace:interaction-bid", causation_id="cause:proposal", correlation_id="correlation:interaction-bid", evaluated_world_revision=head.world_revision, expected_commit_world_revision=head.world_revision, expected_deliberation_revision=head.deliberation_revision))
    return proposal, recorded


def _cursor(ledger: WorldLedger) -> ProjectionCursor:
    head = ledger.project()
    return ProjectionCursor(world_revision=head.world_revision, deliberation_revision=head.deliberation_revision, ledger_sequence=head.ledger_sequence)


def test_delivered_media_bid_is_compiled_and_atomically_accepted() -> None:
    ledger, source, source_revision = _prepared_ledger()
    proposal, audited = _audit(ledger, source, source_revision)
    compiled = InteractionBidProposalCompiler(ledger=ledger).record(world_id=WORLD, cursor=audited.cursor, proposal_id=proposal.proposal_id)
    issuer = AcceptedLedgerBatchIssuer()
    ledger._accepted_batch_issuer = issuer  # type: ignore[attr-defined]
    runtime = InteractionBidAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    result = runtime.accept_runtime_owned(handle=runtime.pin_proposal(cursor=_cursor(ledger), proposal_id=compiled.typed_proposal_id), actor="worker:interaction", source="world-v2:interaction-worker")
    bid = ledger.project().interaction_bids[0]
    assert bid.delivery_event_ref == source.event_id
    assert bid.goal == "invite_reply"
    assert tuple(ledger.lookup_event_commit(event_id)[0].event_type for event_id in result.event_ids) == ("AcceptanceRecorded", "InteractionBidOpened")


def test_sqlite_migrates_v30_head_without_fabricating_interaction_bids(tmp_path) -> None:
    path = tmp_path / "interaction-bid-v30.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit([_event("WorldStarted", {}, "started")], expected_world_revision=0, expected_deliberation_revision=0)
    ledger.close()
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT state_json, world_revision FROM world_v2_heads WHERE world_id = ?", (WORLD,)).fetchone()
        assert row is not None
        state = ReducerState.model_validate_json(row[0])
        semantic = state.semantic_payload(world_id=WORLD, world_revision=int(row[1]), reducer_bundle_version="world-v2-reducers.30")
        legacy_hash = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        connection.execute("UPDATE world_v2_heads SET semantic_hash = ?, reducer_bundle_version = ?, state_hash = '' WHERE world_id = ?", (legacy_hash, "world-v2-reducers.30", WORLD))
    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.31"
    assert migrated.project().interaction_bids == ()
    assert migrated.rebuild() == migrated.project()
