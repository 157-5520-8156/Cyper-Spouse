"""Clock-bound maintenance for source-bound media candidates.

This module owns only terminal expiry.  It neither scores nor selects a
candidate, and it cannot create an opportunity or a provider Action.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from .event_identity import domain_idempotency_key
from .media_v2 import PhotoCandidateExpiredPayload
from .schema_core import FrozenModel
from .schemas import ProjectionCursor, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MediaCandidateMaintenanceResult(FrozenModel):
    status: Literal["expired", "idle", "blocked"]
    event_refs: tuple[str, ...] = ()
    reason_code: str | None = None


class MediaCandidateMaintenanceRuntime:
    """Expire only currently available P1 candidates at an exact clock tick."""

    def __init__(self, *, ledger, source: str = "world-v2:media-candidate-maintenance") -> None:  # type: ignore[no-untyped-def]
        self._ledger, self._source = ledger, source

    def expire_once(
        self, *, logical_time: datetime, actor: str, trace_id: str, correlation_id: str,
    ) -> MediaCandidateMaintenanceResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            return MediaCandidateMaintenanceResult(
                status="blocked", reason_code="media_candidate.logical_time_not_current"
            )
        candidates = tuple(
            sorted(
                (
                    item
                    for item in projection.photo_candidates
                    if item.status == "available"
                    and item.expires_at is not None
                    and item.expires_at <= logical_time
                    and item.opened_event_ref is not None
                    and item.opened_event_payload_hash is not None
                ),
                key=lambda item: item.candidate_id,
            )
        )
        if not candidates:
            return MediaCandidateMaintenanceResult(status="idle")
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        events: list[WorldEvent] = []
        for candidate in candidates:
            payload = PhotoCandidateExpiredPayload(
                candidate_id=candidate.candidate_id,
                expected_entity_revision=candidate.entity_revision,
            ).model_dump(mode="json")
            event_id = "event:media-candidate:expired:" + _digest(
                {"candidate_id": candidate.candidate_id, "revision": candidate.entity_revision}
            )
            events.append(
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id=event_id,
                    event_type="PhotoCandidateExpired",
                    world_id=self._ledger.world_id,
                    logical_time=logical_time,
                    created_at=logical_time,
                    actor=actor,
                    source=self._source,
                    trace_id=trace_id,
                    causation_id=(events[-1].event_id if events else candidate.opened_event_ref),
                    correlation_id=correlation_id,
                    idempotency_key=domain_idempotency_key(
                        event_type="PhotoCandidateExpired", world_id=self._ledger.world_id, payload=payload
                    ) or "media-candidate-expired:" + _digest(payload),
                    payload=payload,
                )
            )
        self._ledger.commit_at_cursor(
            tuple(events),
            expected_cursor=cursor,
            commit_id="commit:media-candidate-expiry:" + _digest(
                {"cursor": cursor.model_dump(mode="json"), "events": [event.event_id for event in events]}
            ),
        )
        return MediaCandidateMaintenanceResult(
            status="expired", event_refs=tuple(event.event_id for event in events)
        )


__all__ = ["MediaCandidateMaintenanceResult", "MediaCandidateMaintenanceRuntime"]
