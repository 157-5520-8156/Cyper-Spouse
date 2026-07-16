"""Controlled write seam for recipient-scoped P3 visual declarations.

This intentionally mirrors the public declaration runtime without sharing its
wire: a caller provides only an immutable source reference, recipient, and a
typed visual slice.  The ledger resolves the exact source bytes and privacy
ceiling, so a host cannot relabel a public event as private (or vice versa).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import Field

from .event_identity import domain_idempotency_key
from .image_evidence_runtime import ImageEvidenceDeclarationRuntime
from .private_image_evidence_contract import (
    RecipientScopedImageEvidenceDeclaredPayload,
    RecipientScopedImageEvidenceV1,
)
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RecipientScopedImageEvidenceDeclarationCommand(FrozenModel):
    """Request one recipient-bound declaration from an already committed source."""

    command_id: str = Field(min_length=1, max_length=256)
    source_event_ref: str = Field(min_length=1, max_length=512)
    recipient_ref: str = Field(min_length=1, max_length=512)
    image_evidence: RecipientScopedImageEvidenceV1


class RecipientScopedImageEvidenceDeclarationRuntime:
    """Resolve all source coordinates before committing the P3 declaration."""

    def __init__(self, *, ledger, source: str = "world-v2:recipient-scoped-image-evidence") -> None:  # type: ignore[no-untyped-def]
        self._ledger, self._source = ledger, source

    def declare(
        self,
        command: RecipientScopedImageEvidenceDeclarationCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            raise ValueError("recipient-scoped image evidence declaration must use the current logical clock")
        source_ref = next(
            (item for item in projection.committed_world_event_refs if item.event_id == command.source_event_ref),
            None,
        )
        if source_ref is None:
            raise ValueError("recipient-scoped image evidence declaration source is unavailable")
        located = self._ledger.lookup_event_commit(command.source_event_ref)
        if located is None:
            raise ValueError("recipient-scoped image evidence declaration source event is unavailable")
        source_event, _source_commit = located
        if (
            source_event.event_type != source_ref.event_type
            or source_event.payload_hash != source_ref.payload_hash
        ):
            raise ValueError("recipient-scoped image evidence declaration source bytes are unavailable")
        privacy = ImageEvidenceDeclarationRuntime._source_privacy(
            projection=projection, source_event_ref=source_event.event_id
        )
        if privacy not in {"personal", "private"}:
            raise ValueError("recipient-scoped image evidence declaration source must be personal or private")
        payload = RecipientScopedImageEvidenceDeclaredPayload(
            source_event_ref=source_event.event_id,
            source_event_payload_hash=source_event.payload_hash,
            source_event_type=source_event.event_type,
            source_privacy_ceiling=privacy,
            recipient_ref=command.recipient_ref,
            image_evidence=command.image_evidence,
            declared_at=logical_time,
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:recipient-scoped-image-evidence-declared:" + _digest(
                {"world_id": self._ledger.world_id, "command_id": command.command_id, "payload": payload}
            ),
            event_type="RecipientScopedImageEvidenceDeclared",
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=created_at,
            actor=actor,
            source=self._source,
            trace_id=trace_id,
            causation_id=source_event.event_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="RecipientScopedImageEvidenceDeclared", world_id=self._ledger.world_id, payload=payload
            ) or "recipient-scoped-image-evidence:" + _digest(payload),
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
            commit_id="commit:recipient-scoped-image-evidence-declared:" + _digest(
                {"cursor": cursor.model_dump(mode="json"), "event_id": event.event_id}
            ),
        )


__all__ = [
    "RecipientScopedImageEvidenceDeclarationCommand",
    "RecipientScopedImageEvidenceDeclarationRuntime",
]
