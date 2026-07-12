"""Deterministic cognitive appraisal for already committed life outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class LifeOutcomeAppraisal:
    agency: str
    certainty: int
    goal_congruence: int
    controllability: int
    norm_compatibility: int
    power_delta: int
    relationship_value: int
    salience: int
    social_exposure: int
    rule_version: str = "life-appraisal-v2"

    def payload(self) -> dict[str, object]:
        return asdict(self)


def appraise_committed_life_outcome(
    outcome: Mapping[str, object],
    *,
    needs: Mapping[str, object],
    npc_relationship: Mapping[str, object],
    goal_importance: int,
) -> LifeOutcomeAppraisal:
    """Interpret one settled fact through current, likewise sourced context."""
    kind = str(outcome.get("appraisal") or "")
    intensity = _bounded(outcome.get("intensity"), 50)
    energy = _bounded(needs.get("energy"), 50)
    security = _bounded(needs.get("security"), 50)
    relationship_value = _bounded(npc_relationship.get("closeness"), 0)
    importance = _bounded(goal_importance, 0)
    positive = kind in {
        "social_warmth", "family_connection", "goal_progress", "goal_completed",
        "creative_satisfaction", "restorative_solitude",
    }
    negative = kind in {"npc_conflict", "goal_strain"}
    if positive:
        goal_congruence = min(100, 25 + intensity // 2 + importance // 4)
    elif negative:
        goal_congruence = max(-100, -(20 + intensity // 3 + importance // 2))
    else:
        goal_congruence = 0
    controllability = max(5, min(95, 20 + energy // 2 + security // 4))
    norm_compatibility = -65 if kind == "npc_conflict" else 20 if positive else -20 if negative else 0
    salience = min(
        100,
        intensity
        + (100 - energy) // 5
        + relationship_value // 5
        + importance // 5,
    )
    return LifeOutcomeAppraisal(
        agency="npc" if outcome.get("npc_id") else "situation",
        certainty=100,
        goal_congruence=goal_congruence,
        controllability=controllability,
        norm_compatibility=norm_compatibility,
        power_delta=-20 if kind == "npc_conflict" and security < 40 else 0,
        relationship_value=relationship_value,
        salience=salience,
        social_exposure=35 if outcome.get("npc_id") else 0,
    )


def _bounded(value: object, default: int) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    return max(0, min(100, int(value)))
