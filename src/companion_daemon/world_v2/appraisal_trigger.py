"""Deterministic ingress lifecycle for source-bound interaction appraisal.

This module does not interpret a message and does not mutate a ledger.  It
only derives the open/claim events for the one appraisal opportunity owned by
an already committed incoming Observation.  Runtime remains the only caller
that commits those events under its per-world orchestration lock.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json

from .batch_invariants import interaction_appraisal_trigger_identity
from .event_identity import domain_idempotency_key
from .schemas import ClaimLease, Observation, TriggerProcess, WorldEvent


INTERACTION_APPRAISAL_TRIGGER_VERSION = "interaction-appraisal-trigger.1"
DEFAULT_INTERACTION_APPRAISAL_LEASE_SECONDS = 120


class InteractionAppraisalTriggerError(ValueError):
    """Stable invalid-authority failure for interaction trigger derivation."""


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _event_id(*, role: str, world_id: str, trigger_id: str, attempt_id: str) -> str:
    return f"event:interaction-appraisal:{role}:{_digest((world_id, trigger_id, attempt_id, role))}"


def _attempt_id(*, world_id: str, trigger_id: str, observation_event: WorldEvent) -> str:
    return "attempt:interaction-appraisal:" + _digest(
        {
            "version": INTERACTION_APPRAISAL_TRIGGER_VERSION,
            "world_id": world_id,
            "trigger_id": trigger_id,
            "observation_event_id": observation_event.event_id,
            "observation_payload_hash": observation_event.payload_hash,
        }
    )


def interaction_appraisal_trigger_events(
    *,
    observation: Observation,
    observation_event: WorldEvent,
    owner_id: str,
    lease_seconds: int = DEFAULT_INTERACTION_APPRAISAL_LEASE_SECONDS,
) -> tuple[WorldEvent, WorldEvent]:
    """Derive the exact open then claim events for one observed user message.

    The pair intentionally has no model output and no domain mutation.  A
    caller may safely retry it because both event identities and idempotency
    keys bind the committed Observation envelope.
    """

    if not owner_id:
        raise InteractionAppraisalTriggerError("interaction appraisal owner is required")
    if lease_seconds <= 0:
        raise InteractionAppraisalTriggerError("interaction appraisal lease must be positive")
    if (
        observation_event.world_id != observation.world_id
        or observation_event.event_type != "ObservationRecorded"
        or observation_event.payload() != observation.model_dump(mode="json")
        or observation_event.logical_time != observation.logical_time
        or observation_event.created_at != observation.created_at
    ):
        raise InteractionAppraisalTriggerError("observation does not match committed authority")

    trigger_id = interaction_appraisal_trigger_identity(
        observation.world_id, observation.observation_id
    )
    attempt_id = _attempt_id(
        world_id=observation.world_id, trigger_id=trigger_id, observation_event=observation_event
    )
    opened = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"interaction:{observation.observation_id}",
        process_kind="interaction_appraisal",
        source_evidence_ref=observation.observation_id,
        state="open",
    )
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id=owner_id,
                attempt_id=attempt_id,
                acquired_at=observation.logical_time,
                expires_at=observation.logical_time + timedelta(seconds=lease_seconds),
            ),
            "attempt_ids": (attempt_id,),
        }
    )
    common = {
        "schema_version": observation.schema_version,
        "world_id": observation.world_id,
        "logical_time": observation.logical_time,
        "created_at": observation.created_at,
        "actor": owner_id,
        "source": "world-runtime:interaction-appraisal",
        "trace_id": observation.trace_id,
        "correlation_id": observation.correlation_id,
    }
    payloads = ({"process": opened.model_dump(mode="json")}, {"process": claimed.model_dump(mode="json")})
    types = ("TriggerProcessOpened", "TriggerProcessClaimed")
    event_ids = (
        _event_id(role="opened", world_id=observation.world_id, trigger_id=trigger_id, attempt_id=attempt_id),
        _event_id(role="claimed", world_id=observation.world_id, trigger_id=trigger_id, attempt_id=attempt_id),
    )
    events: list[WorldEvent] = []
    for event_type, event_id, payload in zip(types, event_ids, payloads, strict=True):
        identity = domain_idempotency_key(
            event_type=event_type, world_id=observation.world_id, payload=payload
        )
        if identity is None:
            raise InteractionAppraisalTriggerError("trigger event has no domain identity")
        events.append(
            WorldEvent.from_payload(
                **common,
                event_id=event_id,
                event_type=event_type,
                causation_id=observation_event.event_id if not events else events[-1].event_id,
                idempotency_key=identity,
                payload=payload,
            )
        )
    return events[0], events[1]


__all__ = [
    "DEFAULT_INTERACTION_APPRAISAL_LEASE_SECONDS",
    "INTERACTION_APPRAISAL_TRIGGER_VERSION",
    "InteractionAppraisalTriggerError",
    "interaction_appraisal_trigger_events",
]
