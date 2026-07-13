from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.usage_metrics import estimate_model_cost_usd


def test_v4_pro_usage_is_priced_instead_of_being_recorded_as_free() -> None:
    cost, version = estimate_model_cost_usd(
        model="deepseek-v4-pro",
        prompt_tokens=3_000,
        completion_tokens=500,
        cache_hit_tokens=1_000,
        cache_miss_tokens=2_000,
    )

    assert version == "deepseek-2026-07-13"
    assert cost == pytest.approx(0.001308625)


def test_unpriced_model_uses_a_conservative_cost_until_a_verified_price_is_added() -> None:
    cost, version = estimate_model_cost_usd(
        model="future-model",
        prompt_tokens=100,
        completion_tokens=10,
        cache_hit_tokens=0,
        cache_miss_tokens=0,
    )

    assert version == "unpriced-conservative-2026-07-13"
    assert cost > 0


def test_usage_report_links_calls_to_turn_and_reports_percentiles_and_cost(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "usage.sqlite")
    for latency, status, attempt in (
        (100, "succeeded", 1),
        (300, "failed", 2),
        (500, "succeeded", 3),
    ):
        store.record_model_usage(
            purpose="reply",
            model="deepseek-v4-flash",
            status=status,
            latency_ms=latency,
            prompt_tokens=3_000,
            completion_tokens=500,
            cache_hit_tokens=1_000,
            cache_miss_tokens=2_000,
            total_tokens=3_500,
            world_id="world-1",
            turn_id="turn-9",
            action_id=f"action-{attempt}",
            cadence="hot",
            attempt=attempt,
        )

    report = store.model_usage_report("day", datetime.now(UTC), cny_per_usd=7.2)

    turn = report["turns"]["turn-9"]
    assert turn["calls"] == 3
    assert turn["total_tokens"] == 10_500
    assert turn["p50_latency_ms"] == 300
    assert turn["p95_latency_ms"] == 500
    assert turn["success_rate"] == pytest.approx(2 / 3)
    assert turn["estimated_cost_usd"] == pytest.approx(0.0012684)
    assert turn["estimated_cost_cny"] == pytest.approx(0.00913248)
    group = report["groups"]["reply|hot|deepseek-v4-flash"]
    assert group["failed_calls"] == 1
    assert group["attempts"] == 3


def test_model_usage_schema_adds_linkage_columns_to_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table model_usage_events (
              id integer primary key autoincrement,
              purpose text not null, model text not null, status text not null,
              latency_ms integer not null, prompt_tokens integer not null,
              completion_tokens integer not null, reasoning_tokens integer not null,
              cache_hit_tokens integer not null, cache_miss_tokens integer not null,
              total_tokens integer not null, error text not null, created_at text not null
            )
            """
        )

    store = CompanionStore(path)
    store.record_model_usage(
        purpose="reply",
        model="deepseek-v4-flash",
        status="succeeded",
        latency_ms=20,
        turn_id="migrated-turn",
        cadence="warm",
    )

    report = store.model_usage_report("day", datetime.now(UTC))
    assert report["turns"]["migrated-turn"]["calls"] == 1
