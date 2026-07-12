from datetime import UTC, datetime
from pathlib import Path

from companion_daemon.budget import BudgetGate, UsageEstimate
from companion_daemon.db import CompanionStore


def test_budget_gate_blocks_soft_daily_for_automatic_calls(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    store.record_usage("vision", 0.95)
    gate = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=1,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )

    decision = gate.check(UsageEstimate("vision", 0.1), automatic=True)

    assert not decision.allowed
    assert decision.reason == "soft_daily_budget_requires_manual"


def test_usage_totals_are_windowed(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    store.record_usage("vision", 0.03)

    assert store.usage_total("day", datetime.now(UTC)) == 0.03
    assert store.usage_count("vision", "month", datetime.now(UTC)) == 1


def test_model_usage_summary_groups_real_tokens_by_purpose(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "model-usage.sqlite")
    store.record_model_usage(
        purpose="reply",
        model="deepseek-v4-flash",
        status="succeeded",
        latency_ms=420,
        prompt_tokens=100,
        completion_tokens=20,
        reasoning_tokens=0,
        cache_hit_tokens=70,
        cache_miss_tokens=30,
        total_tokens=120,
    )
    store.record_model_usage(
        purpose="reply_audit",
        model="deepseek-v4-flash",
        status="succeeded",
        latency_ms=180,
        prompt_tokens=60,
        completion_tokens=8,
        reasoning_tokens=0,
        cache_hit_tokens=40,
        cache_miss_tokens=20,
        total_tokens=68,
    )

    summary = store.model_usage_summary("day", datetime.now(UTC))

    assert summary["reply"]["calls"] == 1
    assert summary["reply"]["total_tokens"] == 120
    assert summary["reply_audit"]["total_tokens"] == 68
    assert summary["_total"]["calls"] == 2
    assert summary["_total"]["total_tokens"] == 188
    assert summary["_total"]["cache_hit_tokens"] == 110
