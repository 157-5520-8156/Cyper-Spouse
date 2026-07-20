from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.batch_invariants import validate_commit_batch
from companion_daemon.world_v2.action_pump import ActionPump
from companion_daemon.world_v2.expression_lifecycle_runtime import ExpressionReceiptLifecycle
from companion_daemon.world_v2.minimal_reply_events import (
    ExpressionBeatSettledPayload,
    ExpressionBeatTerminatedPayload,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.settlement import SettlementPlanner
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    BudgetSettlement,
    ClaimLease,
    ExecutionReceipt,
    ExpressionBeatLifecycleEntry,
    ExpressionBeatProjection,
    ExpressionPlanLifecycleEntry,
    ExpressionPlanProjection,
    LedgerProjection,
    MinimalReplyManifestRef,
    ExternalObservation,
    TriggerProcess,
    WorldEvent,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:expression-lifecycle"


def _event(event_type: str, payload: dict[str, object], suffix: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:expression-lifecycle:{suffix}",
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="test",
        source="test",
        trace_id="trace:expression-lifecycle",
        causation_id="test",
        correlation_id="correlation:expression-lifecycle",
        idempotency_key=f"test:expression-lifecycle:{suffix}",
        payload=payload,
    )


def _action() -> Action:
    lease = ClaimLease(
        owner_id="test", attempt_id="attempt:expression:1", acquired_at=NOW, expires_at=NOW.replace(minute=2)
    )
    return Action(
        schema_version="world-v2.1",
        action_id="action:expression:1",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:expression-lifecycle",
        causation_id="acceptance:expression:1",
        correlation_id="correlation:expression-lifecycle",
        kind="reply",
        layer="external_action",
        intent_ref="proposal:expression:1:intent:1",
        actor="agent:companion",
        target="user:primary",
        payload_ref="payload:expression:1",
        payload_hash="sha256:" + "a" * 64,
        expression_plan_id="plan:expression:1",
        expression_beat_id="beat:expression:1",
        idempotency_key="action:expression:1",
        budget_reservation_id="reservation:expression:1",
        claim_lease=lease,
        state="delivered",
        recovery_policy="effect_once",
    )


def _receipt() -> ExecutionReceipt:
    return ExecutionReceipt(
        receipt_id="receipt:expression:1",
        result_id="result:expression:1",
        action_id="action:expression:1",
        provider="provider:test",
        provider_ref="provider-ref:expression:1",
        source_event_id="source:expression:1",
        receipt_kind="terminal",
        observed_state="delivered",
        is_terminal=True,
        cost_actual=0,
        received_at=NOW,
        raw_payload_hash="raw:expression:1",
    )


def _plan() -> ExpressionPlanProjection:
    return ExpressionPlanProjection(
        acceptance_id="acceptance:expression:1",
        proposal_id="proposal:expression:1",
        expression_change_id="change:expression:1",
        plan_id="plan:expression:1",
        event_ref="event:expression-lifecycle:plan-authorized",
        event_payload_hash="a" * 64,
        history=(
            ExpressionPlanLifecycleEntry(
                state="authorized",
                event_ref="event:expression-lifecycle:plan-authorized",
                event_payload_hash="a" * 64,
            ),
        ),
    )


def _beat() -> ExpressionBeatProjection:
    return ExpressionBeatProjection(
        acceptance_id="acceptance:expression:1",
        proposal_id="proposal:expression:1",
        expression_change_id="change:expression:1",
        plan_id="plan:expression:1",
        beat_id="beat:expression:1",
        payload_ref="payload:expression:1",
        payload_hash="sha256:" + "a" * 64,
        action_id="action:expression:1",
        cancel_policy="cancel-before-dispatch",
        reconsider_policy="reconsider-on-new-observation",
        merge_policy="never",
        event_ref="event:expression-lifecycle:beat-authorized",
        event_payload_hash="b" * 64,
        history=(
            ExpressionBeatLifecycleEntry(
                state="authorized",
                event_ref="event:expression-lifecycle:beat-authorized",
                event_payload_hash="b" * 64,
            ),
        ),
    )


def _projection() -> LedgerProjection:
    return LedgerProjection(
        world_id=WORLD,
        world_revision=0,
        deliberation_revision=0,
        ledger_sequence=0,
        semantic_hash="semantic:expression-lifecycle",
        expression_plans=(_plan(),),
        expression_beats=(_beat(),),
    )


def _manifest() -> MinimalReplyManifestRef:
    return MinimalReplyManifestRef(
        acceptance_id="acceptance:expression:1",
        proposal_id="proposal:expression:1",
        proposal_event_ref="event:proposal:expression:1",
        proposal_event_payload_hash="c" * 64,
        proposal_hash="sha256:" + "c" * 64,
        evaluated_world_revision=0,
        policy_digest="d" * 64,
        expression_change_id="change:expression:1",
        expression_change_hash="sha256:" + "e" * 64,
        intent_id="intent:1",
        intent_hash="f" * 64,
        plan_id="plan:expression:1",
        beat_id="beat:expression:1",
        message_payload_ref="payload:expression:1",
        message_payload_hash="sha256:" + "a" * 64,
        beat_hash="a" * 64,
        reservation_id="reservation:expression:1",
        reservation_hash="b" * 64,
        action_id="action:expression:1",
        action_hash="c" * 64,
        manifest_hash="d" * 64,
        acceptance_event_ref="event:acceptance:expression:1",
        acceptance_event_payload_hash="e" * 64,
        recorded_at_world_revision=1,
    )


def test_terminal_receipt_compiles_and_reduces_one_beat_lifecycle() -> None:
    receipt_event = _event("ExecutionReceiptRecorded", {"receipt": _receipt().model_dump(mode="json")}, "receipt")
    compiled = ExpressionReceiptLifecycle().events_for_terminal_receipt(
        projection=_projection(), action=_action(), receipt=_receipt(), receipt_event=receipt_event
    )

    assert [item.event_type for item in compiled] == ["ExpressionBeatSettled", "ExpressionPlanCompleted"]
    beat_event = _event(compiled[0].event_type, compiled[0].payload, compiled[0].suffix)
    plan_event = _event(compiled[1].event_type, compiled[1].payload, compiled[1].suffix)
    validate_commit_batch((receipt_event, beat_event, plan_event), expected_world_revision=0)

    state = ReducerState(
        actions=(_action(),),
        pending_actions=(),
        minimal_reply_manifests=(_manifest(),),
        expression_plans=(_plan(),),
        expression_beats=(_beat(),),
    )
    state = reduce_event(state, receipt_event)
    state = reduce_event(state, beat_event)
    state = reduce_event(state, plan_event)

    assert state.expression_beats[0].state == "settled"
    assert state.expression_beats[0].history[-1].receipt_id == _receipt().receipt_id
    assert state.expression_plans[0].state == "completed"
    assert state.expression_plans[0].history[-1].terminal_action_state == "delivered"


def test_lifecycle_rejects_tampered_receipt_binding() -> None:
    receipt_event = _event("ExecutionReceiptRecorded", {"receipt": _receipt().model_dump(mode="json")}, "receipt")
    compiled = ExpressionReceiptLifecycle().events_for_terminal_receipt(
        projection=_projection(), action=_action(), receipt=_receipt(), receipt_event=receipt_event
    )
    beat = ExpressionBeatSettledPayload.model_validate(compiled[0].payload).model_copy(
        update={"receipt_id": "receipt:other"}
    )
    beat_event = _event("ExpressionBeatSettled", beat.model_dump(mode="json"), "beat")

    with pytest.raises(ValueError, match="expression_lifecycle.beat_receipt_binding_invalid"):
        validate_commit_batch((receipt_event, beat_event), expected_world_revision=0)


def test_lifecycle_rejects_forged_plan_termination_source_hash() -> None:
    failed_action = _action().model_copy(update={"state": "failed"})
    failed_receipt = _receipt().model_copy(update={"observed_state": "failed"})
    receipt_event = _event(
        "ExecutionReceiptRecorded",
        {"receipt": failed_receipt.model_dump(mode="json")},
        "forged-termination-receipt",
    )
    compiled = ExpressionReceiptLifecycle().events_for_terminal_receipt(
        projection=_projection(),
        action=failed_action,
        receipt=failed_receipt,
        receipt_event=receipt_event,
    )
    beat_event = _event(compiled[0].event_type, compiled[0].payload, "forged-termination-beat")
    forged = dict(compiled[1].payload)
    forged["source_event_payload_hash"] = "0" * 64
    plan_event = _event("ExpressionPlanTerminated", forged, "forged-termination-plan")

    with pytest.raises(ValueError, match="termination_source_binding_invalid"):
        validate_commit_batch((receipt_event, beat_event, plan_event), expected_world_revision=0)


def test_multibeat_plan_completes_only_after_the_last_independent_receipt() -> None:
    first = _beat()
    second_action = _action().model_copy(
        update={
            "action_id": "action:expression:2",
            "expression_beat_id": "beat:expression:2",
            "state": "authorized",
            "claim_lease": None,
        }
    )
    second = _beat().model_copy(
        update={"beat_id": "beat:expression:2", "action_id": second_action.action_id, "dependency_beat_ids": ("beat:expression:1",)}
    )
    receipt = _receipt()
    receipt_event = _event("ExecutionReceiptRecorded", {"receipt": receipt.model_dump(mode="json")}, "receipt")
    projection = _projection().model_copy(update={"expression_beats": (first, second)})
    first_events = ExpressionReceiptLifecycle().events_for_terminal_receipt(
        projection=projection, action=_action(), receipt=receipt, receipt_event=receipt_event
    )
    assert [item.event_type for item in first_events] == ["ExpressionBeatSettled"]
    settled_first = first.model_copy(update={
        "state": "settled",
        "history": (*first.history, ExpressionBeatLifecycleEntry(
            state="settled", event_ref="event:expression-lifecycle:first-settled",
            event_payload_hash="f" * 64, receipt_id="receipt:expression:1",
            terminal_action_state="delivered",
        )),
    })
    second_receipt = receipt.model_copy(update={"receipt_id": "receipt:expression:2", "action_id": second_action.action_id})
    second_event = _event("ExecutionReceiptRecorded", {"receipt": second_receipt.model_dump(mode="json")}, "receipt-2")
    last_events = ExpressionReceiptLifecycle().events_for_terminal_receipt(
        projection=projection.model_copy(update={"expression_beats": (settled_first, second)}),
        action=second_action, receipt=second_receipt, receipt_event=second_event,
    )
    assert [item.event_type for item in last_events] == ["ExpressionBeatSettled", "ExpressionPlanCompleted"]


@pytest.mark.parametrize("terminal_state", ["failed", "unknown", "cancelled", "expired"])
def test_non_delivered_terminal_beat_terminates_but_never_completes_plan(
    terminal_state: str,
) -> None:
    failed_action = _action().model_copy(update={"state": terminal_state})
    failed_receipt = _receipt().model_copy(update={"observed_state": terminal_state})
    receipt_event = _event("ExecutionReceiptRecorded", {"receipt": failed_receipt.model_dump(mode="json")}, f"{terminal_state}-receipt")
    events = ExpressionReceiptLifecycle().events_for_terminal_receipt(
        projection=_projection(), action=failed_action, receipt=failed_receipt, receipt_event=receipt_event
    )
    assert [item.event_type for item in events] == [
        "ExpressionBeatSettled",
        "ExpressionPlanTerminated",
    ]

    beat_event = _event(events[0].event_type, events[0].payload, f"{terminal_state}-beat")
    plan_event = _event(events[1].event_type, events[1].payload, f"{terminal_state}-plan")
    validate_commit_batch((receipt_event, beat_event, plan_event), expected_world_revision=0)
    state = ReducerState(
        actions=(failed_action,),
        pending_actions=(),
        minimal_reply_manifests=(_manifest(),),
        expression_plans=(_plan(),),
        expression_beats=(_beat(),),
    )
    state = reduce_event(state, receipt_event)
    state = reduce_event(state, beat_event)
    state = reduce_event(state, plan_event)
    assert state.expression_beats[0].state == "settled"
    assert state.expression_plans[0].state == "terminated"
    assert state.expression_plans[0].history[-1].terminal_disposition == terminal_state


def test_failed_multibeat_plan_abandons_undispatched_siblings_without_false_delivery() -> None:
    first = _beat()
    second_action = _action().model_copy(
        update={
            "action_id": "action:expression:2",
            "expression_beat_id": "beat:expression:2",
            "state": "authorized",
            "claim_lease": None,
        }
    )
    second = _beat().model_copy(
        update={"beat_id": "beat:expression:2", "action_id": second_action.action_id}
    )
    failed_action = _action().model_copy(update={"state": "failed"})
    failed_receipt = _receipt().model_copy(update={"observed_state": "failed"})
    receipt_event = _event(
        "ExecutionReceiptRecorded",
        {"receipt": failed_receipt.model_dump(mode="json")},
        "multibeat-failed-receipt",
    )
    projection = _projection().model_copy(update={"expression_beats": (first, second)})
    compiled = ExpressionReceiptLifecycle().events_for_terminal_receipt(
        projection=projection,
        action=failed_action,
        receipt=failed_receipt,
        receipt_event=receipt_event,
    )
    beat_event = _event(compiled[0].event_type, compiled[0].payload, "multibeat-failed-beat")
    plan_event = _event(compiled[1].event_type, compiled[1].payload, "multibeat-failed-plan")
    cancel_event = _event(
        "ActionCancelled", {"action_id": second_action.action_id}, "multibeat-sibling-cancel"
    )
    beat_terminated_payload = ExpressionBeatTerminatedPayload(
        acceptance_id=second.acceptance_id,
        proposal_id=second.proposal_id,
        plan_id=second.plan_id,
        beat_id=second.beat_id,
        action_id=second_action.action_id,
        disposition="cancelled",
        source_event_ref=cancel_event.event_id,
        source_event_payload_hash=cancel_event.payload_hash,
    )
    beat_terminated_event = _event(
        "ExpressionBeatTerminated",
        beat_terminated_payload.model_dump(mode="json"),
        "multibeat-sibling-beat-terminated",
    )
    sibling_reservation = BudgetReservation(
        reservation_id=second_action.budget_reservation_id,
        account_id="account:chat",
        action_id=second_action.action_id,
        category="chat",
        amount_limit=10,
    )
    budget_release = _event(
        "BudgetReleased",
        {
            "settlement": BudgetSettlement(
                settlement_id="settlement:multibeat-sibling",
                reservation_id=sibling_reservation.reservation_id,
                action_id=second_action.action_id,
                result_id="result:multibeat-sibling-cancel",
                state="released",
                previous_cost=0,
                cost_actual=0,
                cost_delta=0,
            ).model_dump(mode="json")
        },
        "multibeat-sibling-budget-released",
    )
    validate_commit_batch(
        (
            receipt_event,
            beat_event,
            cancel_event,
            beat_terminated_event,
            budget_release,
            plan_event,
        ),
        expected_world_revision=0,
    )
    state = ReducerState(
        actions=(failed_action, second_action),
        pending_actions=(second_action,),
        budget_accounts=(
            BudgetAccount(
                account_id="account:chat",
                category="chat",
                window_id="window:1",
                limit=100,
                reserved=10,
            ),
        ),
        budget_reservations=(sibling_reservation,),
        minimal_reply_manifests=(_manifest(),),
        expression_plans=(_plan(),),
        expression_beats=(first, second),
    )
    state = reduce_event(state, receipt_event)
    state = reduce_event(state, beat_event)
    state = reduce_event(state, cancel_event)
    state = reduce_event(state, beat_terminated_event)
    state = reduce_event(state, budget_release)
    state = reduce_event(state, plan_event)

    assert [beat.state for beat in state.expression_beats] == ["settled", "terminated"]
    assert state.expression_beats[1].history[-1].receipt_id is None
    assert state.expression_beats[1].history[-1].terminal_disposition == "cancelled"
    assert state.expression_plans[0].state == "terminated"
    assert state.actions[1].state == "cancelled"
    assert state.pending_actions == ()
    assert state.budget_reservations[0].state == "released"
    terminal_projection = _projection().model_copy(
        update={
            "expression_plans": (state.expression_plans[0],),
            "expression_beats": tuple(state.expression_beats),
        }
    )
    assert not ActionPump._expression_dispatch_allowed(state.actions[1], terminal_projection)


def test_inflight_sibling_receipt_settles_after_plan_termination_without_reopening_it() -> None:
    in_flight_action = _action().model_copy(
        update={
            "action_id": "action:expression:inflight",
            "expression_beat_id": "beat:expression:inflight",
            "state": "delivered",
        }
    )
    in_flight_beat = _beat().model_copy(
        update={
            "beat_id": "beat:expression:inflight",
            "action_id": in_flight_action.action_id,
        }
    )
    terminated_plan = _plan().model_copy(
        update={
            "state": "terminated",
            "history": (
                *_plan().history,
                ExpressionPlanLifecycleEntry(
                    state="terminated",
                    event_ref="event:expression:plan-terminated",
                    event_payload_hash="9" * 64,
                    receipt_id="receipt:failed-earlier-beat",
                    terminal_action_state="failed",
                    terminal_disposition="failed",
                ),
            ),
        }
    )
    receipt = _receipt().model_copy(
        update={
            "receipt_id": "receipt:expression:inflight",
            "action_id": in_flight_action.action_id,
        }
    )
    receipt_event = _event(
        "ExecutionReceiptRecorded",
        {"receipt": receipt.model_dump(mode="json")},
        "inflight-receipt-after-plan-terminal",
    )

    events = ExpressionReceiptLifecycle().events_for_terminal_receipt(
        projection=_projection().model_copy(
            update={
                "expression_plans": (terminated_plan,),
                "expression_beats": (in_flight_beat,),
            }
        ),
        action=in_flight_action,
        receipt=receipt,
        receipt_event=receipt_event,
    )

    assert [event.event_type for event in events] == ["ExpressionBeatSettled"]


def test_settlement_planner_explicitly_retires_undispatched_multibeat_siblings() -> None:
    failed_action = _action().model_copy(update={"state": "dispatch_started"})
    sibling_action = _action().model_copy(
        update={
            "action_id": "action:expression:sibling",
            "expression_beat_id": "beat:expression:sibling",
            "budget_reservation_id": "reservation:expression:sibling",
            "state": "authorized",
            "claim_lease": None,
        }
    )
    sibling_beat = _beat().model_copy(
        update={
            "beat_id": sibling_action.expression_beat_id,
            "action_id": sibling_action.action_id,
        }
    )
    sibling_reservation = BudgetReservation(
        reservation_id=sibling_action.budget_reservation_id,
        account_id="account:chat",
        action_id=sibling_action.action_id,
        category="chat",
        amount_limit=10,
    )
    trigger_id = "trigger:settlement:provider:test:source:expression:failed"
    projection = _projection().model_copy(
        update={
            "actions": (failed_action, sibling_action),
            "pending_actions": (failed_action, sibling_action),
            "expression_beats": (_beat(), sibling_beat),
            "budget_reservations": (sibling_reservation,),
            "trigger_processes": (
                TriggerProcess(
                    trigger_id=trigger_id,
                    trigger_ref="result:expression:failed",
                    process_kind="settlement",
                    state="claimed",
                    claim_lease=ClaimLease(
                        owner_id="test",
                        attempt_id="attempt:settlement:failed",
                        acquired_at=NOW,
                        expires_at=NOW + timedelta(minutes=2),
                    ),
                    attempt_ids=("attempt:settlement:failed",),
                ),
            ),
        }
    )
    result = ExternalObservation(
        schema_version="world-v2.1",
        result_id="result:expression:failed",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:expression-lifecycle",
        causation_id="cause:expression:failed",
        correlation_id="correlation:expression-lifecycle",
        kind="execution_receipt",
        source="provider:test",
        source_event_id="source:expression:failed",
        action_id=failed_action.action_id,
        idempotency_key=failed_action.idempotency_key,
        status="failed",
        provider_ref="provider-ref:expression:failed",
        cost_actual=0,
        error_class="provider_rejected",
        observed_at=NOW,
        raw_payload_hash="raw:expression:failed",
    )

    planned = SettlementPlanner(world_id=WORLD).plan(
        result, trigger_id=trigger_id, projection=projection
    )
    event_types = tuple(event.event_type for event in planned.events)

    assert event_types[:8] == (
        "ActionFailed",
        "ExecutionReceiptRecorded",
        "ExpressionBeatSettled",
        "ActionCancelled",
        "ExpressionBeatTerminated",
        "BudgetReleased",
        "ExpressionPlanTerminated",
        "BudgetSettled",
    )
    validate_commit_batch(planned.events, expected_world_revision=0)


def test_settlement_planner_appends_lifecycle_as_one_receipt_suffix() -> None:
    action = _action().model_copy(update={"state": "dispatch_started"})
    trigger_id = "trigger:settlement:provider:test:source:expression:1"
    projection = _projection().model_copy(
        update={
            "actions": (action,),
            "pending_actions": (action,),
            "trigger_processes": (
                TriggerProcess(
                    trigger_id=trigger_id,
                    trigger_ref="result:expression:1",
                    process_kind="settlement",
                    state="claimed",
                    claim_lease=ClaimLease(
                        owner_id="test",
                        attempt_id="attempt:settlement:1",
                        acquired_at=NOW,
                        expires_at=NOW + timedelta(minutes=2),
                    ),
                    attempt_ids=("attempt:settlement:1",),
                ),
            ),
        }
    )
    result = ExternalObservation(
        schema_version="world-v2.1",
        result_id="result:expression:1",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:expression-lifecycle",
        causation_id="cause:expression:1",
        correlation_id="correlation:expression-lifecycle",
        kind="execution_receipt",
        source="provider:test",
        source_event_id="source:expression:1",
        action_id=action.action_id,
        idempotency_key=action.idempotency_key,
        status="delivered",
        provider_ref="provider-ref:expression:1",
        cost_actual=0,
        observed_at=NOW,
        raw_payload_hash="raw:expression:1",
    )

    plan = SettlementPlanner(world_id=WORLD).plan(
        result, trigger_id=trigger_id, projection=projection
    )

    assert tuple(event.event_type for event in plan.events[:4]) == (
        "ActionDelivered",
        "ExecutionReceiptRecorded",
        "ExpressionBeatSettled",
        "ExpressionPlanCompleted",
    )
    validate_commit_batch(plan.events, expected_world_revision=0)
