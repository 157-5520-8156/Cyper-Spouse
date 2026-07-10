import pytest

from datetime import UTC, datetime, timedelta

from companion_daemon.context_orchestrator import (
    build_context_package,
    forbidden_old_topics,
    infer_user_intent,
    select_relevant_memories,
)
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MoodState


def test_infer_user_intent_identifies_emotional_message() -> None:
    assert infer_user_intent("今天真的好累，有点不开心") == "表达情绪，需要先被接住"


def test_select_relevant_memories_prioritizes_current_topic() -> None:
    class Row(dict):
        def keys(self):
            return super().keys()

    rows = [
        Row(kind="favorite_thing", content="用户喜欢桂花乌龙", confidence=0.7),
        Row(kind="life_fact", content="用户人在成都", confidence=0.9),
        Row(kind="recent_event", content="用户最近在准备考试", confidence=0.72),
        Row(kind="tone_inertia", content="last_outgoing_tone=teasing", confidence=0.95),
    ]

    lines = select_relevant_memories(
        rows,
        "我今天复习到脑子发木，考试快来了",
        [],
        max_memories=2,
    )

    assert any("考试" in line for line in lines)
    assert not any("tone_inertia" in line for line in lines)


def test_memory_retrieval_does_not_pad_an_exam_reply_with_unrelated_profile_facts() -> None:
    rows = [
        {"kind": "life_fact", "content": "用户人在成都", "confidence": 0.95},
        {"kind": "favorite_thing", "content": "用户喜欢桂花乌龙", "confidence": 0.9},
        {"kind": "recent_event", "content": "用户最近在准备毛概考试", "confidence": 0.72},
    ]

    lines = select_relevant_memories(rows, "毛概背得我头都大了", [])

    assert any("毛概" in line for line in lines)
    assert not any("成都" in line for line in lines)
    assert not any("桂花乌龙" in line for line in lines)


def test_memory_retrieval_respects_user_explicitly_setting_a_topic_aside() -> None:
    lines = select_relevant_memories(
        [{"kind": "recent_event", "content": "用户最近在准备毛概考试", "confidence": 0.9}],
        "先不说考试了，我今天心里有点闷",
        [],
    )

    assert not lines


def test_memory_retrieval_excludes_expired_schedule_like_facts() -> None:
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    rows = [
        {
            "kind": "schedule",
            "content": "明天要考毛概",
            "confidence": 0.9,
            "updated_at": (now - timedelta(days=3)).isoformat(),
        },
        {
            "kind": "recent_event",
            "content": "用户这周在复习毛概",
            "confidence": 0.75,
            "updated_at": (now - timedelta(hours=4)).isoformat(),
        },
    ]

    lines = select_relevant_memories(rows, "毛概怎么样", [], now=now)

    assert any("这周在复习" in line for line in lines)
    assert not any("明天要考" in line for line in lines)


def test_memory_retrieval_can_recall_an_old_shared_moment_when_the_topic_returns() -> None:
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    rows = [
        {
            "kind": "shared_moment",
            "content": "你们第一次聊到成都夜路时都没舍得睡",
            "confidence": 0.88,
            "updated_at": (now - timedelta(days=90)).isoformat(),
        },
    ]

    lines = select_relevant_memories(rows, "我刚刚又想起成都夜路", [], now=now)

    assert any("成都夜路" in line for line in lines)


def test_memory_retrieval_keeps_only_the_newest_conflicting_location_fact() -> None:
    now = datetime(2026, 7, 10, 12, tzinfo=UTC)
    rows = [
        {
            "kind": "life_fact",
            "content": "用户人在成都",
            "confidence": 0.9,
            "updated_at": (now - timedelta(days=60)).isoformat(),
        },
        {
            "kind": "life_fact",
            "content": "用户现在住在上海",
            "confidence": 0.8,
            "updated_at": (now - timedelta(hours=2)).isoformat(),
        },
    ]

    lines = select_relevant_memories(rows, "上海这两天热不热", [], now=now)

    assert any("上海" in line for line in lines)
    assert not any("成都" in line for line in lines)


def test_forbidden_old_topics_warns_about_assistant_question() -> None:
    forbidden = forbidden_old_topics(
        [
            {"direction": "in", "platform": "qq", "text": "我先忙", "sent_at": "2026-07-10T00:00:00+00:00"},
            {"direction": "out", "platform": "qq", "text": "你等下还回来吗？", "sent_at": "2026-07-10T00:01:00+00:00"},
        ]
    )

    assert any("不要追讨" in item for item in forbidden)


def test_context_package_contains_required_sections() -> None:
    package = build_context_package(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天真的好累"),
        MoodState(mood="worried", emotional_charge=25, relationship_stage="acquaintance"),
        [],
        [],
    )
    block = package.prompt_block()

    assert "当前用户意图" in block
    assert "本轮接话焦点" in block
    assert "禁止误用的旧话" in block
    assert "相关长期记忆" in block
    assert "她自己的当前生活状态" in block
    assert "情绪/关系影响" in block
    assert "最终 prompt 摘要" in block


def test_context_package_uses_compact_behavioral_policy_instead_of_state_monologue() -> None:
    package = build_context_package(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你怎么啦"),
        MoodState(
            mood="sulking",
            emotional_charge=45,
            boundary_level=20,
            unresolved_emotion="刚才的话有点刺人",
        ),
        [],
        [],
    )

    assert package.reply_policy == "先回答当前问句；保留一点情绪，但不翻旧账、不演独白"
    assert "小别扭" not in package.prompt_block()
    assert "情绪余波" in package.prompt_block()


def test_context_package_turns_a_boundary_violation_into_current_reply_behavior() -> None:
    package = build_context_package(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你算什么，闭嘴"),
        MoodState(
            mood="hurt",
            last_interaction_event="boundary_violation",
            reply_style_hint="短、冷静、有边界；不要讨好，不要撒娇。",
        ),
        [],
        [],
    )

    assert "明确表示不舒服" in package.reply_policy
    assert "不要讨好" in package.reply_policy


def test_context_package_exposes_continuity_and_subtext_as_compact_constraints() -> None:
    package = build_context_package(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我回来了"),
        MoodState(mood="sulking", security=35),
        [],
        [],
        continuity_hint="从 afternoon_classes 转到 evening_unwind；刚刚语气偏克制。",
        subtext_hint="想被认真对待，但嘴上会硬一点。",
    )

    block = package.prompt_block()
    assert "afternoon_classes 转到 evening_unwind" in block
    assert "想被认真对待" in block


@pytest.mark.asyncio
async def test_live_prompt_context_focuses_current_turn_in_multi_turn_eval(tmp_path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, "你是沈知栀。")

    await engine.handle_message(IncomingMessage(platform="qq", platform_user_id="geoff", text="我明天考试，毛概"))
    await engine.handle_message(IncomingMessage(platform="qq", platform_user_id="geoff", text="先不说考试了，我今天心里有点闷"))

    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "上下文编排" in prompt_text
    assert "表达情绪，需要先被接住" in prompt_text
    assert "不要追讨她上一个问题" in prompt_text
    assert "心里有点闷" in prompt_text
