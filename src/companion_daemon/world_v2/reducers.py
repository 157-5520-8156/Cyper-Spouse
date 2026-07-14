from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from functools import partial
import hashlib
import json
from typing import Any

from .action_lifecycle import transition_action
from .errors import UnknownEventType
from .schemas import Action, ActionState, LedgerProjection, WorldEvent


REDUCER_BUNDLE_VERSION = "world-v2-reducers.1"


class RevisionClass(StrEnum):
    WORLD = "world"
    DELIBERATION = "deliberation"


@dataclass(frozen=True, slots=True)
class ReducerState:
    observation_refs: tuple[str, ...] = ()
    logical_time: datetime | None = None
    actions: tuple[Action, ...] = ()


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
        logical_time=max(state.logical_time, event.logical_time)
        if state.logical_time is not None
        else event.logical_time,
    )


def _clock_advanced(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time_to = event.payload().get("logical_time_to")
    logical_time_from = event.payload().get("logical_time_from")
    if not isinstance(logical_time_from, str):
        raise ValueError("ClockAdvanced requires logical_time_from")
    if not isinstance(logical_time_to, str):
        raise ValueError("ClockAdvanced requires logical_time_to")
    origin = datetime.fromisoformat(logical_time_from)
    target = datetime.fromisoformat(logical_time_to)
    if target <= origin:
        raise ValueError("ClockAdvanced logical_time_to must follow logical_time_from")
    if state.logical_time is not None and origin != state.logical_time:
        raise ValueError("ClockAdvanced logical_time_from does not match current logical time")
    if state.logical_time is not None and target <= state.logical_time:
        raise ValueError("logical time cannot move backwards or remain unchanged")
    return replace(state, logical_time=target)


def _action_authorized(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_payload = event.payload().get("action")
    action = Action.model_validate_json(
        json.dumps(action_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if action.world_id != event.world_id:
        raise ValueError("ActionAuthorized action belongs to another world")
    if action.state != "authorized":
        raise ValueError("ActionAuthorized requires authorized state")
    if any(existing.action_id == action.action_id for existing in state.actions):
        raise ValueError(f"action {action.action_id!r} is already registered")
    if any(
        existing.idempotency_key == action.idempotency_key for existing in state.actions
    ):
        raise ValueError(f"action idempotency_key {action.idempotency_key!r} already exists")
    return replace(state, actions=(*state.actions, action))


def _action_transitioned(
    state: ReducerState, event: WorldEvent, *, target: ActionState
) -> ReducerState:
    action_id = event.payload().get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError(f"{event.event_type} requires action_id")
    for index, existing in enumerate(state.actions):
        if existing.action_id == action_id:
            transitioned = transition_action(existing, target)
            return replace(
                state,
                actions=(*state.actions[:index], transitioned, *state.actions[index + 1 :]),
            )
    raise ValueError(f"action {action_id!r} does not exist")


_EVENTS = {
    definition.event_type: definition
    for definition in (
        EventDefinition("WorldStarted", RevisionClass.WORLD, _world_started),
        EventDefinition("ObservationRecorded", RevisionClass.WORLD, _observation_recorded),
        EventDefinition("ClockAdvanced", RevisionClass.WORLD, _clock_advanced),
        EventDefinition("ActionAuthorized", RevisionClass.WORLD, _action_authorized),
        EventDefinition(
            "ActionScheduled",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="scheduled"),
        ),
        EventDefinition(
            "ActionClaimed",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="claimed"),
        ),
        EventDefinition(
            "ActionDispatchStarted",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="dispatch_started"),
        ),
        EventDefinition(
            "ActionProviderAccepted",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="provider_accepted"),
        ),
        EventDefinition(
            "ActionDelivered",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="delivered"),
        ),
        EventDefinition(
            "ActionFailed",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="failed"),
        ),
        EventDefinition(
            "ActionUnknown",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="unknown"),
        ),
        EventDefinition(
            "ActionCancelled",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="cancelled"),
        ),
        EventDefinition(
            "ActionExpired",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="expired"),
        ),
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
        "actions": tuple(action.model_dump(mode="json") for action in state.actions),
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
        actions=state.actions,
        semantic_hash=semantic_hash(
            world_id=world_id,
            world_revision=world_revision,
            state=state,
        ),
    )
