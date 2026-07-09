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
