import pytest

from companion_daemon.world_v2.mechanical_evaluation_scope import (
    AffectRetentionAssertion,
    MechanicalEvaluationScope,
    MechanicalEvaluationScopeError,
    PerformanceSampleExpectation,
    RandomDrawExpectation,
)


def _scope(**changes: object) -> MechanicalEvaluationScope:
    values: dict[str, object] = {
        "fixture_id": "replay.disappointment.1",
        "fixture_version": "mechanical-fixtures.1",
        "world_id": "world:evaluator",
        "start_ledger_sequence": 4,
        "end_ledger_sequence": 9,
        "action_ids_expected_to_settle": ("action:reply",),
        "affect_assertions": (AffectRetentionAssertion("episode:hurt"),),
        "random_draw_expectation": RandomDrawExpectation("not_applicable"),
        "performance_samples": (PerformanceSampleExpectation("hot.1", "hot"),),
    }
    values.update(changes)
    return MechanicalEvaluationScope(**values)  # type: ignore[arg-type]


def test_scope_digest_binds_fixture_cursor_and_all_assertion_sets() -> None:
    original = _scope()
    changed = _scope(action_ids_expected_to_settle=("action:other",))

    assert len(original.fixture_manifest_hash) == 64
    assert original.fixture_manifest_hash != changed.fixture_manifest_hash


def test_scope_rejects_missing_hot_sample_and_ambiguous_random_requirement() -> None:
    with pytest.raises(MechanicalEvaluationScopeError, match="hot performance"):
        _scope(performance_samples=(PerformanceSampleExpectation("cold.1", "cold"),))
    with pytest.raises(MechanicalEvaluationScopeError, match="requires expected draw ids"):
        RandomDrawExpectation("installed")
