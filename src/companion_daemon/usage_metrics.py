"""Versioned model pricing and deterministic usage aggregation primitives."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ModelPrice:
    model: str
    version: str
    cache_hit_usd_per_million: float
    cache_miss_usd_per_million: float
    output_usd_per_million: float


# DeepSeek public price table observed 2026-07-12. Historical rows persist the
# version and computed USD amount so later price changes do not rewrite history.
DEEPSEEK_V4_FLASH_PRICE = ModelPrice(
    model="deepseek-v4-flash",
    version="deepseek-2026-07-12",
    cache_hit_usd_per_million=0.0028,
    cache_miss_usd_per_million=0.14,
    output_usd_per_million=0.28,
)


def estimate_model_cost_usd(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
) -> tuple[float, str]:
    if model != DEEPSEEK_V4_FLASH_PRICE.model:
        return 0.0, "unknown"
    price = DEEPSEEK_V4_FLASH_PRICE
    hit = max(0, cache_hit_tokens)
    miss = max(0, cache_miss_tokens)
    # Older/partial provider payloads may omit cache details. Conservatively
    # price all observed prompt tokens as cache misses.
    if hit + miss == 0:
        miss = max(0, prompt_tokens)
    amount = (
        hit * price.cache_hit_usd_per_million
        + miss * price.cache_miss_usd_per_million
        + max(0, completion_tokens) * price.output_usd_per_million
    ) / 1_000_000
    return amount, price.version


def nearest_rank(values: Iterable[int], percentile: float) -> int:
    ordered = sorted(max(0, int(value)) for value in values)
    if not ordered:
        return 0
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def aggregate_usage_rows(
    rows: Iterable[Mapping[str, object]], *, cny_per_usd: float
) -> dict[str, object]:
    materialized = list(rows)
    calls = len(materialized)
    succeeded = sum(1 for row in materialized if row["status"] == "succeeded")
    latencies = [int(row["latency_ms"] or 0) for row in materialized]
    usd = sum(float(row["estimated_cost_usd"] or 0.0) for row in materialized)
    return {
        "calls": calls,
        "succeeded_calls": succeeded,
        "failed_calls": calls - succeeded,
        "success_rate": succeeded / calls if calls else 0.0,
        "prompt_tokens": sum(int(row["prompt_tokens"] or 0) for row in materialized),
        "completion_tokens": sum(int(row["completion_tokens"] or 0) for row in materialized),
        "reasoning_tokens": sum(int(row["reasoning_tokens"] or 0) for row in materialized),
        "cache_hit_tokens": sum(int(row["cache_hit_tokens"] or 0) for row in materialized),
        "cache_miss_tokens": sum(int(row["cache_miss_tokens"] or 0) for row in materialized),
        "total_tokens": sum(int(row["total_tokens"] or 0) for row in materialized),
        "latency_ms": sum(latencies),
        "p50_latency_ms": nearest_rank(latencies, 0.50),
        "p95_latency_ms": nearest_rank(latencies, 0.95),
        "attempts": calls,
        "max_attempt": max((max(1, int(row["attempt"] or 1)) for row in materialized), default=0),
        "estimated_cost_usd": usd,
        "estimated_cost_cny": usd * max(0.0, cny_per_usd),
    }
