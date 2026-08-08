"""World V2 model usage recording and monthly budget gating.

The legacy daemon budget gate never covered World V2 calls, so monthly cost
was invisible and unbounded.  This module records every model call through a
usage observer and exposes monthly/daily CNY aggregates plus a monthly gate
the turn entry can consult before spending another provider call.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from ..usage_metrics import estimate_model_cost_usd

_USD_TO_CNY = 7.2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS world_v2_model_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    world_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    cost_cny REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0
)
"""


class WorldV2UsageStore:
    """SQLite-backed usage recording plus cost aggregates."""

    def __init__(self, *, path: str, usd_to_cny: float = _USD_TO_CNY) -> None:
        if not path:
            raise ValueError("world v2 usage store requires a database path")
        self._path = path
        self._usd_to_cny = usd_to_cny
        self._lock = threading.RLock()
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            connection.execute(_SCHEMA)
        finally:
            connection.close()

    def record(self, usage: object) -> None:
        """usage_observer-compatible callback; never raises on telemetry gaps."""
        try:
            self._record_usage(usage)
        except Exception:
            # Observability must never turn a model response into a failure.
            pass

    def _record_usage(self, usage: object) -> None:
        model = str(getattr(usage, "model", "") or "")
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cache_hit_tokens = int(getattr(usage, "cache_hit_tokens", 0) or 0)
        cache_miss_tokens = int(getattr(usage, "cache_miss_tokens", 0) or 0)
        usd, _version = estimate_model_cost_usd(
            model=model or "__unpriced__",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )
        cost_cny = round(usd * self._usd_to_cny, 4)
        recorded_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            connection = sqlite3.connect(
                self._path, isolation_level=None, check_same_thread=False
            )
            try:
                connection.execute(
                    """
                    INSERT INTO world_v2_model_usage (
                        recorded_at, world_id, turn_id, purpose, model, status,
                        provider, prompt_tokens, completion_tokens, cache_hit_tokens,
                        cache_miss_tokens, total_tokens, error, cost_cny, latency_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recorded_at,
                        str(getattr(usage, "world_id", "") or ""),
                        str(getattr(usage, "turn_id", "") or ""),
                        str(getattr(usage, "purpose", "") or ""),
                        model,
                        str(getattr(usage, "status", "") or ""),
                        str(getattr(usage, "provider", "") or ""),
                        prompt_tokens,
                        completion_tokens,
                        cache_hit_tokens,
                        cache_miss_tokens,
                        int(getattr(usage, "total_tokens", 0) or 0),
                        str(getattr(usage, "error", "") or "")[:512],
                        cost_cny,
                        int(getattr(usage, "latency_ms", 0) or 0),
                    ),
                )
            finally:
                connection.close()

    def cost_since(self, *, since: datetime) -> float:
        """Sum cost_cny for records recorded after ``since`` (UTC)."""
        with self._lock:
            connection = sqlite3.connect(
                self._path, isolation_level=None, check_same_thread=False
            )
            try:
                row = connection.execute(
                    "SELECT COALESCE(SUM(cost_cny), 0) FROM world_v2_model_usage "
                    "WHERE recorded_at >= ?",
                    (since.isoformat(),),
                ).fetchone()
                return float(row[0] if row is not None else 0.0)
            finally:
                connection.close()

    def monthly_cost_cny(self) -> float:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self.cost_since(since=month_start)

    def daily_cost_cny(self) -> float:
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.cost_since(since=day_start)

    def budget_state(
        self,
        *,
        monthly_budget_cny: float | None,
        daily_budget_cny: float | None,
    ) -> dict[str, object]:
        monthly = self.monthly_cost_cny()
        daily = self.daily_cost_cny()
        return {
            "monthly_cost_cny": round(monthly, 2),
            "monthly_budget_cny": monthly_budget_cny,
            "monthly_exhausted": (
                monthly_budget_cny is not None and monthly >= monthly_budget_cny
            ),
            "daily_cost_cny": round(daily, 2),
            "daily_budget_cny": daily_budget_cny,
            "daily_exhausted": (
                daily_budget_cny is not None and daily >= daily_budget_cny
            ),
        }


__all__ = [
    "WorldV2UsageStore",
]
