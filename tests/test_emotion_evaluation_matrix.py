from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.conversation_cadence import ConversationCadence, FrozenTurnContext
from companion_daemon.db import CompanionStore
from companion_daemon.emotion_eval_matrix import run_seeded_sequence_matrix
from companion_daemon.model_call_policy import (
    CandidateGroundingSignals,
    GroundingAuditRisk,
    ModelCallRequest,
    ProviderCircuitState,
    TurnModelCallBudget,
)
from companion_daemon.world import AcceptedTurn, CommittedAppraisal, WorldKernel
from companion_daemon.world_affect import apply_appraisal, decay_affect, initial_affect
from companion_daemon.world_affinity import initial_affinity, settle_affinity_interaction


NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def test_seeded_property_sequence_harness_has_unified_zero_failure_metrics() -> None:
    metrics = run_seeded_sequence_matrix(seeds=32, length=60)

    assert metrics.steps == 1_920
    assert metrics.hard_failures == 0
    assert metrics.invariant_pass_rate == 1.0
    assert metrics.failing_seeds == ()


@pytest.mark.parametrize("days", [1, 7, 30, 180])
def test_longitudinal_affect_matrix_preserves_bounded_monotonic_decay(days: int) -> None:
    initial = initial_affect(NOW.isoformat())
    harmed = apply_appraisal(
        initial,
        "boundary_violation",
        NOW.isoformat(),
        source_reference="message:user-a:1",
        target="companion",
    )

    decayed = decay_affect(
        {**initial, **harmed.__dict__},
        int(timedelta(days=days).total_seconds()),
        (NOW + timedelta(days=days)).isoformat(),
    )

    assert 0 <= decayed.vector["hurt"] <= harmed.vector["hurt"]
    assert 0 <= decayed.vector["anger"] <= harmed.vector["anger"]
    assert all(0 <= value <= 100 for value in decayed.vector.values())


def test_mixed_sources_and_targets_remain_traceable_without_cross_user_affinity() -> None:
    initial = initial_affect(NOW.isoformat())
    user_harm = apply_appraisal(
        initial,
        "boundary_violation",
        NOW.isoformat(),
        source_reference="message:user-a:1",
        target="companion",
    )
    mixed = apply_appraisal(
        {**initial, **user_harm.__dict__},
        "npc_conflict",
        (NOW + timedelta(minutes=1)).isoformat(),
        source_reference="experience:npc-lin:1",
        target="npc:lin",
    )

    references = {str(item["source_reference"]) for item in mixed.active_episodes}
    targets = {str(item["target"]) for item in mixed.active_episodes}
    assert references == {"message:user-a:1", "experience:npc-lin:1"}
    assert targets == {"companion", "npc:lin"}

    states = {"user:a": initial_affinity(), "user:b": initial_affinity()}
    for index in range(3):
        outcome = settle_affinity_interaction(
            states["user:a"],
            user_id="user:a",
            appraisal="boundary_violation",
            settlement_id=f"user-a:{index}",
            logical_at=(NOW + timedelta(days=index)).isoformat(),
        )
        states["user:a"] = outcome.state
    assert states["user:a"]["vector"] == {"warmth": -1, "resentment": 1}
    assert states["user:b"]["vector"] == {}


def test_duplicate_repair_evidence_is_idempotent_in_scenario_matrix() -> None:
    initial = initial_affect(NOW.isoformat())
    harmed = apply_appraisal(
        initial,
        "boundary_violation",
        NOW.isoformat(),
        source_reference="violation:1",
    )
    state = {**initial, **harmed.__dict__}
    repair = apply_appraisal(
        state,
        "repair_specific",
        (NOW + timedelta(minutes=30)).isoformat(),
        source_reference="commitment:1",
    )
    first = apply_appraisal(
        {**state, **repair.__dict__},
        "boundary_respected",
        (NOW + timedelta(hours=1)).isoformat(),
        source_reference="repair:v1:opportunity-1:honor-boundary",
    )
    duplicate = apply_appraisal(
        {**state, **first.__dict__},
        "boundary_respected",
        (NOW + timedelta(hours=2)).isoformat(),
        source_reference="repair:v1:opportunity-1:honor-boundary",
    )

    assert first.repair_evidence_count == 1
    assert duplicate.repair_evidence_count == 1
    assert duplicate.vector == first.vector


def test_twenty_turn_provider_outage_trajectory_is_local_bounded_and_deterministic() -> None:
    turn = FrozenTurnContext(
        turn_id="outage-turn",
        world_id="world",
        user_id="user",
        observed_at=NOW,
        cadence=ConversationCadence(
            heat="hot", observed_gap_seconds=3, alternating_turns=8, reason="matrix"
        ),
    )
    grounding = GroundingAuditRisk().assess(CandidateGroundingSignals(reply_text="我在听。"))
    policy = TurnModelCallBudget()

    trajectory = [
        policy.decide(
            turn=turn,
            request=ModelCallRequest(purpose="reply", calls_used=0),
            grounding=grounding,
            circuit=ProviderCircuitState.open(),
        )
        for _ in range(20)
    ]

    assert len(trajectory) == 20
    assert all(not decision.allowed for decision in trajectory)
    assert all(decision.max_calls == 1 for decision in trajectory)
    assert all(decision.soft_timeout_seconds == 0 for decision in trajectory)
    assert {decision.reason for decision in trajectory} == {
        "provider_circuit_open_use_local_fallback"
    }


def _replay_world(path: Path) -> dict[str, object]:
    kernel = WorldKernel(CompanionStore(path))
    started = kernel.submit(
        {
            "type": "start_world",
            "seed": {
                "world_id": "eval-replay-world",
                "logical_at": NOW.isoformat(),
                "protagonist": {"id": "zhizhi", "name": "知栀", "kind": "companion"},
            },
        },
        expected_revision=0,
    )
    registered = kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:a",
            "name": "A",
            "idempotency_key": "register:user-a",
        },
        expected_revision=started.revision,
    )
    kernel.accept_turn(
        AcceptedTurn(
            world_id=started.world_id,
            user_id="user:a",
            message_id="message:1",
            intent_id="intent:1",
            appraisal=CommittedAppraisal(
                "boundary_violation", severity=4, target="companion", acts=("insult",)
            ),
            expected_revision=registered.revision,
        )
    )
    kernel.advance(
        started.world_id,
        NOW + timedelta(days=7),
        expected_revision=kernel.revision(started.world_id),
    )
    report = kernel.rebuild_projection(started.world_id, "world_current_state")
    assert report.matches_live is True
    repeated = kernel.rebuild_projection(started.world_id, "world_current_state")
    assert repeated.state_hash == report.state_hash
    assert kernel.verify_ledger(started.world_id)["valid"] is True
    state = kernel.snapshot(started.world_id)
    return {key: value for key, value in state.items() if key != "clock_observed_at"}


def test_public_world_commands_replay_deterministically(tmp_path: Path) -> None:
    first_state = _replay_world(tmp_path / "first.sqlite")
    second_state = _replay_world(tmp_path / "second.sqlite")

    assert second_state == first_state
