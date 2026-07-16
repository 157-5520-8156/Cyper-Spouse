"""Compile a pinned World v2 evidence slice for the image machine.

This module is deliberately the only seam that maps committed World evidence
to the versioned image-event snapshot.  It never reads a moving projection
after ``compile`` begins, resolves a ``value_ref``, invents visual prose, or
accepts caller-provided snapshot JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .ledger import LedgerPort
from .image_evidence_contract import ImageEvidenceDeclaredPayload
from .appearance_state import AppearanceStateRecordedPayload, appearance_state_at
from .visible_physical_state import (
    VisiblePhysicalStateRecordedPayload,
    visible_physical_state_at,
)
from .media_v2 import (
    FrozenMediaEvidenceSnapshot,
    CharacterMediaSnapshotAuthorization,
    ImageEvidenceIndexEntry,
    ImageEventSnapshot,
    ImageEventSnapshotV2,
    MediaEvidenceSource,
    PhotoCandidate,
    canonical_media_json,
    media_digest,
    media_payload_hash,
)
from .schemas import ProjectionCursor, WorldEvent
from .schema_core import PrivacyClass


_PUBLIC_VISIBILITIES = frozenset({"public", "shareable"})
_RECIPIENT_SCOPED_VISIBILITIES = frozenset({"personal", "private"})
_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}
_SUPPORTED_EVENT_TYPES = frozenset({
    "ActivityPlanned", "ActivityStarted", "ActivityResumed", "ActivityCompleted", "ActivityAbandoned",
    "WorldOccurrenceSettled", "ExperienceCommitted", "FactCommitted", "FactCorrected",
    "FactCommitMaterializedV2", "ImageEvidenceDeclared", "AppearanceStateRecorded",
    "VisiblePhysicalStateRecorded",
})
_SECTION_FIELDS: dict[str, frozenset[str]] = {
    "location": frozenset({"id", "kind", "country", "region", "city", "publicness", "mirror_available"}),
    "activity": frozenset({"id", "kind", "description", "phase", "intensity", "private_transition"}),
    "environment": frozenset({"light", "weather", "structure", "region"}),
    "participant": frozenset({"id", "role", "present", "visibility_permission"}),
    "object": frozenset({"id", "kind", "description", "ownership", "visibility"}),
    "existing_media": frozenset({"artifact_ref", "artifact_hash", "accessible", "reuse_authorized", "source"}),
}


class MediaEvidenceNotRenderable(ValueError):
    """A source-bound reason the caller must record or route explicitly."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class MediaEvidenceCompileRequest:
    candidate: PhotoCandidate
    category: str
    cursor: ProjectionCursor


@dataclass(frozen=True, slots=True)
class CompiledMediaEvidence:
    snapshot: FrozenMediaEvidenceSnapshot
    snapshot_body: str
    snapshot_ref: str
    snapshot_hash: str
    image_event_snapshot_body: str
    image_event_snapshot_hash: str
    evidence_index_digest: str


class _ProjectionLike(Protocol):
    world_revision: int
    logical_time: object
    committed_world_event_refs: tuple[object, ...]


def _escape_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _leaf_pointers(value: object, pointer: str) -> tuple[str, ...]:
    if isinstance(value, dict):
        return tuple(
            child
            for key, nested in value.items()
            for child in _leaf_pointers(nested, pointer + "/" + _escape_pointer_token(key))
        )
    if isinstance(value, (tuple, list)):
        return tuple(
            child
            for index, nested in enumerate(value)
            for child in _leaf_pointers(nested, pointer + "/" + str(index))
        )
    return (pointer,)


def _visibility(
    value: object, *, reason: str, allowed: frozenset[str] = _PUBLIC_VISIBILITIES
) -> PrivacyClass:
    if value not in allowed:
        raise MediaEvidenceNotRenderable(reason)
    return value  # type: ignore[return-value]


def _plain_leaf(value: object, *, reason: str) -> str | int | float | bool | None:
    if value is None or type(value) in {str, int, float, bool}:
        return value  # type: ignore[return-value]
    raise MediaEvidenceNotRenderable(reason)


def _clean_mapping(
    value: object, *, fields: frozenset[str], reason: str,
    fallback_visibility: PrivacyClass, allowed_visibilities: frozenset[str] = _PUBLIC_VISIBILITIES,
) -> tuple[dict[str, object], PrivacyClass]:
    if not isinstance(value, dict):
        raise MediaEvidenceNotRenderable(reason)
    unknown = set(value) - fields - {"evidence_visibility"}
    if unknown:
        raise MediaEvidenceNotRenderable(reason)
    visibility = _visibility(
        value.get("evidence_visibility", fallback_visibility), reason=reason,
        allowed=allowed_visibilities,
    )
    return (
        {key: _plain_leaf(item, reason=reason) for key, item in value.items() if key != "evidence_visibility"},
        visibility,
    )


class MediaEvidenceSnapshotCompiler:
    """Compile one candidate at one pinned cursor into immutable evidence bytes.

    The interface has one operation and three caller inputs.  The implementation
    performs source lookup, cursor/payload pinning, visibility filtering, JSON
    pointer provenance, and canonical hash construction behind that seam.
    """

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    def compile(self, request: MediaEvidenceCompileRequest) -> CompiledMediaEvidence:
        candidate = request.candidate
        if candidate.family not in {"life_share", "character_media"} or candidate.privacy_ceiling not in _PUBLIC_VISIBILITIES:
            raise MediaEvidenceNotRenderable("media_candidate_requires_public_or_shareable_evidence")
        if candidate.family == "character_media" and candidate.character_media_contract is None:
            raise MediaEvidenceNotRenderable("character_media_contract_missing")
        projection = self._ledger.project_at(request.cursor)
        self._require_exact_cursor(projection, request.cursor)
        events = self._load_sources(candidate=candidate, projection=projection, cursor=request.cursor)
        life_events = tuple(
            item for item in events
            if item.event_type not in {"ImageEvidenceDeclared", "AppearanceStateRecorded", "VisiblePhysicalStateRecorded"}
        )
        if not life_events:
            raise MediaEvidenceNotRenderable("image_evidence_has_no_life_source")
        primary = max(life_events, key=lambda item: (item.logical_time, item.event_id))

        body: dict[str, object] = {
            "schema_version": "world-image-event-snapshot-v1",
            "event": {
                "event_id": primary.event_id,
                "type": primary.event_type,
                "status": "committed",
                "logical_at": primary.logical_time.isoformat(),
            },
            "source": {"channel": "direct_experience", "person": "character"},
            "location": {}, "activity": {}, "participants": (), "objects": (), "environment": {},
            "character": {}, "existing_media": (),
            "visual_requirements": {"requires_readable_text": False},
            "relationship_media_context": None,
        }
        origins: dict[str, tuple[WorldEvent, Literal["public", "shareable"]]] = {
            "/event": (primary, candidate.privacy_ceiling),
            "/source": (primary, candidate.privacy_ceiling),
            "/visual_requirements": (primary, candidate.privacy_ceiling),
        }
        event_by_ref = {event.event_id: event for event in events}
        for event in sorted(events, key=lambda item: (item.logical_time, item.event_id)):
            if event.event_type == "ImageEvidenceDeclared":
                try:
                    declaration = ImageEvidenceDeclaredPayload.model_validate_json(event.payload_json)
                except ValueError as exc:
                    raise MediaEvidenceNotRenderable("malformed_image_evidence_declaration") from exc
                source = event_by_ref.get(declaration.source_event_ref)
                if (
                    source is None
                    or source.event_type != declaration.source_event_type
                    or source.payload_hash != declaration.source_event_payload_hash
                ):
                    raise MediaEvidenceNotRenderable("image_evidence_anchor_unavailable")
                self._merge_explicit_evidence(
                    target=body,
                    origins=origins,
                    event=event,
                    fallback_visibility=candidate.privacy_ceiling,
                    raw=declaration.image_evidence.planner_payload(),
                )
                continue
            self._merge_explicit_evidence(
                target=body, origins=origins, event=event, fallback_visibility=candidate.privacy_ceiling,
            )

        supplemental_events: tuple[WorldEvent, ...] = ()
        if candidate.family == "character_media":
            supplemental_events = self._freeze_character_evidence(
                candidate=candidate,
                primary=primary,
                events=events,
                projection=projection,
                body=body,
                origins=origins,
            )

        source_events = tuple(
            MediaEvidenceSource(event_ref=event.event_id, payload_hash=event.payload_hash)
            for event in sorted({event.event_id: event for event in (*events, *supplemental_events)}.values(), key=lambda item: item.event_id)
        )

        # An event envelope proves that something happened, not that there is
        # a photographable fact.  Do not let the downstream planner turn an
        # otherwise empty snapshot into a generic lifestyle image: at least
        # one concrete, source-indexed visual slice must have been committed.
        if not any(
            body[name]
            for name in ("location", "activity", "participants", "objects", "environment", "existing_media")
        ):
            raise MediaEvidenceNotRenderable("no_visual_evidence")

        evidence_index = self._build_index(body=body, origins=origins)
        character_authorization = body.pop("character_media_authorization", None)
        image_event_snapshot = (
            ImageEventSnapshotV2(
                event=body["event"], source=body["source"], location=body["location"],
                activity=body["activity"], participants=body["participants"], objects=body["objects"],
                environment=body["environment"], character=body["character"],
                existing_media=body["existing_media"], visual_requirements=body["visual_requirements"],
                relationship_media_context=None,
                evidence_index=evidence_index,
            )
            if candidate.family == "character_media"
            else ImageEventSnapshot(
                event=body["event"], source=body["source"], location=body["location"],
                activity=body["activity"], participants=body["participants"], objects=body["objects"],
                environment=body["environment"], character=body["character"],
                existing_media=body["existing_media"], visual_requirements=body["visual_requirements"],
                relationship_media_context=None, evidence_index=evidence_index,
            )
        )
        snapshot = FrozenMediaEvidenceSnapshot(
            source_events=source_events,
            complete_candidate=(candidate.model_dump(mode="json") if candidate.family == "character_media" else None),
            image_event_snapshot=image_event_snapshot,
            character_media_authorization=character_authorization,
        )
        image_snapshot = snapshot.image_event_snapshot
        if image_snapshot is None:  # Keep the sidecar boundary explicit even for type checkers.
            raise AssertionError("compiled media evidence must contain an image event snapshot")
        image_event_snapshot_body = canonical_media_json(image_snapshot.model_dump(mode="json"))
        snapshot_body = canonical_media_json(snapshot.model_dump(mode="json"))
        snapshot_ref = "sidecar:world-image-event-snapshot:" + media_digest({
            "contract": "world-image-event-snapshot-v1", "candidate_id": candidate.candidate_id,
            "category": request.category, "cursor": request.cursor.model_dump(mode="json"),
            "sources": [(item.event_ref, item.payload_hash) for item in source_events],
        })
        return CompiledMediaEvidence(
            snapshot=snapshot, snapshot_body=snapshot_body, snapshot_ref=snapshot_ref,
            snapshot_hash=media_payload_hash(snapshot_body),
            image_event_snapshot_body=image_event_snapshot_body,
            image_event_snapshot_hash=media_payload_hash(image_event_snapshot_body),
            evidence_index_digest=media_digest({
                pointer: entry.model_dump(mode="json")
                for pointer, entry in image_snapshot.evidence_index.items()
            }),
        )

    @staticmethod
    def _require_exact_cursor(projection: _ProjectionLike, cursor: ProjectionCursor) -> None:
        actual = (getattr(projection, "world_revision", None), getattr(projection, "deliberation_revision", None), getattr(projection, "ledger_sequence", None))
        expected = (cursor.world_revision, cursor.deliberation_revision, cursor.ledger_sequence)
        if actual != expected:
            raise ValueError("media evidence compiler requires the exact pinned projection cursor")

    def _load_sources(
        self, *, candidate: PhotoCandidate, projection: _ProjectionLike, cursor: ProjectionCursor,
    ) -> tuple[WorldEvent, ...]:
        projection_refs = {
            getattr(item, "event_id", None): item for item in projection.committed_world_event_refs
        }
        events: list[WorldEvent] = []
        for source_ref in candidate.source_event_refs:
            committed = projection_refs.get(source_ref)
            if committed is None or getattr(committed, "world_revision", cursor.world_revision + 1) > cursor.world_revision:
                raise MediaEvidenceNotRenderable("source_not_committed_at_pinned_cursor")
            found = self._ledger.lookup_event_commit(source_ref)
            if found is None:
                raise MediaEvidenceNotRenderable("source_event_unavailable")
            event, _commit = found
            if event.event_id != source_ref or event.payload_hash != getattr(committed, "payload_hash", None):
                raise MediaEvidenceNotRenderable("source_payload_hash_mismatch")
            if event.event_type not in _SUPPORTED_EVENT_TYPES or event.logical_time > projection.logical_time:
                raise MediaEvidenceNotRenderable("unsupported_or_future_source_event")
            events.append(event)
        if not events:
            raise MediaEvidenceNotRenderable("candidate_has_no_sources")
        return tuple(events)

    def _merge_explicit_evidence(
        self, *, target: dict[str, object], origins: dict[str, tuple[WorldEvent, PrivacyClass]],
        event: WorldEvent, fallback_visibility: PrivacyClass, raw: object | None = None,
        allowed_visibilities: frozenset[str] = _PUBLIC_VISIBILITIES,
        visibility_reason: str = "image_evidence_not_public_or_shareable",
    ) -> None:
        if raw is None:
            payload = event.payload()
            raw = payload.get("image_evidence")
        if raw is None:
            # In particular, ``value_ref`` / ``value_hash`` never give this
            # compiler authority to read a fact payload or invent its content.
            return
        if not isinstance(raw, dict):
            raise MediaEvidenceNotRenderable("malformed_image_evidence")
        allowed = {"visibility", "summary", "outcome", "location", "activity", "participants", "objects", "environment", "existing_media", "requires_readable_text"}
        if set(raw) - allowed:
            raise MediaEvidenceNotRenderable("malformed_image_evidence")
        visibility = _visibility(
            raw.get("visibility", fallback_visibility),
            reason=visibility_reason, allowed=allowed_visibilities,
        )
        for key in ("summary", "outcome"):
            if key in raw:
                target["event"][key] = _plain_leaf(raw[key], reason="malformed_image_evidence")  # type: ignore[index]
                origins["/event/" + key] = (event, visibility)
        for section in ("location", "activity", "environment"):
            if section not in raw:
                continue
            cleaned, section_visibility = _clean_mapping(
                raw[section], fields=_SECTION_FIELDS[section], reason="malformed_image_evidence",
                fallback_visibility=visibility, allowed_visibilities=allowed_visibilities,
            )
            if target[section]:
                raise MediaEvidenceNotRenderable("ambiguous_image_evidence")
            target[section] = cleaned
            origins["/" + section] = (event, section_visibility)
        for plural, singular in (("participants", "participant"), ("objects", "object")):
            if plural not in raw:
                continue
            values = raw[plural]
            if not isinstance(values, list) or target[plural]:
                raise MediaEvidenceNotRenderable("malformed_image_evidence")
            cleaned_values = tuple(
                _clean_mapping(
                    item, fields=_SECTION_FIELDS[singular], reason="malformed_image_evidence",
                    fallback_visibility=visibility, allowed_visibilities=allowed_visibilities,
                )
                for item in values
            )
            target[plural] = tuple(item[0] for item in cleaned_values)
            for index, (_item, item_visibility) in enumerate(cleaned_values):
                origins[f"/{plural}/{index}"] = (event, item_visibility)
        if "existing_media" in raw:
            values = raw["existing_media"]
            if not isinstance(values, list) or target["existing_media"]:
                raise MediaEvidenceNotRenderable("malformed_image_evidence")
            media_with_visibility = tuple(
                _clean_mapping(
                    item, fields=_SECTION_FIELDS["existing_media"], reason="malformed_existing_media",
                    fallback_visibility=visibility, allowed_visibilities=allowed_visibilities,
                )
                for item in values
            )
            media = tuple(item[0] for item in media_with_visibility)
            for item in media:
                if not (item.get("artifact_ref") and item.get("artifact_hash") and item.get("accessible") is True and item.get("reuse_authorized") is True):
                    raise MediaEvidenceNotRenderable("existing_media_requires_accessible_artifact")
            target["existing_media"] = media
            for index, (_item, item_visibility) in enumerate(media_with_visibility):
                origins[f"/existing_media/{index}"] = (event, item_visibility)
        if raw.get("requires_readable_text") is True:
            # P0 has no text-artifact reuse route.  It must not fake a ticket,
            # screen, menu, or sign from a fact description.
            raise MediaEvidenceNotRenderable("readable_text_requires_artifact")
        if "requires_readable_text" in raw and raw["requires_readable_text"] is not False:
            raise MediaEvidenceNotRenderable("malformed_image_evidence")

    def _freeze_character_evidence(
        self,
        *,
        candidate: PhotoCandidate,
        primary: WorldEvent,
        events: tuple[WorldEvent, ...],
        projection: _ProjectionLike,
        body: dict[str, object],
        origins: dict[str, tuple[WorldEvent, Literal["public", "shareable"]]],
    ) -> tuple[WorldEvent, ...]:
        """Freeze P2 character facts, not a current projection or prompt hint."""

        contract = candidate.character_media_contract
        if contract is None:  # The outer gate keeps this explicit for type checkers.
            raise MediaEvidenceNotRenderable("character_media_contract_missing")
        declaration_event: WorldEvent | None = None
        declaration: ImageEvidenceDeclaredPayload | None = None
        for event in events:
            if event.event_type != "ImageEvidenceDeclared":
                continue
            try:
                parsed = ImageEvidenceDeclaredPayload.model_validate_json(event.payload_json)
            except ValueError as exc:
                raise MediaEvidenceNotRenderable("malformed_image_evidence_declaration") from exc
            character = parsed.image_evidence.character_media
            if character is None or character.character_ref != contract.subject_ref:
                continue
            if not set(contract.allowed_capture_modes) <= set(character.capture_capabilities):
                continue
            if not self._character_contract_is_proven(contract=contract, declaration=parsed):
                continue
            declaration_event, declaration = event, parsed
            break
        if declaration_event is None or declaration is None:
            raise MediaEvidenceNotRenderable("character_media_contract_not_proven_by_declaration")
        visibility = _visibility(
            declaration.image_evidence.visibility,
            reason="character_media_evidence_not_public_or_shareable",
        )
        body["character"] = {
            "subject_ref": contract.subject_ref,
            "presence": {"present": True},
        }
        # The planner may read every leaf of ``character``.  Capture modes,
        # contract kind, and the digest are authorization coordinates, not
        # visual evidence, so they belong exclusively to the V2 adapter-only
        # allowance below.  Keeping the two planes separate prevents the
        # image planner from treating an internal permission as image content.
        body["character_media_authorization"] = CharacterMediaSnapshotAuthorization(
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.entity_revision,
            subject_ref=contract.subject_ref,
            kind=contract.kind,
            allowed_capture_modes=contract.allowed_capture_modes,
            allowed_character_visibility=contract.allowed_character_visibility,
            authority_digest=contract.authority_digest,
            source_event_refs=candidate.source_event_refs,
        )
        origins["/character"] = (declaration_event, visibility)
        extras: list[WorldEvent] = []
        appearance = appearance_state_at(
            tuple(getattr(projection, "appearance_states", ())),
            subject_ref=contract.subject_ref,
            at_logical_time=primary.logical_time,
        )
        if appearance is not None and self._visible_at_candidate_ceiling(
            value=appearance.visibility, ceiling=candidate.privacy_ceiling
        ):
            record, anchor = self._state_events(
                projection=projection,
                state=appearance,
                event_type="AppearanceStateRecorded",
                payload_model=AppearanceStateRecordedPayload,
            )
            body["character"]["appearance_state"] = appearance.model_dump(mode="python")  # type: ignore[index]
            origins["/character/appearance_state"] = (record, _visibility(appearance.visibility, reason="appearance_state_not_publishable"))
            extras.extend((record, anchor))
        physical = visible_physical_state_at(
            tuple(getattr(projection, "visible_physical_states", ())),
            subject_ref=contract.subject_ref,
            at_logical_time=primary.logical_time,
        )
        if physical is not None and self._visible_at_candidate_ceiling(
            value=physical.visibility, ceiling=candidate.privacy_ceiling
        ):
            record, anchor = self._state_events(
                projection=projection,
                state=physical,
                event_type="VisiblePhysicalStateRecorded",
                payload_model=VisiblePhysicalStateRecordedPayload,
            )
            body["character"]["visible_physical_state"] = physical.model_dump(mode="python")  # type: ignore[index]
            origins["/character/visible_physical_state"] = (record, _visibility(physical.visibility, reason="visible_physical_state_not_publishable"))
            extras.extend((record, anchor))
        return tuple(extras)

    @staticmethod
    def _visible_at_candidate_ceiling(*, value: object, ceiling: object) -> bool:
        return value in _PUBLIC_VISIBILITIES and _PRIVACY_RANK[value] <= _PRIVACY_RANK[ceiling]

    @staticmethod
    def _character_contract_is_proven(*, contract, declaration: ImageEvidenceDeclaredPayload) -> bool:  # type: ignore[no-untyped-def]
        evidence = declaration.image_evidence
        modes = set(evidence.character_media.capture_capabilities)  # type: ignore[union-attr]
        if contract.kind == "selfie":
            return "character_front_camera" in modes
        if contract.kind == "mirror":
            return "mirror" in modes and isinstance(evidence.location, dict) and evidence.location.get("mirror_available") is True
        if contract.kind == "public_checkin":
            return (
                bool({"timer_fixed", "requested_helper"}.intersection(modes))
                and isinstance(evidence.location, dict)
                and evidence.location.get("publicness") == "public"
            )
        if contract.kind == "companion_shot":
            return "known_companion" in modes and any(
                item.get("id") != contract.subject_ref
                and item.get("present") is True
                and item.get("visibility_permission") in _PUBLIC_VISIBILITIES
                for item in evidence.participants
                if isinstance(item, dict)
            )
        if contract.kind == "body_detail":
            detail = evidence.character_media.body_detail  # type: ignore[union-attr]
            return (
                detail is not None
                and bool({"character_front_camera", "character_rear_camera"}.intersection(modes))
                and any(
                    item.get("id") == detail.object_ref and item.get("visibility") in _PUBLIC_VISIBILITIES
                    for item in evidence.objects
                    if isinstance(item, dict)
                )
            )
        return False

    def _state_events(self, *, projection: _ProjectionLike, state, event_type: str, payload_model):  # type: ignore[no-untyped-def]
        """Find the exact state record and its separately committed anchor."""

        refs = {item.event_id: item for item in projection.committed_world_event_refs}
        record: WorldEvent | None = None
        for ref in refs.values():
            if getattr(ref, "event_type", None) != event_type:
                continue
            found = self._ledger.lookup_event_commit(ref.event_id)
            if found is None:
                continue
            event, _commit = found
            if event.payload_hash != getattr(ref, "payload_hash", None):
                continue
            try:
                if payload_model.model_validate_json(event.payload_json).state == state:
                    record = event
                    break
            except ValueError:
                continue
        if record is None:
            raise MediaEvidenceNotRenderable("active_state_record_unavailable")
        anchor_ref = refs.get(state.source_event_ref)
        anchor_found = self._ledger.lookup_event_commit(state.source_event_ref)
        if (
            anchor_ref is None
            or anchor_found is None
            or anchor_found[0].payload_hash != state.source_event_payload_hash
            or getattr(anchor_ref, "payload_hash", None) != state.source_event_payload_hash
        ):
            raise MediaEvidenceNotRenderable("active_state_anchor_unavailable")
        return record, anchor_found[0]

    @staticmethod
    def _build_index(
        *, body: dict[str, object], origins: dict[str, tuple[WorldEvent, PrivacyClass]],
    ) -> dict[str, ImageEvidenceIndexEntry]:
        index: dict[str, ImageEvidenceIndexEntry] = {}
        roots = [
            "event", "source", "location", "activity", "participants", "objects", "environment",
            "character", "existing_media", "visual_requirements",
        ]
        if body.get("relationship_media_context") is not None:
            roots.append("relationship_media_context")
        for root in roots:
            for pointer in _leaf_pointers(body[root], "/" + root):
                matching = [key for key in origins if pointer == key or pointer.startswith(key + "/")]
                if not matching:
                    raise MediaEvidenceNotRenderable("unproven_snapshot_leaf")
                origin, visibility = origins[max(matching, key=len)]
                index[pointer] = ImageEvidenceIndexEntry(
                    source_event_ref=origin.event_id, source_payload_hash=origin.payload_hash,
                    visibility=visibility,
                )
        return index


__all__ = [
    "CompiledMediaEvidence", "MediaEvidenceCompileRequest", "MediaEvidenceNotRenderable",
    "MediaEvidenceSnapshotCompiler",
]
