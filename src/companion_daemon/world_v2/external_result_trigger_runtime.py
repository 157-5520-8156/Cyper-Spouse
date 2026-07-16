"""Exactly-once lifecycle worker for accepted external semantic results.

The first installed consumer is read-only-tool output.  It deliberately owns
only the trigger lease and terminal marker: a result-aware deliberator may
decide that no visible action is appropriate, but cannot mutate a result or
turn the provider receipt into a fact.  A later reply-producing deliberation
must use its own closed proposal grammar.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal, Protocol

from .event_identity import domain_idempotency_key
from .read_only_tool import external_result_trigger_id
from .schema_core import FrozenModel
from .schemas import ClaimLease, ToolResultProjection, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ToolResultDeliberator(Protocol):
    async def deliberate(self, result: ToolResultProjection) -> Literal["no_visible_action"]: ...


class NoopToolResultDeliberator:
    async def deliberate(self, result: ToolResultProjection) -> Literal["no_visible_action"]:
        del result
        return "no_visible_action"


class ExternalResultTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal["no_visible_action"] | None = None


class ExternalResultTriggerRuntime:
    """Claim/reclaim one immutable tool result and finish it once.

    The worker has no Action executor, payload store, or ledger mutation seam
    besides durable trigger lifecycle events.  It is therefore safe to retry
    after a process crash; a terminal process joins rather than deliberating a
    second time.
    """

    def __init__(self, *, ledger, deliberator: ToolResultDeliberator, owner_id: str, lease_seconds: int = 120) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("external result trigger runtime needs owner and positive lease")
        self._ledger = ledger
        self._deliberator = deliberator
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds

    async def drain_one(self) -> ExternalResultTriggerRunResult:
        projection = self._ledger.project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "external_result_deliberation" and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return ExternalResultTriggerRunResult(trigger_id="", status="idle")
        result = self._source(process, projection)
        active = self._claim_or_reclaim(process=process, result=result, projection=projection)
        if active is None:
            return ExternalResultTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")
        work = await self._deliberator.deliberate(result)
        if work != "no_visible_action":
            raise ValueError("external result deliberator returned unsupported output")
        self._complete(process=active, result=result)
        return ExternalResultTriggerRunResult(
            trigger_id=active.trigger_id, status="processed", work_status=work
        )

    def _source(self, process: TriggerProcess, projection) -> ToolResultProjection:
        result = next(
            (item for item in projection.tool_results if item.accepted_event_ref == process.source_evidence_ref),
            None,
        )
        stored = self._ledger.lookup_event_commit(process.source_evidence_ref or "")
        if (
            result is None
            or stored is None
            or stored[0].event_type != "ToolResultAccepted"
            or process.trigger_id
            != external_result_trigger_id(world_id=self._ledger.world_id, result_id=result.result_id)
            or process.trigger_ref != f"external-result:{result.result_id}"
        ):
            raise ValueError("external result trigger source authority is unavailable")
        return result

    def _claim_or_reclaim(self, *, process: TriggerProcess, result: ToolResultProjection, projection) -> TriggerProcess | None:
        at = projection.logical_time or result.accepted_at
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:external-result:" + _digest(
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
        identity = domain_idempotency_key(event_type=event_type, world_id=self._ledger.world_id, payload=payload)
        if identity is None:
            raise ValueError("external result trigger claim identity is unavailable")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:{process.trigger_id}:{event_type}:{attempt_id}",
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=at,
            created_at=at,
            actor=self._owner_id,
            source="world-v2:external-result-trigger",
            trace_id="trace:" + process.trigger_id,
            causation_id=process.source_evidence_ref or process.trigger_id,
            correlation_id="external-result:" + result.result_id,
            idempotency_key=identity,
            payload=payload,
        )
        self._ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            commit_id="commit:external-result-trigger:claim:" + _digest([process.trigger_id, attempt_id]),
        )
        return claimed

    def _complete(self, *, process: TriggerProcess, result: ToolResultProjection) -> None:
        if process.claim_lease is None:
            raise ValueError("external result completion requires a claim lease")
        projection = self._ledger.project()
        at = max(projection.logical_time or result.accepted_at, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("external result trigger lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": f"outcome:{process.trigger_id}:no-visible-action",
        }
        identity = domain_idempotency_key(
            event_type="TriggerProcessCompleted", world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            identity = "external-result-trigger:completed:" + _digest(
                {"trigger_id": process.trigger_id, "attempt_id": process.claim_lease.attempt_id}
            )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:{process.trigger_id}:completed:{process.claim_lease.attempt_id}",
            world_id=self._ledger.world_id,
            event_type="TriggerProcessCompleted",
            logical_time=at,
            created_at=at,
            actor=self._owner_id,
            source="world-v2:external-result-trigger",
            trace_id="trace:" + process.trigger_id,
            causation_id=process.source_evidence_ref or process.trigger_id,
            correlation_id="external-result:" + result.result_id,
            idempotency_key=identity,
            payload=payload,
        )
        self._ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            commit_id="commit:external-result-trigger:complete:" + _digest(process.trigger_id),
        )


__all__ = [
    "ExternalResultTriggerRunResult",
    "ExternalResultTriggerRuntime",
    "NoopToolResultDeliberator",
    "ToolResultDeliberator",
]
