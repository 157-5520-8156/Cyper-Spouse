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


ExpressionReconsiderationDisposition = Literal["continue"]


class ExpressionReconsiderationReviewer(Protocol):
    """Optional LLM/semantic reviewer hook.

    Returning ``None`` means no explicit acceptance.  More expansive outcomes
    (cancel/merge/supersede) require their own accepted materializer and are
    deliberately not faked by this first safety vertical.
    """

    async def review(
        self,
        *,
        process: TriggerProcess,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
    ) -> ExpressionReconsiderationDisposition | None: ...


class ExpressionReconsiderationRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "awaiting_review", "continued"]


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
        disposition = await self._reviewer.review(
            process=active, observation_event=source[0], cursor=self._cursor(await self._project())
        )
        if disposition != "continue":
            return ExpressionReconsiderationRunResult(
                trigger_id=active.trigger_id, status="awaiting_review"
            )
        await self._complete(process=active, source_event=source[0])
        return ExpressionReconsiderationRunResult(trigger_id=active.trigger_id, status="continued")

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

    async def _complete(self, *, process: TriggerProcess, source_event: WorldEvent) -> None:
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
            "runtime_outcome_ref": f"expression-reconsideration:{process.trigger_id}:continue",
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
    "ExpressionReconsiderationReviewer",
    "ExpressionReconsiderationRunResult",
    "ExpressionReconsiderationRuntime",
]
