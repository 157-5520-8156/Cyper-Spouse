from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.life_evolution import LifeEvolution
from companion_daemon.world import WorldError, WorldKernel


NOW = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)  # Monday


def evolution_state() -> dict[str, object]:
    return {
        "clock": {"logical_at": NOW.isoformat()},
        "weekly_themes": [
            {
                "id": "portfolio",
                "title": "作品集整理",
                "template_id": "portfolio_work",
                "location": "宿舍",
                "weekdays": [1, 4],
                "starts_hour": 19,
                "duration_hours": 2,
                "priority": 8,
            },
            {
                "id": "family-call",
                "title": "给家里打电话",
                "template_id": "family_call",
                "location": "宿舍",
                "weekdays": [6],
                "starts_hour": 20,
                "duration_hours": 1,
                "priority": 5,
            },
            {
                "id": "extra",
                "title": "额外安排",
                "template_id": "extra",
                "location": "宿舍",
                "weekdays": [3],
                "starts_hour": 18,
                "duration_hours": 1,
                "priority": 1,
            },
        ],
        "weekly_plans": {},
        "agenda": {},
        "goals": {},
        "life_outcome_templates": {},
        "outcomes": {},
        "entities": {},
        "needs": {"energy": 70},
        "life_evolution": {
            "influences": {},
            "observations": {},
            "pressure_samples": [],
            "chronic": {"fatigue": 0, "relationship_pressure": 0},
        },
    }


def test_week_plan_is_sparse_stable_and_remains_a_plan() -> None:
    evolution = LifeEvolution()
    state = evolution_state()

    first = evolution.plan_week(state, week_start=NOW)
    second = evolution.plan_week(state, week_start=NOW)

    assert first == second
    assert [kind for kind, _ in first].count("WeeklyThemePlanned") == 2
    activities = [payload for kind, payload in first if kind == "ActivityPlanned"]
    assert len(activities) == 3
    assert {item["starts_at"][:10] for item in activities} == {
        "2026-07-14",
        "2026-07-17",
        "2026-07-19",
    }
    assert not any(kind == "ExperienceCommitted" for kind, _ in first)


def test_goal_scoring_explains_resource_rejection_and_chronic_preferences() -> None:
    state = evolution_state()
    state["goals"] = {
        "submission": {
            "id": "submission",
            "priority": 8,
            "target": 5,
            "progress": 2,
            "status": "active",
            "deadline": (NOW + timedelta(hours=18)).isoformat(),
        }
    }
    state["life_outcome_templates"] = {
        "deadline_work": {
            "goal_id": "submission",
            "energy_cost": 25,
            "load": "high",
            "location": "宿舍",
        },
        "quiet_walk": {
            "energy_cost": 4,
            "load": "low",
            "social": False,
            "location": "校园",
        },
    }
    evolution = LifeEvolution()
    activity = {
        "starts_at": (NOW + timedelta(hours=2)).isoformat(),
        "ends_at": (NOW + timedelta(hours=3)).isoformat(),
    }

    urgent = evolution.score_candidate(state, activity, "deadline_work")
    assert urgent.eligible is True
    assert urgent.score > evolution.score_candidate(state, activity, "quiet_walk").score
    assert "deadline_urgency" in urgent.reasons

    state["needs"]["energy"] = 10
    rejected = evolution.score_candidate(state, activity, "deadline_work")
    assert rejected.eligible is False
    assert rejected.rejected_reasons == ("insufficient_energy",)

    state["needs"]["energy"] = 70
    state["life_evolution"]["chronic"] = {
        "fatigue": 85,
        "relationship_pressure": 75,
    }
    tired_work = evolution.score_candidate(state, activity, "deadline_work")
    tired_walk = evolution.score_candidate(state, activity, "quiet_walk")
    assert tired_walk.score > tired_work.score
    assert "chronic_fatigue_high_load_cost" in tired_work.reasons


def test_user_event_adjusts_only_future_plans() -> None:
    state = evolution_state()
    state["agenda"] = {
        "past": {
            "activity_id": "past",
            "status": "completed",
            "starts_at": (NOW - timedelta(hours=2)).isoformat(),
            "attention_demand": 30,
        },
        "current": {
            "activity_id": "current",
            "status": "active",
            "starts_at": (NOW - timedelta(minutes=15)).isoformat(),
            "attention_demand": 30,
        },
        "future": {
            "activity_id": "future",
            "status": "planned",
            "starts_at": (NOW + timedelta(hours=2)).isoformat(),
            "attention_demand": 30,
        },
    }

    events = LifeEvolution().events_for_user_influence(
        state,
        influence_id="user-vulnerable-1",
        kind="user_vulnerability",
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=12),
    )

    adjusted = [payload for kind, payload in events if kind == "FutureActivityAdjusted"]
    assert [item["activity_id"] for item in adjusted] == ["future"]
    assert adjusted[0]["attention_demand"] == 50
    assert events[0][0] == "LifeInfluenceRecorded"


def test_environment_observation_is_low_risk_expiring_and_never_an_experience() -> None:
    events = LifeEvolution().environment_observation_events(
        observation_id="weather-1",
        category="weather",
        value="小雨",
        source_id="sensor:weather:shanghai",
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=6),
        confidence=0.6,
        confirmed_current=True,
    )

    assert events == [
        (
            "EnvironmentObservationRecorded",
            {
                "observation_id": "weather-1",
                "category": "weather",
                "value": "小雨",
                "source_id": "sensor:weather:shanghai",
                "observed_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(hours=6)).isoformat(),
                "confidence": 0.6,
                "weight": "low",
                "rule_version": "life-evolution-v1",
            },
        )
    ]
    assert not any(kind == "ExperienceCommitted" for kind, _ in events)

    with pytest.raises(ValueError, match="confirmed current"):
        LifeEvolution().environment_observation_events(
            observation_id="guess",
            category="weather",
            value="也许下雨",
            source_id="model:guess",
            observed_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            confidence=0.4,
            confirmed_current=False,
        )


def test_multiweek_pressure_changes_projection_slowly() -> None:
    evolution = LifeEvolution()
    state = evolution_state()

    for week in range(3):
        events = evolution.pressure_events(
            state,
            sample_id=f"week-{week}",
            week_start=NOW + timedelta(days=7 * week),
            fatigue=90,
            relationship_pressure=80,
        )
        _, payload = events[0]
        state["life_evolution"]["chronic"] = payload["chronic"]
        state["life_evolution"]["pressure_samples"].append(payload)

    chronic = state["life_evolution"]["chronic"]
    assert 60 <= chronic["fatigue"] < 90
    assert 50 <= chronic["relationship_pressure"] < 80
    assert chronic["share_willingness"] < 1.0
    assert chronic["social_frequency"] < 1.0


def test_world_kernel_records_week_plan_observation_and_expiry_without_experience(
    tmp_path: Path,
) -> None:
    seed = {
        "world_id": "life-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion"},
        "weekly_themes": evolution_state()["weekly_themes"],
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    planned = kernel.submit(
        {
            "type": "materialize_weekly_plan",
            "world_id": started.world_id,
            "week_start": NOW.isoformat(),
        },
        expected_revision=started.revision,
    )
    observed = kernel.submit(
        {
            "type": "record_environment_observation",
            "world_id": started.world_id,
            "observation_id": "weather-1",
            "category": "weather",
            "value": "小雨",
            "source_id": "sensor:weather:shanghai",
            "observed_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=2)).isoformat(),
            "confidence": 0.6,
            "confirmed_current": True,
        },
        expected_revision=planned.revision,
    )

    snapshot = kernel.snapshot(started.world_id)
    assert len(snapshot["weekly_plans"]) == 1
    assert len(snapshot["agenda"]) == 3
    assert snapshot["experiences"] == {}
    assert snapshot["life_evolution"]["observations"]["weather-1"]["status"] == "active"

    advanced = kernel.advance(
        started.world_id,
        NOW + timedelta(hours=3),
        expected_revision=observed.revision,
    )
    assert "EnvironmentObservationExpired" in {
        event.event_type for event in advanced.events
    }
    assert kernel.snapshot(started.world_id)["life_evolution"]["observations"]["weather-1"]["status"] == "expired"


def test_world_rejects_user_influence_that_would_start_in_the_past(tmp_path: Path) -> None:
    seed = {
        "world_id": "life-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion"},
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)

    with pytest.raises(WorldError, match="future expiry"):
        kernel.submit(
            {
                "type": "record_life_influence",
                "world_id": started.world_id,
                "influence_id": "bad",
                "kind": "user_conflict",
                "observed_at": (NOW - timedelta(hours=1)).isoformat(),
                "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
            },
            expected_revision=started.revision,
        )


def test_world_chronic_pressure_selects_low_load_seeded_fallback_with_audit(
    tmp_path: Path,
) -> None:
    seed = {
        "world_id": "life-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {
            "id": "zhizhi",
            "name": "沈知栀",
            "kind": "companion",
            "resources": {"energy": 80},
        },
        "life_outcome_templates": {
            "submission_work": {
                "location": "宿舍",
                "energy_cost": 20,
                "load": "high",
                "content": "完成了投稿整理。",
            },
            "quiet_walk": {
                "location": "校园",
                "energy_cost": 3,
                "load": "low",
                "social": False,
                "content": "在校园慢慢走了一会儿。",
            },
        },
        "daily_schedule": [
            {
                "slot": "evening-choice",
                "title": "晚间安排",
                "template_id": "submission_work",
                "fallback_templates": ["quiet_walk"],
                "location": "宿舍",
                "starts_hour": 10,
                "ends_hour": 11,
            }
        ],
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    decision = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    for week in range(3):
        decision = kernel.submit(
            {
                "type": "record_life_pressure",
                "world_id": decision.world_id,
                "sample_id": f"week-{week}",
                "week_start": (NOW - timedelta(days=7 * (2 - week))).isoformat(),
                "fatigue": 90,
                "relationship_pressure": 80,
            },
            expected_revision=decision.revision,
        )

    advanced = kernel.advance(
        decision.world_id,
        NOW + timedelta(hours=3),
        expected_revision=decision.revision,
    )

    activity = kernel.snapshot(decision.world_id)["agenda"][
        "2026-07-13:evening-choice"
    ]
    assert activity["template_id"] == "quiet_walk"
    evaluations = [
        event.payload
        for event in advanced.events
        if event.event_type == "ActivityCandidateEvaluated"
    ]
    assert {item["template_id"] for item in evaluations} == {
        "submission_work",
        "quiet_walk",
    }
    assert next(item for item in evaluations if item["selected"])["template_id"] == "quiet_walk"


def test_life_evolution_projection_rebuild_matches_live_state(tmp_path: Path) -> None:
    seed = {
        "world_id": "life-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion"},
        "weekly_themes": evolution_state()["weekly_themes"],
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    decision = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    decision = kernel.submit(
        {
            "type": "materialize_weekly_plan",
            "world_id": decision.world_id,
            "week_start": NOW.isoformat(),
        },
        expected_revision=decision.revision,
    )
    decision = kernel.submit(
        {
            "type": "record_life_pressure",
            "world_id": decision.world_id,
            "sample_id": "week-1",
            "week_start": NOW.isoformat(),
            "fatigue": 70,
            "relationship_pressure": 40,
        },
        expected_revision=decision.revision,
    )
    live = kernel.snapshot(decision.world_id)

    report = kernel.rebuild_projection(decision.world_id, "world_current_state")

    assert report.matches_live is True
    assert kernel.snapshot(decision.world_id)["life_evolution"] == live["life_evolution"]
    assert kernel.snapshot(decision.world_id)["weekly_plans"] == live["weekly_plans"]


def test_clock_tick_automatically_materializes_seeded_week_plan(tmp_path: Path) -> None:
    seed = {
        "world_id": "life-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion"},
        "weekly_themes": evolution_state()["weekly_themes"],
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)

    advanced = kernel.advance(
        started.world_id, NOW, expected_revision=started.revision
    )

    assert [event.event_type for event in advanced.events][:2] == [
        "ClockAdvanced",
        "WeeklyPlanCreated",
    ]
    snapshot = kernel.snapshot(started.world_id)
    assert len(snapshot["weekly_plans"]) == 1
    planned_activities = [
        activity
        for plan in snapshot["weekly_plans"].values()
        for theme in plan["themes"].values()
        for activity in theme["activities"]
    ]
    assert len(planned_activities) == 3
    assert snapshot["agenda"] == {}
    assert snapshot["experiences"] == {}


def test_week_plan_long_jump_and_incremental_replay_match(tmp_path: Path) -> None:
    seed = {
        "world_id": "life-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion"},
        "weekly_themes": evolution_state()["weekly_themes"],
    }
    target = NOW + timedelta(days=10)
    long_kernel = WorldKernel(CompanionStore(tmp_path / "long.sqlite"))
    long_started = long_kernel.submit(
        {"type": "start_world", "seed": seed}, expected_revision=0
    )
    long_kernel.advance(
        long_started.world_id, target, expected_revision=long_started.revision
    )

    step_kernel = WorldKernel(CompanionStore(tmp_path / "step.sqlite"))
    step_started = step_kernel.submit(
        {"type": "start_world", "seed": seed}, expected_revision=0
    )
    revision = step_started.revision
    for days in (3, 7, 10):
        decision = step_kernel.advance(
            step_started.world_id,
            NOW + timedelta(days=days),
            expected_revision=revision,
        )
        revision = decision.revision

    long_state = long_kernel.snapshot(long_started.world_id)
    step_state = step_kernel.snapshot(step_started.world_id)
    assert long_state["weekly_plans"] == step_state["weekly_plans"]
    assert long_state["agenda"] == step_state["agenda"]
    assert long_state["experiences"] == step_state["experiences"]


def test_sustained_pressure_reduces_actual_life_share_willingness(
    tmp_path: Path,
) -> None:
    seed = {
        "world_id": "life-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion"},
        "life_outcome_templates": {
            "quiet_walk": {
                "location": "校园",
                "energy_cost": 3,
                "load": "low",
                "content": "在校园慢慢走了一会儿。",
            }
        },
        "daily_schedule": [
            {
                "slot": "walk",
                "title": "散步",
                "template_id": "quiet_walk",
                "location": "校园",
                "starts_hour": 9,
                "ends_hour": 10,
            }
        ],
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    decision = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    decision = kernel.advance(
        decision.world_id,
        NOW + timedelta(hours=2),
        expected_revision=decision.revision,
    )
    assert kernel.snapshot(decision.world_id)["experiences"]
    for week in range(6):
        decision = kernel.submit(
            {
                "type": "record_life_pressure",
                "world_id": decision.world_id,
                "sample_id": f"pressure-{week}",
                "week_start": (NOW - timedelta(days=7 * week)).isoformat(),
                "fatigue": 100,
                "relationship_pressure": 100,
            },
            expected_revision=decision.revision,
        )

    assert kernel.snapshot(decision.world_id)["life_evolution"]["chronic"][
        "share_willingness"
    ] < 0.4
    assert (
        kernel.schedule_life_share_delivery(
            world_id=decision.world_id,
            canonical_user_id="geoff",
            platform="qq",
            expires_at=NOW + timedelta(hours=4),
            expected_revision=decision.revision,
        )
        is None
    )
    assert not any(
        action.get("trace", {}).get("life_share")
        for action in kernel.snapshot(decision.world_id)["actions"].values()
    )


def test_user_vulnerability_adjusts_future_week_plan_before_materialization(
    tmp_path: Path,
) -> None:
    seed = {
        "world_id": "life-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion"},
        "weekly_themes": evolution_state()["weekly_themes"],
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    decision = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    decision = kernel.advance(
        decision.world_id, NOW, expected_revision=decision.revision
    )
    assert kernel.snapshot(decision.world_id)["agenda"] == {}

    adjusted = kernel.submit(
        {
            "type": "record_life_influence",
            "world_id": decision.world_id,
            "influence_id": "vulnerable-1",
            "kind": "user_vulnerability",
            "observed_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(days=2)).isoformat(),
            "source_message_id": "message:user-vulnerable",
        },
        expected_revision=decision.revision,
    )

    future = [
        activity
        for plan in kernel.snapshot(decision.world_id)["weekly_plans"].values()
        for theme in plan["themes"].values()
        for activity in theme["activities"]
    ]
    changed = [item for item in future if item.get("last_influence_id")]
    assert changed
    assert all(item["starts_at"] > NOW.isoformat() for item in changed)
    assert all(item["preference_bias"] == "phone_accessible" for item in changed)
    assert {event.event_type for event in adjusted.events} >= {
        "LifeInfluenceRecorded",
        "FutureActivityAdjusted",
    }
