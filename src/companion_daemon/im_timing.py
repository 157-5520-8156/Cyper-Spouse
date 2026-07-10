import random

from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.models import MoodState
from companion_daemon.reply_timing import emotion_reply_timing


def initial_reply_delay_seconds(
    incoming: IncomingMessage,
    reply: CompanionReply,
    *,
    state: MoodState | None = None,
    rng: random.Random | None = None,
) -> float:
    rng = rng or random.Random()
    incoming_len = len(incoming.text or "")
    reply_len = len(reply.text or "")
    read_time = min(5.5, 0.7 + incoming_len / 22)
    think_time = {
        "hurt": 3.2,
        "guarded": 2.8,
        "sulking": 2.4,
        "worried": 1.8,
        "curious": 1.5,
        "miss_you": 1.3,
        "happy": 1.0,
        "affectionate": 1.1,
        "sleepy": 2.2,
        "jealous_soft": 2.1,
        "calm": 1.4,
    }.get(reply.mood, 1.4)
    typing_time = min(8.0, 0.8 + reply_len / 18)
    jitter = rng.uniform(0.75, 1.25)
    base_delay = max(1.2, min(14.0, (read_time + think_time + typing_time) * jitter))
    if state is None:
        return base_delay

    emotional = emotion_reply_timing(state, rng=rng)
    emotion_delay = (
        emotional.read_delay_ms
        + emotional.typing_lead_ms
        + emotional.reply_delay_ms
        + min(emotional.ghost_delay_ms, 45_000)
    ) / 1000
    cap = 75.0 if emotional.ghost_delay_ms else 18.0
    return max(base_delay, min(cap, emotion_delay))


def between_part_delay_seconds(part: str, *, rng: random.Random | None = None) -> float:
    """Return the visible pause before a follow-up bubble.

    This is deliberately longer than a network retry interval: the gap is an
    interaction window in which the other person can acknowledge, redirect, or
    take the floor.  The QQ adapters use that window for interruption handling.
    """
    rng = rng or random.Random()
    return max(1.8, min(7.2, (1.35 + len(part) / 13) * rng.uniform(0.82, 1.28)))
