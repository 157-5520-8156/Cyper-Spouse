from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from companion_daemon.world_v2.media_v2 import (
    CharacterMediaCandidateContract,
    MediaEvidenceSource,
    PhotoCandidate,
    character_media_contract_digest,
)
from companion_daemon.world_v2.private_image_evidence_contract import (
    RecipientScopedImageEvidenceDeclaredPayload,
    RecipientScopedImageEvidenceV1,
)
from companion_daemon.world_v2.private_media_evidence_snapshot import (
    PrivateMediaEvidenceCompileRequest,
    PrivateMediaEvidenceSnapshotCompiler,
)
from companion_daemon.world_v2.relationship_media_context import RelationshipMediaContextResolver
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    ProjectionCursor,
    RelationshipStateOrigin,
    RelationshipStateProjection,
    WorldEvent,
)
from companion_daemon.world_v2.visible_physical_state import (
    VisiblePhysicalCue,
    VisiblePhysicalStateProjection,
    VisiblePhysicalStateRecordedPayload,
)


NOW = datetime(2026, 7, 16, 21, tzinfo=UTC)
WORLD = "world:p3-private-snapshot"
SOURCE_ID = "event:activity:private-wind-down"
DECLARATION_ID = "event:recipient-evidence:private-wind-down"
PHYSICAL_RECORD_ID = "event:physical:private-wind-down"
RELATIONSHIP_ORIGIN_ID = "event:relationship:origin"


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, event_type=event_type, world_id=WORLD,
        logical_time=NOW, created_at=NOW, actor="worker:test", source="test:p3",
        trace_id="trace:p3", causation_id="cause:" + event_id, correlation_id="correlation:p3",
        idempotency_key="idempotency:" + event_id, payload=payload,
    )


class _Ledger:
    def __init__(self, projection, events: tuple[WorldEvent, ...]) -> None:
        self._projection = projection
        self._events = {event.event_id: event for event in events}

    def project_at(self, cursor: ProjectionCursor):
        return self._projection

    def lookup_event_commit(self, event_id: str):
        event = self._events.get(event_id)
        return (event, None) if event is not None else None


def test_private_compiler_freezes_recipient_context_and_keeps_authorization_outer() -> None:
    source = _event(SOURCE_ID, "ActivityCompleted", {"status": "committed"})
    evidence = RecipientScopedImageEvidenceDeclaredPayload(
        source_event_ref=SOURCE_ID, source_event_payload_hash=source.payload_hash,
        source_event_type="ActivityCompleted", source_privacy_ceiling="private", recipient_ref="user:1",
        image_evidence=RecipientScopedImageEvidenceV1(
            visibility="private", activity={"id": "activity:wind-down", "kind": "wind_down"},
            character_media={
                "character_ref": "character:ava", "present": True,
                "capture_capabilities": ("character_front_camera",),
            },
        ),
        declared_at=NOW,
    )
    declaration = _event(DECLARATION_ID, "RecipientScopedImageEvidenceDeclared", evidence.model_dump(mode="json"))
    physical = VisiblePhysicalStateProjection(
        physical_state_id="physical:wind-down", subject_ref="character:ava", entity_revision=1,
        source_event_ref=SOURCE_ID, source_event_payload_hash=source.payload_hash,
        source_event_type="ActivityCompleted", valid_from=NOW - timedelta(minutes=5),
        valid_until=NOW + timedelta(minutes=20), visibility="private",
        positive_cues=(VisiblePhysicalCue(cue_id="damp_hair", intensity="light", visible_regions=("hair",)),),
        negative_cues=(),
    )
    physical_record = _event(
        PHYSICAL_RECORD_ID, "VisiblePhysicalStateRecorded",
        VisiblePhysicalStateRecordedPayload(state=physical).model_dump(mode="json"),
    )
    relationship_origin = _event(RELATIONSHIP_ORIGIN_ID, "RelationshipSlowVariableAdjusted", {"opaque": "origin"})
    relationship = RelationshipStateProjection(
        relationship_id="relationship:user:1", subject_ref="user:1", entity_revision=2,
        stage="close_friend", policy_digest="c" * 64,
        origin=RelationshipStateOrigin(
            change_id="change:relationship:1", transition_id="transition:relationship:1",
            policy_refs=("policy:relationship",), accepted_event_ref=RELATIONSHIP_ORIGIN_ID,
        ),
    )
    context = RelationshipMediaContextResolver().resolve(
        projection=SimpleNamespace(relationship_states=(relationship,), visible_physical_states=(physical,)),
        character_ref="character:ava", recipient_ref="user:1", at_logical_time=NOW,
    ).context
    assert context is not None
    sources = tuple(
        MediaEvidenceSource(event_ref=event.event_id, payload_hash=event.payload_hash)
        for event in (source, declaration)
    )
    contract = CharacterMediaCandidateContract(
        subject_ref="character:ava", kind="selfie", allowed_capture_modes=("character_front_camera",),
        allowed_character_visibility=("identifiable",),
        authority_digest=character_media_contract_digest(
            subject_ref="character:ava", kind="selfie", source_events=sources,
            allowed_capture_modes=("character_front_camera",),
            allowed_character_visibility=("identifiable",),
        ),
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:private-selfie", source_event_refs=tuple(item.event_ref for item in sources),
        family="character_media", privacy_ceiling="private", opened_at=NOW,
        expires_at=NOW + timedelta(hours=1), ecology_category="character_media:private-selfie",
        ecology_observed_at=NOW, source_events=sources, opened_event_ref="event:candidate:private",
        opened_event_payload_hash="d" * 64, character_media_contract=contract,
    )
    events = (source, declaration, physical_record, relationship_origin)
    refs = tuple(
        CommittedWorldEventRef(
            event_id=event.event_id, event_type=event.event_type, world_revision=index + 1,
            payload_hash=event.payload_hash, logical_time=NOW,
        )
        for index, event in enumerate(events)
    )
    projection = SimpleNamespace(
        world_revision=4, deliberation_revision=0, ledger_sequence=4, logical_time=NOW,
        committed_world_event_refs=refs, relationship_states=(relationship,),
        visible_physical_states=(physical,), appearance_states=(),
    )
    compiled = PrivateMediaEvidenceSnapshotCompiler(ledger=_Ledger(projection, events)).compile(
        PrivateMediaEvidenceCompileRequest(
            candidate=candidate, category=candidate.ecology_category or "private", cursor=ProjectionCursor(
                world_revision=4, deliberation_revision=0, ledger_sequence=4,
            ), relationship_context=context, media_lane="exclusive_private", expression_charge_ceiling="subtle",
        )
    )

    snapshot = compiled.snapshot.image_event_snapshot
    assert snapshot is not None and snapshot.schema_version == "world-image-event-snapshot-v3"
    assert "private_media_authorization" not in snapshot.model_dump(mode="json")
    assert compiled.snapshot.private_media_authorization is not None
    assert compiled.snapshot.private_media_authorization.recipient_ref == "user:1"
    assert "/relationship_media_context/audience/relationship_stage" in snapshot.evidence_index
    assert "/character/visible_physical_state/positive_cues/0/cue_id" in snapshot.evidence_index, tuple(
        key for key in snapshot.evidence_index if "visible_physical" in key
    )
