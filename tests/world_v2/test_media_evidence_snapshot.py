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
from companion_daemon.world_v2.image_evidence_contract import (
    ImageEvidenceDeclaredPayload,
    ImageEvidenceV1,
)
from companion_daemon.world_v2.media_v2 import (
    CharacterMediaCandidateContract,
    FrozenMediaEvidenceSnapshot,
    ImageEvidenceIndexEntry,
    ImageEventSnapshot,
    MediaEvidenceSource,
    PhotoCandidate,
    character_media_contract_digest,
)
from companion_daemon.world_v2.appearance_state import (
    AppearanceStateProjection,
    VisibleAppearanceAttribute,
)
from companion_daemon.world_v2.visible_physical_state import (
    VisiblePhysicalCue,
    VisiblePhysicalStateProjection,
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


def test_compiler_freezes_evidence_from_a_declaration_not_from_its_life_event_payload() -> None:
    source = _event("declared-source", {})
    declaration = _event(
        "declaration",
        ImageEvidenceDeclaredPayload(
            source_event_ref=source.event_id,
            source_event_payload_hash=source.payload_hash,
            source_event_type=source.event_type,
            source_privacy_ceiling="shareable",
            image_evidence=ImageEvidenceV1(
                visibility="shareable",
                activity={
                    "evidence_visibility": "shareable",
                    "id": "activity:walk",
                    "kind": "walk",
                    "description": "雨后散步",
                    "phase": "completed",
                },
            ),
            declared_at=NOW,
        ).model_dump(mode="json"),
        event_type="ImageEvidenceDeclared",
    )
    ledger = _Ledger(source, declaration)
    candidate = PhotoCandidate(
        candidate_id="candidate:declared",
        source_event_refs=tuple(sorted((source.event_id, declaration.event_id))),
        family="life_share",
        privacy_ceiling="shareable",
    )

    compiled = MediaEvidenceSnapshotCompiler(ledger=ledger).compile(_request(candidate, ledger))

    image = compiled.snapshot.image_event_snapshot
    assert image is not None
    assert image.event["event_id"] == source.event_id
    assert image.activity["description"] == "雨后散步"
    assert image.evidence_index["/activity/description"].source_event_ref == declaration.event_id


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


def test_character_snapshot_freezes_fact_bound_appearance_and_physical_state_at_the_primary_event_time() -> None:
    source = _event("character-source", {}, event_type="ActivityCompleted")
    declaration = _event(
        "character-declaration",
        ImageEvidenceDeclaredPayload(
            source_event_ref=source.event_id,
            source_event_payload_hash=source.payload_hash,
            source_event_type=source.event_type,
            source_privacy_ceiling="public",
            image_evidence=ImageEvidenceV1(
                visibility="public",
                activity={"evidence_visibility": "public", "id": "activity:walk", "kind": "walk"},
                character_media={
                    "character_ref": "agent:companion",
                    "present": True,
                        "capture_capabilities": ("character_front_camera",),
                },
            ),
            declared_at=NOW,
        ).model_dump(mode="json"),
        event_type="ImageEvidenceDeclared",
    )
    appearance_state = AppearanceStateProjection(
        appearance_state_id="appearance:agent:companion", subject_ref="agent:companion",
        entity_revision=1, source_event_ref=source.event_id, source_event_payload_hash=source.payload_hash,
        source_event_type=source.event_type, valid_from=NOW, visibility="public",
        visible_attributes=(VisibleAppearanceAttribute(aspect="outfit", description="深色运动外套"),),
    )
    appearance_record = _event(
        "appearance-record", {"state": appearance_state.model_dump(mode="json")},
        event_type="AppearanceStateRecorded",
    )
    physical_state = VisiblePhysicalStateProjection(
        physical_state_id="visible-physical:agent:companion", subject_ref="agent:companion",
        entity_revision=1, source_event_ref=source.event_id, source_event_payload_hash=source.payload_hash,
        source_event_type=source.event_type, valid_from=NOW, valid_until=NOW.replace(hour=13),
        visibility="public",
        positive_cues=(VisiblePhysicalCue(cue_id="perspiration", intensity="light", visible_regions=("hair",)),),
        negative_cues=(),
    )
    physical_record = _event(
        "physical-record", {"state": physical_state.model_dump(mode="json")},
        event_type="VisiblePhysicalStateRecorded",
    )
    ledger = _Ledger(source, declaration, appearance_record, physical_record)
    ledger.projection.appearance_states = (appearance_state,)
    ledger.projection.visible_physical_states = (physical_state,)
    source_events = tuple(sorted((
        MediaEvidenceSource(event_ref=source.event_id, payload_hash=source.payload_hash),
        MediaEvidenceSource(event_ref=declaration.event_id, payload_hash=declaration.payload_hash),
    ), key=lambda item: item.event_ref))
    contract = CharacterMediaCandidateContract(
        subject_ref="agent:companion", kind="selfie",
        allowed_capture_modes=("character_front_camera",),
        allowed_character_visibility=("identifiable",),
        authority_digest=character_media_contract_digest(
            subject_ref="agent:companion", kind="selfie", source_events=source_events,
            allowed_capture_modes=("character_front_camera",),
            allowed_character_visibility=("identifiable",),
        ),
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:character", source_event_refs=tuple(item.event_ref for item in source_events),
        family="character_media", privacy_ceiling="public", opened_at=NOW, expires_at=NOW.replace(hour=13),
        ecology_category="character_media:selfie", ecology_observed_at=NOW, source_events=source_events,
        character_media_contract=contract,
    )

    compiled = MediaEvidenceSnapshotCompiler(ledger=ledger).compile(_request(candidate, ledger))

    image = compiled.snapshot.image_event_snapshot
    assert image is not None
    assert image.schema_version == "world-image-event-snapshot-v2"
    assert image.character["subject_ref"] == "agent:companion"
    assert compiled.snapshot.character_media_authorization is not None
    assert compiled.snapshot.character_media_authorization.candidate_id == candidate.candidate_id
    assert compiled.snapshot.character_media_authorization.kind == "selfie"
    assert "capture_authorization" not in image.character
    assert "candidate_contract" not in image.character
    assert not any("authorization" in pointer or "contract" in pointer for pointer in image.evidence_index)
    assert image.character["appearance_state"]["visible_attributes"][0]["description"] == "深色运动外套"
    assert image.character["visible_physical_state"]["positive_cues"][0]["cue_id"] == "perspiration"
    assert image.evidence_index["/character/appearance_state/visible_attributes/0/description"].source_event_ref == appearance_record.event_id
    assert image.evidence_index["/character/visible_physical_state/positive_cues/0/cue_id"].source_event_ref == physical_record.event_id
    assert {item.event_ref for item in compiled.snapshot.source_events} == {
        source.event_id, declaration.event_id, appearance_record.event_id, physical_record.event_id,
    }
