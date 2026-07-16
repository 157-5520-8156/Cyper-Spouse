"""Controlled recording seam for sparse visible appearance state."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .appearance_state import (
    AppearanceStateProjection,
    AppearanceStateRecordCommand,
    AppearanceStateRecordedPayload,
    privacy_is_no_broader_than,
)
from .event_identity import domain_idempotency_key
from .schema_core import PrivacyClass
from .schemas import CommitResult, ProjectionCursor, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class AppearanceStateRuntime:
    """Resolve all source coordinates from committed authority before writing."""

    def __init__(self, *, ledger: Any, source: str = "world-v2:appearance-state") -> None:
        self._ledger, self._source = ledger, source

    def record(
        self,
        command: AppearanceStateRecordCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            raise ValueError("appearance state must use the current logical clock")
        source_ref = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == command.source_event_ref
            ),
            None,
        )
        if source_ref is None:
            raise ValueError("appearance state source is unavailable")
        source = self._ledger.lookup_event_commit(command.source_event_ref)
        if source is None:
            raise ValueError("appearance state source event is unavailable")
        source_event, _commit = source
        if (
            source_event.event_type != source_ref.event_type
            or source_event.payload_hash != source_ref.payload_hash
        ):
            raise ValueError("appearance state source bytes are unavailable")
        source_visibility = self._source_visibility(projection, source_event.event_id)
        if not privacy_is_no_broader_than(
            visibility=command.visibility, source_visibility=source_visibility
        ):
            raise ValueError("appearance visibility exceeds its source")
        revisions = tuple(
            state.entity_revision
            for state in projection.appearance_states
            if state.subject_ref == command.subject_ref
        )
        state = AppearanceStateProjection(
            appearance_state_id="appearance:" + command.subject_ref,
            subject_ref=command.subject_ref,
            entity_revision=max(revisions, default=0) + 1,
            source_event_ref=source_event.event_id,
            source_event_payload_hash=source_event.payload_hash,
            source_event_type=source_event.event_type,
            valid_from=logical_time,
            valid_until=command.valid_until,
            visibility=command.visibility,
            visible_attributes=command.visible_attributes,
        )
        payload = AppearanceStateRecordedPayload(state=state).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:appearance-state:"
            + _digest(
                {
                    "world_id": self._ledger.world_id,
                    "command_id": command.command_id,
                    "payload": payload,
                }
            ),
            event_type="AppearanceStateRecorded",
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=created_at,
            actor=actor,
            source=self._source,
            trace_id=trace_id,
            causation_id=source_event.event_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="AppearanceStateRecorded",
                world_id=self._ledger.world_id,
                payload=payload,
            )
            or "appearance-state:" + _digest(payload),
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
            commit_id="commit:appearance-state:"
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
            origin, values = (
                getattr(experience, "origin", None),
                getattr(experience, "values", None),
            )
            if (
                getattr(origin, "accepted_event_ref", None) == source_event_ref
                and values is not None
            ):
                return getattr(values, "privacy_class")
        for fact in projection.facts:
            origin, values = getattr(fact, "origin", None), getattr(fact, "values", None)
            if (
                getattr(origin, "accepted_event_ref", None) == source_event_ref
                and values is not None
            ):
                return getattr(values, "privacy_class")
        raise ValueError("appearance state source has no visibility authority")


__all__ = ["AppearanceStateRuntime"]
