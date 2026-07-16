from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from companion_daemon.world_v2.character_media_fact_binder import CharacterMediaFactBinder
from companion_daemon.world_v2.image_evidence_contract import (
    CharacterMediaEvidenceV1,
    ImageEvidenceDeclaredPayload,
    ImageEvidenceV1,
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

    def project_at(self, cursor: ProjectionCursor):  # type: ignore[no-untyped-def]
        assert cursor.world_revision == self.projection.world_revision
        return self.projection

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        event = self._events.get(event_id)
        return None if event is None else (event, object())


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
