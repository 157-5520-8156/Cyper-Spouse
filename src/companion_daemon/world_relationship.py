"""Deterministic relationship-stage projection for the world ledger."""

from __future__ import annotations

from typing import Literal, cast


RelationshipStage = Literal[
    "stranger", "acquaintance", "friend", "close_friend", "ambiguous", "lover"
]
RULE_VERSION = "world-relationship-v1"

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


def evaluate_relationship_stage(
    relationship: dict[str, object],
    *,
    boundary: int = 0,
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
        required_count, required_trust, required_closeness = _THRESHOLDS[candidate]
        if (
            interaction_count >= required_count
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
) -> dict[str, object]:
    """Build one self-describing stage event payload for audit/replay."""
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
        "rule_version": RULE_VERSION,
    }
