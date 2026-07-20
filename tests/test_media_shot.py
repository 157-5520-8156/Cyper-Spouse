import pytest

from companion_daemon.media_shot import MediaShotDirector, MediaShotPlanner, is_valid_media_shot_plan
from companion_daemon.world_media import WorldMediaDecision


def _decision(capture_mode: str = "check_in_timer") -> WorldMediaDecision:
    return WorldMediaDecision(
        True,
        "character_media",
        "world_relationship_allows_personal_media",
        capture_mode=capture_mode,  # type: ignore[arg-type]
    )


def _exploring_snapshot() -> dict[str, object]:
    return {
        "clock": {"logical_at": "2026-07-13T14:30:00+08:00"},
        "agenda": {
            "exhibition": {
                "activity_id": "exhibition",
                "status": "active",
                "template_id": "photo_portfolio",
                "location": "上海",
                "title": "摄影作品展览",
                "companions": ["photography-zhou"],
            }
        },
        "media": {},
    }


def test_shot_plan_is_replayable_and_keeps_active_activity_evidence() -> None:
    planner = MediaShotPlanner()
    snapshot = _exploring_snapshot()

    first = planner.plan(snapshot, _decision(), "media:exhibition:one")
    replay = planner.plan(snapshot, _decision(), "media:exhibition:one")

    assert first == replay
    assert first.source_activity_id == "exhibition"
    assert first.source_template_id == "photo_portfolio"
    assert first.location == "上海"
    assert first.scene_category == "exploring"
    assert any("No arm reaches toward the camera" in item for item in first.constraints)
    assert first.motion_class in {"transitional", "interaction", "observational", "candid"}
    assert first.motion_cue
    assert first.anti_static_constraints
    assert "Frozen world media shot plan" in first.prompt_block()
    assert "Motion requirement" in first.prompt_block()


def test_no_active_activity_cannot_turn_request_text_into_a_trip_fact() -> None:
    plan = MediaShotPlanner().plan(
        {"clock": {"logical_at": "2026-07-13T14:30:00+08:00"}, "agenda": {}, "media": {}},
        _decision(),
        "media:no-active-trip",
    )

    assert plan.source_activity_id is None
    assert plan.location is None
    assert any("Do not portray a trip" in item for item in plan.constraints)


def test_recent_media_plan_excludes_repeated_pose_fingerprint() -> None:
    planner = MediaShotPlanner()
    snapshot = _exploring_snapshot()
    first = planner.plan(snapshot, _decision(), "media:repeat:one")
    snapshot["media"] = {"one": {"status": "shared", "shot_plan": first.to_payload()}}

    second = planner.plan(snapshot, _decision(), "media:repeat:two")

    assert second.diversity_fingerprint != first.diversity_fingerprint


def test_different_request_seeds_offer_multiple_legal_variants() -> None:
    planner = MediaShotPlanner()
    snapshot = _exploring_snapshot()

    plans = [planner.plan(snapshot, _decision(), f"media:variant:{index}") for index in range(12)]

    assert len({plan.template_id for plan in plans}) > 1
    assert all(plan.source_activity_id == "exhibition" for plan in plans)
    assert len({plan.motion_class for plan in plans}) > 1


def test_v1_shot_plan_payload_remains_recoverable() -> None:
    legacy = MediaShotPlanner().plan(_exploring_snapshot(), _decision(), "media:legacy").to_payload()
    legacy["version"] = "media-shot-v1"
    legacy.pop("motion_class", None)
    legacy.pop("motion_cue", None)
    legacy.pop("anti_static_constraints", None)

    from companion_daemon.media_shot import MediaShotPlan, is_valid_media_shot_plan

    assert is_valid_media_shot_plan(legacy)
    recovered = MediaShotPlan.from_payload(legacy)
    assert recovered.motion_class is None
    assert "Motion requirement" not in recovered.prompt_block()


def test_candid_plan_requires_registered_companion_evidence() -> None:
    snapshot = _exploring_snapshot()
    snapshot["agenda"] = {"exhibition": {**snapshot["agenda"]["exhibition"], "companions": []}}  # type: ignore[index]

    with pytest.raises(ValueError, match="registered companion"):
        MediaShotPlanner().plan(snapshot, _decision("candid_life"), "media:no-companion")


def test_check_in_helper_freezes_a_requested_photo_authorship_not_a_timer() -> None:
    plan = MediaShotPlanner().plan(
        _exploring_snapshot(), _decision("check_in_helper"), "media:helper-check-in"
    )

    assert plan.camera_authorship
    assert "help" in plan.camera_authorship
    assert plan.sharing_motive
    assert "Camera authorship" in plan.prompt_block()
    assert "Sharing impulse" in plan.prompt_block()
    assert any("helper" in item.casefold() for item in plan.constraints)


def test_unfiltered_resting_media_is_a_selfie_with_a_safe_mess_context() -> None:
    snapshot = {
        "clock": {"logical_at": "2026-07-13T21:30:00+08:00"},
        "agenda": {
            "home": {
                "activity_id": "home", "status": "active", "template_id": "independent_sort",
                "location": "住处", "title": "整理一天的东西",
            }
        },
        "media": {},
    }
    plan = MediaShotPlanner().plan(snapshot, _decision("unfiltered"), "media:messy-selfie")

    assert plan.camera_authorship == "the character holding her own front-facing phone"
    assert any(token in str(plan.sharing_motive) for token in ("sorting", "tidying"))
    assert "front-camera" in plan.camera_angle
    assert any("front camera herself" in item for item in plan.constraints)
    assert "harmless pile" in " ".join(plan.environment_cues)


def test_unfiltered_sorting_selfies_vary_the_sharing_impulse_without_losing_camera_authorship() -> None:
    snapshot = {
        "clock": {"logical_at": "2026-07-13T21:30:00+08:00"},
        "agenda": {
            "home": {
                "activity_id": "home", "status": "active", "template_id": "independent_sort",
                "location": "住处", "title": "整理一天的东西",
            }
        },
        "media": {},
    }
    plans = [
        MediaShotPlanner().plan(snapshot, _decision("unfiltered"), f"media:sorting:{index}")
        for index in range(16)
    ]

    assert {plan.template_id for plan in plans} >= {
        "resting-unfiltered", "resting-sorting-complaint-selfie"
    }
    assert all(plan.camera_authorship == "the character holding her own front-facing phone" for plan in plans)
    assert all("front-camera" in plan.camera_angle for plan in plans)
    assert all("externally photographed" in " ".join(plan.anti_static_constraints) for plan in plans)
    assert len({plan.sharing_motive for plan in plans}) >= 2


@pytest.mark.asyncio
async def test_llm_director_can_only_enrich_a_frozen_plan_with_an_allowed_variant() -> None:
    class DirectorModel:
        calls: list[list[dict[str, str]]] = []

        async def complete_json(self, messages, *, temperature=0.8):
            self.calls.append(messages)
            return (
                '{"variant_id":"light-pose-check-in",'
                '"render_direction":"Let her give the timer a small, relaxed half-smile while keeping the turn in motion."}'
            )

    base = MediaShotPlanner().plan(_exploring_snapshot(), _decision(), "media:directed")
    directed = await MediaShotDirector(DirectorModel()).direct(base)

    assert directed.version == "media-shot-v3"
    assert directed.creative_variant_id == "light-pose-check-in"
    assert directed.render_direction
    assert directed.source_activity_id == base.source_activity_id
    assert directed.location == base.location
    assert directed.capture_mode == base.capture_mode
    assert directed.action == base.action
    assert is_valid_media_shot_plan(directed.to_payload())
    assert "Creative rendering direction (light-pose-check-in)" in directed.prompt_block()


@pytest.mark.asyncio
async def test_llm_director_rejects_new_world_facts_and_keeps_a_safe_catalog_fallback() -> None:
    class HallucinatingDirector:
        async def complete(self, _messages, *, temperature=0.8):
            return (
                '{"variant_id":"light-pose-check-in",'
                '"render_direction":"At 上海摄影艺术中心, smile toward a friend beside the exhibit."}'
            )

    base = MediaShotPlanner().plan(_exploring_snapshot(), _decision(), "media:director-fallback")
    directed = await MediaShotDirector(HallucinatingDirector()).direct(base)

    assert directed.version == "media-shot-v3"
    assert directed.creative_variant_id in {"light-pose-check-in", "atmospheric-check-in", "playful-check-in"}
    assert directed.render_direction
    assert "上海" not in directed.render_direction
    assert "friend" not in directed.render_direction.casefold()
