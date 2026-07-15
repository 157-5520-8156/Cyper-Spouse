from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.evaluation_artifacts import corpus_digest
from companion_daemon.world_v2.scenario_corpus import (
    FROZEN_SCENARIO_CORPUS_HASH,
    MINIMUM_EMOTION_GOLD_SIZE,
    SCENARIO_CASES,
    SCENARIO_CORPUS,
    SCENARIO_CORPUS_SIZE,
    verify_frozen_scenario_corpus,
)
from companion_daemon.world_v2.scenario_runner import ScenarioRunner


def test_frozen_phase8_corpus_has_required_family_emotion_and_hash_coverage() -> None:
    cases = verify_frozen_scenario_corpus()

    assert len(cases) == SCENARIO_CORPUS_SIZE == 120
    assert sum(item.entry.emotional_gold for item in cases) >= MINIMUM_EMOTION_GOLD_SIZE == 40
    assert corpus_digest(SCENARIO_CORPUS) == FROZEN_SCENARIO_CORPUS_HASH
    assert len({item.entry.scenario_family for item in cases}) == 17
    assert all(item.entry.input_hash and item.entry.fact_set_hash for item in cases)


@pytest.mark.asyncio
async def test_runner_executes_a_real_v2_turn_then_exports_verified_replay(tmp_path) -> None:
    result = await ScenarioRunner(workdir=tmp_path).run_case(SCENARIO_CASES[0])

    assert result.passed
    assert result.model_calls == 1
    assert result.terminal_action_states == ("delivered",)
    assert result.replay_passed
    assert "ActionAuthorized" in result.event_types
    assert "ExternalObservationProcessed" in result.event_types


@pytest.mark.asyncio
async def test_runner_fault_injection_covers_failed_receipt_and_duplicate_ingress(tmp_path) -> None:
    runner = ScenarioRunner(workdir=tmp_path)
    failed = next(item for item in SCENARIO_CASES if item.fault == "provider_failed")
    unknown = next(item for item in SCENARIO_CASES if item.fault == "provider_unknown")
    restarted = next(item for item in SCENARIO_CASES if item.fault == "restart_before_dispatch")
    duplicate = next(item for item in SCENARIO_CASES if item.fault == "duplicate_ingress")

    failed_result = await runner.run_case(failed)
    unknown_result = await runner.run_case(unknown)
    restarted_result = await runner.run_case(restarted)
    duplicate_result = await runner.run_case(duplicate)

    assert failed_result.passed
    assert failed_result.terminal_action_states == ("failed",)
    assert unknown_result.passed
    assert unknown_result.terminal_action_states == ("unknown",)
    assert restarted_result.passed
    assert restarted_result.terminal_action_states == ("delivered",)
    assert duplicate_result.passed
    assert duplicate_result.observation_count == 1


@pytest.mark.asyncio
async def test_suite_manifest_is_hash_bound_and_explicitly_not_human_evaluation(tmp_path) -> None:
    suite = await ScenarioRunner(workdir=tmp_path).run_frozen_suite(limit=3)

    assert suite.passed
    manifest = suite.export_manifest()
    assert len(manifest["manifest_hash"]) == 64
    assert "not a human" in str(manifest["runner_limitations"])
    assert json.dumps(manifest, ensure_ascii=False)
