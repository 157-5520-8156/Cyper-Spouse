"""Plan regulated affect display from sourced emotion episodes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class AffectDisplayPlan:
    primary_appraisal: str
    secondary_appraisal: str
    mixed: bool
    approach_avoidance: str
    regulation_strategy: str
    attribution_target: str
    leakage: int
    directness: int
    prompt_line: str
    rule_version: str = "affect-display-v1"

    def payload(self) -> dict[str, object]:
        return asdict(self)


def plan_affect_display(
    affect: Mapping[str, object],
    relationship: Mapping[str, object],
    needs: Mapping[str, object],
    *,
    current_appraisal: str,
) -> AffectDisplayPlan:
    raw_episodes = affect.get("active_episodes", ())
    episodes = [
        item
        for item in raw_episodes
        if isinstance(item, dict) and item.get("status") != "resolved"
    ] if isinstance(raw_episodes, (list, tuple)) else []
    episodes.sort(key=lambda item: int(item.get("intensity") or 0), reverse=True)
    primary = episodes[0] if episodes else {}
    primary_valence = int(primary.get("valence") or 0)
    secondary = next(
        (
            item
            for item in episodes[1:]
            if int(item.get("valence") or 0) != primary_valence
        ),
        {},
    )
    mixed = bool(primary and secondary)
    target = str(primary.get("target") or "general")
    stage = str(relationship.get("stage") or "stranger")
    boundary = int(needs.get("boundary") or 0)
    profile = affect.get("profile", {})
    profile = profile if isinstance(profile, dict) else {}
    if target.startswith("npc:") or target.startswith("goal:") or target == "world":
        regulation = "contain_spillover"
        approach_avoidance = "regulated_contact"
        leakage = min(
            int(profile.get("spillover_leakage_cap") or 25),
            int(primary.get("intensity") or 0) // 4,
        )
        prompt = (
            "世界里的情绪可以轻微影响节奏和耐心，但明确归因于原事件；"
            "不要把它算到用户头上，也不要无故设用户边界。"
        )
    elif primary_valence < 0 and target == "companion":
        regulation = "boundary_expression"
        approach_avoidance = "approach_avoidance" if mixed else "protective_distance"
        leakage = min(80, 35 + int(primary.get("intensity") or 0) // 3)
        prompt = (
            "保留受伤或生气，同时只针对有证据的当前行为；"
            "若仍有温暖，允许关心与防御并存，不用假装已经没事。"
        )
    elif mixed:
        regulation = "integrate_mixed_affect"
        approach_avoidance = "approach_avoidance"
        leakage = 35
        prompt = "表达主要感受时保留次要感受的余韵；不要把混合情绪压成单一标签。"
    else:
        regulation = "natural_expression"
        approach_avoidance = "approach"
        leakage = min(45, int(primary.get("intensity") or 0) // 2)
        prompt = "按当前有来源的感受自然回应，不额外补写情绪原因。"
    directness = 45 + min(30, boundary // 4)
    if stage in {"close_friend", "ambiguous", "lover"}:
        directness += 10
    return AffectDisplayPlan(
        primary_appraisal=str(primary.get("appraisal") or current_appraisal),
        secondary_appraisal=str(secondary.get("appraisal") or ""),
        mixed=mixed,
        approach_avoidance=approach_avoidance,
        regulation_strategy=regulation,
        attribution_target=target,
        leakage=max(0, min(100, leakage)),
        directness=max(0, min(100, directness)),
        prompt_line=prompt,
    )
