"""Durably schedule one deterministic adjustment for an accepted signal.

The relationship deliberation lane creates an immutable signal.  This module
does not interpret that signal; it only provides the source-bound work item
which lets the slow-variable compiler run later, recover after a restart, and
remain exactly-once.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .event_identity import domain_idempotency_key
from .schemas import TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def relationship_adjustment_trigger_id(*, world_id: str, signal_event_id: str) -> str:
    """Return the deterministic trigger identity for one accepted signal."""

    return "trigger:relationship-adjustment:" + _digest(
        {"world_id": world_id, "signal_event_id": signal_event_id}
    )


def relationship_adjustment_trigger_open_event(
    *, signal_event: WorldEvent, logical_time: datetime
) -> WorldEvent:
    """Open (but deliberately do not claim) the adjustment work item.

    A signal can be accepted before the scheduler gets CPU.  The claim must
    start at the projection's current logical time, so claiming is owned by
    the runtime instead of being forged at the source event's old timestamp.
    """

    if signal_event.event_type != "RelationshipSignalAccepted":
        raise ValueError("relationship adjustment trigger requires RelationshipSignalAccepted")
    trigger_id = relationship_adjustment_trigger_id(
        world_id=signal_event.world_id, signal_event_id=signal_event.event_id
    )
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"relationship-adjustment:{signal_event.event_id}",
        process_kind="relationship_adjustment",
        source_evidence_ref=signal_event.event_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessOpened", world_id=signal_event.world_id, payload=payload
    )
    if identity is None:
        raise ValueError("relationship adjustment trigger identity missing")
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:relationship-adjustment:opened:"
        + _digest([trigger_id, signal_event.event_id]),
        world_id=signal_event.world_id,
        event_type="TriggerProcessOpened",
        logical_time=logical_time,
        created_at=signal_event.created_at,
        actor="world-v2:relationship-adjustment-trigger",
        source="world-v2:relationship-adjustment-trigger",
        trace_id=signal_event.trace_id,
        causation_id=signal_event.event_id,
        correlation_id=signal_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


__all__ = [
    "relationship_adjustment_trigger_id",
    "relationship_adjustment_trigger_open_event",
]
