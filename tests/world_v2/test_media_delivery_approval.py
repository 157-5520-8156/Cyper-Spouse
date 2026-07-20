from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.media_delivery_runtime import (
    MediaDeliveryReceiptLifecycle,
    MediaDeliveryRuntime,
    require_current_media_delivery_approval,
)
from companion_daemon.world_v2.media_delivery_interaction import (
    media_delivery_interaction_trigger_event,
    media_delivery_interaction_trigger_id,
)
from companion_daemon.world_v2.media_v2 import (
    MediaArtifact,
    MediaAutomaticDeliveryApproval,
    MediaAutomaticDeliveryApprovedPayload,
    MediaInspectionRecord,
    MediaOpportunity,
    MediaPlan,
    media_delivery_action_id,
    media_delivery_reservation_id,
    media_payload_hash,
    planning_request_id,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.settlement import SettlementPlanner
from companion_daemon.world_v2.schemas import (
    Action,
    ClaimLease,
    ExecutionReceipt,
    ExternalObservation,
    MediaDeliveryApprovalBinding,
    TriggerProcess,
    WorldEvent,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:media-delivery"
ARTIFACT_HASH = "sha256:" + "a" * 64


def _event(event_type: str, payload: dict[str, object], suffix: str, *, at: datetime = NOW) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=f"event:media-delivery-test:{suffix}",
        event_type=event_type, world_id=WORLD, logical_time=at, created_at=at,
        actor="operator:alice", source="test", trace_id="trace:delivery",
        causation_id="cause:delivery", correlation_id="correlation:delivery",
        idempotency_key=f"test:{suffix}", payload=payload,
    )


def _approval(*, revision: int = 1, at: datetime = NOW) -> MediaAutomaticDeliveryApproval:
    return MediaAutomaticDeliveryApproval(
        approval_id="approval:media:1", entity_revision=revision, plan_id="plan:1",
        inspection_id="inspection:1", artifact_id="artifact:1", artifact_hash=ARTIFACT_HASH,
        sample_hash=ARTIFACT_HASH, recipient_ref="user:1", operator_ref="operator:alice",
        family="life_share", approved_at=at, expires_at=at + timedelta(minutes=10),
    )


def _state() -> ReducerState:
    opportunity = MediaOpportunity(
        opportunity_id="opportunity:1", candidate_id="candidate:1", family="life_share",
        delivery_mode="automatic", privacy_ceiling="personal", event_snapshot_ref="sidecar:snapshot:1",
        event_snapshot_hash="sha256:" + "1" * 64, source_event_refs=("event:source:1",),
        catalog_version="catalog.1", recipient_ref="user:1", expires_at=NOW + timedelta(hours=1),
    )
    plan = MediaPlan(
        plan_id="plan:1", planning_request_id=planning_request_id(opportunity.opportunity_id),
        opportunity_id=opportunity.opportunity_id, event_snapshot_hash=opportunity.event_snapshot_hash,
        family="life_share", planner_version="planner.1", schema_version="plan.1",
        plan_payload_ref="sidecar:plan:1", plan_payload_hash=media_payload_hash('{"plan":1}'),
        frozen_at=NOW,
    )
    artifact = MediaArtifact(
        artifact_id="artifact:1", plan_id=plan.plan_id, render_action_id="action:render:1",
        artifact_ref="sidecar:artifact:1", artifact_hash=ARTIFACT_HASH, attempts=1,
    )
    inspection = MediaInspectionRecord(
        inspection_id="inspection:1", plan_id=plan.plan_id, artifact_id=artifact.artifact_id,
        inspection_action_id="action:inspection:1", passed=True, reason_code="passed",
        inspection_payload_ref="sidecar:inspection:1", inspection_payload_hash="sha256:" + "2" * 64,
    )
    return ReducerState(
        media_opportunities=(opportunity,), media_plans=(plan,), media_artifacts=(artifact,),
        media_inspections=(inspection,),
    )


def _action(*, approval: MediaAutomaticDeliveryApproval, state: str = "delivered") -> Action:
    action_id = media_delivery_action_id(
        world_id=WORLD, approval_id=approval.approval_id, approval_revision=approval.entity_revision,
    )
    return Action(
        schema_version="world-v2.1", action_id=action_id, world_id=WORLD, logical_time=NOW,
        created_at=NOW, trace_id="trace:delivery", causation_id="cause:delivery",
        correlation_id="correlation:delivery", kind="media_delivery", layer="external_action",
        intent_ref=approval.inspection_id, actor="companion:girl", target="platform:user:1",
        payload_ref="sidecar:artifact:1", payload_hash=ARTIFACT_HASH,
        media_delivery_approval=MediaDeliveryApprovalBinding(
            approval_id=approval.approval_id, approval_revision=approval.entity_revision,
        ),
        idempotency_key=f"media-delivery:{approval.approval_id}:{approval.entity_revision}",
        budget_reservation_id=media_delivery_reservation_id(
            world_id=WORLD, approval_id=approval.approval_id, approval_revision=approval.entity_revision,
        ), claim_lease=(
            ClaimLease(owner_id="worker:delivery", attempt_id="attempt:delivery:1", acquired_at=NOW, expires_at=NOW + timedelta(minutes=1))
            if state in {"dispatch_started", "delivered", "failed", "unknown"} else None
        ), state=state, recovery_policy="effect_once",
    )


def test_operator_approval_is_revisioned_and_invalidates_not_dispatched_action() -> None:
    approval_1 = _approval()
    state = reduce_event(
        _state(), _event("MediaAutomaticDeliveryApproved", MediaAutomaticDeliveryApprovedPayload(approval=approval_1).model_dump(mode="json"), "approved-1"),
    )
    action = _action(approval=approval_1, state="authorized")
    assert require_current_media_delivery_approval(action=action, projection=_projection(state), logical_time=NOW) == approval_1

    approval_2 = _approval(revision=2, at=NOW + timedelta(minutes=1))
    state = reduce_event(
        state, _event("MediaAutomaticDeliveryApproved", MediaAutomaticDeliveryApprovedPayload(approval=approval_2).model_dump(mode="json"), "approved-2", at=approval_2.approved_at),
    )
    with pytest.raises(ValueError, match="stale"):
        require_current_media_delivery_approval(action=action, projection=_projection(state), logical_time=approval_2.approved_at)


def test_authorization_rejects_a_provider_target_not_bound_by_the_operator() -> None:
    approval = _approval().model_copy(update={"delivery_target_ref": "platform:user:1"})
    state = reduce_event(
        _state(), _event(
            "MediaAutomaticDeliveryApproved",
            {"approval": approval.model_dump(mode="json")},
            "approved-target",
        ),
    )

    class _Ledger:
        world_id = WORLD

        def project(self):  # type: ignore[no-untyped-def]
            return _projection(state)

        def commit_at_cursor(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return None

    runtime = MediaDeliveryRuntime(ledger=_Ledger())
    with pytest.raises(ValueError, match="target"):
        runtime.authorize_delivery(
            approval_id=approval.approval_id,
            approval_revision=approval.entity_revision,
            actor="companion:girl",
            target="platform:someone-else",
            account_id="account:image",
            amount_limit=1,
            logical_time=NOW,
            trace_id="trace:target-mismatch",
            correlation_id="correlation:target-mismatch",
        )


def test_share_materializes_only_after_delivered_receipt() -> None:
    approval = _approval()
    state = reduce_event(
        _state(), _event("MediaAutomaticDeliveryApproved", {"approval": approval.model_dump(mode="json")}, "approved"),
    )
    action = _action(approval=approval)
    receipt = ExecutionReceipt(
        receipt_id="receipt:delivery:1", result_id="result:delivery:1", action_id=action.action_id,
        provider="platform", provider_ref="platform:message:1", source_event_id="callback:1",
        receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0,
        received_at=NOW, raw_payload_hash="sha256:" + "3" * 64,
    )
    state = state.model_copy(update={"actions": (action,), "execution_receipts": (receipt,)})
    events = MediaDeliveryReceiptLifecycle().events_for_terminal_receipt(
        projection=_projection(state), action=action, receipt=receipt,
    )
    assert len(events) == 1 and events[0][0] == "MediaDeliveryShared"
    shared_event = _event(events[0][0], events[0][2], "shared")
    state = reduce_event(state, shared_event)
    assert state.media_deliveries[0].recipient_ref == "user:1"

    trigger_event = media_delivery_interaction_trigger_event(source_event=shared_event)
    state = reduce_event(state, trigger_event)
    assert state.trigger_processes[-1].trigger_id == media_delivery_interaction_trigger_id(
        world_id=WORLD, delivery_id=state.media_deliveries[0].delivery_id
    )

    failed = receipt.model_copy(update={"receipt_id": "receipt:delivery:failed", "observed_state": "failed"})
    assert MediaDeliveryReceiptLifecycle().events_for_terminal_receipt(
        projection=_projection(state), action=action.model_copy(update={"state": "failed"}), receipt=failed,
    ) == ()


def test_media_delivery_interaction_trigger_rejects_preview_or_unrelated_source() -> None:
    approval = _approval()
    state = reduce_event(
        _state(), _event("MediaAutomaticDeliveryApproved", {"approval": approval.model_dump(mode="json")}, "approved-preview"),
    )
    action = _action(approval=approval)
    receipt = ExecutionReceipt(
        receipt_id="receipt:delivery:preview", result_id="result:delivery:preview", action_id=action.action_id,
        provider="platform", provider_ref="platform:message:preview", source_event_id="callback:preview",
        receipt_kind="terminal", observed_state="delivered", is_terminal=True, cost_actual=0,
        received_at=NOW, raw_payload_hash="sha256:" + "6" * 64,
    )
    state = state.model_copy(update={"actions": (action,), "execution_receipts": (receipt,)})
    share_type, _, share_payload = MediaDeliveryReceiptLifecycle().events_for_terminal_receipt(
        projection=_projection(state), action=action, receipt=receipt,
    )[0]
    # A preview cannot impersonate the durable share authority even if it has
    # the same delivery-shaped payload.
    preview_source = _event("MediaPreviewGenerated", share_payload, "preview-source")
    with pytest.raises(ValueError, match="MediaDeliveryShared"):
        media_delivery_interaction_trigger_event(source_event=preview_source)


@pytest.mark.parametrize("status, shared", [("delivered", True), ("failed", False), ("unknown", False)])
def test_settlement_uow_only_derives_media_share_from_delivered_receipt(status: str, shared: bool) -> None:
    approval = _approval()
    state = reduce_event(_state(), _event(
        "MediaAutomaticDeliveryApproved", {"approval": approval.model_dump(mode="json")}, "approved-settlement",
    ))
    action = _action(approval=approval, state="dispatch_started")
    source_id = f"callback:{status}"
    trigger_id = f"trigger:settlement:platform:{source_id}"
    lease = ClaimLease(owner_id="runtime", attempt_id="attempt:settlement:1", acquired_at=NOW, expires_at=NOW + timedelta(minutes=1))
    state = state.model_copy(update={
        "actions": (action,),
        "pending_actions": (action,),
        "trigger_processes": (TriggerProcess(
            trigger_id=trigger_id, trigger_ref=f"result:platform:{source_id}", process_kind="settlement",
            state="claimed", claim_lease=lease, attempt_ids=(lease.attempt_id,),
        ),),
    })
    result = ExternalObservation(
        schema_version="world-v2.1", result_id=f"result:platform:{source_id}", world_id=WORLD,
        logical_time=NOW, created_at=NOW, trace_id="trace:delivery", causation_id=action.action_id,
        correlation_id="correlation:delivery", kind="execution_receipt", source="platform",
        source_event_id=source_id, action_id=action.action_id, idempotency_key=action.idempotency_key,
        status=status, provider_ref=f"platform:message:{status}", observed_at=NOW,
        cost_actual=0,
        raw_payload_hash="sha256:" + ("4" if status == "delivered" else "5") * 64,
    )
    plan = SettlementPlanner(world_id=WORLD).plan(result, trigger_id=trigger_id, projection=_projection(state))
    event_types = tuple(event.event_type for event in plan.events)
    assert ("MediaDeliveryShared" in event_types) is shared
    assert ("TriggerProcessOpened" in event_types) is shared
    if shared:
        delivery_index = event_types.index("MediaDeliveryShared")
        trigger = plan.events[delivery_index + 1]
        assert trigger.event_type == "TriggerProcessOpened"
        assert trigger.payload()["process"]["process_kind"] == "media_delivery_interaction"


def _projection(state: ReducerState):
    # The lifecycle is a pure projection reader.  Keeping this adapter local
    # makes the test prove it never reaches a ledger/reducer writer.
    from companion_daemon.world_v2.reducers import make_projection

    return make_projection(
        world_id=WORLD, world_revision=1, deliberation_revision=0, ledger_sequence=1, state=state,
    )
