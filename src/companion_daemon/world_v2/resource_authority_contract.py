"""Immutable event routing for the `.16.0` ResourceAuthority seam."""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal


V2_RESOURCE_TRANSITION_CONTRACT = (
    ("initialize", "V2ResourceStateInitialized"),
    ("adjust", "V2ResourceStateAdjusted"),
    ("compensate", "V2ResourceTransitionCompensated"),
)
V2_RESOURCE_OPERATIONS = tuple(item[0] for item in V2_RESOURCE_TRANSITION_CONTRACT)
V2_RESOURCE_EVENT_TYPES = tuple(item[1] for item in V2_RESOURCE_TRANSITION_CONTRACT)
V2ResourceOperation = Literal[*V2_RESOURCE_OPERATIONS]
V2ResourceEventType = Literal[*V2_RESOURCE_EVENT_TYPES]

V2_RESOURCE_EVENT_BY_OPERATION = MappingProxyType(dict(V2_RESOURCE_TRANSITION_CONTRACT))
V2_RESOURCE_OPERATION_BY_EVENT = MappingProxyType(
    {event_type: operation for operation, event_type in V2_RESOURCE_TRANSITION_CONTRACT}
)

# Clock recovery is deliberately not part of the typed proposal authority.  Its
# future wire event belongs to a separate mechanical registry whose installed
# capability set is empty in `.16.0`.
V2_RESOURCE_MECHANICAL_EVENT_BY_OPERATION = MappingProxyType(
    {"clock_adjust": "V2ResourceClockAdjusted"}
)

if (
    len(V2_RESOURCE_EVENT_BY_OPERATION) != len(V2_RESOURCE_TRANSITION_CONTRACT)
    or len(V2_RESOURCE_OPERATION_BY_EVENT) != len(V2_RESOURCE_TRANSITION_CONTRACT)
):
    raise RuntimeError("Resource transition contract contains duplicate routing keys")


def resource_event_for_operation(operation: str) -> str:
    try:
        return V2_RESOURCE_EVENT_BY_OPERATION[operation]
    except KeyError as exc:
        raise ValueError(f"unknown Resource operation {operation!r}") from exc


def require_resource_event_operation(*, event_type: str, operation: str) -> None:
    try:
        expected = V2_RESOURCE_OPERATION_BY_EVENT[event_type]
    except KeyError as exc:
        raise ValueError(f"event type {event_type!r} is not a typed Resource transition") from exc
    if expected != operation:
        raise ValueError("resource event type does not match operation")
