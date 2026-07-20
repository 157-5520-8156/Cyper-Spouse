"""A narrow, source-bound authority for photographable objects and food.

``FactCommittedV2`` intentionally stores an opaque value ref/hash: it is a
good long-term fact contract, but it is not permission for a media planner to
turn a value ref into a picture.  This module is the separate bridge for the
small set of *already observed* object/food facts that have an exact visual
description.  The description lives in an immutable sidecar and the ledger
record binds both its bytes and the lived-world event that established it.

It is not an LLM prompt lane.  Callers must provide a concrete visual object
whose fields are checked again by ``MediaEvidenceSnapshotCompiler``; no
consumer is permitted to reconstruct content from a fact value, activity name
or user message.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .event_identity import domain_idempotency_key
from .media_v2 import (
    ImmutableMediaPayloadStore,
    StoredMediaPayload,
    canonical_media_json,
    media_digest,
    media_payload_hash,
)
from .schema_core import FrozenModel, PrivacyClass
from .schemas import CommitResult, ProjectionCursor, WorldEvent


VisualFactFacet = Literal[
    "activity.visible_object",
    "meal.visible_food",
    "meal.visible_drink",
]

VISUAL_FACT_SOURCE_EVENT_TYPES = frozenset({
    "ActivityStarted", "ActivityResumed", "ActivityCompleted",
    "WorldOccurrenceSettled", "ExperienceCommitted", "FactCommitted",
    "FactCorrected", "FactCommitMaterializedV2",
})


class VisualObjectEvidenceV1(FrozenModel):
    """One explicit planner-readable object, never a generated description."""

    id: str = Field(min_length=1, max_length=512)
    kind: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=480)
    ownership: str | None = Field(default=None, max_length=128)
    visibility: Literal["public", "shareable"]


class VisualFactContentV1(FrozenModel):
    """Exact sidecar body for a single source-bound visual facet."""

    schema_version: Literal["world-visual-fact-content-v1"] = "world-visual-fact-content-v1"
    facet: VisualFactFacet
    subject_ref: str = Field(min_length=1, max_length=512)
    visibility: Literal["public", "shareable"]
    objects: tuple[VisualObjectEvidenceV1, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def object_visibility_does_not_exceed_content(self) -> "VisualFactContentV1":
        if self.visibility == "public" and any(item.visibility != "public" for item in self.objects):
            raise ValueError("visual object visibility exceeds visual-fact visibility")
        if len({item.id for item in self.objects}) != len(self.objects):
            raise ValueError("visual fact object ids must be unique")
        return self

    def canonical_body(self) -> str:
        return canonical_media_json(self.model_dump(mode="json"))

    def as_image_evidence(self) -> dict[str, object]:
        """Return the only image snapshot slice this authority can expose."""

        return {
            "visibility": self.visibility,
            "objects": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "description": item.description,
                    **({"ownership": item.ownership} if item.ownership is not None else {}),
                    "visibility": item.visibility,
                    "evidence_visibility": item.visibility,
                }
                for item in self.objects
            ],
        }


class VisualFactRecordedPayload(FrozenModel):
    """Ledger descriptor binding one immutable object/food sidecar to reality."""

    visual_fact_id: str = Field(min_length=1, max_length=256)
    facet: VisualFactFacet
    subject_ref: str = Field(min_length=1, max_length=512)
    visibility: Literal["public", "shareable"]
    content_ref: str = Field(min_length=1, max_length=512)
    content_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_event_ref: str = Field(min_length=1, max_length=512)
    source_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_type: str = Field(min_length=1, max_length=128)
    source_privacy_ceiling: PrivacyClass
    observed_at: datetime
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def source_and_expiry_are_closed(self) -> "VisualFactRecordedPayload":
        if self.source_event_type not in VISUAL_FACT_SOURCE_EVENT_TYPES:
            raise ValueError("visual fact source event type is unsupported")
        if self.source_privacy_ceiling not in {"public", "shareable"}:
            raise ValueError("visual fact source must be public or shareable")
        if self.visibility == "shareable" and self.source_privacy_ceiling != "shareable":
            raise ValueError("visual fact visibility exceeds its source privacy")
        if self.valid_until is not None and self.valid_until <= self.observed_at:
            raise ValueError("visual fact expiry must follow observation")
        return self


class VisualFactRecordCommand(FrozenModel):
    """Host-facing input with no caller-controlled source hash or privacy."""

    command_id: str = Field(min_length=1, max_length=256)
    source_event_ref: str = Field(min_length=1, max_length=512)
    content_ref: str = Field(min_length=1, max_length=512)
    content: VisualFactContentV1
    valid_until: datetime | None = None


class VisualFactRuntime:
    """Install sidecar bytes then append one replay-stable descriptor event."""

    def __init__(self, *, ledger, sidecar: ImmutableMediaPayloadStore) -> None:  # type: ignore[no-untyped-def]
        self._ledger = ledger
        self._sidecar = sidecar

    def record(
        self,
        command: VisualFactRecordCommand,
        *,
        logical_time: datetime,
        created_at: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            raise ValueError("visual fact record must use the current logical clock")
        source_ref = next(
            (item for item in projection.committed_world_event_refs if item.event_id == command.source_event_ref),
            None,
        )
        if source_ref is None:
            raise ValueError("visual fact source is unavailable")
        located = self._ledger.lookup_event_commit(command.source_event_ref)
        if located is None:
            raise ValueError("visual fact source event is unavailable")
        source_event, _commit = located
        if (
            source_event.event_type != source_ref.event_type
            or source_event.payload_hash != source_ref.payload_hash
            or source_event.event_type not in VISUAL_FACT_SOURCE_EVENT_TYPES
        ):
            raise ValueError("visual fact source bytes are unavailable")
        privacy = self._source_privacy(projection=projection, source_event_ref=source_event.event_id)
        body = command.content.canonical_body()
        content_hash = media_payload_hash(body)
        self._sidecar.put_if_absent(StoredMediaPayload(
            payload_ref=command.content_ref,
            payload_hash=content_hash,
            content_type="application/vnd.world-v2.visual-fact+json",
            body=body,
        ))
        payload = VisualFactRecordedPayload(
            visual_fact_id="visual-fact:" + media_digest({
                "world_id": self._ledger.world_id,
                "command_id": command.command_id,
                "source_event_ref": source_event.event_id,
                "content_ref": command.content_ref,
                "content_payload_hash": content_hash,
            }),
            facet=command.content.facet,
            subject_ref=command.content.subject_ref,
            visibility=command.content.visibility,
            content_ref=command.content_ref,
            content_payload_hash=content_hash,
            source_event_ref=source_event.event_id,
            source_event_payload_hash=source_event.payload_hash,
            source_event_type=source_event.event_type,
            source_privacy_ceiling=privacy,
            observed_at=logical_time,
            valid_until=command.valid_until,
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:visual-fact-recorded:" + media_digest({
                "world_id": self._ledger.world_id, "payload": payload,
            }),
            event_type="VisualFactRecorded", world_id=self._ledger.world_id,
            logical_time=logical_time, created_at=created_at, actor=actor,
            source="world-v2:visual-fact", trace_id=trace_id,
            causation_id=source_event.event_id, correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="VisualFactRecorded", world_id=self._ledger.world_id, payload=payload,
            ) or "visual-fact:" + command.command_id,
            payload=payload,
        )
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        return self._ledger.commit_at_cursor(
            (event,), expected_cursor=cursor,
            commit_id="commit:visual-fact:" + media_digest({"cursor": cursor.model_dump(mode="json"), "event_id": event.event_id}),
        )

    @staticmethod
    def _source_privacy(*, projection, source_event_ref: str) -> PrivacyClass:  # type: ignore[no-untyped-def]
        # Keep exactly the same public lifecycle authority boundary as image
        # declarations; a fact or activity name alone cannot weaken privacy.
        from .image_evidence_runtime import ImageEvidenceDeclarationRuntime

        return ImageEvidenceDeclarationRuntime._source_privacy(
            projection=projection, source_event_ref=source_event_ref,
        )


VISUAL_FACT_PAYLOAD_MODELS = {"VisualFactRecorded": VisualFactRecordedPayload}


__all__ = [
    "VISUAL_FACT_PAYLOAD_MODELS", "VISUAL_FACT_SOURCE_EVENT_TYPES", "VisualFactContentV1",
    "VisualFactFacet", "VisualFactRecordCommand", "VisualFactRecordedPayload",
    "VisualFactRuntime", "VisualObjectEvidenceV1",
]
