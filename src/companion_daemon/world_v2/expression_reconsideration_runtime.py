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
        "replacement_required", "deferred",
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
        cancellation_payload = {"action_id": action.action_id}
        cancellation = WorldEvent.from_payload(
            **common,
            event_id="event:expression-reconsideration:action-cancelled:" + _digest([action.action_id, cancellation_id]),
            event_type="ActionCancelled", idempotency_key="world-v2:expression-reconsideration:cancel:" + _digest([self._ledger.world_id, cancellation_id]),
            payload=cancellation_payload,
        )
        release = WorldEvent.from_payload(
            **{**common, "causation_id": cancellation.event_id},
            event_id="event:expression-reconsideration:budget-released:" + _digest([reservation.reservation_id, result_id]),
            event_type="BudgetReleased", idempotency_key="world-v2:expression-reconsideration:release:" + _digest([self._ledger.world_id, reservation.reservation_id, result_id]),
            payload={"settlement": settlement},
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
            (cancellation, release, completion), cursor=self._cursor(projection),
            commit_id="commit:expression-reconsideration:replace:" + _digest([process.trigger_id, process.claim_lease.attempt_id, decision_hash]),
        )

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
