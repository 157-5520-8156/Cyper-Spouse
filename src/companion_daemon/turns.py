"""The small, inspectable contract for one companion conversation turn."""
from __future__ import annotations

from dataclasses import dataclass

from companion_daemon.context_orchestrator import ContextPackage
from companion_daemon.emotion_state import InteractionEvent


@dataclass(frozen=True)
class TurnPlan:
    """What the daemon, not the language model, has authorized this turn to do."""

    appraisal: str
    expression_policy: str
    allowed_facts: list[str]
    short_lived_constraint: str | None
    observable_reason: str


_OBSERVABLE_REASONS = {
    "user_vulnerable": "用户在示弱，优先接住情绪。",
    "boundary_violation": "用户越过边界，保持短而清楚。",
    "control_pressure": "用户施加控制，保持平静但不讨好。",
    "repair_attempt": "用户在修复关系，允许缓和但不立刻翻篇。",
    "availability_drop": "用户正在忙，收住主动性。",
    "return_after_gap": "用户回来了，自然接上当前话题。",
}


def build_turn_plan(
    *,
    event: InteractionEvent,
    context_package: ContextPackage,
    allowed_facts: list[str],
    subtext: str | None,
) -> TurnPlan:
    """Turn rich state into the one behavioral contract a reply may use."""
    return TurnPlan(
        appraisal=event.kind,
        expression_policy=context_package.reply_policy,
        allowed_facts=list(allowed_facts),
        short_lived_constraint=subtext,
        observable_reason=_OBSERVABLE_REASONS.get(
            event.kind, "普通推进，回应当前这条消息。"
        ),
    )
