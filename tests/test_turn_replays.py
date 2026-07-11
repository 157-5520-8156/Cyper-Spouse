"""Deterministic multi-turn acceptance replays for the companion's core loop."""
from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MoodState


BASE = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def incoming(text: str, minute: int) -> IncomingMessage:
    return IncomingMessage(
        platform="qq", platform_user_id="geoff", text=text, sent_at=BASE + timedelta(minutes=minute)
    )


@pytest.mark.asyncio
async def test_replay_boundary_then_repair_keeps_a_multi_turn_behavioral_arc(tmp_path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")

    first = await engine.handle_message(incoming("滚，别烦我", 0))
    second = await engine.handle_message(incoming("对不起，刚刚那样说不对", 3))

    assert first and second
    traces = store.recent_turn_traces("geoff")[-2:]
    assert [trace["appraisal"] for trace in traces] == ["boundary_violation", "repair_attempt"]
    assert "保持短而清楚" in traces[0]["observable_reason"]
    assert "允许缓和但不立刻翻篇" in traces[1]["observable_reason"]
    state = store.get_mood_state("geoff")
    assert state.boundary_level > 0
    assert state.emotional_charge > 0


@pytest.mark.asyncio
async def test_replay_busy_then_return_stops_the_old_waiting_impulse(tmp_path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(relationship_stage="friend", initiative=45))
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")

    await engine.handle_message(incoming("我先忙一会儿", 0))
    returned = await engine.handle_message(incoming("我回来了", 40))

    assert returned is not None
    traces = store.recent_turn_traces("geoff")[-2:]
    assert [trace["appraisal"] for trace in traces] == ["availability_drop", "return_after_gap"]
    assert "收住主动性" in traces[0]["observable_reason"]
    assert "自然接上当前话题" in traces[1]["observable_reason"]
    assert not any(task["kind"] == "withheld_impulse" and task["status"] == "pending" for task in store.recent_social_tasks("geoff"))


@pytest.mark.asyncio
async def test_replay_vulnerability_creates_then_cancels_a_followup_when_user_returns(tmp_path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")

    first = await engine.handle_message(incoming("我今天真的有点撑不住", 0))
    tasks_after_first = store.recent_social_tasks("geoff")
    followup = next(task for task in tasks_after_first if task["kind"] == "comfort_followup")
    second = await engine.handle_message(incoming("现在缓过来一点了", 10))

    assert first and second
    tasks = store.recent_social_tasks("geoff")
    cancelled = next(task for task in tasks if task["id"] == followup["id"])
    assert cancelled["status"] == "cancelled"
    assert [trace["appraisal"] for trace in store.recent_turn_traces("geoff")[-2:]] == [
        "user_vulnerable",
        "ordinary_message",
    ]


@pytest.mark.asyncio
async def test_replay_early_intimacy_keeps_boundary_instead_of_accepting_the_label(tmp_path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")

    reply = await engine.handle_message(incoming("宝宝你在吗", 0))

    assert reply is not None
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["appraisal"] == "premature_intimacy"
    assert store.get_mood_state("geoff").relationship_stage == "stranger"
