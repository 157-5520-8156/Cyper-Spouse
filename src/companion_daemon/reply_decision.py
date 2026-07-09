import random
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

from companion_daemon.models import MoodState
from companion_daemon.time import utc_now


class ReplyAction(StrEnum):
    REPLY_NOW = "reply_now"
    DEFER = "defer"
    SKIP = "skip"


@dataclass(frozen=True)
class ReplyDecision:
    action: ReplyAction
    defer_minutes: float | None = None
    reason: str = ""
    mark_unread: bool = False


_ACK_PATTERNS = {
    "嗯", "嗯嗯", "好的", "好", "哦", "噢", "哈哈", "哈哈哈", "行", "对",
    "是的", "收到", "ok", "OK", "嗯哼", "嗯嗯嗯", "好滴", "好嘞",
}
_QUESTION_HINTS = (
    "怎么", "为什么", "什么时候", "哪", "哪个", "是不是", "能不能",
    "可以吗", "吗", "？", "?", "多少", "谁", "怎么办", "什么样",
)
_EMOTIONAL_HINTS = (
    "好累", "好开心", "好难过", "难过", "开心", "生气", "想你",
    "喜欢你", "好烦", "难受", "委屈", "害怕", "担心", "好饿",
    "好困", "不舒服", "好冷", "好热", "心疼", "舍不得",
)
_URGENT_HINTS = (
    "在吗", "在不在", "人呢", "你人呢", "回我", "怎么不回",
    "在么", "在不", "??", "？？", "你还在吗", "理我",
)
LOW_ENERGY_ACK_DEFER_RANGE = (6, 18)
OPEN_THREAD_ACK_DEFER_RANGE = (3, 14)


def classify_message(text: str) -> str:
    """Classify a user message for reply decision purposes."""
    stripped = text.strip()
    if not stripped:
        return "empty"

    for hint in _URGENT_HINTS:
        if hint in stripped:
            return "urgent"

    if stripped in _ACK_PATTERNS:
        return "ack"

    if len(stripped) <= 4 and stripped.endswith(("。", "！", ".", "!")):
        return "ack"

    for hint in _QUESTION_HINTS:
        if hint in stripped:
            return "question"

    for hint in _EMOTIONAL_HINTS:
        if hint in stripped:
            return "emotional"

    if len(stripped) >= 50:
        return "story"

    return "statement"


def current_phase() -> str:
    """Get the current Chengdu time phase without needing a full MoodState."""
    local = utc_now().astimezone(ZoneInfo("Asia/Shanghai"))
    hour = local.hour
    if 5 <= hour <= 8:
        return "early_morning"
    if 9 <= hour <= 11:
        return "morning_focus"
    if 12 <= hour <= 13:
        return "lunch_break"
    if 14 <= hour <= 17:
        return "afternoon_classes"
    if 18 <= hour <= 21:
        return "evening_unwind"
    if 22 <= hour <= 23:
        return "late_evening"
    return "deep_night"


def decide_reply(
    text: str,
    state: MoodState | None = None,
    *,
    phase: str | None = None,
    has_pending_reply: bool = False,
    has_unread: bool = False,
    rng: random.Random | None = None,
) -> ReplyDecision:
    """Decide whether to reply now, defer, or skip.

    Parameters
    ----------
    text : str
        The merged incoming message text.
    state : MoodState | None
        Current mood state. If None, phase must be provided or will be computed.
    phase : str | None
        Override the time-of-day phase. If None, computed from current time or state.
    has_pending_reply : bool
        Whether there is already a deferred reply waiting.
    has_unread : bool
        Whether she already has unread messages (previous skip/defer).
    rng : random.Random | None
        Random source for reproducibility.
    """
    rng = rng or random.Random()
    msg_type = classify_message(text)
    if state:
        has_unread = has_unread or state.has_unread

    if msg_type == "urgent":
        return ReplyDecision(ReplyAction.REPLY_NOW, reason="urgent_interrupt")

    if msg_type == "question":
        return ReplyDecision(ReplyAction.REPLY_NOW, reason="question_needs_answer")

    if msg_type == "emotional":
        return ReplyDecision(ReplyAction.REPLY_NOW, reason="emotional_needs_response")

    resolved_phase = phase or current_phase()
    busy_prob = _busy_probability(resolved_phase)
    if state:
        if state.mood in {"sleepy", "guarded", "hurt", "sulking"}:
            busy_prob += 0.12
        if state.mood in {"worried", "affectionate", "miss_you"}:
            busy_prob -= 0.10
        busy_prob += max(0, state.boundary_level - 35) / 300
        busy_prob = max(0.05, min(0.75, busy_prob))
    is_busy = rng.random() < busy_prob

    if msg_type == "ack":
        if has_pending_reply or has_unread:
            return ReplyDecision(ReplyAction.REPLY_NOW, reason="ack_after_pending")
        if state and _ack_may_be_low_energy_emotion(state):
            return ReplyDecision(
                ReplyAction.DEFER,
                defer_minutes=rng.uniform(*LOW_ENERGY_ACK_DEFER_RANGE),
                reason="low_energy_ack_needs_space",
                mark_unread=True,
            )
        if state and _ack_may_leave_open_thread(state, rng):
            return ReplyDecision(
                ReplyAction.DEFER,
                defer_minutes=rng.uniform(*OPEN_THREAD_ACK_DEFER_RANGE),
                reason="ack_leaves_open_thread",
                mark_unread=False,
            )
        return ReplyDecision(ReplyAction.SKIP, reason="pure_acknowledgment", mark_unread=False)

    if msg_type == "empty":
        return ReplyDecision(ReplyAction.SKIP, reason="empty_message", mark_unread=False)

    if has_unread:
        return ReplyDecision(ReplyAction.REPLY_NOW, reason="catching_up_after_unread")

    if is_busy and msg_type == "story":
        if rng.random() < 0.55:
            defer_minutes = _defer_minutes_for_phase(resolved_phase, rng)
            return ReplyDecision(
                ReplyAction.DEFER,
                defer_minutes=defer_minutes,
                reason=f"busy_{resolved_phase}",
                mark_unread=True,
            )

    return ReplyDecision(ReplyAction.REPLY_NOW, reason="default_reply")


def is_urgent_interrupt(text: str) -> bool:
    """Check if a message should immediately fire any pending deferred reply."""
    return classify_message(text) == "urgent"


def _busy_probability(phase: str) -> float:
    return {
        "early_morning": 0.45,
        "morning_focus": 0.55,
        "lunch_break": 0.20,
        "afternoon_classes": 0.50,
        "evening_unwind": 0.10,
        "late_evening": 0.15,
        "deep_night": 0.40,
    }.get(phase, 0.25)


def _defer_minutes_for_phase(phase: str, rng: random.Random) -> float:
    ranges = {
        "early_morning": (5, 15),
        "morning_focus": (15, 45),
        "lunch_break": (5, 15),
        "afternoon_classes": (15, 45),
        "evening_unwind": (3, 12),
        "late_evening": (3, 10),
        "deep_night": (5, 20),
    }
    low, high = ranges.get(phase, (5, 15))
    return rng.uniform(low, high)


def _ack_may_be_low_energy_emotion(state: MoodState) -> bool:
    if state.unresolved_emotion:
        return True
    if state.mood in {"hurt", "guarded", "sulking", "worried"}:
        return True
    return state.emotional_charge >= 45 and state.security <= 40


def _ack_may_leave_open_thread(state: MoodState, rng: random.Random) -> bool:
    chance = 0.08
    if state.relationship_stage in {"close_friend", "ambiguous", "lover"}:
        chance += 0.12
    if state.mood in {"curious", "affectionate", "miss_you", "happy"}:
        chance += 0.10
    if state.initiative >= 45:
        chance += 0.08
    if state.mood in {"sleepy", "guarded"}:
        chance -= 0.08
    return rng.random() < max(0.0, min(0.35, chance))
