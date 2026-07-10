from dataclasses import dataclass
import random

from companion_daemon.models import MoodState


@dataclass(frozen=True)
class EmotionReplyTiming:
    read_delay_ms: int
    ghost_delay_ms: int
    typing_lead_ms: int
    reply_delay_ms: int


GHOST_VOLATILITY_THRESHOLD = 0.42


def emotional_volatility(state: MoodState) -> float:
    """Cold-over-warm imbalance in the Plutchik vector; the shared ghost signal."""
    emotion = state.emotion_vector
    warm_factor = (
        emotion.get("joy", 0) * 0.55
        + emotion.get("trust", 0) * 0.45
        + emotion.get("anticipation", 0) * 0.3
    ) / 100
    cold_factor = (
        emotion.get("anger", 0) * 0.6
        + emotion.get("sadness", 0) * 0.35
        + emotion.get("fear", 0) * 0.25
    ) / 100
    return max(0.0, cold_factor - warm_factor * 0.65)


def emotional_ghost_minutes(
    state: MoodState,
    *,
    rng: random.Random | None = None,
) -> float | None:
    """Read-but-not-reply window in minutes, or None when she is not upset enough.

    This is the long-form ghost: the reply decision layer turns it into a real
    silent gap, while `emotion_reply_timing` only stretches the pre-reply pause.
    """
    rng = rng or random.Random()
    volatility = emotional_volatility(state)
    if volatility <= GHOST_VOLATILITY_THRESHOLD:
        return None
    return round(10 + rng.random() * 25 * min(1.9, 1 + volatility), 1)


def emotion_reply_timing(
    state: MoodState,
    *,
    rng: random.Random | None = None,
) -> EmotionReplyTiming:
    rng = rng or random.Random()
    volatility = emotional_volatility(state)
    warm_factor = (
        state.emotion_vector.get("joy", 0) * 0.55
        + state.emotion_vector.get("trust", 0) * 0.45
        + state.emotion_vector.get("anticipation", 0) * 0.3
    ) / 100

    return EmotionReplyTiming(
        read_delay_ms=round((700 + rng.random() * 1800) * (1 + volatility * 1.6)),
        ghost_delay_ms=round((20000 + rng.random() * 70000) * min(1.9, 1 + volatility))
        if volatility > GHOST_VOLATILITY_THRESHOLD
        else 0,
        typing_lead_ms=round(300 + rng.random() * 900),
        reply_delay_ms=round(
            (300 + rng.random() * 2200)
            * max(0.25, 1 + volatility * 1.8 - warm_factor * 0.95)
        ),
    )
