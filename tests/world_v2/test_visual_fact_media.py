"""Replay tests for the object/food visual-fact sidecar lane."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.event_ecology_media import (
    EcologyPolicy,
    EventEcologyMediaCandidateRuntime,
)
from companion_daemon.world_v2.media_evidence_snapshot import (
    MediaEvidenceCompileRequest,
    MediaEvidenceNotRenderable,
    MediaEvidenceSnapshotCompiler,
)
from companion_daemon.world_v2.media_v2 import (
    InMemoryImmutableMediaPayloadStore,
    MediaEvidenceSource,
    MediaOpportunity,
    PhotoCandidate,
)
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, ProjectionCursor, WorldEvent
from companion_daemon.world_v2.visual_fact import (
    VisualFactContentV1,
    VisualFactRecordCommand,
    VisualFactRuntime,
    VisualObjectEvidenceV1,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:visual-fact-media"


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, event_type=event_type,
        world_id=WORLD, logical_time=NOW, created_at=NOW, actor="test:visual-fact",
        source="test:visual-fact", trace_id="trace:visual-fact", causation_id="cause:visual-fact",
        correlation_id="correlation:visual-fact", idempotency_key="identity:" + event_id,
        payload=payload,
    )


def _ref(event: WorldEvent, revision: int) -> CommittedWorldEventRef:
    return CommittedWorldEventRef(
        event_id=event.event_id, event_type=event.event_type, world_revision=revision,
        payload_hash=event.payload_hash, logical_time=event.logical_time,
    )


class _Ledger:
    world_id = WORLD

    def __init__(self, source: WorldEvent) -> None:
        self.events = {source.event_id: source}
        self.projection = SimpleNamespace(
            world_revision=1, deliberation_revision=0, ledger_sequence=1, logical_time=NOW,
            committed_world_event_refs=(_ref(source, 1),),
            plans=(SimpleNamespace(
                authority_origin=SimpleNamespace(accepted_event_ref=source.event_id),
                privacy_class="shareable",
            ),),
            world_occurrences=(), experiences=(), facts=(), npcs=(),
            photo_candidates=(), media_opportunities=(),
        )

    def project(self):
        return self.projection

    def project_at(self, cursor: ProjectionCursor):
        assert cursor == ProjectionCursor(world_revision=self.projection.world_revision, deliberation_revision=0, ledger_sequence=self.projection.ledger_sequence)
        return self.projection

    def lookup_event_commit(self, event_id: str):
        event = self.events.get(event_id)
        return None if event is None else (event, object())

    def commit_at_cursor(self, events, *, expected_cursor, commit_id):  # type: ignore[no-untyped-def]
        assert expected_cursor == ProjectionCursor(
            world_revision=self.projection.world_revision,
            deliberation_revision=self.projection.deliberation_revision,
            ledger_sequence=self.projection.ledger_sequence,
        )
        candidates = list(self.projection.photo_candidates)
        opportunities = list(self.projection.media_opportunities)
        for event in events:
            self.events[event.event_id] = event
            revision = self.projection.world_revision + 1
            self.projection.committed_world_event_refs += (_ref(event, revision),)
            self.projection.world_revision = revision
            self.projection.ledger_sequence += 1
            if event.event_type == "PhotoCandidateOpened":
                candidates.append(PhotoCandidate.model_validate_json(json.dumps(event.payload()["candidate"])))
            elif event.event_type == "MediaOpportunityFrozen":
                opportunities.append(MediaOpportunity.model_validate_json(json.dumps(event.payload()["opportunity"])))
        self.projection.photo_candidates = tuple(candidates)
        self.projection.media_opportunities = tuple(opportunities)
        return SimpleNamespace(events=tuple(events))


def test_object_food_sidecar_is_source_bound_and_replayable_for_ecology() -> None:
    source = _event("event:activity-completed", "ActivityCompleted", {"plan_id": "plan:brunch"})
    ledger = _Ledger(source)
    sidecar = InMemoryImmutableMediaPayloadStore()
    recorded = VisualFactRuntime(ledger=ledger, sidecar=sidecar).record(
        VisualFactRecordCommand(
            command_id="command:brunch-noodles",
            source_event_ref=source.event_id,
            content_ref="sidecar:visual-fact:brunch-noodles",
            content=VisualFactContentV1(
                facet="meal.visible_food", subject_ref="activity:brunch", visibility="shareable",
                objects=(VisualObjectEvidenceV1(
                    id="object:noodles", kind="food", description="一碗番茄鸡蛋面",
                    ownership="character", visibility="shareable",
                ),),
            ),
        ),
        logical_time=NOW, created_at=NOW, actor="worker:visual-fact",
        trace_id="trace:visual-fact", correlation_id="correlation:visual-fact",
    )
    descriptor = recorded.events[0]
    # The ledger descriptor proves identity and source, but deliberately does
    # not duplicate planner-readable prose.  The sidecar is the only reader.
    assert "一碗番茄鸡蛋面" not in descriptor.payload_json
    assert sidecar.read_exact(payload_ref="sidecar:visual-fact:brunch-noodles") is not None

    ecology = EventEcologyMediaCandidateRuntime(
        ledger=ledger,
        sidecar=sidecar,
        policy=EcologyPolicy(max_candidates_per_drain=1, max_opportunities_per_day=3, direct_preview_compatibility=True),
        compiler=MediaEvidenceSnapshotCompiler(ledger=ledger, visual_fact_sidecar=sidecar),
    )
    result = ecology.drain_once(
        wake_event_ref=descriptor.event_id, logical_time=NOW, actor="worker:event-ecology",
        trace_id="trace:visual-fact", correlation_id="correlation:visual-fact",
    )

    assert result.status == "created"
    candidate = ledger.projection.photo_candidates[0]
    assert candidate.ecology_category == "object_or_food"
    assert candidate.source_event_refs == (descriptor.event_id,)
    opportunity = ledger.projection.media_opportunities[0]
    snapshot_record = sidecar.read_exact(payload_ref=opportunity.event_snapshot_ref)
    assert snapshot_record is not None
    image = json.loads(snapshot_record.body)["image_event_snapshot"]
    assert image["objects"] == [{
        "description": "一碗番茄鸡蛋面", "id": "object:noodles", "kind": "food",
        "ownership": "character", "visibility": "shareable",
    }]
    assert image["evidence_index"]["/objects/0/description"]["source_event_ref"] == descriptor.event_id
    # Replaying after the candidate is committed opens neither a duplicate
    # candidate nor a different snapshot.
    assert ecology.drain_once(
        wake_event_ref=descriptor.event_id, logical_time=NOW, actor="worker:event-ecology",
        trace_id="trace:visual-fact", correlation_id="correlation:visual-fact",
    ).status == "idle"


def test_object_food_descriptor_never_degrades_to_a_fact_or_prompt_guess() -> None:
    source = _event("event:activity-completed", "ActivityCompleted", {"plan_id": "plan:brunch"})
    ledger = _Ledger(source)
    sidecar = InMemoryImmutableMediaPayloadStore()
    descriptor = VisualFactRuntime(ledger=ledger, sidecar=sidecar).record(
        VisualFactRecordCommand(
            command_id="command:tea", source_event_ref=source.event_id,
            content_ref="sidecar:visual-fact:tea",
            content=VisualFactContentV1(
                facet="meal.visible_drink", subject_ref="activity:tea", visibility="shareable",
                objects=(VisualObjectEvidenceV1(
                    id="object:tea", kind="drink", description="一杯热乌龙茶", visibility="shareable",
                ),),
            ),
        ), logical_time=NOW, created_at=NOW, actor="worker:visual-fact",
        trace_id="trace:visual-fact", correlation_id="correlation:visual-fact",
    ).events[0]
    candidate = PhotoCandidate(
        candidate_id="photo-candidate:tea", source_event_refs=(descriptor.event_id,), family="life_share",
        privacy_ceiling="shareable", opened_at=NOW, expires_at=NOW.replace(hour=13),
        ecology_category="object_or_food", ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(
            event_ref=descriptor.event_id, payload_hash=descriptor.payload_hash,
        ),),
    )
    # A descriptor hash without the exact sidecar body is intentionally not
    # enough to tell the image machine what the drink looks like.
    with pytest.raises(MediaEvidenceNotRenderable, match="visual_fact_content_missing"):
        MediaEvidenceSnapshotCompiler(
            ledger=ledger, visual_fact_sidecar=InMemoryImmutableMediaPayloadStore(),
        ).compile(MediaEvidenceCompileRequest(
            candidate=candidate, category="object_or_food",
            cursor=ProjectionCursor(world_revision=2, deliberation_revision=0, ledger_sequence=2),
        ))
