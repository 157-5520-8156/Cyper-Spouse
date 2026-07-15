"""Recovery-safe worker for source-bound ``outcome_deliberation`` triggers."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from typing import Literal

from .event_identity import domain_idempotency_key
from .outcome_deliberation_turn import OutcomeDeliberationTurn
from .outcome_proposal_worker import OutcomeProposalWorker
from .schema_core import FrozenModel
from .schemas import ClaimLease, OutcomeObservationProjection, ProjectionCursor, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class OutcomeTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal["no_proposal", "candidate_content_unavailable", "accepted"] | None = None


class OutcomeTriggerRuntime:
    """Drain one outcome trigger after the visible chat path has returned.

    The trigger claims/reclaims before model work.  On retry it reuses a
    cursor-matching audit; if an earlier audit is stale it fails closed rather
    than compiling it against a newer occurrence head.  Completion occurs only
    after a durable no-proposal disposition; accepted work leaves the source
    trigger claimed until the acceptance batch's follow-on NPC appraisal is
    durably available, preserving recovery visibility.
    """

    def __init__(
        self, *, ledger, turn: OutcomeDeliberationTurn, worker: OutcomeProposalWorker,
        owner_id: str, lease_seconds: int = 120,
        source: str = "world-v2:outcome-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("outcome trigger runtime needs owner and positive lease")
        if worker.ledger is not ledger:
            raise ValueError("outcome worker must own the exact ledger")
        self._ledger = ledger
        self._turn = turn
        self._worker = worker
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> OutcomeTriggerRunResult:
        projection = await self._project()
        process = next((item for item in projection.trigger_processes if item.process_kind == "outcome_deliberation" and item.state != "terminal"), None)
        if process is None:
            return OutcomeTriggerRunResult(trigger_id="", status="idle")
        source_event, observation = await self._source_observation(process, self._cursor(projection))
        active = await self._claim_or_reclaim(process=process, source_event=source_event, projection=projection)
        if active is None:
            return OutcomeTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")

        current = await self._project()
        current_cursor = self._cursor(current)
        audit = next((item for item in current.proposal_audits if item.proposal_kind == "decision" and item.trigger_ref == source_event.event_id), None)
        if audit is None:
            audited = await self._turn.audit_observation(observation_event=source_event, cursor=current_cursor)
            if audited.commit is None:
                disposition = audited.disposition or "no_proposal"
                await self._complete(process=active, source_event=source_event, cursor=current_cursor,
                                     outcome_ref=f"outcome:{active.trigger_id}:{disposition}")
                return OutcomeTriggerRunResult(trigger_id=active.trigger_id, status="processed", work_status=disposition)
            if audited.commit.proposal_id is None:
                await self._complete(process=active, source_event=source_event, cursor=audited.commit.cursor,
                                     outcome_ref=f"outcome:{active.trigger_id}:no-proposal")
                return OutcomeTriggerRunResult(trigger_id=active.trigger_id, status="processed", work_status="no_proposal")
            audit = next((item for item in (await self._project()).proposal_audits if item.proposal_id == audited.commit.proposal_id and item.proposal_kind == "decision"), None)
            if audit is None:
                raise RuntimeError("outcome deliberation audit was not durably recorded")
            work_cursor = audited.commit.cursor
        else:
            stored = await self._lookup(audit.event_ref)
            if stored is None:
                raise RuntimeError("outcome deliberation audit event is unavailable")
            work_cursor = self._cursor_from_commit(stored[1])
            if work_cursor != current_cursor:
                raise RuntimeError("outcome deliberation audit cursor is stale")

        if self._ledger.blocks_event_loop:
            work = await asyncio.to_thread(self._worker.process, world_id=self._ledger.world_id, cursor=work_cursor, proposal_id=audit.proposal_id)
        else:
            work = self._worker.process(world_id=self._ledger.world_id, cursor=work_cursor, proposal_id=audit.proposal_id)
        if work.acceptance_commit is None:
            raise RuntimeError("accepted outcome has no acceptance commit")
        # Outcome acceptance atomically opens npc_world_appraisal.  The trigger
        # is now terminal only after that source-bound effect exists.
        await self._complete(process=active, source_event=source_event,
                             cursor=self._cursor(await self._project()),
                             outcome_ref=f"outcome:{active.trigger_id}:accepted:{work.typed_proposal_id}")
        return OutcomeTriggerRunResult(trigger_id=active.trigger_id, status="processed", work_status="accepted")

    async def _source_observation(self, process: TriggerProcess, cursor: ProjectionCursor) -> tuple[WorldEvent, OutcomeObservationProjection]:
        if process.source_evidence_ref is None:
            raise ValueError("outcome trigger has no source observation")
        stored = await self._lookup(process.source_evidence_ref)
        if stored is None or stored[0].event_type != "OutcomeObservationRecorded" or stored[1].world_revision > cursor.world_revision:
            raise ValueError("outcome trigger source observation is unavailable")
        event = stored[0]
        observation = OutcomeObservationProjection.model_validate_json(
            json.dumps(event.payload().get("observation"))
        )
        expected_ref = f"outcome:{observation.occurrence_id}:{observation.observation_id}"
        if event.event_id != f"event:outcome-observation:{observation.observation_id}" or process.trigger_ref != expected_ref:
            raise ValueError("outcome trigger source proof does not bind its observation")
        return event, observation

    async def _claim_or_reclaim(self, *, process: TriggerProcess, source_event: WorldEvent, projection) -> TriggerProcess | None:
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:outcome-deliberation:" + _digest({"trigger_id": process.trigger_id, "attempt": len(process.attempt_ids) + 1})
        claimed = process.model_copy(update={"state": "claimed", "claim_lease": ClaimLease(owner_id=self._owner_id, attempt_id=attempt_id, acquired_at=at, expires_at=at + timedelta(seconds=self._lease_seconds)), "attempt_ids": (*process.attempt_ids, attempt_id)})
        event_type = "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        payload = {"process": claimed.model_dump(mode="json")}
        identity = domain_idempotency_key(event_type=event_type, world_id=self._ledger.world_id, payload=payload)
        if identity is None:
            raise ValueError("outcome trigger claim identity is missing")
        event = WorldEvent.from_payload(schema_version="world-v2.1", event_id=f"event:outcome-deliberation:trigger:{event_type.lower()}:" + _digest([process.trigger_id, attempt_id]), world_id=self._ledger.world_id, event_type=event_type, logical_time=at, created_at=source_event.created_at, actor=self._owner_id, source=self._source, trace_id=source_event.trace_id, causation_id=source_event.event_id, correlation_id=source_event.correlation_id, idempotency_key=identity, payload=payload)
        await self._commit((event,), world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, commit_id="commit:outcome-deliberation:claim:" + _digest([process.trigger_id, attempt_id]))
        return claimed

    async def _complete(self, *, process: TriggerProcess, source_event: WorldEvent, cursor: ProjectionCursor, outcome_ref: str) -> None:
        if process.claim_lease is None:
            raise ValueError("outcome trigger completion requires a claimed process")
        projection = await self._project_at(cursor)
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("outcome trigger lease expired before completion")
        payload = {"trigger_id": process.trigger_id, "owner_id": process.claim_lease.owner_id, "attempt_id": process.claim_lease.attempt_id, "completed_at": at.isoformat(), "runtime_outcome_ref": outcome_ref}
        event = WorldEvent.from_payload(schema_version="world-v2.1", event_id="event:outcome-deliberation:trigger:completed:" + _digest([process.trigger_id, process.claim_lease.attempt_id]), world_id=self._ledger.world_id, event_type="TriggerProcessCompleted", logical_time=at, created_at=source_event.created_at, actor=self._owner_id, source=self._source, trace_id=source_event.trace_id, causation_id=source_event.event_id, correlation_id=source_event.correlation_id, idempotency_key="world-v2:outcome-deliberation-trigger:completion:" + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]), payload=payload)
        await self._commit_at_cursor((event,), cursor=cursor, commit_id="commit:outcome-deliberation:completed:" + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]))

    async def _project(self):
        return await asyncio.to_thread(self._ledger.project) if self._ledger.blocks_event_loop else self._ledger.project()

    async def _project_at(self, cursor):
        return await asyncio.to_thread(self._ledger.project_at, cursor) if self._ledger.blocks_event_loop else self._ledger.project_at(cursor)

    async def _lookup(self, event_id):
        return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id) if self._ledger.blocks_event_loop else self._ledger.lookup_event_commit(event_id)

    async def _commit(self, events, *, world_revision, deliberation_revision, commit_id):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.commit, events, expected_world_revision=world_revision, expected_deliberation_revision=deliberation_revision, commit_id=commit_id)
        return self._ledger.commit(events, expected_world_revision=world_revision, expected_deliberation_revision=deliberation_revision, commit_id=commit_id)

    async def _commit_at_cursor(self, events, *, cursor, commit_id):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.commit_at_cursor, events, expected_cursor=cursor, commit_id=commit_id)
        return self._ledger.commit_at_cursor(events, expected_cursor=cursor, commit_id=commit_id)

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)

    @staticmethod
    def _cursor_from_commit(commit) -> ProjectionCursor:
        return ProjectionCursor(world_revision=commit.world_revision, deliberation_revision=commit.deliberation_revision, ledger_sequence=commit.ledger_sequence)


__all__ = ["OutcomeTriggerRunResult", "OutcomeTriggerRuntime"]
