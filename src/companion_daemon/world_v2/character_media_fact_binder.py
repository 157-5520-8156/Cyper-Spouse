"""Discover ordinary character-media candidates from committed visual facts.

The binder is the only P2 seam allowed to turn a typed character-presence and
capture-capability declaration into a ``character_media`` candidate.  It does
not compose prompts, choose a pose, authorize an opportunity, or infer a
camera from a location/activity description.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from .image_evidence_contract import ImageEvidenceDeclaredPayload
from .media_v2 import (
    CharacterCaptureMode,
    CharacterMediaCandidateContract,
    CharacterMediaKind,
    CharacterVisibility,
    MediaEvidenceSource,
    PhotoCandidate,
    PhotoCandidateOpenedPayload,
    character_media_contract_digest,
    media_digest,
)
from .event_identity import domain_idempotency_key
from .schemas import ProjectionCursor, WorldEvent


_PUBLIC_VISIBILITIES = frozenset({"public", "shareable"})


class CharacterMediaFactBinderError(ValueError):
    """A declaration was malformed or unavailable at the pinned cursor."""


class _Ledger(Protocol):
    world_id: str

    def project_at(self, cursor: ProjectionCursor): ...  # type: ignore[no-untyped-def]
    def lookup_event_commit(self, event_id: str): ...  # type: ignore[no-untyped-def]


class CharacterMediaFactBinder:
    """Derive only fact-proven ordinary character-media candidate contracts."""

    def __init__(self, *, ledger: _Ledger, default_expiry: timedelta = timedelta(hours=24)) -> None:
        if default_expiry <= timedelta(0):
            raise ValueError("character media candidate expiry must be positive")
        self._ledger, self._default_expiry = ledger, default_expiry

    def discover(
        self, *, cursor: ProjectionCursor, logical_time: datetime,
    ) -> tuple[PhotoCandidate, ...]:
        projection = self._ledger.project_at(cursor)
        if (
            projection.world_revision != cursor.world_revision
            or projection.deliberation_revision != cursor.deliberation_revision
            or projection.ledger_sequence != cursor.ledger_sequence
            or projection.logical_time != logical_time
        ):
            raise CharacterMediaFactBinderError("character media discovery requires current pinned cursor")
        committed = {item.event_id: item for item in projection.committed_world_event_refs}
        existing = {item.candidate_id for item in getattr(projection, "photo_candidates", ())}
        discovered: list[PhotoCandidate] = []
        for ref in projection.committed_world_event_refs:
            if ref.event_type != "ImageEvidenceDeclared":
                continue
            declaration_event = self._event_at(ref.event_id, committed=committed)
            if declaration_event is None:
                continue
            try:
                declaration = ImageEvidenceDeclaredPayload.model_validate_json(declaration_event.payload_json)
            except ValueError:
                continue
            character = declaration.image_evidence.character_media
            if character is None:
                continue
            source_event = self._event_at(declaration.source_event_ref, committed=committed)
            if source_event is None or source_event.payload_hash != declaration.source_event_payload_hash:
                continue
            sources = tuple(sorted((
                MediaEvidenceSource(event_ref=source_event.event_id, payload_hash=source_event.payload_hash),
                MediaEvidenceSource(event_ref=declaration_event.event_id, payload_hash=declaration_event.payload_hash),
            ), key=lambda item: item.event_ref))
            for kind, modes, visibility in self._contracts(declaration=declaration):
                contract = CharacterMediaCandidateContract(
                    subject_ref=character.character_ref,
                    kind=kind,
                    allowed_capture_modes=modes,
                    allowed_character_visibility=visibility,
                    authority_digest=character_media_contract_digest(
                        subject_ref=character.character_ref,
                        kind=kind,
                        source_events=sources,
                        allowed_capture_modes=modes,
                        allowed_character_visibility=visibility,
                    ),
                )
                candidate_id = "photo-candidate:character-media:" + media_digest({
                    "contract": "character-media-candidate.1",
                    "world_id": self._ledger.world_id,
                    "kind": kind,
                    "sources": [item.model_dump(mode="json") for item in sources],
                    "contract_digest": contract.authority_digest,
                })
                if candidate_id in existing:
                    continue
                discovered.append(PhotoCandidate(
                    candidate_id=candidate_id,
                    source_event_refs=tuple(item.event_ref for item in sources),
                    family="character_media",
                    privacy_ceiling=declaration.image_evidence.visibility,
                    opened_at=logical_time,
                    expires_at=logical_time + self._default_expiry,
                    ecology_category="character_media:" + kind,
                    ecology_observed_at=source_event.logical_time,
                    source_events=sources,
                    character_media_contract=contract,
                ))
        return tuple(sorted(discovered, key=lambda item: item.candidate_id))

    def _event_at(self, event_id: str, *, committed: dict[str, object]) -> WorldEvent | None:
        ref = committed.get(event_id)
        found = self._ledger.lookup_event_commit(event_id)
        if ref is None or found is None:
            return None
        event, _commit = found
        if event.world_id != self._ledger.world_id or event.payload_hash != ref.payload_hash:
            return None
        return event

    @staticmethod
    def _contracts(
        *, declaration: ImageEvidenceDeclaredPayload,
    ) -> tuple[tuple[CharacterMediaKind, tuple[CharacterCaptureMode, ...], tuple[CharacterVisibility, ...]], ...]:
        evidence = declaration.image_evidence
        character = evidence.character_media
        assert character is not None
        if evidence.visibility not in _PUBLIC_VISIBILITIES:
            return ()
        modes = set(character.capture_capabilities)
        values: list[tuple[CharacterMediaKind, tuple[CharacterCaptureMode, ...], tuple[CharacterVisibility, ...]]] = []
        if "character_front_camera" in modes:
            values.append(("selfie", ("character_front_camera",), ("identifiable",)))
        if "mirror" in modes and isinstance(evidence.location, dict) and evidence.location.get("mirror_available") is True:
            values.append(("mirror", ("mirror",), ("identifiable",)))
        if (
            {"timer_fixed", "requested_helper"}.intersection(modes)
            and isinstance(evidence.location, dict)
            and evidence.location.get("publicness") == "public"
        ):
            allowed = tuple(sorted({mode for mode in modes if mode in {"timer_fixed", "requested_helper"}}))
            values.append(("public_checkin", allowed, ("identifiable",)))
        if "known_companion" in modes and any(
            item.get("id") != character.character_ref
            and item.get("present") is True
            and item.get("visibility_permission") in _PUBLIC_VISIBILITIES
            for item in evidence.participants
            if isinstance(item, dict)
        ):
            values.append(("companion_shot", ("known_companion",), ("identifiable",)))
        detail = character.body_detail
        if detail is not None and any(
            item.get("id") == detail.object_ref and item.get("visibility") in _PUBLIC_VISIBILITIES
            for item in evidence.objects
            if isinstance(item, dict)
        ):
            allowed = tuple(sorted({mode for mode in modes if mode in {"character_front_camera", "character_rear_camera"}}))
            if allowed:
                values.append(("body_detail", allowed, ("body_detail",)))
        return tuple(values)


class CharacterMediaCandidateRuntime:
    """Open the binder's immutable candidates after one declaration wake.

    This is a mechanical lifecycle seam: it cannot choose a candidate, create
    an opportunity or authorize an image.  A later selector still has to make
    the social choice and Acceptance still owns budget/provider authority.
    """

    def __init__(self, *, ledger: _Ledger, binder: CharacterMediaFactBinder | None = None) -> None:
        self._ledger = ledger
        self._binder = binder or CharacterMediaFactBinder(ledger=ledger)

    def open_once(
        self, *, wake_event_ref: str, logical_time: datetime, actor: str, trace_id: str,
        correlation_id: str,
    ) -> tuple[str, ...]:
        projection = self._ledger.project()  # type: ignore[attr-defined]
        if projection.logical_time != logical_time:
            raise ValueError("character media candidate opening requires current logical time")
        wake = next(
            (item for item in projection.committed_world_event_refs if item.event_id == wake_event_ref), None
        )
        if wake is None or wake.event_type != "ImageEvidenceDeclared":
            raise ValueError("character media candidate opening requires an image evidence declaration wake")
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        candidates = self._binder.discover(cursor=cursor, logical_time=logical_time)
        if not candidates:
            return ()
        events: list[WorldEvent] = []
        for candidate in candidates:
            payload = PhotoCandidateOpenedPayload(candidate=candidate).model_dump(mode="json")
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:character-media-candidate:" + media_digest({
                    "candidate_id": candidate.candidate_id,
                    "payload": payload,
                }),
                event_type="PhotoCandidateOpened",
                world_id=self._ledger.world_id,
                logical_time=logical_time,
                created_at=logical_time,
                actor=actor,
                source="world-v2:character-media-candidate",
                trace_id=trace_id,
                causation_id=wake_event_ref,
                correlation_id=correlation_id,
                idempotency_key=domain_idempotency_key(
                    event_type="PhotoCandidateOpened", world_id=self._ledger.world_id, payload=payload,
                ) or "character-media-candidate:" + candidate.candidate_id,
                payload=payload,
            )
            events.append(event)
        self._ledger.commit_at_cursor(  # type: ignore[attr-defined]
            tuple(events), expected_cursor=cursor,
            commit_id="commit:character-media-candidates:" + media_digest([event.event_id for event in events]),
        )
        return tuple(candidate.candidate_id for candidate in candidates)


__all__ = [
    "CharacterMediaCandidateRuntime",
    "CharacterMediaFactBinder",
    "CharacterMediaFactBinderError",
]
