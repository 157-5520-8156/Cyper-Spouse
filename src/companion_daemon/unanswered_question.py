from dataclasses import dataclass
from datetime import datetime

from companion_daemon.models import MoodState
from companion_daemon.time import utc_now


@dataclass(frozen=True)
class PendingQuestion:
    text: str
    sent_at: str


@dataclass(frozen=True)
class QuestionResponse:
    """Reaches the model through state changes and the reply policy's
    forbidden-topics list, never as a literal prompt label."""

    kind: str
    memory: str


def last_unanswered_own_question(recent_rows: list[dict[str, str]]) -> PendingQuestion | None:
    seen_incoming_after = False
    for row in reversed(recent_rows):
        direction = row.get("direction")
        text = str(row.get("text") or "").strip()
        if direction == "in":
            seen_incoming_after = True
        if direction == "out" and _looks_like_question(text):
            if seen_incoming_after:
                return None
            return PendingQuestion(text=text, sent_at=str(row.get("sent_at") or ""))
    return None


def apply_unanswered_question_waiting(
    state: MoodState,
    question: PendingQuestion | None,
) -> MoodState:
    if not question or not question.sent_at:
        return state
    sent_at = datetime.fromisoformat(question.sent_at)
    hours = (utc_now() - sent_at).total_seconds() / 3600
    if hours < 0.5:
        return state
    if hours < 6:
        note = "她刚刚问了你一个问题但没等到回答，有点困惑是不是自己问得突然。"
        if state.unresolved_emotion == note:
            return state
        return state.model_copy(
            update={
                "mood": "curious" if state.mood == "calm" else state.mood,
                "curiosity": _clamp(state.curiosity + 3),
                "security": _clamp(state.security - 2),
                "emotional_charge": _clamp(state.emotional_charge + 4),
                "unresolved_emotion": note,
            }
        )
    if hours < 24:
        note = "她问的问题一直没被回答，于是开始收住，好像不想显得追问。"
        if state.unresolved_emotion == note:
            return state
        return state.model_copy(
            update={
                "mood": "miss_you" if state.mood == "calm" else state.mood,
                "curiosity": _clamp(state.curiosity - 2),
                "security": _clamp(state.security - 4),
                "initiative": _clamp(state.initiative - 2),
                "emotional_charge": _clamp(state.emotional_charge + 5),
                "unresolved_emotion": note,
            }
        )
    note = "她把之前那个没被回答的问题放下了，但心里会更少主动追问。"
    if state.unresolved_emotion == note:
        return state
    return state.model_copy(
        update={
            "curiosity": _clamp(state.curiosity - 3),
            "security": _clamp(state.security - 3),
            "initiative": _clamp(state.initiative - 3),
            "unresolved_emotion": note,
        }
    )


def classify_response_to_own_question(
    user_text: str,
    question: PendingQuestion | None,
) -> QuestionResponse | None:
    if not question:
        return None
    text = user_text.strip()
    if not text:
        return None
    if _looks_like_meta_response(text):
        return QuestionResponse("meta", f"用户回应了她的语气而不是问题：{question.text[:80]}")
    if _looks_like_answer(text, question.text):
        return QuestionResponse("answered", f"用户回答了她的问题：{question.text[:80]}")
    return QuestionResponse("skipped", f"用户跳过了她的问题：{question.text[:80]}")


def apply_question_response(state: MoodState, response: QuestionResponse | None) -> MoodState:
    if not response:
        return state
    if response.kind == "answered":
        return state.model_copy(
            update={
                "security": _clamp(state.security + 3),
                "curiosity": _clamp(state.curiosity + 1),
                "emotional_charge": _clamp(state.emotional_charge - 5),
                "unresolved_emotion": None,
            }
        )
    if response.kind == "meta":
        return state.model_copy(
            update={
                "emotional_charge": _clamp(state.emotional_charge + 1),
                "unresolved_emotion": "用户注意到她刚才的语气，她知道自己有点别扭，但不是因为对方冷漠。",
            }
        )
    return state.model_copy(
        update={
            "security": _clamp(state.security - 4),
            "curiosity": _clamp(state.curiosity - 1),
            "emotional_charge": _clamp(state.emotional_charge + 3),
            "unresolved_emotion": "你回来了，但绕开了她刚刚问的问题，她会有一点困惑。",
        }
    )


def _looks_like_question(text: str) -> bool:
    return text.rstrip().endswith(("?", "？")) or any(
        token in text for token in ["你觉得", "要不要", "是不是", "可以吗", "在吗"]
    )


def _looks_like_answer(text: str, question_text: str = "") -> bool:
    if text.rstrip().endswith(("?", "？")):
        return False
    if any(token in question_text for token in ["在哪", "哪里", "哪儿", "上学", "学校", "城市"]):
        if any(token in text for token in ["在", "成都", "上海", "学校", "大学", "理工"]):
            return True
    return any(
        token in text
        for token in [
            "因为",
            "在",
            "是",
            "不是",
            "可以",
            "不可以",
            "要",
            "不要",
            "喜欢",
            "不喜欢",
            "我觉得",
            "刚才",
            "其实",
            "嗯",
            "对",
            "没有",
            "有",
        ]
    )


def _looks_like_meta_response(text: str) -> bool:
    return any(
        token in text
        for token in [
            "语气",
            "口气",
            "怎么啦",
            "怎么了",
            "生气",
            "不开心",
            "吃醋",
            "阴阳怪气",
            "怪怪的",
        ]
    )


def _clamp(value: int) -> int:
    return max(0, min(100, value))
