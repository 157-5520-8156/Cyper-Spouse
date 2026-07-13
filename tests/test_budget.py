from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
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


def test_model_budget_remaining_uses_persisted_real_token_cost(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "model-budget.sqlite")
    store.record_model_usage(
        purpose="reply",
        model="deepseek-v4-flash",
        status="succeeded",
        latency_ms=100,
        prompt_tokens=1_000_000,
        completion_tokens=0,
        cache_hit_tokens=0,
        cache_miss_tokens=1_000_000,
        total_tokens=1_000_000,
    )
    gate = BudgetGate(
        store,
        monthly_budget_cny=10,
        daily_budget_cny=2,
        soft_daily_budget_cny=1.01,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )

    # One million cache-miss input tokens cost USD 0.14, or CNY 1.008 at
    # the persisted report rate. This must reduce the automatic budget.
    assert 0 <= gate.remaining_model_budget_cny(automatic=True) < 0.01


def test_model_call_reservation_is_atomic_across_concurrent_budget_gates(
    tmp_path: Path,
) -> None:
    """Two concurrent turns cannot both spend the same remaining model budget."""
    path = tmp_path / "atomic-model-budget.sqlite"

    def reserve(reservation_id: str):
        gate = BudgetGate(
            CompanionStore(path),
            monthly_budget_cny=0.03,
            daily_budget_cny=0.02,
            soft_daily_budget_cny=0.02,
            monthly_image_limit=20,
            monthly_vision_limit=120,
            monthly_audio_limit=60,
        )
        return gate.reserve_model_call(
            reservation_id=reservation_id,
            estimated_cny=0.015,
            automatic=True,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(executor.map(reserve, ("turn-a", "turn-b")))

    assert sum(decision.allowed for decision in decisions) == 1
    assert {decision.reason for decision in decisions} == {
        "reserved",
        "daily_budget_exceeded",
    }


def test_model_call_reservation_settles_actual_usage_and_releases_failures(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "model-reservation.sqlite")
    gate = BudgetGate(
        store,
        monthly_budget_cny=0.05,
        daily_budget_cny=0.05,
        soft_daily_budget_cny=0.05,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )

    reserved = gate.reserve_model_call(
        reservation_id="successful-call",
        estimated_cny=0.04,
        automatic=True,
    )
    assert reserved.allowed
    store.record_model_usage(
        purpose="reply",
        model="deepseek-v4-flash",
        status="succeeded",
        latency_ms=20,
        prompt_tokens=1_000,
        cache_miss_tokens=1_000,
        total_tokens=1_000,
        budget_reservation_id="successful-call",
    )

    # The real price is about CNY 0.001, not the CNY 0.04 preflight envelope.
    # Settlement must return the unused envelope before the next call reserves.
    assert gate.reserve_model_call(
        reservation_id="next-call",
        estimated_cny=0.04,
        automatic=True,
    ).allowed

    failed = gate.reserve_model_call(
        reservation_id="failed-call",
        estimated_cny=0.005,
        automatic=True,
    )
    assert failed.allowed
    store.record_model_usage(
        purpose="reply",
        model="deepseek-v4-flash",
        status="failed",
        latency_ms=20,
        budget_reservation_id="failed-call",
        # A provider-side validation rejection is evidence that no billable
        # completion was created. Generic failed calls intentionally default
        # to unknown billing and keep their envelope.
        billing_state="not_billed",
    )

    assert gate.reserve_model_call(
        reservation_id="after-failure",
        estimated_cny=0.005,
        automatic=True,
    ).allowed


def test_model_call_with_unknown_billing_keeps_its_envelope_after_usage_persistence_fails(
    tmp_path: Path,
) -> None:
    """An emitted provider request stays charged when its usage row is unavailable."""
    store = CompanionStore(tmp_path / "unknown-model-billing.sqlite")
    gate = BudgetGate(
        store,
        monthly_budget_cny=0.05,
        daily_budget_cny=0.05,
        soft_daily_budget_cny=0.05,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )

    reserved = gate.reserve_model_call(
        reservation_id="emitted-but-unpersisted",
        estimated_cny=0.04,
        automatic=True,
    )
    assert reserved.allowed
    assert gate.start_model_call("emitted-but-unpersisted")

    gate.finalize_model_call(
        "emitted-but-unpersisted",
        request_emitted=True,
        usage_persisted=False,
    )

    assert not gate.reserve_model_call(
        reservation_id="would-overrun-after-unknown-billing",
        estimated_cny=0.02,
        automatic=True,
    ).allowed


def test_expired_unstarted_reservations_release_but_started_calls_become_unknown(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "model-reservation-lease.sqlite")
    gate = BudgetGate(
        store,
        monthly_budget_cny=0.10,
        daily_budget_cny=0.10,
        soft_daily_budget_cny=0.10,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    now = datetime(2032, 4, 3, 12, tzinfo=UTC)
    assert gate.reserve_model_call(
        reservation_id="never-started",
        estimated_cny=0.04,
        automatic=True,
        now=now,
        lease_seconds=60,
    ).allowed
    assert gate.reserve_model_call(
        reservation_id="started-before-crash",
        estimated_cny=0.04,
        automatic=True,
        now=now,
        lease_seconds=60,
    ).allowed
    assert gate.start_model_call("started-before-crash", now=now, lease_seconds=60)

    # Query-time recovery is enough after a crash: work never started returns
    # capacity, while a started request is held at its conservative envelope.
    recovered = now.replace(minute=2)
    assert not gate.reserve_model_call(
        reservation_id="new-call",
        estimated_cny=0.07,
        automatic=True,
        now=recovered,
    ).allowed
    assert gate.reserve_model_call(
        reservation_id="fits-after-unstarted-release",
        estimated_cny=0.06,
        automatic=True,
        now=recovered,
    ).allowed
