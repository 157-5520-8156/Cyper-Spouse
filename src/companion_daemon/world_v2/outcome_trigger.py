"""Deterministic, recovery-safe trigger opening for observed world outcomes."""

from __future__ import annotations

import hashlib
import json

from .event_identity import domain_idempotency_key
from .schemas import OutcomeObservationProjection, TriggerProcess, WorldEvent


def outcome_deliberation_trigger_id(
    *, world_id: str, occurrence_id: str, observation_id: str
) -> str:
    material = json.dumps(
        {
            "contract": "outcome-deliberation-trigger.1",
            "world_id": world_id,
            "occurrence_id": occurrence_id,
            "observation_id": observation_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "trigger:outcome-deliberation:" + hashlib.sha256(material.encode()).hexdigest()


def outcome_deliberation_trigger_event(
    *, world_id: str, source_event: WorldEvent, observation: OutcomeObservationProjection
) -> WorldEvent:
    """Open the one worker opportunity for an immutable outcome observation."""

    if source_event.event_type != "OutcomeObservationRecorded":
        raise ValueError("outcome trigger requires an outcome observation event")
    expected_event_id = f"event:outcome-observation:{observation.observation_id}"
    if source_event.event_id != expected_event_id:
        raise ValueError("outcome trigger source event does not bind the observation")
    trigger_id = outcome_deliberation_trigger_id(
        world_id=world_id,
        occurrence_id=observation.occurrence_id,
        observation_id=observation.observation_id,
    )
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"outcome:{observation.occurrence_id}:{observation.observation_id}",
        process_kind="outcome_deliberation",
        source_evidence_ref=source_event.event_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessOpened", world_id=world_id, payload=payload
    )
    if identity is None:
        raise ValueError("outcome trigger event identity is unavailable")
    return WorldEvent.from_payload(
        schema_version=source_event.schema_version,
        event_id="event:outcome-deliberation-trigger-opened:" + trigger_id.removeprefix("trigger:"),
        world_id=world_id,
        event_type="TriggerProcessOpened",
        logical_time=source_event.logical_time,
        created_at=source_event.created_at,
        actor="system:outcome-deliberation-trigger",
        source="world-runtime",
        trace_id=source_event.trace_id,
        causation_id=source_event.event_id,
        correlation_id=source_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


__all__ = ["outcome_deliberation_trigger_event", "outcome_deliberation_trigger_id"]
