import random

from companion_daemon.im_timing import between_part_delay_seconds, initial_reply_delay_seconds
from companion_daemon.models import CompanionReply, IncomingMessage, MoodState


def test_initial_reply_delay_has_human_floor() -> None:
    delay = initial_reply_delay_seconds(
        IncomingMessage(platform="qq", platform_user_id="u", text="这个事情我想了挺久"),
        CompanionReply(canonical_user_id="geoff", mood="calm", text="嗯，我懂你的意思。"),
        rng=random.Random(1),
    )

    assert delay >= 1.2


def test_initial_reply_delay_uses_emotion_timing_when_state_is_cold() -> None:
    incoming = IncomingMessage(platform="qq", platform_user_id="u", text="随便你")
    reply = CompanionReply(canonical_user_id="geoff", mood="hurt", text="嗯。")
    warm = initial_reply_delay_seconds(
        incoming,
        reply,
        state=MoodState(emotion_vector={"joy": 70, "trust": 65}),
        rng=random.Random(1),
    )
    cold = initial_reply_delay_seconds(
        incoming,
        reply,
        state=MoodState(emotion_vector={"anger": 90, "sadness": 70, "fear": 50}),
        rng=random.Random(1),
    )

    assert cold > warm
    assert cold > 10


def test_between_part_delay_scales_with_text() -> None:
    short = between_part_delay_seconds("嗯。", rng=random.Random(1))
    long = between_part_delay_seconds("刚刚其实还想补一句，但又怕打扰你。", rng=random.Random(1))

    assert long > short
