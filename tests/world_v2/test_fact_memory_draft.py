from __future__ import annotations

import asyncio
import json

import pytest

from companion_daemon.world_v2.fact_memory_draft import (
    FactMemoryDraftAdapter,
    FactMemoryDraftTechnicalFailure,
    materialize_fact_memory_draft,
)


class _BlockedChat:
    model = "blocked-fact-memory"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del messages, temperature
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_fact_memory_provider_has_a_stable_total_deadline() -> None:
    with pytest.raises(FactMemoryDraftTechnicalFailure) as failure:
        await FactMemoryDraftAdapter(
            model=_BlockedChat(),
            timeout_seconds=0.1,
        ).classify(predicate_code="preference.likes", source_text="我喜欢乌龙茶。")

    assert failure.value.failure_code == "provider_timeout"


def _retained() -> dict[str, object]:
    return {
        "retain": True,
        "cue_kind": "future_utility",
        "retention_rationales": ["future_utility", "identity_relevance"],
        "salience": {
            "autobiographical_relevance_bp": 6800,
            "relationship_relevance_bp": 2200,
            "emotional_residue_bp": 500,
            "unfinished_business_bp": 0,
            "recurrence_bp": 1800,
            "novelty_bp": 3200,
            "future_utility_bp": 7900,
            "world_continuity_bp": 1000,
        },
    }


def test_fact_memory_draft_is_limited_to_the_installed_salience_matrix() -> None:
    result = materialize_fact_memory_draft(json.dumps(_retained()))

    assert result is not None
    assert result.cue_kind == "future_utility"
    assert result.salience.future_utility_bp == 7900


def test_fact_memory_draft_normalizes_provider_probability_salience() -> None:
    raw = _retained()
    raw["salience"] = {
        key: value / 10_000 for key, value in raw["salience"].items()  # type: ignore[union-attr]
    }

    result = materialize_fact_memory_draft(json.dumps(raw))

    assert result is not None
    assert result.salience.future_utility_bp == 7900


@pytest.mark.parametrize(
    "raw",
    [
        {"retain": False, "rationale": "extra"},
        {**_retained(), "privacy_ceiling": "public"},
        {**_retained(), "retention_rationales": ["future_utility", "future_utility"]},
        {**_retained(), "salience": {"future_utility_bp": 1}},
    ],
)
def test_fact_memory_draft_rejects_unbounded_or_incomplete_model_output(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        materialize_fact_memory_draft(json.dumps(raw))


class _Chat:
    model = "test-memory"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert "emotional_residue" in messages[0]["content"]
        assert "relationship_continuity" in messages[0]["content"]
        assert "乌龙茶" in messages[1]["content"]
        assert temperature == 0.15
        return json.dumps(_retained())


@pytest.mark.asyncio
async def test_adapter_exposes_only_retention_classification() -> None:
    result = await FactMemoryDraftAdapter(model=_Chat()).classify(
        predicate_code="preference.likes", source_text="我最近很喜欢喝乌龙茶。"
    )

    assert result is not None
    assert result.retention_rationales == ("future_utility", "identity_relevance")
