from __future__ import annotations


import pytest

from companion_daemon.world_v2.model_usage_budget import WorldV2UsageStore


class _Usage:
    def __init__(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cache_hit_tokens = cache_hit_tokens
        self.cache_miss_tokens = cache_miss_tokens
        self.total_tokens = prompt_tokens + completion_tokens
        self.status = "ok"
        self.purpose = "test"
        self.world_id = "world:test"
        self.turn_id = "turn:test"
        self.provider = "test"
        self.error = ""
        self.latency_ms = 10


def test_usage_store_records_and_aggregates_cost(tmp_path) -> None:
    store = WorldV2UsageStore(path=str(tmp_path / "usage.sqlite"))
    store.record(
        _Usage(model="deepseek-v4-flash", prompt_tokens=1_000_000, completion_tokens=0)
    )
    # 1M miss tokens at $0.14/M = $0.14 * 7.2 = 1.008 CNY
    monthly = store.monthly_cost_cny()
    daily = store.daily_cost_cny()
    assert monthly == pytest.approx(1.008, abs=0.01)
    assert daily == pytest.approx(1.008, abs=0.01)


def test_usage_store_records_failed_calls_without_raising(tmp_path) -> None:
    store = WorldV2UsageStore(path=str(tmp_path / "usage.sqlite"))
    # record() must never raise on malformed observer payloads.
    store.record(object())
    store.record(None)
    assert store.monthly_cost_cny() == 0.0


def test_usage_store_budget_state_reports_exhaustion(tmp_path) -> None:
    store = WorldV2UsageStore(path=str(tmp_path / "usage.sqlite"))
    store.record(
        _Usage(model="deepseek-v4-flash", prompt_tokens=10_000_000, completion_tokens=0)
    )
    state = store.budget_state(monthly_budget_cny=1.0, daily_budget_cny=1.0)
    assert state["monthly_exhausted"] is True
    assert state["daily_exhausted"] is True
    assert state["monthly_cost_cny"] == pytest.approx(10.08, abs=0.05)


def test_usage_store_missing_budget_never_exhausts(tmp_path) -> None:
    store = WorldV2UsageStore(path=str(tmp_path / "usage.sqlite"))
    state = store.budget_state(monthly_budget_cny=None, daily_budget_cny=None)
    assert state["monthly_exhausted"] is False
    assert state["daily_exhausted"] is False
