from pathlib import Path

from companion_daemon.media_subject import (
    SubjectPresentationPlan,
    build_subject_candidates,
    load_subject_catalog,
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
