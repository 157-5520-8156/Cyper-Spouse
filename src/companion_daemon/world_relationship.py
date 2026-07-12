"""Deterministic relationship-stage projection for the world ledger."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Literal, cast


RelationshipStage = Literal[
    "stranger", "acquaintance", "friend", "close_friend", "ambiguous", "lover"
]
RULE_VERSION = "world-relationship-v2"

STAGES: tuple[RelationshipStage, ...] = (
    "stranger", "acquaintance", "friend", "close_friend", "ambiguous", "lover"
)

_THRESHOLDS: dict[RelationshipStage, tuple[int, int, int]] = {
    "stranger": (0, 0, 0),
    "acquaintance": (4, 18, 0),
    "friend": (12, 25, 18),
    "close_friend": (35, 45, 35),
    "ambiguous": (70, 55, 55),
    "lover": (120, 70, 75),
}

_SIGNIFICANCE_BY_APPRAISAL = {
    "warmth_received": 1,
    "user_vulnerable": 2,
    "return_after_gap": 1,
    "repair_specific": 2,
    "repair_restitution": 3,
}


def relationship_slow_warmth(protagonist: dict[str, object]) -> int:
    """Read the explicit relationship pacing parameter from Character Core.

    Fifty preserves the historical baseline.  Faster or slower characters can
    vary the threshold without changing global policy, and malformed seeds are
    bounded rather than becoming an unreviewed relationship shortcut.
    """
    raw = protagonist.get("relationship_pacing", 50)
    if isinstance(raw, dict):
        raw = raw.get("slow_warmth", 50)
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return 50


def relationship_event_significance(appraisal: str) -> int:
    """Return bounded relationship evidence for one observed appraisal.

    Only constructive, sourced appraisals receive progression credit.  A
    boundary violation is emotionally significant, but must never help a
    relationship promote merely because it was dramatic.
    """
    return _SIGNIFICANCE_BY_APPRAISAL.get(str(appraisal), 0)


def _effective_thresholds(
    stage: RelationshipStage,
    *,
    slow_warmth: int,
) -> tuple[int, int, int]:
    count, trust, closeness = _THRESHOLDS[stage]
    pacing = max(0, min(100, int(slow_warmth)))
    count_factor = max(0.5, 1.0 + ((pacing - 50) / 100))
    metric_factor = max(0.75, 1.0 + ((pacing - 50) / 200))
    return (
        ceil(count * count_factor),
        ceil(trust * metric_factor),
        ceil(closeness * metric_factor),
    )


@dataclass(frozen=True)
class ControlledTransgression:
    """Auditable allowance for a character choice that carries social cost."""

    allowed: bool
    relationship_cost: int
    affect_cost: int
    reason: str
    cooldown_seconds: int


def evaluate_controlled_transgression(
    relationship: dict[str, object],
    *,
    unresolved_affect: bool,
    seconds_since_last: int | None,
    safety_ok: bool = True,
    consent_ok: bool = True,
) -> ControlledTransgression:
    """Price a small relational risk without turning closeness into permission.

    Ordinary stubbornness or boundary-testing remains available at every stage;
    early relationships pay more. Safety and consent remain hard invariants, and
    a cooldown prevents “personality” from becoming repetitive harassment.
    """
    if not safety_ok:
        return ControlledTransgression(False, 0, 0, "safety_invariant", 0)
    if not consent_ok:
        return ControlledTransgression(False, 0, 0, "consent_required", 0)
    stage = str(relationship.get("stage") or "stranger")
    stage_cost = {
        "stranger": 9,
        "acquaintance": 7,
        "friend": 5,
        "close_friend": 3,
        "ambiguous": 3,
        "lover": 2,
    }.get(stage, 9)
    respect = int(relationship.get("respect") or 0)
    cooldown = 6 * 3600 if respect >= 40 else 12 * 3600
    if seconds_since_last is not None and max(0, int(seconds_since_last)) < cooldown:
        return ControlledTransgression(False, stage_cost, 3, "transgression_cooldown", cooldown)
    affect_cost = 4 if unresolved_affect else 1
    return ControlledTransgression(
        True,
        stage_cost + (3 if unresolved_affect else 0),
        affect_cost,
        "relational_risk_accepted",
        cooldown,
    )


def evaluate_relationship_stage(
    relationship: dict[str, object],
    *,
    boundary: int = 0,
    slow_warmth: int = 50,
    event_significance: int = 0,
) -> tuple[RelationshipStage, str]:
    """Return the bounded next stage and an auditable transition reason.

    Promotion requires all thresholds. Regression is deliberately slower than
    promotion: a boundary or respect failure can lower at most one stage in a
    single appraisal. Logical time alone never promotes a relationship.
    """
    current_raw = str(relationship.get("stage") or "stranger")
    current: RelationshipStage = (
        cast(RelationshipStage, current_raw) if current_raw in STAGES else "stranger"
    )
    current_index = STAGES.index(current)
    interaction_count = int(relationship.get("interaction_count") or 0)
    trust = int(relationship.get("trust") or 0)
    closeness = int(relationship.get("closeness") or 0)
    respect = int(relationship.get("respect") or 0)

    if current_index > 0 and (int(boundary) >= 55 or respect <= -20 or trust <= -10):
        return STAGES[current_index - 1], "relationship_boundary_regression"

    if current_index < len(STAGES) - 1:
        candidate = STAGES[current_index + 1]
        required_count, required_trust, required_closeness = _effective_thresholds(
            candidate,
            slow_warmth=slow_warmth,
        )
        effective_count = interaction_count + max(0, min(3, int(event_significance)))
        if (
            effective_count >= required_count
            and trust >= required_trust
            and closeness >= required_closeness
        ):
            return candidate, "relationship_progression"
    return current, "relationship_steady"


def relationship_stage_instruction(stage: str) -> str:
    """Translate the world stage into a short-lived expression constraint."""
    return {
        "stranger": "刚认识：礼貌、自然、保留边界，不使用恋人语气。",
        "acquaintance": "开始熟悉：可以轻松一点，但仍不过度暧昧。",
        "friend": "普通朋友：可以自然关心，有一点稳定默契。",
        "close_friend": "亲近朋友：可以更坦诚，允许轻微小脾气。",
        "ambiguous": "暧昧阶段：可以克制地表达在意，不宣布恋爱或占有。",
        "lover": "恋人阶段：可以亲密和撒娇，但仍受事实、边界和行动规则约束。",
    }.get(stage, "关系阶段未知：保持慢热和边界，不越级。")


def stage_event_payload(
    *,
    entity_id: str,
    stage: RelationshipStage,
    from_stage: str | None,
    relationship: dict[str, object],
    boundary: int,
    reason: str,
    slow_warmth: int = 50,
    event_significance: int = 0,
) -> dict[str, object]:
    """Build one self-describing stage event payload for audit/replay."""
    evaluation_stage = (
        cast(RelationshipStage, from_stage) if from_stage in STAGES else stage
    )
    current_index = STAGES.index(evaluation_stage)
    threshold_stage = (
        STAGES[current_index + 1]
        if current_index < len(STAGES) - 1
        else evaluation_stage
    )
    required_count, required_trust, required_closeness = _effective_thresholds(
        threshold_stage,
        slow_warmth=slow_warmth,
    )
    return {
        "entity_id": entity_id,
        "stage": stage,
        "from_stage": from_stage,
        "interaction_count": int(relationship.get("interaction_count") or 0),
        "metrics": {
            "trust": int(relationship.get("trust") or 0),
            "closeness": int(relationship.get("closeness") or 0),
            "respect": int(relationship.get("respect") or 0),
            "reliability": int(relationship.get("reliability") or 0),
            "boundary": int(boundary),
        },
        "reason": reason,
        "slow_warmth": max(0, min(100, int(slow_warmth))),
        "event_significance": max(0, min(3, int(event_significance))),
        "effective_thresholds": {
            "interaction_count": required_count,
            "trust": required_trust,
            "closeness": required_closeness,
        },
        "rule_version": RULE_VERSION,
    }
