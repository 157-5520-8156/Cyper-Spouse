"""Immutable, same-cursor evidence for deterministic World v2 replay checks.

This module deliberately sits beside, rather than inside, :mod:`ledger`.  A
replay evaluator needs one coherent snapshot of authority, not a collection of
individually-racy read methods.  Adapters that can provide that snapshot expose
the small ``ReplayEvidencePort`` seam; the details of locks, transactions and
event storage stay private to each adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .schemas import CommitResult, LedgerProjection, ProjectionCursor, WorldEvent


@dataclass(frozen=True, slots=True)
class ReplayEventEvidence:
    """One immutable ledger event at the exported cursor boundary."""

    event: WorldEvent
    commit_id: str
    cursor: ProjectionCursor
    event_envelope_hash: str


@dataclass(frozen=True, slots=True)
class ReplayCommitEvidence:
    """The exact commit binding a contiguous group of exported events."""

    commit_id: str
    request_hash: str
    result: CommitResult


@dataclass(frozen=True, slots=True)
class ReplayEvidence:
    """A coherent authority snapshot for a pure replay evaluator.

    ``projection`` is the adapter's persisted/head projection at ``cursor``.
    ``replay`` is independently reduced from the exact events in this object,
    while holding the same adapter-level snapshot.  Events and commits retain
    their ledger ordering and are sufficient for an evaluator to verify their
    binding without accessing mutable ledger state again.
    """

    world_id: str
    cursor: ProjectionCursor
    reducer_bundle_version: str
    projection: LedgerProjection
    replay: LedgerProjection
    events: tuple[ReplayEventEvidence, ...]
    commits: tuple[ReplayCommitEvidence, ...]


class ReplayEvidencePort(Protocol):
    """Adapter seam for exporting one deterministic replay snapshot."""

    def export_replay_evidence(
        self, *, at_cursor: ProjectionCursor | None = None
    ) -> ReplayEvidence: ...


__all__ = [
    "ReplayCommitEvidence",
    "ReplayEvidence",
    "ReplayEvidencePort",
    "ReplayEventEvidence",
]
