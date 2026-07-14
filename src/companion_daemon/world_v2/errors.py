from __future__ import annotations


class WorldV2Error(RuntimeError):
    """Base error for World v2 contracts."""


class ConcurrencyConflict(WorldV2Error):
    """The caller evaluated against stale world or deliberation state."""


class UnknownEventType(WorldV2Error):
    """An event without a declared revision class or reducer was submitted."""


class IdempotencyConflict(WorldV2Error):
    """An idempotency identity was reused for different immutable content."""
