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

def _clamp(value: int) -> int:
    return max(0, min(100, value))
