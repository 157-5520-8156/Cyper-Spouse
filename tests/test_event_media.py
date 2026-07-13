import json
from pathlib import Path

import httpx
import pytest

from companion_daemon.event_media import (
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
) -> MediaOpportunity:
    return MediaOpportunity(
        opportunity_id="opportunity:42",
        family=family,
        privacy_ceiling=privacy,
        event_snapshot=snapshot or _snapshot(),
        delivery_mode="automatic" if automatic else "preview",
    )


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
        value["subject_variant_id"] = "body_detail_showcase" if value["character_visibility"] == "body_detail" else {
            "character_front_camera": "aware_three_quarter",
            "character_rear_camera": "aware_three_quarter",
            "mirror": "mirror_composed",
            "timer_fixed": "timer_environment_pose",
            "requested_helper": "helper_checkin_pose",
            "known_companion": "companion_reaction",
            "external_sender": "external_candid_glance",
            "existing_artifact": "aware_three_quarter",
        }[str(value["capture_mode"])]
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
        return json.dumps(self.payload, ensure_ascii=False)


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
    model = FakeModel(_proposal(subject_variant_id="screen_check_reaction"))

    result = await MediaPlanner(model).plan(_opportunity())

    assert isinstance(result, PlannedMedia)
    assert result.plan.version == "event-media-plan-v2"
    assert result.plan.subject_presentation is not None
    assert result.plan.subject_presentation.variant_id == "screen_check_reaction"
    assert "legal_subject_presentation_candidates" in model.messages[1]["content"]
    restored = MediaPlan.from_payload(result.plan.to_payload())
    assert restored == result.plan
    prompt = compile_media_prompt(restored, Path("configs/visual_identity.yaml"))
    assert "Frozen subject presentation" in prompt
    assert "Do not copy their head angle" in prompt
    assert prompt.rfind("Frozen subject presentation") > prompt.rfind("Character identity anchor")


@pytest.mark.asyncio
async def test_generated_character_plan_requires_legal_subject_variant() -> None:
    missing = _proposal()
    missing.pop("subject_variant_id")
    missing_result = await MediaPlanner(FakeModel(missing)).plan(_opportunity())
    illegal_result = await MediaPlanner(
        FakeModel(_proposal(subject_variant_id="mirror_composed"))
    ).plan(_opportunity())

    assert isinstance(missing_result, NotRenderable)
    assert missing_result.reason == "missing_subject_variant"
    assert isinstance(illegal_result, NotRenderable)
    assert illegal_result.reason == "illegal_subject_variant"


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

    result = await MediaPlanner(
        model, subject_config_path=tmp_path / "missing-subjects.yaml"
    ).plan(_opportunity())

    assert isinstance(result, NotRenderable)
    assert result.reason == "subject_catalog_unavailable"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_v1_plan_replays_without_new_subject_interpretation() -> None:
    current = await MediaPlanner(FakeModel(_proposal())).plan(_opportunity())
    assert isinstance(current, PlannedMedia)
    payload = current.plan.to_payload()
    payload["version"] = "event-media-plan-v1"
    payload["subject_presentation"] = None

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
            "tone": "tender",
            "intimate_intensity": "soft",
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
    result = await MediaPlanner(FakeModel(_proposal(**overrides))).plan(
        _opportunity(family="character_media", privacy="intimate")
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
    )


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
async def test_automatic_v2_render_requires_structural_quality_fields(tmp_path: Path) -> None:
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
