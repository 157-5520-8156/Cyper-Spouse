"""Process-local rolling reliability counters for the visible chat lane.

The ledger remains the durable audit authority (``ModelResultRecorded`` and
``MessagePayloadStored`` events); this module only makes the current failure
rate checkable at a glance from ``/health`` without a ledger scan.  Counters
are in-memory, thread-safe, bounded, and reset on process restart — the
``since`` field makes that window explicit.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import threading
import time


_WINDOW_SECONDS = 24 * 3600.0
_MAX_EVENTS_PER_KIND = 20_000

# One deque of unix timestamps per counter kind.  Kinds are a closed set so a
# typo cannot silently create a new dashboard field.
_KINDS = (
    # One inbound turn produced an authorized visible reply (denominator).
    "visible_replies",
    # A local expression failsafe (canned acknowledgement or intent-bounded
    # boundary line) became the visible reply candidate.
    "failsafe",
    # One corrective retry repaired a world-claim bookkeeping near-miss.
    "claim_repair",
    # One corrective retry repaired a non-claim structural draft violation.
    "shape_repair",
    # One claim-free boundary line was chosen for an unverifiable world probe.
    "claim_free",
    # The configured backup provider produced the reply after a main failure.
    "backup_recovery",
)

_lock = threading.Lock()
_events: dict[str, deque[float]] = {kind: deque(maxlen=_MAX_EVENTS_PER_KIND) for kind in _KINDS}
_process_started_at = datetime.now(UTC)


def _record(kind: str) -> None:
    now = time.time()
    with _lock:
        bucket = _events[kind]
        bucket.append(now)
        _prune(bucket, now)


def _prune(bucket: deque[float], now: float) -> None:
    horizon = now - _WINDOW_SECONDS
    while bucket and bucket[0] < horizon:
        bucket.popleft()


def record_visible_reply() -> None:
    _record("visible_replies")


def record_failsafe() -> None:
    _record("failsafe")


def record_claim_repair() -> None:
    _record("claim_repair")


def record_shape_repair() -> None:
    _record("shape_repair")


def record_claim_free_reply() -> None:
    _record("claim_free")


def record_backup_recovery() -> None:
    _record("backup_recovery")


def reliability_snapshot() -> dict[str, object]:
    """Read-only rolling counts for the last 24 hours plus the failsafe rate."""

    now = time.time()
    with _lock:
        counts = {}
        for kind, bucket in _events.items():
            _prune(bucket, now)
            counts[kind] = len(bucket)
    visible = counts["visible_replies"]
    failsafe = counts["failsafe"]
    return {
        "window_hours": 24,
        "since": _process_started_at.isoformat(),
        "visible_replies_24h": visible,
        "failsafe_24h": failsafe,
        "failsafe_rate_24h": (round(failsafe / visible, 4) if visible else None),
        "claim_repair_24h": counts["claim_repair"],
        "shape_repair_24h": counts["shape_repair"],
        "claim_free_24h": counts["claim_free"],
        "backup_recovery_24h": counts["backup_recovery"],
    }


def reset_for_tests() -> None:
    """Clear all counters; only test isolation may call this."""

    with _lock:
        for bucket in _events.values():
            bucket.clear()


__all__ = [
    "record_backup_recovery",
    "record_claim_free_reply",
    "record_claim_repair",
    "record_failsafe",
    "record_shape_repair",
    "record_visible_reply",
    "reliability_snapshot",
    "reset_for_tests",
]
