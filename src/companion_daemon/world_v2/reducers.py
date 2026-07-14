from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Any

from .errors import UnknownEventType
from .schemas import LedgerProjection, WorldEvent


REDUCER_BUNDLE_VERSION = "world-v2-reducers.1"


class RevisionClass(StrEnum):
    WORLD = "world"
    DELIBERATION = "deliberation"


@dataclass(frozen=True, slots=True)
class ReducerState:
    observation_refs: tuple[str, ...] = ()
    logical_time: datetime | None = None


Reducer = Callable[[ReducerState, WorldEvent], ReducerState]


@dataclass(frozen=True, slots=True)
class EventDefinition:
    event_type: str
    revision_class: RevisionClass
    reducer: Reducer


def _audit_only(state: ReducerState, _event: WorldEvent) -> ReducerState:
    return state


def _world_started(state: ReducerState, _event: WorldEvent) -> ReducerState:
    return state


def _observation_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    observation_id = event.payload().get("observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise ValueError("ObservationRecorded requires observation_id")
    if observation_id in state.observation_refs:
        return state
    return replace(
        state,
        observation_refs=(*state.observation_refs, observation_id),
        logical_time=event.logical_time,
    )


_EVENTS = {
    definition.event_type: definition
    for definition in (
        EventDefinition("WorldStarted", RevisionClass.WORLD, _world_started),
        EventDefinition("ObservationRecorded", RevisionClass.WORLD, _observation_recorded),
        EventDefinition("ProposalRecorded", RevisionClass.DELIBERATION, _audit_only),
        EventDefinition("AcceptanceRecorded", RevisionClass.WORLD, _audit_only),
    )
}


def event_definition(event_type: str) -> EventDefinition:
    try:
        return _EVENTS[event_type]
    except KeyError as exc:
        raise UnknownEventType(f"event type {event_type!r} is not registered") from exc


def reduce_event(state: ReducerState, event: WorldEvent) -> ReducerState:
    return event_definition(event.event_type).reducer(state, event)


def semantic_hash(*, world_id: str, world_revision: int, state: ReducerState) -> str:
    semantic_projection: dict[str, Any] = {
        "reducer_bundle_version": REDUCER_BUNDLE_VERSION,
        "schema_version": "world-v2.1",
        "world_id": world_id,
        "world_revision": world_revision,
        "observation_refs": state.observation_refs,
        "logical_time": state.logical_time.isoformat() if state.logical_time else None,
    }
    encoded = json.dumps(
        semantic_projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_projection(
    *,
    world_id: str,
    world_revision: int,
    deliberation_revision: int,
    ledger_sequence: int,
    state: ReducerState,
) -> LedgerProjection:
    return LedgerProjection(
        world_id=world_id,
        world_revision=world_revision,
        deliberation_revision=deliberation_revision,
        ledger_sequence=ledger_sequence,
        logical_time=state.logical_time,
        observation_refs=state.observation_refs,
        semantic_hash=semantic_hash(
            world_id=world_id,
            world_revision=world_revision,
            state=state,
        ),
    )
