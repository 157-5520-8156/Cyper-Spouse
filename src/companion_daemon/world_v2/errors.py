from __future__ import annotations


class WorldV2Error(RuntimeError):
    """Base error for World v2 contracts."""


class ConcurrencyConflict(WorldV2Error):
    """The caller evaluated against stale world or deliberation state."""


class UnknownEventType(WorldV2Error):
    """An event without a declared revision class or reducer was submitted."""


class IdempotencyConflict(WorldV2Error):
    """An idempotency identity was reused for different immutable content."""


class UnknownAction(WorldV2Error):
    """An external result referenced an Action that does not exist in this World."""


class LedgerIntegrityError(WorldV2Error):
    """Persisted ledger bytes, hashes, revisions, or projections do not agree."""


class InvalidActionTransition(WorldV2Error):
    """An Action lifecycle event attempted a transition outside the frozen graph."""


class ActionIdentityMismatch(WorldV2Error):
    """A receipt's immutable Action or provider identity does not match the ledger."""
