from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from .action_lifecycle import settlement_event_type, transition_action
from .errors import InvalidActionTransition
from .schemas import (
    Action,
    ActionReconciliation,
    ActionState,
    BudgetSettlement,
    ClaimLease,
    ExecutionReceipt,
    ExternalObservation,
    LedgerProjection,
    TriggerProcess,
    WorldEvent,
)


ReconciliationReason = Literal[
    "unknown_action", "identity_mismatch", "terminal_conflict", "invalid_transition"
]


@dataclass(frozen=True, slots=True)
class SettlementPlan:
    events: tuple[WorldEvent, ...]
    runtime_status: Literal["observed_only", "action_executed", "deferred"]
    deferred_ref: str | None
    projection_hint: str


class SettlementPlanner:
    """Pure settlement domain module; callers only orchestrate the two ledger commits."""

    def __init__(self, *, world_id: str) -> None:
        self._world_id = world_id

    def recording_events(
        self, result: ExternalObservation, *, trigger_id: str
    ) -> tuple[WorldEvent, ...]:
        attempt_id = f"attempt:{trigger_id}:1"
        process = TriggerProcess(
            trigger_id=trigger_id,
            trigger_ref=result.result_id,
            process_kind="settlement",
            state="claimed",
            claim_lease=ClaimLease(
                owner_id=f"world-runtime:settlement:{attempt_id}",
                attempt_id=attempt_id,
                acquired_at=result.observed_at,
                expires_at=result.observed_at + timedelta(minutes=2),
            ),
            attempt_ids=(attempt_id,),
        )
        return (
            self._event(
                result,
                trigger_id=trigger_id,
                event_type="ExternalObservationRecorded",
                suffix="inbox",
                payload={"result": result.model_dump(mode="json")},
            ),
            self._event(
                result,
                trigger_id=trigger_id,
                event_type="TriggerProcessClaimed",
                suffix="process-claimed",
                payload={"process": process.model_dump(mode="json")},
            ),
        )

    def plan(
        self,
        result: ExternalObservation,
        *,
        trigger_id: str,
        projection: LedgerProjection,
    ) -> SettlementPlan:
        prior_receipt = next(
            (
                receipt
                for receipt in projection.execution_receipts
                if receipt.result_id == result.result_id
            ),
            None,
        )
        prior_reconciliation = next(
            (
                reconciliation
                for reconciliation in projection.reconciliations
                if reconciliation.result_id == result.result_id
            ),
            None,
        )
        action = next(
            (
                candidate
                for candidate in projection.actions
                if candidate.action_id == result.action_id
            ),
            None,
        )
        process = next(
            (
                candidate
                for candidate in projection.trigger_processes
                if candidate.trigger_id == trigger_id
            ),
            None,
        )
        if process is None:
            raise ValueError(f"settlement trigger {trigger_id!r} was not claimed")
        equivalent_receipt = next(
            (
                existing
                for existing in projection.execution_receipts
                if existing.provider == result.source
                and existing.provider_ref == result.provider_ref
                and existing.raw_payload_hash == result.raw_payload_hash
            ),
            None,
        )
        if equivalent_receipt is not None and prior_receipt is None:
            return self._duplicate_provider_plan(
                result, trigger_id=trigger_id, projection=projection, process=process
            )
        reason = self._reconciliation_reason(
            result,
            action=action,
            projection=projection,
            prior_receipt=prior_receipt,
            prior_reconciliation=prior_reconciliation,
        )
        terminal = result.status != "provider_accepted"
        receipt = ExecutionReceipt(
            receipt_id=f"receipt:{result.source}:{result.source_event_id}",
            result_id=result.result_id,
            action_id=result.action_id,
            provider=result.source,
            provider_ref=result.provider_ref,
            source_event_id=result.source_event_id,
            receipt_kind="terminal" if terminal else "ack",
            observed_state=result.status,
            is_terminal=terminal,
            artifact_refs=result.artifact_refs,
            cost_actual=result.cost_actual,
            error_class=result.error_class,
            received_at=result.observed_at,
            raw_payload_hash=result.raw_payload_hash,
        )
        if reason is None:
            if action is None:
                raise AssertionError("normal settlement requires an Action")
            events = self._normal_events(
                result,
                trigger_id=trigger_id,
                receipt=receipt,
                budget_reservation_id=action.budget_reservation_id,
            )
            deferred_ref = None
            runtime_status = "action_executed"
            hint = f"action:{result.action_id}:{result.status}"
        else:
            events = self._reconciliation_events(
                result,
                trigger_id=trigger_id,
                projection=projection,
                receipt=receipt,
                reason=reason,
                existing_state=action.state if action is not None else None,
                budget_reservation_id=(
                    action.budget_reservation_id if action is not None else None
                ),
            )
            deferred_ref = f"reconciliation:{result.source}:{result.source_event_id}"
            runtime_status = "deferred"
            hint = deferred_ref
        completion_events = self._completion_events(
            result,
            trigger_id=trigger_id,
            projection=projection,
            process=process,
        )
        return SettlementPlan(
            events=(*events, *completion_events),
            runtime_status=runtime_status,
            deferred_ref=deferred_ref,
            projection_hint=hint,
        )

    def _reconciliation_reason(
        self,
        result: ExternalObservation,
        *,
        action: Action | None,
        projection: LedgerProjection,
        prior_receipt: ExecutionReceipt | None,
        prior_reconciliation: ActionReconciliation | None,
    ) -> ReconciliationReason | None:
        if prior_reconciliation is not None:
            return prior_reconciliation.reason
        if prior_receipt is not None:
            if (
                prior_receipt.action_id == result.action_id
                and prior_receipt.provider == result.source
                and prior_receipt.provider_ref == result.provider_ref
                and prior_receipt.source_event_id == result.source_event_id
                and prior_receipt.raw_payload_hash == result.raw_payload_hash
                and prior_receipt.observed_state == result.status
            ):
                return None
            return "identity_mismatch"
        if action is None:
            return "unknown_action"
        if action.idempotency_key != result.idempotency_key:
            return "identity_mismatch"
        if any(
            receipt.provider == result.source
            and receipt.provider_ref == result.provider_ref
            and receipt.raw_payload_hash != result.raw_payload_hash
            for receipt in projection.execution_receipts
        ):
            return "identity_mismatch"
        try:
            transition_action(action, result.status)
        except InvalidActionTransition:
            if action.state in {"delivered", "failed", "unknown", "cancelled", "expired"}:
                return "terminal_conflict"
            return "invalid_transition"
        return None

    def _normal_events(
        self,
        result: ExternalObservation,
        *,
        trigger_id: str,
        receipt: ExecutionReceipt,
        budget_reservation_id: str,
    ) -> tuple[WorldEvent, ...]:
        events = [
            self._event(
                result,
                trigger_id=trigger_id,
                event_type=settlement_event_type(result.status),
                suffix="action-state",
                payload=result.model_dump(mode="json"),
            ),
            self._receipt_event(result, trigger_id=trigger_id, receipt=receipt),
        ]
        if receipt.is_terminal:
            budget = BudgetSettlement(
                settlement_id=f"budget-settlement:{result.source}:{result.source_event_id}",
                reservation_id=budget_reservation_id,
                action_id=result.action_id,
                result_id=result.result_id,
                state=(
                    "released" if result.status in {"cancelled", "expired"} else "settled"
                ),
                previous_cost=0,
                cost_actual=result.cost_actual,
                cost_delta=result.cost_actual,
            )
            events.append(
                self._event(
                    result,
                    trigger_id=trigger_id,
                    event_type=(
                        "BudgetReleased" if budget.state == "released" else "BudgetSettled"
                    ),
                    suffix="budget",
                    payload={"settlement": budget.model_dump(mode="json")},
                )
            )
        return tuple(events)

    def _reconciliation_events(
        self,
        result: ExternalObservation,
        *,
        trigger_id: str,
        projection: LedgerProjection,
        receipt: ExecutionReceipt,
        reason: ReconciliationReason,
        existing_state: ActionState | None,
        budget_reservation_id: str | None,
    ) -> tuple[WorldEvent, ...]:
        reconciliation = ActionReconciliation(
            reconciliation_id=(
                f"reconciliation:{result.source}:{result.source_event_id}"
            ),
            result_id=result.result_id,
            action_id=result.action_id,
            reason=reason,
            observed_state=result.status,
            existing_state=existing_state,
            provider=result.source,
            provider_ref=result.provider_ref,
            raw_payload_hash=result.raw_payload_hash,
        )
        events = [
            self._receipt_event(result, trigger_id=trigger_id, receipt=receipt),
            self._event(
                result,
                trigger_id=trigger_id,
                event_type="ActionReconciliationRequired",
                suffix="reconciliation",
                payload={"reconciliation": reconciliation.model_dump(mode="json")},
            ),
        ]
        if budget_reservation_id is not None and existing_state in {
            "delivered",
            "failed",
            "unknown",
            "cancelled",
            "expired",
        }:
            reservation = next(
                (
                    item
                    for item in projection.budget_reservations
                    if item.reservation_id == budget_reservation_id
                ),
                None,
            )
            if reservation is not None and reservation.settled_cost != result.cost_actual:
                adjustment = BudgetSettlement(
                    settlement_id=f"budget-adjustment:{result.source}:{result.source_event_id}",
                    reservation_id=budget_reservation_id,
                    action_id=result.action_id,
                    result_id=result.result_id,
                    state="settled",
                    settlement_kind="reconciliation_adjustment",
                    previous_cost=reservation.settled_cost,
                    cost_actual=result.cost_actual,
                    cost_delta=result.cost_actual - reservation.settled_cost,
                )
                events.append(
                    self._event(
                        result,
                        trigger_id=trigger_id,
                        event_type="BudgetAdjusted",
                        suffix="budget-adjustment",
                        payload={"settlement": adjustment.model_dump(mode="json")},
                    )
                )
        return tuple(events)

    def _duplicate_provider_plan(
        self,
        result: ExternalObservation,
        *,
        trigger_id: str,
        projection: LedgerProjection,
        process: TriggerProcess,
    ) -> SettlementPlan:
        completion_events = self._completion_events(
            result,
            trigger_id=trigger_id,
            projection=projection,
            process=process,
        )
        return SettlementPlan(
            events=completion_events,
            runtime_status="observed_only",
            deferred_ref=None,
            projection_hint=f"duplicate-receipt:{result.provider_ref}",
        )

    def _completion_events(
        self,
        result: ExternalObservation,
        *,
        trigger_id: str,
        projection: LedgerProjection,
        process: TriggerProcess,
    ) -> tuple[WorldEvent, ...]:
        current_time = max(
            result.observed_at,
            projection.logical_time or result.observed_at,
        )
        events: list[WorldEvent] = []
        active_process = process
        if process.state == "claimed" and current_time > process.claim_lease.expires_at:
            attempt_id = f"attempt:{trigger_id}:{len(process.attempt_ids) + 1}"
            active_process = process.model_copy(
                update={
                    "claim_lease": ClaimLease(
                        owner_id=f"world-runtime:settlement:{attempt_id}",
                        attempt_id=attempt_id,
                        acquired_at=current_time,
                        expires_at=current_time + timedelta(minutes=2),
                    ),
                    "attempt_ids": (*process.attempt_ids, attempt_id),
                }
            )
            events.append(
                self._event(
                    result,
                    trigger_id=trigger_id,
                    event_type="TriggerProcessReclaimed",
                    suffix=f"process-reclaimed-{len(active_process.attempt_ids)}",
                    payload={"process": active_process.model_dump(mode="json")},
                )
            )
        completed_at = max(current_time, active_process.claim_lease.acquired_at)
        if completed_at > active_process.claim_lease.expires_at:
            raise ValueError("active trigger lease expired before completion")
        events.extend(
            [
                self._event(
                    result,
                    trigger_id=trigger_id,
                    event_type="ExternalObservationProcessed",
                    suffix="processed",
                    payload={"result_id": result.result_id},
                ),
                self._event(
                    result,
                    trigger_id=trigger_id,
                    event_type="TriggerProcessCompleted",
                    suffix="completed",
                    payload={
                        "trigger_id": trigger_id,
                        "owner_id": active_process.claim_lease.owner_id,
                        "attempt_id": active_process.claim_lease.attempt_id,
                        "completed_at": completed_at.isoformat(),
                        "runtime_outcome_ref": f"outcome:{trigger_id}",
                    },
                ),
            ]
        )
        return tuple(events)

    def _receipt_event(
        self,
        result: ExternalObservation,
        *,
        trigger_id: str,
        receipt: ExecutionReceipt,
    ) -> WorldEvent:
        return self._event(
            result,
            trigger_id=trigger_id,
            event_type="ExecutionReceiptRecorded",
            suffix="execution-receipt",
            payload={"receipt": receipt.model_dump(mode="json")},
        )

    def _event(
        self,
        result: ExternalObservation,
        *,
        trigger_id: str,
        event_type: str,
        suffix: str,
        payload: dict[str, object],
    ) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version=result.schema_version,
            event_id=f"event:{trigger_id}:{suffix}",
            world_id=self._world_id,
            event_type=event_type,
            logical_time=result.logical_time,
            created_at=result.created_at,
            actor=f"provider:{result.source}",
            source=result.source,
            trace_id=result.trace_id,
            causation_id=result.causation_id,
            correlation_id=result.correlation_id,
            idempotency_key=(
                f"settlement:{result.source}:{result.source_event_id}:{suffix}"
            ),
            payload=payload,
        )
