"""Ledger-only recording seam for short-lived visible physical state."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .event_identity import domain_idempotency_key
from .schema_core import PrivacyClass
from .schemas import CommitResult, ProjectionCursor, WorldEvent
from .visible_physical_state import (
    MAX_VISIBLE_PHYSICAL_STATE_LIFETIME,
    VisiblePhysicalStateProjection,
    VisiblePhysicalStateRecordCommand,
    VisiblePhysicalStateRecordedPayload,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class VisiblePhysicalStateRuntime:
    """Resolve source hash/type/visibility from the ledger before appending."""

    def __init__(
        self,
        *,
        ledger: Any,
        source: str = "world-v2:visible-physical-state",
    ) -> None:
        self._ledger, self._source = ledger, source

    def record(
        self,
        command: VisiblePhysicalStateRecordCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            raise ValueError("visible physical state must use the current logical clock")
        source_ref = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == command.source_event_ref
            ),
            None,
        )
        if source_ref is None:
            raise ValueError("visible physical state source is unavailable")
        source = self._ledger.lookup_event_commit(command.source_event_ref)
        if source is None:
            raise ValueError("visible physical state source event is unavailable")
        source_event, _commit = source
        if (
            source_event.event_type != source_ref.event_type
            or source_event.payload_hash != source_ref.payload_hash
        ):
            raise ValueError("visible physical state source bytes are unavailable")
        visibility = self._source_visibility(projection, source_event.event_id)
        valid_until = command.valid_until or logical_time + MAX_VISIBLE_PHYSICAL_STATE_LIFETIME
        if valid_until > logical_time + MAX_VISIBLE_PHYSICAL_STATE_LIFETIME:
            raise ValueError("visible physical state exceeds maximum lifetime")
        revisions = tuple(
            state.entity_revision
            for state in projection.visible_physical_states
            if state.subject_ref == command.subject_ref
        )
        state = VisiblePhysicalStateProjection(
            physical_state_id="visible-physical:" + command.subject_ref,
            subject_ref=command.subject_ref,
            entity_revision=max(revisions, default=0) + 1,
            source_event_ref=source_event.event_id,
            source_event_payload_hash=source_event.payload_hash,
            source_event_type=source_event.event_type,
            valid_from=logical_time,
            valid_until=valid_until,
            visibility=visibility,
            positive_cues=command.positive_cues,
            negative_cues=command.negative_cues,
        )
        payload = VisiblePhysicalStateRecordedPayload(state=state).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:visible-physical-state:"
            + _digest(
                {
                    "world_id": self._ledger.world_id,
                    "command_id": command.command_id,
                    "payload": payload,
                }
            ),
            event_type="VisiblePhysicalStateRecorded",
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=created_at,
            actor=actor,
            source=self._source,
            trace_id=trace_id,
            causation_id=source_event.event_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="VisiblePhysicalStateRecorded",
                world_id=self._ledger.world_id,
                payload=payload,
            )
            or "visible-physical-state:" + _digest(payload),
            payload=payload,
        )
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        return self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=cursor,
            commit_id="commit:visible-physical-state:"
            + _digest({"cursor": cursor.model_dump(mode="json"), "event_id": event.event_id}),
        )

    @staticmethod
    def _source_visibility(projection: Any, source_event_ref: str) -> PrivacyClass:
        for plan in projection.plans:
            origin = getattr(plan, "authority_origin", None)
            if getattr(origin, "accepted_event_ref", None) == source_event_ref:
                return getattr(plan, "privacy_class")
        for occurrence in projection.world_occurrences:
            if getattr(occurrence, "settlement_event_ref", None) == source_event_ref:
                return getattr(occurrence, "visibility")
        for experience in projection.experiences:
            origin, values = getattr(experience, "origin", None), getattr(experience, "values", None)
            if getattr(origin, "accepted_event_ref", None) == source_event_ref and values is not None:
                return getattr(values, "privacy_class")
        for fact in projection.facts:
            origin, values = getattr(fact, "origin", None), getattr(fact, "values", None)
            if getattr(origin, "accepted_event_ref", None) == source_event_ref and values is not None:
                return getattr(values, "privacy_class")
        raise ValueError("visible physical state source has no visibility authority")


__all__ = ["VisiblePhysicalStateRuntime"]
