"""Durable opportunity to deliberate whether one media item warrants analysis."""

from __future__ import annotations
import hashlib
import json
from .event_identity import domain_idempotency_key
from .schemas import Observation, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def perception_trigger_event(
    *, observation: Observation, observation_event: WorldEvent
) -> WorldEvent:
    if (
        observation_event.event_type != "ObservationRecorded"
        or observation_event.payload() != observation.model_dump(mode="json")
    ):
        raise ValueError("perception trigger requires exact committed Observation")
    process = TriggerProcess(
        trigger_id="trigger:perception:"
        + _digest({"world": observation.world_id, "observation": observation.observation_id}),
        trigger_ref=f"perception:{observation.observation_id}",
        process_kind="perception_deliberation",
        source_evidence_ref=observation.observation_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    return WorldEvent.from_payload(
        schema_version=observation.schema_version,
        event_id="event:perception-trigger:"
        + _digest({"world": observation.world_id, "trigger": process.trigger_id}),
        world_id=observation.world_id,
        event_type="TriggerProcessOpened",
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor="system:world-v2-perception",
        source="world-runtime:perception",
        trace_id=observation.trace_id,
        causation_id=observation_event.event_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="TriggerProcessOpened", world_id=observation.world_id, payload=payload
        )
        or process.trigger_id,
        payload=payload,
    )


__all__ = ["perception_trigger_event"]
