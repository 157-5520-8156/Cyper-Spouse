from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

import pytest

from companion_daemon.world_v2.media_v2 import (
    MediaOpportunity,
    MediaOpportunityFrozenPayload,
    MediaPlan,
    MediaPlanRecordedPayload,
    MediaArtifact,
    MediaInspectionRecord,
    MediaPreview,
    MediaRenderArtifactRecordedPayload,
    MediaInspectionRecordedPayload,
    MediaPreviewGeneratedPayload,
    MediaPreviewFailedPayload,
    MediaRepairAuthorization,
    MediaRepairAuthorizedPayload,
    PhotoCandidate,
    FrozenMediaEvidenceSnapshot,
    MediaEvidenceSource,
    canonical_media_json,
    PhotoCandidateOpenedPayload,
    continuation_trigger_id,
    media_payload_hash,
    planning_request_id,
    media_repair_attempt_id,
    media_repair_action_id,
    media_repair_reservation_id,
    media_repair_trigger_id,
    SQLiteImmutableMediaPayloadStore,
    StoredMediaPayload,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import (
    Action,
    ClaimLease,
    CommittedWorldEventRef,
    ExecutionReceipt,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:media-v2"
SOURCE = "event:world:committed"


def _event(event_type: str, payload: dict[str, object], suffix: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=f"event:media-test:{suffix}", event_type=event_type,
        world_id=WORLD, logical_time=NOW, created_at=NOW, actor="system:test", source="test",
        trace_id="trace:media", causation_id="cause:media", correlation_id="correlation:media",
        idempotency_key=f"idempotency:media:{suffix}", payload=payload,
    )


def _snapshot(*, event_ref: str = SOURCE, payload_hash: str = "a" * 64) -> str:
    return canonical_media_json(FrozenMediaEvidenceSnapshot(source_events=(
        MediaEvidenceSource(event_ref=event_ref, payload_hash=payload_hash),
    )).model_dump(mode="json"))


def _opportunity(*, event_ref: str = SOURCE, payload_hash: str = "a" * 64) -> MediaOpportunity:
    snapshot = _snapshot(event_ref=event_ref, payload_hash=payload_hash)
    return MediaOpportunity(
        opportunity_id="opportunity:1", candidate_id="candidate:1", family="life_share",
        delivery_mode="preview", privacy_ceiling="personal", event_snapshot_ref="sidecar:snapshot:1",
        event_snapshot_hash=media_payload_hash(snapshot), source_event_refs=(event_ref,), catalog_version="media-catalog.1",
        expires_at=NOW + timedelta(hours=1),
    )


def _state() -> ReducerState:
    return ReducerState(committed_world_event_refs=(CommittedWorldEventRef(
        event_id=SOURCE, event_type="WorldOccurrenceSettled", world_revision=1,
        payload_hash="a" * 64, logical_time=NOW,
    ),))


def test_candidate_and_opportunity_are_bound_only_to_prior_committed_events() -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:1", source_event_refs=(SOURCE,), family="life_share", privacy_ceiling="personal",
    )
    state = reduce_event(_state(), _event("PhotoCandidateOpened", PhotoCandidateOpenedPayload(candidate=candidate).model_dump(mode="json"), "candidate"))
    state = reduce_event(state, _event("MediaOpportunityFrozen", MediaOpportunityFrozenPayload(opportunity=_opportunity()).model_dump(mode="json"), "opportunity"))
    assert state.photo_candidates == (
        candidate.model_copy(update={"entity_revision": 2, "status": "selected"}),
    )
    assert state.media_opportunities == (_opportunity(),)

    invalid = candidate.model_copy(update={"candidate_id": "candidate:invalid", "source_event_refs": ("event:uncommitted",)})
    with pytest.raises(ValueError, match="prior committed"):
        reduce_event(_state(), _event("PhotoCandidateOpened", PhotoCandidateOpenedPayload(candidate=invalid).model_dump(mode="json"), "invalid"))


def test_p1_candidate_pins_source_hashes_expiry_and_selected_transition() -> None:
    candidate = PhotoCandidate(
        candidate_id="candidate:p1",
        source_event_refs=(SOURCE,),
        family="life_share",
        privacy_ceiling="personal",
        opened_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        ecology_category="activity_result",
        ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref=SOURCE, payload_hash="a" * 64),),
    )
    state = reduce_event(
        _state(),
        _event("PhotoCandidateOpened", {"candidate": candidate.model_dump(mode="json")}, "candidate-p1"),
    )
    opportunity = _opportunity().model_copy(
        update={
            "opportunity_id": "opportunity:p1",
            "candidate_id": candidate.candidate_id,
            "ecology_category": "activity_result",
        }
    )
    state = reduce_event(
        state,
        _event("MediaOpportunityFrozen", {"opportunity": opportunity.model_dump(mode="json")}, "opportunity-p1"),
    )
    assert state.photo_candidates[0].status == "selected"
    assert state.photo_candidates[0].entity_revision == 2

    forged = candidate.model_copy(
        update={"candidate_id": "candidate:forged", "source_events": (MediaEvidenceSource(event_ref=SOURCE, payload_hash="b" * 64),)}
    )
    with pytest.raises(ValueError, match="source hashes"):
        reduce_event(
            _state(),
            _event("PhotoCandidateOpened", {"candidate": forged.model_dump(mode="json")}, "candidate-forged"),
        )


def test_recorded_plan_requires_exact_delivered_action_receipt_and_opens_deterministic_continuation() -> None:
    opportunity = _opportunity()
    candidate = PhotoCandidate(candidate_id="candidate:1", source_event_refs=(SOURCE,), family="life_share", privacy_ceiling="personal")
    state = reduce_event(_state(), _event("PhotoCandidateOpened", {"candidate": candidate.model_dump(mode="json")}, "candidate"))
    state = reduce_event(state, _event("MediaOpportunityFrozen", {"opportunity": opportunity.model_dump(mode="json")}, "opportunity"))
    request = planning_request_id(opportunity.opportunity_id)
    lease = ClaimLease(owner_id="worker:media", attempt_id="attempt:1", acquired_at=NOW, expires_at=NOW + timedelta(minutes=1))
    action = Action.model_construct(
        schema_version="world-v2.1", action_id="action:planning:1", world_id=WORLD, logical_time=NOW, created_at=NOW,
        trace_id="trace:media", causation_id="cause:media", correlation_id="correlation:media", kind="media_planning",
        layer="media_action", intent_ref=opportunity.opportunity_id, actor="companion:girl", target="provider:media-planner",
        payload_ref=opportunity.event_snapshot_ref, payload_hash=opportunity.event_snapshot_hash, provider_media_grant=None,
        idempotency_key=request, budget_reservation_id="reservation:1", claim_lease=lease, dispatch_pending=None,
        state="delivered", recovery_policy="effect_once",
    )
    receipt = ExecutionReceipt(
        receipt_id="receipt:planning:1", result_id="result:planning:1", action_id=action.action_id,
        provider="provider:media-planner", provider_ref=request, source_event_id="source:planning:1",
        receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0,
        received_at=NOW, raw_payload_hash="sha256:" + "b" * 64,
    )
    state = state.model_copy(update={"actions": (action,), "execution_receipts": (receipt,)})
    plan_body = '{"plan":"frozen"}'
    plan = MediaPlan(
        plan_id="plan:1", planning_request_id=request, opportunity_id=opportunity.opportunity_id,
        event_snapshot_hash=opportunity.event_snapshot_hash, family="life_share", planner_version="planner.1",
        schema_version="media-plan.1", plan_payload_ref="sidecar:plan:1", plan_payload_hash=media_payload_hash(plan_body), frozen_at=NOW,
    )
    state = reduce_event(state, _event("MediaPlanRecorded", MediaPlanRecordedPayload(action_id=action.action_id, receipt_id=receipt.receipt_id, plan=plan).model_dump(mode="json"), "plan"))
    assert state.media_plans == (plan,)
    trigger_id = continuation_trigger_id(plan)
    state = reduce_event(state, _event("TriggerProcessOpened", {"process": TriggerProcess(
        trigger_id=trigger_id, trigger_ref=trigger_id, process_kind="media_continuation",
        source_evidence_ref="event:media-test:plan", state="open",
    ).model_dump(mode="json")}, "continuation"))
    assert state.trigger_processes[0].trigger_id == trigger_id

    tampered = plan.model_copy(update={"event_snapshot_hash": "sha256:" + hashlib.sha256(b"other").hexdigest()})
    with pytest.raises(ValueError, match="bound to one delivered"):
        reduce_event(state, _event("MediaPlanRecorded", {"action_id": action.action_id, "receipt_id": receipt.receipt_id, "plan": tampered.model_dump(mode="json")}, "tampered"))


def test_media_freeze_replays_across_sqlite_restart_and_sidecar_ref_cannot_rebind(tmp_path) -> None:
    path = tmp_path / "media-v2.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    start = _event("WorldStarted", {}, "start")
    ledger.commit((start,), expected_world_revision=0, expected_deliberation_revision=0)
    candidate = PhotoCandidate(candidate_id="candidate:1", source_event_refs=(start.event_id,), family="life_share", privacy_ceiling="personal")
    snapshot = _snapshot(event_ref=start.event_id, payload_hash=start.payload_hash)
    opportunity = _opportunity(event_ref=start.event_id, payload_hash=start.payload_hash)
    store = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    record = StoredMediaPayload(payload_ref=opportunity.event_snapshot_ref, payload_hash=opportunity.event_snapshot_hash,
        content_type="application/vnd.world-v2.media-opportunity+json", body=snapshot)
    store.put_if_absent(record)
    with pytest.raises(ValueError, match="different immutable bytes"):
        store.put_if_absent(StoredMediaPayload(payload_ref=record.payload_ref, payload_hash=media_payload_hash('{"other":true}'),
            content_type=record.content_type, body='{"other":true}'))
    events = []
    for event_type, payload, suffix in (
        ("PhotoCandidateOpened", {"candidate": candidate.model_dump(mode="json")}, "candidate"),
        ("MediaOpportunityFrozen", {"opportunity": opportunity.model_dump(mode="json")}, "opportunity"),
    ):
        events.append(_event(event_type, payload, suffix).model_copy(update={
            "idempotency_key": domain_idempotency_key(event_type=event_type, world_id=WORLD, payload=payload),
        }))
    projection = ledger.project()
    cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )
    ledger.commit_at_cursor(tuple(events), expected_cursor=cursor, commit_id="commit:media-freeze-test")
    expected = ledger.project()
    ledger.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()
    store.close()


def test_render_inspection_preview_are_receipt_bound_and_preview_can_never_be_delivery() -> None:
    opportunity = _opportunity()
    candidate = PhotoCandidate(candidate_id="candidate:1", source_event_refs=(SOURCE,), family="life_share", privacy_ceiling="personal")
    state = reduce_event(_state(), _event("PhotoCandidateOpened", {"candidate": candidate.model_dump(mode="json")}, "candidate"))
    state = reduce_event(state, _event("MediaOpportunityFrozen", {"opportunity": opportunity.model_dump(mode="json")}, "opportunity"))
    plan = MediaPlan(plan_id="plan:render", planning_request_id=planning_request_id(opportunity.opportunity_id), opportunity_id=opportunity.opportunity_id, event_snapshot_hash=opportunity.event_snapshot_hash, family="life_share", planner_version="planner.1", schema_version="media-plan.1", plan_payload_ref="sidecar:plan:render", plan_payload_hash=media_payload_hash('{"plan":"render"}'), frozen_at=NOW)
    planning_action = Action.model_construct(schema_version="world-v2.1", action_id="action:planning:render", world_id=WORLD, logical_time=NOW, created_at=NOW, trace_id="trace:media", causation_id="cause:media", correlation_id="correlation:media", kind="media_planning", layer="media_action", intent_ref=opportunity.opportunity_id, actor="companion:girl", target="provider:media-planner", payload_ref=opportunity.event_snapshot_ref, payload_hash=opportunity.event_snapshot_hash, provider_media_grant=None, idempotency_key=plan.planning_request_id, budget_reservation_id="reservation:planning:render", state="delivered", recovery_policy="effect_once")
    planning_receipt = ExecutionReceipt(receipt_id="receipt:planning:render", result_id="result:planning:render", action_id=planning_action.action_id, provider="provider", provider_ref="planning", source_event_id="planning", receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0, received_at=NOW, raw_payload_hash="sha256:" + "1" * 64)
    state = state.model_copy(update={"actions": (planning_action,), "execution_receipts": (planning_receipt,)})
    state = reduce_event(state, _event("MediaPlanRecorded", MediaPlanRecordedPayload(action_id=planning_action.action_id, receipt_id=planning_receipt.receipt_id, plan=plan).model_dump(mode="json"), "plan-render"))
    render = Action.model_construct(schema_version="world-v2.1", action_id="action:render:1", world_id=WORLD, logical_time=NOW, created_at=NOW, trace_id="trace:media", causation_id="cause:media", correlation_id="correlation:media", kind="media_render", layer="media_action", intent_ref=plan.plan_id, actor="companion:girl", target="provider:media-renderer", payload_ref=plan.plan_payload_ref, payload_hash=plan.plan_payload_hash, provider_media_grant=None, idempotency_key="render:1", budget_reservation_id="reservation:render:1", state="delivered", recovery_policy="effect_once")
    render_receipt = ExecutionReceipt(receipt_id="receipt:render:1", result_id="result:render:1", action_id=render.action_id, provider="provider", provider_ref="render", source_event_id="render", receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0, received_at=NOW, raw_payload_hash="sha256:" + "2" * 64)
    artifact = MediaArtifact(artifact_id="artifact:1", plan_id=plan.plan_id, render_action_id=render.action_id, artifact_ref="sidecar:artifact:1", artifact_hash="sha256:" + "3" * 64, attempts=1)
    state = state.model_copy(update={"actions": (*state.actions, render), "execution_receipts": (*state.execution_receipts, render_receipt)})
    state = reduce_event(state, _event("MediaRenderArtifactRecorded", MediaRenderArtifactRecordedPayload(action_id=render.action_id, receipt_id=render_receipt.receipt_id, artifact=artifact).model_dump(mode="json"), "artifact"))
    inspect = Action.model_construct(schema_version="world-v2.1", action_id="action:inspection:1", world_id=WORLD, logical_time=NOW, created_at=NOW, trace_id="trace:media", causation_id="cause:media", correlation_id="correlation:media", kind="media_inspection", layer="media_action", intent_ref=artifact.artifact_id, actor="companion:girl", target="provider:media-inspector", payload_ref=artifact.artifact_ref, payload_hash=artifact.artifact_hash, provider_media_grant=None, idempotency_key="inspect:1", budget_reservation_id="reservation:inspection:1", state="delivered", recovery_policy="effect_once")
    inspect_receipt = ExecutionReceipt(receipt_id="receipt:inspection:1", result_id="result:inspection:1", action_id=inspect.action_id, provider="provider", provider_ref="inspect", source_event_id="inspect", receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0, received_at=NOW, raw_payload_hash="sha256:" + "4" * 64)
    inspection = MediaInspectionRecord(inspection_id="inspection:1", plan_id=plan.plan_id, artifact_id=artifact.artifact_id, inspection_action_id=inspect.action_id, passed=True, reason_code="passed", observed_summary="matches", inspection_payload_ref="sidecar:inspection:1", inspection_payload_hash="sha256:" + "5" * 64)
    state = state.model_copy(update={"actions": (*state.actions, inspect), "execution_receipts": (*state.execution_receipts, inspect_receipt)})
    state = reduce_event(state, _event("MediaInspectionRecorded", MediaInspectionRecordedPayload(action_id=inspect.action_id, receipt_id=inspect_receipt.receipt_id, inspection=inspection).model_dump(mode="json"), "inspection"))
    preview = MediaPreview(preview_id="preview:1", plan_id=plan.plan_id, artifact_id=artifact.artifact_id, inspection_id=inspection.inspection_id, recipient_ref=None)
    state = reduce_event(state, _event("MediaPreviewGenerated", MediaPreviewGeneratedPayload(preview=preview).model_dump(mode="json"), "preview"))
    assert state.media_previews == (preview,)
    with pytest.raises(ValueError, match="preview"):
        reduce_event(state, _event("MediaPreviewGenerated", {"preview": preview.model_dump(mode="json") | {"delivery_mode": "automatic"}}, "delivery"))


def test_repairable_inspection_has_one_source_bound_repair_then_second_failure_is_terminal() -> None:
    """W2-MED-002: repair is accepted once, on the same frozen plan only."""
    opportunity = _opportunity()
    plan = MediaPlan(plan_id="plan:repair", planning_request_id=planning_request_id(opportunity.opportunity_id), opportunity_id=opportunity.opportunity_id, event_snapshot_hash=opportunity.event_snapshot_hash, family="life_share", planner_version="planner.1", schema_version="media-plan.1", plan_payload_ref="sidecar:plan:repair", plan_payload_hash=media_payload_hash('{"plan":"repair"}'), frozen_at=NOW)
    original = MediaArtifact(artifact_id="artifact:repair:1", plan_id=plan.plan_id, render_action_id="action:render:repair", artifact_ref="sidecar:artifact:repair:1", artifact_hash="sha256:" + "a" * 64, attempts=1)
    failed = MediaInspectionRecord(inspection_id="inspection:repair:1", plan_id=plan.plan_id, artifact_id=original.artifact_id, inspection_action_id="action:inspection:repair:1", passed=False, reason_code="subject_pose_mismatch", observed_summary="pose mismatch", inspection_payload_ref="sidecar:inspection:repair:1", inspection_payload_hash="sha256:" + "b" * 64, repairable=True, repair_scope=("subject_pose",))
    trigger_id = media_repair_trigger_id(world_id=WORLD, inspection_id=failed.inspection_id)
    lease = ClaimLease(owner_id="worker:media", attempt_id="attempt:repair:1", acquired_at=NOW, expires_at=NOW + timedelta(minutes=1))
    trigger = TriggerProcess(trigger_id=trigger_id, trigger_ref=f"media-repair:{failed.inspection_id}", process_kind="media_repair", source_evidence_ref=f"inspection:{failed.inspection_id}", state="claimed", claim_lease=lease, attempt_ids=(lease.attempt_id,))
    state = _state().model_copy(update={"media_opportunities": (opportunity,), "media_plans": (plan,), "media_artifacts": (original,), "media_inspections": (failed,), "trigger_processes": (trigger,)})
    repair_id = media_repair_attempt_id(plan_id=plan.plan_id, failed_artifact_hash=original.artifact_hash)
    repair_action_id = media_repair_action_id(world_id=WORLD, repair_attempt_id=repair_id)
    repair_reservation_id = media_repair_reservation_id(world_id=WORLD, repair_attempt_id=repair_id)
    repair = MediaRepairAuthorization(repair_attempt_id=repair_id, trigger_id=trigger_id, plan_id=plan.plan_id, opportunity_id=opportunity.opportunity_id, event_snapshot_hash=opportunity.event_snapshot_hash, failed_artifact_id=original.artifact_id, failed_artifact_hash=original.artifact_hash, inspection_id=failed.inspection_id, inspection_payload_hash=failed.inspection_payload_hash, defect_scope=failed.repair_scope, action_id=repair_action_id, reservation_id=repair_reservation_id)
    state = reduce_event(state, _event("MediaRepairAuthorized", MediaRepairAuthorizedPayload(repair=repair).model_dump(mode="json"), "repair-authorized"))

    repair_action = Action.model_construct(schema_version="world-v2.1", action_id=repair_action_id, world_id=WORLD, logical_time=NOW, created_at=NOW, trace_id="trace:repair", causation_id="cause:repair", correlation_id="correlation:repair", kind="media_repair", layer="media_action", intent_ref=plan.plan_id, actor="companion:girl", target="provider:media-renderer", payload_ref=failed.inspection_payload_ref, payload_hash=failed.inspection_payload_hash, provider_media_grant=None, idempotency_key=repair_id, budget_reservation_id=repair.reservation_id, state="delivered", recovery_policy="effect_once")
    receipt = ExecutionReceipt(receipt_id="receipt:repair:1", result_id="result:repair:1", action_id=repair_action_id, provider="provider", provider_ref=repair_id, source_event_id="repair", receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0, received_at=NOW, raw_payload_hash="sha256:" + "c" * 64)
    state = state.model_copy(update={"actions": (repair_action,), "execution_receipts": (receipt,)})
    repaired_artifact = MediaArtifact(artifact_id="artifact:repair:2", plan_id=plan.plan_id, render_action_id=repair_action_id, artifact_ref="sidecar:artifact:repair:2", artifact_hash="sha256:" + "d" * 64, attempts=2)
    state = reduce_event(state, _event("MediaRenderArtifactRecorded", MediaRenderArtifactRecordedPayload(action_id=repair_action_id, receipt_id=receipt.receipt_id, artifact=repaired_artifact).model_dump(mode="json"), "repair-artifact"))
    second_action = Action.model_construct(schema_version="world-v2.1", action_id="action:inspection:repair:2", world_id=WORLD, logical_time=NOW, created_at=NOW, trace_id="trace:repair", causation_id="cause:repair", correlation_id="correlation:repair", kind="media_inspection", layer="media_action", intent_ref=repaired_artifact.artifact_id, actor="companion:girl", target="provider:media-inspector", payload_ref=repaired_artifact.artifact_ref, payload_hash=repaired_artifact.artifact_hash, provider_media_grant=None, idempotency_key="inspect:repair:2", budget_reservation_id="reservation:inspect:repair:2", state="delivered", recovery_policy="effect_once")
    second_receipt = ExecutionReceipt(receipt_id="receipt:inspection:repair:2", result_id="result:inspection:repair:2", action_id=second_action.action_id, provider="provider", provider_ref="inspect:repair:2", source_event_id="inspection:repair:2", receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0, received_at=NOW, raw_payload_hash="sha256:" + "e" * 64)
    second = MediaInspectionRecord(inspection_id="inspection:repair:2", plan_id=plan.plan_id, artifact_id=repaired_artifact.artifact_id, inspection_action_id=second_action.action_id, passed=False, reason_code="still_wrong", inspection_payload_ref="sidecar:inspection:repair:2", inspection_payload_hash="sha256:" + "f" * 64)
    state = state.model_copy(update={"actions": (*state.actions, second_action), "execution_receipts": (*state.execution_receipts, second_receipt)})
    state = reduce_event(state, _event("MediaInspectionRecorded", MediaInspectionRecordedPayload(action_id=second_action.action_id, receipt_id=second_receipt.receipt_id, inspection=second).model_dump(mode="json"), "repair-inspection-2"))
    state = reduce_event(state, _event("MediaPreviewFailed", MediaPreviewFailedPayload(plan_id=plan.plan_id, artifact_id=repaired_artifact.artifact_id, inspection_id=second.inspection_id, reason_code=second.reason_code).model_dump(mode="json"), "repair-terminal"))
    assert state.media_failed_plan_ids == (plan.plan_id,)
    assert len(state.media_artifacts) == 2
    assert not any(item.process_kind == "media_repair" and item.source_evidence_ref == f"inspection:{second.inspection_id}" for item in state.trigger_processes)
