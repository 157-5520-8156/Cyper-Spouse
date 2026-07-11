from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import (
    CompanionEngine,
    _afterthought_repeats_recent,
    afterthought_prompt,
    relative_chat_time_hint,
    seed_user,
)
from companion_daemon.image_generation import GeneratedImage
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MessageAttachment, MoodState
from companion_daemon.multimodal_analysis import AttachmentInsight
from companion_daemon.stickers import StickerCatalog, Sticker
from companion_daemon.character import load_character
from companion_daemon.budget import BudgetGate
from companion_daemon.time import utc_now
from companion_daemon.models import LifeRuntimeState

TEST_PROMPT = "你是凛，用户的赛博女友。"


def test_afterthought_rejects_rephrased_repeat() -> None:
    assert _afterthought_repeats_recent(
        "哦对，刚才那句是复习间隙顺手敲的。",
        ["[qq][刚刚] 她: 哦对，你刚说要考试来着。那我不打扰你啦，好好考。"],
    )


def test_afterthought_prompt_keeps_speaker_ownership_and_does_not_invent_a_reply() -> None:
    prompt = afterthought_prompt(
        "quick_continue",
        ["[qq][刚刚] 你: 你是不是在跟别人聊天", "[qq][刚刚] 她: 没有啊，我刚在发呆。"],
    )

    assert "不能假装用户在这之后又说了一句" in prompt
    assert "我信你" in prompt


@pytest.mark.asyncio
async def test_handle_message_updates_mood_and_replies(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )

    assert reply.canonical_user_id == "geoff"
    assert reply.mood == "miss_you"
    assert "我在呢" in reply.text


@pytest.mark.asyncio
async def test_character_examples_are_not_replayed_as_fake_chat_history(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(
        store,
        model,
        load_character("configs/character.yaml").system_prompt(),
        character_profile=load_character("configs/character.yaml"),
    )

    await engine.handle_message(IncomingMessage(platform="qq", platform_user_id="geoff", text="你好"))

    prompt = model.calls[-1]
    assert not any(message["role"] == "user" and message["content"] == "你叫什么？" for message in prompt)


def test_self_fact_ledger_uses_canonical_facts_not_freeform_background(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    character = load_character("configs/character.yaml")
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        character.system_prompt(),
        character_profile=character,
    )

    facts = engine._self_fact_lines("geoff")

    assert any("没有可验证的宠物饲养经历" in fact for fact in facts)
    assert not any("成长背景" in fact for fact in facts)
    assert not any("书店门口" in fact for fact in facts)


def test_self_fact_ledger_excludes_legacy_model_authored_life_events(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    now = utc_now()
    store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="图书馆遇到一本奇怪的书名。",
        started_at=now,
        ends_at=now,
        status="completed",
        source="life_event:spontaneous_recall",
    )
    store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="看书时翻到一段有点好笑的注释。",
        started_at=now,
        ends_at=now,
        status="completed",
        source="life_runtime:incidental:test",
    )

    facts = engine._self_fact_lines("geoff")

    assert any("有点好笑的注释" in fact for fact in facts)
    assert not any("奇怪的书名" in fact for fact in facts)


def test_engine_wakes_for_the_next_message_after_persisting_an_unread_state(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = utc_now()
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="正在上课，手机放在包里",
            activity_kind="class",
            attention_demand=88,
            interruptible=False,
            started_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(minutes=35),
            phone_attention="away",
            updated_at=now,
        ),
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    first = engine.phone_attention_decision(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我到啦")
    )
    second = engine.phone_attention_decision(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我再说一句")
    )

    assert first.read_now is False
    assert store.get_mood_state("geoff").has_unread is True
    assert second.read_now is True


@pytest.mark.asyncio
async def test_normal_reply_runs_the_shared_output_safety_cleanup(tmp_path: Path) -> None:
    class LocationConfusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return "（手机震了一下）成都这边食堂倒没这么卷。"

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, LocationConfusedModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你们那边食堂怎么样")
    )

    assert reply is not None
    assert reply.text == "上海这边食堂倒没这么卷。"


@pytest.mark.asyncio
async def test_rewrite_uses_fact_ledger_to_remove_ungrounded_history(tmp_path: Path) -> None:
    class FactCheckingModel:
        def __init__(self):
            self.calls: list[list[dict[str, str]]] = []

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls.append(messages)
            if "她的消息：" in messages[0]["content"]:
                return "没有呢，不过 B 站吸猫倒是真的很快乐。"
            return "没有呢，小时候养过几条金鱼，最后都……你懂的。后来就只敢养植物了。不过 B 站吸猫倒是真的很快乐。"

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FactCheckingModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, rewrite_model=model)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你养过别的宠物吗"),
    )

    assert reply is not None
    assert "金鱼" not in reply.text
    assert "后来就只敢养植物" not in reply.text
    assert "B 站吸猫" in reply.text
    rewrite_prompt = model.calls[-1][0]["content"]
    assert "事实账本" in rewrite_prompt
    assert "可用知栀事实" in rewrite_prompt


@pytest.mark.asyncio
async def test_deferred_reply_only_enters_history_after_delivery_confirmation(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿"),
        defer_delivery=True,
    )

    assert reply.delivery_id is not None
    assert store.outbox_message(reply.delivery_id)["status"] == "planned"
    assert not any(row["direction"] == "out" for row in store.recent_messages("geoff"))

    engine.fail_reply_delivery(reply, "network failed")

    assert store.outbox_message(reply.delivery_id)["status"] == "failed"
    assert not any(row["direction"] == "out" for row in store.recent_messages("geoff"))


@pytest.mark.asyncio
async def test_failed_deferred_reply_creates_reconsider_task_without_writing_history(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    original = IncomingMessage(platform="qq", platform_user_id="geoff", text="你刚刚还没回我那个")
    task_id = engine.create_deferred_reply_task(original, defer_minutes=5, reason="unread_during_study")

    reply = await engine.handle_message(original, defer_delivery=True, context_hint="刚才因为在忙，隔了一会儿才回来。")
    engine.fail_reply_delivery(reply, "network failed", source_task_id=task_id)

    tasks = store.recent_social_tasks("geoff")
    assert store.outbox_message(reply.delivery_id)["status"] == "failed"
    assert not any(row["direction"] == "out" for row in store.recent_messages("geoff"))
    assert any(task["id"] == task_id and task["kind"] == "reply_later" and task["status"] == "cancelled" for task in tasks)
    retry = [task for task in tasks if task["kind"] == "reply_reconsider"]
    assert retry and retry[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_serious_apology_uses_single_repair_curve_and_still_records_key_event(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            mood="hurt",
            trust=30,
            security=30,
            emotional_charge=30,
            patience=40,
        ),
    )
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我认真道歉，以后我会注意")
    )

    state = store.get_mood_state("geoff")
    assert state.mood == "calm"
    assert state.security > 30
    assert state.patience > 40
    assert state.emotional_charge < 30
    assert state.last_interaction_event == "repair_attempt"
    assert any(
        row["kind"] == "key_relationship_event" and "认真修复" in row["content"]
        for row in store.memories("geoff")
    )
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "关键事件" in prompt_text


@pytest.mark.asyncio
async def test_key_relationship_event_reaches_the_reply_prompt(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你还记得那天说的桂花乌龙吗")
    )

    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "关键事件" in prompt_text
    assert any(row["kind"] == "key_relationship_event" for row in store.memories("geoff"))


def test_failed_proactive_delivery_creates_reconsider_task(tmp_path: Path) -> None:
    from companion_daemon.models import ProactiveDecision

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    decision = ProactiveDecision(
        canonical_user_id="geoff",
        private_thought="想跟他说一句",
        should_send=True,
        platform="qq",
        message_type="text",
        message="刚路过操场，突然想到你。",
        delivery_id=store.queue_outgoing("geoff", "qq", "刚路过操场，突然想到你。", kind="proactive"),
    )

    engine.fail_proactive_delivery(decision, "network down")

    assert store.outbox_message(decision.delivery_id)["status"] == "failed"
    tasks = store.recent_social_tasks("geoff")
    reconsider = [task for task in tasks if task["kind"] == "reply_reconsider"]
    assert reconsider and reconsider[0]["status"] == "pending"
    assert "主动消息" in reconsider[0]["reason"]
    assert not any(row["direction"] == "out" for row in store.recent_messages("geoff"))


def test_refresh_waiting_state_advances_waiting_psychology(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    # A proactive delivery four hours ago with no incoming reply since.
    with store.connect() as conn:
        conn.execute(
            "insert into proactive_delivery (canonical_user_id, platform, sent_at) values (?, ?, ?)",
            ("geoff", "qq", (utc_now() - timedelta(hours=4)).isoformat()),
        )

    state = engine.refresh_waiting_state("geoff")

    assert state.unresolved_emotion == "主动消息没等到回应，她会把分享欲收住，先回到自己的节奏。"
    assert store.get_mood_state("geoff").unresolved_emotion == state.unresolved_emotion


def test_long_unanswered_outgoing_streak_blocks_new_proactive_turns(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    store.save_outgoing("geoff", "qq", "刚刚想到一件小事。")
    store.save_outgoing("geoff", "qq", "再补一句。")
    with store.connect() as conn:
        conn.execute(
            "update messages set sent_at = ? where canonical_user_id = ? and direction = 'out'",
            ((utc_now() - timedelta(hours=2)).isoformat(), "geoff"),
        )

    assert engine.outreach_block_reason("geoff")


def test_waiting_clock_uses_first_outgoing_after_last_user_turn(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_incoming("geoff", IncomingMessage(platform="qq", platform_user_id="geoff", text="晚安"))
    store.save_outgoing("geoff", "qq", "晚安。")
    store.save_outgoing("geoff", "qq", "刚刚又想到一点。")
    with store.connect() as conn:
        conn.execute(
            "update messages set sent_at = ? where canonical_user_id = ? and direction = 'in'",
            ((utc_now() - timedelta(hours=14)).isoformat(), "geoff"),
        )
        rows = conn.execute(
            "select id from messages where canonical_user_id = ? and direction = 'out' order by id",
            ("geoff",),
        ).fetchall()
        conn.execute(
            "update messages set sent_at = ? where id = ?",
            ((utc_now() - timedelta(hours=13)).isoformat(), rows[0]["id"]),
        )
        conn.execute(
            "update messages set sent_at = ? where id = ?",
            ((utc_now() - timedelta(minutes=5)).isoformat(), rows[1]["id"]),
        )

    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    state = engine.refresh_waiting_state("geoff")

    assert state.initiative <= 8
    assert "不再追着补话" in (state.unresolved_emotion or "")


def test_debug_snapshot_includes_social_task_visibility(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    store.create_social_task(
        "geoff",
        kind="reply_later",
        platform="qq",
        platform_user_id="2759284998",
        payload={"text": "稍后回"},
        reason="unread_during_class",
        due_at=utc_now(),
        expires_at=utc_now() + timedelta(hours=1),
    )

    snapshot = engine.debug_snapshot("geoff")

    assert snapshot["recent_social_tasks"][0]["reason"] == "unread_during_class"
    assert snapshot["dashboard"]["active_task_count"] == 1
    assert snapshot["dashboard"]["next_plan"]
    assert snapshot["dashboard"]["scene"]["location"]
    assert snapshot["dashboard"]["scene"]["action"]


@pytest.mark.asyncio
async def test_rudeness_updates_persistent_impression_and_current_reply_policy(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你算什么，闭嘴")
    )

    state = store.get_mood_state("geoff")
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert state.perceived_respect < 50
    assert state.mood == "hurt"
    assert "明确表示不舒服" in prompt_text
    assert "最近感到不被尊重" in prompt_text


@pytest.mark.asyncio
async def test_skip_reply_can_avoid_unread_for_pure_ack(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="嗯嗯"),
        skip_reply=True,
        mark_unread=False,
    )

    assert reply is None
    assert store.get_mood_state("geoff").has_unread is False


@pytest.mark.asyncio
async def test_skip_reply_can_mark_unread_for_deferred_message(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我刚刚想了很久，" * 8),
        skip_reply=True,
        mark_unread=True,
    )

    assert reply is None
    assert store.get_mood_state("geoff").has_unread is True


@pytest.mark.asyncio
async def test_handle_message_injects_human_rhythm_context(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )

    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "生活节律" in prompt_text
    assert "上下文编排" in prompt_text
    assert "当前用户意图" in prompt_text
    assert "像手机私聊" in prompt_text
    assert any(row["kind"] == "life_continuity" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_reply_prompt_does_not_duplicate_current_user_message_in_recent_history(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_outgoing("geoff", "qq", "我刚从图书馆出来。")
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    unique_text = "单轮上下文排重测试XYZ"
    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text=unique_text)
    )

    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    recent_block = next(message["content"] for message in model.calls[-1] if message["content"].startswith("最近聊天:"))
    assert unique_text not in recent_block
    assert prompt_text.count(unique_text) >= 1
    assert "图书馆" in recent_block


def test_recent_lines_sanitize_previous_bad_outgoing(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    store.save_outgoing(
        "geoff",
        "qq",
        "成都理工啊，那你们学校后门是不是有条街全是串串和冰粉？我有个高中同学在那读土木，她跟我提过。",
    )

    recent = engine._recent_lines("geoff")

    assert "高中同学" not in "\n".join(recent)


def test_recent_lines_include_local_recency_hint(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    store.save_outgoing("geoff", "qq", "我刚从图书馆出来。")

    recent = engine._recent_lines("geoff")

    assert "[qq][" in recent[-1]
    assert "] 她:" in recent[-1]


def test_relative_chat_time_hint_uses_local_overnight_labels() -> None:
    now = datetime(2026, 7, 10, 10, 34, tzinfo=UTC)

    assert relative_chat_time_hint("2026-07-10T10:30:00+00:00", now=now) == "刚刚"
    assert relative_chat_time_hint("2026-07-10T09:50:00+00:00", now=now) == "刚才"
    assert relative_chat_time_hint("2026-07-09T19:40:00+00:00", now=now) == "今天凌晨"
    assert relative_chat_time_hint("2026-07-08T19:40:00+00:00", now=now) == "昨晚"


def test_debug_snapshot_exposes_daemon_context_and_prompt(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_outgoing("geoff", "qq", "我刚从图书馆出来。")
    store.upsert_memory(
        "geoff",
        kind="life_fact",
        content="用户人在成都",
        source="test",
        confidence=0.8,
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    snapshot = engine.debug_snapshot("geoff", preview_text="你在干嘛")

    assert snapshot["canonical_user_id"] == "geoff"
    assert "state" in snapshot
    assert any("图书馆" in line for line in snapshot["recent"])
    assert not any("成都" in line for line in snapshot["memories"])
    assert "context_package" in snapshot
    prompt_text = "\n".join(message["content"] for message in snapshot["preview_prompt"])
    assert "最近聊天" in prompt_text
    assert "上下文编排" in prompt_text
    assert "你在干嘛" in prompt_text


@pytest.mark.asyncio
async def test_handle_message_relaxes_after_warm_proactive_response(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            mood="miss_you",
            security=35,
            initiative=45,
            emotional_charge=18,
            last_platform="qq",
        ),
    )
    store.record_proactive_delivery("geoff", "qq")
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我在呀，刚刚忙完")
    )

    state = store.get_mood_state("geoff")
    assert state.security > 35
    assert state.initiative < 45
    assert state.emotional_charge < 18
    assert any(row["kind"] == "proactive_response" for row in store.memories("geoff"))
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "主动反馈" not in prompt_text
    assert "本轮回复策略" in prompt_text


@pytest.mark.asyncio
async def test_life_share_response_uses_the_same_feedback_loop_as_proactive_message(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(mood="miss_you", security=35, initiative=45))
    store.record_proactive_delivery("geoff", "qq:life_event")
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="哈哈，听起来也太离谱了")
    )

    state = store.get_mood_state("geoff")
    assert state.security > 35
    assert state.perceived_responsiveness > 50
    assert any(row["kind"] == "proactive_response" for row in store.memories("geoff"))


def test_confirm_life_event_delivery_feeds_back_into_life_runtime(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(initiative=45, emotional_charge=12))
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    engine.confirm_life_event_delivery("geoff")

    state = store.get_mood_state("geoff")
    assert store.last_initiated_delivery("geoff", "qq")
    assert state.initiative < 45
    assert store.get_life_runtime("geoff") is not None


@pytest.mark.asyncio
async def test_user_event_can_change_current_life_context(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我现在真的好难受")
    )

    runtime = store.get_life_runtime("geoff")
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert runtime.user_event_effect
    assert "用户事件余波" in prompt_text


@pytest.mark.asyncio
async def test_handle_message_notices_skipped_own_question(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_outgoing("geoff", "qq", "你刚刚是不是在忙？")
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我回来了")
    )

    state = store.get_mood_state("geoff")
    assert state.security < 45
    assert any(row["kind"] == "own_question_skipped" for row in store.memories("geoff"))
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "没有回答她刚刚问的问题" not in prompt_text
    assert "保留一点情绪" in prompt_text


@pytest.mark.asyncio
async def test_platform_switch_context_is_reported(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    store.map_account("wechat", "wechat-geoff", "geoff")
    await engine.handle_message(
        IncomingMessage(platform="wechat", platform_user_id="wechat-geoff", text="等我一下")
    )
    store.map_account("qq", "qq-geoff", "geoff")
    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="qq-geoff", text="我回来了")
    )

    assert reply.platform_context == "刚刚在 wechat 聊，现在切到了 qq。"


@pytest.mark.asyncio
async def test_user_self_image_claim_records_visual_anchor(tmp_path: Path) -> None:
    class FakeAnalyzer:
        async def analyze(self, attachment: MessageAttachment) -> AttachmentInsight:
            return AttachmentInsight("image", "图片内容：一张室内自拍，人物穿深色上衣。", 0.82)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, multimodal_analyzer=FakeAnalyzer())

    await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="这是我刚拍的自拍",
            attachments=[MessageAttachment(kind="image", url="https://example.test/me.jpg")],
        )
    )

    memories = store.memories("geoff", limit=20)
    assert any(row["kind"] == "user_visual_anchor" and "室内自拍" in row["content"] for row in memories)
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "视觉身份" in prompt_text


@pytest.mark.asyncio
async def test_proactive_tick_records_decision(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(initiative=60, emotional_charge=15))
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )
    outgoing_before = sum(
        row["direction"] == "out" for row in store.recent_messages("geoff", limit=20)
    )
    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is True
    assert decision.platform == "qq"
    assert decision.message
    assert decision.delivery_id is not None
    state = store.get_mood_state("geoff")
    assert store.outbox_message(decision.delivery_id)["status"] == "planned"
    assert sum(row["direction"] == "out" for row in store.recent_messages("geoff", limit=20)) == outgoing_before

    engine.confirm_proactive_delivery(decision)

    state = store.get_mood_state("geoff")
    assert state.initiative < 61
    assert state.emotional_charge < 15
    assert store.outbox_message(decision.delivery_id)["status"] == "delivered"


@pytest.mark.asyncio
async def test_proactive_tick_attaches_sticker_path(tmp_path: Path) -> None:
    class StickerModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"想发个表情但先不长篇说话",'
                    '"should_send":true,"platform":"qq","message_type":"sticker",'
                    '"message":null,"sticker_category":null,"cooldown_minutes":30}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    stickers = StickerCatalog(
        stickers=[
            Sticker(
                id="miss_you",
                category="miss_you",
                mood="miss_you",
                intent="reaching_out",
                path=Path("assets/stickers/rin-miss-you.png"),
            )
        ]
    )
    engine = CompanionEngine(store, StickerModel(), TEST_PROMPT, stickers)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )
    decision = await engine.proactive_tick("geoff")

    assert decision.message_type == "sticker"
    assert decision.sticker_path == "assets/stickers/rin-miss-you.png"


@pytest.mark.asyncio
async def test_proactive_tick_can_attach_self_initiated_image(tmp_path: Path) -> None:
    class ImageModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"路过图书馆窗边，突然想拍给你看",'
                    '"should_send":true,"platform":"qq","message_type":"text_image",'
                    '"message":"刚刚窗边光很好，突然想给你看一下。",'
                    '"sticker_category":null,"cooldown_minutes":45}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=68,
            intimacy=52,
            security=60,
            initiative=72,
            last_platform="qq",
        ),
    )
    image_generator = FakeImageGenerator()
    engine = CompanionEngine(
        store,
        ImageModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    decision = await engine.proactive_tick("geoff")

    assert decision.message_type == "text_image"
    assert decision.image_path
    assert Path(decision.image_path).exists()
    assert "virtual-life selfie-style" in image_generator.prompts[0]
    assert any(row["kind"] == "generated_image" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_proactive_tick_records_withheld_impulse(tmp_path: Path) -> None:
    class NoSendModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"有点想问他后来怎么样，但怕打扰",'
                    '"should_send":false,"platform":null,"message_type":"none",'
                    '"message":null,"sticker_category":null,"cooldown_minutes":30}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="friend",
            trust=50,
            intimacy=35,
            initiative=30,
            emotional_charge=4,
        ),
    )
    engine = CompanionEngine(store, NoSendModel(), TEST_PROMPT)
    await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="所以你觉得呢？",
            sent_at=utc_now() - timedelta(hours=1),
        )
    )
    before_tick = store.get_mood_state("geoff")

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    state = store.get_mood_state("geoff")
    assert state.initiative > before_tick.initiative
    assert state.emotional_charge > before_tick.emotional_charge
    assert any(row["kind"] == "withheld_proactive_impulse" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_attaches_ordinary_reply_sticker(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    stickers = StickerCatalog(
        stickers=[
            Sticker(
                id="comfort",
                category="comfort",
                mood="calm",
                intent="comfort",
                path=Path("assets/stickers/rin-comfort.png"),
            )
        ]
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, stickers)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天好累，有点难受")
    )

    assert reply.sticker_path == "assets/stickers/rin-comfort.png"


class FakeImageGenerator:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(
        self,
        prompt: str,
        *,
        output_path: Path,
        size: str = "1024x1024",
    ) -> GeneratedImage:
        self.prompts.append(prompt)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-png")
        return GeneratedImage(output_path, prompt)


@pytest.mark.asyncio
async def test_handle_message_generates_requested_image(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=70,
            intimacy=55,
            security=62,
        ),
    )
    image_generator = FakeImageGenerator()
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张水彩风格自拍看看")
    )

    assert reply.image_path
    assert Path(reply.image_path).exists()
    assert reply.sticker_path is None
    assert "Character identity anchor" in image_generator.prompts[0]
    assert any(row["kind"] == "generated_image" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_can_defer_early_selfie_request(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    image_generator = FakeImageGenerator()
    model = FakeCompanionModel()
    engine = CompanionEngine(
        store,
        model,
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张自拍看看")
    )

    assert reply.image_path is None
    assert image_generator.prompts == []
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "不要立刻发自拍" in prompt_text
    assert any(row["kind"] == "selfie_deferred" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_auto_image_generation_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=70,
            intimacy=55,
            security=62,
        ),
    )
    image_generator = FakeImageGenerator()
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=0.1,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        budget_gate=budget,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张自拍看看")
    )

    assert reply.image_path is None
    assert image_generator.prompts == []
    assert any(row["kind"] == "image_request_blocked" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_proactive_tick_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_incoming(
        "geoff",
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿"),
    )
    store.record_usage("vision", 1.0)
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=1.0,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, budget_gate=budget)

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    assert "预算阀门" in decision.private_thought
    assert model.calls == []


@pytest.mark.asyncio
async def test_afterthought_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(mood="miss_you"))
    store.save_incoming(
        "geoff",
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我刚才心里有点闷"),
    )
    store.save_outgoing("geoff", "qq", "我在听。")
    store.record_usage("vision", 1.0)
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=1.0,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, budget_gate=budget)

    text = await engine.generate_afterthought("geoff", utc_now())

    assert text is None
    assert model.calls == []
    assert any(row["kind"] == "afterthought_blocked" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_memory_maintenance_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    for index in range(22):
        store.save_incoming(
            "geoff",
            IncomingMessage(platform="qq", platform_user_id="geoff", text=f"消息{index}"),
        )
    for index in range(5):
        store.upsert_memory("geoff", kind="life_fact", content=f"用户事实{index}", source="test", confidence=0.7)
    store.record_usage("vision", 1.0)
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=1.0,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, budget_gate=budget)

    await engine._maybe_consolidate("geoff", MoodState())

    assert model.calls == []
    assert any(row["kind"] == "memory_maintenance_blocked" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_records_tool_request_without_executing(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="帮我打开浏览器看一下这个网页")
    )

    proposals = store.recent_tool_proposals("geoff")
    assert proposals
    assert proposals[-1]["kind"] == "computer_assist"
    sent_prompt = "\n".join(message["content"] for message in model.calls[-1])
    assert "必须先请求用户明确确认" in sent_prompt
