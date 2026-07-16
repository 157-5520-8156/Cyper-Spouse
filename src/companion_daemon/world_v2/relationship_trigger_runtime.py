"""Recovery-safe executor for relationship deliberation triggers.

The runner owns only the trigger lease and completion record.  The injected
worker owns the source-bound audit/proposal/acceptance details, which keeps
this module independent of a particular relationship proposal schema while
retaining the same durable scheduling semantics as the Affect lane.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal, Protocol

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .relationship_trigger import relationship_deliberation_trigger_id
from .schema_core import FrozenModel
from .schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class RelationshipDeliberationWorkResult(Protocol):
    """The minimal durable-worker result contract.

    A worker may expose richer commit/proposal details.  The trigger runtime
    intentionally records only its stable disposition string in the process
    outcome, preventing this scheduling seam from becoming a second authority
    for relationship mutations.
    """

    status: str


class RelationshipDeliberationWorker(Protocol):
    """Duck-typed source-bound relationship work seam."""

    async def process(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        appraisal_event: WorldEvent,
    ) -> RelationshipDeliberationWorkResult: ...


class RelationshipTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "processed"]
    work_status: str | None = None


class RelationshipTriggerRuntime:
    """Drain one source-bound relationship trigger with lease recovery."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        worker: RelationshipDeliberationWorker,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:relationship-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("relationship trigger runtime needs owner and positive lease")
        self._ledger = ledger
        self._worker = worker
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> RelationshipTriggerRunResult:
        """Finish at most one relationship trigger from the current head."""

        projection = self._ledger.project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "relationship_deliberation" and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return RelationshipTriggerRunResult(trigger_id="", status="idle")
        source_event = self._source_event(process)
        active = self._claim_or_reclaim(
            process=process, source_event=source_event, projection=projection
        )
        if active is None:
            return RelationshipTriggerRunResult(
                trigger_id=process.trigger_id, status="owned_elsewhere"
            )
        work = await self._worker.process(
            world_id=self._ledger.world_id,
            cursor=self._cursor(),
            appraisal_event=source_event,
        )
        work_status = getattr(work, "status", None)
        if not isinstance(work_status, str) or not work_status:
            raise ValueError("relationship trigger worker must return a non-empty status")
        self._complete(
            process=active,
            source_event=source_event,
            cursor=self._cursor(),
            outcome_ref=f"outcome:{process.trigger_id}:{work_status}",
        )
        return RelationshipTriggerRunResult(
            trigger_id=process.trigger_id, status="processed", work_status=work_status
        )

    def _source_event(self, process: TriggerProcess) -> WorldEvent:
        event_id = process.source_evidence_ref
        located = self._ledger.lookup_event_commit(event_id) if event_id is not None else None
        if located is None or located[0].event_type != "AppraisalAccepted":
            raise ValueError("relationship trigger source appraisal is unavailable")
        if relationship_deliberation_trigger_id(
            world_id=located[0].world_id, appraisal_event_id=located[0].event_id
        ) != process.trigger_id:
            raise ValueError("relationship trigger identity does not bind source appraisal")
        if process.trigger_ref != f"relationship:{located[0].event_id}":
            raise ValueError("relationship trigger reference does not bind source appraisal")
        return located[0]

    def _claim_or_reclaim(
        self, *, process: TriggerProcess, source_event: WorldEvent, projection
    ) -> TriggerProcess | None:
        current_time = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if (
                process.claim_lease.owner_id == self._owner_id
                and current_time <= process.claim_lease.expires_at
            ):
                return process
            if current_time < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:relationship-deliberation:" + _digest(
            {"trigger_id": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": ClaimLease(
                    owner_id=self._owner_id,
                    attempt_id=attempt_id,
                    acquired_at=current_time,
                    expires_at=current_time + timedelta(seconds=self._lease_seconds),
                ),
                "attempt_ids": (*process.attempt_ids, attempt_id),
            }
        )
        event_type = (
            "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        )
        payload = {"process": claimed.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("relationship trigger claim identity missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=(
                f"event:relationship-trigger:{event_type.lower()}:"
                f"{_digest([process.trigger_id, attempt_id])}"
            ),
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=current_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        self._ledger.commit(
            [event],
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            commit_id=(
                f"commit:relationship-trigger:{event_type.lower()}:"
                f"{_digest([process.trigger_id, attempt_id])}"
            ),
        )
        return claimed

    def _complete(
        self,
        *,
        process: TriggerProcess,
        source_event: WorldEvent,
        cursor: ProjectionCursor,
        outcome_ref: str,
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("relationship trigger completion requires a claimed process")
        completed_at = max(
            self._ledger.project_at(cursor).logical_time or source_event.logical_time,
            process.claim_lease.acquired_at,
        )
        if completed_at > process.claim_lease.expires_at:
            raise ValueError("relationship trigger lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": completed_at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        identity = "world-v2:relationship-trigger:completion:" + _digest(
            [self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]
        )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=(
                "event:relationship-trigger:completed:"
                + _digest([process.trigger_id, process.claim_lease.attempt_id])
            ),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessCompleted",
            logical_time=completed_at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            [event],
            expected_cursor=cursor,
            commit_id=(
                "commit:relationship-trigger:completed:"
                + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref])
            ),
        )

    def _cursor(self) -> ProjectionCursor:
        projection = self._ledger.project()
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


__all__ = [
    "RelationshipDeliberationWorker",
    "RelationshipDeliberationWorkResult",
    "RelationshipTriggerRunResult",
    "RelationshipTriggerRuntime",
]
