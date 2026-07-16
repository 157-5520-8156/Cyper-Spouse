"""Exactly-once no-change consumer for immutable perception descriptors."""

from __future__ import annotations
from datetime import timedelta
import hashlib
import json
from typing import Literal, Protocol
from .event_identity import domain_idempotency_key
from .perception import perception_result_trigger_id
from .schema_core import FrozenModel
from .schemas import ClaimLease, PerceptionResultProjection, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PerceptionResultDeliberator(Protocol):
    async def deliberate(
        self, result: PerceptionResultProjection
    ) -> Literal["no_visible_action"]: ...


class NoopPerceptionResultDeliberator:
    async def deliberate(self, result: PerceptionResultProjection) -> Literal["no_visible_action"]:
        del result
        return "no_visible_action"


class PerceptionResultTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "processed"]


class PerceptionResultTriggerRuntime:
    def __init__(
        self,
        *,
        ledger,
        deliberator: PerceptionResultDeliberator,
        owner_id: str,
        lease_seconds: int = 120,
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("perception result trigger needs owner and lease")
        self._ledger, self._deliberator, self._owner, self._lease = (
            ledger,
            deliberator,
            owner_id,
            lease_seconds,
        )

    async def drain_one(self) -> PerceptionResultTriggerRunResult:
        projection = self._ledger.project()
        process = next(
            (
                x
                for x in projection.trigger_processes
                if x.process_kind == "perception_result_deliberation" and x.state != "terminal"
            ),
            None,
        )
        if process is None:
            return PerceptionResultTriggerRunResult(trigger_id="", status="idle")
        result = next(
            (
                x
                for x in projection.perception_results
                if x.accepted_event_ref == process.source_evidence_ref
            ),
            None,
        )
        stored = self._ledger.lookup_event_commit(process.source_evidence_ref or "")
        if (
            result is None
            or stored is None
            or stored[0].event_type != "PerceptionResultAccepted"
            or process.trigger_id
            != perception_result_trigger_id(
                world_id=self._ledger.world_id, result_id=result.result_id
            )
        ):
            raise ValueError("perception result source authority is unavailable")
        at = projection.logical_time or result.accepted_at
        if (
            process.state == "claimed"
            and process.claim_lease is not None
            and process.claim_lease.owner_id != self._owner
            and at < process.claim_lease.expires_at
        ):
            return PerceptionResultTriggerRunResult(
                trigger_id=process.trigger_id, status="owned_elsewhere"
            )
        if (
            process.state != "claimed"
            or process.claim_lease is None
            or process.claim_lease.owner_id != self._owner
            or at > process.claim_lease.expires_at
        ):
            attempt = "attempt:perception-result:" + _digest(
                {"trigger": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
            )
            claimed = process.model_copy(
                update={
                    "state": "claimed",
                    "claim_lease": ClaimLease(
                        owner_id=self._owner,
                        attempt_id=attempt,
                        acquired_at=at,
                        expires_at=at + timedelta(seconds=self._lease),
                    ),
                    "attempt_ids": (*process.attempt_ids, attempt),
                }
            )
            event_type = (
                "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
            )
            self._commit(
                event_type, {"process": claimed.model_dump(mode="json")}, process, at, attempt
            )
            process = claimed
        if await self._deliberator.deliberate(result) != "no_visible_action":
            raise ValueError("perception result deliberator returned unsupported output")
        if process.claim_lease is None:
            raise ValueError("perception result completion needs claim")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": f"outcome:{process.trigger_id}:no-visible-action",
        }
        self._commit(
            "TriggerProcessCompleted", payload, process, at, process.claim_lease.attempt_id
        )
        return PerceptionResultTriggerRunResult(trigger_id=process.trigger_id, status="processed")

    def _commit(
        self, event_type: str, payload: dict[str, object], process: TriggerProcess, at, suffix: str
    ) -> None:
        projection = self._ledger.project()
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        self._ledger.commit(
            (
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id=f"event:{process.trigger_id}:{event_type}:{suffix}",
                    world_id=self._ledger.world_id,
                    event_type=event_type,
                    logical_time=at,
                    created_at=at,
                    actor=self._owner,
                    source="world-v2:perception-result-trigger",
                    trace_id="trace:" + process.trigger_id,
                    causation_id=process.source_evidence_ref or process.trigger_id,
                    correlation_id="perception-result:" + process.trigger_id,
                    idempotency_key=identity or _digest(payload),
                    payload=payload,
                ),
            ),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            commit_id="commit:perception-result:"
            + _digest([process.trigger_id, event_type, suffix]),
        )


__all__ = [
    "NoopPerceptionResultDeliberator",
    "PerceptionResultDeliberator",
    "PerceptionResultTriggerRunResult",
    "PerceptionResultTriggerRuntime",
]
