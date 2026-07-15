from pathlib import Path

import yaml

from companion_daemon.media_facial import (
    FACIAL_MICRO_VERSION_V1,
    FacialMicroPerformance,
    choose_facial_contract,
)
from companion_daemon.media_subject import (
    PhotoDisplayStrategy,
    SubjectAppearance,
    SubjectPerformance,
    SubjectPresentationPlan,
    build_subject_candidates,
    load_subject_catalog,
    presentation_prompt_block,
    select_identity_references,
    upgrade_subject_presentation_v3,
)


CONFIG = Path("configs/media_subject_templates.yaml")
FACIAL_CONFIG = Path("configs/media_facial_performance_templates.yaml")


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
        limit=64,
    )
    second = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:1",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
        limit=64,
    )

    assert first == second
    assert len(first) >= 3
    assert all(item.presentation.appearance.source == "media_local" for item in first)
    assert all(item.presentation.performance.photo_awareness for item in first)
    assert len({item.presentation.subject_signature for item in first}) == len(first)


def test_pre_beat_v1_facial_catalog_uses_bounded_compatibility_matrix(tmp_path: Path) -> None:
    """An installed v1 affinity catalog must not become a planning outage."""
    legacy_catalog = yaml.safe_load(FACIAL_CONFIG.read_text(encoding="utf-8"))
    legacy_catalog.pop("expression_beats")
    legacy_catalog.pop("micro_recipes")
    custom_path = tmp_path / "legacy-facial-catalog.yaml"
    custom_path.write_text(yaml.safe_dump(legacy_catalog), encoding="utf-8")

    strategy, micro = choose_facial_contract(
        stable_seed="legacy-facial-catalog",
        engagement_tactic="affection",
        attraction_mechanism=None,
        catalog_path=custom_path,
    )

    assert strategy.strategy_family in {"tender_private", "warm_connection", "desire_withheld"}
    assert micro.recipe_id.startswith("legacy:")
    assert micro.expression_beat_id.endswith("coherent_visible_beat")


def test_default_style_uses_complete_recipe_ids_from_catalog(tmp_path: Path) -> None:
    """The runtime selects catalog-owned complete performances, not Python defaults."""
    catalog = yaml.safe_load(FACIAL_CONFIG.read_text(encoding="utf-8"))
    for recipes in catalog["micro_recipes"].values():
        for recipe in recipes:
            recipe["id"] = f"catalog-owned:{recipe['id']}"
    custom_path = tmp_path / "catalog-owned-recipes.yaml"
    custom_path.write_text(yaml.safe_dump(catalog), encoding="utf-8")

    _, micro = choose_facial_contract(
        stable_seed="catalog-owned-recipes",
        engagement_tactic="affection",
        attraction_mechanism=None,
        catalog_path=custom_path,
    )

    assert micro.recipe_id.startswith("catalog-owned:")


def test_character_candidates_include_coherent_social_performance_recipes() -> None:
    candidates = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:social-performance",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
        limit=32,
    )

    pretend = next(
        item
        for item in candidates
        if item.presentation.display_strategy
        and item.presentation.display_strategy.strategy_id == "pretend_innocent"
    )
    strategy = pretend.presentation.display_strategy
    assert strategy is not None
    assert strategy.communicative_goals == ("invite_playful_exchange",)
    assert strategy.mouth == "subtle_relaxed_pout"
    assert "exaggerated_duck_face" in strategy.forbidden_cues
    assert pretend.presentation.version == "subject-presentation-v2"
    assert SubjectPresentationPlan.from_payload(pretend.presentation.to_payload()) == (
        pretend.presentation
    )


def test_social_performance_prompt_leads_with_holistic_behavior() -> None:
    strategy = PhotoDisplayStrategy(
        strategy_id="pretend_innocent",
        communicative_goals=("invite_playful_exchange",),
        intentionality="lightly_performed",
        intensity="subtle",
        holistic_cue="knowingly plays innocent for the recipient",
        mouth="subtle_relaxed_pout",
        eyes="relaxed_slightly_widened",
        brows="barely_raised",
        gaze_quality="direct_soft_lens_contact",
        facial_tension="relaxed",
        temporal_beat="holding_the_look_before_breaking_character",
        forbidden_cues=("exaggerated_duck_face", "broad_smile"),
    )
    appearance = SubjectAppearance("media_local", "natural_down", "home_cooking", "natural")
    performance = SubjectPerformance(
        "near_front",
        "level",
        "none",
        "lens",
        "pretend_innocent",
        "slightly_turned",
        "compact_casual",
        "show_primary_evidence",
        "aware_light_pose",
    )
    presentation = SubjectPresentationPlan.create_v2(
        variant_id="test__pretend_innocent",
        appearance=appearance,
        performance=performance,
        display_strategy=strategy,
    )

    prompt = presentation_prompt_block(presentation, config_path=CONFIG)

    assert prompt.index("knowingly plays innocent") < prompt.index("subtle relaxed pout")
    assert "not an exaggerated duck face" in prompt


def test_subject_v3_keeps_expression_out_of_pose_performance() -> None:
    source = next(
        item.presentation
        for item in build_subject_candidates(
            snapshot=_snapshot(),
            opportunity_id="op:v3-face",
            capture_mode="character_front_camera",
            character_visibility="identifiable",
            config_path=CONFIG,
            limit=32,
        )
        if item.presentation.display_strategy is not None
    )

    upgraded = upgrade_subject_presentation_v3(source)
    payload = upgraded.to_payload()

    assert upgraded.version == "subject-presentation-v3"
    assert "expression" not in payload["performance"]
    assert "gaze_target" not in payload["performance"]
    assert payload["facial_performance"]["expression_family"]
    assert SubjectPresentationPlan.from_payload(payload) == upgraded


def test_facial_matrix_exposes_broad_social_and_visible_action_variety() -> None:
    tactics = (
        "presence",
        "reveal",
        "demonstration",
        "question",
        "comparison",
        "contrast",
        "comic_hook",
        "celebration",
        "vulnerability",
        "reassurance",
        "coordination",
        "affection",
        "nostalgia",
        "attraction",
    )
    mechanisms = (
        None,
        "direct_invitation",
        "playful_tease",
        "withheld_attention",
        "sensory_immediacy",
        "private_trust",
        "confident_display",
        "interrupted_transition",
        "close_proximity",
        "atmospheric_suggestion",
    )
    contracts = [
        choose_facial_contract(
            stable_seed=f"matrix:{index}:{tactic}:{mechanism}",
            engagement_tactic=tactic,
            attraction_mechanism=mechanism if tactic == "attraction" else None,
        )
        for index in range(96)
        for tactic in tactics
        for mechanism in mechanisms
        if tactic == "attraction" or mechanism is None
    ]

    strategies = {strategy.strategy_family for strategy, _micro in contracts}
    assert {
        "present_and_available",
        "warm_connection",
        "amusement_leaking",
        "deliberate_cuteness",
        "mock_defiance",
        "comic_self_exposure",
        "proud_display",
        "consultative_check",
        "frustrated_complaint",
        "embarrassed_repair",
        "tired_access",
        "vulnerable_disclosure",
        "tender_private",
        "desire_direct",
        "desire_withheld",
        "neutral_evidence",
    }.issubset(strategies)
    assert len({micro.mouth_action for _strategy, micro in contracts}) >= 9
    assert len({micro.eye_aperture for _strategy, micro in contracts}) >= 7
    assert len({micro.nose_cheek_action for _strategy, micro in contracts}) >= 6
    assert len({micro.performance_authorship for _strategy, micro in contracts}) >= 6
    assert len({micro.temporal_phase for _strategy, micro in contracts}) >= 6
    assert all(strategy.catalog_version == "media-facial-catalog-v1" for strategy, _ in contracts)
    assert all(
        micro.recipe_id and micro.catalog_version == "media-facial-catalog-v1"
        for _, micro in contracts
    )


def test_legacy_facial_micro_contract_round_trips_without_v2_expression_beat() -> None:
    legacy = FacialMicroPerformance.create(
        brow_action="neutral",
        eye_aperture="natural",
        gaze_target="lens",
        gaze_sequence="held_lens",
        nose_cheek_action="relaxed",
        mouth_action="small_smile",
        facial_asymmetry="balanced",
        display_intensity="subtle",
        performance_authorship="recipient_aware",
        temporal_phase="held_beat",
        facial_energy="contained",
        recipe_id="warm_connection:legacy",
        version=FACIAL_MICRO_VERSION_V1,
    )

    payload = legacy.to_payload()
    assert "expression_beat_id" not in payload
    assert "visible_evidence" not in payload
    assert FacialMicroPerformance.from_payload(payload) == legacy


def test_facial_performance_respects_camera_authorship_and_face_visibility() -> None:
    tactics = (
        "presence",
        "comic_hook",
        "vulnerability",
        "attraction",
        "question",
    )
    mechanisms = (None, "playful_tease", "direct_invitation", "withheld_attention")
    for capture_mode in (
        "character_front_camera",
        "mirror",
        "timer_fixed",
        "requested_helper",
        "known_companion",
        "external_sender",
    ):
        for index in range(48):
            tactic = tactics[index % len(tactics)]
            mechanism = mechanisms[index % len(mechanisms)] if tactic == "attraction" else None
            _strategy, micro = choose_facial_contract(
                stable_seed=f"physics:{capture_mode}:{index}",
                engagement_tactic=tactic,
                attraction_mechanism=mechanism,
                capture_mode=capture_mode,
            )
            if capture_mode == "character_front_camera":
                assert micro.performance_authorship not in {
                    "responsive_candid",
                    "photographer_prompted",
                    "unperformed_capture",
                }
                assert micro.gaze_target != "companion"
            if capture_mode in {"requested_helper", "known_companion", "external_sender"}:
                assert micro.performance_authorship != "selfie_micro_pose"
                assert micro.gaze_target not in {"screen", "screen_preview"}

    strategy, micro = choose_facial_contract(
        stable_seed="physics:no-face",
        engagement_tactic="demonstration",
        attraction_mechanism=None,
        capture_mode="character_rear_camera",
        face_visible=False,
    )
    assert strategy.performance_intent == "face is not visible"
    assert micro.gaze_sequence == "no_face"
    assert micro.performance_authorship == "not_visible"


def test_subject_candidate_matrix_exposes_every_social_strategy_without_flat_rules() -> None:
    strategies: set[str] = set()
    for capture_mode in (
        "character_front_camera",
        "character_rear_camera",
        "mirror",
        "timer_fixed",
        "requested_helper",
        "known_companion",
        "external_sender",
    ):
        for visibility in ("identifiable", "body_detail"):
            candidates = build_subject_candidates(
                snapshot=_snapshot(),
                opportunity_id=f"op:matrix:{capture_mode}:{visibility}",
                capture_mode=capture_mode,
                character_visibility=visibility,
                privacy_ceiling="intimate",
                relationship_stage="close_friend",
                config_path=CONFIG,
                limit=64,
            )
            strategies.update(
                item.presentation.display_strategy.strategy_id
                for item in candidates
                if item.presentation.display_strategy is not None
            )

    assert strategies == {
        "matter_of_fact_showing",
        "candid_enjoyment",
        "warm_include_you",
        "pretend_innocent",
        "mock_wronged",
        "deadpan_reveal",
        "suppressed_laugh",
        "self_deprecating_grin",
        "curious_check",
        "small_proud_reveal",
        "soft_bid_for_care",
        "tired_unfiltered",
        "composed_attraction",
        "playful_challenge",
    }


def test_different_opportunities_stably_change_social_candidate_order() -> None:
    first = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:variation:a",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
        limit=32,
    )
    repeated = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:variation:a",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
        limit=32,
    )
    second = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:variation:b",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        config_path=CONFIG,
        limit=32,
    )

    assert first == repeated
    assert [item.variant_id for item in first] != [item.variant_id for item in second]


def test_recent_social_strategy_axes_are_softly_downranked_before_seeded_sampling() -> None:
    kwargs = {
        "snapshot": _snapshot(),
        "opportunity_id": "op:soft-social-history",
        "capture_mode": "character_front_camera",
        "character_visibility": "identifiable",
        "config_path": CONFIG,
    }
    baseline = build_subject_candidates(**kwargs)
    first_strategy = baseline[0].presentation.display_strategy
    assert first_strategy is not None
    historical_near_match = baseline[0].presentation.subject_signature + "|historical-extra"

    varied = build_subject_candidates(**kwargs, recent_subject_signatures=(historical_near_match,))

    assert varied[0].presentation.display_strategy is not None
    assert varied[0].presentation.display_strategy.strategy_id != first_strategy.strategy_id


def test_world_context_only_filters_privacy_and_clear_affect_conflicts() -> None:
    ordinary = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:ordinary",
        capture_mode="mirror",
        character_visibility="identifiable",
        privacy_ceiling="ordinary",
        config_path=CONFIG,
        limit=64,
    )
    severe = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:severe",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        privacy_ceiling="personal",
        relationship_stage="close_friend",
        public_affect={"severity": "severe"},
        config_path=CONFIG,
        limit=64,
    )
    no_relationship = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:no-relationship",
        capture_mode="character_front_camera",
        character_visibility="identifiable",
        privacy_ceiling="personal",
        config_path=CONFIG,
        limit=64,
    )

    ordinary_strategies = {
        item.presentation.display_strategy.strategy_id
        for item in ordinary
        if item.presentation.display_strategy
    }
    severe_strategies = {
        item.presentation.display_strategy.strategy_id
        for item in severe
        if item.presentation.display_strategy
    }
    no_relationship_strategies = {
        item.presentation.display_strategy.strategy_id
        for item in no_relationship
        if item.presentation.display_strategy
    }
    assert "composed_attraction" not in ordinary_strategies
    assert "playful_challenge" not in no_relationship_strategies
    assert "pretend_innocent" not in severe_strategies
    assert {"matter_of_fact_showing", "tired_unfiltered"} & severe_strategies


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
        limit=64,
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
    assert all(
        item.presentation.display_strategy is not None
        and item.presentation.display_strategy.mouth == "not_applicable"
        and not item.presentation.display_strategy.forbidden_cues
        for item in candidates
    )


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
        limit=64,
    )
    timer = build_subject_candidates(
        snapshot=_snapshot(),
        opportunity_id="op:hands",
        capture_mode="timer_fixed",
        character_visibility="identifiable",
        config_path=CONFIG,
        limit=64,
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
        limit=64,
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
