"""Deterministic personality baseline and settled-interaction affinity rules.

This module is deliberately free of persistence and wall-clock access.  The
world records its outcomes as events, so projection rebuild never has to infer
an interaction again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import pow
from typing import Mapping

from companion_daemon.world_interaction_rules import HARMFUL_INTERACTION_APPRAISALS


AFFECT_KEYS = (
    "hurt",
    "anger",
    "sadness",
    "loneliness",
    "anxiety",
    "resentment",
    "shame",
    "guilt",
    "jealousy",
    "warmth",
    "joy",
)
RULE_VERSION = "world-affinity-v2"
EVIDENCE_HALF_LIFE_DAYS = 30
AFFINITY_VECTOR_HALF_LIFE_DAYS = 180


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
        str(item) for field in ("stable_traits", "values") for item in _items(data.get(field))
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
        "vector_weights": {},
        "vector_last_weighted_at": "",
        "evidence_counts": {},
        "evidence_weights": {},
        "pattern_last_seen_at": {},
        "pattern_exposure_days": {},
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

    delta = _decay_affinity_vector(state, logical_at)
    recent.append(settlement_id)
    state["recent_settlement_ids"] = recent[-128:]
    state["settled_interaction_count"] = int(state["settled_interaction_count"]) + 1
    state["last_settled_at"] = logical_at
    state["user_id"] = user_id

    pattern, learned_delta = _pattern(appraisal)
    if pattern:
        counts = dict(state["evidence_counts"])
        weights = dict(state["evidence_weights"])
        last_seen = dict(state["pattern_last_seen_at"])
        exposure_days = dict(state["pattern_exposure_days"])
        exposure_day = _logical_day(logical_at)
        if exposure_days.get(pattern) == exposure_day:
            return AffinityOutcome(state=state, delta=delta)
        previous_weight = float(weights.get(pattern, counts.get(pattern, 0)) or 0)
        elapsed_days = _whole_days_between(str(last_seen.get(pattern) or ""), logical_at)
        decayed_weight = previous_weight * pow(0.5, elapsed_days / EVIDENCE_HALF_LIFE_DAYS)
        weight = decayed_weight + 1.0
        count = int(weight)
        counts[pattern] = count
        weights[pattern] = weight
        last_seen[pattern] = logical_at
        exposure_days[pattern] = exposure_day
        state["evidence_counts"] = counts
        state["evidence_weights"] = weights
        state["pattern_last_seen_at"] = last_seen
        state["pattern_exposure_days"] = exposure_days
        if _evidence_level(weight) > _evidence_level(decayed_weight):
            vector = dict(state["vector"])
            vector_weights = dict(state["vector_weights"])
            for key, change in learned_delta.items():
                bounded = max(-1, min(1, int(change)))
                previous = int(vector.get(key, 0))
                weighted_previous = float(vector_weights.get(key, previous))
                weighted_current = max(-30.0, min(30.0, weighted_previous + bounded))
                current_value = int(round(weighted_current))
                actual = current_value - previous
                vector_weights[key] = weighted_current
                if actual:
                    if current_value:
                        vector[key] = current_value
                    else:
                        vector.pop(key, None)
                    delta[key] = delta.get(key, 0) + actual
            state["vector"] = vector
            state["vector_weights"] = vector_weights
    return AffinityOutcome(state=state, delta=delta)


def _pattern(appraisal: str) -> tuple[str, dict[str, int]]:
    if appraisal == "warmth_received":
        return "warmth", {"warmth": 1}
    if appraisal in {"repair_specific", "repair_restitution", "boundary_respected"}:
        return "reliable_repair", {"warmth": 1, "resentment": -1}
    if appraisal in HARMFUL_INTERACTION_APPRAISALS:
        return "boundary_harm", {"warmth": -1, "resentment": 1}
    return "", {}


def _copy_state(current: Mapping[str, object]) -> dict[str, object]:
    return {
        "vector": dict(current.get("vector", {}))
        if isinstance(current.get("vector"), Mapping)
        else {},
        "vector_weights": (
            dict(current.get("vector_weights", {}))
            if isinstance(current.get("vector_weights"), Mapping)
            else {}
        ),
        "vector_last_weighted_at": str(current.get("vector_last_weighted_at") or ""),
        "evidence_counts": (
            dict(current.get("evidence_counts", {}))
            if isinstance(current.get("evidence_counts"), Mapping)
            else {}
        ),
        "evidence_weights": (
            dict(current.get("evidence_weights", {}))
            if isinstance(current.get("evidence_weights"), Mapping)
            else {}
        ),
        "pattern_last_seen_at": (
            dict(current.get("pattern_last_seen_at", {}))
            if isinstance(current.get("pattern_last_seen_at"), Mapping)
            else {}
        ),
        "pattern_exposure_days": (
            dict(current.get("pattern_exposure_days", {}))
            if isinstance(current.get("pattern_exposure_days"), Mapping)
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


def _whole_days_between(previous: str, current: str) -> int:
    if not previous:
        return 0
    try:
        elapsed = datetime.fromisoformat(current) - datetime.fromisoformat(previous)
    except (TypeError, ValueError):
        return 0
    return max(0, int(elapsed.total_seconds()) // 86400)


def _logical_day(logical_at: str) -> str:
    try:
        return datetime.fromisoformat(logical_at).date().isoformat()
    except (TypeError, ValueError):
        return logical_at[:10]


def _evidence_level(weight: float) -> int:
    # Three distinct daily exposures should still form one pattern despite the
    # small, intentional decay between days.
    return int((max(0.0, weight) + 0.1) // 3)


def _decay_affinity_vector(state: dict[str, object], logical_at: str) -> dict[str, int]:
    vector = dict(state["vector"])
    weights = dict(state["vector_weights"])
    previous_at = str(state.get("vector_last_weighted_at") or state.get("last_settled_at") or "")
    elapsed_days = _whole_days_between(previous_at, logical_at)
    factor = pow(0.5, elapsed_days / AFFINITY_VECTOR_HALF_LIFE_DAYS)
    delta: dict[str, int] = {}
    projected: dict[str, int] = {}
    for key in sorted(set(vector) | set(weights)):
        previous = int(vector.get(key, 0))
        decayed_weight = float(weights.get(key, previous)) * factor
        weights[key] = decayed_weight
        current = int(round(decayed_weight))
        if current:
            projected[key] = current
        if current != previous:
            delta[key] = current - previous
    state["vector"] = projected
    state["vector_weights"] = weights
    state["vector_last_weighted_at"] = logical_at
    return delta
