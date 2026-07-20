"""Recovery-safe worker for optional attachment-perception decisions."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort, ObservationEventLocator
from .perception_proposal_compiler import PerceptionProposalCompiler
from .pinned_turn import PinnedTurnCompiler
from .schema_core import FrozenModel
from .schemas import ClaimLease, Observation, ProjectionCursor, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PerceptionTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "processed"]
    work_status: Literal["no_change", "accepted", "rejected"] | None = None


class PerceptionTriggerRuntime:
    """Let a closed model grammar decide whether an attachment needs analysis."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        turn: PinnedTurnCompiler,
        compiler: PerceptionProposalCompiler,
        owner_id: str,
        lease_seconds: int = 120,
    ) -> None:
        if (
            not owner_id
            or lease_seconds <= 0
            or compiler.ledger is not ledger
        ):
            raise ValueError("perception trigger needs matching compiler, owner and lease")
        self._ledger = ledger
        self._turn = turn
        self._compiler = compiler
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    async def drain_one(self) -> PerceptionTriggerRunResult:
        projection = self._ledger.project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "perception_deliberation" and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return PerceptionTriggerRunResult(trigger_id="", status="idle")
        source_event, observation = self._source(process, self._cursor(projection))
        active = self._claim_or_reclaim(
            process=process, source_event=source_event, projection=projection
        )
        if active is None:
            return PerceptionTriggerRunResult(
                trigger_id=process.trigger_id, status="owned_elsewhere"
            )
        cursor = self._cursor(self._ledger.project())
        audit = await self._turn.audit_observation(
            observation=observation, observation_event=source_event, cursor=cursor
        )
        if audit.proposal_id is None:
            self._complete(active, source_event, "no-change")
            return PerceptionTriggerRunResult(
                trigger_id=process.trigger_id, status="processed", work_status="no_change"
            )
        try:
            compiled = self._compiler.accept(
                world_id=self._ledger.world_id,
                cursor=audit.cursor,
                proposal_id=audit.proposal_id,
                actor=self._owner_id,
                source="world-v2:perception-trigger",
            )
        except ValueError:
            # Missing/revoked/ambiguous enforcement authority is a terminal
            # refusal for this exact audited proposal.  It must not spin or
            # delay the already-completed text turn.
            self._complete(active, source_event, "rejected")
            return PerceptionTriggerRunResult(
                trigger_id=process.trigger_id, status="processed", work_status="rejected"
            )
        self._complete(active, source_event, compiled.status)
        return PerceptionTriggerRunResult(
            trigger_id=process.trigger_id,
            status="processed",
            work_status="accepted" if compiled.status == "accepted" else "no_change",
        )

    def _source(
        self, process: TriggerProcess, cursor: ProjectionCursor
    ) -> tuple[WorldEvent, Observation]:
        projection = self._ledger.project_at(cursor)
        reference = next(
            (
                item
                for item in projection.message_observations
                if item.observation_id == process.source_evidence_ref
            ),
            None,
        )
        if reference is None:
            raise ValueError("perception source Observation is unavailable")
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
        if len(located) != 1:
            raise ValueError("perception source proof is incomplete")
        event = located[0].event
        observation = Observation.model_validate_json(event.payload_json)
        if (
            not observation.attachment_refs
            or process.trigger_ref != f"perception:{observation.observation_id}"
        ):
            raise ValueError("perception trigger does not bind attachment evidence")
        return event, observation

    def _claim_or_reclaim(
        self, *, process: TriggerProcess, source_event: WorldEvent, projection
    ) -> TriggerProcess | None:
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:perception:" + _digest(
            {"trigger": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
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
            identity = "perception:claim:" + _digest(
                [self._ledger.world_id, process.trigger_id, attempt_id, event_type]
            )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:perception-claim:" + _digest([process.trigger_id, attempt_id]),
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source="world-v2:perception-trigger",
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        self._ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            commit_id="commit:perception-claim:" + _digest([process.trigger_id, attempt_id]),
        )
        return claimed

    def _complete(self, process: TriggerProcess, source_event: WorldEvent, outcome: str) -> None:
        if process.claim_lease is None:
            raise ValueError("perception completion requires a claim")
        projection = self._ledger.project()
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("perception trigger lease expired")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": f"outcome:{process.trigger_id}:{outcome}",
        }
        identity = domain_idempotency_key(
            event_type="TriggerProcessCompleted", world_id=self._ledger.world_id, payload=payload
        ) or "perception:completed:" + _digest(payload)
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:perception-completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessCompleted",
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source="world-v2:perception-trigger",
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        self._ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            commit_id="commit:perception-completed:" + _digest([process.trigger_id, outcome]),
        )

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


__all__ = ["PerceptionTriggerRunResult", "PerceptionTriggerRuntime"]
