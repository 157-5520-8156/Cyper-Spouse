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


async def _frozen_replay(tmp_path, monkeypatch, name: str) -> tuple[dict[str, object], list[tuple[object, ...]]]:
    """Run the same real turn path under one frozen daemon clock."""
    import companion_daemon.context_orchestrator as context_orchestrator
    import companion_daemon.db as db_module
    import companion_daemon.emotion_state as emotion_state
    import companion_daemon.engine as engine_module
    import companion_daemon.life_runtime as life_runtime

    for module in (db_module, engine_module, emotion_state, life_runtime, context_orchestrator):
        monkeypatch.setattr(module, "utc_now", lambda: BASE)
    store = CompanionStore(tmp_path / f"{name}.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")
    await engine.handle_message(incoming("我先忙一会儿", 0))
    await engine.handle_message(incoming("我回来了", 40))
    state = store.get_mood_state("geoff").model_dump(mode="json")
    traces = [
        (row["appraisal"], row["expression_policy"], row["status"], row["output_text"])
        for row in store.recent_turn_traces("geoff")
    ]
    return state, traces


@pytest.mark.asyncio
async def test_frozen_clock_replay_is_reproducible_end_to_end(tmp_path, monkeypatch) -> None:
    first = await _frozen_replay(tmp_path, monkeypatch, "first")
    second = await _frozen_replay(tmp_path, monkeypatch, "second")

    assert first == second


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_text", "second_text", "expected"),
    [
        ("你必须马上回我", "对不起，刚刚太急了", ("control_pressure", "repair_attempt")),
        ("谢谢你刚才认真听我说", "你觉得呢？", ("warmth_received", "curiosity_invited")),
        ("我先忙一下", "我今天其实挺难受的", ("availability_drop", "user_vulnerable")),
        ("今天下雨了", "你在吗？", ("ordinary_message", "curiosity_invited")),
        ("宝宝你在吗", "那我们慢慢聊", ("premature_intimacy", "ordinary_message")),
        ("我好烦，想哭", "现在好一点了", ("user_vulnerable", "ordinary_message")),
        ("闭嘴", "我认真道歉，以后会注意", ("boundary_violation", "repair_attempt")),
        ("我刚下班", "你觉得今天怎么样？", ("return_after_gap", "curiosity_invited")),
        ("没空，晚点说", "我回来了", ("availability_drop", "return_after_gap")),
            ("你只能听我的", "对不起，我不该这样命令你", ("control_pressure", "repair_attempt")),
        ("你真细心", "我有点累", ("warmth_received", "user_vulnerable")),
        ("我先忙", "今天成都下雨了", ("availability_drop", "ordinary_message")),
        ("亲爱的你在吗", "你叫什么？", ("premature_intimacy", "curiosity_invited")),
        ("我撑不住了", "谢谢你", ("user_vulnerable", "warmth_received")),
        ("有病", "对不起", ("boundary_violation", "repair_attempt")),
        ("我到家了", "你还在吗？", ("return_after_gap", "curiosity_invited")),
            ("等一下", "我想问你个问题？", ("availability_drop", "curiosity_invited")),
        ("今天普通的一天", "我有点焦虑", ("ordinary_message", "user_vulnerable")),
        ("老婆你在吗", "算了慢慢认识", ("premature_intimacy", "ordinary_message")),
        ("我真的失眠了", "我现在去睡了", ("user_vulnerable", "ordinary_message")),
            ("你算什么", "对不起，刚才是我不对", ("boundary_violation", "repair_attempt")),
    ],
)
async def test_replay_matrix_preserves_two_turn_appraisal_causality(
    tmp_path, first_text: str, second_text: str, expected: tuple[str, str]
) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")

    first = await engine.handle_message(incoming(first_text, 0))
    second = await engine.handle_message(incoming(second_text, 5))

    assert first and second
    traces = store.recent_turn_traces("geoff")[-2:]
    assert tuple(trace["appraisal"] for trace in traces) == expected
    assert all(trace["status"] == "delivered" for trace in traces)
