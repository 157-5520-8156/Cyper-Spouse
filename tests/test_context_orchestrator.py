import pytest

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
