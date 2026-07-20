"""Recovery-safe deliberation gate for user-interrupted expression beats.

This runtime intentionally has a tiny authority surface.  It may claim a
source-bound trigger and, only after a reviewer explicitly returns ``continue``,
close that gate.  It cannot alter a frozen payload or dispatch an Action.  An
absent/ambiguous reviewer leaves the gate durable and active, which is the safe
default: the Action pump will not send the old beat.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from typing import Literal, Protocol

from .event_identity import domain_idempotency_key
from .minimal_reply_events import (
    ExpressionBeatTerminatedPayload,
    ExpressionPlanTerminatedPayload,
)
from .schema_core import FrozenModel
from .schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


ExpressionReconsiderationDisposition = Literal[
    "continue", "cancel", "merge", "defer", "supersede", "new_beat"
]


class ExpressionReconsiderationDecision(FrozenModel):
    """An LLM-facing, closed decision for one frozen beat.

    ``replacement_plan_ref`` is deliberately opaque.  A reviewer may point to
    an already-audited follow-up ExpressionPlan proposal, but it cannot inject
    prose or mutate this beat's payload through this decision.  A composition
    root is responsible for accepting that new plan through the normal
    ExpressionPlan acceptance capability.
    """

    disposition: ExpressionReconsiderationDisposition
    rationale_ref: str | None = None
    replacement_plan_ref: str | None = None

    def requires_replacement(self) -> bool:
        return self.disposition in {"merge", "supersede", "new_beat"}


class ExpressionReconsiderationReviewer(Protocol):
    """Optional LLM/semantic reviewer hook.

    Returning ``None`` means no explicit acceptance.  Replacement dispositions
    may only reference a separately audited plan; this worker never accepts
    arbitrary generated text or changes an already-dispatched payload.
    """

    async def review(
        self,
        *,
        process: TriggerProcess,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
    ) -> ExpressionReconsiderationDecision | ExpressionReconsiderationDisposition | None: ...


class ExpressionReconsiderationRunResult(FrozenModel):
    trigger_id: str
    status: Literal[
        "idle", "owned_elsewhere", "awaiting_review", "continued", "cancelled",
        "replacement_required", "deferred", "moot",
    ]
    disposition: ExpressionReconsiderationDisposition | None = None
    replacement_plan_ref: str | None = None


class ExpressionReconsiderationRuntime:
    """Claim/reclaim one gate without granting old-payload dispatch by default."""

    def __init__(
        self,
        *,
        ledger,
        owner_id: str,
        reviewer: ExpressionReconsiderationReviewer | None = None,
        lease_seconds: int = 120,
        source: str = "world-v2:expression-reconsideration-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("expression reconsideration runtime needs owner and positive lease")
        self._ledger = ledger
        self._owner_id = owner_id
        self._reviewer = reviewer
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> ExpressionReconsiderationRunResult:
        # A crash may occur after the atomic Action/budget/gate cancellation
        # and before its separately-deliberated commitment release.  Repair
        # that bounded gap before looking for new work.
        await self._recover_cancelled_commitments()
        projection = await self._project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "expression_reconsideration" and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return ExpressionReconsiderationRunResult(trigger_id="", status="idle")
        source = await self._lookup(process.source_evidence_ref or "")
        if source is None or source[0].event_type != "ObservationRecorded":
            raise ValueError("expression reconsideration source observation is unavailable")
        active = await self._claim_or_reclaim(process=process, source_event=source[0], projection=projection)
        if active is None:
            return ExpressionReconsiderationRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")
        moot_reason = self._moot_reason(
            process=active, projection=await self._project()
        )
        if moot_reason is not None:
            # The gate's frozen beat can no longer be retired through this
            # process: its Action already reached a provider, settled, or was
            # cancelled/terminated by an earlier decision on the same plan.
            # There is no semantic judgement left to ask a reviewer for, so
            # complete deterministically (recorded as a moot continuation)
            # instead of wedging the whole lane on an impossible cancellation.
            # This is exactly how a backlog of stale gates drains after the
            # reviewer was absent for a while: the first real decision retires
            # the beat and every later gate on it becomes moot.
            await self._complete(
                process=active,
                source_event=source[0],
                decision=ExpressionReconsiderationDecision(
                    disposition="continue",
                    rationale_ref=f"moot-gate:{moot_reason}",
                ),
            )
            return ExpressionReconsiderationRunResult(
                trigger_id=active.trigger_id, status="moot", disposition="continue"
            )
        if self._reviewer is None:
            return ExpressionReconsiderationRunResult(
                trigger_id=active.trigger_id, status="awaiting_review"
            )
        reviewed = await self._reviewer.review(
            process=active, observation_event=source[0], cursor=self._cursor(await self._project())
        )
        decision = self._normalize_decision(reviewed)
        if decision is None:
            return ExpressionReconsiderationRunResult(
                trigger_id=active.trigger_id, status="awaiting_review"
            )
        if decision.disposition == "continue":
            await self._complete(process=active, source_event=source[0], decision=decision)
            return ExpressionReconsiderationRunResult(
                trigger_id=active.trigger_id, status="continued", disposition=decision.disposition
            )
        if decision.disposition == "defer":
            # Completion makes the decision durable, while the gate helper
            # keeps this particular frozen beat non-dispatchable until a later
            # source-bound observation opens a fresh reconsideration trigger.
            await self._complete(process=active, source_event=source[0], decision=decision)
            return ExpressionReconsiderationRunResult(
                trigger_id=active.trigger_id, status="deferred", disposition=decision.disposition
            )
        await self._replace_or_cancel(process=active, source_event=source[0], decision=decision)
        return ExpressionReconsiderationRunResult(
            trigger_id=active.trigger_id,
            status="replacement_required" if decision.requires_replacement() else "cancelled",
            disposition=decision.disposition,
            replacement_plan_ref=decision.replacement_plan_ref,
        )

    @classmethod
    def _moot_reason(cls, *, process: TriggerProcess, projection) -> str | None:
        """Name why no reconsideration decision remains, or ``None`` if one does.

        Mirrors the trigger-opening eligibility: a gate only means something
        while its beat is authorized and its Action has not been handed to a
        provider.  Anything else is evidence the beat already settled or was
        retired, and only the terminal-state name is recorded.
        """

        lineage = cls._lineage(process)
        beat = next(
            (
                item
                for item in projection.expression_beats
                if item.beat_id == lineage["beat_id"] and item.plan_id == lineage["plan_id"]
            ),
            None,
        )
        if beat is None:
            return "beat-unavailable"
        if beat.state != "authorized":
            return f"beat-{beat.state}"
        action = next(
            (item for item in projection.actions if item.action_id == beat.action_id),
            None,
        )
        if action is None:
            return "action-unavailable"
        if action.state not in {"authorized", "scheduled", "claimed"}:
            return f"action-{action.state}"
        return None

    @staticmethod
    def _normalize_decision(
        value: ExpressionReconsiderationDecision | ExpressionReconsiderationDisposition | None,
    ) -> ExpressionReconsiderationDecision | None:
        if value is None:
            return None
        if isinstance(value, str):
            return ExpressionReconsiderationDecision(disposition=value)
        if type(value) is not ExpressionReconsiderationDecision:
            raise TypeError("expression reconsideration reviewer returned an invalid decision")
        if value.requires_replacement() and not value.replacement_plan_ref:
            raise ValueError("expression reconsideration replacement requires an audited plan ref")
        if value.disposition == "defer" and value.replacement_plan_ref is not None:
            raise ValueError("expression reconsideration defer cannot carry a replacement plan")
        return value

    async def _claim_or_reclaim(self, *, process: TriggerProcess, source_event: WorldEvent, projection) -> TriggerProcess | None:
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:expression-reconsideration:" + _digest(
            {"trigger_id": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": ClaimLease(
                    owner_id=self._owner_id,
                    attempt_id=attempt_id,
                    acquired_at=at,
                    expires_at=at + timedelta(seconds=self._lease_seconds),
                ),
                "attempt_ids": (*process.attempt_ids, attempt_id),
            }
        )
        event_type = "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        payload = {"process": claimed.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("expression reconsideration claim lacks an identity")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:expression-reconsideration:trigger:"
            + event_type.lower()
            + ":"
            + _digest([process.trigger_id, attempt_id]),
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        await self._commit(
            (event,),
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id="commit:expression-reconsideration:claim:"
            + _digest([process.trigger_id, attempt_id]),
        )
        return claimed

    async def _complete(
        self, *, process: TriggerProcess, source_event: WorldEvent,
        decision: ExpressionReconsiderationDecision,
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("expression reconsideration completion requires a claim")
        projection = await self._project()
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("expression reconsideration claim expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": self._outcome_ref(process=process, decision=decision),
        }
        identity = "world-v2:expression-reconsideration:completed:" + _digest(
            [self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]
        )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:expression-reconsideration:trigger:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessCompleted",
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        cursor = self._cursor(projection)
        await self._commit_at_cursor(
            (event,),
            cursor=cursor,
            commit_id="commit:expression-reconsideration:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id]),
        )

    async def _replace_or_cancel(
        self, *, process: TriggerProcess, source_event: WorldEvent,
        decision: ExpressionReconsiderationDecision,
    ) -> None:
        """Atomically retire the old, not-yet-dispatched Action and its budget.

        ``ActionCancelled`` is intentionally unavailable for dispatch-started
        actions (the reducer transition table rejects it).  That property is
        the durable guard against silently changing an effect already handed to
        a provider.
        """
        if process.claim_lease is None:
            raise ValueError("expression reconsideration cancellation requires a claim")
        projection = await self._project()
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("expression reconsideration claim expired before cancellation")
        lineage = self._lineage(process)
        action = next(
            (
                item for item in projection.actions
                if item.expression_plan_id == lineage["plan_id"]
                and item.expression_beat_id == lineage["beat_id"]
            ),
            None,
        )
        if action is None or action.state not in {"authorized", "scheduled", "claimed"}:
            raise ValueError("expression reconsideration cannot replace a dispatched or terminal action")
        reservation = next(
            (item for item in projection.budget_reservations if item.reservation_id == action.budget_reservation_id),
            None,
        )
        if reservation is None or reservation.state != "reserved":
            raise ValueError("expression reconsideration requires an active budget reservation")
        decision_hash = _digest(decision.model_dump(mode="json"))
        cancellation_id = "cancellation:expression-reconsideration:" + _digest(
            [process.trigger_id, process.claim_lease.attempt_id, decision_hash]
        )
        result_id = "result:expression-reconsideration:" + _digest(
            [process.trigger_id, action.action_id, cancellation_id]
        )
        settlement = {
            "settlement_id": "settlement:expression-reconsideration:" + _digest(
                [reservation.reservation_id, result_id]
            ),
            "reservation_id": reservation.reservation_id,
            "action_id": action.action_id,
            "result_id": result_id,
            "state": "released",
            "settlement_kind": "terminal",
            "previous_cost": reservation.settled_cost,
            "cost_actual": 0,
            "cost_delta": -reservation.settled_cost,
        }
        common = {
            "schema_version": "world-v2.1", "world_id": self._ledger.world_id,
            "logical_time": at, "created_at": source_event.created_at,
            "actor": self._owner_id, "source": self._source,
            "trace_id": source_event.trace_id, "causation_id": source_event.event_id,
            "correlation_id": source_event.correlation_id,
        }
        cancellation_payload = {
            "action_id": action.action_id,
        }
        cancellation = WorldEvent.from_payload(
            **common,
            event_id="event:expression-reconsideration:action-cancelled:" + _digest([action.action_id, cancellation_id]),
            event_type="ActionCancelled",
            idempotency_key="world-v2:expression-reconsideration:cancel:"
            + _digest([self._ledger.world_id, cancellation_id]),
            payload=cancellation_payload,
        )
        plan = next(
            (item for item in projection.expression_plans if item.plan_id == lineage["plan_id"]),
            None,
        )
        beat = next(
            (item for item in projection.expression_beats if item.beat_id == lineage["beat_id"]),
            None,
        )
        if (
            plan is None
            or beat is None
            or plan.state != "authorized"
            or beat.state != "authorized"
            or beat.plan_id != plan.plan_id
        ):
            raise ValueError("expression reconsideration lacks an active plan and beat")
        plan_termination_payload = ExpressionPlanTerminatedPayload(
            acceptance_id=plan.acceptance_id,
            proposal_id=plan.proposal_id,
            plan_id=plan.plan_id,
            terminal_beat_id=beat.beat_id,
            disposition=(
                "superseded" if decision.disposition in {"merge", "supersede", "new_beat"}
                else "cancelled"
            ),
            source_event_ref=cancellation.event_id,
            source_event_payload_hash=cancellation.payload_hash,
        )
        plan_termination_body = plan_termination_payload.model_dump(mode="json")
        plan_termination_identity = domain_idempotency_key(
            event_type="ExpressionPlanTerminated",
            world_id=self._ledger.world_id,
            payload=plan_termination_body,
        )
        if plan_termination_identity is None:
            raise ValueError("expression plan termination lacks a domain identity")
        plan_termination = WorldEvent.from_payload(
            **{**common, "causation_id": cancellation.event_id},
            event_id="event:expression-reconsideration:plan-terminated:"
            + _digest([plan.plan_id, process.trigger_id, decision_hash]),
            event_type="ExpressionPlanTerminated",
            idempotency_key=plan_termination_identity,
            payload=plan_termination_body,
        )
        beat_termination_payload = ExpressionBeatTerminatedPayload(
            acceptance_id=beat.acceptance_id,
            proposal_id=beat.proposal_id,
            plan_id=beat.plan_id,
            beat_id=beat.beat_id,
            action_id=action.action_id,
            disposition=(
                "superseded" if decision.disposition in {"merge", "supersede", "new_beat"}
                else "cancelled"
            ),
            source_event_ref=cancellation.event_id,
            source_event_payload_hash=cancellation.payload_hash,
        )
        beat_termination_body = beat_termination_payload.model_dump(mode="json")
        beat_termination_identity = domain_idempotency_key(
            event_type="ExpressionBeatTerminated",
            world_id=self._ledger.world_id,
            payload=beat_termination_body,
        )
        if beat_termination_identity is None:
            raise ValueError("expression beat termination lacks a domain identity")
        beat_termination = WorldEvent.from_payload(
            **{**common, "causation_id": cancellation.event_id},
            event_id="event:expression-reconsideration:beat-terminated:"
            + _digest([beat.beat_id, process.trigger_id, decision_hash]),
            event_type="ExpressionBeatTerminated",
            idempotency_key=beat_termination_identity,
            payload=beat_termination_body,
        )
        release = WorldEvent.from_payload(
            **{**common, "causation_id": plan_termination.event_id},
            event_id="event:expression-reconsideration:budget-released:" + _digest([reservation.reservation_id, result_id]),
            event_type="BudgetReleased", idempotency_key="world-v2:expression-reconsideration:release:" + _digest([self._ledger.world_id, reservation.reservation_id, result_id]),
            payload={"settlement": settlement},
        )
        sibling_terminal_events: list[WorldEvent] = []
        for sibling in projection.actions:
            if (
                sibling.action_id == action.action_id
                or sibling.expression_plan_id != plan.plan_id
                or sibling.state not in {"authorized", "scheduled", "claimed"}
            ):
                continue
            sibling_reservation = next(
                (
                    item
                    for item in projection.budget_reservations
                    if item.reservation_id == sibling.budget_reservation_id
                ),
                None,
            )
            if sibling_reservation is None or sibling_reservation.state != "reserved":
                raise ValueError(
                    "expression reconsideration sibling requires an active budget reservation"
                )
            sibling_cancellation_id = "cancellation:expression-plan-terminal:" + _digest(
                [process.trigger_id, sibling.action_id, decision_hash]
            )
            sibling_cancel_payload = {
                "action_id": sibling.action_id,
            }
            sibling_cancel = WorldEvent.from_payload(
                **{**common, "causation_id": plan_termination.event_id},
                event_id="event:expression-reconsideration:sibling-action-cancelled:"
                + _digest([sibling.action_id, sibling_cancellation_id]),
                event_type="ActionCancelled",
                idempotency_key="world-v2:expression-reconsideration:sibling-cancel:"
                + _digest([self._ledger.world_id, sibling_cancellation_id]),
                payload=sibling_cancel_payload,
            )
            sibling_beat = next(
                (
                    item
                    for item in projection.expression_beats
                    if item.beat_id == sibling.expression_beat_id
                ),
                None,
            )
            if sibling_beat is None:
                raise ValueError("expression reconsideration sibling lacks beat authority")
            sibling_beat_payload = ExpressionBeatTerminatedPayload(
                acceptance_id=sibling_beat.acceptance_id,
                proposal_id=sibling_beat.proposal_id,
                plan_id=sibling_beat.plan_id,
                beat_id=sibling_beat.beat_id,
                action_id=sibling.action_id,
                disposition=beat_termination_payload.disposition,
                source_event_ref=sibling_cancel.event_id,
                source_event_payload_hash=sibling_cancel.payload_hash,
            )
            sibling_beat_body = sibling_beat_payload.model_dump(mode="json")
            sibling_beat_identity = domain_idempotency_key(
                event_type="ExpressionBeatTerminated",
                world_id=self._ledger.world_id,
                payload=sibling_beat_body,
            )
            if sibling_beat_identity is None:
                raise ValueError("expression sibling beat termination lacks a domain identity")
            sibling_beat_event = WorldEvent.from_payload(
                **{**common, "causation_id": sibling_cancel.event_id},
                event_id="event:expression-reconsideration:sibling-beat-terminated:"
                + _digest([sibling_beat.beat_id, sibling_cancellation_id]),
                event_type="ExpressionBeatTerminated",
                idempotency_key=sibling_beat_identity,
                payload=sibling_beat_body,
            )
            sibling_result_id = "result:expression-plan-terminal:" + _digest(
                [process.trigger_id, sibling.action_id, decision_hash]
            )
            sibling_settlement = {
                "settlement_id": "settlement:expression-plan-terminal:" + _digest(
                    [sibling_reservation.reservation_id, sibling_result_id]
                ),
                "reservation_id": sibling_reservation.reservation_id,
                "action_id": sibling.action_id,
                "result_id": sibling_result_id,
                "state": "released",
                "settlement_kind": "terminal",
                "previous_cost": sibling_reservation.settled_cost,
                "cost_actual": 0,
                "cost_delta": -sibling_reservation.settled_cost,
            }
            sibling_release_payload = {"settlement": sibling_settlement}
            sibling_release = WorldEvent.from_payload(
                **{**common, "causation_id": sibling_cancel.event_id},
                event_id="event:expression-reconsideration:sibling-budget-released:"
                + _digest([sibling_reservation.reservation_id, sibling_result_id]),
                event_type="BudgetReleased",
                idempotency_key="world-v2:expression-reconsideration:sibling-release:"
                + _digest(
                    [self._ledger.world_id, sibling_reservation.reservation_id, sibling_result_id]
                ),
                payload=sibling_release_payload,
            )
            sibling_terminal_events.extend(
                (sibling_cancel, sibling_beat_event, sibling_release)
            )
        completion_payload = {
            "trigger_id": process.trigger_id, "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id, "completed_at": at.isoformat(),
            "runtime_outcome_ref": self._outcome_ref(process=process, decision=decision),
        }
        completion = WorldEvent.from_payload(
            **{**common, "causation_id": release.event_id},
            event_id="event:expression-reconsideration:trigger:completed:" + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            event_type="TriggerProcessCompleted", idempotency_key="world-v2:expression-reconsideration:completed:" + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
            payload=completion_payload,
        )
        await self._commit_at_cursor(
            (
                cancellation,
                beat_termination,
                release,
                *sibling_terminal_events,
                plan_termination,
                completion,
            ),
            cursor=self._cursor(projection),
            commit_id="commit:expression-reconsideration:replace:" + _digest([process.trigger_id, process.claim_lease.attempt_id, decision_hash]),
        )
        await self._release_interrupted_commitment(
            action_id=action.action_id,
            source_event=source_event,
            causation_id=completion.event_id,
        )

    async def _recover_cancelled_commitments(self) -> None:
        projection = await self._project()
        active_action_ids = {
            item.values.fulfillment_contract.expected_action_id
            for item in projection.commitments
            if item.values.status in {"open", "due"}
            and item.values.fulfillment_contract.contract_kind == "execution_receipt"
            and item.values.fulfillment_contract.expected_action_id
        }
        for action in projection.actions:
            if action.action_id not in active_action_ids or action.state != "cancelled":
                continue
            process = next(
                (
                    item
                    for item in projection.trigger_processes
                    if item.process_kind == "expression_reconsideration"
                    and item.state == "terminal"
                    and item.source_evidence_ref is not None
                    and self._lineage_matches_action(item, action)
                ),
                None,
            )
            if process is None:
                continue
            source = await self._lookup(process.source_evidence_ref or "")
            if source is None or source[0].event_type != "ObservationRecorded":
                raise ValueError("cancelled commitment recovery source is unavailable")
            await self._release_interrupted_commitment(
                action_id=action.action_id,
                source_event=source[0],
                causation_id=process.trigger_id,
            )

    @classmethod
    def _lineage_matches_action(cls, process: TriggerProcess, action) -> bool:
        try:
            lineage = cls._lineage(process)
        except (ValueError, json.JSONDecodeError):
            return False
        return (
            action.expression_plan_id == lineage["plan_id"]
            and action.expression_beat_id == lineage["beat_id"]
        )

    async def _release_interrupted_commitment(
        self, *, action_id: str, source_event: WorldEvent, causation_id: str
    ) -> None:
        from .deferred_reply_runtime import DeferredReplyRuntime

        kwargs = dict(
            action_id=action_id,
            observation_event_id=source_event.event_id,
            logical_time=max((await self._project()).logical_time or source_event.logical_time,
                             source_event.logical_time),
            created_at=source_event.created_at,
            trace_id=source_event.trace_id,
            causation_id=causation_id,
            correlation_id=source_event.correlation_id,
        )
        runtime = DeferredReplyRuntime(ledger=self._ledger)
        if self._ledger.blocks_event_loop:
            await asyncio.to_thread(runtime.release_interrupted_action, **kwargs)
        else:
            runtime.release_interrupted_action(**kwargs)

    @staticmethod
    def _lineage(process: TriggerProcess) -> dict[str, str]:
        prefix = "expression-reconsideration:"
        if not process.trigger_ref.startswith(prefix):
            raise ValueError("expression reconsideration trigger lineage is invalid")
        value = json.loads(process.trigger_ref.removeprefix(prefix))
        if not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value[key] for key in ("plan_id", "beat_id", "observation_id")):
            raise ValueError("expression reconsideration trigger lineage is invalid")
        return value

    @classmethod
    def _outcome_ref(cls, *, process: TriggerProcess, decision: ExpressionReconsiderationDecision) -> str:
        # A canonical JSON ref is the durable decision/audit lineage.  The
        # payload itself remains immutable and any replacement is only a ref.
        return "expression-reconsideration-decision:" + json.dumps(
            {
                "trigger_id": process.trigger_id,
                "decision": decision.model_dump(mode="json"),
                "lineage": cls._lineage(process),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _lookup(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _commit(self, events, *, world_revision: int, deliberation_revision: int, commit_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit,
                events,
                expected_world_revision=world_revision,
                expected_deliberation_revision=deliberation_revision,
                commit_id=commit_id,
            )
        return self._ledger.commit(
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
            commit_id=commit_id,
        )

    async def _commit_at_cursor(self, events, *, cursor: ProjectionCursor, commit_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit_at_cursor,
                events,
                expected_cursor=cursor,
                commit_id=commit_id,
            )
        return self._ledger.commit_at_cursor(events, expected_cursor=cursor, commit_id=commit_id)

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


__all__ = [
    "ExpressionReconsiderationDisposition",
    "ExpressionReconsiderationDecision",
    "ExpressionReconsiderationReviewer",
    "ExpressionReconsiderationRunResult",
    "ExpressionReconsiderationRuntime",
]
