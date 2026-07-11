from companion_daemon.world_interaction_rules import WorldInteractionRules


def test_world_emotion_charge_decays_on_logical_time_without_erasing_relationship(tmp_path) -> None:
    from datetime import timedelta
    from pathlib import Path

    from companion_daemon.db import CompanionStore
    from companion_daemon.world import WorldKernel

    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    appraised = kernel.submit(
        {
            "type": "appraise_turn", "world_id": started.world_id,
            "appraisal": "boundary_violation", "intent_id": "turn:decay",
            "user_id": "user:geoff",
        },
        expected_revision=user.revision,
    )
    logical_at = __import__("datetime").datetime.fromisoformat(
        str(kernel.snapshot(started.world_id)["clock"]["logical_at"])
    )
    respect_before = kernel.snapshot(started.world_id)["relationships"]["user:geoff"]["respect"]

    kernel.advance(started.world_id, logical_at + timedelta(hours=9), expected_revision=appraised.revision)

    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["emotion_modulation"]["charge"] == 0
    assert snapshot["emotion_modulation"]["mode"] == "calm"
    assert snapshot["relationships"]["user:geoff"]["respect"] == respect_before


def test_interaction_rules_are_versioned_and_return_only_structured_consequences() -> None:
    rules = WorldInteractionRules()

    consequence = rules.consequence("boundary_violation")

    assert rules.RULE_VERSION == "world-interaction-v1"
    assert consequence.need_deltas == {"security": -12, "boundary": 12, "initiative": -8}
    assert consequence.relationship_deltas["respect"] == -12
    assert consequence.emotion_mode == "guarded"
