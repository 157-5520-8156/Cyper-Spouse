"""Versioned, deterministic consequences for a classified user turn."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class InteractionConsequence:
    policy: str
    need_deltas: dict[str, int]
    relationship_deltas: dict[str, int]
    emotion_mode: str
    emotion_expression: str
    emotion_charge_delta: int


class WorldInteractionRules:
    """One deep module replacing scattered legacy relationship/mood updates."""

    RULE_VERSION = "world-interaction-v2"

    _POLICIES = {
        "user_vulnerable": "先接住情绪，不急着追问。",
        "boundary_violation": "短而清楚地守住边界。",
        "control_pressure": "不讨好，平静地说明边界。",
        "repair_attempt": "可以缓和，但不立刻翻篇。",
        "repair_perfunctory": "听见了道歉，但先保留判断。",
        "repair_specific": "具体的道歉值得回应，仍需要时间观察。",
        "repair_restitution": "看见了补偿行动，允许逐步恢复信任。",
        "repeated_violation": "修复期再次越界，明确收紧边界。",
        "availability_drop": "收住主动性，不追发。",
        "return_after_gap": "自然接上，不抱怨。",
    }
    _NEEDS = {
        "boundary_violation": {"security": -12, "boundary": 12, "initiative": -8},
        "control_pressure": {"security": -8, "boundary": 8, "initiative": -5},
        "repair_attempt": {"security": 5, "boundary": -3},
        "repair_perfunctory": {"security": 1},
        "repair_specific": {"security": 4, "boundary": -2},
        "repair_restitution": {"security": 7, "boundary": -4},
        "repeated_violation": {"security": -18, "boundary": 18, "initiative": -12},
        "warmth_received": {"security": 4, "initiative": 3},
        "user_vulnerable": {"initiative": 5, "attention": -4},
        "availability_drop": {"initiative": -6},
        "return_after_gap": {"security": 2, "initiative": 2},
    }
    _RELATIONSHIP = {
        "boundary_violation": {"respect": -12, "reliability": -4, "trust": -8},
        "control_pressure": {"respect": -8, "trust": -5},
        "repair_attempt": {"respect": 3, "reliability": 2, "trust": 4},
        "repair_perfunctory": {"respect": 1, "reliability": 0, "trust": 0},
        "repair_specific": {"respect": 3, "reliability": 1, "trust": 3},
        "repair_restitution": {"respect": 5, "reliability": 4, "trust": 6},
        "repeated_violation": {"respect": -18, "reliability": -9, "trust": -14},
        "warmth_received": {"closeness": 4, "reliability": 1, "trust": 5},
        "user_vulnerable": {"closeness": 2, "trust": 3},
        "availability_drop": {"reliability": -1, "trust": -1},
        "return_after_gap": {"closeness": 1, "reliability": 1, "trust": 2},
    }
    _EMOTION = {
        "boundary_violation": ("guarded", "guarded", 16),
        "control_pressure": ("guarded", "guarded", 11),
        "repair_attempt": ("softening", "soft", -5),
        "repair_perfunctory": ("guarded", "neutral", -1),
        "repair_specific": ("softening", "soft", -4),
        "repair_restitution": ("softening", "soft", -7),
        "repeated_violation": ("guarded", "guarded", 22),
        "warmth_received": ("warm", "smile", 5),
        "user_vulnerable": ("caring", "worry", 7),
        "availability_drop": ("patient", "neutral", -2),
        "return_after_gap": ("open", "soft", 3),
    }

    def consequence(self, appraisal: str) -> InteractionConsequence:
        mode, expression, charge_delta = self._EMOTION.get(appraisal, ("calm", "neutral", -1))
        return InteractionConsequence(
            policy=self._POLICIES.get(appraisal, "自然回应当前消息。"),
            need_deltas=dict(self._NEEDS.get(appraisal, {})),
            relationship_deltas=dict(self._RELATIONSHIP.get(appraisal, {})),
            emotion_mode=mode,
            emotion_expression=expression,
            emotion_charge_delta=charge_delta,
        )


_APOLOGY_RE = re.compile(r"对不起|抱歉|道歉|是我不对|我的错|我错了")
_SPECIFIC_RE = re.compile(
    r"刚才|之前|那样说|说重了|不该|不应该|命令|逼你|催你|越界|伤害|忽略|打断"
)
_RESTITUTION_RE = re.compile(
    r"已经(?:取消|删除|改|关|停止|补上|处理)|我会(?:先问|改|停止|补偿|尊重)|以后(?:会|不再)|补偿|弥补"
)


def classify_repair_appraisal(text: str) -> str | None:
    """Classify observable repair quality without inferring hidden intent.

    Restitution requires both owning a concrete breach and an observable or
    prospective corrective action.  A bare apology remains perfunctory; this
    prevents one polite phrase from restoring the relationship ledger.
    """
    normalized = "".join(str(text).strip().split())
    if not normalized or not _APOLOGY_RE.search(normalized):
        # Concrete ownership can be a repair even without the word “sorry”.
        owns_breach = re.search(r"(?:我(?:不该|不应该|错在)|不对)", normalized)
        if not (owns_breach and _SPECIFIC_RE.search(normalized)):
            return None
    specific = bool(_SPECIFIC_RE.search(normalized))
    if specific and _RESTITUTION_RE.search(normalized):
        return "repair_restitution"
    if specific:
        return "repair_specific"
    return "repair_perfunctory"
