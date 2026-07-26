"""Crash and replay helpers for bounded vertical tests."""

from __future__ import annotations

from dataclasses import dataclass

from companion_daemon.world_v2.ledger import canonical_event_json


class CrashInjected(RuntimeError):
    """An interruption injected at one ledger commit boundary."""


class CrashingLedger:
    """Ledger delegate that interrupts the selected commit exactly once."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.commits_seen = 0
        self._crash_at: int | None = None
        self._mode = "pre"

    def arm(self, *, crash_at_commit: int, mode: str) -> None:
        if mode not in {"pre", "post"}:
            raise ValueError("crash mode must be pre or post")
        self._crash_at = crash_at_commit
        self._mode = mode
        self.commits_seen = 0

    def disarm(self) -> None:
        self._crash_at = None

    def _guard(self, apply):
        self.commits_seen += 1
        if self._crash_at is not None and self.commits_seen == self._crash_at:
            if self._mode == "pre":
                self.disarm()
                raise CrashInjected(f"pre-commit crash at lane commit {self.commits_seen}")
            apply()
            self.disarm()
            raise CrashInjected(f"post-commit crash at lane commit {self.commits_seen}")
        return apply()

    @property
    def world_id(self) -> str:
        return self._inner.world_id

    @property
    def blocks_event_loop(self) -> bool:
        return self._inner.blocks_event_loop

    def commit(self, events, **kwargs):
        return self._guard(lambda: self._inner.commit(events, **kwargs))

    def commit_at_cursor(self, events, **kwargs):
        return self._guard(lambda: self._inner.commit_at_cursor(events, **kwargs))

    def commit_accepted(self, batch, **kwargs):
        return self._guard(lambda: self._inner.commit_accepted(batch, **kwargs))

    def project(self):
        return self._inner.project()

    def project_at(self, cursor):
        return self._inner.project_at(cursor)

    def observation_events_at(self, locators, *, cursor):
        return self._inner.observation_events_at(locators, cursor=cursor)

    def lookup_event_commit(self, event_id):
        return self._inner.lookup_event_commit(event_id)

    def resolve_committed_event_refs(self, event_ids, *, at_world_revision):
        return self._inner.resolve_committed_event_refs(
            event_ids, at_world_revision=at_world_revision
        )

    def resolve_initial_world_event_ref(self, *, at_world_revision):
        return self._inner.resolve_initial_world_event_ref(
            at_world_revision=at_world_revision
        )


@dataclass(frozen=True)
class LedgerTail:
    commits: tuple[tuple[str, str], ...]
    events: tuple[tuple[int, str, str, str, str], ...]
    semantic_hash: str
    world_revision: int
    deliberation_revision: int
    ledger_sequence: int


def ledger_tail(ledger, *, since_ledger_sequence: int = 0) -> LedgerTail:
    evidence = ledger.export_replay_evidence()
    events = tuple(
        (
            item.cursor.ledger_sequence,
            item.commit_id,
            item.event.event_id,
            item.event.idempotency_key,
            canonical_event_json(item.event),
        )
        for item in evidence.events
        if item.cursor.ledger_sequence > since_ledger_sequence
    )
    tail_commit_ids = {item[1] for item in events}
    commits = tuple(
        (item.commit_id, item.request_hash)
        for item in evidence.commits
        if item.commit_id in tail_commit_ids
    )
    return LedgerTail(
        commits=commits,
        events=events,
        semantic_hash=evidence.projection.semantic_hash,
        world_revision=evidence.projection.world_revision,
        deliberation_revision=evidence.projection.deliberation_revision,
        ledger_sequence=evidence.projection.ledger_sequence,
    )


def assert_identical_tails(left: LedgerTail, right: LedgerTail, *, label: str) -> None:
    assert left.ledger_sequence == right.ledger_sequence, f"{label}: ledger lengths diverged"
    assert left.events == right.events, f"{label}: event bytes diverged"
    assert left.commits == right.commits, f"{label}: commit hashes diverged"
    assert left.semantic_hash == right.semantic_hash, f"{label}: semantic hash diverged"
    assert (
        left.world_revision,
        left.deliberation_revision,
    ) == (
        right.world_revision,
        right.deliberation_revision,
    ), f"{label}: final revisions diverged"


__all__ = [
    "CrashInjected",
    "CrashingLedger",
    "LedgerTail",
    "assert_identical_tails",
    "ledger_tail",
]
