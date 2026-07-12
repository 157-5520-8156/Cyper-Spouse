from __future__ import annotations

from companion_daemon.emotion_programs import (
    EmotionProgramInput,
    evaluate_emotion_program,
)


def test_self_caused_norm_violation_produces_guilt_and_repair_coping() -> None:
    result = evaluate_emotion_program(
        EmotionProgramInput(
            event="hurt_someone",
            agency="companion",
            target="third_party",
            certainty=90,
            goal_congruence=-70,
            controllability=70,
            norm_compatibility=-85,
            power_delta=0,
        )
    )
    assert result.primary == "guilt"
    assert result.components["guilt"] >= 60
    assert result.coping == "repair"


def test_global_self_evaluation_produces_shame_and_concealment() -> None:
    result = evaluate_emotion_program(
        EmotionProgramInput(
            event="public_failure",
            agency="companion",
            target="self",
            certainty=85,
            goal_congruence=-80,
            controllability=20,
            norm_compatibility=-70,
            power_delta=-60,
            self_evaluation="global_negative",
            social_exposure=90,
        )
    )
    assert result.primary == "shame"
    assert result.coping == "conceal_or_withdraw"


def test_relationship_threat_with_comparison_produces_bounded_jealousy() -> None:
    result = evaluate_emotion_program(
        EmotionProgramInput(
            event="comparison",
            agency="third_party",
            target="valued_relationship",
            certainty=55,
            goal_congruence=-65,
            controllability=25,
            norm_compatibility=-10,
            power_delta=-20,
            relationship_value=80,
            comparison_salience=75,
            comparison_target="npc:colleague",
            source_event_ids=("world:event:1",),
        )
    )
    assert result.primary == "jealousy"
    assert 1 <= result.components["jealousy"] <= 100
    assert result.coping == "seek_clarity_without_control"


def test_relationship_value_without_a_sourced_comparison_does_not_invent_jealousy() -> None:
    result = evaluate_emotion_program(
        EmotionProgramInput(
            event="ordinary_absence",
            agency="third_party",
            target="valued_relationship",
            certainty=20,
            goal_congruence=-40,
            controllability=20,
            norm_compatibility=0,
            power_delta=0,
            relationship_value=90,
            comparison_salience=80,
            comparison_target="npc:unknown",
        )
    )
    assert "jealousy" not in result.components


def test_controllability_changes_threat_from_anxiety_to_assertive_anger() -> None:
    shared = dict(
        event="blocked_goal",
        agency="user",
        target="companion",
        certainty=45,
        goal_congruence=-80,
        norm_compatibility=-50,
        power_delta=0,
    )
    low = evaluate_emotion_program(EmotionProgramInput(**shared, controllability=15))
    high = evaluate_emotion_program(EmotionProgramInput(**shared, controllability=80))
    assert (low.primary, high.primary) == ("anxiety", "anger")


def test_suppression_and_rumination_are_processes_not_invented_events() -> None:
    result = evaluate_emotion_program(
        EmotionProgramInput(
            event="unresolved_offence",
            agency="user",
            target="companion",
            certainty=85,
            goal_congruence=-75,
            controllability=15,
            norm_compatibility=-65,
            power_delta=-50,
            expression_safety=15,
            unresolved=True,
            attention_capture=80,
        )
    )
    assert result.processes == ("suppression", "rumination")
    assert result.process_effects["display_multiplier"] < 1.0
    assert result.process_effects["decay_multiplier"] < 1.0
    assert result.invented_stimulus is False
