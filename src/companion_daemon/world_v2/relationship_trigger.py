"""Durable trigger derivation for a fresh relationship deliberation.

This module deliberately only turns one accepted appraisal into one durable
work item.  It does not interpret the appraisal, mutate relationship state, or
choose a relationship stage; those remain the relationship deliberation and
acceptance vertical's responsibilities.
"""

from __future__ import annotations

from datetime import timedelta
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


def relationship_deliberation_trigger_events(
    *, appraisal_event: WorldEvent, owner_id: str, lease_seconds: int = 120
) -> tuple[WorldEvent, WorldEvent]:
    """Open and claim one relationship turn for an ``AppraisalAccepted`` event.

    The opening event is source-bound to the accepted appraisal.  The claim is
    emitted in the same deterministic helper call so the producer can append
    both events atomically, while recovery still has a normal durable claim
    lease to reason about.
    """

    if appraisal_event.event_type != "AppraisalAccepted":
        raise ValueError("relationship trigger requires AppraisalAccepted")
    if not owner_id or lease_seconds <= 0:
        raise ValueError("relationship trigger owner and positive lease are required")
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
                acquired_at=appraisal_event.logical_time,
                expires_at=appraisal_event.logical_time + timedelta(seconds=lease_seconds),
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
                logical_time=appraisal_event.logical_time,
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
    "relationship_deliberation_trigger_events",
    "relationship_deliberation_trigger_id",
]
