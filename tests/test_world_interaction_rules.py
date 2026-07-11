from companion_daemon.world_interaction_rules import WorldInteractionRules


def test_interaction_rules_are_versioned_and_return_only_structured_consequences() -> None:
    rules = WorldInteractionRules()

    consequence = rules.consequence("boundary_violation")

    assert rules.RULE_VERSION == "world-interaction-v1"
    assert consequence.need_deltas == {"security": -12, "boundary": 12, "initiative": -8}
    assert consequence.relationship_deltas["respect"] == -12
    assert consequence.emotion_mode == "guarded"
