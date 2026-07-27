"""Deterministic low-priority fact-review trigger for a user message.

This creates no Fact and makes no judgement about what the message means.
It only leaves a recoverable work opportunity which a source-bound model
adapter may later turn into an audited Fact-v2 proposal.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import Field

from .event_identity import domain_idempotency_key
from .fact_memory_decision import FactMemoryDecisionRecordedPayload
from .interaction_fact_decision import InteractionFactDecisionRecordedPayload
from .schema_core import FrozenModel
from .schemas import Observation, TriggerProcess, WorldEvent


INTERACTION_FACT_TRIGGER_VERSION = "interaction-fact-trigger.1"
INTERACTION_FACT_RETRY_DELAYS_SECONDS = (600, 1_800, 7_200)


class InteractionFactTechnicalFailurePayload(FrozenModel):
    """Durable retry boundary for one exact background model attempt."""

    trigger_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    phase: Literal["fact_draft", "memory_draft"]
    failure_code: str = Field(min_length=1, max_length=128)
    failed_at: datetime
    retry_ordinal: int = Field(ge=1)
    next_retry_at: datetime

    def expected_delay_seconds(self) -> int:
        return INTERACTION_FACT_RETRY_DELAYS_SECONDS[
            min(max(self.retry_ordinal, 1), len(INTERACTION_FACT_RETRY_DELAYS_SECONDS)) - 1
        ]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def interaction_fact_trigger_identity(world_id: str, observation_id: str | None) -> str:
    if not world_id or not observation_id:
        raise ValueError("interaction fact identity requires world and observation")
    return "trigger:interaction-fact:" + _digest(
        {
            "version": INTERACTION_FACT_TRIGGER_VERSION,
            "world_id": world_id,
            "observation_id": observation_id,
        }
    )


def interaction_fact_failure_event_id(*, trigger_id: str, attempt_id: str) -> str:
    if not trigger_id or not attempt_id:
        raise ValueError("interaction fact failure identity requires trigger and attempt")
    return "event:interaction-fact:technical-failure:" + _digest(
        {"trigger_id": trigger_id, "attempt_id": attempt_id}
    )


def interaction_fact_decision_event_id(
    *, trigger_id: str, fact_context_hash: str
) -> str:
    if not trigger_id or not fact_context_hash:
        raise ValueError("interaction fact decision identity requires trigger and context")
    return "event:interaction-fact:decision:" + _digest(
        {"trigger_id": trigger_id, "fact_context_hash": fact_context_hash}
    )


def interaction_fact_decision_identity(
    *, trigger_id: str, fact_context_hash: str
) -> str:
    if not trigger_id or not fact_context_hash:
        raise ValueError("interaction fact decision identity requires trigger and context")
    return "interaction-fact-decision:" + _digest(
        {"trigger_id": trigger_id, "fact_context_hash": fact_context_hash}
    )


def fact_memory_decision_event_id(
    *, trigger_id: str, fact_authority_event_ref: str
) -> str:
    if not trigger_id or not fact_authority_event_ref:
        raise ValueError("Fact-memory decision identity requires trigger and Fact authority")
    return "event:fact-memory:decision:" + _digest(
        {
            "trigger_id": trigger_id,
            "fact_authority_event_ref": fact_authority_event_ref,
        }
    )


def fact_memory_decision_identity(
    *, trigger_id: str, fact_authority_event_ref: str
) -> str:
    if not trigger_id or not fact_authority_event_ref:
        raise ValueError("Fact-memory decision identity requires trigger and Fact authority")
    return "fact-memory-decision:" + _digest(
        {
            "trigger_id": trigger_id,
            "fact_authority_event_ref": fact_authority_event_ref,
        }
    )


def interaction_fact_trigger_event(
    *, observation: Observation, observation_event: WorldEvent
) -> WorldEvent:
    """Derive the one open trigger only from its exact committed message."""

    if (
        observation_event.world_id != observation.world_id
        or observation_event.event_type != "ObservationRecorded"
        or observation_event.payload() != observation.model_dump(mode="json")
        or observation_event.logical_time != observation.logical_time
        or observation_event.created_at != observation.created_at
    ):
        raise ValueError("Fact trigger requires its exact committed Observation")
    trigger_id = interaction_fact_trigger_identity(observation.world_id, observation.observation_id)
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"fact:{observation.observation_id}",
        process_kind="interaction_fact",
        source_evidence_ref=observation.observation_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessOpened", world_id=observation.world_id, payload=payload
    )
    if identity is None:
        raise ValueError("Fact trigger has no domain identity")
    return WorldEvent.from_payload(
        schema_version=observation.schema_version,
        event_id="event:interaction-fact:opened:" + _digest(
            {"world_id": observation.world_id, "trigger_id": trigger_id}
        ),
        world_id=observation.world_id,
        event_type="TriggerProcessOpened",
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor="system:world-v2-fact-trigger",
        source="world-runtime:interaction-fact",
        trace_id=observation.trace_id,
        causation_id=observation_event.event_id,
        correlation_id=observation.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


__all__ = [
    "INTERACTION_FACT_TRIGGER_VERSION",
    "INTERACTION_FACT_RETRY_DELAYS_SECONDS",
    "FactMemoryDecisionRecordedPayload",
    "InteractionFactDecisionRecordedPayload",
    "InteractionFactTechnicalFailurePayload",
    "fact_memory_decision_event_id",
    "fact_memory_decision_identity",
    "interaction_fact_decision_event_id",
    "interaction_fact_decision_identity",
    "interaction_fact_failure_event_id",
    "interaction_fact_trigger_event",
    "interaction_fact_trigger_identity",
]
