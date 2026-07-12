from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.emotion_state import interpret_interaction
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.world import WorldKernel
from companion_daemon.world_affect import apply_appraisal, decay_affect, initial_affect
from companion_daemon.world_interaction_rules import (
    WorldInteractionRules,
    classify_repair_appraisal,
)
from companion_daemon.world_relationship import evaluate_controlled_transgression


def test_repair_classifier_distinguishes_apology_specificity_and_restitution() -> None:
    assert classify_repair_appraisal("对不起") == "repair_perfunctory"
    assert classify_repair_appraisal("刚才逼你马上回复不对，我不该命令你") == "repair_specific"
    assert classify_repair_appraisal("刚才逼你马上回复不对，我已经取消提醒，以后会先问你") == "repair_restitution"
    assert classify_repair_appraisal("今天天气不错") is None


def test_boundary_followthrough_is_explicit_repair_evidence() -> None:
    event = interpret_interaction(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="respect-boundary",
            text="你不想说就不说，我尊重你的边界。",
        ),
        MoodState(),
    )

    assert event.kind == "boundary_respected"
    assert event.target == "companion"


def test_repair_quality_has_monotonic_but_bounded_consequences() -> None:
    rules = WorldInteractionRules()
    perfunctory = rules.consequence("repair_perfunctory")
    specific = rules.consequence("repair_specific")
    restitution = rules.consequence("repair_restitution")

    assert perfunctory.relationship_deltas["trust"] == 0
    assert 0 < specific.relationship_deltas["trust"] < restitution.relationship_deltas["trust"]
    assert perfunctory.need_deltas["security"] < specific.need_deltas["security"]
    assert specific.need_deltas["security"] < restitution.need_deltas["security"]


def test_single_specific_apology_starts_observation_without_clearing_hurt() -> None:
    state = initial_affect("2026-01-01T00:00:00+00:00")
    hurt = apply_appraisal(state, "boundary_violation", "2026-01-01T00:00:00+00:00")
    repaired = apply_appraisal(
        hurt.__dict__, "repair_specific", "2026-01-01T00:01:00+00:00"
    )

    assert repaired.vector["hurt"] > 0
    assert repaired.unresolved is True
    assert repaired.behavior_tendency == "repair_observing"
    assert repaired.repair_quality == "specific"
    assert repaired.repair_observation_seconds == 24 * 3600
    assert repaired.repair_streak == 1


def test_restitution_repairs_more_but_still_requires_an_observation_period() -> None:
    state = initial_affect("2026-01-01T00:00:00+00:00")
    hurt = apply_appraisal(state, "boundary_violation", "2026-01-01T00:00:00+00:00")
    repaired = apply_appraisal(
        hurt.__dict__, "repair_restitution", "2026-01-01T00:01:00+00:00"
    )

    assert 0 < repaired.vector["hurt"] < hurt.vector["hurt"]
    assert repaired.unresolved is True
    assert repaired.behavior_tendency == "repair_observing"
    assert repaired.repair_quality == "restitution"
    assert repaired.repair_observation_seconds == 12 * 3600
    assert repaired.repair_streak == 2


def test_violation_during_observation_is_repeated_and_heavier() -> None:
    state = initial_affect("2026-01-01T00:00:00+00:00")
    first = apply_appraisal(state, "boundary_violation", "2026-01-01T00:00:00+00:00")
    apology = apply_appraisal(
        first.__dict__, "repair_specific", "2026-01-01T00:01:00+00:00"
    )
    repeated = apply_appraisal(
        apology.__dict__, "boundary_violation", "2026-01-01T00:02:00+00:00"
    )

    assert repeated.source_appraisal == "repeated_violation"
    assert repeated.vector["hurt"] - apology.vector["hurt"] > first.vector["hurt"]
    assert repeated.vector["resentment"] - apology.vector["resentment"] > first.vector["resentment"]
    assert repeated.repair_streak == 0
    assert repeated.repair_observation_seconds == 0
    assert repeated.violation_count == 2


def test_observation_window_waits_for_behavior_evidence_after_time_elapsed() -> None:
    state = initial_affect("2026-01-01T00:00:00+00:00")
    hurt = apply_appraisal(state, "boundary_violation", "2026-01-01T00:00:00+00:00")
    apology = apply_appraisal(
        hurt.__dict__, "repair_specific", "2026-01-01T00:01:00+00:00"
    )
    halfway = decay_affect(apology.__dict__, 12 * 3600, "2026-01-01T12:01:00+00:00")
    completed = decay_affect(halfway.__dict__, 12 * 3600, "2026-01-02T00:01:00+00:00")

    assert halfway.repair_observation_seconds == 12 * 3600
    assert halfway.unresolved is True
    assert halfway.behavior_tendency == "repair_observing"
    assert completed.repair_observation_seconds == 3600
    assert completed.repair_evidence_count == 0
    assert completed.repair_quality == "specific"
    assert completed.unresolved is True
    assert completed.behavior_tendency == "repair_observing"


def test_controlled_transgression_is_a_cost_not_a_relationship_stage_ban() -> None:
    early = evaluate_controlled_transgression(
        {"stage": "acquaintance", "trust": 20, "respect": 10},
        unresolved_affect=False,
        seconds_since_last=13 * 3600,
    )
    close_but_hurt = evaluate_controlled_transgression(
        {"stage": "close_friend", "trust": 70, "respect": 60},
        unresolved_affect=True,
        seconds_since_last=8 * 3600,
    )

    assert early.allowed is True
    assert early.relationship_cost > close_but_hurt.relationship_cost
    assert close_but_hurt.allowed is True
    assert close_but_hurt.affect_cost > 0


def test_controlled_transgression_keeps_safety_consent_and_frequency_hard() -> None:
    consent = evaluate_controlled_transgression(
        {"stage": "lover", "trust": 90, "respect": 90},
        unresolved_affect=False,
        seconds_since_last=24 * 3600,
        consent_ok=False,
    )
    too_soon = evaluate_controlled_transgression(
        {"stage": "lover", "trust": 90, "respect": 90},
        unresolved_affect=False,
        seconds_since_last=5 * 60,
    )

    assert consent.allowed is False
    assert consent.reason == "consent_required"
    assert too_soon.allowed is False
    assert too_soon.reason == "transgression_cooldown"


def test_world_reclassifies_violation_during_repair_observation(tmp_path: Path) -> None:
    world = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    registered = world.submit(
        {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=world.revision(world_id),
    )
    revision = registered.revision
    for index, appraisal in enumerate(
        ("boundary_violation", "repair_specific", "boundary_violation")
    ):
        decision = world.submit(
            {
                "type": "appraise_turn", "world_id": world_id,
                "intent_id": f"repair-turn:{index}", "appraisal": appraisal,
                "user_id": "user:geoff",
            },
            expected_revision=revision,
        )
        revision = decision.revision

    appraisals = [
        event.payload["appraisal"]
        for event in world.events(world_id)
        if event.event_type == "TurnAppraised"
    ]
    assert appraisals[-1] == "repeated_violation"
    affect = world.snapshot(world_id)["emotion_modulation"]
    assert affect["source_appraisal"] == "repeated_violation"
    assert affect["violation_count"] == 2
