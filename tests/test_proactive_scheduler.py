import random
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import companion_daemon.proactive_scheduler as proactive_scheduler
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.proactive_scheduler import (
    _jittered_cooldown_minutes,
    _minutes_since,
    _next_sleep_seconds,
    scheduler_loop,
    recover_world_due_replies,
    recover_world_due_conversation_pulses,
)
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world import WorldKernel


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
async def test_world_due_reply_recovery_settles_original_action(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC

    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=world, world_id=world_id)
    message = IncomingMessage(
        platform="qq", platform_user_id="openid", text="晚点说", message_id="recover-1",
        sent_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
    )
    action_id = engine.create_deferred_reply_task(
        message, defer_minutes=1, reason="busy", now=datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
    )

    class FakeDelivery:
        def __init__(self, *args, **kwargs):
            pass

        async def send_text(self, recipient_id: str, text: str) -> None:
            assert recipient_id == "openid"

    monkeypatch.setattr(proactive_scheduler, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(proactive_scheduler, "get_settings", lambda: SimpleNamespace())
    logical_at = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(world_id, logical_at.replace(minute=logical_at.minute + 2), expected_revision=world.revision(world_id))
    recovered = await recover_world_due_replies(engine, send=True, sandbox=True)

    assert recovered == 1
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "delivered"


@pytest.mark.asyncio
async def test_policy_deferred_reply_recovery_does_not_cancel_itself_or_reobserve_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    logical_at = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.submit(
        {"type": "change_need", "world_id": world_id, "need": "energy", "delta": -50},
        expected_revision=world.revision(world_id),
    )
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=world, world_id=world_id)
    message = IncomingMessage(platform="qq", platform_user_id="openid", text="在吗", message_id="policy-recover")
    assert await engine.handle_message(message) is None
    action_id = str(world.snapshot(world_id)["communication"]["deferred_action_id"])

    class FakeDelivery:
        def __init__(self, *args, **kwargs):
            pass

        async def send_text(self, recipient_id: str, text: str) -> None:
            assert recipient_id == "openid"
            assert text

    monkeypatch.setattr(proactive_scheduler, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(proactive_scheduler, "get_settings", lambda: SimpleNamespace())
    world.advance(world_id, logical_at + timedelta(minutes=21), expected_revision=world.revision(world_id))

    recovered = await recover_world_due_replies(engine, send=True, sandbox=True)

    events = world.events(world_id)
    assert recovered == 1
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "delivered"
    assert sum(event.event_type == "UserMessageObserved" and event.payload["message_id"] == "policy-recover" for event in events) == 1
    assert not any(event.event_type == "ActionCancelled" and event.payload["action_id"] == action_id for event in events)


@pytest.mark.asyncio
async def test_world_conversation_pulse_recovery_uses_world_action(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=world, world_id=world_id)
    logical_at = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    action_id = engine.schedule_conversation_pulse(
        canonical_user_id="geoff", platform="qq", platform_user_id="openid",
        reply_sent_at=logical_at, mode="quick_continue", delay_seconds=1, remaining=[],
    )
    world.advance(world_id, logical_at + timedelta(minutes=1), expected_revision=world.revision(world_id))

    class FakeDelivery:
        def __init__(self, *args, **kwargs):
            pass

        async def send_text(self, recipient_id: str, text: str) -> None:
            assert recipient_id == "openid"
            assert text

    monkeypatch.setattr(proactive_scheduler, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(proactive_scheduler, "get_settings", lambda: SimpleNamespace())
    recovered = await recover_world_due_conversation_pulses(engine, send=True, sandbox=True)

    assert recovered == 1
    snapshot = world.snapshot(world_id)
    assert snapshot["actions"][str(action_id)]["status"] == "delivered"
    assert any(
        action["kind"] == "outgoing_message"
        and action.get("message_kind") == "afterthought"
        and action["status"] == "delivered"
        for action in snapshot["actions"].values()
    )


@pytest.mark.asyncio
async def test_scheduler_honors_last_decision_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta

    class FakeStore:
        def canonical_users(self) -> list[str]:
            return ["geoff"]

        def get_mood_state(self, user_id: str) -> MoodState:
            return MoodState(relationship_stage="friend", mood="calm")

        def last_proactive_delivery(self, user_id: str, channel: str) -> None:
            return None

        def last_proactive_event(self, user_id: str) -> dict:
            return {
                "should_send": 0,
                "cooldown_minutes": 120,
                "created_at": (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(),
            }

        def next_due_social_task(self, user_id: str, *, kinds, now) -> None:
            return None

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()

    calls: list[str] = []

    async def fake_run_once(user_id: str, *, send: bool, sandbox: bool) -> None:
        calls.append(user_id)

    monkeypatch.setattr(proactive_scheduler, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(proactive_scheduler, "get_settings", lambda: SimpleNamespace(proactive_min_cooldown_minutes=30))
    monkeypatch.setattr(proactive_scheduler, "run_once", fake_run_once)

    await scheduler_loop(
        send=True,
        sandbox=True,
        once=True,
        life_events=False,
        generate_life_images=False,
        life_image_kind="life",
    )

    assert calls == []


@pytest.mark.asyncio
async def test_due_social_task_overrides_decision_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta

    class FakeStore:
        def canonical_users(self) -> list[str]:
            return ["geoff"]

        def get_mood_state(self, user_id: str) -> MoodState:
            return MoodState(relationship_stage="friend", mood="calm")

        def last_proactive_delivery(self, user_id: str, channel: str) -> None:
            return None

        def last_proactive_event(self, user_id: str) -> dict:
            return {
                "should_send": 0,
                "cooldown_minutes": 120,
                "created_at": (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(),
            }

        def next_due_social_task(self, user_id: str, *, kinds, now) -> dict:
            return {"id": 7, "kind": "comfort_followup"}

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()

    calls: list[str] = []

    async def fake_run_once(user_id: str, *, send: bool, sandbox: bool) -> None:
        calls.append(user_id)

    monkeypatch.setattr(proactive_scheduler, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(proactive_scheduler, "get_settings", lambda: SimpleNamespace(proactive_min_cooldown_minutes=30))
    monkeypatch.setattr(proactive_scheduler, "run_once", fake_run_once)

    await scheduler_loop(
        send=True,
        sandbox=True,
        once=True,
        life_events=False,
        generate_life_images=False,
        life_image_kind="life",
    )

    assert calls == ["geoff"]


@pytest.mark.asyncio
async def test_scheduler_refreshes_waiting_state_even_when_cooldown_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def canonical_users(self) -> list[str]:
            return ["geoff"]

        def get_mood_state(self, user_id: str) -> MoodState:
            return MoodState(relationship_stage="friend", mood="calm")

        def last_proactive_delivery(self, user_id: str, channel: str) -> str:
            # A very recent delivery keeps the proactive cooldown active.
            from datetime import datetime

            return datetime.now().astimezone().isoformat()

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.refreshed: list[str] = []

        def refresh_waiting_state(self, user_id: str) -> MoodState:
            self.refreshed.append(user_id)
            return self.store.get_mood_state(user_id)

    engine = FakeEngine()

    async def fake_run_once(user_id: str, *, send: bool, sandbox: bool) -> None:
        raise AssertionError("cooldown should have skipped the proactive decision")

    monkeypatch.setattr(proactive_scheduler, "build_companion_engine", lambda: engine)
    monkeypatch.setattr(proactive_scheduler, "get_settings", lambda: SimpleNamespace(proactive_min_cooldown_minutes=30))
    monkeypatch.setattr(proactive_scheduler, "run_once", fake_run_once)

    await scheduler_loop(
        send=True,
        sandbox=True,
        once=True,
        life_events=False,
        generate_life_images=False,
        life_image_kind="life",
    )

    assert engine.refreshed == ["geoff"]


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
