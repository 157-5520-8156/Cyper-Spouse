"""Recovery-safe executor for persisted Affect deliberation triggers.

The trigger is deliberately separate from interactive reply generation.  It
may take a fresh model turn, but its durable state lets a service restart
finish a partially completed attempt without re-running that model turn.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

from .affect_deliberation_worker import AffectDeliberationWorker
from .affect_trigger import affect_deliberation_trigger_id
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .schema_core import FrozenModel
from .schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class AffectTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal["no_proposal", "no_change", "accepted"] | None = None


class AffectTriggerRuntime:
    """Drain one durable affect trigger with lease and recovery semantics."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        worker: AffectDeliberationWorker,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:affect-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("affect trigger runtime needs owner and positive lease")
        if worker.ledger is not ledger:
            raise ValueError("affect trigger worker must own the exact ledger")
        self._ledger = ledger
        self._worker = worker
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> AffectTriggerRunResult:
        """Finish at most one affect trigger from the current ledger head."""

        projection = self._ledger.project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "affect_deliberation" and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return AffectTriggerRunResult(trigger_id="", status="idle")
        source_event = self._source_event(process)
        existing_outcome = self._accepted_outcome_ref(
            projection=projection, appraisal_event_id=source_event.event_id
        )
        active = self._claim_or_reclaim(process=process, source_event=source_event, projection=projection)
        if active is None:
            return AffectTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")
        if existing_outcome is not None:
            self._complete(
                process=active,
                source_event=source_event,
                cursor=self._cursor(),
                outcome_ref=existing_outcome,
            )
            return AffectTriggerRunResult(
                trigger_id=process.trigger_id, status="completed_existing", work_status="accepted"
            )
        current = self._cursor()
        work = await self._worker.process(
            world_id=self._ledger.world_id,
            cursor=current,
            appraisal_event=source_event,
        )
        self._complete(
            process=active,
            source_event=source_event,
            cursor=self._cursor(),
            outcome_ref=f"outcome:{process.trigger_id}:{work.status}",
        )
        return AffectTriggerRunResult(
            trigger_id=process.trigger_id, status="processed", work_status=work.status
        )

    def _source_event(self, process: TriggerProcess) -> WorldEvent:
        event_id = process.source_evidence_ref
        located = self._ledger.lookup_event_commit(event_id) if event_id is not None else None
        if located is None or located[0].event_type != "AppraisalAccepted":
            raise ValueError("affect trigger source appraisal is unavailable")
        if affect_deliberation_trigger_id(
            world_id=located[0].world_id, appraisal_event_id=located[0].event_id
        ) != process.trigger_id:
            raise ValueError("affect trigger identity does not bind source appraisal")
        return located[0]

    def _accepted_outcome_ref(self, *, projection, appraisal_event_id: str) -> str | None:
        """Find an accepted typed proposal descended from this exact appraisal."""

        for decision in projection.acceptance_decisions:
            if decision.manifest_version != "affect-acceptance.1" or decision.acceptance_event_ref is None:
                continue
            accepted = self._ledger.lookup_event_commit(decision.acceptance_event_ref)
            if accepted is None:
                continue
            proposal_event_ref = accepted[0].payload().get("proposal_event_ref")
            proposal = (
                self._ledger.lookup_event_commit(proposal_event_ref)
                if isinstance(proposal_event_ref, str)
                else None
            )
            source_audit = proposal[0].payload().get("source_audit") if proposal is not None else None
            if not isinstance(source_audit, dict):
                continue
            audit_ref = source_audit.get("proposal_event_ref")
            audit = self._ledger.lookup_event_commit(audit_ref) if isinstance(audit_ref, str) else None
            if audit is not None and audit[0].payload().get("trigger_ref") == appraisal_event_id:
                return decision.acceptance_event_ref
        return None

    def _claim_or_reclaim(self, *, process: TriggerProcess, source_event: WorldEvent, projection) -> TriggerProcess | None:
        current_time = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and current_time <= process.claim_lease.expires_at:
                return process
            if current_time < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:affect-deliberation:" + _digest(
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
        identity = domain_idempotency_key(event_type=event_type, world_id=self._ledger.world_id, payload=payload)
        if identity is None:
            raise ValueError("affect trigger claim identity missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:affect-trigger:{event_type.lower()}:{_digest([process.trigger_id, attempt_id])}",
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
            commit_id=f"commit:affect-trigger:{event_type.lower()}:{_digest([process.trigger_id, attempt_id])}",
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
        assert process.claim_lease is not None
        completed_at = max(
            self._ledger.project_at(cursor).logical_time or source_event.logical_time,
            process.claim_lease.acquired_at,
        )
        if completed_at > process.claim_lease.expires_at:
            raise ValueError("affect trigger lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": completed_at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        identity = "world-v2:affect-trigger:completion:" + _digest(
            [self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]
        )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:affect-trigger:completed:" + _digest([process.trigger_id, process.claim_lease.attempt_id]),
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
            commit_id="commit:affect-trigger:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]),
        )

    def _cursor(self) -> ProjectionCursor:
        projection = self._ledger.project()
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


__all__ = ["AffectTriggerRunResult", "AffectTriggerRuntime"]
