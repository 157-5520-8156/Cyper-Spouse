"""Closed event routing for the pure `.16.0` AttentionAuthority seam."""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, Mapping, cast


V2AttentionOperation = Literal["establish", "change", "compensate"]
V2AttentionEventType = Literal[
    "V2AttentionChanged",
    "V2AttentionTransitionCompensated",
]

_EVENT_BY_OPERATION: Mapping[V2AttentionOperation, V2AttentionEventType] = MappingProxyType(
    {
        "establish": "V2AttentionChanged",
        "change": "V2AttentionChanged",
        "compensate": "V2AttentionTransitionCompensated",
    }
)


def attention_event_for_operation(operation: str) -> V2AttentionEventType:
    try:
        return _EVENT_BY_OPERATION[cast(V2AttentionOperation, operation)]
    except KeyError as exc:
        raise ValueError(f"unsupported Attention operation {operation!r}") from exc


def require_attention_event_operation(*, event_type: str, operation: str) -> None:
    if attention_event_for_operation(operation) != event_type:
        raise ValueError("Attention event type does not match operation")


V2_ATTENTION_MUTATION_EVENT_TYPES = tuple(sorted(set(_EVENT_BY_OPERATION.values())))
