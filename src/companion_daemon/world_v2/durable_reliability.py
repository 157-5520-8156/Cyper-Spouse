"""Ledger-window reliability rates for the visible chat lane.

Process-local counters in ``production_reliability_metrics`` reset on restart.
This module scans the durable World-v2 ledger so the failsafe rate survives
restarts and matches the diagnosis methodology in
``output/failsafe-diagnosis/``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3
import threading
import time
from typing import Any


_CACHE_TTL_SECONDS = 60.0
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload_json")
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _is_failsafe_audit(audit: dict[str, Any]) -> bool:
    model_id = str(audit.get("model_id") or "")
    model_version = str(audit.get("model_version") or "")
    return (
        "local-expression-failsafe" in model_id
        or "local-expression-failsafe" in model_version
    )


def durable_reliability_snapshot(
    database_path: str,
    *,
    hours: float = 24.0,
    now: datetime | None = None,
    max_events: int = 8_000,
) -> dict[str, Any]:
    """Compute visible-reply and failsafe rates from a read-only ledger window.

    Walks newest events first until the time cutoff or ``max_events``.  A short
    process cache keeps ``/health`` from rescanning on every probe.
    """

    if hours <= 0 or max_events <= 0:
        raise ValueError("durable reliability window must be positive")
    cache_key = f"{database_path}|{hours}|{max_events}"
    cached_at = time.monotonic()
    with _lock:
        hit = _cache.get(cache_key)
        if hit is not None and cached_at - hit[0] < _CACHE_TTL_SECONDS:
            return dict(hit[1])

    wall = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = wall - timedelta(hours=hours)
    delivered_ids: set[str] = set()
    authorized_replies: list[tuple[datetime, str, str]] = []
    failsafe_by_attempt: set[str] = set()
    scanned = 0
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT event_json FROM world_v2_events ORDER BY ledger_sequence DESC LIMIT ?",
            (max_events,),
        )
        for (event_json,) in rows:
            scanned += 1
            event = json.loads(event_json)
            logical = _parse_time(event.get("logical_time"))
            if logical is not None and logical < cutoff:
                continue
            event_type = event.get("event_type")
            payload = _payload(event)
            if event_type == "ActionDelivered":
                action_id = payload.get("action_id")
                if isinstance(action_id, str) and action_id:
                    delivered_ids.add(action_id)
                nested = payload.get("action")
                if isinstance(nested, dict):
                    nested_id = nested.get("action_id")
                    if isinstance(nested_id, str) and nested_id:
                        delivered_ids.add(nested_id)
            elif event_type == "ActionAuthorized":
                action = payload.get("action")
                if not isinstance(action, dict) or action.get("kind") != "reply":
                    continue
                action_id = action.get("action_id")
                if isinstance(action_id, str) and action_id and logical is not None:
                    authorized_replies.append((logical, action_id, str(event.get("correlation_id") or "")))
            elif event_type == "ModelResultRecorded":
                try:
                    audit = json.loads(payload.get("audit_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(audit, dict) and _is_failsafe_audit(audit):
                    attempt = str(audit.get("attempt_id") or audit.get("model_call_id") or "")
                    if attempt:
                        failsafe_by_attempt.add(attempt)
    finally:
        connection.close()

    visible = sum(1 for _, action_id, _ in authorized_replies if action_id in delivered_ids)
    failsafe = len(failsafe_by_attempt)
    snapshot = {
        "window_hours": hours,
        "as_of": wall.isoformat(),
        "events_scanned": scanned,
        "visible_delivered_24h": visible,
        "failsafe_model_results_24h": failsafe,
        "failsafe_rate_24h": (round(failsafe / visible, 4) if visible else None),
        "source": "ledger",
    }
    with _lock:
        _cache[cache_key] = (time.monotonic(), dict(snapshot))
    return snapshot


def clear_durable_reliability_cache_for_tests() -> None:
    with _lock:
        _cache.clear()


__all__ = [
    "clear_durable_reliability_cache_for_tests",
    "durable_reliability_snapshot",
]
