from dataclasses import dataclass
from typing import Literal

from companion_daemon.models import MoodState

RepairQuality = Literal["serious", "perfunctory"]

# Shared vocabulary for both state changes and key-event bookkeeping.
SERIOUS_REPAIR_TOKENS = (
    "认真道歉",
    "我想好好解释",
    "刚才我确实不该",
    "以后我会注意",
    "解释",
    "我会注意",
    "不是敷衍",
)

PERFUNCTORY_REPAIRS = frozenset({"对不起", "抱歉", "错了", "行了对不起"})

BASIC_REPAIR_TOKENS = ("抱歉", "对不起", "刚才我不该", "我说重了")


def is_repair_message(message_text: str) -> bool:
    """Whether this message should enter the repair_attempt interaction path."""
    stripped = message_text.strip()
    if stripped in PERFUNCTORY_REPAIRS:
        return True
    if any(token in message_text for token in BASIC_REPAIR_TOKENS):
        return True
    return any(token in message_text for token in SERIOUS_REPAIR_TOKENS)


@dataclass(frozen=True)
class SeriousRepairKeyEvent:
    """Memory/prompt marker only — state is owned by apply_repair_curve."""

    kind: str = "serious_repair"
    memory: str = "用户做了认真修复，不只是随口道歉。"
    prompt_line: str = "关键事件: 用户认真修复关系；她可以明显缓和，但仍保留一点观察。"


def classify_repair_quality(message_text: str) -> RepairQuality | None:
    stripped = message_text.strip()
    if stripped in PERFUNCTORY_REPAIRS:
        return "perfunctory"
    if any(token in message_text for token in SERIOUS_REPAIR_TOKENS):
        return "serious"
    return None


def serious_repair_key_event(state: MoodState, message_text: str) -> SeriousRepairKeyEvent | None:
    """Return a key-event record when a repair attempt reads as genuinely serious.

    This never mutates state; callers use it for memory, prompt hints, and
    relationship bonus after apply_repair_curve has already run.
    """
    if state.last_interaction_event != "repair_attempt":
        return None
    if classify_repair_quality(message_text) != "serious":
        return None
    return SeriousRepairKeyEvent()


def apply_repair_curve(state: MoodState, *, message_text: str) -> MoodState:
    if state.last_interaction_event != "repair_attempt":
        return state
    quality = classify_repair_quality(message_text)
    if quality == "serious":
        return state.model_copy(
            update={
                "mood": "calm" if state.mood in {"hurt", "guarded", "sulking"} else state.mood,
                "trust": _clamp(state.trust + 4),
                "patience": _clamp(state.patience + 6),
                "security": _clamp(state.security + 5),
                "boundary_level": _clamp(state.boundary_level - 2),
                "emotional_charge": _clamp(state.emotional_charge - 12),
                "unresolved_emotion": "这次道歉听起来更认真，她真正松动了一些，但还会看后续行动。",
            }
        )
    if quality == "perfunctory":
        return state.model_copy(
            update={
                "mood": "guarded" if state.mood in {"hurt", "sulking"} else state.mood,
                "patience": _clamp(state.patience - 3),
                "security": _clamp(state.security - 2),
                "emotional_charge": _clamp(state.emotional_charge + 4),
                "unresolved_emotion": "道歉太短，她会觉得可能只是想快点翻篇。",
            }
        )
    return state


def _clamp(value: int) -> int:
    return max(0, min(100, value))
