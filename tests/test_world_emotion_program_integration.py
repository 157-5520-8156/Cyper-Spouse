from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.world import WorldKernel
from companion_daemon.expression_plan import compile_expression_plan
from companion_daemon.world_affect import apply_appraisal, decay_affect, initial_affect


def test_committed_appraisal_dimensions_drive_shame_episode_in_world_projection(
    tmp_path: Path,
) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "emotion-program-world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "emotion-program-user",
        },
        expected_revision=started.revision,
    )

    kernel.submit(
        {
            "type": "appraise_turn",
            "world_id": started.world_id,
            "appraisal": "goal_strain",
            "interaction": {
                "target": "self",
                "severity": 3,
                "agency": "companion",
                "certainty": 90,
                "goal_congruence": -80,
                "controllability": 35,
                "responsibility": 75,
                "norm_compatibility": -70,
                "power_delta": -20,
                "self_evaluation": "global_negative",
                "social_exposure": 70,
            },
            "intent_id": "self-evaluation",
            "message_id": "self-evaluation-source",
            "user_id": "user:geoff",
            "idempotency_key": "appraise-self-evaluation",
        },
        expected_revision=kernel.revision(started.world_id),
    )

    affect = kernel.snapshot(started.world_id)["emotion_modulation"]
    assert affect["vector"]["shame"] > 0
    episode = affect["active_episodes"][-1]
    assert episode["emotion_program"]["primary"] == "shame"
    assert episode["emotion_program"]["coping"] == "conceal_or_withdraw"
    assert episode["responsibility"] == 75
    assert episode["emotion_program"]["appraisal_inputs"]["responsibility"] == 75


def test_suppression_changes_display_not_felt_intensity_and_rumination_slows_decay() -> None:
    at = "2026-07-12T09:00:00+00:00"
    base = initial_affect(at)
    regulated = apply_appraisal(
        base,
        "goal_strain",
        at,
        source_reference="outcome:pressure",
        target="self",
        appraisal_dimensions={
            "agency": "situation",
            "goal_congruence": -75,
            "certainty": 80,
            "controllability": 30,
            "norm_compatibility": -20,
            "power_delta": -10,
            "expression_safety": 20,
            "unresolved": True,
            "attention_capture": 80,
        },
    )
    plain = apply_appraisal(
        base,
        "goal_strain",
        at,
        source_reference="outcome:plain-pressure",
        target="self",
        appraisal_dimensions={
            "agency": "situation",
            "goal_congruence": -75,
            "certainty": 80,
            "controllability": 30,
            "norm_compatibility": -20,
            "power_delta": -10,
        },
    )

    regulated_plan = compile_expression_plan(
        regulated.__dict__, {}, {}, current_appraisal="goal_strain"
    )
    plain_plan = compile_expression_plan(
        plain.__dict__, {}, {}, current_appraisal="goal_strain"
    )
    assert regulated.vector == plain.vector
    assert regulated_plan.policy_spec.leakage < plain_plan.policy_spec.leakage

    regulated_later = decay_affect(
        regulated.__dict__, 18 * 3600, "2026-07-13T03:00:00+00:00"
    )
    plain_later = decay_affect(
        plain.__dict__, 18 * 3600, "2026-07-13T03:00:00+00:00"
    )
    assert regulated_later.vector["anxiety"] > plain_later.vector["anxiety"]


def test_appraisal_dimensions_causally_change_episode_component_deltas() -> None:
    """The durable episode projection must reflect, not merely name, appraisals."""
    at = "2026-07-12T09:00:00+00:00"
    base = initial_affect(at)
    common = {
        "agency": "user",
        "goal_congruence": -80,
        "norm_compatibility": -50,
        "power_delta": 0,
    }
    low_control = apply_appraisal(
        base,
        "goal_strain",
        at,
        source_reference="outcome:low-control",
        target="companion",
        appraisal_dimensions={**common, "certainty": 45, "controllability": 15},
    )
    high_control = apply_appraisal(
        base,
        "goal_strain",
        at,
        source_reference="outcome:high-control",
        target="companion",
        appraisal_dimensions={**common, "certainty": 45, "controllability": 80},
    )
    low_certainty = apply_appraisal(
        base,
        "goal_strain",
        at,
        source_reference="outcome:low-certainty",
        target="companion",
        appraisal_dimensions={**common, "certainty": 15, "controllability": 15},
    )
    high_certainty = apply_appraisal(
        base,
        "goal_strain",
        at,
        source_reference="outcome:high-certainty",
        target="companion",
        appraisal_dimensions={**common, "certainty": 90, "controllability": 15},
    )
    low_responsibility = apply_appraisal(
        base,
        "goal_strain",
        at,
        source_reference="outcome:low-responsibility",
        target="self",
        appraisal_dimensions={
            "agency": "companion",
            "goal_congruence": -80,
            "norm_compatibility": -80,
            "certainty": 90,
            "controllability": 70,
            "responsibility": 10,
        },
    )
    high_responsibility = apply_appraisal(
        base,
        "goal_strain",
        at,
        source_reference="outcome:high-responsibility",
        target="self",
        appraisal_dimensions={
            "agency": "companion",
            "goal_congruence": -80,
            "norm_compatibility": -80,
            "certainty": 90,
            "controllability": 70,
            "responsibility": 90,
        },
    )

    assert low_control.vector["anxiety"] > high_control.vector["anxiety"]
    assert high_control.vector["anger"] > low_control.vector["anger"]
    assert low_certainty.vector["anxiety"] > high_certainty.vector["anxiety"]
    assert high_responsibility.vector["guilt"] > low_responsibility.vector["guilt"]

    episode = high_responsibility.active_episodes[-1]
    assert episode["responsibility"] == 90
    assert episode["emotion_program"]["component_deltas"]["guilt"] > 0
    assert episode["emotion_program"]["appraisal_inputs"] == {
        "certainty": 90,
        "controllability": 70,
        "responsibility": 90,
    }
