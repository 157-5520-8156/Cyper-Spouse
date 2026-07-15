import pytest

from companion_daemon.media_address import MediaAddressStrategy
from companion_daemon.media_authenticity import choose_authenticity_profile
from companion_daemon.media_camera import CameraGeometry
from companion_daemon.media_embodiment import build_embodied_candidates
from companion_daemon.media_expression import (
    PERCEPTUAL_SIGNATURE_VERSION,
    build_complete_candidates,
    candidate_perceptual_signature,
)
from companion_daemon.media_moment import choose_moment_capture, load_moment_catalog
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


def test_camera_geometry_v2_freezes_face_distance_without_changing_v1_payloads() -> None:
    legacy = CameraGeometry.create(
        shot_distance="close",
        camera_height="eye",
        view_axis="front",
        pitch="level",
        roll="level",
        orientation="portrait",
        subject_occupancy="dominant",
        subject_placement="center",
        environment_share="supporting",
        focus_behavior="subject_priority",
        imperfection_profile="casual_offset",
        device_visibility="out_of_frame",
    )
    assert legacy.version == "camera-geometry-v1"
    assert "camera_face_distance" not in legacy.to_payload()

    current = CameraGeometry.create(
        **{
            **{
                key: value
                for key, value in legacy.to_payload().items()
                if key not in {"version", "contract_signature"}
            },
            "camera_face_distance": "arm_length",
            "face_radial_position": "inner_third",
        }
    )
    assert current.version == "camera-geometry-v2"
    assert (
        current.compatibility_error(
            capture_mode="character_front_camera", visual_form="portrait_closeup"
        )
        is None
    )
    assert CameraGeometry.from_payload(current.to_payload()) == current


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
                item.subject_presentation["facial_display_strategy"]["strategy_family"]
                for item in invite_desire
            }
        )
        >= 4
    )


def test_character_media_freezes_rich_visible_expression_beats_not_only_face_axes() -> None:
    """Playful recipient-facing media keeps a whole still-frame beat for the renderer."""
    snapshot = {
        "event": {"event_id": "event:playful", "status": "committed"},
        "activity": {"kind": "daily", "description": "出门前收拾包，突然想逗一下收件人"},
        "location": {"kind": "home", "name": "玄关"},
        "character": {"emotion": "playful"},
    }
    subject = next(
        item
        for item in build_subject_candidates(
            snapshot=snapshot,
            opportunity_id="op:rich-beat-source",
            capture_mode="character_front_camera",
            character_visibility="identifiable",
            privacy_ceiling="intimate",
            relationship_stage="lover",
            limit=64,
        )
        if item.presentation.display_strategy is not None
    )
    embodiment = next(
        item
        for item in build_embodied_candidates(
            snapshot=snapshot,
            opportunity_id="op:rich-beat-source",
            relationship_stage="lover",
            sensual_charge_ceiling="charged",
            limit=256,
        )
        if item.presentation.sensual_charge == "charged"
        and "character_front_camera" in item.legal_capture_modes
    )
    source = {
        "presentation_candidate_id": "source:rich-beat",
        "legal_capture_modes": ["character_front_camera"],
        "legal_share_intents": ["intimate_signal"],
        "character_visibility": "identifiable",
        "subject_presentation": subject.presentation.to_payload(),
        "embodied_presentation": embodiment.presentation.to_payload(),
    }

    beats = []
    for index in range(32):
        candidates = build_complete_candidates(
            opportunity_id=f"op:rich-beat:{index}",
            family="character_media",
            expression_charge_ceiling="charged",
            presentation_candidates=(source,),
            event_snapshot=snapshot,
            limit=64,
        )
        playful = next(
            item
            for item in candidates
            if item.media_address_strategy.attraction_mechanism == "playful_tease"
        )
        beats.append(playful.subject_presentation["facial_micro_performance"])

    assert all(item["expression_beat_id"] for item in beats)
    assert all(item["visible_evidence"] for item in beats)
    assert len({item["expression_beat_id"] for item in beats}) >= 3
    assert any("nose" in " ".join(item["visible_evidence"]).lower() for item in beats)
    assert any("head shake" in " ".join(item["visible_evidence"]).lower() for item in beats)


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


def test_interaction_bids_have_multiple_compatible_address_routes() -> None:
    candidates = build_complete_candidates(
        opportunity_id="op:broad-address-routes",
        family="life_share",
        expression_charge_ceiling="none",
        limit=256,
    )

    discovery = {
        (item.media_address_strategy.address_mode, item.media_address_strategy.engagement_tactic)
        for item in candidates
        if "share_discovery" in item.legal_interaction_bids
    }
    validation = {
        (item.media_address_strategy.address_mode, item.media_address_strategy.engagement_tactic)
        for item in candidates
        if "seek_validation" in item.legal_interaction_bids
    }

    assert len(discovery) >= 3
    assert {"reveal", "demonstration", "comparison"}.issubset(
        {tactic for _address, tactic in discovery}
    )
    assert len(validation) >= 2
    assert {"vulnerability", "contrast"}.issubset({tactic for _address, tactic in validation})


def test_complete_candidates_freeze_authenticity_and_varied_visible_face_actions() -> None:
    snapshot = {
        "event": {"event_id": "event:kitchen", "status": "committed"},
        "activity": {"kind": "cooking", "description": "料理刚刚翻车"},
        "location": {"kind": "home", "name": "厨房"},
        "character": {"emotion": "amused"},
    }
    subject = next(
        item
        for item in build_subject_candidates(
            snapshot=snapshot,
            opportunity_id="op:face-source",
            capture_mode="character_front_camera",
            character_visibility="identifiable",
            privacy_ceiling="personal",
            relationship_stage="close_friend",
            limit=64,
        )
        if item.presentation.display_strategy is not None
    )
    embodiment = next(
        item
        for item in build_embodied_candidates(
            snapshot=snapshot,
            opportunity_id="op:face-source",
            relationship_stage="close_friend",
            sensual_charge_ceiling="none",
            limit=256,
        )
        if "character_front_camera" in item.legal_capture_modes
    )
    source = {
        "presentation_candidate_id": "source:kitchen",
        "legal_capture_modes": ["character_front_camera"],
        "legal_share_intents": ["record", "humor", "complain"],
        "character_visibility": "identifiable",
        "subject_presentation": subject.presentation.to_payload(),
        "embodied_presentation": embodiment.presentation.to_payload(),
    }

    candidates = build_complete_candidates(
        opportunity_id="op:facial-matrix",
        family="character_media",
        expression_charge_ceiling="none",
        presentation_candidates=(source,),
        event_snapshot=snapshot,
        limit=24,
    )

    assert candidates
    assert all(item.photographic_authenticity is not None for item in candidates)
    faces = [item.subject_presentation for item in candidates if item.subject_presentation]
    assert all(item["version"] == "subject-presentation-v4" for item in faces)
    assert len({item["facial_display_strategy"]["strategy_family"] for item in faces}) >= 3
    assert len({item["facial_micro_performance"]["nose_cheek_action"] for item in faces}) >= 2
    assert len({item.photographic_authenticity.aesthetic_intent for item in candidates}) >= 2
    assert all(
        item.photographic_authenticity.aesthetic_intent not in {"editorial", "commercial"}
        for item in candidates
    )
    assert all(
        item.photographic_authenticity.processing_level in {"light", "typical_phone"}
        for item in candidates
    )


def test_character_media_candidates_freeze_lived_moment_contracts() -> None:
    """Every character photo has a believable moment, not only a pose shell."""
    snapshot = {
        "event": {"event_id": "event:tea-break", "status": "committed"},
        "activity": {"kind": "study", "description": "写作业时泡了一杯热茶"},
        "location": {"kind": "home", "name": "书桌边"},
        "objects": [{"kind": "tea", "description": "刚泡好的热茶"}],
        "character": {"emotion": "calm"},
    }
    subject = next(
        item
        for item in build_subject_candidates(
            snapshot=snapshot,
            opportunity_id="op:lived-moment-source",
            capture_mode="character_front_camera",
            character_visibility="identifiable",
            privacy_ceiling="personal",
            relationship_stage="close_friend",
            limit=64,
        )
        if item.presentation.display_strategy is not None
    )
    embodiment = next(
        item
        for item in build_embodied_candidates(
            snapshot=snapshot,
            opportunity_id="op:lived-moment-source",
            relationship_stage="close_friend",
            sensual_charge_ceiling="none",
            limit=256,
        )
        if "character_front_camera" in item.legal_capture_modes
    )
    source = {
        "presentation_candidate_id": "source:lived-moment",
        "legal_capture_modes": ["character_front_camera"],
        "legal_share_intents": ["record", "show_and_tell", "complain"],
        "character_visibility": "identifiable",
        "subject_presentation": subject.presentation.to_payload(),
        "embodied_presentation": embodiment.presentation.to_payload(),
    }

    candidates = build_complete_candidates(
        opportunity_id="op:lived-moment",
        family="character_media",
        expression_charge_ceiling="none",
        presentation_candidates=(source,),
        event_snapshot=snapshot,
        limit=24,
    )

    assert candidates
    assert all(item.moment_capture is not None for item in candidates)
    assert {item.moment_capture.moment_mode for item in candidates} >= {
        "uninterrupted_activity",
        "brief_pause",
        "responsive_reaction",
    }
    assert all(item.moment_capture.anti_static_direction for item in candidates)


def test_moment_capture_uses_stable_but_varied_wording_without_reading_world_facts() -> None:
    choices = {
        choose_moment_capture(
            temporal_beat="held_for_response",
            capture_mode="character_front_camera",
            visual_form="portrait_context",
            stable_seed=f"moment:{index}",
        ).strategy_id
        for index in range(16)
    }

    assert choices == {"recipient-pause", "show-then-return"}


def test_moment_capture_primary_evidence_remains_the_visual_anchor() -> None:
    moment = choose_moment_capture(
        temporal_beat="held_for_response",
        capture_mode="character_front_camera",
        visual_form="portrait_context",
        stable_seed="moment:primary",
    ).bind_evidence(
        primary_evidence_ref="/objects/0/description",
        supporting_evidence_refs=("/participants/0/role",),
    )

    assert moment.scene_anchor == "event_object"
    assert moment.evidence_refs == ("/objects/0/description", "/participants/0/role")


def test_moment_catalog_rejects_a_missing_temporal_beat(tmp_path) -> None:
    catalog = tmp_path / "incomplete-moment.yaml"
    catalog.write_text("version: moment-capture-v2\ntemporal_beats: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete moment temporal beat catalog"):
        load_moment_catalog(catalog)


def test_complete_candidates_use_versioned_perceptual_axes_and_varied_face_geometry() -> None:
    snapshot = {
        "event": {"event_id": "event:selfie", "status": "committed"},
        "activity": {"kind": "daily", "description": "准备出门"},
        "character": {"emotion": "playful"},
    }
    subject = build_subject_candidates(
        snapshot=snapshot,
        opportunity_id="op:face-geometry-source",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        privacy_ceiling="personal",
        relationship_stage="close_friend",
        limit=8,
    )[0]
    embodiment = next(
        item
        for item in build_embodied_candidates(
            snapshot=snapshot,
            opportunity_id="op:face-geometry-source",
            relationship_stage="close_friend",
            sensual_charge_ceiling="none",
            limit=128,
        )
        if "character_front_camera" in item.legal_capture_modes
    )
    source = {
        "presentation_candidate_id": "source:front",
        "legal_capture_modes": ["character_front_camera"],
        "legal_share_intents": ["record", "humor"],
        "character_visibility": "identifiable",
        "subject_presentation": subject.presentation.to_payload(),
        "embodied_presentation": embodiment.presentation.to_payload(),
    }

    candidates = build_complete_candidates(
        opportunity_id="op:face-geometry",
        family="character_media",
        expression_charge_ceiling="none",
        presentation_candidates=(source,),
        event_snapshot=snapshot,
        limit=24,
    )

    assert candidates
    assert {item.camera_geometry.version for item in candidates} == {"camera-geometry-v2"}
    assert {item.camera_geometry.camera_face_distance for item in candidates}.issubset(
        {"very_close", "arm_length", "supported_near"}
    )
    assert len({item.camera_geometry.face_radial_position for item in candidates}) >= 2
    signatures = {candidate_perceptual_signature(item) for item in candidates}
    assert all(item.startswith(PERCEPTUAL_SIGNATURE_VERSION + "|") for item in signatures)
    assert len(signatures) == len(candidates)


def test_body_detail_complete_candidate_carries_no_face_contract() -> None:
    snapshot = {
        "event": {"event_id": "event:bracelet", "status": "committed"},
        "objects": [{"kind": "bracelet", "description": "新手链"}],
        "character": {"emotion": "bright"},
    }
    subject = build_subject_candidates(
        snapshot=snapshot,
        opportunity_id="op:detail-source",
        capture_mode="character_rear_camera",
        character_visibility="body_detail",
        privacy_ceiling="personal",
        relationship_stage="close_friend",
        limit=8,
    )[0]
    embodiment = next(
        item
        for item in build_embodied_candidates(
            snapshot=snapshot,
            opportunity_id="op:detail-source",
            relationship_stage="close_friend",
            sensual_charge_ceiling="none",
            limit=128,
        )
        if "character_rear_camera" in item.legal_capture_modes
    )
    source = {
        "presentation_candidate_id": "source:detail",
        "legal_capture_modes": ["character_rear_camera"],
        "legal_share_intents": ["show_and_tell"],
        "character_visibility": "body_detail",
        "subject_presentation": subject.presentation.to_payload(),
        "embodied_presentation": embodiment.presentation.to_payload(),
    }

    candidates = build_complete_candidates(
        opportunity_id="op:detail",
        family="character_media",
        expression_charge_ceiling="none",
        presentation_candidates=(source,),
        event_snapshot=snapshot,
        limit=24,
    )

    face = candidates[0].subject_presentation["facial_micro_performance"]
    assert face["mouth_action"] == "not_visible"
    assert face["eye_aperture"] == "not_visible"
    assert face["gaze_sequence"] == "no_face"


def test_life_share_authenticity_never_invents_region_or_defaults_to_commercial() -> None:
    snapshot = {
        "event": {"event_id": "event:cafe", "status": "committed"},
        "activity": {"kind": "reading"},
        "location": {"kind": "public", "name": "咖啡店"},
    }

    candidates = build_complete_candidates(
        opportunity_id="op:ordinary-life-share",
        family="life_share",
        expression_charge_ceiling="none",
        event_snapshot=snapshot,
        limit=24,
    )

    assert candidates
    assert all(
        item.photographic_authenticity.regional_grounding in {"none", "artifact_inherited"}
        for item in candidates
    )
    assert all(
        item.photographic_authenticity.aesthetic_intent != "commercial" for item in candidates
    )
    assert {item.photographic_authenticity.catalog_version for item in candidates} == {
        "media-authenticity-catalog-v1"
    }


def test_authenticity_axes_vary_independently_but_only_from_grounded_scene_cues() -> None:
    ordinary = {
        "activity": {"kind": "reading", "description": "安静看书"},
        "environment": {"lighting": "ordinary indoor daylight"},
        "location": {"kind": "public"},
    }
    ordinary_profiles = {
        choose_authenticity_profile(
            stable_seed=f"ordinary:{index}",
            capture_mode="character_rear_camera",
            family="life_share",
            staging_degree="unposed",
            visual_form="contextual_still_life",
            event_snapshot=ordinary,
        )
        for index in range(32)
    }
    assert {item.aesthetic_intent for item in ordinary_profiles} == {
        "documentary",
        "pleasant_share",
    }
    assert {item.exposure_behavior for item in ordinary_profiles}.issubset(
        {"stable", "highlight_protected"}
    )
    assert all(item.regional_grounding == "none" for item in ordinary_profiles)
    assert all(item.scene_orderliness != "commercial" for item in ordinary_profiles)

    backlit = choose_authenticity_profile(
        stable_seed="grounded:backlight",
        capture_mode="character_rear_camera",
        family="life_share",
        staging_degree="unposed",
        visual_form="wide_scene",
        event_snapshot={
            "environment": {"lighting": "strong backlight"},
            "location": {"city": "上海"},
        },
    )
    assert backlit.exposure_behavior in {"backlit_compromise", "highlight_protected"}
    assert backlit.regional_grounding == "explicit"


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
