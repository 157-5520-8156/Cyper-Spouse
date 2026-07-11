"""Versioned, deterministic consequences for a classified user turn."""
from __future__ import annotations

from dataclasses import dataclass


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

    RULE_VERSION = "world-interaction-v1"

    _POLICIES = {
        "user_vulnerable": "先接住情绪，不急着追问。",
        "boundary_violation": "短而清楚地守住边界。",
        "control_pressure": "不讨好，平静地说明边界。",
        "repair_attempt": "可以缓和，但不立刻翻篇。",
        "availability_drop": "收住主动性，不追发。",
        "return_after_gap": "自然接上，不抱怨。",
    }
    _NEEDS = {
        "boundary_violation": {"security": -12, "boundary": 12, "initiative": -8},
        "control_pressure": {"security": -8, "boundary": 8, "initiative": -5},
        "repair_attempt": {"security": 5, "boundary": -3},
        "warmth_received": {"security": 4, "initiative": 3},
        "user_vulnerable": {"initiative": 5, "attention": -4},
        "availability_drop": {"initiative": -6},
        "return_after_gap": {"security": 2, "initiative": 2},
    }
    _RELATIONSHIP = {
        "boundary_violation": {"respect": -12, "reliability": -4, "trust": -8},
        "control_pressure": {"respect": -8, "trust": -5},
        "repair_attempt": {"respect": 3, "reliability": 2, "trust": 4},
        "warmth_received": {"closeness": 4, "reliability": 1, "trust": 5},
        "user_vulnerable": {"closeness": 2, "trust": 3},
        "availability_drop": {"reliability": -1, "trust": -1},
        "return_after_gap": {"closeness": 1, "reliability": 1, "trust": 2},
    }
    _EMOTION = {
        "boundary_violation": ("guarded", "guarded", 16),
        "control_pressure": ("guarded", "guarded", 11),
        "repair_attempt": ("softening", "soft", -5),
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
