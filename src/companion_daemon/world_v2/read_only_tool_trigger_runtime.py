"""Recovery-safe background runner for the optional read-only-tool lane."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort, ObservationEventLocator
from .pinned_turn import PinnedTurnCompiler
from .read_only_tool_proposal_compiler import ReadOnlyToolProposalCompiler
from .schema_core import FrozenModel
from .schemas import ClaimLease, Observation, ProjectionCursor, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReadOnlyToolTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "processed"]
    work_status: Literal["no_change", "accepted"] | None = None


class ReadOnlyToolTriggerRuntime:
    """Consume exactly one optional tool-decision opportunity.

    Merely creating this worker does not enable a provider.  Composition must
    also install a matching ``ReadOnlyToolActionExecutor``; otherwise the
    application does not construct this runner at all.
    """

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        turn: PinnedTurnCompiler,
        compiler: ReadOnlyToolProposalCompiler,
        owner_id: str,
        lease_seconds: int = 120,
    ) -> None:
        if not owner_id or lease_seconds <= 0 or compiler.ledger is not ledger:
            raise ValueError("read-only tool trigger needs matching compiler, owner and lease")
        self._ledger = ledger
        self._turn = turn
        self._compiler = compiler
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    async def drain_one(self) -> ReadOnlyToolTriggerRunResult:
        projection = self._ledger.project()
        process = next(
            (item for item in projection.trigger_processes if item.process_kind == "read_only_tool_deliberation" and item.state != "terminal"),
            None,
        )
        if process is None:
            return ReadOnlyToolTriggerRunResult(trigger_id="", status="idle")
        source_event, observation = self._source(process, self._cursor(projection))
        active = self._claim_or_reclaim(process=process, source_event=source_event, projection=projection)
        if active is None:
            return ReadOnlyToolTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")
        cursor = self._cursor(self._ledger.project())
        audit = await self._turn.audit_observation(
            observation=observation, observation_event=source_event, cursor=cursor
        )
        if audit.proposal_id is None:
            self._complete(active, source_event, "no-change")
            return ReadOnlyToolTriggerRunResult(trigger_id=process.trigger_id, status="processed", work_status="no_change")
        compiled = self._compiler.accept(
            world_id=self._ledger.world_id,
            cursor=audit.cursor,
            proposal_id=audit.proposal_id,
            actor=self._owner_id,
            source="world-v2:read-only-tool-trigger",
        )
        self._complete(active, source_event, compiled.status)
        return ReadOnlyToolTriggerRunResult(
            trigger_id=process.trigger_id,
            status="processed",
            work_status="accepted" if compiled.status == "accepted" else "no_change",
        )

    def _source(self, process: TriggerProcess, cursor: ProjectionCursor) -> tuple[WorldEvent, Observation]:
        observation_id = process.source_evidence_ref
        projection = self._ledger.project_at(cursor)
        reference = next(
            (item for item in projection.message_observations if item.observation_id == observation_id), None
        )
        if reference is None:
            raise ValueError("read-only tool source observation is unavailable")
        located = self._ledger.observation_events_at(
            (
                ObservationEventLocator.for_message(
                    world_id=self._ledger.world_id,
                    observation_id=reference.observation_id,
                    source=reference.source,
                    source_event_id=reference.source_event_id,
                ),
            ),
            cursor=cursor,
        )
        if len(located) != 1 or located[0].event.event_type != "ObservationRecorded":
            raise ValueError("read-only tool source proof is incomplete")
        event = located[0].event
        observation = Observation.model_validate_json(event.payload_json)
        if process.trigger_ref != f"read-only-tool:{observation.observation_id}":
            raise ValueError("read-only tool trigger source does not bind")
        return event, observation

    def _claim_or_reclaim(self, *, process: TriggerProcess, source_event: WorldEvent, projection) -> TriggerProcess | None:
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:read-only-tool:" + _digest(
            {"trigger": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
        )
        claimed = process.model_copy(update={
            "state": "claimed",
            "claim_lease": ClaimLease(owner_id=self._owner_id, attempt_id=attempt_id, acquired_at=at, expires_at=at + timedelta(seconds=self._lease_seconds)),
            "attempt_ids": (*process.attempt_ids, attempt_id),
        })
        event_type = "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        payload = {"process": claimed.model_dump(mode="json")}
        identity = domain_idempotency_key(event_type=event_type, world_id=self._ledger.world_id, payload=payload)
        if identity is None:
            raise ValueError("read-only tool claim identity missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1", event_id=f"event:read-only-tool:{event_type}:{_digest([process.trigger_id, attempt_id])}",
            world_id=self._ledger.world_id, event_type=event_type, logical_time=at, created_at=source_event.created_at,
            actor=self._owner_id, source="world-v2:read-only-tool-trigger", trace_id=source_event.trace_id,
            causation_id=source_event.event_id, correlation_id=source_event.correlation_id,
            idempotency_key=identity, payload=payload,
        )
        self._ledger.commit((event,), expected_world_revision=projection.world_revision,
                            expected_deliberation_revision=projection.deliberation_revision,
                            commit_id="commit:read-only-tool:claim:" + _digest([process.trigger_id, attempt_id]))
        return claimed

    def _complete(self, process: TriggerProcess, source_event: WorldEvent, outcome: str) -> None:
        if process.claim_lease is None:
            raise ValueError("read-only tool completion requires claim")
        projection = self._ledger.project()
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("read-only tool trigger lease expired")
        payload = {"trigger_id": process.trigger_id, "owner_id": process.claim_lease.owner_id,
                   "attempt_id": process.claim_lease.attempt_id, "completed_at": at.isoformat(),
                   "runtime_outcome_ref": f"outcome:{process.trigger_id}:{outcome}"}
        event = WorldEvent.from_payload(
            schema_version="world-v2.1", event_id="event:read-only-tool:completed:" + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            world_id=self._ledger.world_id, event_type="TriggerProcessCompleted", logical_time=at,
            created_at=source_event.created_at, actor=self._owner_id, source="world-v2:read-only-tool-trigger",
            trace_id=source_event.trace_id, causation_id=source_event.event_id, correlation_id=source_event.correlation_id,
            idempotency_key="read-only-tool:completed:" + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]), payload=payload,
        )
        self._ledger.commit((event,), expected_world_revision=projection.world_revision,
                            expected_deliberation_revision=projection.deliberation_revision,
                            commit_id="commit:read-only-tool:completed:" + _digest([process.trigger_id, outcome]))

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)


__all__ = ["ReadOnlyToolTriggerRunResult", "ReadOnlyToolTriggerRuntime"]
