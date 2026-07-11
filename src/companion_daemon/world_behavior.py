"""Deterministic, world-only behaviour rules for online companion actions.

The module is deliberately pure: callers supply a world projection and turn
text, then persist its decision through :class:`WorldKernel`.  It never reads
wall clock time, MoodState, social tasks, or a model response.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any, Literal


CommunicationAttention = Literal["seen", "deferred", "do_not_disturb"]


@dataclass(frozen=True)
class CommunicationDecision:
    attention: CommunicationAttention
    reason: str
    defer_minutes: int | None = None


@dataclass(frozen=True)
class OutreachConstraint:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ExpressionGuidance:
    label: str
    prompt_line: str


class WorldBehaviorPolicy:
    """Hide world communication and outreach policy behind two pure methods."""

    RULE_VERSION = "world-behavior-v1"
    _URGENT_MARKERS = ("急", "紧急", "救命", "出事", "医院", "危险", "现在就", "立刻")
    _PRESSURE_MARKERS = ("必须", "立刻", "马上", "现在就", "快点", "不发", "证明", "听话")

    def communication_decision(
        self,
        state: dict[str, Any],
        *,
        text: str,
        resumed_action: bool = False,
    ) -> CommunicationDecision:
        """Choose a communication state from explicit, replayable world facts."""
        needs = _mapping(state.get("needs"))
        boundary = int(needs.get("boundary", 0))
        security = int(needs.get("security", 50))
        if boundary >= 75 and self._is_pressure(text):
            return CommunicationDecision("do_not_disturb", "boundary_high_under_pressure")
        if resumed_action or self._is_urgent(text):
            return CommunicationDecision("seen", "resumed_or_urgent_turn")
        active = next(
            (
                _mapping(item)
                for item in _mapping(state.get("agenda")).values()
                if _mapping(item).get("status") == "active"
            ),
            None,
        )
        if active:
            attention_demand = int(active.get("attention_demand", 35))
            interruptible = bool(active.get("interruptible", True))
            if attention_demand >= 90 or (attention_demand >= 70 and not interruptible):
                phase, remaining_minutes = _activity_phase(state, active)
                return CommunicationDecision(
                    "deferred",
                    f"active_world_activity_not_interruptible:{phase}",
                    defer_minutes=max(1, min(20, remaining_minutes)),
                )
        if active and int(needs.get("energy", 70)) <= 35:
            return CommunicationDecision("deferred", "active_world_activity_low_energy", defer_minutes=20)
        if security <= 20 and boundary >= 55:
            return CommunicationDecision("deferred", "security_low_boundary_recovery", defer_minutes=15)
        return CommunicationDecision("seen", "world_available")

    def outreach_constraint(self, state: dict[str, Any], *, user_id: str) -> OutreachConstraint:
        """Prevent new initiative when a world commitment already owns the turn."""
        for raw in _mapping(state.get("conversation_threads")).values():
            thread = _mapping(raw)
            if thread.get("status") == "open" and thread.get("user_id") == user_id:
                return OutreachConstraint(False, "open_conversation_thread")
        for raw in _mapping(state.get("actions")).values():
            action = _mapping(raw)
            if action.get("kind") == "outgoing_message" and action.get("status") in {"scheduled", "sending", "unknown"}:
                return OutreachConstraint(False, "outgoing_action_unresolved")
        needs = _mapping(state.get("needs"))
        if int(needs.get("boundary", 0)) >= 55:
            return OutreachConstraint(False, "boundary_high")
        if int(needs.get("security", 50)) <= 25:
            return OutreachConstraint(False, "security_low")
        return OutreachConstraint(True, "world_allows_outreach")

    def expression_guidance(self, state: dict[str, Any]) -> ExpressionGuidance:
        """Derive a short-lived expression guide without writing private prose."""
        needs = _mapping(state.get("needs"))
        modulation = _mapping(state.get("emotion_modulation"))
        mode = str(modulation.get("mode") or "calm")
        if int(needs.get("boundary", 0)) >= 55 or mode == "guarded":
            return ExpressionGuidance("guarded", "表达简短、清楚，不讨好；只说愿意说的部分。")
        if mode == "caring":
            return ExpressionGuidance("caring", "先接住对方情绪，语气温和，不把关心变成追问。")
        if mode in {"warm", "open", "softening"}:
            return ExpressionGuidance("warm", "可以自然亲近，但不夸张、不补写未发生的共同经历。")
        if int(needs.get("energy", 70)) <= 30:
            return ExpressionGuidance("low_energy", "用较短句回应，允许保留一点安静感，不解释成冷漠。")
        return ExpressionGuidance("neutral", "自然接话，避免模板化表态和没有依据的心理独白。")

    @classmethod
    def _is_urgent(cls, text: str) -> bool:
        return any(marker in text for marker in cls._URGENT_MARKERS)

    @classmethod
    def _is_pressure(cls, text: str) -> bool:
        return any(marker in text for marker in cls._PRESSURE_MARKERS)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _activity_phase(
    state: dict[str, Any], active: dict[str, Any]
) -> tuple[str, int]:
    now = datetime.fromisoformat(str(_mapping(state.get("clock")).get("logical_at")))
    starts_at = datetime.fromisoformat(str(active.get("starts_at")))
    ends_at = datetime.fromisoformat(str(active.get("ends_at")))
    duration = max(1.0, (ends_at - starts_at).total_seconds())
    progress = max(0.0, min(1.0, (now - starts_at).total_seconds() / duration))
    phase = "early" if progress < 0.33 else "middle" if progress < 0.8 else "ending"
    remaining = max(1, ceil((ends_at - now).total_seconds() / 60))
    return phase, remaining
