"""Durable, source-bound background opportunity for a read-only tool decision."""

from __future__ import annotations

import hashlib
import json

from .event_identity import domain_idempotency_key
from .schemas import Observation, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_only_tool_trigger_identity(*, world_id: str, observation_id: str) -> str:
    return "trigger:read-only-tool:" + _digest(
        {"contract": "read-only-tool-trigger.1", "world_id": world_id, "observation_id": observation_id}
    )


def read_only_tool_trigger_event(*, observation: Observation, observation_event: WorldEvent) -> WorldEvent:
    if (
        observation_event.world_id != observation.world_id
        or observation_event.event_type != "ObservationRecorded"
        or observation_event.payload() != observation.model_dump(mode="json")
    ):
        raise ValueError("read-only tool trigger requires exact committed Observation")
    trigger_id = read_only_tool_trigger_identity(
        world_id=observation.world_id, observation_id=observation.observation_id
    )
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"read-only-tool:{observation.observation_id}",
        process_kind="read_only_tool_deliberation",
        source_evidence_ref=observation.observation_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessOpened", world_id=observation.world_id, payload=payload
    )
    if identity is None:
        raise ValueError("read-only tool trigger identity is unavailable")
    return WorldEvent.from_payload(
        schema_version=observation.schema_version,
        event_id="event:read-only-tool-trigger:opened:" + _digest(
            {"world_id": observation.world_id, "trigger_id": trigger_id}
        ),
        world_id=observation.world_id,
        event_type="TriggerProcessOpened",
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor="system:world-v2-read-only-tool-trigger",
        source="world-runtime:read-only-tool",
        trace_id=observation.trace_id,
        causation_id=observation_event.event_id,
        correlation_id=observation.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


__all__ = ["read_only_tool_trigger_event", "read_only_tool_trigger_identity"]
