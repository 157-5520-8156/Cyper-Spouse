"""Deterministic personality baseline and settled-interaction affinity rules.

This module is deliberately free of persistence and wall-clock access.  The
world records its outcomes as events, so projection rebuild never has to infer
an interaction again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from companion_daemon.world_interaction_rules import HARMFUL_INTERACTION_APPRAISALS


AFFECT_KEYS = (
    "hurt",
    "anger",
    "sadness",
    "loneliness",
    "anxiety",
    "resentment",
    "warmth",
    "joy",
)
RULE_VERSION = "world-affinity-v1"


@dataclass(frozen=True)
class AffinityOutcome:
    state: dict[str, object]
    delta: dict[str, int]
    duplicate: bool = False
    rule_version: str = RULE_VERSION


def personality_affect_baseline(protagonist: object) -> dict[str, int]:
    """Derive a conservative tonic affect from reviewed character seed data.

    Negative feelings are not inferred from personality adjectives: being
    sensitive is not the same as being perpetually hurt.  Explicit reviewed
    ``affect_baseline`` values may tune the seed, while stable traits and values
    provide small deterministic warmth/joy anchors.
    """

    data = protagonist if isinstance(protagonist, Mapping) else {}
    baseline = {key: 0 for key in AFFECT_KEYS}
    text = " ".join(
        str(item)
        for field in ("stable_traits", "values")
        for item in _items(data.get(field))
    )
    if "温和" in text:
        baseline["warmth"] += 4
        baseline["joy"] += 1
    if "敏感" in text:
        baseline["warmth"] += 1
    if "真诚" in text:
        baseline["warmth"] += 1
        baseline["joy"] += 1
    explicit = data.get("affect_baseline")
    if isinstance(explicit, Mapping):
        for key in AFFECT_KEYS:
            if key in explicit:
                baseline[key] = _clamp_baseline(explicit[key])
    return {key: _clamp_baseline(value) for key, value in baseline.items()}


def initial_affinity() -> dict[str, object]:
    return {
        "vector": {},
        "evidence_counts": {},
        "recent_settlement_ids": [],
        "settled_interaction_count": 0,
        "last_settled_at": "",
    }


def settle_affinity_interaction(
    current: Mapping[str, object],
    *,
    user_id: str,
    appraisal: str,
    settlement_id: str,
    logical_at: str,
) -> AffinityOutcome:
    """Accept one terminal interaction and slowly learn repeated patterns.

    Evidence is recorded for every delivered interaction, but affinity moves
    only on each third occurrence of the same pattern.  Every dimension is
    capped to a one-point change per settlement.
    """

    state = _copy_state(current)
    recent = list(state["recent_settlement_ids"])
    if settlement_id in recent:
        return AffinityOutcome(state=state, delta={}, duplicate=True)

    recent.append(settlement_id)
    state["recent_settlement_ids"] = recent[-128:]
    state["settled_interaction_count"] = int(state["settled_interaction_count"]) + 1
    state["last_settled_at"] = logical_at
    state["user_id"] = user_id

    pattern, learned_delta = _pattern(appraisal)
    delta: dict[str, int] = {}
    if pattern:
        counts = dict(state["evidence_counts"])
        count = int(counts.get(pattern, 0)) + 1
        counts[pattern] = count
        state["evidence_counts"] = counts
        if count % 3 == 0:
            vector = dict(state["vector"])
            for key, change in learned_delta.items():
                bounded = max(-1, min(1, int(change)))
                previous = int(vector.get(key, 0))
                current_value = max(-30, min(30, previous + bounded))
                actual = current_value - previous
                if actual:
                    vector[key] = current_value
                    delta[key] = actual
            state["vector"] = vector
    return AffinityOutcome(state=state, delta=delta)


def _pattern(appraisal: str) -> tuple[str, dict[str, int]]:
    if appraisal == "warmth_received":
        return "warmth", {"warmth": 1}
    if appraisal in {"repair_specific", "repair_restitution"}:
        return "reliable_repair", {"warmth": 1, "resentment": -1}
    if appraisal in HARMFUL_INTERACTION_APPRAISALS:
        return "boundary_harm", {"warmth": -1, "resentment": 1}
    return "", {}


def _copy_state(current: Mapping[str, object]) -> dict[str, object]:
    return {
        "vector": dict(current.get("vector", {})) if isinstance(current.get("vector"), Mapping) else {},
        "evidence_counts": (
            dict(current.get("evidence_counts", {}))
            if isinstance(current.get("evidence_counts"), Mapping)
            else {}
        ),
        "recent_settlement_ids": list(_items(current.get("recent_settlement_ids"))),
        "settled_interaction_count": int(current.get("settled_interaction_count") or 0),
        "last_settled_at": str(current.get("last_settled_at") or ""),
        **({"user_id": str(current["user_id"])} if current.get("user_id") else {}),
    }


def _items(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _clamp_baseline(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(20, number))
