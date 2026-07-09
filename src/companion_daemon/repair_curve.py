from companion_daemon.models import MoodState


def apply_repair_curve(state: MoodState, *, message_text: str) -> MoodState:
    if state.last_interaction_event != "repair_attempt":
        return state
    serious = any(token in message_text for token in ["认真", "解释", "以后", "我会注意", "不是敷衍"])
    perfunctory = message_text.strip() in {"对不起", "抱歉", "错了", "行了对不起"}
    if serious:
        return state.model_copy(
            update={
                "mood": "calm",
                "trust": _clamp(state.trust + 3),
                "security": _clamp(state.security + 4),
                "emotional_charge": _clamp(state.emotional_charge - 8),
                "boundary_level": _clamp(state.boundary_level - 1),
                "unresolved_emotion": "这次道歉听起来更认真，她真正松动了一些。",
            }
        )
    if perfunctory:
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
