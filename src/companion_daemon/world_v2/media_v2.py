"""The first source-bound World v2 media vertical.

The old image machine remains a private planner/renderer.  This module owns
only the durable contract around it: an opportunity freezes committed evidence,
one planning Action is budgeted with a deterministic idempotency key, and its
single terminal planner result becomes either a frozen plan or a durable
``not_renderable`` outcome.  No free prompt or post-hoc event may enter here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import sqlite3
from threading import RLock
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .schema_core import FrozenModel, PrivacyClass


def canonical_media_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def media_digest(value: object) -> str:
    return hashlib.sha256(canonical_media_json(value).encode("utf-8")).hexdigest()


def media_payload_hash(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


MediaFamily = Literal["life_share", "character_media"]
MediaDeliveryMode = Literal["preview", "automatic"]
MediaCandidateStatus = Literal["available", "selected", "planned", "unrenderable"]
MediaLane = Literal["ordinary_life", "alluring_life", "exclusive_private", "explicit_reserved"]


class PhotoCandidate(FrozenModel):
    candidate_id: str = Field(min_length=1, max_length=256)
    source_event_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    family: MediaFamily
    privacy_ceiling: PrivacyClass

    @model_validator(mode="after")
    def canonical_sources(self) -> "PhotoCandidate":
        if self.source_event_refs != tuple(sorted(set(self.source_event_refs))):
            raise ValueError("photo candidate source refs must be sorted and unique")
        return self


class MediaEvidenceSource(FrozenModel):
    event_ref: str = Field(min_length=1, max_length=512)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FrozenMediaEvidenceSnapshot(FrozenModel):
    """The only ledger-independent bytes that planning may read.

    Rich location/activity/appearance rendering stays inside the image module;
    this wire object proves that every such adapter input descended from these
    exact committed ledger envelopes rather than chat prose or private state.
    """

    source_events: tuple[MediaEvidenceSource, ...] = Field(min_length=1, max_length=32)
    # These values deliberately remain evidence, not a prompt vocabulary.  A
    # media adapter may consume them only after verifying this exact sidecar
    # hash.  Keeping the complete selected candidate here prevents a later
    # planner/render worker from reading the mutable world projection.
    complete_candidate: dict[str, object] | None = None
    location: dict[str, object] | None = None
    visible_physical_state: dict[str, object] | None = None
    recipient_context: dict[str, object] | None = None

    @model_validator(mode="after")
    def source_events_are_canonical(self) -> "FrozenMediaEvidenceSnapshot":
        refs = tuple(item.event_ref for item in self.source_events)
        if refs != tuple(sorted(set(refs))):
            raise ValueError("media evidence snapshot source events must be sorted and unique")
        return self


class MediaArtifact(FrozenModel):
    """An immutable rendered/reused file, before any viewer-facing preview."""

    artifact_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=256)
    render_action_id: str = Field(min_length=1, max_length=256)
    artifact_ref: str = Field(min_length=1, max_length=1024)
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    media_type: str = Field(default="image/png", min_length=1, max_length=128)
    attempts: int = Field(ge=0, le=2)


class MediaInspectionRecord(FrozenModel):
    """Bounded inspection output.  World state never stores the image prompt."""

    inspection_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=256)
    artifact_id: str = Field(min_length=1, max_length=256)
    inspection_action_id: str = Field(min_length=1, max_length=256)
    passed: bool
    reason_code: str = Field(min_length=1, max_length=256)
    observed_summary: str | None = Field(default=None, max_length=4_000)
    inspection_payload_ref: str = Field(min_length=1, max_length=1024)
    inspection_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MediaPreview(FrozenModel):
    """A preview is an operator/viewer artifact, explicitly not a delivery."""

    preview_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=256)
    artifact_id: str = Field(min_length=1, max_length=256)
    inspection_id: str = Field(min_length=1, max_length=256)
    recipient_ref: str | None = Field(default=None, min_length=1, max_length=256)
    delivery_mode: Literal["preview"] = "preview"


class MediaRenderArtifactRecordedPayload(FrozenModel):
    action_id: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)
    artifact: MediaArtifact


class MediaInspectionRecordedPayload(FrozenModel):
    action_id: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)
    inspection: MediaInspectionRecord


class MediaPreviewGeneratedPayload(FrozenModel):
    preview: MediaPreview


class MediaPreviewFailedPayload(FrozenModel):
    plan_id: str = Field(min_length=1)
    artifact_id: str | None = Field(default=None, min_length=1)
    inspection_id: str | None = Field(default=None, min_length=1)
    reason_code: str = Field(min_length=1, max_length=256)


class MediaOpportunity(FrozenModel):
    opportunity_id: str = Field(min_length=1, max_length=256)
    candidate_id: str = Field(min_length=1, max_length=256)
    family: MediaFamily
    delivery_mode: MediaDeliveryMode
    privacy_ceiling: PrivacyClass
    event_snapshot_ref: str = Field(min_length=1, max_length=512)
    event_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_event_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    catalog_version: str = Field(min_length=1, max_length=128)
    media_machine_version: Literal["media-machine.v5"] = "media-machine.v5"
    inspection_contract_version: Literal["media-inspection.v7"] = "media-inspection.v7"
    media_lane: MediaLane = "ordinary_life"
    recipient_ref: str | None = Field(default=None, min_length=1, max_length=256)
    private_expression_basis_ref: str | None = Field(default=None, min_length=1, max_length=512)
    expires_at: datetime

    @model_validator(mode="after")
    def opportunity_sources_are_canonical(self) -> "MediaOpportunity":
        if self.source_event_refs != tuple(sorted(set(self.source_event_refs))):
            raise ValueError("media opportunity source refs must be sorted and unique")
        if self.media_lane == "alluring_life" and (
            self.privacy_ceiling != "private" or self.recipient_ref is None
        ):
            raise ValueError("alluring life requires a private ceiling and frozen recipient")
        if self.media_lane == "exclusive_private" and (
            self.privacy_ceiling != "private"
            or self.recipient_ref is None
            or self.private_expression_basis_ref is None
        ):
            raise ValueError("exclusive private requires recipient-specific private-expression basis")
        if self.media_lane not in {"exclusive_private"} and self.private_expression_basis_ref is not None:
            raise ValueError("private-expression basis may only authorize exclusive-private media")
        return self


class MediaPlan(FrozenModel):
    plan_id: str = Field(min_length=1, max_length=256)
    planning_request_id: str = Field(min_length=1, max_length=256)
    opportunity_id: str = Field(min_length=1, max_length=256)
    event_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    family: MediaFamily
    planner_version: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=128)
    media_machine_version: Literal["media-machine.v5"] = "media-machine.v5"
    inspection_contract_version: Literal["media-inspection.v7"] = "media-inspection.v7"
    media_lane: MediaLane = "ordinary_life"
    plan_payload_ref: str = Field(min_length=1, max_length=512)
    plan_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    frozen_at: datetime


class MediaNotRenderable(FrozenModel):
    opportunity_id: str = Field(min_length=1)
    planning_request_id: str = Field(min_length=1)
    event_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason_code: str = Field(min_length=1, max_length=128)
    planner_version: str = Field(min_length=1, max_length=128)


class PhotoCandidateOpenedPayload(FrozenModel):
    candidate: PhotoCandidate


class MediaOpportunityFrozenPayload(FrozenModel):
    opportunity: MediaOpportunity


class MediaPlanRecordedPayload(FrozenModel):
    action_id: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)
    plan: MediaPlan


class MediaNotRenderableRecordedPayload(FrozenModel):
    action_id: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)
    result: MediaNotRenderable


MEDIA_V2_PAYLOAD_MODELS = {
    "PhotoCandidateOpened": PhotoCandidateOpenedPayload,
    "MediaOpportunityFrozen": MediaOpportunityFrozenPayload,
    "MediaPlanRecorded": MediaPlanRecordedPayload,
    "MediaNotRenderableRecorded": MediaNotRenderableRecordedPayload,
    "MediaRenderArtifactRecorded": MediaRenderArtifactRecordedPayload,
    "MediaInspectionRecorded": MediaInspectionRecordedPayload,
    "MediaPreviewGenerated": MediaPreviewGeneratedPayload,
    "MediaPreviewFailed": MediaPreviewFailedPayload,
}


class StoredMediaPayload:
    """Opaque bytes owned by the media adapter, never decoded by World reducers."""

    __slots__ = ("payload_ref", "payload_hash", "content_type", "body")

    def __init__(self, *, payload_ref: str, payload_hash: str, content_type: str, body: str) -> None:
        if not payload_ref or not content_type or not body:
            raise ValueError("media sidecar record is incomplete")
        if payload_hash != media_payload_hash(body):
            raise ValueError("media sidecar payload hash does not bind exact bytes")
        self.payload_ref, self.payload_hash, self.content_type, self.body = payload_ref, payload_hash, content_type, body

    def __eq__(self, other: object) -> bool:
        return type(other) is StoredMediaPayload and tuple(getattr(self, key) for key in self.__slots__) == tuple(getattr(other, key) for key in self.__slots__)


class ImmutableMediaPayloadStore(Protocol):
    def put_if_absent(self, record: StoredMediaPayload) -> None: ...
    def read_exact(self, *, payload_ref: str) -> StoredMediaPayload | None: ...


class InMemoryImmutableMediaPayloadStore:
    def __init__(self) -> None:
        self._records: dict[str, StoredMediaPayload] = {}
        self._lock = RLock()

    def put_if_absent(self, record: StoredMediaPayload) -> None:
        with self._lock:
            prior = self._records.get(record.payload_ref)
            if prior is None:
                self._records[record.payload_ref] = record
            elif prior != record:
                raise ValueError("media payload ref is already bound to different immutable bytes")

    def read_exact(self, *, payload_ref: str) -> StoredMediaPayload | None:
        with self._lock:
            return self._records.get(payload_ref)


class SQLiteImmutableMediaPayloadStore:
    def __init__(self, *, path: str, world_id: str) -> None:
        self._world_id, self._lock = world_id, RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("""CREATE TABLE IF NOT EXISTS world_v2_media_payload (
            world_id TEXT NOT NULL, payload_ref TEXT NOT NULL, payload_hash TEXT NOT NULL,
            content_type TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(world_id, payload_ref))""")
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def put_if_absent(self, record: StoredMediaPayload) -> None:
        with self._lock:
            row = self._connection.execute("SELECT payload_hash, content_type, body FROM world_v2_media_payload WHERE world_id=? AND payload_ref=?", (self._world_id, record.payload_ref)).fetchone()
            if row is not None:
                if StoredMediaPayload(payload_ref=record.payload_ref, payload_hash=row[0], content_type=row[1], body=row[2]) != record:
                    raise ValueError("media payload ref is already bound to different immutable bytes")
                return
            self._connection.execute("INSERT INTO world_v2_media_payload VALUES (?, ?, ?, ?, ?)", (self._world_id, record.payload_ref, record.payload_hash, record.content_type, record.body))
            self._connection.commit()

    def read_exact(self, *, payload_ref: str) -> StoredMediaPayload | None:
        with self._lock:
            row = self._connection.execute("SELECT payload_hash, content_type, body FROM world_v2_media_payload WHERE world_id=? AND payload_ref=?", (self._world_id, payload_ref)).fetchone()
        return None if row is None else StoredMediaPayload(payload_ref=payload_ref, payload_hash=row[0], content_type=row[1], body=row[2])


@dataclass(frozen=True, slots=True)
class MediaPlanningResult:
    """Adapter result. Exactly one of ``plan`` or ``not_renderable`` is present."""
    plan: MediaPlan | None = None
    not_renderable: MediaNotRenderable | None = None
    plan_payload: StoredMediaPayload | None = None

    def __post_init__(self) -> None:
        if (self.plan is None) == (self.not_renderable is None):
            raise ValueError("media planning result must contain exactly one terminal outcome")
        if self.plan is None and self.plan_payload is not None:
            raise ValueError("not-renderable planning result cannot include a plan payload")
        if self.plan is not None and self.plan_payload is not None and (
            self.plan_payload.payload_ref != self.plan.plan_payload_ref
            or self.plan_payload.payload_hash != self.plan.plan_payload_hash
            or self.plan_payload.content_type != "application/vnd.world-v2.media-plan+json"
        ):
            raise ValueError("planning result payload does not bind frozen MediaPlan")


class MediaPlanner(Protocol):
    async def plan(self, *, opportunity: MediaOpportunity, planning_request_id: str) -> MediaPlanningResult: ...
    async def lookup(self, *, planning_request_id: str) -> MediaPlanningResult | None: ...


def planning_request_id(opportunity_id: str) -> str:
    return "media-plan-request:" + media_digest({"contract": "media-v2-planning.1", "opportunity_id": opportunity_id})


def continuation_trigger_id(plan: MediaPlan) -> str:
    return "media-continuation:" + media_digest({"plan_id": plan.plan_id, "step": "plan_to_render"})


__all__ = [
    "MEDIA_V2_PAYLOAD_MODELS", "PhotoCandidate", "MediaEvidenceSource", "FrozenMediaEvidenceSnapshot", "MediaOpportunity", "MediaPlan", "MediaNotRenderable", "MediaArtifact", "MediaInspectionRecord", "MediaPreview",
    "PhotoCandidateOpenedPayload", "MediaOpportunityFrozenPayload", "MediaPlanRecordedPayload", "MediaNotRenderableRecordedPayload", "MediaRenderArtifactRecordedPayload", "MediaInspectionRecordedPayload", "MediaPreviewGeneratedPayload", "MediaPreviewFailedPayload",
    "StoredMediaPayload", "ImmutableMediaPayloadStore", "InMemoryImmutableMediaPayloadStore", "SQLiteImmutableMediaPayloadStore",
    "MediaPlanner", "MediaPlanningResult", "media_digest", "media_payload_hash", "planning_request_id", "continuation_trigger_id",
]
