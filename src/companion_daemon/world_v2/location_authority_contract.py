"""Single routing contract for the `.16.0` LocationAuthority event family.

DORMANT — no producer: no production ledger holds a committed ``V2Location*``
event and no runtime constructs these payloads (current location reaches
deliberation through active Plan heads instead).  Before wiring a producer,
read the Producer-First Authority rule in CONTEXT.md and record the
activation verdict in ``configs/mechanism_closure.yaml``
(``v16-situation-constituents``).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, Mapping, cast


V2LocationOperation = Literal["establish", "change", "compensate"]
V2LocationEventType = Literal[
    "V2LocationChanged",
    "V2LocationChangeCompensated",
]

_EVENT_BY_OPERATION: Mapping[V2LocationOperation, V2LocationEventType] = MappingProxyType(
    {
        "establish": "V2LocationChanged",
        "change": "V2LocationChanged",
        "compensate": "V2LocationChangeCompensated",
    }
)


def location_event_for_operation(operation: str) -> V2LocationEventType:
    try:
        return _EVENT_BY_OPERATION[cast(V2LocationOperation, operation)]
    except KeyError as exc:
        raise ValueError(f"unsupported Location operation {operation!r}") from exc


def require_location_event_operation(*, event_type: str, operation: str) -> None:
    if location_event_for_operation(operation) != event_type:
        raise ValueError("location event type does not match operation")


V2_LOCATION_MUTATION_EVENT_TYPES = tuple(sorted(set(_EVENT_BY_OPERATION.values())))
