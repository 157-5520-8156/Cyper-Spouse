from pathlib import Path

import yaml

from companion_daemon.media_subject import (
    SubjectAppearance,
    SubjectPerformance,
    SubjectPresentationPlan,
    build_subject_candidates,
    load_subject_catalog,
    presentation_prompt_block,
    select_identity_references,
)


CONFIG = Path("configs/media_subject_templates.yaml")


def _snapshot(*, appearance_state=None):
    character = {"emotion": "amused", "energy": "normal"}
    if appearance_state is not None:
        character["appearance_state"] = appearance_state
    return {
        "event": {"event_id": "event:meal", "status": "committed"},
        "activity": {"kind": "cooking", "description": "刚把面盛出来"},
        "character": character,
    }


def test_catalog_builds_deterministic_coherent_candidates() -> None:
    first = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:1",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
    )
    second = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:1",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
    )

    assert first == second
    assert len(first) >= 3
    assert all(item.presentation.appearance.source == "media_local" for item in first)
    assert all(item.presentation.performance.photo_awareness for item in first)
    assert len({item.presentation.subject_signature for item in first}) == len(first)


def test_world_appearance_is_frozen_without_media_local_override() -> None:
    candidates = build_subject_candidates(
        snapshot=_snapshot(
            appearance_state={
                "hair_arrangement": "low_ponytail",
                "outfit_role": "navy_cardigan_and_cream_top",
                "grooming": "natural",
                "accessories": ["teal_hair_clip"],
            }
        ),
        opportunity_id="op:world-look",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
    )

    assert candidates
    for candidate in candidates:
        appearance = candidate.presentation.appearance
        assert appearance.source == "world_fact"
        assert appearance.hair_arrangement == "low_ponytail"
        assert appearance.outfit_role == "navy_cardigan_and_cream_top"
        assert appearance.evidence_refs == ("/character/appearance_state",)


def test_partial_world_appearance_does_not_invent_missing_world_facts() -> None:
    candidates = build_subject_candidates(
        snapshot=_snapshot(appearance_state={"hair_arrangement": "low_ponytail"}),
        opportunity_id="op:partial-look",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
    )

    assert candidates
    assert all(item.presentation.appearance.source == "media_local" for item in candidates)


def test_recent_subject_signature_is_hard_filtered() -> None:
    baseline = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:repeat",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
    )
    blocked = baseline[0].presentation.subject_signature

    filtered = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:repeat",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        recent_subject_signatures=(blocked,),
        config_path=CONFIG,
    )

    assert blocked not in {item.presentation.subject_signature for item in filtered}


def test_body_detail_uses_partial_non_face_presentation() -> None:
    candidates = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:detail",
        capture_mode="character_rear_camera",
        character_visibility="body_detail",
        config_path=CONFIG,
    )

    assert candidates
    assert {item.presentation.performance.gaze_target for item in candidates} == {"not_applicable"}
    assert all("detail" in item.variant_id for item in candidates)


def test_subject_presentation_payload_round_trip() -> None:
    presentation = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:roundtrip",
        capture_mode="mirror",
        character_visibility="identifiable",
        config_path=CONFIG,
    )[0].presentation

    assert SubjectPresentationPlan.from_payload(presentation.to_payload()) == presentation


def test_legacy_v2_subject_payload_without_hand_fields_still_loads() -> None:
    presentation = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:legacy-v2",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
    )[0].presentation
    payload = presentation.to_payload()
    performance = payload["performance"]
    assert isinstance(performance, dict)
    performance.pop("hand_occupancy")
    performance.pop("occlusion_complexity")

    restored = SubjectPresentationPlan.from_payload(payload)

    assert restored.performance.hand_occupancy == "unspecified"
    assert restored.performance.occlusion_complexity == "unknown"


def test_capture_mode_derives_hand_occupancy_and_occlusion_risk() -> None:
    selfie = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:hands",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
    )
    timer = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:hands",
        capture_mode="timer_fixed",
        character_visibility="identifiable",
        config_path=CONFIG,
    )

    aware = next(item for item in selfie if item.variant_id == "aware_three_quarter")
    timed = next(item for item in timer if item.variant_id == "timer_environment_pose")
    assert aware.presentation.performance.hand_occupancy == (
        "one_hand_operates_phone_other_presents_evidence"
    )
    assert aware.presentation.performance.occlusion_complexity == "medium"
    assert timed.presentation.performance.hand_occupancy == "both_hands_available"
    assert timed.presentation.performance.occlusion_complexity == "low"


def test_configured_high_occlusion_is_downranked_but_remains_legal(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    aware = next(item for item in raw["variants"] if item["id"] == "aware_three_quarter")
    aware["performance"]["occlusion_complexity"] = "high"
    configured = tmp_path / "subjects.yaml"
    configured.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    candidates = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:high-risk",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=configured,
    )

    selected = next(item for item in candidates if item.variant_id == "aware_three_quarter")
    assert selected.presentation.performance.occlusion_complexity == "high"
    assert candidates.index(selected) > 0


def test_prompt_compiles_internal_enums_to_visible_instructions() -> None:
    appearance = SubjectAppearance(
        source="media_local",
        hair_arrangement="clipped_back",
        outfit_role="event_appropriate_casual",
        grooming="natural",
        accessories=("teal_hair_clip",),
    )
    performance = SubjectPerformance(
        head_yaw="toward_frame_right",
        head_pitch="level",
        head_roll="none",
        gaze_target="lens",
        expression="trying_not_to_laugh",
        shoulder_orientation="three_quarter_opposite_head",
        posture="relaxed_engaged",
        gesture="show_primary_evidence",
        photo_awareness="aware_light_pose",
        hand_occupancy="one_hand_operates_phone_other_presents_evidence",
        occlusion_complexity="medium",
    )
    signature = (
        "clipped_back|toward_frame_right|level|none|lens|trying_not_to_laugh|"
        "three_quarter_opposite_head|show_primary_evidence"
    )

    prompt = presentation_prompt_block(
        SubjectPresentationPlan("test", appearance, performance, signature),
        config_path=CONFIG,
    )

    assert "front and side strands visibly secured away from the face" in prompt
    assert "one hand operates the phone" in prompt
    assert "hair: clipped_back" not in prompt


def test_reference_selector_avoids_copying_planned_pose(tmp_path: Path) -> None:
    identity_config = tmp_path / "identity.yaml"
    same = tmp_path / "canonical.png"
    different = tmp_path / "thoughtful.png"
    same.write_bytes(b"same")
    different.write_bytes(b"different")
    identity_config.write_text(
        "reference_asset: " + str(same) + "\n"
        "reference_sets:\n"
        "  everyday_selfie:\n"
        f"    - {same}\n"
        f"    - {different}\n"
        "name: test\nanchor_prompt: test\nselfie_style: test\nnegative_prompt: test\n",
        encoding="utf-8",
    )
    catalog_path = tmp_path / "subjects.yaml"
    catalog_path.write_text(
        "version: 1\nvariants: []\nreference_pose_metadata:\n"
        f"  '{same}': {{head_yaw: near_front, gaze_target: lens, expression: soft_closed_smile}}\n"
        f"  '{different}': {{head_yaw: toward_frame_left, gaze_target: primary_evidence, expression: thoughtful}}\n",
        encoding="utf-8",
    )
    payload = {
        "variant_id": "planned",
        "appearance": {
            "source": "media_local",
            "hair_arrangement": "natural_down",
            "outfit_role": "everyday",
            "grooming": "natural",
            "accessories": [],
            "evidence_refs": [],
        },
        "performance": {
            "head_yaw": "near_front",
            "head_pitch": "level",
            "head_roll": "slight",
            "gaze_target": "lens",
            "expression": "soft_closed_smile",
            "shoulder_orientation": "near_front",
            "posture": "relaxed",
            "gesture": "show_primary",
            "photo_awareness": "aware_light_pose",
        },
        "subject_signature": (
            "natural_down|near_front|level|slight|lens|soft_closed_smile|near_front|show_primary"
        ),
    }
    presentation = SubjectPresentationPlan.from_payload(payload)

    selected = select_identity_references(
        identity_path=identity_config,
        presentation=presentation,
        subject_config_path=catalog_path,
        profile="everyday_selfie",
        limit=1,
    )

    assert selected == (different,)
    assert load_subject_catalog(catalog_path).reference_pose_metadata
