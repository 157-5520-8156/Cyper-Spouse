from companion_daemon.life_appraisal import appraise_committed_life_outcome


def test_same_npc_conflict_is_appraised_through_current_relationship_and_energy() -> None:
    outcome = {
        "appraisal": "npc_conflict",
        "npc_id": "roommate",
        "intensity": 70,
        "source_reference": "outcome:conflict",
    }
    rested = appraise_committed_life_outcome(
        outcome,
        needs={"energy": 90, "security": 60},
        npc_relationship={"closeness": 10},
        goal_importance=0,
    )
    exhausted_close = appraise_committed_life_outcome(
        outcome,
        needs={"energy": 15, "security": 25},
        npc_relationship={"closeness": 80},
        goal_importance=0,
    )

    assert rested.agency == "npc"
    assert exhausted_close.controllability < rested.controllability
    assert exhausted_close.relationship_value > rested.relationship_value
    assert exhausted_close.salience > rested.salience


def test_goal_importance_changes_goal_failure_appraisal_without_changing_fact() -> None:
    outcome = {
        "appraisal": "goal_strain",
        "goal_id": "portfolio",
        "intensity": 55,
        "source_reference": "outcome:goal-strain",
    }

    low = appraise_committed_life_outcome(
        outcome, needs={"energy": 50}, npc_relationship={}, goal_importance=15
    )
    high = appraise_committed_life_outcome(
        outcome, needs={"energy": 50}, npc_relationship={}, goal_importance=90
    )

    assert high.goal_congruence < low.goal_congruence
    assert high.salience > low.salience
