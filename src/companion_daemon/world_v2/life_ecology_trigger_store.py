"""Ledger-backed ownership for one source-bound Life Ecology wake.

This adapter deliberately stores no state outside the World v2 ledger.  The
generic ``TriggerProcess`` is the durable process record; compare-and-swap
ledger commits turn its ``open -> claimed -> terminal`` lifecycle into the
small ``LifeEcologyTriggerStore`` interface consumed by ``LifeEcologyRuntime``.
Consequently a process survives a worker restart and competing workers converge
on the same immutable trigger rather than creating a second ecology run.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import re

from .errors import ConcurrencyConflict, IdempotencyConflict
from .event_identity import domain_idempotency_key
from .life_ecology_contract import (
    LIFE_ECOLOGY_PROCESS_KIND,
    LIFE_ECOLOGY_WAKE_EVENT_TYPES,
    LifeEcologyRunClaim,
    LifeEcologyRunKey,
    life_ecology_trigger_id,
    life_ecology_trigger_ref,
    parse_life_ecology_trigger_ref,
    validate_life_ecology_run_key,
)
from .schemas import ClaimLease, TriggerProcess, WorldEvent


_OUTCOME = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_MAX_CAS_RETRIES = 8


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


class LedgerLifeEcologyTriggerStore:
    """Durable ``LifeEcologyTriggerStore`` adapter backed solely by the ledger.

    The interface intentionally has no lookup, lock, or recovery method.  A
    caller gets one of three facts for a key: it owns the current lease, a live
    owner already exists, or a terminal run exists.  Expired leases are
    reclaimed through the normal TriggerProcess lineage, never by deleting or
    overwriting historical state.
    """

    def __init__(
        self,
        *,
        ledger,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:life-ecology-trigger-store",
    ) -> None:
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("life ecology trigger store requires owner_id")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("life ecology trigger store requires positive lease_seconds")
        if not isinstance(source, str) or not source:
            raise ValueError("life ecology trigger store requires source")
        self._ledger = ledger
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def claim_or_join(
        self, *, key: LifeEcologyRunKey, trace_id: str, correlation_id: str
    ) -> LifeEcologyRunClaim:
        validate_life_ecology_run_key(key)
        if key.world_id != self._ledger.world_id:
            raise ValueError("life ecology run key belongs to another world")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("life ecology claim requires trace_id")
        if not isinstance(correlation_id, str) or not correlation_id:
            raise ValueError("life ecology claim requires correlation_id")

        trigger_id = life_ecology_trigger_id(
            world_id=key.world_id,
            wake_event_ref=key.wake_event_ref,
            catalog_version=key.catalog_version,
        )
        for _ in range(_MAX_CAS_RETRIES):
            projection = await self._project()
            source_event = await self._verified_wake(key=key, projection=projection)
            process = next(
                (item for item in projection.trigger_processes if item.trigger_id == trigger_id),
                None,
            )
            if process is None:
                opened = TriggerProcess(
                    trigger_id=trigger_id,
                    trigger_ref=life_ecology_trigger_ref(
                        wake_event_ref=key.wake_event_ref,
                        catalog_version=key.catalog_version,
                    ),
                    process_kind=LIFE_ECOLOGY_PROCESS_KIND,
                    source_evidence_ref=key.wake_event_ref,
                    state="open",
                )
                if await self._try_commit(
                    self._opened_event(
                        process=opened,
                        source_event=source_event,
                        logical_time=projection.logical_time or source_event.logical_time,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    ),
                    projection=projection,
                ):
                    continue
                continue
            if process.process_kind != LIFE_ECOLOGY_PROCESS_KIND:
                raise ValueError("life ecology trigger identity is occupied by another process kind")
            if process.state == "terminal":
                return LifeEcologyRunClaim(trigger_id=trigger_id, state="completed")
            if process.state == "claimed" and process.claim_lease is not None:
                logical_time = projection.logical_time or source_event.logical_time
                if logical_time <= process.claim_lease.expires_at:
                    return LifeEcologyRunClaim(trigger_id=trigger_id, state="joined")
                event_type = "TriggerProcessReclaimed"
            elif process.state == "open":
                logical_time = projection.logical_time or source_event.logical_time
                event_type = "TriggerProcessClaimed"
            else:
                raise ValueError("life ecology trigger state is invalid")

            attempt_id = "attempt:life-ecology:" + _digest(
                {"trigger_id": trigger_id, "attempt": len(process.attempt_ids) + 1}
            )
            claimed = process.model_copy(
                update={
                    "state": "claimed",
                    "claim_lease": ClaimLease(
                        owner_id=self._owner_id,
                        attempt_id=attempt_id,
                        acquired_at=logical_time,
                        expires_at=logical_time + timedelta(seconds=self._lease_seconds),
                    ),
                    "attempt_ids": (*process.attempt_ids, attempt_id),
                }
            )
            if await self._try_commit(
                self._claim_event(
                    process=claimed,
                    event_type=event_type,
                    source_event=source_event,
                    logical_time=logical_time,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                ),
                projection=projection,
            ):
                return LifeEcologyRunClaim(trigger_id=trigger_id, state="owned")
        raise ConcurrencyConflict("life ecology trigger claim did not converge")

    async def complete(
        self, *, key: LifeEcologyRunKey, trigger_id: str, outcome: str
    ) -> None:
        validate_life_ecology_run_key(key)
        if key.world_id != self._ledger.world_id:
            raise ValueError("life ecology run key belongs to another world")
        if not isinstance(outcome, str) or not _OUTCOME.fullmatch(outcome):
            raise ValueError("life ecology completion outcome is invalid")
        expected_trigger_id = life_ecology_trigger_id(
            world_id=key.world_id,
            wake_event_ref=key.wake_event_ref,
            catalog_version=key.catalog_version,
        )
        if trigger_id != expected_trigger_id:
            raise ValueError("life ecology completion does not bind its run key")

        for _ in range(_MAX_CAS_RETRIES):
            projection = await self._project()
            process = next(
                (item for item in projection.trigger_processes if item.trigger_id == trigger_id),
                None,
            )
            if process is None or process.process_kind != LIFE_ECOLOGY_PROCESS_KIND:
                raise ValueError("life ecology trigger is unavailable")
            outcome_ref = f"life-ecology:{outcome}"
            if process.state == "terminal":
                if process.runtime_outcome_ref != outcome_ref:
                    raise ValueError("life ecology terminal outcome conflicts with completion")
                return
            if process.state != "claimed" or process.claim_lease is None:
                raise ValueError("life ecology trigger must be claimed before completion")
            if process.claim_lease.owner_id != self._owner_id:
                raise ValueError("life ecology completion does not own the active claim lease")
            source_event = await self._source_event(process)
            logical_time = projection.logical_time or source_event.logical_time
            completed_at = max(logical_time, process.claim_lease.acquired_at)
            if completed_at > process.claim_lease.expires_at:
                raise ValueError("life ecology lease expired before completion")
            payload = {
                "trigger_id": process.trigger_id,
                "owner_id": process.claim_lease.owner_id,
                "attempt_id": process.claim_lease.attempt_id,
                "completed_at": completed_at.isoformat(),
                "runtime_outcome_ref": outcome_ref,
            }
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:life-ecology:completed:"
                + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome]),
                world_id=key.world_id,
                event_type="TriggerProcessCompleted",
                logical_time=completed_at,
                created_at=source_event.created_at,
                actor=self._owner_id,
                source=self._source,
                trace_id=source_event.trace_id,
                causation_id=source_event.event_id,
                correlation_id=source_event.correlation_id,
                idempotency_key="world-v2:life-ecology-trigger:completed:"
                + _digest([key.world_id, process.trigger_id, process.claim_lease.attempt_id]),
                payload=payload,
            )
            if await self._try_commit(event, projection=projection):
                return
        raise ConcurrencyConflict("life ecology trigger completion did not converge")

    async def _verified_wake(self, *, key: LifeEcologyRunKey, projection) -> WorldEvent:
        source_event = await self._source_event_ref(key.wake_event_ref)
        if source_event.event_type not in LIFE_ECOLOGY_WAKE_EVENT_TYPES:
            raise ValueError("life ecology trigger source is not a durable wake")
        committed = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == key.wake_event_ref
            ),
            None,
        )
        if (
            committed is None
            or committed.event_type != source_event.event_type
            or committed.payload_hash != source_event.payload_hash
            or committed.logical_time != source_event.logical_time
        ):
            raise ValueError("life ecology trigger source is not exactly committed")
        return source_event

    async def _source_event(self, process: TriggerProcess) -> WorldEvent:
        if process.source_evidence_ref is None:
            raise ValueError("life ecology trigger has no source evidence")
        return await self._source_event_ref(process.source_evidence_ref)

    async def _source_event_ref(self, event_id: str) -> WorldEvent:
        located = await self._lookup(event_id)
        if located is None or located[0].world_id != self._ledger.world_id:
            raise ValueError("life ecology trigger source is unavailable")
        return located[0]

    def _opened_event(
        self,
        *,
        process: TriggerProcess,
        source_event: WorldEvent,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> WorldEvent:
        payload = {"process": process.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type="TriggerProcessOpened", world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("life ecology opened trigger has no domain identity")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:life-ecology:opened:" + _digest(process.trigger_id),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessOpened",
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=trace_id,
            causation_id=source_event.event_id,
            correlation_id=correlation_id,
            idempotency_key=identity,
            payload=payload,
        )

    def _claim_event(
        self,
        *,
        process: TriggerProcess,
        event_type: str,
        source_event: WorldEvent,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> WorldEvent:
        payload = {"process": process.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("life ecology claimed trigger has no domain identity")
        attempt_id = process.claim_lease.attempt_id if process.claim_lease is not None else "missing"
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:life-ecology:"
            + event_type.removeprefix("TriggerProcess").lower()
            + ":"
            + _digest([process.trigger_id, attempt_id]),
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=trace_id,
            causation_id=source_event.event_id,
            correlation_id=correlation_id,
            idempotency_key=identity,
            payload=payload,
        )

    async def _try_commit(self, event: WorldEvent, *, projection) -> bool:
        try:
            await self._commit(
                (event,),
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
            )
        except (ConcurrencyConflict, IdempotencyConflict):
            return False
        return True

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _lookup(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _commit(self, events, *, world_revision: int, deliberation_revision: int):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit,
                events,
                expected_world_revision=world_revision,
                expected_deliberation_revision=deliberation_revision,
            )
        return self._ledger.commit(
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
        )


__all__ = [
    "LIFE_ECOLOGY_PROCESS_KIND",
    "LIFE_ECOLOGY_WAKE_EVENT_TYPES",
    "LedgerLifeEcologyTriggerStore",
    "life_ecology_trigger_id",
    "life_ecology_trigger_ref",
    "parse_life_ecology_trigger_ref",
    "validate_life_ecology_run_key",
]
