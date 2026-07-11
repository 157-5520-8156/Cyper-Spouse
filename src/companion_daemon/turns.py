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
    allowed_facts: tuple[str, ...]
    short_lived_constraint: str | None
    observable_reason: str
    context_block: str

    def prompt_block(self) -> str:
        facts = "；".join(self.allowed_facts) or "无额外授权事实"
        constraint = self.short_lived_constraint or "无"
        return (
            f"{self.context_block}\n\n"
            "回合授权（daemon 决定，必须遵守）：\n"
            f"- 本轮关系判断: {self.appraisal}\n"
            f"- 本轮表达策略: {self.expression_policy}\n"
            f"- 本轮可用事实: {facts}\n"
            f"- 本轮短期倾向: {constraint}\n"
            "- 不得把短期倾向或未授权记忆写成事实；不得另行创建承诺、事实或经历。"
        )


@dataclass(frozen=True)
class TurnCommit:
    """The persisted outcome of an authorized turn after delivery settles."""

    trace_id: int | None
    delivery_id: int | None
    status: str
    reason: str | None = None


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
        allowed_facts=tuple(allowed_facts),
        short_lived_constraint=subtext,
        observable_reason=_OBSERVABLE_REASONS.get(
            event.kind, "普通推进，回应当前这条消息。"
        ),
        context_block=context_package.prompt_block(),
    )
