"""Deterministic receipt-to-expression lifecycle projection.

This is deliberately not a policy engine.  The model has already chosen and
Acceptance has already authorized an expression plan.  Once a provider emits a
terminal receipt, this module can only advance the matching beat head and, for
the currently supported single-beat plan, complete its plan head.  It never
creates text, chooses a next beat, or performs an external effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .minimal_reply_events import (
    ExpressionBeatSettledPayload,
    ExpressionPlanCompletedPayload,
)
from .schemas import Action, ExecutionReceipt, LedgerProjection, WorldEvent


ExpressionLifecycleEventType = Literal["ExpressionBeatSettled", "ExpressionPlanCompleted"]


@dataclass(frozen=True, slots=True)
class ExpressionLifecycleEvent:
    event_type: ExpressionLifecycleEventType
    suffix: str
    payload: dict[str, object]


class ExpressionReceiptLifecycle:
    """Compile terminal receipt authority into closed lifecycle event payloads."""

    def events_for_terminal_receipt(
        self,
        *,
        projection: LedgerProjection,
        action: Action,
        receipt: ExecutionReceipt,
        receipt_event: WorldEvent,
    ) -> tuple[ExpressionLifecycleEvent, ...]:
        if action.expression_plan_id is None:
            return ()
        if not receipt.is_terminal:
            return ()
        plan_id = action.expression_plan_id
        beat_id = action.expression_beat_id
        assert beat_id is not None
        plan = next((item for item in projection.expression_plans if item.plan_id == plan_id), None)
        beat = next((item for item in projection.expression_beats if item.beat_id == beat_id), None)
        if plan is None or beat is None:
            raise ValueError("expression Action is missing its durable plan or beat")
        if (
            beat.plan_id != plan_id
            or beat.action_id != action.action_id
            or receipt.action_id != action.action_id
        ):
            raise ValueError("terminal receipt does not bind an expression beat")
        terminal_state = receipt.observed_state
        if terminal_state == "provider_accepted":  # defensive; receipt schema already rejects it
            raise ValueError("expression lifecycle requires terminal receipt")
        if beat.state == "settled":
            history = beat.history[-1] if beat.history else None
            if (
                history is None
                or history.receipt_id != receipt.receipt_id
                or history.terminal_action_state != terminal_state
            ):
                raise ValueError("terminal receipt conflicts with settled expression beat")
        elif beat.state != "authorized":
            raise ValueError("terminal receipt does not settle an authorized expression beat")
        if plan.state == "completed":
            history = plan.history[-1] if plan.history else None
            if (
                history is None
                or history.receipt_id != receipt.receipt_id
                or history.terminal_action_state != terminal_state
            ):
                raise ValueError("terminal receipt conflicts with completed expression plan")
        elif plan.state != "authorized":
            raise ValueError("terminal receipt does not settle an active expression plan")
        settled = ExpressionBeatSettledPayload(
            acceptance_id=beat.acceptance_id,
            proposal_id=beat.proposal_id,
            plan_id=plan_id,
            beat_id=beat_id,
            action_id=action.action_id,
            receipt_id=receipt.receipt_id,
            receipt_event_ref=receipt_event.event_id,
            receipt_event_payload_hash=receipt_event.payload_hash,
            terminal_action_state=terminal_state,
        )
        events: list[ExpressionLifecycleEvent] = [
            ExpressionLifecycleEvent(
                event_type="ExpressionBeatSettled",
                suffix="expression-beat-settled",
                payload=settled.model_dump(mode="json"),
            )
        ]
        # This vertical intentionally supports exactly the minimal lane's one
        # beat.  A later scheduler can add additional beats and complete only
        # after their individual receipt settlements; this compiler must not
        # invent that policy.
        plan_beats = tuple(item for item in projection.expression_beats if item.plan_id == plan_id)
        if len(plan_beats) == 1:
            completed = ExpressionPlanCompletedPayload(
                acceptance_id=plan.acceptance_id,
                proposal_id=plan.proposal_id,
                plan_id=plan_id,
                terminal_beat_id=beat_id,
                receipt_id=receipt.receipt_id,
                receipt_event_ref=receipt_event.event_id,
                receipt_event_payload_hash=receipt_event.payload_hash,
                terminal_action_state=terminal_state,
            )
            events.append(
                ExpressionLifecycleEvent(
                    event_type="ExpressionPlanCompleted",
                    suffix="expression-plan-completed",
                    payload=completed.model_dump(mode="json"),
                )
            )
        return tuple(events)


__all__ = ["ExpressionLifecycleEvent", "ExpressionReceiptLifecycle"]
