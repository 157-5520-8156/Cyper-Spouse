"""Bounded read-only storage growth diagnostics for the production ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from threading import Lock
import time


_CACHE_LOCK = Lock()
_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_CACHE_SECONDS = 30.0


def ledger_storage_snapshot(
    database: str | Path, *, now: datetime | None = None
) -> dict[str, object]:
    path = Path(database).expanduser().resolve()
    current = now or datetime.now(UTC)
    cache_key = str(path)
    if now is None:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return dict(cached[1])
    cutoff = (current - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    wal = Path(str(path) + "-wal")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        total, payload_bytes, idle = connection.execute(
            """SELECT COUNT(*), COALESCE(SUM(length(event_json)),0),
                      COALESCE(SUM(CASE
                        WHEN json_extract(event_json,'$.event_type')='ClockAdvanced'
                          OR (json_extract(event_json,'$.event_type') LIKE 'TriggerProcess%'
                              AND json_extract(event_json,'$.source')
                                  ='world-v2:life-ecology-trigger-store')
                        THEN 1 ELSE 0 END),0)
                 FROM world_v2_events
                WHERE json_extract(event_json,'$.created_at') >= ?""",
            (cutoff,),
        ).fetchone()
        categories = {
            str(event_type): int(count)
            for event_type, count in connection.execute(
                """SELECT json_extract(event_json,'$.event_type'), COUNT(*)
                     FROM world_v2_events
                    WHERE json_extract(event_json,'$.created_at') >= ?
                    GROUP BY 1 ORDER BY 2 DESC""",
                (cutoff,),
            )
        }
    total = int(total)
    payload_bytes = int(payload_bytes)
    idle = int(idle)
    idle_ratio = idle / total if total else 0.0
    warning_reasons = []
    if payload_bytes > 10 * 1024 * 1024:
        warning_reasons.append("event_payload_growth_over_10mb")
    if total and idle_ratio > 0.60:
        warning_reasons.append("clock_trigger_ratio_over_60pct")
    snapshot = {
        "status": "warning" if warning_reasons else "ok",
        "database_bytes": path.stat().st_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "event_count_24h": total,
        "event_payload_bytes_24h": payload_bytes,
        "clock_life_trigger_count_24h": idle,
        "clock_life_trigger_ratio_24h": round(idle_ratio, 4),
        "event_types_24h": categories,
        "warning_reasons": warning_reasons,
    }
    if now is None:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (time.monotonic(), snapshot)
    return dict(snapshot)


__all__ = ["ledger_storage_snapshot"]
