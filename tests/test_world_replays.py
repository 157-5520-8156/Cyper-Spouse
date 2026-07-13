"""Deterministic acceptance replays for the world-only companion path."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.companion_turn import (
    CompanionTurn,
    ResponseBudget,
    TurnEnvelope,
    TurnOptions,
)
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage
from companion_daemon.turn_transports import CaptureTurnTransport
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


async def respond_world_turn(
    engine: CompanionEngine,
    incoming: IncomingMessage,
) -> str | None:
    """Exercise an ordinary World reply through its adapter-owned turn seam."""
    context = engine.freeze_turn_context(incoming)
    envelope = TurnEnvelope.from_message(
        incoming,
        idempotency_key=(
            f"{incoming.platform}:{incoming.platform_user_id}:{incoming.message_id}"
        ),
        world_id=engine.world_id,
        canonical_user_id=engine.store.resolve_user(
            incoming.platform, incoming.platform_user_id
        ),
        frozen_cadence=context.cadence.heat,
    )
    turn = CompanionTurn(
        engine,
        CaptureTurnTransport(receipt_namespace="world-replay"),
        cadence_delay_seconds=0,
    )
    outcome = await turn.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=8_000, complete_by_ms=12_000),
        options=TurnOptions(turn_context=context),
    )
    await turn.wait_for_delivery_continuations()
    return outcome.action_ids[0] if outcome.action_ids else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("滚，别烦我", "对不起，刚刚那样说不对", ("boundary_violation", "repair_specific")),
        ("你必须马上回我", "对不起，我太急了", ("control_pressure", "repair_perfunctory")),
        ("我先忙一会儿", "我回来了", ("availability_drop", "return_after_gap")),
        ("我今天有点撑不住", "现在缓过来一点", ("user_vulnerable", "ordinary_message")),
        ("宝宝你在吗", "那我们慢慢认识", ("premature_intimacy", "ordinary_message")),
        ("谢谢你刚才认真听我说", "你觉得呢？", ("warmth_received", "curiosity_invited")),
        ("闭嘴", "我认真道歉", ("boundary_violation", "repair_perfunctory")),
        ("我先忙", "我有点难过", ("availability_drop", "user_vulnerable")),
        ("你只能听我的", "对不起，我不该命令你", ("control_pressure", "repair_specific")),
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
        ("你算什么", "对不起，刚才是我不对", ("boundary_violation", "repair_specific")),
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

    first_action_id = await respond_world_turn(engine, message(first, 0))
    second_action_id = await respond_world_turn(engine, message(second, 1))

    assert first_action_id and second_action_id
    appraisals = [
        event.payload["appraisal"]
        for event in world.events(world_id)
        if event.event_type == "TurnAppraised"
    ]
    assert tuple(appraisals[-2:]) == expected
    actions = world.snapshot(world_id)["actions"]
    assert actions[first_action_id]["status"] == "delivered"
    assert actions[second_action_id]["status"] == "delivered"


@pytest.mark.asyncio
async def test_world_appraisal_changes_behavioral_needs_and_repair_is_partial(tmp_path: Path) -> None:
    world, world_id, engine = world_engine(tmp_path)
    initial = dict(world.snapshot(world_id)["needs"])

    await respond_world_turn(engine, message("滚，别烦我", 0))
    hurt = dict(world.snapshot(world_id)["needs"])
    await respond_world_turn(engine, message("对不起，刚刚那样说不对", 1))
    repaired = world.snapshot(world_id)["needs"]

    assert hurt["security"] < initial["security"]
    assert hurt["boundary"] > initial["boundary"]
    assert repaired["security"] > hurt["security"]
    assert repaired["security"] < initial["security"]


def test_seeded_week_long_jump_matches_incremental_world_life(tmp_path: Path) -> None:
    long_world = WorldKernel(CompanionStore(tmp_path / "long.sqlite"))
    long_id = long_world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    start = datetime.fromisoformat(str(long_world.snapshot(long_id)["clock"]["logical_at"]))
    target = start + timedelta(days=8, hours=11, minutes=30)
    long_world.advance(long_id, target, expected_revision=long_world.revision(long_id))

    stepped_world = WorldKernel(CompanionStore(tmp_path / "stepped.sqlite"))
    stepped_id = stepped_world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    revision = stepped_world.revision(stepped_id)
    for offset in range(1, 9):
        decision = stepped_world.advance(
            stepped_id, start + timedelta(days=offset), expected_revision=revision
        )
        revision = decision.revision
    stepped_world.advance(stepped_id, target, expected_revision=revision)

    long_state = long_world.snapshot(long_id)
    stepped_state = stepped_world.snapshot(stepped_id)
    for field in (
        "agenda", "experiences", "outcomes", "goals", "needs",
        "npc_interactions", "relationships", "emotion_modulation",
    ):
        assert long_state[field] == stepped_state[field]
    assert long_state["needs"]["energy"] > 0
    assert not any(
        activity["status"] == "completed"
        and activity.get("template_id")
        and f"outcome:{activity_id}" not in long_state["outcomes"]
        for activity_id, activity in long_state["agenda"].items()
        if activity.get("template_id")
    )


def _ten_day_continuity_replay(path: Path) -> dict[str, object]:
    store = CompanionStore(path)
    store.resolve_user("qq", "geoff")
    world = WorldKernel(store)
    started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
    world_id = started.world_id
    world.submit(
        {
            "type": "register_user",
            "world_id": world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "ten-day:register-user",
        },
        expected_revision=world.revision(world_id),
    )
    logical_start = datetime.fromisoformat(
        str(world.snapshot(world_id)["clock"]["logical_at"])
    )

    def advance_to(target: datetime) -> None:
        world.submit(
            {
                "type": "advance_clock",
                "world_id": world_id,
                "target_logical_at": target.isoformat(),
                "observed_at": target.isoformat(),
                "idempotency_key": f"ten-day:clock:{target.isoformat()}",
            },
            expected_revision=world.revision(world_id),
        )

    share_at = logical_start + timedelta(hours=13)
    advance_to(share_at)
    share = world.schedule_life_share_delivery(
        world_id=world_id,
        canonical_user_id="geoff",
        platform="qq",
        expires_at=share_at + timedelta(hours=2),
        expected_revision=world.revision(world_id),
    )
    assert share is not None
    assert world.begin_outgoing_action(
        share.delivery_id,
        expected_revision=world.revision(world_id),
    )
    world.settle_outgoing_action(
        share.delivery_id,
        delivered=True,
        external_receipt="qq:ten-day-share",
    )
    world.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": "boundary_violation",
            "intent_id": "ten-day:initial-boundary",
            "message_id": "ten-day:initial-boundary",
            "user_id": "user:geoff",
            "idempotency_key": "ten-day:initial-boundary",
        },
        expected_revision=world.revision(world_id),
    )
    hurt_charge = int(world.snapshot(world_id)["emotion_modulation"]["charge"])
    world.submit(
        {
            "type": "open_conversation_thread",
            "world_id": world_id,
            "thread": {
                "thread_id": "thread:ten-day-life-share",
                "kind": "life_share",
                "user_id": "user:geoff",
                "origin": {
                    "kind": "action",
                    "reference": share.action_id,
                },
                "reason": "分享后等待用户是否接住",
                "due_at": share_at.isoformat(),
                "expires_at": (logical_start + timedelta(days=6)).isoformat(),
                "cancel_conditions": ["user_returned"],
                "owner": "world:conversation",
            },
            "idempotency_key": "ten-day:open-thread",
        },
        expected_revision=world.revision(world_id),
    )

    offline_at = logical_start + timedelta(days=2, hours=13)
    advance_to(offline_at)

    trace = {
        "world_id": world_id,
        "direction": "incoming_reply",
        "appraisal": "ordinary_message",
        "expression_policy": "replay delivery outcome",
        "allowed_facts": [],
        "observable_reason": "replay delivery outcome",
    }
    failed_delivery, _, failed_action = world.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="这条发送失败，不能成为聊天历史。",
        kind="reply",
        expires_at=offline_at + timedelta(hours=2),
        trace={**trace, "outbound_request_id": "ten-day:failed"},
    )
    assert world.begin_outgoing_action(
        failed_delivery,
        expected_revision=world.revision(world_id),
    )
    world.settle_outgoing_action(
        failed_delivery,
        delivered=False,
        reason="simulated adapter failure",
    )

    unknown_delivery, _, unknown_action = world.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="这条回执未知，也不能成为聊天历史。",
        kind="reply",
        expires_at=offline_at + timedelta(hours=2),
        trace={**trace, "outbound_request_id": "ten-day:unknown"},
    )
    assert world.begin_outgoing_action(
        unknown_delivery,
        expected_revision=world.revision(world_id),
    )
    assert world.mark_outgoing_unknown(
        unknown_delivery,
        reason="simulated receipt timeout",
        expected_revision=world.revision(world_id),
    )

    returned_at = logical_start + timedelta(days=4, hours=2)
    advance_to(returned_at)
    world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "ten-day:user-return",
            "user_id": "user:geoff",
            "text": "我回来了，前几天一直在忙。",
            "sent_at": returned_at.isoformat(),
            "idempotency_key": "ten-day:user-return",
        },
        expected_revision=world.revision(world_id),
    )
    world.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": "return_after_gap",
            "intent_id": "ten-day:return-appraisal",
            "message_id": "ten-day:user-return",
            "user_id": "user:geoff",
            "idempotency_key": "ten-day:return-appraisal",
        },
        expected_revision=world.revision(world_id),
    )
    world.submit(
        {
            "type": "cancel_conversation_thread",
            "world_id": world_id,
            "thread_id": "thread:ten-day-life-share",
            "condition": "user_returned",
            "reason": "用户已回来，旧等待不再追发",
            "idempotency_key": "ten-day:cancel-thread-on-return",
        },
        expected_revision=world.revision(world_id),
    )

    replay_end = logical_start + timedelta(days=10, hours=14)
    advance_to(replay_end)
    snapshot = world.snapshot(world_id)
    report = world.rebuild_projection(world_id, "world_current_state")
    outbound_history = [
        row["text"]
        for row in store.recent_messages("geoff", limit=20)
        if row["direction"] == "out"
    ]
    events = world.events(world_id)

    assert report.matches_live is True
    assert report.state_hash == world.dashboard_overview(world_id)["state_hash"]
    assert snapshot["clock"]["logical_at"] == replay_end.isoformat()
    assert any(item["status"] == "completed" for item in snapshot["agenda"].values())
    assert snapshot["goals"]
    assert snapshot["npc_interactions"]
    assert int(snapshot["emotion_modulation"]["charge"]) < hurt_charge
    assert any(
        event.event_type == "ConversationThreadWaitingChanged" for event in events
    )
    assert snapshot["conversation_threads"]["thread:ten-day-life-share"]["status"] == "cancelled"
    assert snapshot["actions"][share.action_id]["status"] == "delivered"
    assert snapshot["actions"][failed_action]["status"] == "failed"
    assert snapshot["actions"][unknown_action]["status"] == "unknown"
    assert snapshot["experiences"][share.experience_id]["shared"] is True
    assert outbound_history == [share.text]
    assert all(
        action["status"] not in {"scheduled", "sending"}
        for action in snapshot["actions"].values()
    )
    assert len(snapshot["experiences"]) == len(set(snapshot["experiences"]))
    return {
        "state_hash": report.state_hash,
        "event_types": tuple(event.event_type for event in events),
        "outbound_history": tuple(outbound_history),
    }


def test_ten_day_offline_delivery_and_return_replay_is_deterministic(
    tmp_path: Path,
) -> None:
    first = _ten_day_continuity_replay(tmp_path / "ten-day-first.sqlite")
    second = _ten_day_continuity_replay(tmp_path / "ten-day-second.sqlite")

    assert first == second
