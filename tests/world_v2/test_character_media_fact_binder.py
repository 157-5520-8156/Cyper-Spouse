from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from companion_daemon.world_v2.character_media_fact_binder import (
    CharacterMediaCandidateRuntime,
    CharacterMediaFactBinder,
)
from companion_daemon.world_v2.image_evidence_contract import (
    CharacterMediaEvidenceV1,
    ImageEvidenceDeclaredPayload,
    ImageEvidenceV1,
)
from companion_daemon.world_v2.private_image_evidence_contract import (
    RecipientScopedImageEvidenceDeclaredPayload,
    RecipientScopedImageEvidenceV1,
)
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, ProjectionCursor, WorldEvent


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:character-media-binder"


def _event(*, event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, event_type=event_type,
        world_id=WORLD, logical_time=NOW, created_at=NOW, actor="agent:companion",
        source="test", trace_id="trace:character-media", causation_id="cause:character-media",
        correlation_id="correlation:character-media", idempotency_key=event_id, payload=payload,
    )


class _Ledger:
    world_id = WORLD

    def __init__(self, *events: WorldEvent) -> None:
        self._events = {event.event_id: event for event in events}
        self.projection = SimpleNamespace(
            world_revision=len(events), deliberation_revision=0, ledger_sequence=len(events), logical_time=NOW,
            photo_candidates=(),
            committed_world_event_refs=tuple(
                CommittedWorldEventRef(
                    event_id=event.event_id, event_type=event.event_type, world_revision=index,
                    payload_hash=event.payload_hash, logical_time=event.logical_time,
                ) for index, event in enumerate(events, start=1)
            ),
        )
        self.commits: list[tuple[tuple[WorldEvent, ...], ProjectionCursor, str]] = []

    def project(self):  # type: ignore[no-untyped-def]
        return self.projection

    def project_at(self, cursor: ProjectionCursor):  # type: ignore[no-untyped-def]
        assert cursor.world_revision == self.projection.world_revision
        return self.projection

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        event = self._events.get(event_id)
        return None if event is None else (event, object())

    def commit_at_cursor(self, events, *, expected_cursor, commit_id):  # type: ignore[no-untyped-def]
        self.commits.append((events, expected_cursor, commit_id))


def _cursor(ledger: _Ledger) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=ledger.projection.world_revision,
        deliberation_revision=0,
        ledger_sequence=ledger.projection.ledger_sequence,
    )


def test_binder_discovers_only_a_fact_proven_mirror_candidate() -> None:
    source = _event(event_id="event:activity", event_type="ActivityCompleted", payload={})
    declaration = _event(
        event_id="event:declaration", event_type="ImageEvidenceDeclared",
        payload=ImageEvidenceDeclaredPayload(
            source_event_ref=source.event_id, source_event_payload_hash=source.payload_hash,
            source_event_type=source.event_type, source_privacy_ceiling="public", declared_at=NOW,
            image_evidence=ImageEvidenceV1(
                visibility="public", location={"id": "location:home", "mirror_available": True},
                character_media=CharacterMediaEvidenceV1(
                    character_ref="agent:companion", present=True, capture_capabilities=("mirror",),
                ),
            ),
        ).model_dump(mode="json"),
    )
    ledger = _Ledger(source, declaration)

    candidates = CharacterMediaFactBinder(ledger=ledger).discover(cursor=_cursor(ledger), logical_time=NOW)

    assert len(candidates) == 1
    assert candidates[0].family == "character_media"
    assert candidates[0].character_media_contract is not None
    assert candidates[0].character_media_contract.kind == "mirror"
    assert candidates[0].character_media_contract.allowed_capture_modes == ("mirror",)


def test_binder_refuses_to_infer_a_mirror_from_character_presence_alone() -> None:
    source = _event(event_id="event:activity", event_type="ActivityCompleted", payload={})
    declaration = _event(
        event_id="event:declaration", event_type="ImageEvidenceDeclared",
        payload=ImageEvidenceDeclaredPayload(
            source_event_ref=source.event_id, source_event_payload_hash=source.payload_hash,
            source_event_type=source.event_type, source_privacy_ceiling="public", declared_at=NOW,
            image_evidence=ImageEvidenceV1(
                visibility="public", activity={"id": "activity:home"},
                character_media=CharacterMediaEvidenceV1(
                    character_ref="agent:companion", present=True, capture_capabilities=("mirror",),
                ),
            ),
        ).model_dump(mode="json"),
    )
    ledger = _Ledger(source, declaration)

    assert CharacterMediaFactBinder(ledger=ledger).discover(cursor=_cursor(ledger), logical_time=NOW) == ()


def test_recipient_scoped_personal_never_opens_a_p3_candidate_but_private_still_does() -> None:
    """Candidate discovery mirrors P3's private-only authorization boundary."""

    personal_source = _event(event_id="event:personal-activity", event_type="ActivityCompleted", payload={})
    private_source = _event(event_id="event:private-activity", event_type="ActivityCompleted", payload={})

    def declaration(*, event_id: str, source: WorldEvent, visibility: str) -> WorldEvent:
        return _event(
            event_id=event_id,
            event_type="RecipientScopedImageEvidenceDeclared",
            payload=RecipientScopedImageEvidenceDeclaredPayload(
                source_event_ref=source.event_id,
                source_event_payload_hash=source.payload_hash,
                source_event_type=source.event_type,
                source_privacy_ceiling=visibility,
                recipient_ref="user:recipient",
                image_evidence=RecipientScopedImageEvidenceV1(
                    visibility=visibility,
                    activity={"id": "activity:wind-down", "kind": "wind_down"},
                    character_media=CharacterMediaEvidenceV1(
                        character_ref="agent:companion",
                        present=True,
                        capture_capabilities=("character_front_camera",),
                    ),
                ),
                declared_at=NOW,
            ).model_dump(mode="json"),
        )

    personal = declaration(
        event_id="event:personal-declaration", source=personal_source, visibility="personal"
    )
    private = declaration(
        event_id="event:private-declaration", source=private_source, visibility="private"
    )
    ledger = _Ledger(personal_source, personal, private_source, private)

    candidates = CharacterMediaFactBinder(ledger=ledger).discover(cursor=_cursor(ledger), logical_time=NOW)

    assert len(candidates) == 1
    assert candidates[0].privacy_ceiling == "private"
    assert candidates[0].source_event_refs == tuple(sorted((private_source.event_id, private.event_id)))
    assert personal.event_id not in candidates[0].source_event_refs


def test_candidate_runtime_opens_only_the_binder_discovered_candidate_after_its_declaration_wake() -> None:
    source = _event(event_id="event:activity", event_type="ActivityCompleted", payload={})
    declaration = _event(
        event_id="event:declaration", event_type="ImageEvidenceDeclared",
        payload=ImageEvidenceDeclaredPayload(
            source_event_ref=source.event_id, source_event_payload_hash=source.payload_hash,
            source_event_type=source.event_type, source_privacy_ceiling="public", declared_at=NOW,
            image_evidence=ImageEvidenceV1(
                visibility="public", activity={"id": "activity:walk"},
                character_media=CharacterMediaEvidenceV1(
                    character_ref="agent:companion", present=True,
                    capture_capabilities=("character_front_camera",),
                ),
            ),
        ).model_dump(mode="json"),
    )
    ledger = _Ledger(source, declaration)

    opened = CharacterMediaCandidateRuntime(ledger=ledger).open_once(
        wake_event_ref=declaration.event_id, logical_time=NOW, actor="worker:character-media",
        trace_id="trace:character-media", correlation_id="correlation:character-media",
    )

    assert len(opened) == 1
    event = ledger.commits[0][0][0]
    assert event.event_type == "PhotoCandidateOpened"
    assert event.causation_id == declaration.event_id
    assert event.payload()["candidate"]["character_media_contract"]["kind"] == "selfie"
