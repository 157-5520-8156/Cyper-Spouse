import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from companion_daemon.event_media import (
    AudienceContext,
    LegacyMediaShotAdapter,
    MediaInspection,
    MediaOpportunity,
    MediaPlan,
    MediaPlanner,
    MediaRenderer,
    NotRenderable,
    OpenAIMediaInspector,
    PlannedMedia,
    RenderedMedia,
    compile_media_prompt,
    _enforce_inspection_contract,
    _inspection_prompt,
    _repair_prompt,
)
from companion_daemon.image_generation import GeneratedImage


@pytest.fixture(autouse=True)
def _enable_event_media(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANION_EVENT_MEDIA_ENABLED", "1")


def _snapshot(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "world-event-snapshot-v1",
        "event": {
            "event_id": "event:42",
            "type": "daily_activity",
            "status": "committed",
            "logical_at": "2026-07-14T10:00:00+08:00",
            "summary": "在咖啡馆吃早餐",
            "outcome": "点了拿铁和可颂",
        },
        "source": {"channel": "direct_experience", "person": "character"},
        "location": {"name": "学校附近咖啡馆", "kind": "public", "mirror_available": True},
        "activity": {"kind": "eating", "description": "正在吃早餐"},
        "participants": [{"id": "friend:lin", "role": "known_companion"}],
        "objects": [{"id": "food:croissant", "kind": "food", "description": "可颂和拿铁"}],
        "environment": {"lighting": "window daylight", "weather": "sunny"},
        "character": {
            "emotion": "bright",
            "energy": "normal",
            "appearance": "头发被风吹得稍微有点乱",
            "body_health": {"kind": "bruise", "description": "左膝轻微淤青"},
        },
        "existing_media": [
            {"id": "media:original", "path": "/tmp/original.png", "accessible": True}
        ],
    }
    value.update(overrides)
    return value


def _opportunity(
    *,
    family: str = "character_media",
    privacy: str = "personal",
    snapshot=None,
    automatic: bool = False,
    sensual_charge_ceiling: str = "none",
    expression_charge_ceiling: str | None = None,
    audience_context: AudienceContext | None = None,
) -> MediaOpportunity:
    return MediaOpportunity(
        opportunity_id="opportunity:42",
        family=family,
        privacy_ceiling=privacy,
        event_snapshot=snapshot or _snapshot(),
        delivery_mode="automatic" if automatic else "preview",
        sensual_charge_ceiling=sensual_charge_ceiling,
        expression_charge_ceiling=expression_charge_ceiling,
        audience_context=audience_context,
    )


@pytest.mark.asyncio
async def test_expression_charge_ceiling_falls_back_and_rejects_conflicts() -> None:
    audience = AudienceContext(recipient_ref="user:1", relationship_stage="lover")
    compatible = _opportunity(
        privacy="intimate",
        sensual_charge_ceiling="charged",
        audience_context=audience,
    )
    explicit = _opportunity(
        privacy="intimate",
        sensual_charge_ceiling="charged",
        expression_charge_ceiling="charged",
        audience_context=audience,
    )
    conflict = _opportunity(
        privacy="intimate",
        sensual_charge_ceiling="subtle",
        expression_charge_ceiling="charged",
        audience_context=audience,
    )

    proposal = _proposal()
    assert not isinstance(await MediaPlanner(FakeModel(proposal)).plan(compatible), NotRenderable)
    assert not isinstance(await MediaPlanner(FakeModel(proposal)).plan(explicit), NotRenderable)
    rejected = await MediaPlanner(FakeModel(proposal)).plan(conflict)
    assert isinstance(rejected, NotRenderable)
    assert rejected.reason == "conflicting_expression_charge_ceilings"


def _proposal(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "content_domain": "food_drink",
        "visual_form": "portrait_context",
        "share_intent": "show_and_tell",
        "capture_mode": "character_front_camera",
        "character_visibility": "identifiable",
        "other_people_visibility": "none",
        "polish": "casual",
        "tone": "bright",
        "privacy": "personal",
        "primary_evidence_ref": "/activity/description",
        "supporting_evidence_refs": ["/objects/0/description", "/location/name"],
        "composition": "主体与事件环境同时可辨的自然中近景",
        "action": "自然地把{primary}带进画面",
        "camera_direction": "略高于视线的轻微倾斜手机机位",
        "sharing_motive": "把这个生活瞬间分享给熟悉的人",
        "constraints": ["不生成可读文字", "手部结构自然"],
        "route": "generate",
        "interaction_bid_id": "share_discovery",
        "subject_variant_id": "aware_three_quarter",
    }
    value.update(overrides)
    if "camera_direction" not in overrides:
        value["camera_direction"] = {
            "character_front_camera": "略高于视线的轻微倾斜手机机位",
            "character_rear_camera": "后摄正常透视且没有自拍臂",
            "mirror": "镜面反射成立且手机位置自然",
            "timer_fixed": "固定设备的稳定第三人称视角",
            "requested_helper": "他人代拍的自然观看距离",
            "known_companion": "同伴手持相机的友好观看距离",
            "external_sender": "他人代拍的自然观看距离",
            "existing_artifact": "保持原始媒体已有的相机视角",
        }[str(value["capture_mode"])]
    if "subject_variant_id" not in overrides:
        value["subject_variant_id"] = (
            "body_detail_showcase"
            if value["character_visibility"] == "body_detail"
            else {
                "character_front_camera": "aware_three_quarter",
                "character_rear_camera": "aware_three_quarter",
                "mirror": "mirror_composed",
                "timer_fixed": "timer_environment_pose",
                "requested_helper": "helper_checkin_pose",
                "known_companion": "companion_reaction",
                "external_sender": "external_candid_glance",
                "existing_artifact": "aware_three_quarter",
            }[str(value["capture_mode"])]
        )
    if "interaction_bid_id" not in overrides:
        variant = str(value.get("subject_variant_id") or "")
        value["interaction_bid_id"] = (
            "seek_validation"
            if variant.startswith("screen_check_reaction")
            else "invite_appreciation"
            if variant.startswith("helper_checkin_pose")
            else "share_presence"
            if variant.startswith(("companion_reaction", "look_at_primary"))
            else "invite_playful_exchange"
            if variant.startswith("playful_level_pose")
            else "share_discovery"
        )
    primary = value["primary_evidence_ref"]
    value["supporting_evidence_refs"] = [
        item for item in value["supporting_evidence_refs"] if item != primary
    ]
    return value


class FakeModel:
    def __init__(self, payload: object):
        self.payload = payload
        self.calls = 0
        self.messages: list[dict[str, str]] = []

    async def complete(self, messages, *, temperature=0.8):
        self.calls += 1
        self.messages = messages
        payload = dict(self.payload) if isinstance(self.payload, dict) else self.payload
        if (
            isinstance(payload, dict)
            and payload.get("character_visibility") in {"identifiable", "body_detail"}
            and "presentation_candidate_id" not in payload
        ):
            marker = "legal_character_presentation_candidates="
            user = str(messages[-1]["content"])
            encoded = user.split(marker, 1)[1].split("\n", 1)[0]
            candidates = json.loads(encoded)
            capture = payload.get("capture_mode")
            visibility = payload.get("character_visibility")
            intent = payload.get("share_intent")
            preferred = str(payload.get("subject_variant_id") or "")
            bid = payload.get("interaction_bid_id")
            privacy_rank = {"ordinary": 0, "personal": 1, "intimate": 2}
            legal = [
                item
                for item in candidates
                if capture in item["legal_capture_modes"]
                and visibility == item["character_visibility"]
                and intent in item["legal_share_intents"]
                and privacy_rank[item["minimum_privacy"]] <= privacy_rank[str(payload["privacy"])]
                and bid in item["subject_presentation"]["display_strategy"]["communicative_goals"]
            ]
            if not legal:
                legal = [
                    item
                    for item in candidates
                    if capture in item["legal_capture_modes"]
                    and visibility == item["character_visibility"]
                    and intent in item["legal_share_intents"]
                    and privacy_rank[item["minimum_privacy"]]
                    <= privacy_rank[str(payload["privacy"])]
                ]
            selected = next(
                (
                    item
                    for item in legal
                    if str(item["presentation_candidate_id"]).split("~", 1)[0].startswith(preferred)
                ),
                legal[0] if legal else None,
            )
            if selected:
                payload["presentation_candidate_id"] = selected["presentation_candidate_id"]
                goals = selected["subject_presentation"]["display_strategy"]["communicative_goals"]
                if payload.get("interaction_bid_id") not in goals:
                    payload["interaction_bid_id"] = goals[0]
        return json.dumps(payload, ensure_ascii=False)


class ChargeSelectingModel(FakeModel):
    def __init__(self, payload: dict[str, object], charge: str):
        super().__init__(payload)
        self.charge = charge

    async def complete(self, messages, *, temperature=0.8):
        payload = dict(self.payload)
        user = str(messages[-1]["content"])
        encoded = user.split("legal_character_presentation_candidates=", 1)[1].split("\n", 1)[0]
        candidates = json.loads(encoded)
        selected = next(
            item
            for item in candidates
            if item["embodied_presentation"]["sensual_charge"] == self.charge
            and payload["capture_mode"] in item["legal_capture_modes"]
            and payload["share_intent"] in item["legal_share_intents"]
            and payload["interaction_bid_id"]
            in item["subject_presentation"]["display_strategy"]["communicative_goals"]
        )
        payload["presentation_candidate_id"] = selected["presentation_candidate_id"]
        self.calls += 1
        self.messages = messages
        return json.dumps(payload, ensure_ascii=False)


class RawFakeModel(FakeModel):
    async def complete(self, messages, *, temperature=0.8):
        self.calls += 1
        self.messages = messages
        return json.dumps(self.payload, ensure_ascii=False)


class V5SelectingModel(FakeModel):
    async def complete(self, messages, *, temperature=0.8):
        self.calls += 1
        self.messages = messages
        payload = dict(self.payload)
        user = str(messages[-1]["content"])
        encoded = user.split("legal_complete_media_expression_candidates=", 1)[1].split("\n", 1)[0]
        candidates = json.loads(encoded)
        legal = [
            item
            for item in candidates
            if payload["interaction_bid_id"] in item["legal_interaction_bids"]
            and payload["character_visibility"] in item["legal_character_visibilities"]
            and payload["route"] in item["legal_routes"]
        ]
        if not legal:
            legal = [
                item
                for item in candidates
                if payload["visual_form"] in item["legal_visual_forms"]
                and payload["share_intent"] in item["legal_share_intents"]
                and payload["character_visibility"] in item["legal_character_visibilities"]
                and payload["route"] in item["legal_routes"]
            ] or candidates
        selected = next(
            (
                item
                for item in legal
                if payload["capture_mode"] in item["legal_capture_modes"]
                and payload["visual_form"] in item["legal_visual_forms"]
                and payload["share_intent"] in item["legal_share_intents"]
            ),
            next(
                (
                    item
                    for item in legal
                    if payload["visual_form"] in item["legal_visual_forms"]
                    and payload["share_intent"] in item["legal_share_intents"]
                ),
                legal[0],
            ),
        )
        payload["capture_mode"] = selected["legal_capture_modes"][0]
        payload["visual_form"] = selected["legal_visual_forms"][0]
        if payload["share_intent"] not in selected["legal_share_intents"]:
            payload["share_intent"] = selected["legal_share_intents"][0]
        payload["complete_candidate_id"] = selected["complete_candidate_id"]
        payload["interaction_bid_id"] = selected["legal_interaction_bids"][0]
        payload["character_visibility"] = selected["legal_character_visibilities"][0]
        payload["route"] = selected["legal_routes"][0]
        if payload["content_domain"] == "food_drink" and payload["visual_form"] not in {
            "portrait_closeup",
            "portrait_context",
        }:
            if payload["visual_form"] in {"full_body", "social_frame"}:
                payload["content_domain"] = "activity_process"
                payload["primary_evidence_ref"] = "/activity/description"
                payload["share_intent"] = "record"
            elif payload["visual_form"] == "wide_scene":
                payload["content_domain"] = "place_environment"
                payload["primary_evidence_ref"] = "/location/name"
                payload["share_intent"] = "atmosphere"
            elif payload["visual_form"] in {"subject_closeup", "body_detail"}:
                payload["content_domain"] = "object_possession"
                payload["primary_evidence_ref"] = "/objects/0/description"
                payload["share_intent"] = "show_and_tell"
            payload["supporting_evidence_refs"] = [
                item
                for item in payload["supporting_evidence_refs"]
                if item != payload["primary_evidence_ref"]
            ]
        return json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_v5_freezes_complete_expression_candidate_without_free_direction_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPANION_EVENT_MEDIA_V5_ENABLED", "1")
    proposal = _proposal()
    proposal["interaction_bid_id"] = "share_presence"
    for field in (
        "composition",
        "action",
        "camera_direction",
        "sharing_motive",
        "subject_variant_id",
    ):
        proposal.pop(field, None)

    result = await MediaPlanner(V5SelectingModel(proposal)).plan(_opportunity())

    assert isinstance(result, PlannedMedia)
    assert result.plan.version == "event-media-plan-v5"
    assert result.plan.action_template_id
    assert result.plan.media_address_strategy is not None
    assert result.plan.camera_geometry is not None
    assert result.plan.identity_reference_selection is not None
    assert result.plan.photographic_authenticity is not None
    assert result.plan.moment_capture is not None
    assert result.plan.subject_presentation.version == "subject-presentation-v4"
    assert result.plan.subject_presentation.facial_display_strategy is not None
    assert result.plan.subject_presentation.facial_micro_performance is not None
    assert result.plan.embodied_presentation.version == "embodied-presentation-v3"
    payload = result.plan.to_payload()
    assert not {"composition", "action", "camera_direction", "sharing_motive"} & payload.keys()
    assert set(result.plan.moment_capture.evidence_refs) == set(result.plan.evidence_values)
    assert MediaPlan.from_payload(payload) == result.plan
    tampered_moment = result.plan.to_payload()
    tampered_moment["moment_capture"]["scene_anchor"] = "social_context"
    with pytest.raises(ValueError, match="invalid media plan payload"):
        MediaPlan.from_payload(tampered_moment)
    assert "photographic_authenticity" not in replace(
        result.plan, photographic_authenticity=None
    ).to_payload()
    prompt = compile_media_prompt(result.plan, None)
    assert prompt.index("Selected event evidence") < prompt.index("Interaction Bid")
    assert prompt.index("Interaction Bid") < prompt.index("Media Address Strategy")
    assert prompt.index("Media Address Strategy") < prompt.index("Camera Geometry")
    assert prompt.index("Camera Geometry") < prompt.index("Moment Capture")
    assert prompt.index("Camera Geometry") < prompt.index("Photographic Authenticity")
    assert "Facial Display Strategy" in prompt
    assert "Facial Micro-Performance" in prompt
    assert "nose/cheek" in prompt
    assert "action unit" not in prompt.lower()
    assert "facial micro performance v1" not in prompt.lower()


def test_moment_capture_quality_gate_requires_a_match_and_repairs_only_that_contract() -> None:
    mismatch = _enforce_inspection_contract(
        replace(
            _inspection(True),
            rule_version="media-inspection-v7",
            moment_capture_matches=False,
        ),
        automatic=True,
        v5_required=True,
        moment_capture_required=True,
    )
    missing = _enforce_inspection_contract(
        replace(_inspection(True), rule_version="media-inspection-v7", moment_capture_matches=None),
        automatic=True,
        v5_required=True,
        moment_capture_required=True,
    )

    assert mismatch.reason == "moment_capture_mismatch"
    assert missing.reason == "inspection_v7_fields_missing"
    assert "lived moment continuity" in _repair_prompt("frozen", mismatch)
    assert "Moment Capture contract" in _repair_prompt("frozen", mismatch)


@pytest.mark.asyncio
async def test_v5_allows_grounded_intimate_life_share_without_character_invention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPANION_EVENT_MEDIA_V5_ENABLED", "1")
    proposal = {
        "content_domain": "place_environment",
        "visual_form": "contextual_still_life",
        "share_intent": "intimate_signal",
        "capture_mode": "character_rear_camera",
        "character_visibility": "trace_only",
        "other_people_visibility": "none",
        "polish": "casual",
        "tone": "tender",
        "privacy": "intimate",
        "primary_evidence_ref": "/location/name",
        "supporting_evidence_refs": ["/environment/lighting", "/participants/0/id"],
        "constraints": ["不生成可读文字"],
        "route": "generate",
        "interaction_bid_id": "invite_desire",
    }
    opportunity = _opportunity(
        family="life_share",
        privacy="intimate",
        sensual_charge_ceiling="charged",
        audience_context=AudienceContext(recipient_ref="user:1", relationship_stage="lover"),
    )

    result = await MediaPlanner(V5SelectingModel(proposal)).plan(opportunity)

    assert isinstance(result, PlannedMedia)
    assert result.plan.family == "life_share"
    assert result.plan.subject_presentation is None
    assert result.plan.media_address_strategy.attraction_mechanism == "atmospheric_suggestion"


@pytest.mark.asyncio
async def test_v5_new_status_bid_is_available_to_character_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPANION_EVENT_MEDIA_V5_ENABLED", "1")
    proposal = _proposal(interaction_bid_id="inform_status")
    for field in (
        "composition",
        "action",
        "camera_direction",
        "sharing_motive",
        "subject_variant_id",
    ):
        proposal.pop(field, None)

    result = await MediaPlanner(V5SelectingModel(proposal)).plan(_opportunity())

    assert isinstance(result, PlannedMedia)
    assert result.plan.interaction_bid.communicative_goal == "inform_status"
    assert result.plan.media_address_strategy.engagement_tactic in {"presence", "demonstration"}


@pytest.mark.asyncio
async def test_v5_candidate_space_is_replay_stable_and_varies_across_opportunities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPANION_EVENT_MEDIA_V5_ENABLED", "1")
    proposal = _proposal(interaction_bid_id="share_presence")
    proposal["supporting_evidence_refs"].append("/participants/0/id")
    for field in (
        "composition",
        "action",
        "camera_direction",
        "sharing_motive",
        "subject_variant_id",
    ):
        proposal.pop(field, None)
    base = _opportunity()

    first = await MediaPlanner(V5SelectingModel(proposal)).plan(base)
    repeated = await MediaPlanner(V5SelectingModel(proposal)).plan(base)
    assert isinstance(first, PlannedMedia)
    assert isinstance(repeated, PlannedMedia)
    assert first.plan.to_payload() == repeated.plan.to_payload()

    signatures: set[tuple[str, ...]] = set()
    for index in range(8):
        model = V5SelectingModel(proposal)
        result = await MediaPlanner(model).plan(
            replace(base, opportunity_id=f"opportunity:variation:{index}")
        )
        assert isinstance(result, PlannedMedia)
        signatures.add(
            (
                result.plan.camera_geometry.shot_distance,
                result.plan.camera_geometry.camera_height,
                result.plan.camera_geometry.view_axis,
                result.plan.camera_geometry.orientation,
                    result.plan.subject_presentation.facial_display_strategy.strategy_family,
                result.plan.subject_presentation.performance.head_yaw,
            )
        )
        encoded = (
            model.messages[-1]["content"]
            .split("legal_complete_media_expression_candidates=", 1)[1]
            .split("\n", 1)[0]
        )
        assert len(json.loads(encoded)) <= 24

    assert len(signatures) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "overrides"),
    [
        (
            "life_share",
            {
                "content_domain": "place_environment",
                "visual_form": "wide_scene",
                "share_intent": "atmosphere",
                "capture_mode": "character_rear_camera",
                "character_visibility": "none",
                "privacy": "ordinary",
                "primary_evidence_ref": "/location/name",
            },
        ),
        (
            "life_share",
            {
                "content_domain": "food_drink",
                "visual_form": "contextual_still_life",
                "capture_mode": "character_rear_camera",
                "character_visibility": "trace_only",
                "privacy": "ordinary",
                "primary_evidence_ref": "/objects/0/description",
            },
        ),
        (
            "life_share",
            {
                "content_domain": "object_possession",
                "visual_form": "subject_closeup",
                "capture_mode": "character_rear_camera",
                "character_visibility": "none",
                "privacy": "ordinary",
                "primary_evidence_ref": "/objects/0/description",
            },
        ),
        (
            "life_share",
            {
                "content_domain": "activity_process",
                "visual_form": "process_pov",
                "share_intent": "record",
                "capture_mode": "character_rear_camera",
                "character_visibility": "trace_only",
                "privacy": "ordinary",
            },
        ),
        (
            "life_share",
            {
                "content_domain": "outcome_progress",
                "visual_form": "result_showcase",
                "capture_mode": "character_rear_camera",
                "character_visibility": "none",
                "privacy": "ordinary",
                "primary_evidence_ref": "/event/outcome",
            },
        ),
        (
            "life_share",
            {
                "content_domain": "travel_transit",
                "visual_form": "wide_scene",
                "share_intent": "check_in",
                "capture_mode": "character_rear_camera",
                "character_visibility": "none",
                "privacy": "ordinary",
                "primary_evidence_ref": "/location/name",
            },
        ),
        (
            "life_share",
            {
                "content_domain": "nature_animal",
                "visual_form": "subject_closeup",
                "capture_mode": "character_rear_camera",
                "character_visibility": "none",
                "privacy": "ordinary",
                "primary_evidence_ref": "/environment/weather",
            },
        ),
        ("character_media", {}),
        (
            "character_media",
            {
                "content_domain": "appearance_style",
                "visual_form": "portrait_closeup",
                "share_intent": "seek_feedback",
                "capture_mode": "mirror",
                "primary_evidence_ref": "/character/appearance",
            },
        ),
        (
            "character_media",
            {
                "content_domain": "place_environment",
                "visual_form": "full_body",
                "share_intent": "check_in",
                "capture_mode": "requested_helper",
                "primary_evidence_ref": "/location/name",
            },
        ),
        (
            "character_media",
            {
                "content_domain": "social_interaction",
                "visual_form": "social_frame",
                "share_intent": "memory_keep",
                "capture_mode": "known_companion",
                "other_people_visibility": "known_anonymized",
                "primary_evidence_ref": "/participants/0/id",
            },
        ),
        (
            "character_media",
            {
                "content_domain": "body_health",
                "visual_form": "body_detail",
                "share_intent": "care_update",
                "capture_mode": "character_rear_camera",
                "character_visibility": "body_detail",
                "primary_evidence_ref": "/character/body_health/description",
            },
        ),
    ],
)
async def test_planner_accepts_cross_matrix_prototypes(
    family: str, overrides: dict[str, object]
) -> None:
    model = FakeModel(_proposal(**overrides))
    result = await MediaPlanner(model).plan(_opportunity(family=family))

    assert isinstance(result, PlannedMedia)
    assert result.plan.family == family
    assert result.plan.primary_evidence_ref == str(_proposal(**overrides)["primary_evidence_ref"])
    assert result.plan.evidence_values[result.plan.primary_evidence_ref]
    assert model.calls == 1


@pytest.mark.asyncio
async def test_new_character_plan_freezes_subject_presentation_in_same_call() -> None:
    model = FakeModel(
        _proposal(
            subject_variant_id="screen_check_reaction",
            interaction_bid_id="seek_validation",
        )
    )

    result = await MediaPlanner(model).plan(_opportunity())

    assert isinstance(result, PlannedMedia)
    assert result.plan.version == "event-media-plan-v4"
    assert result.plan.interaction_bid is not None
    assert result.plan.interaction_bid.bid_id == "media-bid:opportunity:42"
    assert result.plan.subject_presentation is not None
    assert result.plan.subject_presentation.display_strategy is not None
    assert (
        result.plan.interaction_bid.communicative_goal
        in result.plan.subject_presentation.display_strategy.communicative_goals
    )
    assert "legal_character_presentation_candidates" in model.messages[1]["content"]
    restored = MediaPlan.from_payload(result.plan.to_payload())
    assert restored == result.plan
    prompt = compile_media_prompt(restored, Path("configs/visual_identity.yaml"))
    assert "Frozen subject presentation" in prompt
    assert "Do not copy their head angle" in prompt
    assert prompt.rfind("Frozen subject presentation") > prompt.rfind("Character identity anchor")


@pytest.mark.asyncio
async def test_planner_freezes_interaction_bid_for_life_share_without_subject() -> None:
    proposal = _proposal(
        content_domain="place_environment",
        visual_form="wide_scene",
        share_intent="atmosphere",
        capture_mode="character_rear_camera",
        character_visibility="none",
        privacy="ordinary",
        interaction_bid_id="share_presence",
    )
    proposal.pop("subject_variant_id")

    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity(family="life_share"))

    assert isinstance(result, PlannedMedia)
    assert result.plan.subject_presentation is None
    assert result.plan.interaction_bid is not None
    assert result.plan.interaction_bid.hoped_response == "acknowledge_or_light_reaction"
    assert MediaPlan.from_payload(result.plan.to_payload()) == result.plan


@pytest.mark.asyncio
async def test_interaction_bid_id_is_unique_per_media_opportunity() -> None:
    proposal = _proposal(
        content_domain="place_environment",
        visual_form="wide_scene",
        share_intent="atmosphere",
        capture_mode="character_rear_camera",
        character_visibility="none",
        privacy="ordinary",
        interaction_bid_id="share_presence",
    )
    proposal.pop("subject_variant_id")
    first = await MediaPlanner(FakeModel(proposal)).plan(_opportunity(family="life_share"))
    second_opportunity = MediaOpportunity(
        **{
            **_opportunity(family="life_share").__dict__,
            "opportunity_id": "opportunity:43",
        }
    )
    second = await MediaPlanner(FakeModel(proposal)).plan(second_opportunity)

    assert isinstance(first, PlannedMedia) and isinstance(second, PlannedMedia)
    assert first.plan.interaction_bid is not None
    assert second.plan.interaction_bid is not None
    assert first.plan.interaction_bid.bid_id != second.plan.interaction_bid.bid_id


@pytest.mark.asyncio
async def test_planner_exposes_all_ordinary_bids_but_requires_audience_for_closeness() -> None:
    without_audience = FakeModel(_proposal())
    result = await MediaPlanner(without_audience).plan(_opportunity(privacy="intimate"))
    assert isinstance(result, PlannedMedia)
    prompt = without_audience.messages[1]["content"]
    assert '"interaction_bid_id":"share_presence"' in prompt
    assert '"interaction_bid_id":"invite_closeness"' not in prompt

    with_audience = FakeModel(
        _proposal(
            content_domain="appearance_style",
            share_intent="intimate_signal",
            privacy="intimate",
            capture_mode="mirror",
            interaction_bid_id="invite_closeness",
            subject_variant_id="playful_level_pose",
            primary_evidence_ref="/character/appearance",
            sharing_motive="传递克制且非露骨的亲密信号",
        )
    )
    opportunity = _opportunity(privacy="intimate", sensual_charge_ceiling="subtle")
    opportunity = MediaOpportunity(
        **{
            **opportunity.__dict__,
            "audience_context": AudienceContext(
                recipient_ref="user:geoff", relationship_stage="ambiguous"
            ),
        }
    )
    result = await MediaPlanner(with_audience).plan(opportunity)
    assert isinstance(result, PlannedMedia)
    assert result.plan.interaction_bid is not None
    assert result.plan.interaction_bid.audience_ref == "user:geoff"
    assert '"interaction_bid_id":"invite_closeness"' in with_audience.messages[1]["content"]


@pytest.mark.asyncio
async def test_planner_rejects_interaction_bid_outside_frozen_privacy() -> None:
    result = await MediaPlanner(
        RawFakeModel(_proposal(privacy="ordinary", interaction_bid_id="invite_closeness"))
    ).plan(_opportunity(privacy="ordinary"))

    assert isinstance(result, NotRenderable)
    assert result.reason == "illegal_interaction_bid"


@pytest.mark.asyncio
async def test_planner_rejects_semantically_mismatched_bid_and_subject_strategy() -> None:
    result = await MediaPlanner(
        RawFakeModel(
            _proposal(
                interaction_bid_id="seek_care",
                presentation_candidate_id="not-a-legal-complete-candidate",
            )
        )
    ).plan(_opportunity())

    assert isinstance(result, NotRenderable)
    assert result.reason == "illegal_presentation_candidate"


@pytest.mark.asyncio
async def test_final_plan_privacy_limits_subject_display_strategy() -> None:
    opportunity = _opportunity()
    planned = await MediaPlanner(
        FakeModel(
            _proposal(
                content_domain="body_health",
                visual_form="body_detail",
                share_intent="care_update",
                capture_mode="character_rear_camera",
                character_visibility="body_detail",
                primary_evidence_ref="/character/body_health/description",
                interaction_bid_id="seek_care",
                subject_variant_id="body_detail_showcase",
            )
        )
    ).plan(opportunity)
    assert isinstance(planned, PlannedMedia)

    tampered = planned.plan.to_payload()
    tampered["privacy"] = "ordinary"
    with pytest.raises(ValueError, match="interaction_bid_privacy_conflict"):
        MediaPlan.from_payload(tampered)


@pytest.mark.asyncio
async def test_generated_character_plan_requires_legal_complete_candidate() -> None:
    missing = _proposal()
    missing.pop("subject_variant_id")
    missing_result = await MediaPlanner(RawFakeModel(missing)).plan(_opportunity())
    illegal_result = await MediaPlanner(
        RawFakeModel(_proposal(presentation_candidate_id="not-legal"))
    ).plan(_opportunity())

    assert isinstance(missing_result, NotRenderable)
    assert missing_result.reason == "missing_presentation_candidate"
    assert isinstance(illegal_result, NotRenderable)
    assert illegal_result.reason == "illegal_presentation_candidate"


@pytest.mark.asyncio
async def test_front_camera_rejects_no_selfie_arm_constraint() -> None:
    proposal = _proposal(constraints=["不出现自拍臂", "手部结构自然"])

    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity())

    assert isinstance(result, NotRenderable)
    assert result.reason == "unsupported_model_constraint"


@pytest.mark.asyncio
async def test_non_selfie_capture_derives_no_selfie_arm_constraint() -> None:
    result = await MediaPlanner(
        FakeModel(
            _proposal(
                capture_mode="timer_fixed",
                constraints=["手部结构自然"],
                subject_variant_id="timer_environment_pose",
            )
        )
    ).plan(_opportunity())

    assert isinstance(result, PlannedMedia)
    assert "不出现自拍臂" in result.plan.constraints


@pytest.mark.asyncio
async def test_missing_subject_catalog_fails_closed_before_model_call(tmp_path: Path) -> None:
    model = FakeModel(_proposal())

    result = await MediaPlanner(model, subject_config_path=tmp_path / "missing-subjects.yaml").plan(
        _opportunity()
    )

    assert isinstance(result, NotRenderable)
    assert result.reason == "presentation_catalog_unavailable"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_missing_interaction_catalog_fails_closed_before_model_call(
    tmp_path: Path,
) -> None:
    model = FakeModel(_proposal())

    result = await MediaPlanner(
        model, interaction_config_path=tmp_path / "missing-interactions.yaml"
    ).plan(_opportunity())

    assert isinstance(result, NotRenderable)
    assert result.reason == "interaction_catalog_unavailable"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_v1_plan_replays_without_new_subject_interpretation() -> None:
    current = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(current, PlannedMedia)
    payload = current.plan.to_payload()
    payload["version"] = "event-media-plan-v1"
    payload["subject_presentation"] = None
    payload["interaction_bid"] = None
    payload["embodied_presentation"] = None
    payload["diversity_fingerprint"] = "|".join(current.plan.diversity_fingerprint.split("|")[:8])

    restored = MediaPlan.from_payload(payload)

    assert restored.subject_presentation is None
    assert "Frozen subject presentation" not in compile_media_prompt(restored, None)


@pytest.mark.asyncio
async def test_frozen_plan_rejects_tampered_hand_occupancy() -> None:
    current = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(current, PlannedMedia)
    payload = current.plan.to_payload()
    subject = payload["subject_presentation"]
    assert isinstance(subject, dict)
    performance = subject["performance"]
    assert isinstance(performance, dict)
    performance["hand_occupancy"] = "both_hands_available"

    with pytest.raises(ValueError, match="capture_hand_occupancy_conflict"):
        MediaPlan.from_payload(payload)


@pytest.mark.asyncio
async def test_frozen_v3_rejects_tampered_social_meaning() -> None:
    current = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(current, PlannedMedia)

    bid_tamper = current.plan.to_payload()
    bid = bid_tamper["interaction_bid"]
    assert isinstance(bid, dict)
    bid["communicative_goal"] = "demand_unconditional_reply"
    with pytest.raises(ValueError, match="invalid media plan payload"):
        MediaPlan.from_payload(bid_tamper)

    display_tamper = current.plan.to_payload()
    subject = display_tamper["subject_presentation"]
    assert isinstance(subject, dict)
    strategy = subject["display_strategy"]
    assert isinstance(strategy, dict)
    strategy["holistic_cue"] = "perform an unrelated expression"
    with pytest.raises(ValueError, match="invalid media plan payload"):
        MediaPlan.from_payload(display_tamper)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {
            "content_domain": "information_screen",
            "visual_form": "subject_closeup",
            "share_intent": "progress_update",
            "capture_mode": "existing_artifact",
            "character_visibility": "none",
            "privacy": "ordinary",
            "route": "reuse_existing",
            "primary_evidence_ref": "/existing_media/0/path",
        },
        {
            "content_domain": "social_interaction",
            "visual_form": "social_frame",
            "share_intent": "memory_keep",
            "capture_mode": "known_companion",
            "character_visibility": "trace_only",
            "other_people_visibility": "known_anonymized",
            "privacy": "ordinary",
            "primary_evidence_ref": "/participants/0/id",
        },
        {
            "content_domain": "other_grounded",
            "visual_form": "subject_closeup",
            "share_intent": "record",
            "capture_mode": "character_rear_camera",
            "character_visibility": "none",
            "privacy": "ordinary",
            "primary_evidence_ref": "/event/outcome",
        },
    ],
)
async def test_planner_accepts_remaining_life_share_rows(overrides: dict[str, object]) -> None:
    result = await MediaPlanner(FakeModel(_proposal(**overrides))).plan(
        _opportunity(family="life_share")
    )
    assert isinstance(result, PlannedMedia)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {
            "content_domain": "appearance_style",
            "visual_form": "full_body",
            "share_intent": "show_and_tell",
            "capture_mode": "mirror",
            "primary_evidence_ref": "/character/appearance",
        },
        {
            "content_domain": "activity_process",
            "visual_form": "portrait_closeup",
            "share_intent": "complain",
            "polish": "raw",
            "tone": "embarrassed",
        },
        {
            "content_domain": "object_possession",
            "visual_form": "body_detail",
            "share_intent": "seek_feedback",
            "capture_mode": "character_rear_camera",
            "character_visibility": "body_detail",
            "primary_evidence_ref": "/objects/0/description",
        },
        {
            "content_domain": "appearance_style",
            "visual_form": "portrait_context",
            "share_intent": "intimate_signal",
            "privacy": "intimate",
            "capture_mode": "mirror",
            "tone": "tender",
            "interaction_bid_id": "invite_closeness",
            "sharing_motive": "传递克制且非露骨的亲密信号",
            "primary_evidence_ref": "/character/appearance",
        },
        {
            "content_domain": "social_interaction",
            "visual_form": "portrait_context",
            "share_intent": "humor",
            "capture_mode": "known_companion",
            "primary_evidence_ref": "/participants/0/id",
        },
    ],
)
async def test_planner_accepts_remaining_character_media_rows(overrides: dict[str, object]) -> None:
    intimate = overrides.get("share_intent") == "intimate_signal"
    result = await MediaPlanner(FakeModel(_proposal(**overrides))).plan(
        _opportunity(
            family="character_media",
            privacy="intimate" if intimate else "personal",
            sensual_charge_ceiling="subtle" if intimate else "none",
            audience_context=(
                AudienceContext(recipient_ref="user:geoff", relationship_stage="ambiguous")
                if intimate
                else None
            ),
        )
    )
    assert isinstance(result, PlannedMedia)


@pytest.mark.asyncio
async def test_planner_accepts_external_character_media_with_sender_evidence() -> None:
    snapshot = _snapshot(source={"channel": "message", "person": "friend:lin"})
    result = await MediaPlanner(
        FakeModel(
            _proposal(
                capture_mode="external_sender",
                supporting_evidence_refs=["/source/person", "/location/name"],
            )
        )
    ).plan(_opportunity(snapshot=snapshot))
    assert isinstance(result, PlannedMedia)


@pytest.mark.asyncio
async def test_feature_flag_defaults_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPANION_EVENT_MEDIA_ENABLED", raising=False)
    result = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(result, NotRenderable)
    assert result.reason == "event_media_feature_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "snapshot", "family", "privacy", "reason"),
    [
        (
            {
                "character_visibility": "identifiable",
                "capture_mode": "character_rear_camera",
                "privacy": "ordinary",
                "visual_form": "contextual_still_life",
            },
            None,
            "life_share",
            "ordinary",
            "family_visibility_conflict",
        ),
        (
            {"character_visibility": "none"},
            None,
            "character_media",
            "personal",
            "family_visibility_conflict",
        ),
        (
            {"capture_mode": "known_companion"},
            {"participants": []},
            "character_media",
            "personal",
            "missing_companion_evidence",
        ),
        (
            {"capture_mode": "external_sender"},
            {"source": {"channel": "direct_experience"}},
            "character_media",
            "personal",
            "missing_external_sender_evidence",
        ),
        (
            {"capture_mode": "existing_artifact", "route": "reuse_existing"},
            {"existing_media": []},
            "character_media",
            "personal",
            "missing_existing_artifact",
        ),
        (
            {
                "content_domain": "body_health",
                "visual_form": "body_detail",
                "share_intent": "care_update",
                "character_visibility": "body_detail",
                "primary_evidence_ref": "/activity/description",
            },
            {"character": {"emotion": "calm"}},
            "character_media",
            "personal",
            "missing_body_health_evidence",
        ),
        (
            {
                "content_domain": "appearance_style",
                "visual_form": "portrait_context",
                "primary_evidence_ref": "/character/appearance",
                "privacy": "intimate",
                "share_intent": "intimate_signal",
            },
            None,
            "character_media",
            "ordinary",
            "privacy_ceiling_exceeded",
        ),
        (
            {
                "content_domain": "social_interaction",
                "visual_form": "social_frame",
                "share_intent": "record",
                "other_people_visibility": "none",
            },
            None,
            "character_media",
            "personal",
            "social_frame_requires_people",
        ),
        (
            {
                "visual_form": "social_frame",
                "content_domain": "social_interaction",
                "share_intent": "record",
                "capture_mode": "known_companion",
                "other_people_visibility": "identity_referenced",
                "primary_evidence_ref": "/participants/0/id",
            },
            None,
            "character_media",
            "personal",
            "missing_identity_reference",
        ),
        (
            {
                "content_domain": "information_screen",
                "primary_evidence_ref": "/event/summary",
                "capture_mode": "character_rear_camera",
            },
            {"visual_requirements": {"requires_readable_text": True}, "existing_media": []},
            "character_media",
            "personal",
            "readable_text_requires_artifact",
        ),
    ],
)
async def test_planner_rejects_illegal_combinations(
    overrides: dict[str, object],
    snapshot: dict[str, object] | None,
    family: str,
    privacy: str,
    reason: str,
) -> None:
    base = _snapshot()
    if snapshot:
        base.update(snapshot)
    result = await MediaPlanner(FakeModel(_proposal(**overrides))).plan(
        _opportunity(family=family, privacy=privacy, snapshot=base)
    )

    assert isinstance(result, NotRenderable)
    assert result.reason == reason


@pytest.mark.asyncio
async def test_planner_rejects_unknown_pointer_invalid_json_and_duplicate_fingerprint() -> None:
    unknown = await MediaPlanner(FakeModel(_proposal(primary_evidence_ref="/missing"))).plan(
        _opportunity()
    )
    malformed = await MediaPlanner(FakeModel("not an object")).plan(_opportunity())
    good = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(good, PlannedMedia)
    duplicate = await MediaPlanner(FakeModel(_proposal())).plan(
        _opportunity(), recent_media=[good.plan.diversity_fingerprint] * 12
    )

    assert isinstance(unknown, NotRenderable) and unknown.reason == "unknown_evidence_ref"
    assert isinstance(malformed, NotRenderable) and malformed.reason == "invalid_model_output"
    assert (
        isinstance(duplicate, NotRenderable) and duplicate.reason == "duplicate_recent_fingerprint"
    )


@pytest.mark.asyncio
async def test_matrix_is_a_hard_rule_not_only_prompt_guidance() -> None:
    invalid = _proposal(
        content_domain="food_drink",
        visual_form="full_body",
        share_intent="show_and_tell",
        capture_mode="character_rear_camera",
        character_visibility="none",
        privacy="ordinary",
    )
    result = await MediaPlanner(FakeModel(invalid)).plan(_opportunity(family="life_share"))

    assert isinstance(result, NotRenderable)
    assert result.reason == "matrix_visual_form_conflict"


@pytest.mark.asyncio
async def test_direction_cannot_smuggle_an_unselected_snapshot_fact_into_prompt() -> None:
    proposal = _proposal(
        supporting_evidence_refs=["/objects/0/description"],
        action="在学校附近咖啡馆咬了一口可颂",
    )
    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity())

    assert isinstance(result, NotRenderable)
    assert result.reason == "unselected_fact_in_direction"
    assert result.details == "/location/name"


@pytest.mark.asyncio
async def test_direction_cannot_invent_a_fact_absent_from_snapshot() -> None:
    proposal = _proposal(action="撑着一把不存在于事件中的红色雨伞")
    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity())

    assert isinstance(result, NotRenderable)
    assert result.reason == "unsupported_action_direction"


@pytest.mark.asyncio
async def test_camera_direction_must_match_capture_authorship() -> None:
    proposal = _proposal(camera_direction="固定设备的稳定第三人称视角")
    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity())

    assert isinstance(result, NotRenderable)
    assert result.reason == "capture_camera_direction_conflict"


@pytest.mark.asyncio
async def test_subject_closeup_can_legally_show_only_a_body_detail() -> None:
    proposal = _proposal(
        content_domain="object_possession",
        visual_form="subject_closeup",
        character_visibility="body_detail",
        capture_mode="character_rear_camera",
        primary_evidence_ref="/objects/0/description",
    )
    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity())

    assert isinstance(result, PlannedMedia)


@pytest.mark.asyncio
async def test_planner_exposes_last_twelve_hard_bans_and_last_three_soft_penalties() -> None:
    model = FakeModel(_proposal())
    history = [f"fingerprint:{index}" for index in range(15)]
    result = await MediaPlanner(model).plan(_opportunity(), recent_media=history)

    assert isinstance(result, PlannedMedia)
    prompt = model.messages[1]["content"]
    assert "fingerprint:3" in prompt and "fingerprint:14" in prompt
    assert "soft_penalty_last_3" in prompt
    assert "fingerprint:12" in prompt
    assert "never use URI-fragment form beginning with '#/'" in model.messages[0]["content"]


@pytest.mark.asyncio
async def test_frozen_plan_round_trip_needs_no_model_call() -> None:
    model = FakeModel(_proposal())
    result = await MediaPlanner(model).plan(_opportunity())
    assert isinstance(result, PlannedMedia)

    restored = MediaPlan.from_payload(result.plan.to_payload())

    assert restored == result.plan
    assert model.calls == 1


@pytest.mark.asyncio
async def test_v4_plan_freezes_one_complete_character_presentation_candidate() -> None:
    proposal = _proposal()

    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity())

    assert isinstance(result, PlannedMedia)
    assert result.plan.version == "event-media-plan-v4"
    assert result.plan.intimate_intensity is None
    assert result.plan.embodied_presentation is not None
    assert result.plan.embodied_presentation.physical_salience == "none"
    assert result.plan.embodied_presentation.sensual_charge == "none"
    assert result.plan.embodied_presentation.coverage_mode in {
        "fully_dressed",
        "functional_bodywear",
    }
    assert result.plan.diversity_fingerprint.endswith(
        result.plan.embodied_presentation.action_variant_id
    )
    payload = result.plan.to_payload()
    assert (
        not {
            "action_template_id",
            "action_cue",
            "media_address_strategy",
            "camera_geometry",
            "identity_reference_selection",
        }
        & payload.keys()
    )
    assert MediaPlan.from_payload(payload) == result.plan


@pytest.mark.asyncio
async def test_v4_charged_workout_freezes_evidenced_body_state_and_prompt() -> None:
    snapshot = _snapshot(
        activity={"kind": "workout", "intensity": "high", "description": "训练结束"}
    )
    audience = AudienceContext(recipient_ref="user:geoff", relationship_stage="ambiguous")
    proposal = _proposal(
        content_domain="activity_process",
        share_intent="intimate_signal",
        privacy="intimate",
        tone="playful",
        interaction_bid_id="invite_desire",
        primary_evidence_ref="/activity/description",
        supporting_evidence_refs=["/activity/kind", "/activity/intensity"],
        sharing_motive="传递克制且非露骨的亲密信号",
    )
    result = await MediaPlanner(ChargeSelectingModel(proposal, "charged")).plan(
        _opportunity(
            privacy="intimate",
            snapshot=snapshot,
            sensual_charge_ceiling="charged",
            audience_context=audience,
        )
    )

    assert isinstance(result, PlannedMedia)
    body = result.plan.embodied_presentation
    assert body is not None
    assert body.physical_salience in {"contextual", "foregrounded"}
    assert body.sensual_charge == "charged"
    assert {cue.cue_id for cue in body.physical_cues} & {
        "perspiration",
        "flush",
        "recovering_breath",
    }
    prompt = compile_media_prompt(result.plan, None)
    assert "Frozen embodied presentation" in prompt
    assert "sensual_charge=charged" in prompt
    assert "source=derived" in prompt
    assert "Complete character-photo contract" in prompt
    assert "camera_authorship=" in prompt
    assert "hand_occupancy=" in prompt
    assert "interaction_bid=invite_desire" in prompt


@pytest.mark.asyncio
async def test_renderer_repairs_impossible_camera_hand_action_without_replanning(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        activity={"kind": "workout", "intensity": "high", "description": "训练结束"}
    )
    planned = await MediaPlanner(FakeModel(_proposal())).plan(
        _opportunity(snapshot=snapshot, automatic=True)
    )
    assert isinstance(planned, PlannedMedia)
    first = MediaInspection(
        **{
            **_inspection(True).__dict__,
            "capture_authorship_matches": False,
            "hand_action_contract_matches": False,
        }
    )
    generator = FakeGenerator()

    result = await MediaRenderer(
        generator=generator,
        inspector=FakeInspector([first, _inspection(True)]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert isinstance(result, RenderedMedia)
    assert result.attempts == 2
    assert "capture_authorship_mismatch" in generator.prompts[1]
    assert planned.plan.embodied_presentation is not None
    assert (
        planned.plan.embodied_presentation.action_variant_id in generator.prompts[0]
        and planned.plan.embodied_presentation.action_variant_id in generator.prompts[1]
    )


@pytest.mark.asyncio
async def test_v4_rejects_legacy_intimate_intensity_field() -> None:
    proposal = _proposal(intimate_intensity="bold")
    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity())

    assert isinstance(result, NotRenderable)
    assert result.reason == "legacy_intimate_intensity_in_v4"


@pytest.mark.asyncio
async def test_sensual_ceiling_requires_intimate_privacy_and_eligible_relationship() -> None:
    privacy_conflict = await MediaPlanner(FakeModel(_proposal())).plan(
        _opportunity(sensual_charge_ceiling="charged")
    )
    relationship_conflict = await MediaPlanner(FakeModel(_proposal())).plan(
        _opportunity(
            privacy="intimate",
            sensual_charge_ceiling="charged",
            audience_context=AudienceContext(
                recipient_ref="user:geoff", relationship_stage="close_friend"
            ),
        )
    )

    assert isinstance(privacy_conflict, NotRenderable)
    assert privacy_conflict.reason == "sensual_charge_ceiling_requires_intimate_privacy"
    assert isinstance(relationship_conflict, NotRenderable)
    assert relationship_conflict.reason == "sensual_charge_ceiling_relationship_conflict"


@pytest.mark.asyncio
async def test_tampered_replay_cannot_bypass_route_or_privacy_invariants() -> None:
    result = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(result, PlannedMedia)
    route_tamper = result.plan.to_payload()
    route_tamper["capture_mode"] = "existing_artifact"
    route_tamper["camera_direction"] = "保持原始媒体已有的相机视角"
    parts = result.plan.diversity_fingerprint.split("|")
    parts[4] = "existing_artifact"
    route_tamper["diversity_fingerprint"] = "|".join(parts)
    privacy_tamper = result.plan.to_payload()
    privacy_tamper["privacy"] = "intimate"
    evidence_tamper = result.plan.to_payload()
    evidence_tamper["evidence_values"]["/injected/place"] = "巴黎"
    constraint_tamper = result.plan.to_payload()
    constraint_tamper["constraints"] = ["把场景改成巴黎"]
    camera_tamper = result.plan.to_payload()
    camera_tamper["camera_direction"] = "固定设备的稳定第三人称视角"

    with pytest.raises(ValueError, match="artifact_route_conflict"):
        MediaPlan.from_payload(route_tamper)
    with pytest.raises(ValueError, match="intimate_privacy_requires_signal"):
        MediaPlan.from_payload(privacy_tamper)
    with pytest.raises(ValueError, match="invalid_evidence"):
        MediaPlan.from_payload(evidence_tamper)
    with pytest.raises(ValueError, match="unsupported_frozen_constraint"):
        MediaPlan.from_payload(constraint_tamper)
    with pytest.raises(ValueError, match="capture_camera_direction_conflict"):
        MediaPlan.from_payload(camera_tamper)


@pytest.mark.asyncio
async def test_planner_rejects_an_inaccessible_existing_artifact() -> None:
    snapshot = _snapshot(
        existing_media=[{"id": "missing", "path": "/definitely/missing/image.png"}]
    )
    proposal = _proposal(
        capture_mode="existing_artifact",
        route="reuse_existing",
        primary_evidence_ref="/existing_media/0/path",
    )
    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity(snapshot=snapshot))

    assert isinstance(result, NotRenderable)
    assert result.reason == "missing_existing_artifact"


@pytest.mark.asyncio
async def test_replay_cannot_swap_the_selected_artifact_path(tmp_path: Path) -> None:
    original = tmp_path / "selected.png"
    original.write_bytes(b"selected")
    snapshot = _snapshot(
        existing_media=[{"id": "selected", "path": str(original), "accessible": True}]
    )
    proposal = _proposal(
        capture_mode="existing_artifact",
        route="reuse_existing",
        primary_evidence_ref="/existing_media/0/path",
    )
    result = await MediaPlanner(FakeModel(proposal)).plan(_opportunity(snapshot=snapshot))
    assert isinstance(result, PlannedMedia)
    tampered = result.plan.to_payload()
    tampered["existing_artifact_path"] = "/etc/hosts"

    with pytest.raises(ValueError, match="existing_artifact_path_mismatch"):
        MediaPlan.from_payload(tampered)


@pytest.mark.parametrize("version", ["media-shot-v1", "media-shot-v2", "media-shot-v3"])
def test_legacy_media_shot_v1_v3_can_enter_new_renderer_seam(version: str) -> None:
    from companion_daemon.media_shot import MediaShotPlanner
    from companion_daemon.world_media import WorldMediaDecision

    decision = WorldMediaDecision(
        allowed=True,
        kind="character_media",
        reason="test",
        prompt_topic="咖啡馆",
        requires_deliberation=False,
        capture_mode="handheld_selfie",
        intimacy_tier=None,
    )
    old_snapshot = _snapshot()
    old_snapshot["clock"] = {"logical_at": "2026-07-14T10:00:00+08:00"}
    current = MediaShotPlanner().plan(old_snapshot, decision, "legacy:1").to_payload()
    current["version"] = version
    if version == "media-shot-v1":
        current["motion_class"] = None
        current["motion_cue"] = None
        current["anti_static_constraints"] = []
    if version == "media-shot-v3":
        current["creative_variant_id"] = "legacy-expression"
        current["render_direction"] = "a restrained, shareable expression"
    from companion_daemon.media_shot import MediaShotPlan

    legacy = MediaShotPlan.from_payload(current)

    adapted = LegacyMediaShotAdapter.adapt(
        legacy, opportunity_id="opportunity:legacy", event_id="event:legacy"
    )

    assert MediaPlan.from_payload(adapted.to_payload()) == adapted
    assert adapted.family == "character_media"
    assert adapted.capture_mode == "character_front_camera"


class FakeGenerator:
    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    async def generate(self, prompt: str, *, output_path: Path, **_kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"image-{self.calls}".encode())
        return GeneratedImage(output_path, prompt)


class FakeInspector:
    def __init__(self, results: list[MediaInspection]):
        self.results = results
        self.calls = 0

    async def inspect(self, _path: Path, *, plan: MediaPlan, prompt: str, reference_images=()):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


def _inspection(
    passed: bool, reason: str = "ok", summary: str = "角色拿着可颂在咖啡馆自拍"
) -> MediaInspection:
    return MediaInspection(
        passed=passed,
        reason=reason,
        observed_summary=summary,
        observed_facts=("咖啡馆", "可颂", "角色可识别"),
        deviations=() if passed else (reason,),
        inspector_model="fake",
        rule_version="media-inspection-v1",
        observed_subject_presentation={"gaze_target": "camera"},
        garment_topology_ok=True,
        hand_sleeve_occlusion_ok=True,
        evidence_attachment_ok=True,
        display_strategy_broadly_matches=True,
        expression_artifact_free=True,
        physical_salience_matches=True,
        sensual_charge_broadly_matches=True,
        coverage_mode_matches=True,
        non_explicit_boundary_ok=True,
        body_framing_non_fetishizing=True,
        capture_authorship_matches=True,
        hand_action_contract_matches=True,
        social_bid_broadly_legible=True,
        observed_camera_geometry={"shot_distance": "medium"},
        camera_geometry_broadly_matches=True,
        observed_address_strategy={"engagement_tactic": "presence"},
        address_strategy_broadly_matches=True,
        interaction_bid_legible=True,
        capture_relationship_legible=True,
        generic_portrait_dilution=False,
        photographic_authenticity_ok=True,
        identity_consistency_ok=True,
        observed_expression_family="warm",
        perceptual_signature="presence|medium|eye|left_three_quarter|balanced|warm",
        observed_facial_display_strategy="warm_connection",
        facial_display_strategy_matches=True,
        observed_facial_actions={"mouth": "small_smile", "nose_cheek": "cheek_lift"},
        facial_micro_performance_matches=True,
        generic_smile_fallback=False,
        reference_expression_copy_detected=False,
        authenticity_profile_matches=True,
        commercial_render_dilution=False,
        regional_grounding_matches=True,
        observed_authenticity={"aesthetic_intent": "pleasant_share"},
        moment_capture_matches=True,
    )


@pytest.mark.asyncio
async def test_v5_renderer_repairs_generic_portrait_without_replanning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COMPANION_EVENT_MEDIA_V5_ENABLED", "1")
    proposal = _proposal(interaction_bid_id="share_presence")
    for field in (
        "composition",
        "action",
        "camera_direction",
        "sharing_motive",
        "subject_variant_id",
    ):
        proposal.pop(field, None)
    planned = await MediaPlanner(V5SelectingModel(proposal)).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    rejected = MediaInspection(
        **{
            **_inspection(True).__dict__,
            "rule_version": "media-inspection-v7",
            "generic_portrait_dilution": True,
        }
    )
    accepted = MediaInspection(
        **{**_inspection(True).__dict__, "rule_version": "media-inspection-v7"}
    )
    generator = FakeGenerator()

    rendered = await MediaRenderer(
        generator=generator,
        inspector=FakeInspector([rejected, accepted]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert isinstance(rendered, RenderedMedia)
    assert rendered.attempts == 2
    assert "generic_portrait_dilution" in generator.prompts[1]
    assert planned.plan.plan_id in generator.prompts[1]


@pytest.mark.asyncio
async def test_v5_renderer_repairs_generic_smile_and_commercial_render_dilution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COMPANION_EVENT_MEDIA_V5_ENABLED", "1")
    proposal = _proposal(interaction_bid_id="share_presence")
    for field in ("composition", "action", "camera_direction", "sharing_motive", "subject_variant_id"):
        proposal.pop(field, None)
    planned = await MediaPlanner(V5SelectingModel(proposal)).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    rejected = MediaInspection(
        **{
            **_inspection(True).__dict__,
            "rule_version": "media-inspection-v7",
            "generic_smile_fallback": True,
            "commercial_render_dilution": True,
        }
    )
    accepted = MediaInspection(**{**_inspection(True).__dict__, "rule_version": "media-inspection-v7"})
    generator = FakeGenerator()

    rendered = await MediaRenderer(
        generator=generator,
        inspector=FakeInspector([rejected, accepted]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert isinstance(rendered, RenderedMedia)
    assert rendered.attempts == 2
    assert "generic_smile_fallback" in generator.prompts[1]
    assert "commercial_render_dilution" in generator.prompts[1]
    assert "same frozen facial" in generator.prompts[1]


@pytest.mark.asyncio
async def test_complete_candidates_cross_filter_body_action_against_camera_hand_contract() -> None:
    snapshot = _snapshot(
        activity={"kind": "workout", "intensity": "high", "description": "训练结束"}
    )
    model = FakeModel(_proposal())

    result = await MediaPlanner(model).plan(_opportunity(snapshot=snapshot))

    assert isinstance(result, PlannedMedia)
    marker = "legal_character_presentation_candidates="
    encoded = model.messages[-1]["content"].split(marker, 1)[1].split("\n", 1)[0]
    candidates = json.loads(encoded)
    front_camera = [
        item for item in candidates if "character_front_camera" in item["legal_capture_modes"]
    ]
    assert front_camera
    assert all(item["embodied_presentation"]["required_free_hands"] <= 1 for item in front_camera)
    assert all(
        item["capture_physics_contracts"]["character_front_camera"]["camera_authorship"]
        == "character_holds_capture_device"
        for item in front_camera
    )
    assert all(item["subject_presentation"]["display_strategy"] for item in candidates)


@pytest.mark.asyncio
async def test_renderer_repairs_charged_image_that_dilutes_to_ordinary_portrait(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        activity={"kind": "workout", "intensity": "high", "description": "训练结束"}
    )
    audience = AudienceContext(recipient_ref="user:geoff", relationship_stage="ambiguous")
    proposal = _proposal(
        content_domain="activity_process",
        share_intent="intimate_signal",
        privacy="intimate",
        interaction_bid_id="invite_desire",
        primary_evidence_ref="/activity/description",
        supporting_evidence_refs=["/activity/kind", "/activity/intensity"],
        sharing_motive="传递克制且非露骨的亲密信号",
    )
    planned = await MediaPlanner(ChargeSelectingModel(proposal, "charged")).plan(
        _opportunity(
            privacy="intimate",
            snapshot=snapshot,
            sensual_charge_ceiling="charged",
            audience_context=audience,
            automatic=True,
        )
    )
    assert isinstance(planned, PlannedMedia)
    diluted = _inspection(True)
    diluted = MediaInspection(
        **{
            **diluted.__dict__,
            "sensual_charge_broadly_matches": False,
        }
    )
    generator = FakeGenerator()
    result = await MediaRenderer(
        generator=generator,
        inspector=FakeInspector([diluted, _inspection(True)]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert isinstance(result, RenderedMedia)
    assert result.attempts == 2
    assert "sensual_charge_mismatch" in generator.prompts[1]


@pytest.mark.asyncio
async def test_renderer_generates_inspects_and_repairs_same_plan(tmp_path: Path) -> None:
    planned = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    generator = FakeGenerator()
    renderer = MediaRenderer(
        generator=generator,
        inspector=FakeInspector([_inspection(False, "手指畸形"), _inspection(True)]),
        output_dir=tmp_path,
        visual_identity_path=None,
    )

    result = await renderer.render(planned.plan)

    assert isinstance(result, RenderedMedia)
    assert result.attempts == 2
    assert result.inspection.observed_summary
    assert "手指畸形" in generator.prompts[1]
    assert planned.plan.event_id in generator.prompts[0]


@pytest.mark.asyncio
async def test_renderer_rejects_reference_pose_copy_and_repairs_once(tmp_path: Path) -> None:
    planned = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    copied = MediaInspection(
        passed=True,
        reason="ok",
        observed_summary="角色姿态与身份参考图几乎一致",
        observed_facts=("角色可识别",),
        deviations=(),
        inspector_model="fake",
        observed_subject_presentation={"gaze_target": "lens"},
        reference_pose_copy=True,
    )
    generator = FakeGenerator()

    result = await MediaRenderer(
        generator=generator,
        inspector=FakeInspector([copied, _inspection(True)]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert isinstance(result, RenderedMedia)
    assert result.attempts == 2
    assert "reference_pose_copy" in generator.prompts[1]


@pytest.mark.asyncio
async def test_renderer_repairs_garment_and_evidence_attachment_defects(tmp_path: Path) -> None:
    planned = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    defective = MediaInspection(
        passed=True,
        reason="ok",
        observed_summary="角色展示袖口上的物品",
        observed_facts=("角色可识别",),
        deviations=(),
        inspector_model="fake",
        observed_subject_presentation={"gesture": "show_primary_evidence"},
        garment_topology_ok=False,
        hand_sleeve_occlusion_ok=False,
        evidence_attachment_ok=False,
    )
    generator = FakeGenerator()

    result = await MediaRenderer(
        generator=generator,
        inspector=FakeInspector([defective, _inspection(True)]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert isinstance(result, RenderedMedia)
    assert result.attempts == 2
    assert "garment_topology_failed" in generator.prompts[1]
    assert "hand_sleeve_occlusion" in generator.prompts[1]
    assert "evidence_attachment" in generator.prompts[1]


@pytest.mark.asyncio
async def test_renderer_repairs_contradicted_photo_display_strategy(tmp_path: Path) -> None:
    planned = await MediaPlanner(
        FakeModel(
            _proposal(
                interaction_bid_id="share_discovery",
                subject_variant_id="aware_three_quarter",
            )
        )
    ).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    contradicted = MediaInspection(
        passed=True,
        reason="ok",
        observed_summary="角色大笑着展示食物",
        observed_facts=("角色可识别",),
        deviations=(),
        inspector_model="fake",
        observed_subject_presentation={"expression": "broad_smile"},
        garment_topology_ok=True,
        hand_sleeve_occlusion_ok=True,
        evidence_attachment_ok=True,
        observed_photo_display_strategy="commercial_smile",
        display_strategy_broadly_matches=False,
        expression_artifact_free=True,
        salient_expression_cues=("broad smile",),
        forbidden_expression_cues=("commercial_smile",),
    )
    accepted = MediaInspection(
        passed=True,
        reason="ok",
        observed_summary="角色温和自然地把早餐分享给熟悉的人",
        observed_facts=("角色可识别",),
        deviations=(),
        inspector_model="fake",
        observed_subject_presentation={"expression": "warm_include_you"},
        garment_topology_ok=True,
        hand_sleeve_occlusion_ok=True,
        evidence_attachment_ok=True,
        observed_photo_display_strategy="warm_include_you",
        display_strategy_broadly_matches=True,
        expression_artifact_free=True,
        salient_expression_cues=("small warm smile", "friendly gaze"),
        forbidden_expression_cues=(),
    )
    generator = FakeGenerator()

    result = await MediaRenderer(
        generator=generator,
        inspector=FakeInspector([contradicted, accepted]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert isinstance(result, RenderedMedia)
    assert result.attempts == 2
    assert "display_strategy_contradiction" in generator.prompts[1]
    assert "same social performance" in generator.prompts[1]


@pytest.mark.asyncio
async def test_renderer_keeps_references_for_adapter_without_quality_parameter(
    tmp_path: Path,
) -> None:
    class ComfyLikeGenerator:
        references: tuple[Path, ...] = ()

        async def generate(
            self,
            prompt: str,
            *,
            output_path: Path,
            size: str,
            reference_images=(),
        ) -> GeneratedImage:
            self.references = tuple(reference_images)
            output_path.write_bytes(b"image")
            return GeneratedImage(output_path, prompt)

    planned = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    reference = tmp_path / "identity.png"
    reference.write_bytes(b"reference")
    generator = ComfyLikeGenerator()
    renderer = MediaRenderer(
        generator=generator,
        inspector=FakeInspector([_inspection(True)]),
        output_dir=tmp_path,
        visual_identity_path=None,
    )
    renderer._references = lambda _plan: (reference,)  # type: ignore[method-assign]

    result = await renderer.render(planned.plan)

    assert isinstance(result, RenderedMedia)
    assert generator.references == (reference,)


@pytest.mark.asyncio
async def test_existing_artifact_is_not_generated_and_is_inspected(tmp_path: Path) -> None:
    original = tmp_path / "original.png"
    original.write_bytes(b"original")
    snapshot = _snapshot(existing_media=[{"id": "media:original", "path": str(original)}])
    proposal = _proposal(
        capture_mode="existing_artifact",
        route="reuse_existing",
        primary_evidence_ref="/existing_media/0/path",
    )
    planned = await MediaPlanner(FakeModel(proposal)).plan(_opportunity(snapshot=snapshot))
    assert isinstance(planned, PlannedMedia)
    generator = FakeGenerator()

    result = await MediaRenderer(
        generator=generator,
        inspector=FakeInspector([_inspection(True)]),
        output_dir=tmp_path / "output",
        visual_identity_path=None,
    ).render(planned.plan)

    assert isinstance(result, RenderedMedia)
    assert result.path == original
    assert result.artifact_hash
    assert generator.calls == 0


@pytest.mark.asyncio
async def test_automatic_render_fails_closed_without_observed_summary(tmp_path: Path) -> None:
    planned = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity(automatic=True))
    assert isinstance(planned, PlannedMedia)
    result = await MediaRenderer(
        generator=FakeGenerator(),
        inspector=FakeInspector([_inspection(True, summary=""), _inspection(True, summary="")]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert not isinstance(result, RenderedMedia)
    assert result.reason == "inspection_summary_missing"


@pytest.mark.asyncio
async def test_automatic_v3_render_requires_structural_quality_fields(tmp_path: Path) -> None:
    planned = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity(automatic=True))
    assert isinstance(planned, PlannedMedia)
    incomplete = MediaInspection(
        passed=True,
        reason="ok",
        observed_summary="角色在咖啡馆展示食物",
        observed_facts=("角色可识别",),
        deviations=(),
        inspector_model="fake",
        observed_subject_presentation={"gesture": "show_primary_evidence"},
    )

    result = await MediaRenderer(
        generator=FakeGenerator(),
        inspector=FakeInspector([incomplete, incomplete]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert not isinstance(result, RenderedMedia)
    assert result.reason == "inspection_quality_fields_missing"


@pytest.mark.asyncio
async def test_automatic_v3_render_requires_social_performance_fields(tmp_path: Path) -> None:
    planned = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity(automatic=True))
    assert isinstance(planned, PlannedMedia)
    incomplete = MediaInspection(
        passed=True,
        reason="ok",
        observed_summary="角色在咖啡馆自然地分享早餐",
        observed_facts=("角色可识别",),
        deviations=(),
        inspector_model="fake",
        observed_subject_presentation={"gesture": "show_primary_evidence"},
        garment_topology_ok=True,
        hand_sleeve_occlusion_ok=True,
        evidence_attachment_ok=True,
    )

    result = await MediaRenderer(
        generator=FakeGenerator(),
        inspector=FakeInspector([incomplete, incomplete]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(planned.plan)

    assert not isinstance(result, RenderedMedia)
    assert result.reason == "inspection_social_performance_fields_missing"


@pytest.mark.asyncio
async def test_legacy_v2_plan_keeps_structural_quality_gate_after_v3_upgrade(
    tmp_path: Path,
) -> None:
    current = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity(automatic=True))
    assert isinstance(current, PlannedMedia)
    payload = current.plan.to_payload()
    payload["version"] = "event-media-plan-v2"
    payload["interaction_bid"] = None
    payload["embodied_presentation"] = None
    payload["diversity_fingerprint"] = "|".join(current.plan.diversity_fingerprint.split("|")[:8])
    subject = payload["subject_presentation"]
    assert isinstance(subject, dict)
    subject.pop("version")
    subject.pop("display_strategy")
    appearance = subject["appearance"]
    performance = subject["performance"]
    assert isinstance(appearance, dict) and isinstance(performance, dict)
    subject["subject_signature"] = "|".join(
        str(value)
        for value in (
            appearance["hair_arrangement"],
            performance["head_yaw"],
            performance["head_pitch"],
            performance["head_roll"],
            performance["gaze_target"],
            performance["expression"],
            performance["shoulder_orientation"],
            performance["gesture"],
        )
    )
    restored = MediaPlan.from_payload(payload)
    incomplete = MediaInspection(
        passed=True,
        reason="ok",
        observed_summary="角色在咖啡馆展示早餐",
        observed_facts=("角色可识别",),
        deviations=(),
        inspector_model="fake",
        observed_subject_presentation={"gesture": "show_primary_evidence"},
    )

    result = await MediaRenderer(
        generator=FakeGenerator(),
        inspector=FakeInspector([incomplete, incomplete]),
        output_dir=tmp_path,
        visual_identity_path=None,
    ).render(restored)

    assert not isinstance(result, RenderedMedia)
    assert result.reason == "inspection_quality_fields_missing"


@pytest.mark.asyncio
async def test_openai_inspector_returns_actual_summary_in_same_visual_call(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "passed": True,
                                    "reason": "ok",
                                    "observed_summary": "角色在咖啡馆举着吃过一口的可颂自拍。",
                                    "observed_facts": ["咖啡馆", "可颂", "自拍"],
                                    "deviations": [],
                                    "garment_topology_ok": True,
                                    "hand_sleeve_occlusion_ok": True,
                                    "evidence_attachment_ok": True,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    planned = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    image = tmp_path / "image.png"
    image.write_bytes(b"png")
    inspector = OpenAIMediaInspector("key", transport=httpx.MockTransport(handler))

    result = await inspector.inspect(image, plan=planned.plan, prompt="ignored")

    assert result.passed and result.observed_summary.startswith("角色在咖啡馆")
    assert result.garment_topology_ok is True
    assert result.hand_sleeve_occlusion_ok is True
    assert result.evidence_attachment_ok is True
    content = observed["payload"]["messages"][0]["content"]  # type: ignore[index]
    assert "observed_summary" in content[0]["text"]
    assert "garment_topology_ok" in content[0]["text"]


@pytest.mark.asyncio
async def test_v5_inspection_prompt_keeps_late_contracts_with_large_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPANION_EVENT_MEDIA_V5_ENABLED", "1")
    proposal = _proposal()
    for field in (
        "composition",
        "action",
        "camera_direction",
        "sharing_motive",
        "subject_variant_id",
    ):
        proposal.pop(field, None)
    planned = await MediaPlanner(V5SelectingModel(proposal)).plan(_opportunity())
    assert isinstance(planned, PlannedMedia)
    expanded = replace(
        planned.plan,
        evidence_values={
            pointer: ("很长的世界事实" * 2000 if index == 0 else value)
            for index, (pointer, value) in enumerate(planned.plan.evidence_values.items())
        },
    )

    prompt = _inspection_prompt(expanded)

    assert '"facial_micro_performance"' in prompt
    assert '"photographic_authenticity"' in prompt
    assert '"camera_face_distance"' in prompt
    assert len(prompt) < 20_000


def test_body_detail_v5_inspection_does_not_require_visible_facial_fields() -> None:
    no_face = replace(
        _inspection(True),
        observed_expression_family="",
        observed_facial_display_strategy="",
        facial_display_strategy_matches=None,
        observed_facial_actions={},
        facial_micro_performance_matches=None,
        generic_smile_fallback=None,
        reference_expression_copy_detected=None,
    )

    result = _enforce_inspection_contract(
        no_face,
        automatic=True,
        subject_required=True,
        quality_required=True,
        embodied_required=True,
        capture_contract_required=True,
        v5_required=True,
        enhanced_v5_required=True,
        facial_contract_required=False,
    )

    assert result.passed


def test_pre_extension_v5_does_not_adopt_new_inspection_rejection_semantics() -> None:
    legacy_observation = replace(
        _inspection(True),
        facial_display_strategy_matches=False,
        facial_micro_performance_matches=False,
        generic_smile_fallback=True,
        authenticity_profile_matches=False,
        commercial_render_dilution=True,
    )

    result = _enforce_inspection_contract(
        legacy_observation,
        automatic=True,
        subject_required=True,
        quality_required=True,
        v5_required=True,
        enhanced_v5_required=False,
        facial_contract_required=False,
    )

    assert result.passed
