from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from .errors import ConcurrencyConflict, IdempotencyConflict
from .reducers import (
    REDUCER_BUNDLE_VERSION,
    RevisionClass,
    ReducerState,
    event_definition,
    make_projection,
    reduce_event,
    require_reducer_bundle,
)
from .schemas import CommitResult, LedgerProjection, ProjectionCursor, WorldEvent


class LedgerPort(Protocol):
    """Persistence seam consumed by WorldRuntime; adapters own concurrency semantics."""

    @property
    def world_id(self) -> str: ...

    @property
    def blocks_event_loop(self) -> bool: ...

    def commit(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_world_revision: int,
        expected_deliberation_revision: int,
        commit_id: str | None = None,
    ) -> CommitResult: ...

    def project(self) -> LedgerProjection: ...

    def project_at(self, cursor: ProjectionCursor) -> LedgerProjection: ...


@dataclass(frozen=True, slots=True)
class _StoredEvent:
    ledger_sequence: int
    world_revision: int
    deliberation_revision: int
    event: WorldEvent


@dataclass(frozen=True, slots=True)
class _StoredCommit:
    request_hash: str
    result: CommitResult


def canonical_event_json(event: WorldEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def commit_request_hash(events: Sequence[WorldEvent]) -> str:
    encoded = json.dumps(
        [json.loads(canonical_event_json(event)) for event in events],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derived_commit_id(events: Sequence[WorldEvent]) -> str:
    return f"commit:{commit_request_hash(events)}"


class WorldLedger:
    """Append-only World v2 ledger with separated revision streams."""

    def __init__(self, *, world_id: str) -> None:
        self._world_id = world_id
        self._events: list[_StoredEvent] = []
        self._by_idempotency: dict[str, _StoredEvent] = {}
        self._by_event_id: dict[str, _StoredEvent] = {}
        self._commits: dict[str, _StoredCommit] = {}
        self._world_revision = 0
        self._deliberation_revision = 0
        self._state = ReducerState()

    @classmethod
    def in_memory(cls, *, world_id: str) -> WorldLedger:
        return cls(world_id=world_id)

    @property
    def world_id(self) -> str:
        return self._world_id

    @property
    def blocks_event_loop(self) -> bool:
        return False

    def commit(
        self,
        events: Sequence[WorldEvent],
        *,
        commit_id: str | None = None,
        expected_world_revision: int,
        expected_deliberation_revision: int,
    ) -> CommitResult:
        if not events:
            raise ValueError("commit requires at least one event")
        commit_id = commit_id or derived_commit_id(events)
        if not commit_id:
            raise ValueError("commit_id must not be empty")
        request_hash = commit_request_hash(events)
        existing_commit = self._commits.get(commit_id)
        if existing_commit is not None:
            if existing_commit.request_hash != request_hash:
                raise IdempotencyConflict(f"commit_id {commit_id!r} has different content")
            return existing_commit.result

        event_ids = [event.event_id for event in events]
        idempotency_keys = [event.idempotency_key for event in events]
        if len(set(event_ids)) != len(event_ids):
            raise IdempotencyConflict("duplicate event_id inside one commit")
        if len(set(idempotency_keys)) != len(idempotency_keys):
            raise IdempotencyConflict("duplicate idempotency key inside one commit")

        definitions = []
        for event in events:
            if event.world_id != self._world_id:
                raise ValueError("event belongs to another world")
            definitions.append(event_definition(event.event_type))
            existing_by_id = self._by_event_id.get(event.event_id)
            if existing_by_id is not None:
                raise IdempotencyConflict(f"event_id {event.event_id!r} already exists")
            existing = self._by_idempotency.get(event.idempotency_key)
            if existing is not None:
                raise IdempotencyConflict(
                    f"idempotency key {event.idempotency_key!r} already exists under "
                    "a different commit"
                )

        revision_classes = {definition.revision_class for definition in definitions}
        if (
            RevisionClass.WORLD in revision_classes
            and expected_world_revision != self._world_revision
        ):
            raise ConcurrencyConflict("stale world revision")
        if (
            RevisionClass.DELIBERATION in revision_classes
            and expected_deliberation_revision != self._deliberation_revision
        ):
            raise ConcurrencyConflict("stale deliberation revision")

        next_world_revision = self._world_revision
        next_deliberation_revision = self._deliberation_revision
        next_state = self._state
        staged: list[_StoredEvent] = []
        for event, definition in zip(events, definitions, strict=True):
            if definition.revision_class is RevisionClass.WORLD:
                next_world_revision += 1
            else:
                next_deliberation_revision += 1
            next_state = reduce_event(next_state, event)
            staged.append(
                _StoredEvent(
                    ledger_sequence=len(self._events) + len(staged) + 1,
                    world_revision=next_world_revision,
                    deliberation_revision=next_deliberation_revision,
                    event=event,
                )
            )

        self._events.extend(staged)
        self._by_idempotency.update(
            (stored.event.idempotency_key, stored) for stored in staged
        )
        self._by_event_id.update((stored.event.event_id, stored) for stored in staged)
        self._world_revision = next_world_revision
        self._deliberation_revision = next_deliberation_revision
        self._state = next_state
        result = CommitResult(
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=len(self._events),
            event_ids=tuple(event.event_id for event in events),
        )
        self._commits[commit_id] = _StoredCommit(request_hash=request_hash, result=result)
        return result

    def project(self) -> LedgerProjection:
        return make_projection(
            world_id=self._world_id,
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=len(self._events),
            state=self._state,
        )

    def project_at(self, cursor: ProjectionCursor) -> LedgerProjection:
        head_cursor = ProjectionCursor(
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=len(self._events),
        )
        if cursor.ledger_sequence > len(self._events):
            raise ValueError("requested projection cursor is outside the ledger range")
        if cursor == head_cursor:
            return self.project()
        state = ReducerState()
        reached_world_revision = 0
        reached_deliberation_revision = 0
        ledger_sequence = 0
        for stored in self._events:
            if stored.ledger_sequence > cursor.ledger_sequence:
                break
            state = reduce_event(state, stored.event)
            reached_world_revision = stored.world_revision
            reached_deliberation_revision = stored.deliberation_revision
            ledger_sequence = stored.ledger_sequence
        reached = ProjectionCursor(
            world_revision=reached_world_revision,
            deliberation_revision=reached_deliberation_revision,
            ledger_sequence=ledger_sequence,
        )
        if reached != cursor:
            raise ValueError("requested projection cursor is not present in the ledger")
        return make_projection(
            world_id=self._world_id,
            world_revision=reached_world_revision,
            deliberation_revision=reached_deliberation_revision,
            ledger_sequence=ledger_sequence,
            state=state,
        )

    def rebuild(
        self, *, reducer_bundle_version: str = REDUCER_BUNDLE_VERSION
    ) -> LedgerProjection:
        require_reducer_bundle(reducer_bundle_version)
        state = ReducerState()
        world_revision = 0
        deliberation_revision = 0
        for stored in self._events:
            definition = event_definition(stored.event.event_type)
            if definition.revision_class is RevisionClass.WORLD:
                world_revision += 1
            else:
                deliberation_revision += 1
            state = reduce_event(state, stored.event)
        return make_projection(
            world_id=self._world_id,
            world_revision=world_revision,
            deliberation_revision=deliberation_revision,
            ledger_sequence=len(self._events),
            state=state,
            reducer_bundle_version=reducer_bundle_version,
        )
