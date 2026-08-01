"""Durable trigger derivation for a fresh Affect deliberation."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from .event_identity import domain_idempotency_key
from .schemas import ClaimLease, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def affect_deliberation_trigger_id(*, world_id: str, appraisal_event_id: str) -> str:
    return "trigger:affect-deliberation:" + _digest(
        {"world_id": world_id, "appraisal_event_id": appraisal_event_id}
    )


def affect_deliberation_trigger_events(
    *,
    appraisal_event: WorldEvent,
    owner_id: str,
    lease_seconds: int = 120,
    claimed_at: datetime | None = None,
) -> tuple[WorldEvent, WorldEvent]:
    """Open and claim the one effect-once affect turn for an AppraisalAccepted event.

    ``claimed_at`` is the caller's current projection Logical Time.  Omitting
    it preserves the historical same-turn derivation used by existing callers.
    """

    if appraisal_event.event_type != "AppraisalAccepted":
        raise ValueError("affect trigger requires AppraisalAccepted")
    if not owner_id or lease_seconds <= 0:
        raise ValueError("affect trigger owner and positive lease are required")
    claim_time = appraisal_event.logical_time if claimed_at is None else claimed_at
    if claim_time.tzinfo is None or claim_time.utcoffset() is None:
        raise ValueError("affect trigger claim time must be timezone-aware")
    if claim_time < appraisal_event.logical_time:
        raise ValueError("affect trigger claim cannot precede its appraisal")
    trigger_id = affect_deliberation_trigger_id(
        world_id=appraisal_event.world_id, appraisal_event_id=appraisal_event.event_id
    )
    attempt_id = "attempt:affect-deliberation:" + _digest(
        {"trigger_id": trigger_id, "appraisal_payload_hash": appraisal_event.payload_hash}
    )
    opened = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"affect:{appraisal_event.event_id}",
        process_kind="affect_deliberation",
        source_evidence_ref=appraisal_event.event_id,
        state="open",
    )
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id=owner_id,
                attempt_id=attempt_id,
                acquired_at=claim_time,
                expires_at=claim_time + timedelta(seconds=lease_seconds),
            ),
            "attempt_ids": (attempt_id,),
        }
    )
    events: list[WorldEvent] = []
    for role, event_type, process in (
        ("opened", "TriggerProcessOpened", opened),
        ("claimed", "TriggerProcessClaimed", claimed),
    ):
        payload = {"process": process.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type=event_type, world_id=appraisal_event.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("affect trigger event identity missing")
        events.append(
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=f"event:affect-deliberation:{role}:{_digest([trigger_id, attempt_id, role])}",
                world_id=appraisal_event.world_id,
                event_type=event_type,
                logical_time=appraisal_event.logical_time if role == "opened" else claim_time,
                created_at=appraisal_event.created_at,
                actor=owner_id,
                source="world-v2:affect-trigger",
                trace_id=appraisal_event.trace_id,
                causation_id=appraisal_event.event_id if not events else events[-1].event_id,
                correlation_id=appraisal_event.correlation_id,
                idempotency_key=identity,
                payload=payload,
            )
        )
    return events[0], events[1]


__all__ = ["affect_deliberation_trigger_events", "affect_deliberation_trigger_id"]
