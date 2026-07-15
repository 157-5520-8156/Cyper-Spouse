import pytest

from companion_daemon.media_address import MediaAddressStrategy
from companion_daemon.media_camera import CameraGeometry
from companion_daemon.media_embodiment import build_embodied_candidates
from companion_daemon.media_expression import build_complete_candidates
from companion_daemon.media_subject import build_subject_candidates


def test_media_address_strategy_round_trips_a_recipient_directed_photo() -> None:
    strategy = MediaAddressStrategy.create(
        address_mode="direct_recipient",
        engagement_tactic="attraction",
        disclosure_mode="partial_reveal",
        staging_degree="privately_composed",
        temporal_beat="held_for_response",
        visual_priority="character",
        expression_charge="charged",
        attraction_mechanism="playful_tease",
    )

    assert MediaAddressStrategy.from_payload(strategy.to_payload()) == strategy


def test_media_address_strategy_rejects_attraction_without_recipient_address() -> None:
    with pytest.raises(ValueError, match="attraction requires recipient address"):
        MediaAddressStrategy.create(
            address_mode="observational",
            engagement_tactic="attraction",
            disclosure_mode="open_context",
            staging_degree="unposed",
            temporal_beat="mid_action",
            visual_priority="environment",
            expression_charge="charged",
            attraction_mechanism="direct_invitation",
        )


@pytest.mark.parametrize(
    ("capture_mode", "visual_form", "geometry", "expected"),
    [
        (
            "character_front_camera",
            "portrait_context",
            dict(
                shot_distance="medium",
                camera_height="chest",
                view_axis="left_three_quarter",
                pitch="level",
                roll="slight_right",
                orientation="landscape",
                subject_occupancy="balanced",
                subject_placement="left_third",
                environment_share="balanced",
                focus_behavior="subject_priority",
                imperfection_profile="casual_offset",
                device_visibility="out_of_frame",
            ),
            None,
        ),
        (
            "character_front_camera",
            "portrait_closeup",
            dict(
                shot_distance="long",
                camera_height="eye",
                view_axis="front",
                pitch="level",
                roll="level",
                orientation="portrait",
                subject_occupancy="dominant",
                subject_placement="center",
                environment_share="minimal",
                focus_behavior="subject_priority",
                imperfection_profile="clean_intentional",
                device_visibility="out_of_frame",
            ),
            "capture_distance_conflict",
        ),
    ],
)
def test_camera_geometry_validates_capture_physics_and_visual_form(
    capture_mode: str,
    visual_form: str,
    geometry: dict[str, str],
    expected: str | None,
) -> None:
    frozen = CameraGeometry.create(**geometry)
    assert (
        frozen.compatibility_error(capture_mode=capture_mode, visual_form=visual_form) == expected
    )
    assert CameraGeometry.from_payload(frozen.to_payload()) == frozen


@pytest.mark.parametrize(
    ("capture_mode", "visual_form", "geometry"),
    [
        (
            "character_front_camera",
            "portrait_closeup",
            ("close", "eye", "front", "dominant", "center", "out_of_frame"),
        ),
        (
            "character_rear_camera",
            "process_pov",
            ("detail", "chest", "top_down_pov", "detail", "lower_frame", "out_of_frame"),
        ),
        (
            "mirror",
            "full_body",
            ("full_body", "chest", "reflection_oblique", "balanced", "center", "mirror_visible"),
        ),
        (
            "timer_fixed",
            "portrait_context",
            ("medium", "low", "left_three_quarter", "balanced", "left_third", "fixed_unseen"),
        ),
        (
            "requested_helper",
            "wide_scene",
            ("wide", "eye", "front", "small", "right_third", "external_unseen"),
        ),
        (
            "known_companion",
            "social_frame",
            ("medium", "eye", "over_shoulder", "balanced", "distributed", "external_unseen"),
        ),
        (
            "external_sender",
            "portrait_context",
            ("medium", "eye", "right_three_quarter", "balanced", "right_third", "external_unseen"),
        ),
        (
            "existing_artifact",
            "result_showcase",
            ("wide", "eye", "environment_pov", "absent", "not_applicable", "artifact_inherited"),
        ),
    ],
)
def test_every_capture_mode_has_a_legal_camera_contract(
    capture_mode: str,
    visual_form: str,
    geometry: tuple[str, str, str, str, str, str],
) -> None:
    distance, height, axis, occupancy, placement, device = geometry
    frozen = CameraGeometry.create(
        shot_distance=distance,
        camera_height=height,
        view_axis=axis,
        pitch="level",
        roll="level",
        orientation="landscape",
        subject_occupancy=occupancy,
        subject_placement=placement,
        environment_share="balanced",
        focus_behavior="evidence_priority" if occupancy == "absent" else "subject_priority",
        imperfection_profile="clean_intentional",
        device_visibility=device,
    )

    assert frozen.compatibility_error(capture_mode=capture_mode, visual_form=visual_form) is None


@pytest.mark.parametrize(
    ("visual_form", "distance", "occupancy"),
    [
        ("wide_scene", "wide", "absent"),
        ("contextual_still_life", "medium", "trace"),
        ("process_pov", "detail", "detail"),
        ("subject_closeup", "close", "dominant"),
        ("result_showcase", "medium", "balanced"),
        ("portrait_closeup", "intimate_close", "dominant"),
        ("portrait_context", "medium", "balanced"),
        ("full_body", "full_body", "balanced"),
        ("body_detail", "detail", "detail"),
        ("social_frame", "long", "balanced"),
    ],
)
def test_every_visual_form_has_a_legal_geometry(
    visual_form: str, distance: str, occupancy: str
) -> None:
    capture_mode = (
        "character_rear_camera"
        if visual_form in {"contextual_still_life", "process_pov", "subject_closeup", "body_detail"}
        else "timer_fixed"
        if visual_form in {"wide_scene", "result_showcase", "portrait_context", "full_body"}
        else "known_companion"
        if visual_form == "social_frame"
        else "character_front_camera"
    )
    frozen = CameraGeometry.create(
        shot_distance=distance,
        camera_height="eye",
        view_axis="environment_pov" if occupancy in {"absent", "trace", "detail"} else "front",
        pitch="level",
        roll="level",
        orientation="portrait",
        subject_occupancy=occupancy,
        subject_placement=(
            "not_applicable"
            if occupancy in {"absent", "trace"}
            else "distributed"
            if visual_form == "social_frame"
            else "center"
        ),
        environment_share="supporting",
        focus_behavior="evidence_priority"
        if occupancy in {"absent", "trace"}
        else "subject_priority",
        imperfection_profile="clean_intentional",
        device_visibility={
            "character_rear_camera": "out_of_frame",
            "timer_fixed": "fixed_unseen",
            "known_companion": "external_unseen",
            "character_front_camera": "out_of_frame",
        }[capture_mode],
    )

    assert frozen.compatibility_error(capture_mode=capture_mode, visual_form=visual_form) is None


def test_invite_desire_has_nine_visual_mechanisms_not_two_scene_templates() -> None:
    snapshot = {
        "event": {
            "event_id": "event:workout",
            "status": "committed",
            "logical_at": "2026-07-15T20:00:00+08:00",
        },
        "activity": {"kind": "exercise", "intensity": "high"},
        "character": {"emotion": "bright"},
    }
    subject = next(
        item
        for item in build_subject_candidates(
            snapshot=snapshot,
            opportunity_id="op:mechanisms",
            capture_mode="mirror",
            character_visibility="identifiable",
            privacy_ceiling="intimate",
            relationship_stage="lover",
            limit=64,
        )
        if item.presentation.display_strategy
        and "invite_desire" in item.presentation.display_strategy.communicative_goals
    )
    embodiment = next(
        item
        for item in build_embodied_candidates(
            snapshot=snapshot,
            opportunity_id="op:mechanisms",
            relationship_stage="lover",
            sensual_charge_ceiling="charged",
            limit=256,
        )
        if item.presentation.sensual_charge == "charged" and "mirror" in item.legal_capture_modes
    )
    source = {
        "presentation_candidate_id": "source:charged-mirror",
        "legal_capture_modes": ["mirror"],
        "legal_share_intents": ["intimate_signal"],
        "character_visibility": "identifiable",
        "subject_presentation": subject.presentation.to_payload(),
        "embodied_presentation": embodiment.presentation.to_payload(),
    }

    candidates = build_complete_candidates(
        opportunity_id="op:mechanisms",
        family="character_media",
        expression_charge_ceiling="charged",
        presentation_candidates=(source,),
        limit=24,
    )

    invite_desire = [item for item in candidates if "invite_desire" in item.legal_interaction_bids]
    assert {item.media_address_strategy.attraction_mechanism for item in invite_desire} == {
        "direct_invitation",
        "playful_tease",
        "withheld_attention",
        "sensory_immediacy",
        "private_trust",
        "confident_display",
        "interrupted_transition",
        "close_proximity",
        "atmospheric_suggestion",
    }
    assert all(
        item.media_address_strategy.address_mode == "direct_recipient"
        for item in invite_desire
        if item.media_address_strategy.attraction_mechanism == "atmospheric_suggestion"
    )
    assert (
        len(
            {
                item.subject_presentation["facial_performance"]["expression_family"]
                for item in invite_desire
            }
        )
        >= 4
    )


def test_life_share_candidates_cover_environment_process_and_detail_geometry() -> None:
    candidates = build_complete_candidates(
        opportunity_id="op:life-coverage",
        family="life_share",
        expression_charge_ceiling="none",
        limit=24,
    )

    forms = {item.legal_visual_forms[0] for item in candidates}
    captures = {item.legal_capture_modes[0] for item in candidates}

    assert {"wide_scene", "contextual_still_life", "process_pov"}.issubset(forms)
    assert {"character_rear_camera", "known_companion", "external_sender"}.issubset(captures)


@pytest.mark.parametrize(
    ("bid", "tactic", "address", "charge", "mechanism"),
    [
        ("inform_status", "presence", "shared_attention", "none", None),
        ("coordinate_next_step", "coordination", "consultative", "none", None),
        ("share_presence", "presence", "shared_attention", "none", None),
        ("share_discovery", "reveal", "shared_attention", "none", None),
        ("invite_appreciation", "celebration", "direct_recipient", "none", None),
        ("invite_opinion", "question", "consultative", "none", None),
        ("celebrate_together", "celebration", "direct_recipient", "none", None),
        ("invite_playful_exchange", "comic_hook", "direct_recipient", "none", None),
        ("seek_validation", "vulnerability", "direct_recipient", "none", None),
        ("seek_care", "vulnerability", "direct_recipient", "none", None),
        ("offer_reassurance", "reassurance", "evidence_mediated", "none", None),
        ("invite_closeness", "affection", "direct_recipient", "subtle", None),
        ("invite_desire", "attraction", "direct_recipient", "charged", "direct_invitation"),
        ("revisit_memory", "nostalgia", "memory_recall", "none", None),
    ],
)
def test_every_interaction_bid_has_a_legal_whole_image_address(
    bid: str,
    tactic: str,
    address: str,
    charge: str,
    mechanism: str | None,
) -> None:
    strategy = MediaAddressStrategy.create(
        address_mode=address,
        engagement_tactic=tactic,
        disclosure_mode="selective_focus",
        staging_degree="privately_composed" if charge != "none" else "camera_aware",
        temporal_beat="held_for_response",
        visual_priority="relationship" if charge != "none" else "primary_evidence",
        expression_charge=charge,
        attraction_mechanism=mechanism,
    )

    assert strategy.bid_compatibility_error(bid) is None
