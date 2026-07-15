"""Source-bound continuation opportunity after a *delivered* media share.

This module deliberately does not decide whether a delivered photo should
become a bid, an unfinished thread, both, or neither.  It only creates the
durable, replayable opportunity for an audited deliberation lane to make that
decision.  A preview, a provider acknowledgement, and failed/unknown delivery
are intentionally not sources for this trigger.
"""

from __future__ import annotations

import hashlib
import json

from .event_identity import domain_idempotency_key
from .media_v2 import MediaDeliverySharedPayload
from .schemas import TriggerProcess, WorldEvent


MEDIA_DELIVERY_INTERACTION_TRIGGER_VERSION = "media-delivery-interaction-trigger.1"


def media_delivery_interaction_trigger_id(*, world_id: str, delivery_id: str) -> str:
    """Stable one-shot worker identity for an immutable delivery claim."""

    material = json.dumps(
        {
            "contract": MEDIA_DELIVERY_INTERACTION_TRIGGER_VERSION,
            "world_id": world_id,
            "delivery_id": delivery_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "trigger:media-delivery-interaction:" + hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()


def media_delivery_interaction_trigger_event(*, source_event: WorldEvent) -> WorldEvent:
    """Derive the only valid interaction continuation from ``MediaDeliveryShared``.

    The caller must append this after the share in the same atomic settlement
    commit.  The reducer independently checks all bindings, so constructing a
    lookalike event cannot open a continuation from a preview or receipt.
    """

    if source_event.event_type != "MediaDeliveryShared":
        raise ValueError("media delivery interaction trigger requires MediaDeliveryShared")
    delivery = MediaDeliverySharedPayload.model_validate_json(source_event.payload_json).delivery
    trigger_id = media_delivery_interaction_trigger_id(
        world_id=source_event.world_id, delivery_id=delivery.delivery_id
    )
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"media-delivery:{delivery.delivery_id}",
        process_kind="media_delivery_interaction",
        source_evidence_ref=source_event.event_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessOpened", world_id=source_event.world_id, payload=payload
    )
    if identity is None:
        raise ValueError("media delivery interaction trigger identity is unavailable")
    return WorldEvent.from_payload(
        schema_version=source_event.schema_version,
        event_id="event:media-delivery-interaction-trigger-opened:"
        + trigger_id.removeprefix("trigger:"),
        event_type="TriggerProcessOpened",
        world_id=source_event.world_id,
        logical_time=source_event.logical_time,
        created_at=source_event.created_at,
        actor="system:media-delivery-interaction-trigger",
        source="world-runtime",
        trace_id=source_event.trace_id,
        causation_id=source_event.event_id,
        correlation_id=source_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


__all__ = [
    "MEDIA_DELIVERY_INTERACTION_TRIGGER_VERSION",
    "media_delivery_interaction_trigger_event",
    "media_delivery_interaction_trigger_id",
]
