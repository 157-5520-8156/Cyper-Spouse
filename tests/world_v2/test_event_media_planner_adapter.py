from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace
import hashlib

import pytest

from companion_daemon import event_media
from companion_daemon.world_v2.event_media_planner_adapter import EventMediaPlannerAdapter
from companion_daemon.world_v2.media_v2 import (
    CharacterMediaCandidateContract,
    CharacterMediaSnapshotAuthorization,
    FrozenMediaEvidenceSnapshot,
    ImageEventSnapshotV2,
    InMemoryImmutableMediaPayloadStore,
    MediaEvidenceSource,
    MediaOpportunity,
    MediaPlanningResult,
    PhotoCandidate,
    StoredMediaPayload,
    canonical_media_json,
    media_payload_hash,
    planning_request_id,
    character_media_contract_digest,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
SOURCE_REF = "event:activity-completed:bridge"
SOURCE_HASH = "a" * 64


class _ResultStore:
    """Test double for the separately composed durable receipt seam."""

    def __init__(self) -> None:
        self.values: dict[str, MediaPlanningResult] = {}

    async def lookup(self, *, planning_request_id: str) -> MediaPlanningResult | None:
        return self.values.get(planning_request_id)

    async def put_if_absent(
        self, *, planning_request_id: str, result: MediaPlanningResult
    ) -> None:
        current = self.values.setdefault(planning_request_id, result)
        if current != result:
            raise ValueError("planning result key already has a different terminal result")


class _LegacyPlanner:
    def __init__(self, result: event_media.PlanningResult) -> None:
        self.result = result
        self.calls: list[event_media.MediaOpportunity] = []

    async def plan(
        self, opportunity: event_media.MediaOpportunity, recent_media=()
    ) -> event_media.PlanningResult:
        assert recent_media == ()
        self.calls.append(opportunity)
        return self.result


def _image_snapshot() -> dict[str, object]:
    return {
        "schema_version": "world-image-event-snapshot-v1",
        "event": {
            "event_id": SOURCE_REF,
            "type": "activity_completed",
            "status": "committed",
            "logical_at": NOW.isoformat(),
            "summary": "雨后的校园散步",
            "outcome": "已回到宿舍",
        },
        "source": {"channel": "direct_experience", "person": "character"},
        "location": {"kind": "campus", "city": "Shanghai"},
        "activity": {"kind": "walk", "description": "雨后散步", "phase": "completed"},
        "participants": [],
        "objects": [],
        "environment": {"weather": "rain_cleared"},
        "character": {},
        "existing_media": [],
        "visual_requirements": {"requires_readable_text": False},
        "relationship_media_context": None,
        "evidence_index": {
            "/event/event_id": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/event/type": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/event/status": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/event/logical_at": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/event/summary": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/event/outcome": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/source/channel": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/source/person": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/location/kind": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/location/city": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/activity/kind": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/activity/description": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/activity/phase": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/environment/weather": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
            "/visual_requirements/requires_readable_text": {
                "source_event_ref": SOURCE_REF,
                "source_payload_hash": SOURCE_HASH,
                "visibility": "shareable",
            },
        },
    }


def _sidecar(*, snapshot: dict[str, object] | None = None) -> tuple[InMemoryImmutableMediaPayloadStore, MediaOpportunity]:
    snapshot = _image_snapshot() if snapshot is None else snapshot
    body_data = FrozenMediaEvidenceSnapshot(
        source_events=(MediaEvidenceSource(event_ref=SOURCE_REF, payload_hash=SOURCE_HASH),)
    ).model_dump(mode="json") | {"image_event_snapshot": snapshot}
    body = canonical_media_json(body_data)
    opportunity = MediaOpportunity(
        opportunity_id="opportunity:bridge",
        candidate_id="candidate:bridge",
        family="life_share",
        delivery_mode="preview",
        privacy_ceiling="shareable",
        event_snapshot_ref="sidecar:bridge:snapshot",
        event_snapshot_hash=media_payload_hash(body),
        source_event_refs=(SOURCE_REF,),
        catalog_version="world-image-event-snapshot-v1",
        expires_at=NOW + timedelta(hours=1),
    )
    sidecar = InMemoryImmutableMediaPayloadStore()
    sidecar.put_if_absent(
        StoredMediaPayload(
            payload_ref=opportunity.event_snapshot_ref,
            payload_hash=opportunity.event_snapshot_hash,
            content_type="application/vnd.world-v2.media-opportunity+json",
            body=body,
        )
    )
    return sidecar, opportunity


def _legacy_plan(*, opportunity: MediaOpportunity, snapshot: dict[str, object]) -> event_media.MediaPlan:
    return event_media.MediaPlan(
        version="event-media-plan-v5",
        plan_id="event-plan:bridge",
        opportunity_id=opportunity.opportunity_id,
        event_id=SOURCE_REF,
        snapshot_hash=hashlib.sha256(canonical_media_json(snapshot).encode()).hexdigest(),
        delivery_mode="preview",
        family="life_share",
        content_domain="place_environment",
        visual_form="scene",
        share_intent="ambient_share",
        capture_mode="character_rear_camera",
        character_visibility="absent",
        other_people_visibility="none",
        polish="casual",
        tone="quiet",
        privacy="ordinary",
        primary_evidence_ref="/activity/description",
        supporting_evidence_refs=(),
        evidence_values={"/activity/description": "雨后散步"},
        composition="校园小路",
        action="记录路面雨水",
        camera_direction="normal eye level",
        sharing_motive="分享生活",
        constraints=(),
        route="generate",
        diversity_fingerprint="bridge-test",
        planned_summary="雨后校园",
    )


def _character_sidecar() -> tuple[InMemoryImmutableMediaPayloadStore, MediaOpportunity, dict[str, object]]:
    declaration = MediaEvidenceSource(event_ref="event:declaration:bridge", payload_hash="b" * 64)
    activity = MediaEvidenceSource(event_ref=SOURCE_REF, payload_hash=SOURCE_HASH)
    contract = CharacterMediaCandidateContract(
        subject_ref="agent:companion", kind="selfie",
        allowed_capture_modes=("character_front_camera",),
        allowed_character_visibility=("identifiable",),
        authority_digest=character_media_contract_digest(
            subject_ref="agent:companion", kind="selfie", source_events=(activity, declaration),
            allowed_capture_modes=("character_front_camera",),
            allowed_character_visibility=("identifiable",),
        ),
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:character-bridge", source_event_refs=(SOURCE_REF, declaration.event_ref),
        family="character_media", privacy_ceiling="shareable", opened_at=NOW,
        expires_at=NOW + timedelta(hours=1), ecology_category="character_media:selfie",
        ecology_observed_at=NOW, source_events=(activity, declaration),
        opened_event_ref="event:candidate:character-bridge", opened_event_payload_hash="c" * 64,
        character_media_contract=contract,
    )
    authorization = CharacterMediaSnapshotAuthorization(
        candidate_id=candidate.candidate_id, candidate_revision=candidate.entity_revision,
        subject_ref=contract.subject_ref, kind=contract.kind,
        allowed_capture_modes=contract.allowed_capture_modes,
        allowed_character_visibility=contract.allowed_character_visibility,
        authority_digest=contract.authority_digest, source_event_refs=candidate.source_event_refs,
    )
    snapshot = _image_snapshot() | {
        "schema_version": "world-image-event-snapshot-v2",
        "character": {"subject_ref": "agent:companion", "presence": {"present": True}},
        "participants": (), "objects": (), "existing_media": (),
    }
    snapshot["evidence_index"] = snapshot["evidence_index"] | {
        "/character/subject_ref": {
            "source_event_ref": declaration.event_ref,
            "source_payload_hash": declaration.payload_hash,
            "visibility": "shareable",
        },
        "/character/presence/present": {
            "source_event_ref": declaration.event_ref,
            "source_payload_hash": declaration.payload_hash,
            "visibility": "shareable",
        },
    }
    frozen = FrozenMediaEvidenceSnapshot(
        source_events=(activity, declaration), complete_candidate=candidate.model_dump(mode="json"),
        image_event_snapshot=ImageEventSnapshotV2.model_validate(snapshot),
        character_media_authorization=authorization,
    )
    body = canonical_media_json(frozen.model_dump(mode="json"))
    opportunity = MediaOpportunity(
        opportunity_id="opportunity:character-bridge", candidate_id=candidate.candidate_id,
        family="character_media", delivery_mode="preview", privacy_ceiling="shareable",
        event_snapshot_ref="sidecar:character-bridge:snapshot", event_snapshot_hash=media_payload_hash(body),
        source_event_refs=(SOURCE_REF, declaration.event_ref),
        candidate_source_event_refs=candidate.source_event_refs,
        snapshot_source_events=(activity, declaration), catalog_version="world-image-event-snapshot-v2",
        expires_at=NOW + timedelta(hours=1),
    )
    sidecar = InMemoryImmutableMediaPayloadStore()
    sidecar.put_if_absent(StoredMediaPayload(
        payload_ref=opportunity.event_snapshot_ref, payload_hash=opportunity.event_snapshot_hash,
        content_type="application/vnd.world-v2.media-opportunity+json", body=body,
    ))
    return sidecar, opportunity, snapshot


@pytest.mark.asyncio
async def test_bridge_maps_only_frozen_public_life_preview_and_preserves_both_hashes() -> None:
    sidecar, opportunity = _sidecar()
    snapshot = _image_snapshot()
    legacy = _LegacyPlanner(event_media.PlannedMedia(_legacy_plan(opportunity=opportunity, snapshot=snapshot)))
    adapter = EventMediaPlannerAdapter(
        sidecar=sidecar, legacy_planner=legacy, result_store=_ResultStore()
    )

    result = await adapter.plan(
        opportunity=opportunity, planning_request_id=planning_request_id(opportunity.opportunity_id)
    )

    assert result.not_renderable is None
    assert result.plan is not None
    assert result.plan.event_snapshot_hash == opportunity.event_snapshot_hash
    assert result.plan_payload is not None
    assert legacy.calls == [
        event_media.MediaOpportunity(
            opportunity_id=opportunity.opportunity_id,
            family="life_share",
            privacy_ceiling="ordinary",
            event_snapshot=snapshot,
            delivery_mode="preview",
            expression_requirements=(),
            audience_context=None,
            expression_charge_ceiling="none",
            private_expression_basis=None,
            allowed_evidence_refs=tuple(sorted(_image_snapshot()["evidence_index"])),
        )
    ]


@pytest.mark.asyncio
async def test_bridge_admits_only_the_outer_authorized_ordinary_character_preview() -> None:
    sidecar, opportunity, snapshot = _character_sidecar()
    legacy_plan = replace(
        _legacy_plan(opportunity=opportunity, snapshot=snapshot), family="character_media",
        capture_mode="character_front_camera", character_visibility="identifiable",
    )
    legacy = _LegacyPlanner(event_media.PlannedMedia(legacy_plan))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy, result_store=_ResultStore())

    result = await adapter.plan(
        opportunity=opportunity, planning_request_id=planning_request_id(opportunity.opportunity_id)
    )

    assert result.plan is not None
    assert result.plan.family == "character_media"
    assert len(legacy.calls) == 1
    planner_snapshot = legacy.calls[0].event_snapshot
    assert "character_media_authorization" not in planner_snapshot
    assert "capture_authorization" not in planner_snapshot["character"]
    assert "candidate_contract" not in planner_snapshot["character"]
    assert result.plan_payload is not None
    assert result.plan_payload.content_type == "application/vnd.world-v2.media-plan+json"
    assert result.plan_payload.body == canonical_media_json(legacy_plan.to_payload())


@pytest.mark.asyncio
async def test_bridge_rejects_a_character_plan_that_exceeds_frozen_capture_authority() -> None:
    sidecar, opportunity, snapshot = _character_sidecar()
    legacy_plan = replace(
        _legacy_plan(opportunity=opportunity, snapshot=snapshot), family="character_media",
        capture_mode="character_rear_camera", character_visibility="identifiable",
    )
    legacy = _LegacyPlanner(event_media.PlannedMedia(legacy_plan))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy, result_store=_ResultStore())

    result = await adapter.plan(
        opportunity=opportunity, planning_request_id=planning_request_id(opportunity.opportunity_id)
    )

    assert result.not_renderable is not None
    assert result.not_renderable.reason_code == "p2_legacy_plan_exceeds_authorization"


@pytest.mark.asyncio
async def test_bridge_uses_durable_lookup_before_calling_legacy_planner() -> None:
    sidecar, opportunity = _sidecar()
    snapshot = _image_snapshot()
    store = _ResultStore()
    legacy = _LegacyPlanner(event_media.PlannedMedia(_legacy_plan(opportunity=opportunity, snapshot=snapshot)))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy, result_store=store)
    request = planning_request_id(opportunity.opportunity_id)

    first = await adapter.plan(opportunity=opportunity, planning_request_id=request)
    second = await adapter.plan(opportunity=opportunity, planning_request_id=request)

    assert second == first
    assert len(legacy.calls) == 1
    assert await adapter.lookup(planning_request_id=request) == first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("family", "character_media"),
        ("delivery_mode", "automatic"),
        ("privacy_ceiling", "personal"),
        ("privacy_ceiling", "private"),
    ),
)
async def test_bridge_rejects_non_p0_opportunity_before_legacy_planning(field: str, value: str) -> None:
    sidecar, opportunity = _sidecar()
    legacy = _LegacyPlanner(event_media.NotRenderable(opportunity.opportunity_id, "should_not_run"))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy, result_store=_ResultStore())
    result = await adapter.plan(
        opportunity=opportunity.model_copy(update={field: value}),
        planning_request_id=planning_request_id(opportunity.opportunity_id),
    )

    assert result.not_renderable is not None
    assert result.not_renderable.reason_code == "p0_opportunity_not_authorized"
    assert legacy.calls == []


@pytest.mark.asyncio
async def test_bridge_rejects_unindexed_frozen_evidence_without_reading_a_projection() -> None:
    snapshot = _image_snapshot()
    del snapshot["evidence_index"]["/activity/description"]  # type: ignore[index]
    sidecar, opportunity = _sidecar(snapshot=snapshot)
    legacy = _LegacyPlanner(event_media.NotRenderable(opportunity.opportunity_id, "should_not_run"))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy, result_store=_ResultStore())

    result = await adapter.plan(
        opportunity=opportunity, planning_request_id=planning_request_id(opportunity.opportunity_id)
    )

    assert result.not_renderable is not None
    assert result.not_renderable.reason_code == "malformed_image_event_snapshot"
    assert legacy.calls == []


@pytest.mark.asyncio
async def test_bridge_rejects_evidence_index_hash_not_bound_to_outer_sidecar_source() -> None:
    snapshot = _image_snapshot()
    snapshot["evidence_index"]["/activity/description"]["source_payload_hash"] = "b" * 64  # type: ignore[index]
    sidecar, opportunity = _sidecar(snapshot=snapshot)
    legacy = _LegacyPlanner(event_media.NotRenderable(opportunity.opportunity_id, "should_not_run"))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy, result_store=_ResultStore())

    result = await adapter.plan(
        opportunity=opportunity, planning_request_id=planning_request_id(opportunity.opportunity_id)
    )

    assert result.not_renderable is not None
    assert result.not_renderable.reason_code == "malformed_image_event_snapshot"
    assert legacy.calls == []


@pytest.mark.asyncio
async def test_bridge_fails_closed_for_existing_media_without_a_verified_artifact_lookup() -> None:
    snapshot = _image_snapshot()
    snapshot["existing_media"] = [{
        "artifact_ref": "artifact:source-photo",
        "artifact_hash": "sha256:" + "b" * 64,
        "accessible": True,
        "reuse_authorized": True,
    }]
    for key in ("artifact_ref", "artifact_hash", "accessible", "reuse_authorized"):
        snapshot["evidence_index"][f"/existing_media/0/{key}"] = {  # type: ignore[index]
            "source_event_ref": SOURCE_REF,
            "source_payload_hash": SOURCE_HASH,
            "visibility": "shareable",
        }
    sidecar, opportunity = _sidecar(snapshot=snapshot)
    legacy = _LegacyPlanner(event_media.NotRenderable(opportunity.opportunity_id, "should_not_run"))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy, result_store=_ResultStore())

    result = await adapter.plan(
        opportunity=opportunity, planning_request_id=planning_request_id(opportunity.opportunity_id)
    )

    assert result.not_renderable is not None
    assert result.not_renderable.reason_code == "existing_media_lookup_unavailable"
    assert legacy.calls == []


@pytest.mark.asyncio
async def test_bridge_refuses_a_non_deterministic_request_id_before_reading_sidecar() -> None:
    sidecar, opportunity = _sidecar()
    legacy = _LegacyPlanner(event_media.NotRenderable(opportunity.opportunity_id, "should_not_run"))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy, result_store=_ResultStore())

    result = await adapter.plan(opportunity=opportunity, planning_request_id="caller-made-request-id")

    assert result.not_renderable is not None
    assert result.not_renderable.reason_code == "planning_request_id_mismatch"
    assert legacy.calls == []


@pytest.mark.asyncio
async def test_bridge_is_unavailable_without_a_durable_result_store() -> None:
    sidecar, opportunity = _sidecar()
    legacy = _LegacyPlanner(event_media.NotRenderable(opportunity.opportunity_id, "should_not_run"))
    adapter = EventMediaPlannerAdapter(sidecar=sidecar, legacy_planner=legacy)

    result = await adapter.plan(
        opportunity=opportunity, planning_request_id=planning_request_id(opportunity.opportunity_id)
    )

    assert result.not_renderable is not None
    assert result.not_renderable.reason_code == "planning_result_store_unavailable"
    assert legacy.calls == []
