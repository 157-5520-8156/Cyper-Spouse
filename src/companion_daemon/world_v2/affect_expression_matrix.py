"""Model-facing bridge from accepted affect state to expressive choice.

This module does not choose prose or a social action.  It promotes the small
part of an authoritative Context Capsule that the reply model must actively
weigh, so a durable emotion is not technically present yet practically buried
under the rest of the capsule.
"""

from __future__ import annotations

import json
from typing import Any


_NEGATIVE_DIMENSIONS = frozenset({"hurt", "anger", "disgust", "resentment"})
_SIGNIFICANT_BP = 3_500
_HIGH_BP = 6_000


def affect_expression_matrix(compact_context_json: str) -> dict[str, object] | None:
    """Derive advisory expressive latitude from source-bound active affect.

    The thresholds classify attention priority only.  They never select anger,
    comfort, confrontation, silence, or wording; those remain model decisions.
    """

    try:
        context = json.loads(compact_context_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(context, dict) or not isinstance(context.get("slices"), dict):
        return None
    slices = context["slices"]
    components: list[dict[str, object]] = []
    affect_slice = slices.get("affect_episodes")
    if isinstance(affect_slice, dict) and affect_slice.get("availability") == "available":
        for item in _items(affect_slice):
            source_ref = item.get("source_ref")
            value = item.get("value")
            if not isinstance(source_ref, str) or not isinstance(value, dict):
                continue
            if value.get("status", "active") != "active":
                continue
            raw_components = value.get("components")
            if not isinstance(raw_components, list):
                continue
            for component in raw_components:
                if not isinstance(component, dict):
                    continue
                dimension = component.get("dimension")
                intensity = component.get("intensity_bp")
                if (
                    isinstance(dimension, str)
                    and dimension in _NEGATIVE_DIMENSIONS
                    and isinstance(intensity, int)
                    and not isinstance(intensity, bool)
                    and intensity >= _SIGNIFICANT_BP
                ):
                    components.append(
                        {
                            "dimension": dimension,
                            "intensity_bp": min(10_000, intensity),
                            "source_ref": source_ref,
                        }
                    )
    if not components:
        return None

    peak = max(int(item["intensity_bp"]) for item in components)
    return {
        "contract": "affect-expression-choice.1",
        "salience": "high" if peak >= _HIGH_BP else "moderate",
        "relationship_latitude": _relationship_latitude(slices.get("relationship_slice")),
        "source_bound_components": components,
        "decision_axes": {
            "awareness": "Treat these accepted feelings as present on this turn.",
            "care": (
                "Choose whether to protect the connection, hold a boundary, withhold care, "
                "or defer engagement from relationship, personality, and the live exchange."
            ),
            "display": (
                "Choose direct disclosure, partial disclosure, tonal distance, boundary, "
                "delay, or silence; emotion and display need not match one-to-one."
            ),
        },
        "visible_expression_floor": (
            "If choosing a visible reply, give at least one affect-bearing conversational function "
            "such as discomfort, boundary, distance, refusal, or conflicted care; do not make the "
            "whole reply a neutral information probe—not merely a curiosity question about the "
            "counterpart's motive."
        ),
        "choice_contract": (
            "This is guidance, not a script: it does not force comfort, anger, confrontation, "
            "forgiveness, or a fixed response. Controlled variation is expected, while pretending "
            "the source-bound significant affect is absent is not."
        ),
    }


def _items(slice_value: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = slice_value.get("items")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, dict))


def _relationship_latitude(slice_value: object) -> str:
    if not isinstance(slice_value, dict) or slice_value.get("availability") != "available":
        return "unknown"
    for item in _items(slice_value):
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        stage = value.get("stage")
        variables = value.get("variables")
        trust = variables.get("trust_bp", 0) if isinstance(variables, dict) else 0
        closeness = variables.get("closeness_bp", 0) if isinstance(variables, dict) else 0
        if stage in {"stranger", "acquaintance"} or min(_bp(trust), _bp(closeness)) < 2_500:
            return "reserved"
        if stage in {"friend", "ambiguous"}:
            return "bounded"
        if stage in {"close_friend", "lover"}:
            return "open"
    return "unknown"


def _bp(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["affect_expression_matrix"]
