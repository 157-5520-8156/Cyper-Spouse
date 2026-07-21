"""Single immutable routing contract for typed Goal authority transitions.

DORMANT — no producer: no production ledger holds a committed ``V2Goal*``
event and no runtime constructs these payloads.  Before wiring a producer,
read the Producer-First Authority rule in CONTEXT.md and record the
activation verdict in ``configs/mechanism_closure.yaml``
(``v16-situation-constituents``).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal


V2_GOAL_TRANSITION_CONTRACT = (
    ("open", "V2GoalOpened"),
    ("revise", "V2GoalRevised"),
    ("progress", "V2GoalProgressed"),
    ("pause", "V2GoalPaused"),
    ("resume", "V2GoalResumed"),
    ("block", "V2GoalBlocked"),
    ("unblock", "V2GoalUnblocked"),
    ("complete", "V2GoalCompleted"),
    ("abandon", "V2GoalAbandoned"),
    ("compensate", "V2GoalTransitionCompensated"),
)
V2_GOAL_OPERATIONS = tuple(operation for operation, _ in V2_GOAL_TRANSITION_CONTRACT)
V2_GOAL_EVENT_TYPES = tuple(event_type for _, event_type in V2_GOAL_TRANSITION_CONTRACT)
V2GoalOperation = Literal[*V2_GOAL_OPERATIONS]
V2GoalEventType = Literal[*V2_GOAL_EVENT_TYPES]
V2GoalTransitionOperation = Literal[*V2_GOAL_OPERATIONS, "expire"]

V2_GOAL_EVENT_BY_OPERATION = MappingProxyType(dict(V2_GOAL_TRANSITION_CONTRACT))
V2_GOAL_OPERATION_BY_EVENT = MappingProxyType(
    {event_type: operation for operation, event_type in V2_GOAL_TRANSITION_CONTRACT}
)

if (
    len(V2_GOAL_EVENT_BY_OPERATION) != len(V2_GOAL_TRANSITION_CONTRACT)
    or len(V2_GOAL_OPERATION_BY_EVENT) != len(V2_GOAL_TRANSITION_CONTRACT)
):
    raise RuntimeError("Goal transition contract contains duplicate routing keys")


def goal_event_for_operation(operation: str) -> str:
    try:
        return V2_GOAL_EVENT_BY_OPERATION[operation]
    except KeyError as exc:
        raise ValueError(f"unknown Goal operation {operation!r}") from exc


def goal_operation_for_event(event_type: str) -> str:
    try:
        return V2_GOAL_OPERATION_BY_EVENT[event_type]
    except KeyError as exc:
        raise ValueError(f"event type {event_type!r} is not a typed Goal transition") from exc


def require_goal_event_operation(*, event_type: str, operation: str) -> None:
    if goal_operation_for_event(event_type) != operation:
        raise ValueError("goal event type does not match operation")
