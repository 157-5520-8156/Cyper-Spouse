from dataclasses import dataclass
import random

from companion_daemon.models import MoodState


@dataclass(frozen=True)
class EmotionReplyTiming:
    delivered_delay_ms: int
    read_delay_ms: int
    ghost_delay_ms: int
    typing_lead_ms: int
    reply_delay_ms: int


def emotion_reply_timing(
    state: MoodState,
    *,
    rng: random.Random | None = None,
) -> EmotionReplyTiming:
    rng = rng or random.Random()
    emotion = state.emotion_vector
    anger = emotion.get("anger", 0)
    sadness = emotion.get("sadness", 0)
    fear = emotion.get("fear", 0)
    trust = emotion.get("trust", 0)
    joy = emotion.get("joy", 0)
    anticipation = emotion.get("anticipation", 0)

    warm_factor = (joy * 0.55 + trust * 0.45 + anticipation * 0.3) / 100
    cold_factor = (anger * 0.6 + sadness * 0.35 + fear * 0.25) / 100
    volatility = max(0.0, cold_factor - warm_factor * 0.65)

    return EmotionReplyTiming(
        delivered_delay_ms=round(250 + rng.random() * 900),
        read_delay_ms=round((700 + rng.random() * 1800) * (1 + volatility * 1.6)),
        ghost_delay_ms=round((20000 + rng.random() * 70000) * min(1.9, 1 + volatility))
        if volatility > 0.42
        else 0,
        typing_lead_ms=round(300 + rng.random() * 900),
        reply_delay_ms=round(
            (300 + rng.random() * 2200)
            * max(0.25, 1 + volatility * 1.8 - warm_factor * 0.95)
        ),
    )
