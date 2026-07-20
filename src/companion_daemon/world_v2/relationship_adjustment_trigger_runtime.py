"""Recovery-safe scheduler for post-signal relationship adjustments."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal, Protocol

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .relationship_adjustment_trigger import (
    relationship_adjustment_trigger_id,
    relationship_adjustment_trigger_open_event,
)
from .schema_core import FrozenModel
from .schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RelationshipAdjustmentWorkResult(Protocol):
    status: str


class RelationshipAdjustmentWorker(Protocol):
    async def process(
        self, *, world_id: str, cursor: ProjectionCursor, signal_event: WorldEvent
    ) -> RelationshipAdjustmentWorkResult: ...


class RelationshipAdjustmentTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "processed"]
    work_status: str | None = None


class RelationshipAdjustmentTriggerRuntime:
    """Drain one signal-bound adjustment with a durable lease."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        worker: RelationshipAdjustmentWorker,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:relationship-adjustment-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("relationship adjustment runtime needs owner and positive lease")
        self._ledger = ledger
        self._worker = worker
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> RelationshipAdjustmentTriggerRunResult:
        projection = self._ledger.project()
        process = self._find_or_open_process(projection)
        if process is None:
            return RelationshipAdjustmentTriggerRunResult(trigger_id="", status="idle")
        projection = self._ledger.project()
        process = next(item for item in projection.trigger_processes if item.trigger_id == process.trigger_id)
        source_event = self._source_event(process)
        active = self._claim_or_reclaim(
            process=process, source_event=source_event, projection=projection
        )
        if active is None:
            return RelationshipAdjustmentTriggerRunResult(
                trigger_id=process.trigger_id, status="owned_elsewhere"
            )
        work = await self._worker.process(
            world_id=self._ledger.world_id, cursor=self._cursor(), signal_event=source_event
        )
        work_status = getattr(work, "status", None)
        if not isinstance(work_status, str) or not work_status:
            raise ValueError("relationship adjustment worker must return a non-empty status")
        self._complete(
            process=active,
            source_event=source_event,
            cursor=self._cursor(),
            outcome_ref=f"outcome:{process.trigger_id}:{work_status}",
        )
        return RelationshipAdjustmentTriggerRunResult(
            trigger_id=process.trigger_id, status="processed", work_status=work_status
        )

    def _find_or_open_process(self, projection) -> TriggerProcess | None:
        active = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "relationship_adjustment" and item.state != "terminal"
            ),
            None,
        )
        if active is not None:
            return active
        consumed = {
            signal_ref
            for adjustment in projection.relationship_adjustments
            if adjustment.operation == "adjust"
            for signal_ref in adjustment.signal_refs
        }
        terminal = {
            item.trigger_id
            for item in projection.trigger_processes
            if item.process_kind == "relationship_adjustment" and item.state == "terminal"
        }
        for signal in projection.relationship_signals:
            if signal.signal_id in consumed:
                continue
            source_event = self._source_event_ref(signal.origin.accepted_event_ref)
            trigger_id = relationship_adjustment_trigger_id(
                world_id=self._ledger.world_id, signal_event_id=source_event.event_id
            )
            if trigger_id in terminal:
                continue
            if projection.logical_time is None:
                raise ValueError("relationship adjustment trigger needs logical time")
            opened = relationship_adjustment_trigger_open_event(
                signal_event=source_event, logical_time=projection.logical_time
            )
            self._ledger.commit(
                [opened],
                expected_world_revision=projection.world_revision,
                expected_deliberation_revision=projection.deliberation_revision,
                commit_id="commit:relationship-adjustment-trigger:opened:"
                + _digest([trigger_id, projection.ledger_sequence]),
            )
            return TriggerProcess(
                trigger_id=trigger_id,
                trigger_ref=f"relationship-adjustment:{source_event.event_id}",
                process_kind="relationship_adjustment",
                source_evidence_ref=source_event.event_id,
                state="open",
            )
        return None

    def _source_event_ref(self, event_id: str | None) -> WorldEvent:
        located = self._ledger.lookup_event_commit(event_id) if event_id is not None else None
        if located is None or located[0].event_type != "RelationshipSignalAccepted":
            raise ValueError("relationship adjustment source signal is unavailable")
        return located[0]

    def _source_event(self, process: TriggerProcess) -> WorldEvent:
        event = self._source_event_ref(process.source_evidence_ref)
        if (
            relationship_adjustment_trigger_id(
                world_id=event.world_id, signal_event_id=event.event_id
            )
            != process.trigger_id
            or process.trigger_ref != f"relationship-adjustment:{event.event_id}"
        ):
            raise ValueError("relationship adjustment trigger identity does not bind source signal")
        return event

    def _claim_or_reclaim(self, *, process: TriggerProcess, source_event: WorldEvent, projection) -> TriggerProcess | None:
        current_time = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if (
                process.claim_lease.owner_id == self._owner_id
                and current_time <= process.claim_lease.expires_at
            ):
                return process
            if current_time < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:relationship-adjustment:" + _digest(
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
        event_type = "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        payload = {"process": claimed.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("relationship adjustment claim identity missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:relationship-adjustment:"
            + event_type.lower()
            + ":"
            + _digest([process.trigger_id, attempt_id]),
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
            commit_id="commit:relationship-adjustment-trigger:"
            + event_type.lower()
            + ":"
            + _digest([process.trigger_id, attempt_id]),
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
            raise ValueError("relationship adjustment completion requires a claim")
        completed_at = max(
            self._ledger.project_at(cursor).logical_time or source_event.logical_time,
            process.claim_lease.acquired_at,
        )
        if completed_at > process.claim_lease.expires_at:
            raise ValueError("relationship adjustment lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": completed_at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:relationship-adjustment:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessCompleted",
            logical_time=completed_at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key="world-v2:relationship-adjustment-trigger:completion:"
            + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            [event],
            expected_cursor=cursor,
            commit_id="commit:relationship-adjustment-trigger:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]),
        )

    def _cursor(self) -> ProjectionCursor:
        projection = self._ledger.project()
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


__all__ = [
    "RelationshipAdjustmentTriggerRunResult",
    "RelationshipAdjustmentTriggerRuntime",
    "RelationshipAdjustmentWorker",
    "RelationshipAdjustmentWorkResult",
]
