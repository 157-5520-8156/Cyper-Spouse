from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.media_evidence_snapshot import (
    MediaEvidenceCompileRequest,
    MediaEvidenceNotRenderable,
    MediaEvidenceSnapshotCompiler,
)
from companion_daemon.world_v2.media_v2 import (
    FrozenMediaEvidenceSnapshot,
    ImageEvidenceIndexEntry,
    ImageEventSnapshot,
    MediaEvidenceSource,
    PhotoCandidate,
)
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, ProjectionCursor, WorldEvent


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:image-evidence"


def _event(name: str, payload: dict[str, object], *, event_type: str = "ActivityCompleted") -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id="event:image-evidence:" + name, event_type=event_type,
        world_id=WORLD, logical_time=NOW, created_at=NOW, actor="companion:celia", source="test",
        trace_id="trace:image-evidence", causation_id="cause:image-evidence", correlation_id="correlation:image-evidence",
        idempotency_key="idempotency:image-evidence:" + name, payload=payload,
    )


class _Ledger:
    def __init__(self, *events: WorldEvent) -> None:
        self._events = {event.event_id: event for event in events}
        self.projection = SimpleNamespace(
            world_revision=len(events), deliberation_revision=0, ledger_sequence=len(events), logical_time=NOW,
            committed_world_event_refs=tuple(
                CommittedWorldEventRef(
                    event_id=event.event_id, event_type=event.event_type, world_revision=index,
                    payload_hash=event.payload_hash, logical_time=event.logical_time,
                ) for index, event in enumerate(events, start=1)
            ),
        )
        self.project_at_calls = 0

    def project_at(self, cursor: ProjectionCursor):  # type: ignore[no-untyped-def]
        self.project_at_calls += 1
        assert cursor == ProjectionCursor(
            world_revision=self.projection.world_revision, deliberation_revision=0,
            ledger_sequence=self.projection.ledger_sequence,
        )
        return self.projection

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        event = self._events.get(event_id)
        return None if event is None else (event, object())


def _request(candidate: PhotoCandidate, ledger: _Ledger) -> MediaEvidenceCompileRequest:
    return MediaEvidenceCompileRequest(
        candidate=candidate, category="activity_result",
        cursor=ProjectionCursor(
            world_revision=ledger.projection.world_revision, deliberation_revision=0,
            ledger_sequence=ledger.projection.ledger_sequence,
        ),
    )


def test_compiler_freezes_only_explicit_public_image_evidence_with_leaf_provenance() -> None:
    event = _event("rich", {
        "image_evidence": {
            "visibility": "shareable", "summary": "雨后散步", "outcome": "到了公园",
            "location": {"evidence_visibility": "public", "id": "location:park", "kind": "park", "city": "杭州"},
            "activity": {"evidence_visibility": "shareable", "id": "activity:walk", "kind": "walk", "description": "雨后散步", "phase": "completed"},
            "participants": [{"evidence_visibility": "public", "id": "companion:celia", "role": "character", "present": True, "visibility_permission": "shareable"}],
            "objects": [{"evidence_visibility": "shareable", "id": "object:umbrella", "kind": "umbrella", "description": "透明雨伞", "ownership": "character", "visibility": "shareable"}],
            "environment": {"evidence_visibility": "public", "weather": "雨后", "light": "阴天"},
        },
    })
    ledger = _Ledger(event)
    candidate = PhotoCandidate(candidate_id="candidate:rich", source_event_refs=(event.event_id,), family="life_share", privacy_ceiling="shareable")

    compiled = MediaEvidenceSnapshotCompiler(ledger=ledger).compile(_request(candidate, ledger))

    image = compiled.snapshot.image_event_snapshot
    assert image is not None
    assert image.schema_version == "world-image-event-snapshot-v1"
    assert image.activity["description"] == "雨后散步"
    assert image.location == {"id": "location:park", "kind": "park", "city": "杭州"}
    assert image.evidence_index["/activity/description"].source_event_ref == event.event_id
    assert image.evidence_index["/objects/0/description"].source_payload_hash == event.payload_hash
    assert compiled.snapshot_hash.startswith("sha256:")
    assert compiled.image_event_snapshot_hash.startswith("sha256:")
    assert compiled.image_event_snapshot_hash != compiled.snapshot_hash
    assert len(compiled.evidence_index_digest) == 64
    assert ledger.project_at_calls == 1


def test_compiler_never_resolves_value_refs_or_infers_a_visual_description() -> None:
    event = _event("opaque-fact", {
        "values": {"predicate_code": "meal.visible_food", "value_ref": "fact-value:noodles", "value_hash": "a" * 64},
    }, event_type="FactCommitted")
    ledger = _Ledger(event)
    candidate = PhotoCandidate(candidate_id="candidate:opaque", source_event_refs=(event.event_id,), family="life_share", privacy_ceiling="public")

    with pytest.raises(MediaEvidenceNotRenderable, match="no_visual_evidence"):
        MediaEvidenceSnapshotCompiler(ledger=ledger).compile(_request(candidate, ledger))


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"image_evidence": {"visibility": "private", "activity": {"evidence_visibility": "private", "id": "activity:x"}}}, "image_evidence_not_public_or_shareable"),
        ({"image_evidence": {"visibility": "public", "requires_readable_text": True}}, "readable_text_requires_artifact"),
        ({"image_evidence": {"visibility": "public", "existing_media": [{"evidence_visibility": "public", "artifact_ref": "artifact:x", "artifact_hash": "sha256:" + "a" * 64, "accessible": False, "reuse_authorized": True}]}}, "existing_media_requires_accessible_artifact"),
    ],
)
def test_compiler_fails_closed_for_private_readable_or_unavailable_media(payload: dict[str, object], reason: str) -> None:
    event = _event("closed-" + reason, payload)
    ledger = _Ledger(event)
    candidate = PhotoCandidate(candidate_id="candidate:" + reason, source_event_refs=(event.event_id,), family="life_share", privacy_ceiling="public")

    with pytest.raises(MediaEvidenceNotRenderable, match=reason):
        MediaEvidenceSnapshotCompiler(ledger=ledger).compile(_request(candidate, ledger))


def test_schema_rejects_missing_or_extra_leaf_provenance_and_outer_hash_rebinding() -> None:
    index = {"/event/event_id": ImageEvidenceIndexEntry(source_event_ref="event:one", source_payload_hash="a" * 64, visibility="public")}
    with pytest.raises(ValidationError, match="cover exactly"):
        ImageEventSnapshot(
            event={"event_id": "event:one"}, source={"channel": "direct_experience"}, location={}, activity={}, participants=(), objects=(), environment={},
            character={}, existing_media=(), visual_requirements={}, evidence_index=index,
        )

    complete = ImageEventSnapshot(
        event={"event_id": "event:one"}, source={}, location={}, activity={}, participants=(), objects=(), environment={},
        character={}, existing_media=(), visual_requirements={}, evidence_index=index,
    )
    with pytest.raises(ValidationError, match="outer snapshot source"):
        FrozenMediaEvidenceSnapshot(
            source_events=(MediaEvidenceSource(event_ref="event:one", payload_hash="b" * 64),),
            image_event_snapshot=complete,
        )


def test_replay_at_same_cursor_is_byte_stable_and_does_not_use_current_projection() -> None:
    event = _event("stable", {"image_evidence": {"visibility": "public", "environment": {"evidence_visibility": "public", "weather": "晴"}}})
    ledger = _Ledger(event)
    candidate = PhotoCandidate(candidate_id="candidate:stable", source_event_refs=(event.event_id,), family="life_share", privacy_ceiling="public")
    compiler = MediaEvidenceSnapshotCompiler(ledger=ledger)

    first = compiler.compile(_request(candidate, ledger))
    ledger.projection.mutable_later_state = {"weather": "暴雨", "location": "别处"}
    second = compiler.compile(_request(candidate, ledger))

    assert first.snapshot_body == second.snapshot_body
    assert first.snapshot_hash == second.snapshot_hash
    assert first.snapshot_ref == second.snapshot_ref
    assert first.image_event_snapshot_hash == second.image_event_snapshot_hash
