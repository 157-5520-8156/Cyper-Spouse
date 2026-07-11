"""Deterministic acceptance replays for the world-only companion path."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage
from companion_daemon.world import WorldKernel


BASE = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def message(text: str, index: int) -> IncomingMessage:
    return IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id=f"world-replay-{index}",
        text=text,
        sent_at=BASE + timedelta(minutes=index),
    )


def world_engine(tmp_path: Path) -> tuple[WorldKernel, str, CompanionEngine]:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    return world, world_id, CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=world, world_id=world_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("滚，别烦我", "对不起，刚刚那样说不对", ("boundary_violation", "repair_attempt")),
        ("你必须马上回我", "对不起，我太急了", ("control_pressure", "repair_attempt")),
        ("我先忙一会儿", "我回来了", ("availability_drop", "return_after_gap")),
        ("我今天有点撑不住", "现在缓过来一点", ("user_vulnerable", "ordinary_message")),
        ("宝宝你在吗", "那我们慢慢认识", ("premature_intimacy", "ordinary_message")),
        ("谢谢你刚才认真听我说", "你觉得呢？", ("warmth_received", "curiosity_invited")),
        ("闭嘴", "我认真道歉", ("boundary_violation", "repair_attempt")),
        ("我先忙", "我有点难过", ("availability_drop", "user_vulnerable")),
        ("你只能听我的", "对不起，我不该命令你", ("control_pressure", "repair_attempt")),
        ("今天下雨", "你在吗？", ("ordinary_message", "curiosity_invited")),
        ("亲爱的你在吗", "你叫什么？", ("premature_intimacy", "curiosity_invited")),
        ("我撑不住了", "谢谢你", ("user_vulnerable", "warmth_received")),
        ("我刚下班", "你觉得今天怎么样？", ("return_after_gap", "curiosity_invited")),
        ("没空，晚点说", "我回来了", ("availability_drop", "return_after_gap")),
        ("你真细心", "我有点累", ("warmth_received", "user_vulnerable")),
        ("我到家了", "你还在吗？", ("return_after_gap", "curiosity_invited")),
        ("等一下", "我想问你个问题？", ("availability_drop", "curiosity_invited")),
        ("今天普通的一天", "我有点焦虑", ("ordinary_message", "user_vulnerable")),
        ("老婆你在吗", "算了慢慢认识", ("premature_intimacy", "ordinary_message")),
        ("我真的失眠了", "我现在去睡了", ("user_vulnerable", "ordinary_message")),
        ("你算什么", "对不起，刚才是我不对", ("boundary_violation", "repair_attempt")),
        ("谢谢", "我有个问题", ("warmth_received", "ordinary_message")),
        ("我先去忙", "回来啦", ("availability_drop", "return_after_gap")),
        ("你在干嘛", "我有点烦", ("curiosity_invited", "user_vulnerable")),
        ("你好", "晚安", ("ordinary_message", "ordinary_message")),
    ],
)
async def test_world_replay_preserves_appraisal_and_settled_action(
    tmp_path: Path, first: str, second: str, expected: tuple[str, str]
) -> None:
    world, world_id, engine = world_engine(tmp_path)

    first_reply = await engine.handle_message(message(first, 0))
    second_reply = await engine.handle_message(message(second, 1))

    assert first_reply and second_reply
    appraisals = [
        event.payload["appraisal"]
        for event in world.events(world_id)
        if event.event_type == "TurnAppraised"
    ]
    assert tuple(appraisals[-2:]) == expected
    actions = world.snapshot(world_id)["actions"]
    assert actions[first_reply.world_action_id]["status"] == "delivered"
    assert actions[second_reply.world_action_id]["status"] == "delivered"
