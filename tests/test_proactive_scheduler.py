import random
from types import SimpleNamespace

import pytest

import companion_daemon.proactive_scheduler as proactive_scheduler
from companion_daemon.models import MoodState
from companion_daemon.proactive_scheduler import (
    _jittered_cooldown_minutes,
    _minutes_since,
    _next_sleep_seconds,
    scheduler_loop,
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


@pytest.mark.asyncio
async def test_scheduler_survives_life_event_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def canonical_users(self) -> list[str]:
            return ["geoff"]

        def get_mood_state(self, user_id: str) -> MoodState:
            return MoodState(relationship_stage="friend", mood="calm", attachment=20)

        def last_proactive_delivery(self, user_id: str, channel: str) -> None:
            return None

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()

    async def fake_run_once(user_id: str, *, send: bool, sandbox: bool) -> None:
        return None

    async def failing_life_event(**kwargs) -> bool:
        raise RuntimeError("qq 400")

    monkeypatch.setattr(proactive_scheduler, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(proactive_scheduler, "get_settings", lambda: SimpleNamespace(proactive_min_cooldown_minutes=30))
    monkeypatch.setattr(proactive_scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(proactive_scheduler, "run_life_event", failing_life_event)
    monkeypatch.setattr(proactive_scheduler, "life_event_probability", lambda state: 1.0)
    monkeypatch.setattr(proactive_scheduler.random, "random", lambda: 0.0)

    await scheduler_loop(
        send=True,
        sandbox=True,
        once=True,
        life_events=True,
        generate_life_images=False,
        life_image_kind="life",
    )
