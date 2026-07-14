from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import ConcurrencyConflict, IdempotencyConflict
from .reducers import RevisionClass, ReducerState, event_definition, make_projection, reduce_event
from .schemas import CommitResult, LedgerProjection, WorldEvent


@dataclass(frozen=True, slots=True)
class _StoredEvent:
    ledger_sequence: int
    world_revision: int
    deliberation_revision: int
    event: WorldEvent


class WorldLedger:
    """Append-only World v2 ledger with separated revision streams."""

    def __init__(self, *, world_id: str) -> None:
        self._world_id = world_id
        self._events: list[_StoredEvent] = []
        self._by_idempotency: dict[str, _StoredEvent] = {}
        self._world_revision = 0
        self._deliberation_revision = 0
        self._state = ReducerState()

    @classmethod
    def in_memory(cls, *, world_id: str) -> WorldLedger:
        return cls(world_id=world_id)

    def commit(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_world_revision: int,
        expected_deliberation_revision: int,
    ) -> CommitResult:
        if not events:
            raise ValueError("commit requires at least one event")

        definitions = []
        for event in events:
            if event.world_id != self._world_id:
                raise ValueError("event belongs to another world")
            definitions.append(event_definition(event.event_type))
            existing = self._by_idempotency.get(event.idempotency_key)
            if existing is not None:
                if existing.event != event:
                    raise IdempotencyConflict(
                        f"idempotency key {event.idempotency_key!r} has different content"
                    )
                if len(events) == 1:
                    return CommitResult(
                        world_revision=existing.world_revision,
                        deliberation_revision=existing.deliberation_revision,
                        ledger_sequence=existing.ledger_sequence,
                        event_ids=(existing.event.event_id,),
                    )
                raise IdempotencyConflict("mixed new and existing events are not atomic")

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
        self._world_revision = next_world_revision
        self._deliberation_revision = next_deliberation_revision
        self._state = next_state
        return CommitResult(
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=len(self._events),
            event_ids=tuple(event.event_id for event in events),
        )

    def project(self) -> LedgerProjection:
        return make_projection(
            world_id=self._world_id,
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=len(self._events),
            state=self._state,
        )

    def rebuild(self) -> LedgerProjection:
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
        )
