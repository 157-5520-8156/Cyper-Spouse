"""Durable trigger derivation for a fresh relationship deliberation.

This module turns either one accepted appraisal or one ordinary observed
interaction into a durable work item.  It does not interpret either source,
mutate relationship state, or choose a relationship stage; those remain the
relationship deliberation and acceptance vertical's responsibilities.
"""

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


def relationship_deliberation_trigger_id(*, world_id: str, appraisal_event_id: str) -> str:
    """Return the deterministic, effect-once trigger id for an appraisal."""

    return "trigger:relationship-deliberation:" + _digest(
        {"world_id": world_id, "appraisal_event_id": appraisal_event_id}
    )


def relationship_continuity_trigger_id(
    *, world_id: str, observation_event_id: str
) -> str:
    """Return the effect-once identity for ordinary-interaction consideration."""

    return "trigger:relationship-continuity:" + _digest(
        {"world_id": world_id, "observation_event_id": observation_event_id}
    )


def relationship_continuity_trigger_open_event(
    *, observation_event: WorldEvent, owner_id: str
) -> WorldEvent:
    """Open one neutral relationship opportunity from committed interaction.

    Unlike the accepted-appraisal path this helper intentionally does not
    claim the process.  Several ordinary turns can therefore join the one
    still-open relationship opportunity before the worker owns it.  The
    scheduler decides only that the source is available; the relationship
    model remains the sole semantic authority for ``signal`` or ``no_change``.
    """

    if observation_event.event_type != "ObservationRecorded":
        raise ValueError("relationship continuity trigger requires ObservationRecorded")
    if not owner_id:
        raise ValueError("relationship continuity trigger owner is required")
    trigger_id = relationship_continuity_trigger_id(
        world_id=observation_event.world_id,
        observation_event_id=observation_event.event_id,
    )
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"relationship-continuity:{observation_event.event_id}",
        process_kind="relationship_deliberation",
        source_evidence_ref=observation_event.event_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessOpened",
        world_id=observation_event.world_id,
        payload=payload,
    )
    if identity is None:
        raise ValueError("relationship continuity trigger event identity missing")
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:relationship-continuity:opened:"
        + _digest([trigger_id, observation_event.payload_hash]),
        world_id=observation_event.world_id,
        event_type="TriggerProcessOpened",
        logical_time=observation_event.logical_time,
        created_at=observation_event.created_at,
        actor=owner_id,
        source="world-v2:relationship-trigger",
        trace_id=observation_event.trace_id,
        causation_id=observation_event.event_id,
        correlation_id=observation_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


def relationship_deliberation_trigger_events(
    *,
    appraisal_event: WorldEvent,
    owner_id: str,
    lease_seconds: int = 120,
    claimed_at: datetime | None = None,
) -> tuple[WorldEvent, WorldEvent]:
    """Open and claim one relationship turn for an ``AppraisalAccepted`` event.

    The opening event is source-bound to the accepted appraisal.  The claim is
    emitted in the same deterministic helper call so the producer can append
    both events atomically, while recovery still has a normal durable claim
    lease to reason about.  ``claimed_at`` is the caller's current projection
    Logical Time; omitting it preserves historical same-turn derivation.
    """

    if appraisal_event.event_type != "AppraisalAccepted":
        raise ValueError("relationship trigger requires AppraisalAccepted")
    if not owner_id or lease_seconds <= 0:
        raise ValueError("relationship trigger owner and positive lease are required")
    claim_time = appraisal_event.logical_time if claimed_at is None else claimed_at
    if claim_time.tzinfo is None or claim_time.utcoffset() is None:
        raise ValueError("relationship trigger claim time must be timezone-aware")
    if claim_time < appraisal_event.logical_time:
        raise ValueError("relationship trigger claim cannot precede its appraisal")
    trigger_id = relationship_deliberation_trigger_id(
        world_id=appraisal_event.world_id, appraisal_event_id=appraisal_event.event_id
    )
    attempt_id = "attempt:relationship-deliberation:" + _digest(
        {"trigger_id": trigger_id, "appraisal_payload_hash": appraisal_event.payload_hash}
    )
    opened = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"relationship:{appraisal_event.event_id}",
        process_kind="relationship_deliberation",
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
            raise ValueError("relationship trigger event identity missing")
        events.append(
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=(
                    f"event:relationship-deliberation:{role}:"
                    f"{_digest([trigger_id, attempt_id, role])}"
                ),
                world_id=appraisal_event.world_id,
                event_type=event_type,
                logical_time=appraisal_event.logical_time if role == "opened" else claim_time,
                created_at=appraisal_event.created_at,
                actor=owner_id,
                source="world-v2:relationship-trigger",
                trace_id=appraisal_event.trace_id,
                causation_id=appraisal_event.event_id if not events else events[-1].event_id,
                correlation_id=appraisal_event.correlation_id,
                idempotency_key=identity,
                payload=payload,
            )
        )
    return events[0], events[1]


__all__ = [
    "relationship_continuity_trigger_id",
    "relationship_continuity_trigger_open_event",
    "relationship_deliberation_trigger_events",
    "relationship_deliberation_trigger_id",
]
