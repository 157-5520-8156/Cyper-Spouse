from dataclasses import dataclass

from companion_daemon.emotion_core import apply_emotion_deltas, text_emotion_deltas
from companion_daemon.models import MoodState

BLEED_WEIGHT = 0.10
PER_EMOTION_CAP = 2.0
TOTAL_DELTA_CAP = 5.0


@dataclass(frozen=True)
class ContextMessage:
    text: str
    is_user: bool


def apply_context_emotion_bleed(
    state: MoodState,
    messages: list[ContextMessage],
    *,
    source: str = "external_context_bleed",
) -> MoodState:
    deltas = context_emotion_deltas(messages)
    if not deltas:
        return state
    return apply_emotion_deltas(
        state,
        deltas,
        source=source,
        update_affinity=False,
    )


def context_emotion_deltas(messages: list[ContextMessage]) -> dict[str, float]:
    combined: dict[str, float] = {}
    for message in messages:
        raw = text_emotion_deltas(message.text, is_user=message.is_user)
        for emotion, value in raw.items():
            combined[emotion] = combined.get(emotion, 0.0) + value * BLEED_WEIGHT
    if not combined:
        return {}

    capped = {
        emotion: _signed_min(value, PER_EMOTION_CAP)
        for emotion, value in combined.items()
    }
    total = sum(abs(value) for value in capped.values())
    if total > TOTAL_DELTA_CAP:
        scale = TOTAL_DELTA_CAP / total
        capped = {emotion: value * scale for emotion, value in capped.items()}
    return {
        emotion: value
        for emotion, value in capped.items()
        if abs(value) > 0.08
    }


def _signed_min(value: float, cap: float) -> float:
    if value >= 0:
        return min(value, cap)
    return max(value, -cap)
