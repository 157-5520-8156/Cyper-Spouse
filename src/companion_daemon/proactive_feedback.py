import re
from dataclasses import dataclass

from companion_daemon.models import MoodState


@dataclass(frozen=True)
class ProactiveFeedback:
    """The prompt never names this feedback; it reaches the model only through
    the state changes applied below and the stored memory."""

    kind: str
    memory_content: str


def classify_proactive_feedback(text: str) -> ProactiveFeedback:
    stripped = text.strip()
    if _has_any(stripped, [r"滚", r"别烦", r"烦不烦", r"闭嘴", r"不用你"]):
        return ProactiveFeedback("rejected", "主动找用户后被拒斥，之后会更克制。")
    if _has_any(stripped, [r"哈哈", r"嘿嘿", r"我在", r"来了", r"刚忙完", r"抱歉", r"对不起", r"谢谢", r"想你"]):
        return ProactiveFeedback("warm", "主动找用户后得到温和回应，安全感上升。")
    if _has_any(stripped, [r"等下", r"一会儿", r"忙", r"晚点", r"先不说", r"哦$", r"嗯$"]):
        return ProactiveFeedback("thin_or_busy", "主动找用户后得到短回应，知道对方在忙但会稍微收住。")
    return ProactiveFeedback("answered", "主动找用户后得到回应，紧张感下降。")


def apply_proactive_feedback(state: MoodState, feedback: ProactiveFeedback) -> MoodState:
    if feedback.kind == "warm":
        return state.model_copy(
            update={
                "mood": "happy" if state.mood in {"miss_you", "worried", "curious"} else state.mood,
                "trust": _clamp(state.trust + 2),
                "security": _clamp(state.security + 5),
                "attachment": _clamp(state.attachment + 1),
                "initiative": _clamp(state.initiative - 4),
                "emotional_charge": _clamp(state.emotional_charge - 10),
                "unresolved_emotion": None,
            }
        )
    if feedback.kind == "thin_or_busy":
        return state.model_copy(
            update={
                "security": _clamp(state.security - 1),
                "attachment": _clamp(state.attachment + 1),
                "initiative": _clamp(state.initiative + 1),
                "emotional_charge": _clamp(state.emotional_charge + 2),
                "unresolved_emotion": "你回应了她，但她感觉你可能还在忙，所以会稍微收住一点。",
            }
        )
    if feedback.kind == "rejected":
        return state.model_copy(
            update={
                "mood": "hurt" if state.relationship_stage in {"friend", "close_friend", "ambiguous", "lover"} else "guarded",
                "trust": _clamp(state.trust - 3),
                "security": _clamp(state.security - 7),
                "patience": _clamp(state.patience - 5),
                "initiative": _clamp(state.initiative - 8),
                "emotional_charge": _clamp(state.emotional_charge + 10),
                "boundary_level": _clamp(state.boundary_level + 1),
                "unresolved_emotion": "主动找你之后被冷冷推开，她会先保持距离。",
            }
        )
    return state.model_copy(
        update={
            "security": _clamp(state.security + 2),
            "initiative": _clamp(state.initiative - 2),
            "emotional_charge": _clamp(state.emotional_charge - 5),
        }
    )


def _has_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _clamp(value: int) -> int:
    return max(0, min(100, value))
