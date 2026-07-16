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
from .media_v2 import (
    FrozenMediaEvidenceSnapshot,
    ImageEvidenceIndexEntry,
    ImageEventSnapshot,
    MediaEvidenceSource,
    PhotoCandidate,
    canonical_media_json,
    media_digest,
    media_payload_hash,
)
from .schemas import ProjectionCursor, WorldEvent


_PUBLIC_VISIBILITIES = frozenset({"public", "shareable"})
_SUPPORTED_EVENT_TYPES = frozenset({
    "ActivityPlanned", "ActivityStarted", "ActivityResumed", "ActivityCompleted", "ActivityAbandoned",
    "WorldOccurrenceSettled", "ExperienceCommitted", "FactCommitted", "FactCorrected",
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
    if isinstance(value, tuple):
        return tuple(
            child
            for index, nested in enumerate(value)
            for child in _leaf_pointers(nested, pointer + "/" + str(index))
        )
    return (pointer,)


def _visibility(value: object, *, reason: str) -> Literal["public", "shareable"]:
    if value not in _PUBLIC_VISIBILITIES:
        raise MediaEvidenceNotRenderable(reason)
    return value  # type: ignore[return-value]


def _plain_leaf(value: object, *, reason: str) -> str | int | float | bool | None:
    if value is None or type(value) in {str, int, float, bool}:
        return value  # type: ignore[return-value]
    raise MediaEvidenceNotRenderable(reason)


def _clean_mapping(
    value: object, *, fields: frozenset[str], reason: str,
    fallback_visibility: Literal["public", "shareable"],
) -> tuple[dict[str, object], Literal["public", "shareable"]]:
    if not isinstance(value, dict):
        raise MediaEvidenceNotRenderable(reason)
    unknown = set(value) - fields - {"evidence_visibility"}
    if unknown:
        raise MediaEvidenceNotRenderable(reason)
    visibility = _visibility(value.get("evidence_visibility", fallback_visibility), reason=reason)
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
        if candidate.family != "life_share" or candidate.privacy_ceiling not in _PUBLIC_VISIBILITIES:
            raise MediaEvidenceNotRenderable("p0_requires_public_life_share")
        projection = self._ledger.project_at(request.cursor)
        self._require_exact_cursor(projection, request.cursor)
        events = self._load_sources(candidate=candidate, projection=projection, cursor=request.cursor)
        primary = max(events, key=lambda item: (item.logical_time, item.event_id))

        source_events = tuple(
            MediaEvidenceSource(event_ref=event.event_id, payload_hash=event.payload_hash)
            for event in sorted(events, key=lambda item: item.event_id)
        )
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
        for event in sorted(events, key=lambda item: (item.logical_time, item.event_id)):
            self._merge_explicit_evidence(
                target=body, origins=origins, event=event, fallback_visibility=candidate.privacy_ceiling,
            )

        evidence_index = self._build_index(body=body, origins=origins)
        snapshot = FrozenMediaEvidenceSnapshot(
            source_events=source_events,
            image_event_snapshot=ImageEventSnapshot(
                event=body["event"], source=body["source"], location=body["location"],
                activity=body["activity"], participants=body["participants"], objects=body["objects"],
                environment=body["environment"], character=body["character"],
                existing_media=body["existing_media"], visual_requirements=body["visual_requirements"],
                relationship_media_context=None, evidence_index=evidence_index,
            ),
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
            evidence_index_digest=media_digest(image_snapshot.evidence_index),
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
        self, *, target: dict[str, object], origins: dict[str, tuple[WorldEvent, Literal["public", "shareable"]]],
        event: WorldEvent, fallback_visibility: Literal["public", "shareable"],
    ) -> None:
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
        visibility = _visibility(raw.get("visibility", fallback_visibility), reason="image_evidence_not_public_or_shareable")
        for key in ("summary", "outcome"):
            if key in raw:
                target["event"][key] = _plain_leaf(raw[key], reason="malformed_image_evidence")  # type: ignore[index]
                origins["/event/" + key] = (event, visibility)
        for section in ("location", "activity", "environment"):
            if section not in raw:
                continue
            cleaned, section_visibility = _clean_mapping(
                raw[section], fields=_SECTION_FIELDS[section], reason="malformed_image_evidence",
                fallback_visibility=visibility,
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
                    fallback_visibility=visibility,
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
                    fallback_visibility=visibility,
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

    @staticmethod
    def _build_index(
        *, body: dict[str, object], origins: dict[str, tuple[WorldEvent, Literal["public", "shareable"]]],
    ) -> dict[str, ImageEvidenceIndexEntry]:
        index: dict[str, ImageEvidenceIndexEntry] = {}
        for root in ("event", "source", "location", "activity", "participants", "objects", "environment", "character", "existing_media", "visual_requirements"):
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
