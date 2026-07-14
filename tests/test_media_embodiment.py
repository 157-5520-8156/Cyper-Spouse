from companion_daemon.media_embodiment import (
    EmbodiedPresentation,
    VisiblePhysicalStateResolver,
    build_embodied_candidates,
)


def _snapshot(**character_overrides: object) -> dict[str, object]:
    character: dict[str, object] = {"emotion": "bright", **character_overrides}
    return {
        "schema_version": "world-event-snapshot-v1",
        "event": {
            "event_id": "event:workout",
            "status": "committed",
            "logical_at": "2026-07-14T20:00:00+08:00",
            "summary": "完成晚间训练",
            "outcome": "完成训练后准备休息",
        },
        "activity": {"kind": "exercise", "intensity": "high"},
        "environment": {"weather": "clear", "temperature": "warm"},
        "character": character,
    }


def test_world_visible_physical_state_takes_precedence_over_derivation() -> None:
    state = VisiblePhysicalStateResolver().resolve(
        _snapshot(
            visible_physical_state={
                "schema_version": "visible-physical-state-v1",
                "observed_at": "2026-07-14T20:00:00+08:00",
                "source_event_ids": ["event:workout"],
                "cues": [
                    {
                        "cue_id": "perspiration",
                        "intensity": "marked",
                        "regions": ["face", "neck", "arms"],
                    }
                ],
            }
        )
    )

    assert [cue.cue_id for cue in state.cues] == ["perspiration"]
    assert state.cues[0].source == "world_fact"
    assert state.cues[0].intensity == "marked"
    assert state.cues[0].evidence_refs == (
        "/character/visible_physical_state/cues/0",
    )
    assert state.cues[0].derivation_id is None
    assert state.cues[0].logical_at == "2026-07-14T20:00:00+08:00"
    assert state.cues[0].source_event_id == "event:workout"


def test_high_intensity_exercise_derives_visible_but_not_extreme_cues() -> None:
    state = VisiblePhysicalStateResolver().resolve(_snapshot())

    assert [(cue.cue_id, cue.intensity) for cue in state.cues] == [
        ("perspiration", "moderate"),
        ("flush", "moderate"),
        ("recovering_breath", "moderate"),
    ]
    assert all(cue.source == "derived" for cue in state.cues)
    assert all(cue.derivation_id == "exercise-high-v1" for cue in state.cues)
    assert all(cue.source_event_id == "event:workout" for cue in state.cues)
    assert all("/activity/intensity" in cue.evidence_refs for cue in state.cues)


def test_world_counterevidence_blocks_conflicting_derived_cues() -> None:
    state = VisiblePhysicalStateResolver().resolve(
        _snapshot(
            visible_physical_state={
                "schema_version": "visible-physical-state-v1",
                "observed_at": "2026-07-14T20:00:00+08:00",
                "source_event_ids": ["event:workout"],
                "counterevidence": ["dry", "settled_breathing"],
                "cues": [],
            }
        )
    )

    assert state.cues == ()


def test_rain_weather_alone_does_not_imply_the_character_was_wet() -> None:
    snapshot = _snapshot()
    snapshot["activity"] = {"kind": "reading", "intensity": "low"}
    snapshot["environment"] = {"weather": "heavy rain"}

    assert VisiblePhysicalStateResolver().resolve(snapshot).cues == ()


def test_ambiguous_workout_candidates_allow_charged_but_not_veiled() -> None:
    candidates = build_embodied_candidates(
        snapshot=_snapshot(),
        opportunity_id="opportunity:workout",
        relationship_stage="ambiguous",
        sensual_charge_ceiling="charged",
        limit=64,
    )

    assert candidates
    assert any(item.presentation.sensual_charge == "charged" for item in candidates)
    assert all(
        item.legal_share_intents == ("intimate_signal",)
        for item in candidates
        if item.presentation.sensual_charge == "charged"
    )
    assert any(item.presentation.physical_salience == "foregrounded" for item in candidates)
    assert all(item.presentation.sensual_charge != "veiled" for item in candidates)
    assert {
        "recovery_pause",
        "retie_or_lift_hair",
        "wipe_or_cool_down",
    }.issubset({item.presentation.body_strategy_id for item in candidates})

    restored = EmbodiedPresentation.from_payload(candidates[0].presentation.to_payload())
    assert restored == candidates[0].presentation


def test_veiled_candidates_require_lover_and_explicit_private_wardrobe_evidence() -> None:
    without_evidence = build_embodied_candidates(
        snapshot=_snapshot(),
        opportunity_id="opportunity:private",
        relationship_stage="lover",
        sensual_charge_ceiling="veiled",
        limit=128,
    )
    assert all(item.presentation.sensual_charge != "veiled" for item in without_evidence)

    snapshot = _snapshot(
        appearance_state={
            "schema_version": "appearance-state-v1",
            "outfit_role": "restrained_lingerie",
            "coverage_mode": "private_apparel",
        }
    )
    with_evidence = build_embodied_candidates(
        snapshot=snapshot,
        opportunity_id="opportunity:private",
        relationship_stage="lover",
        sensual_charge_ceiling="veiled",
        limit=128,
    )

    veiled = [
        item.presentation
        for item in with_evidence
        if item.presentation.sensual_charge == "veiled"
    ]
    assert veiled
    assert {item.coverage_mode for item in veiled} <= {
        "private_apparel",
        "strategic_cover",
    }
    assert all(item.wardrobe_evidence_refs for item in veiled)


def test_full_catalog_can_represent_every_body_strategy_charge_and_coverage_axis() -> None:
    cue_regions = {
        "perspiration": ["face", "neck", "arms"],
        "flush": ["face", "neck"],
        "recovering_breath": ["torso"],
        "damp_hair": ["hair"],
        "wet_skin": ["face", "arms"],
        "rain_damp_fabric": ["clothing"],
        "sleepy_face": ["face"],
        "posture_fatigue": ["shoulders", "torso"],
        "muscle_tension": ["shoulders", "arms", "legs"],
    }
    snapshot = _snapshot(
        appearance_state={
            "schema_version": "appearance-state-v1",
            "outfit_role": "restrained_lingerie",
            "coverage_mode": "private_apparel",
        },
        visible_physical_state={
            "schema_version": "visible-physical-state-v1",
            "observed_at": "2026-07-14T20:00:00+08:00",
            "source_event_ids": ["event:workout"],
            "cues": [
                {"cue_id": cue_id, "intensity": "moderate", "regions": regions}
                for cue_id, regions in cue_regions.items()
            ],
        },
    )
    candidates = build_embodied_candidates(
        snapshot=snapshot,
        opportunity_id="opportunity:full-matrix",
        relationship_stage="lover",
        sensual_charge_ceiling="veiled",
        limit=512,
    )

    assert {item.presentation.body_strategy_id for item in candidates} == {
        "neutral_presence",
        "recovery_pause",
        "retie_or_lift_hair",
        "wipe_or_cool_down",
        "damp_hair_adjustment",
        "stretch_or_reach",
        "movement_afterglow",
        "mirror_adjustment",
        "close_private_pause",
        "resting_repose",
        "covered_transition",
    }
    assert {item.presentation.sensual_charge for item in candidates} == {
        "none",
        "subtle",
        "charged",
        "veiled",
    }
    assert {item.presentation.coverage_mode for item in candidates} == {
        "fully_dressed",
        "functional_bodywear",
        "private_apparel",
        "strategic_cover",
    }


def test_recent_contract_is_hard_filtered_and_recent_axes_softly_change_order() -> None:
    baseline = build_embodied_candidates(
        snapshot=_snapshot(),
        opportunity_id="opportunity:dedup",
        relationship_stage="ambiguous",
        sensual_charge_ceiling="charged",
        limit=64,
    )
    first = baseline[0].presentation
    axis_token = "|".join(
        (
            "different-contract",
            first.physical_salience,
            first.sensual_charge,
            first.coverage_mode,
            first.body_strategy_id,
        )
    )
    soft_penalized = build_embodied_candidates(
        snapshot=_snapshot(),
        opportunity_id="opportunity:dedup",
        relationship_stage="ambiguous",
        sensual_charge_ceiling="charged",
        recent_signatures=(axis_token,),
        limit=64,
    )
    hard_filtered = build_embodied_candidates(
        snapshot=_snapshot(),
        opportunity_id="opportunity:dedup",
        relationship_stage="ambiguous",
        sensual_charge_ceiling="charged",
        recent_signatures=(first.contract_signature,),
        limit=64,
    )

    assert soft_penalized[0].candidate_id != baseline[0].candidate_id
    assert all(
        item.presentation.contract_signature != first.contract_signature
        for item in hard_filtered
    )
