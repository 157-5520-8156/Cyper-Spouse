import random

from companion_daemon.models import CompanionReply, IncomingMessage


def initial_reply_delay_seconds(
    incoming: IncomingMessage,
    reply: CompanionReply,
    *,
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
    return max(1.2, min(14.0, (read_time + think_time + typing_time) * jitter))


def between_part_delay_seconds(part: str, *, rng: random.Random | None = None) -> float:
    rng = rng or random.Random()
    return max(0.9, min(4.2, (0.8 + len(part) / 16) * rng.uniform(0.8, 1.25)))
