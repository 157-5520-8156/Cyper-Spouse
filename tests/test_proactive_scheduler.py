import random

from companion_daemon.proactive_scheduler import (
    _jittered_cooldown_minutes,
    _minutes_since,
    _next_sleep_seconds,
)


def test_minutes_since_none() -> None:
    assert _minutes_since(None) is None


def test_jittered_cooldown_is_stable_and_bounded() -> None:
    first = _jittered_cooldown_minutes(
        user_id="geoff",
        base_minutes=166,
        state_key="stranger:calm",
        last_sent="2026-07-09T12:11:56+00:00",
    )
    second = _jittered_cooldown_minutes(
        user_id="geoff",
        base_minutes=166,
        state_key="stranger:calm",
        last_sent="2026-07-09T12:11:56+00:00",
    )

    assert first == second
    assert 142 <= first <= 213


def test_jittered_cooldown_does_not_shorten_guarded_states() -> None:
    cooldown = _jittered_cooldown_minutes(
        user_id="geoff",
        base_minutes=166,
        state_key="friend:guarded",
        last_sent="2026-07-09T12:11:56+00:00",
    )

    assert cooldown >= 166


def test_next_sleep_seconds_adds_scheduler_jitter() -> None:
    sleep = _next_sleep_seconds(900, random.Random(1))

    assert 585 <= sleep <= 1215
    assert sleep != 900
