from companion_daemon.models import MoodState

# Affinity is stored as baseline minus anchor, so composite scores stay in the
# low single digits even after sustained relationship arcs. The old threshold
# of 8 effectively never fired in production.
WARMTH_DRIFT_THRESHOLD = 4.0
AVERSION_DRIFT_THRESHOLD = 1.4


def affinity_warmth_score(affinity: dict[str, float] | None) -> float:
    affinity = affinity or {}
    return (
        affinity.get("trust", 0)
        + affinity.get("joy", 0) * 0.6
        + affinity.get("love", 0) * 0.5
    )


def affinity_aversion_score(affinity: dict[str, float] | None) -> float:
    affinity = affinity or {}
    return (
        affinity.get("anger", 0)
        + affinity.get("disgust", 0)
        + affinity.get("fear", 0) * 0.5
    )


def apply_personality_drift(state: MoodState) -> MoodState:
    warmth = affinity_warmth_score(state.emotion_affinity)
    aversion = affinity_aversion_score(state.emotion_affinity)
    if warmth >= WARMTH_DRIFT_THRESHOLD and aversion < AVERSION_DRIFT_THRESHOLD:
        return state.model_copy(
            update={
                "security": _clamp(state.security + 1),
                "curiosity": _clamp(state.curiosity + 1),
                "boundary_level": _clamp(state.boundary_level - 1),
            }
        )
    if aversion >= AVERSION_DRIFT_THRESHOLD:
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
