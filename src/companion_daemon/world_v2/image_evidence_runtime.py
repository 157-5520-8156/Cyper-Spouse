"""Controlled authoring seam for source-bound visual declarations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import Field

from .event_identity import domain_idempotency_key
from .image_evidence_contract import ImageEvidenceDeclaredPayload, ImageEvidenceV1
from .schema_core import FrozenModel, PrivacyClass
from .schemas import CommitResult, ProjectionCursor, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ImageEvidenceDeclarationCommand(FrozenModel):
    """A request to describe visual evidence of one already committed event."""

    command_id: str = Field(min_length=1, max_length=256)
    source_event_ref: str = Field(min_length=1, max_length=512)
    image_evidence: ImageEvidenceV1


class ImageEvidenceDeclarationRuntime:
    """Derive declaration coordinates from projection; never trust caller ones."""

    def __init__(self, *, ledger, source: str = "world-v2:image-evidence") -> None:  # type: ignore[no-untyped-def]
        self._ledger, self._source = ledger, source

    def declare(
        self,
        command: ImageEvidenceDeclarationCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            raise ValueError("image evidence declaration must use the current logical clock")
        source_ref = next(
            (item for item in projection.committed_world_event_refs if item.event_id == command.source_event_ref),
            None,
        )
        if source_ref is None:
            raise ValueError("image evidence declaration source is unavailable")
        source = self._ledger.lookup_event_commit(command.source_event_ref)
        if source is None:
            raise ValueError("image evidence declaration source event is unavailable")
        source_event, _source_commit = source
        if (
            source_event.event_type != source_ref.event_type
            or source_event.payload_hash != source_ref.payload_hash
        ):
            raise ValueError("image evidence declaration source bytes are unavailable")
        privacy = self._source_privacy(projection=projection, source_event_ref=source_event.event_id)
        payload = ImageEvidenceDeclaredPayload(
            source_event_ref=source_event.event_id,
            source_event_payload_hash=source_event.payload_hash,
            source_event_type=source_event.event_type,
            source_privacy_ceiling=privacy,
            image_evidence=command.image_evidence,
            declared_at=logical_time,
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:image-evidence-declared:" + _digest(
                {"world_id": self._ledger.world_id, "command_id": command.command_id, "payload": payload}
            ),
            event_type="ImageEvidenceDeclared",
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=created_at,
            actor=actor,
            source=self._source,
            trace_id=trace_id,
            causation_id=source_event.event_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ImageEvidenceDeclared", world_id=self._ledger.world_id, payload=payload
            ) or "image-evidence:" + _digest(payload),
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
            commit_id="commit:image-evidence-declared:" + _digest(
                {"cursor": cursor.model_dump(mode="json"), "event_id": event.event_id}
            ),
        )

    @staticmethod
    def _source_privacy(*, projection, source_event_ref: str) -> PrivacyClass:  # type: ignore[no-untyped-def]
        for plan in projection.plans:
            origin = getattr(plan, "authority_origin", None)
            if getattr(origin, "accepted_event_ref", None) == source_event_ref:
                return getattr(plan, "privacy_class")
        for occurrence in projection.world_occurrences:
            if getattr(occurrence, "settlement_event_ref", None) == source_event_ref:
                return getattr(occurrence, "visibility")
        for experience in projection.experiences:
            origin = getattr(experience, "origin", None)
            values = getattr(experience, "values", None)
            if getattr(origin, "accepted_event_ref", None) == source_event_ref and values is not None:
                return getattr(values, "privacy_class")
        for fact in projection.facts:
            origin = getattr(fact, "origin", None)
            values = getattr(fact, "values", None)
            if getattr(origin, "accepted_event_ref", None) == source_event_ref and values is not None:
                return getattr(values, "privacy_class")
        raise ValueError("image evidence declaration source has no public lifecycle authority")


__all__ = ["ImageEvidenceDeclarationCommand", "ImageEvidenceDeclarationRuntime"]
