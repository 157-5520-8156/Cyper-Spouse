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
MediaCandidateStatus = Literal[
    "available",
    "selected",
    "planned",
    "generated",
    "shared",
    "skipped",
    "unrenderable",
    "expired",
    "failed",
]
MediaLane = Literal["ordinary_life", "alluring_life", "exclusive_private", "explicit_reserved"]
MediaPrivacyCeiling = Literal["ordinary", "personal", "intimate"]
CharacterMediaKind = Literal[
    "public_checkin", "selfie", "mirror", "companion_shot", "body_detail",
]
CharacterCaptureMode = Literal[
    "character_front_camera", "character_rear_camera", "mirror", "timer_fixed",
    "requested_helper", "known_companion",
]
CharacterVisibility = Literal["identifiable", "body_detail"]


class MediaEvidenceSource(FrozenModel):
    event_ref: str = Field(min_length=1, max_length=512)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def character_media_contract_digest(
    *,
    subject_ref: str,
    kind: CharacterMediaKind,
    source_events: tuple[MediaEvidenceSource, ...],
    allowed_capture_modes: tuple[CharacterCaptureMode, ...],
    allowed_character_visibility: tuple[CharacterVisibility, ...],
) -> str:
    """Hash the complete, non-model-editable P2 visual allowance."""

    return media_digest({
        "contract": "character-media-candidate.1",
        "subject_ref": subject_ref,
        "kind": kind,
        "source_events": [item.model_dump(mode="json") for item in source_events],
        "allowed_capture_modes": allowed_capture_modes,
        "allowed_character_visibility": allowed_character_visibility,
    })


class CharacterMediaCandidateContract(FrozenModel):
    """The closed P2 visual space attached to one character-media candidate."""

    subject_ref: str = Field(min_length=1, max_length=512)
    kind: CharacterMediaKind
    allowed_capture_modes: tuple[CharacterCaptureMode, ...] = Field(min_length=1, max_length=6)
    allowed_character_visibility: tuple[CharacterVisibility, ...] = Field(min_length=1, max_length=2)
    authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def allowed_space_is_canonical(self) -> "CharacterMediaCandidateContract":
        if self.allowed_capture_modes != tuple(sorted(set(self.allowed_capture_modes))):
            raise ValueError("character media capture modes must be sorted and unique")
        if self.allowed_character_visibility != tuple(sorted(set(self.allowed_character_visibility))):
            raise ValueError("character media visibility must be sorted and unique")
        kind_modes: dict[CharacterMediaKind, frozenset[CharacterCaptureMode]] = {
            "public_checkin": frozenset({"timer_fixed", "requested_helper"}),
            "selfie": frozenset({"character_front_camera"}),
            "mirror": frozenset({"mirror"}),
            "companion_shot": frozenset({"known_companion"}),
            "body_detail": frozenset({"character_front_camera", "character_rear_camera"}),
        }
        if not set(self.allowed_capture_modes) <= kind_modes[self.kind]:
            raise ValueError("character media capture mode is incompatible with candidate kind")
        expected_visibility = ("body_detail",) if self.kind == "body_detail" else ("identifiable",)
        if self.allowed_character_visibility != expected_visibility:
            raise ValueError("character media visibility is incompatible with candidate kind")
        return self


class PhotoCandidate(FrozenModel):
    """Durable media-candidate aggregate, not merely a source descriptor.

    P0 records did not retain lifecycle coordinates, so those fields remain
    optional for replay compatibility.  Every P1 candidate must carry the
    complete source/time tuple; later selection acceptance pins this aggregate
    by ``entity_revision`` instead of trusting an ID string alone.
    """

    candidate_id: str = Field(min_length=1, max_length=256)
    source_event_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    family: MediaFamily
    privacy_ceiling: PrivacyClass
    entity_revision: int = Field(default=1, ge=1)
    status: MediaCandidateStatus = "available"
    opened_at: datetime | None = None
    expires_at: datetime | None = None
    ecology_category: str | None = Field(default=None, min_length=1, max_length=128)
    ecology_observed_at: datetime | None = None
    source_events: tuple[MediaEvidenceSource, ...] = ()
    character_media_contract: CharacterMediaCandidateContract | None = None
    opened_event_ref: str | None = Field(default=None, min_length=1, max_length=512)
    opened_event_payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def lifecycle_coordinates_are_closed(self) -> "PhotoCandidate":
        if self.source_event_refs != tuple(sorted(set(self.source_event_refs))):
            raise ValueError("photo candidate source refs must be sorted and unique")
        source_refs = tuple(item.event_ref for item in self.source_events)
        if self.source_events and source_refs != self.source_event_refs:
            raise ValueError("photo candidate source hashes must match source refs exactly")
        lifecycle_values = (
            self.opened_at,
            self.expires_at,
            self.ecology_category,
            self.ecology_observed_at,
        )
        if any(value is not None for value in lifecycle_values):
            if not all(value is not None for value in lifecycle_values) or not self.source_events:
                raise ValueError("P1 photo candidate requires complete lifecycle coordinates")
            if self.expires_at <= self.opened_at:
                raise ValueError("photo candidate expiry must follow opening")
        elif self.source_events:
            raise ValueError("legacy photo candidate cannot carry source hashes without lifecycle coordinates")
        if (self.opened_event_ref is None) != (self.opened_event_payload_hash is None):
            raise ValueError("photo candidate opening event coordinates are incomplete")
        if self.opened_event_ref is not None and self.opened_at is None:
            raise ValueError("candidate opening event requires P1 lifecycle coordinates")
        if self.family == "character_media":
            if self.character_media_contract is None:
                raise ValueError("character media candidate requires a frozen visual contract")
            if self.character_media_contract.authority_digest != character_media_contract_digest(
                subject_ref=self.character_media_contract.subject_ref,
                kind=self.character_media_contract.kind,
                source_events=self.source_events,
                allowed_capture_modes=self.character_media_contract.allowed_capture_modes,
                allowed_character_visibility=self.character_media_contract.allowed_character_visibility,
            ):
                raise ValueError("character media candidate contract digest does not bind candidate sources")
        elif self.character_media_contract is not None:
            raise ValueError("life-share candidate may not carry a character media contract")
        return self


class ImageEvidenceIndexEntry(FrozenModel):
    """Provenance for one planner-readable RFC 6901 snapshot leaf."""

    source_event_ref: str = Field(min_length=1, max_length=512)
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    visibility: PrivacyClass


class ImageEventSnapshot(FrozenModel):
    """Versioned, immutable World → image-machine event slice.

    This is deliberately data, not a prompt.  Every non-structural value the
    image planner may read is bound through ``evidence_index`` to one member of
    the enclosing ``FrozenMediaEvidenceSnapshot.source_events`` tuple.
    """

    schema_version: Literal["world-image-event-snapshot-v1"] = "world-image-event-snapshot-v1"
    event: dict[str, object]
    source: dict[str, object]
    location: dict[str, object]
    activity: dict[str, object]
    participants: tuple[dict[str, object], ...]
    objects: tuple[dict[str, object], ...]
    environment: dict[str, object]
    character: dict[str, object]
    existing_media: tuple[dict[str, object], ...]
    visual_requirements: dict[str, object]
    relationship_media_context: None = None
    evidence_index: dict[str, ImageEvidenceIndexEntry]

    @model_validator(mode="after")
    def planner_readable_leaves_have_exact_provenance(self) -> "ImageEventSnapshot":
        """Keep the index a closed allow-list instead of advisory metadata."""

        def escape(token: str) -> str:
            return token.replace("~", "~0").replace("/", "~1")

        def leaves(value: object, pointer: str) -> set[str]:
            if isinstance(value, dict):
                result: set[str] = set()
                for key, nested in value.items():
                    result |= leaves(nested, pointer + "/" + escape(key))
                return result
            if isinstance(value, tuple):
                result = set()
                for index, nested in enumerate(value):
                    result |= leaves(nested, pointer + "/" + str(index))
                return result
            return {pointer}

        # ``schema_version`` and the absence-only relationship slot are wire
        # structure, not image facts.  Everything else is planner-readable.
        readable = {
            "event": self.event,
            "source": self.source,
            "location": self.location,
            "activity": self.activity,
            "participants": self.participants,
            "objects": self.objects,
            "environment": self.environment,
            "character": self.character,
            "existing_media": self.existing_media,
            "visual_requirements": self.visual_requirements,
        }
        expected = set().union(*(leaves(value, "/" + key) for key, value in readable.items()))
        supplied = set(self.evidence_index)
        if expected != supplied:
            raise ValueError("image evidence index must cover exactly every planner-readable snapshot leaf")
        return self


class CharacterMediaSnapshotAuthorization(FrozenModel):
    """Adapter-only P2 allowance; it is intentionally not planner evidence."""

    candidate_id: str = Field(min_length=1, max_length=256)
    candidate_revision: int = Field(ge=1)
    subject_ref: str = Field(min_length=1, max_length=512)
    kind: CharacterMediaKind
    allowed_capture_modes: tuple[CharacterCaptureMode, ...] = Field(min_length=1, max_length=6)
    allowed_character_visibility: tuple[CharacterVisibility, ...] = Field(min_length=1, max_length=2)
    authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_event_refs: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def is_canonical(self) -> "CharacterMediaSnapshotAuthorization":
        if self.source_event_refs != tuple(sorted(set(self.source_event_refs))):
            raise ValueError("character snapshot authorization source refs must be sorted and unique")
        if self.allowed_capture_modes != tuple(sorted(set(self.allowed_capture_modes))):
            raise ValueError("character snapshot authorization modes must be sorted and unique")
        if self.allowed_character_visibility != tuple(sorted(set(self.allowed_character_visibility))):
            raise ValueError("character snapshot authorization visibility must be sorted and unique")
        return self


class ImageEventSnapshotV2(ImageEventSnapshot):
    """P2 ordinary-character snapshot, distinct from P0's public life-share wire."""

    schema_version: Literal["world-image-event-snapshot-v2"] = "world-image-event-snapshot-v2"


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
    image_event_snapshot: ImageEventSnapshotV2 | ImageEventSnapshot | None = None
    # P2 authorization is deliberately outer-sidecar data.  The inner image
    # event snapshot is planner input; letting a capture capability or a
    # contract digest appear there would turn permission data into prompt
    # material.  An adapter may inspect this value only to validate and build
    # a smaller planner view.
    character_media_authorization: CharacterMediaSnapshotAuthorization | None = None

    @model_validator(mode="after")
    def source_events_are_canonical(self) -> "FrozenMediaEvidenceSnapshot":
        refs = tuple(item.event_ref for item in self.source_events)
        if refs != tuple(sorted(set(refs))):
            raise ValueError("media evidence snapshot source events must be sorted and unique")
        if self.image_event_snapshot is not None:
            source_hashes = {item.event_ref: item.payload_hash for item in self.source_events}
            for pointer, entry in self.image_event_snapshot.evidence_index.items():
                if not pointer.startswith("/"):
                    raise ValueError("image evidence index key must be an RFC 6901 pointer")
                if source_hashes.get(entry.source_event_ref) != entry.source_payload_hash:
                    raise ValueError("image evidence index must bind an outer snapshot source")
        if self.character_media_authorization is not None:
            if self.image_event_snapshot is None or self.image_event_snapshot.schema_version != "world-image-event-snapshot-v2":
                raise ValueError("character snapshot authorization requires a V2 image snapshot")
            if self.complete_candidate is None:
                raise ValueError("character snapshot authorization requires the complete candidate")
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
    repairable: bool = False
    repair_scope: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def repair_contract_is_closed(self) -> "MediaInspectionRecord":
        if self.repair_scope != tuple(sorted(set(self.repair_scope))):
            raise ValueError("media inspection repair scope must be sorted and unique")
        if self.passed and (self.repairable or self.repair_scope):
            raise ValueError("passed inspection cannot authorize repair")
        if self.repairable and not self.repair_scope:
            raise ValueError("repairable inspection requires a visible defect scope")
        if not self.repairable and self.repair_scope:
            raise ValueError("non-repairable inspection cannot carry repair scope")
        return self


class MediaPreview(FrozenModel):
    """A preview is an operator/viewer artifact, explicitly not a delivery."""

    preview_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=256)
    artifact_id: str = Field(min_length=1, max_length=256)
    inspection_id: str = Field(min_length=1, max_length=256)
    recipient_ref: str | None = Field(default=None, min_length=1, max_length=256)
    delivery_mode: Literal["preview"] = "preview"


class MediaAutomaticDeliveryApproval(FrozenModel):
    """One operator-issued, revisioned exception to preview-only delivery.

    A rendered image is *never* an implicit permission to transmit it.  This
    object pins the exact inspected artifact (including a human-review sample
    hash), recipient and contract versions.  Re-approving the same approval
    id advances its revision, which invalidates any not-yet-dispatched Action
    bound to an older revision.
    """

    approval_id: str = Field(min_length=1, max_length=256)
    entity_revision: int = Field(ge=1)
    plan_id: str = Field(min_length=1, max_length=256)
    inspection_id: str = Field(min_length=1, max_length=256)
    artifact_id: str = Field(min_length=1, max_length=256)
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sample_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recipient_ref: str = Field(min_length=1, max_length=256)
    operator_ref: str = Field(min_length=1, max_length=256)
    family: MediaFamily
    media_machine_version: Literal["media-machine.v5"] = "media-machine.v5"
    inspection_contract_version: Literal["media-inspection.v7"] = "media-inspection.v7"
    approved_at: datetime
    expires_at: datetime
    approval_contract_version: Literal["media-auto-delivery.1"] = "media-auto-delivery.1"

    @model_validator(mode="after")
    def approval_is_time_bound_and_sample_bound(self) -> "MediaAutomaticDeliveryApproval":
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("media automatic delivery approval time must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("media automatic delivery approval expiry must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("media automatic delivery approval must expire after approval")
        if self.sample_hash != self.artifact_hash:
            raise ValueError("operator sample hash must bind the exact inspected artifact")
        return self


class MediaDeliveryShared(FrozenModel):
    """The sole postcondition claiming that a media artifact reached a viewer."""

    delivery_id: str = Field(min_length=1, max_length=256)
    approval_id: str = Field(min_length=1, max_length=256)
    approval_revision: int = Field(ge=1)
    plan_id: str = Field(min_length=1, max_length=256)
    inspection_id: str = Field(min_length=1, max_length=256)
    artifact_id: str = Field(min_length=1, max_length=256)
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recipient_ref: str = Field(min_length=1, max_length=256)
    action_id: str = Field(min_length=1, max_length=256)
    receipt_id: str = Field(min_length=1, max_length=512)


class MediaAutomaticDeliveryApprovedPayload(FrozenModel):
    approval: MediaAutomaticDeliveryApproval


class MediaDeliverySharedPayload(FrozenModel):
    delivery: MediaDeliveryShared


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


class MediaRepairAuthorization(FrozenModel):
    """One accepted, source-bound repair of a failed visual inspection.

    The object deliberately holds references and hashes only.  The image
    module resolves the frozen plan, failed artifact, and inspection sidecars
    itself; no repaired prompt or new world evidence can enter this contract.
    """

    repair_attempt_id: str = Field(min_length=1, max_length=256)
    trigger_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=256)
    opportunity_id: str = Field(min_length=1, max_length=256)
    event_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    failed_artifact_id: str = Field(min_length=1, max_length=256)
    failed_artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    inspection_id: str = Field(min_length=1, max_length=256)
    inspection_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    defect_scope: tuple[str, ...] = Field(min_length=1, max_length=32)
    action_id: str = Field(min_length=1, max_length=256)
    reservation_id: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def repair_scope_is_canonical(self) -> "MediaRepairAuthorization":
        if self.defect_scope != tuple(sorted(set(self.defect_scope))):
            raise ValueError("media repair defect scope must be sorted and unique")
        return self


class MediaRepairAuthorizedPayload(FrozenModel):
    repair: MediaRepairAuthorization


class MediaOpportunity(FrozenModel):
    opportunity_id: str = Field(min_length=1, max_length=256)
    candidate_id: str = Field(min_length=1, max_length=256)
    family: MediaFamily
    delivery_mode: MediaDeliveryMode
    # World visibility ceiling.  It deliberately remains distinct from the
    # image machine's intimacy ceiling below for replay compatibility.
    privacy_ceiling: PrivacyClass
    media_privacy_ceiling: MediaPrivacyCeiling = "ordinary"
    event_snapshot_ref: str = Field(min_length=1, max_length=512)
    event_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # ``source_event_refs`` bind the fully frozen snapshot.  The candidate's
    # opening lineage is intentionally separate: P2 may add a fact-bound
    # appearance or short-lived physical-state record while accepting, but it
    # may never revise the candidate authority selected by deliberation.
    source_event_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    candidate_source_event_refs: tuple[str, ...] = ()
    snapshot_source_events: tuple[MediaEvidenceSource, ...] = ()
    catalog_version: str = Field(min_length=1, max_length=128)
    media_machine_version: Literal["media-machine.v5"] = "media-machine.v5"
    inspection_contract_version: Literal["media-inspection.v7"] = "media-inspection.v7"
    media_lane: MediaLane = "ordinary_life"
    # Ecology-only selection coordinates.  They are not a visual prompt and
    # give cooldown/replay logic a durable category without inspecting image
    # sidecar prose from legacy releases.
    ecology_category: str | None = Field(default=None, min_length=1, max_length=128)
    ecology_observed_at: datetime | None = None
    recipient_ref: str | None = Field(default=None, min_length=1, max_length=256)
    private_expression_basis_ref: str | None = Field(default=None, min_length=1, max_length=512)
    selection_proposal_id: str | None = Field(default=None, min_length=1, max_length=256)
    selection_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selected_candidate_revision: int | None = Field(default=None, ge=1)
    expires_at: datetime

    @model_validator(mode="after")
    def opportunity_sources_are_canonical(self) -> "MediaOpportunity":
        if self.source_event_refs != tuple(sorted(set(self.source_event_refs))):
            raise ValueError("media opportunity source refs must be sorted and unique")
        candidate_refs = self.candidate_source_event_refs or self.source_event_refs
        if candidate_refs != tuple(sorted(set(candidate_refs))):
            raise ValueError("media opportunity candidate source refs must be sorted and unique")
        if not set(candidate_refs).issubset(self.source_event_refs):
            raise ValueError("media opportunity snapshot sources must include candidate sources")
        if self.snapshot_source_events:
            snapshot_refs = tuple(item.event_ref for item in self.snapshot_source_events)
            if snapshot_refs != self.source_event_refs:
                raise ValueError("media opportunity snapshot source hashes must match source refs exactly")
        if self.family == "character_media" and (
            not self.candidate_source_event_refs or not self.snapshot_source_events
        ):
            raise ValueError("character media opportunity requires explicit snapshot lineage")
        if self.family == "life_share" and self.snapshot_source_events and candidate_refs != self.source_event_refs:
            raise ValueError("life-share opportunity may not expand candidate lineage")
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
        selection_coordinates = (
            self.selection_proposal_id,
            self.selection_hash,
            self.selected_candidate_revision,
        )
        if any(value is not None for value in selection_coordinates) and not all(
            value is not None for value in selection_coordinates
        ):
            raise ValueError("media opportunity selection coordinates are incomplete")
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


class PhotoCandidateUnrenderablePayload(FrozenModel):
    """Close one selected-attempt candidate before a planner Action exists.

    Evidence compilation is a hard precondition, not a retryable image
    provider failure.  Retaining the candidate ID/revision and reason makes
    this terminal result inspectable without inventing a replacement event.
    """

    candidate_id: str = Field(min_length=1, max_length=256)
    expected_entity_revision: int = Field(ge=1)
    reason_code: str = Field(min_length=1, max_length=128)


class PhotoCandidateExpiredPayload(FrozenModel):
    """Close an unselected source-bound candidate once its fixed window ends."""

    candidate_id: str = Field(min_length=1, max_length=256)
    expected_entity_revision: int = Field(ge=1)
    reason_code: Literal["expiry_elapsed"] = "expiry_elapsed"


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
    "PhotoCandidateUnrenderable": PhotoCandidateUnrenderablePayload,
    "PhotoCandidateExpired": PhotoCandidateExpiredPayload,
    "MediaOpportunityFrozen": MediaOpportunityFrozenPayload,
    "MediaPlanRecorded": MediaPlanRecordedPayload,
    "MediaNotRenderableRecorded": MediaNotRenderableRecordedPayload,
    "MediaRenderArtifactRecorded": MediaRenderArtifactRecordedPayload,
    "MediaInspectionRecorded": MediaInspectionRecordedPayload,
    "MediaPreviewGenerated": MediaPreviewGeneratedPayload,
    "MediaPreviewFailed": MediaPreviewFailedPayload,
    "MediaRepairAuthorized": MediaRepairAuthorizedPayload,
    "MediaAutomaticDeliveryApproved": MediaAutomaticDeliveryApprovedPayload,
    "MediaDeliveryShared": MediaDeliverySharedPayload,
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


def media_repair_trigger_id(*, world_id: str, inspection_id: str) -> str:
    return "media-repair:" + media_digest({"world_id": world_id, "inspection_id": inspection_id})


def media_repair_attempt_id(*, plan_id: str, failed_artifact_hash: str) -> str:
    return "media-repair-attempt:" + media_digest({
        "contract": "media-v2-repair.1", "plan_id": plan_id,
        "failed_artifact_hash": failed_artifact_hash, "attempt": 1,
    })


def media_repair_action_id(*, world_id: str, repair_attempt_id: str) -> str:
    return "action:media-repair:" + media_digest({"world": world_id, "repair": repair_attempt_id})


def media_repair_reservation_id(*, world_id: str, repair_attempt_id: str) -> str:
    return "reservation:media-repair:" + media_digest({"world": world_id, "repair": repair_attempt_id})


def media_delivery_action_id(*, world_id: str, approval_id: str, approval_revision: int) -> str:
    return "action:media-delivery:" + media_digest({
        "world": world_id, "approval": approval_id, "revision": approval_revision,
    })


def media_delivery_reservation_id(*, world_id: str, approval_id: str, approval_revision: int) -> str:
    return "reservation:media-delivery:" + media_digest({
        "world": world_id, "approval": approval_id, "revision": approval_revision,
    })


def media_delivery_id(*, action_id: str, receipt_id: str) -> str:
    return "media-delivery:" + media_digest({"action": action_id, "receipt": receipt_id})


__all__ = [
    "MEDIA_V2_PAYLOAD_MODELS", "PhotoCandidate", "MediaEvidenceSource", "ImageEvidenceIndexEntry", "ImageEventSnapshot", "FrozenMediaEvidenceSnapshot", "MediaPrivacyCeiling", "MediaOpportunity", "MediaPlan", "MediaNotRenderable", "MediaArtifact", "MediaInspectionRecord", "MediaPreview", "MediaRepairAuthorization", "MediaAutomaticDeliveryApproval", "MediaDeliveryShared",
    "PhotoCandidateOpenedPayload", "PhotoCandidateUnrenderablePayload", "PhotoCandidateExpiredPayload", "MediaOpportunityFrozenPayload", "MediaPlanRecordedPayload", "MediaNotRenderableRecordedPayload", "MediaRenderArtifactRecordedPayload", "MediaInspectionRecordedPayload", "MediaPreviewGeneratedPayload", "MediaPreviewFailedPayload", "MediaRepairAuthorizedPayload", "MediaAutomaticDeliveryApprovedPayload", "MediaDeliverySharedPayload",
    "StoredMediaPayload", "ImmutableMediaPayloadStore", "InMemoryImmutableMediaPayloadStore", "SQLiteImmutableMediaPayloadStore",
    "MediaPlanner", "MediaPlanningResult", "media_digest", "media_payload_hash", "planning_request_id", "continuation_trigger_id", "media_repair_trigger_id", "media_repair_attempt_id", "media_repair_action_id", "media_repair_reservation_id", "media_delivery_action_id", "media_delivery_reservation_id", "media_delivery_id",
]
