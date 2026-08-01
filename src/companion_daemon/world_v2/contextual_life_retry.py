"""Durable lane/source-scoped retry authority for life model post-processing.

The same immutable 10/30/120-minute authority is shared by contextual life
deliberation and Experience-memory retention. A lane and exact source event
remain part of the retry identity, so success in one lane cannot reset another.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

from .event_identity import domain_idempotency_key
from .schemas import (
    ContextualLifeRetryProjection,
    ContextualLifeSourceDispositionRecordedPayload,
    ContextualLifeTechnicalFailureRecordedPayload,
    ProjectionCursor,
    WorldEvent,
)


CONTEXTUAL_LIFE_RETRY_DELAYS_SECONDS = (600, 1800, 7200)
ContextualLifeLane = Literal["formation", "planning", "experience_memory"]


def retry_for(
    projection,
    *,
    lane: ContextualLifeLane,
    source_event_ref: str,
) -> ContextualLifeRetryProjection | None:
    return next(
        (
            item
            for item in projection.contextual_life_retries
            if item.lane == lane and item.source_event_ref == source_event_ref
        ),
        None,
    )


def retry_is_due(
    projection,
    *,
    lane: ContextualLifeLane,
    source_event_ref: str,
) -> bool:
    retry = retry_for(
        projection,
        lane=lane,
        source_event_ref=source_event_ref,
    )
    return (
        retry is None
        or projection.logical_time is not None
        and projection.logical_time >= retry.next_retry_at
    )


def record_technical_failure(
    *,
    ledger,
    projection,
    lane: ContextualLifeLane,
    source_event_ref: str,
    source_payload_hash: str,
    context_cursor: ProjectionCursor,
    failure_code: str,
    actor: str,
    trace_id: str,
    correlation_id: str,
) -> ContextualLifeRetryProjection:
    """Append one replayable retry result at the exact deliberation cursor."""

    if projection.logical_time is None:
        raise ValueError("contextual life retry requires authoritative logical time")
    prior = retry_for(
        projection,
        lane=lane,
        source_event_ref=source_event_ref,
    )
    ordinal = 1 if prior is None else prior.retry_ordinal + 1
    delay = CONTEXTUAL_LIFE_RETRY_DELAYS_SECONDS[
        min(ordinal, len(CONTEXTUAL_LIFE_RETRY_DELAYS_SECONDS)) - 1
    ]
    material = {
        "world_id": ledger.world_id,
        "lane": lane,
        "source_event_ref": source_event_ref,
        "retry_ordinal": ordinal,
    }
    suffix = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = ContextualLifeTechnicalFailureRecordedPayload(
        failure_id="contextual-life-failure:" + suffix,
        lane=lane,
        source_event_ref=source_event_ref,
        source_payload_hash=source_payload_hash,
        evaluated_world_revision=context_cursor.world_revision,
        retry_ordinal=ordinal,
        failure_code=failure_code,
        failed_at=projection.logical_time,
        next_retry_at=projection.logical_time + timedelta(seconds=delay),
    )
    payload_json = payload.model_dump(mode="json")
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:contextual-life-failure:" + suffix,
        world_id=ledger.world_id,
        event_type="ContextualLifeTechnicalFailureRecorded",
        logical_time=projection.logical_time,
        created_at=projection.logical_time,
        actor=actor,
        source=(
            "world-v2:experience-memory"
            if lane == "experience_memory"
            else "world-v2:contextual-life-inspiration"
        ),
        trace_id=trace_id,
        causation_id=source_event_ref,
        correlation_id=correlation_id,
        idempotency_key=(
            domain_idempotency_key(
                event_type="ContextualLifeTechnicalFailureRecorded",
                world_id=ledger.world_id,
                payload=payload_json,
            )
            or "contextual-life-failure:" + suffix
        ),
        payload=payload_json,
    )
    ledger.commit_at_cursor(
        (event,),
        expected_cursor=context_cursor,
        commit_id="commit:" + event.event_id,
    )
    recorded = retry_for(
        ledger.project(),
        lane=lane,
        source_event_ref=source_event_ref,
    )
    if recorded is None or recorded.failure_event_ref != event.event_id:
        raise RuntimeError("contextual life retry projection did not advance")
    return recorded


def record_invisible_source_disposition(
    *,
    ledger,
    projection,
    source_event_ref: str,
    source_payload_hash: str,
    context_cursor: ProjectionCursor,
    actor: str,
    trace_id: str,
    correlation_id: str,
) -> None:
    if projection.logical_time is None:
        raise ValueError("contextual life disposition requires authoritative logical time")
    material = {
        "world_id": ledger.world_id,
        "source_event_ref": source_event_ref,
        "disposition": "source_not_character_visible",
    }
    suffix = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = ContextualLifeSourceDispositionRecordedPayload(
        disposition_id="contextual-life-disposition:" + suffix,
        source_event_ref=source_event_ref,
        source_payload_hash=source_payload_hash,
        evaluated_world_revision=context_cursor.world_revision,
        disposition="source_not_character_visible",
        disposed_at=projection.logical_time,
    )
    payload_json = payload.model_dump(mode="json")
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:contextual-life-disposition:" + suffix,
        world_id=ledger.world_id,
        event_type="ContextualLifeSourceDispositionRecorded",
        logical_time=projection.logical_time,
        created_at=projection.logical_time,
        actor=actor,
        source="world-v2:contextual-life-inspiration",
        trace_id=trace_id,
        causation_id=source_event_ref,
        correlation_id=correlation_id,
        idempotency_key=(
            domain_idempotency_key(
                event_type="ContextualLifeSourceDispositionRecorded",
                world_id=ledger.world_id,
                payload=payload_json,
            )
            or "contextual-life-disposition:" + suffix
        ),
        payload=payload_json,
    )
    ledger.commit_at_cursor(
        (event,),
        expected_cursor=context_cursor,
        commit_id="commit:" + event.event_id,
    )


__all__ = [
    "CONTEXTUAL_LIFE_RETRY_DELAYS_SECONDS",
    "ContextualLifeLane",
    "record_invisible_source_disposition",
    "record_technical_failure",
    "retry_for",
    "retry_is_due",
]
