from companion_daemon.life_appraisal import (
    appraise_committed_life_outcome,
    derive_life_appraisal_context,
)
from companion_daemon.world_affect import apply_appraisal, initial_affect


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


def test_repeated_unresolved_npc_conflict_intensifies_from_committed_context() -> None:
    outcome = {
        "appraisal": "npc_conflict",
        "npc_id": "roommate",
        "intensity": 60,
        "source_reference": "outcome:today-conflict",
    }
    context = derive_life_appraisal_context(
        outcome,
        prior_outcomes={
            "outcome:earlier-conflict": {"npc_id": "roommate"},
            "outcome:other-npc": {"npc_id": "colleague"},
        },
        experiences={
            "outcome:earlier-conflict": {
                "source_outcome_id": "outcome:earlier-conflict",
                "affect_appraisal": "npc_conflict",
            },
            "outcome:other-npc": {
                "source_outcome_id": "outcome:other-npc",
                "affect_appraisal": "npc_conflict",
            },
        },
        active_episodes=[
            {
                "source_reference": "outcome:earlier-conflict",
                "target": "npc:roommate",
                "appraisal": "npc_conflict",
                "valence": -1,
            }
        ],
    )
    fresh = appraise_committed_life_outcome(
        outcome, needs={"energy": 60, "security": 60}, npc_relationship={}, goal_importance=0
    )
    repeated = appraise_committed_life_outcome(
        outcome,
        needs={"energy": 60, "security": 60},
        npc_relationship={},
        goal_importance=0,
        context=context,
    )
    at = "2026-07-13T09:00:00+00:00"
    fresh_affect = apply_appraisal(
        initial_affect(at),
        "npc_conflict",
        at,
        source_reference="outcome:fresh",
        intensity=fresh.salience,
        target="npc:roommate",
        appraisal_dimensions={**fresh.payload(), **fresh.context.payload()},
    )
    repeated_affect = apply_appraisal(
        initial_affect(at),
        "npc_conflict",
        at,
        source_reference=outcome["source_reference"],
        intensity=repeated.salience,
        target="npc:roommate",
        appraisal_dimensions={**repeated.payload(), **context.payload()},
    )

    assert context.recurrence_count == 1
    assert context.unresolved_related_count == 1
    assert context.source_event_ids == (
        "outcome:earlier-conflict",
        "outcome:today-conflict",
    )
    assert repeated.salience > fresh.salience
    assert repeated_affect.vector["anger"] > fresh_affect.vector["anger"]
    assert repeated_affect.active_episodes[-1]["emotion_program"]["source_event_ids"] == [
        "outcome:earlier-conflict",
        "outcome:today-conflict",
    ]


def test_restorative_context_buffers_life_conflict_without_reassigning_agency() -> None:
    outcome = {
        "appraisal": "npc_conflict",
        "npc_id": "roommate",
        "intensity": 70,
        "source_reference": "outcome:conflict-after-rest",
    }
    buffered_context = derive_life_appraisal_context(
        outcome,
        prior_outcomes={},
        experiences={},
        active_episodes=[
            {
                "source_reference": "outcome:quiet-walk",
                "target": "world",
                "appraisal": "restorative_solitude",
                "valence": 1,
            }
        ],
    )
    unbuffered = appraise_committed_life_outcome(
        outcome, needs={"energy": 40, "security": 50}, npc_relationship={}, goal_importance=0
    )
    buffered = appraise_committed_life_outcome(
        outcome,
        needs={"energy": 40, "security": 50},
        npc_relationship={},
        goal_importance=0,
        context=buffered_context,
    )

    assert buffered_context.restorative_context == 1
    assert buffered.salience < unbuffered.salience
    assert buffered.controllability > unbuffered.controllability
    assert buffered.agency == "npc"
    assert all(not source.startswith("message:") for source in buffered.context.source_event_ids)
