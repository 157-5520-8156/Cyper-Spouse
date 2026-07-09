from companion_daemon.models import MoodState


def apply_personality_drift(state: MoodState) -> MoodState:
    affinity = state.emotion_affinity or {}
    warmth = affinity.get("trust", 0) + affinity.get("joy", 0) * 0.6 + affinity.get("love", 0) * 0.5
    aversion = affinity.get("anger", 0) + affinity.get("disgust", 0) + affinity.get("fear", 0) * 0.5
    if warmth >= 8 and aversion < 8:
        return state.model_copy(
            update={
                "security": _clamp(state.security + 1),
                "curiosity": _clamp(state.curiosity + 1),
                "boundary_level": _clamp(state.boundary_level - 1),
            }
        )
    if aversion >= 8:
        return state.model_copy(
            update={
                "security": _clamp(state.security - 1),
                "patience": _clamp(state.patience - 1),
                "boundary_level": _clamp(state.boundary_level + 1),
            }
        )
    return state


def personality_drift_line(state: MoodState) -> str:
    affinity = state.emotion_affinity or {}
    warmth = affinity.get("trust", 0) + affinity.get("joy", 0) * 0.6 + affinity.get("love", 0) * 0.5
    aversion = affinity.get("anger", 0) + affinity.get("disgust", 0) + affinity.get("fear", 0) * 0.5
    if warmth >= 8 and aversion < 8:
        return "长期性格漂移: 长期被稳定尊重后，她默认更放松、更敢表达。"
    if aversion >= 8:
        return "长期性格漂移: 长期紧张感偏高，她默认更敏感、更慢热。"
    return "长期性格漂移: 目前仍接近初始性格，不要突然大幅改变。"


def _clamp(value: int) -> int:
    return max(0, min(100, value))
