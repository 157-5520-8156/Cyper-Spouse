from pathlib import Path
import random

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.context_orchestrator import select_relevant_memories
from companion_daemon.character import CharacterProfile
from companion_daemon.emotion_personality import (
    extract_mbti,
    initial_mood_for_character,
    personality_baseline,
)
from companion_daemon.emotion_reactions import select_character_reaction
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.image_requests import detect_image_request, detect_style_tags
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.memory import detect_memory_candidates, extract_memories
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.reply_timing import emotion_reply_timing


def test_memory_candidates_detect_life_fact_and_favorite() -> None:
    candidates = detect_memory_candidates("我人在成都，最近特别喜欢桂花乌龙。")

    assert any(candidate.kind == "custom" and "成都" in candidate.text for candidate in candidates)
    assert any(candidate.kind == "favorite_thing" and "桂花乌龙" in candidate.text for candidate in candidates)


def test_extract_memories_includes_echotext_style_candidates() -> None:
    memories = extract_memories(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我明天准备去玉林路那边走走")
    )

    assert any(memory.kind == "shared_moment" and "明天" in memory.content for memory in memories)


def test_memory_candidates_ignore_pronoun_question_noise() -> None:
    memories = extract_memories(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你刚刚问我啥了吗")
    )

    assert not any(memory.kind == "person" for memory in memories)


def test_extract_memories_does_not_turn_today_emotion_into_schedule() -> None:
    memories = extract_memories(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天心里有点闷")
    )

    assert not any(memory.kind == "schedule" for memory in memories)


def test_context_retrieval_injects_small_topic_relevant_subset(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    for kind, content, confidence in [
        ("shared_moment", "用户昨天散步", 0.55),
        ("favorite_thing", "用户喜欢桂花乌龙", 0.68),
        ("life_fact", "用户人在成都", 0.7),
        ("custom", "普通碎片", 0.4),
        ("person", "用户提到室友", 0.65),
    ]:
        store.upsert_memory("geoff", kind=kind, content=content, source=content, confidence=confidence)

    lines = select_relevant_memories(store.memories("geoff", limit=10), "成都的桂花乌龙", [], max_memories=3)

    assert len(lines) <= 3
    assert any("成都" in line for line in lines)
    assert any("桂花乌龙" in line for line in lines)


def test_context_retrieval_excludes_runtime_impulses_from_reply_prompt(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    for kind, content, confidence in [
        ("withheld_proactive_impulse", "念头=你昨天说声音好听那个梦", 0.9),
        ("own_question_skipped", "用户跳过了她的问题：错误归因", 0.9),
        ("tone_inertia", "last_outgoing_tone=teasing", 0.9),
        ("life_continuity", "phase=morning_focus; activity=自习", 0.9),
        ("life_fact", "用户人在成都", 0.7),
    ]:
        store.upsert_memory("geoff", kind=kind, content=content, source=content, confidence=confidence)

    lines = select_relevant_memories(store.memories("geoff", limit=10), "成都", [], max_memories=3)

    assert lines == ["- [life_fact] 用户人在成都"]


def test_select_character_reaction_matches_emotional_delta() -> None:
    reaction = select_character_reaction(
        "谢谢你还记得，我真的很开心！！",
        MoodState(emotion_vector={"joy": 55, "trust": 50}),
    )

    assert reaction
    assert reaction.reaction_id in {"like", "star", "heart", "haha"}
    assert reaction.probability >= 0.25


def test_detect_image_request_direct_and_offer_response() -> None:
    direct = detect_image_request("给我发一张自拍看看")
    offered = detect_image_request("好呀，发吧", ["要不要看我刚拍的生活照？"])

    assert direct.triggered
    assert direct.type == "direct_request"
    assert offered.triggered
    assert offered.type == "offer_response"


def test_image_request_detects_style_tags() -> None:
    request = detect_image_request("给我发一张水彩风格自拍看看")

    assert request.triggered
    assert "watercolor" in request.style_tags
    assert "pixel art" in detect_style_tags("像素风格头像")


def test_emotion_reply_timing_ghosts_more_when_cold() -> None:
    warm = emotion_reply_timing(MoodState(emotion_vector={"joy": 70, "trust": 70}), rng=random.Random(1))
    cold = emotion_reply_timing(
        MoodState(emotion_vector={"anger": 85, "sadness": 70, "fear": 60}),
        rng=random.Random(1),
    )

    assert cold.read_delay_ms > warm.read_delay_ms
    assert cold.ghost_delay_ms > 0


def test_personality_baseline_uses_explicit_mbti() -> None:
    character = CharacterProfile(
        base_prompt="你是沈知栀。",
        identity={"mbti": "INFJ，但她不太喜欢把人简单归类"},
    )

    baseline = personality_baseline(character)

    assert extract_mbti(character.identity["mbti"]) == "INFJ"
    assert baseline["love"] > 10
    assert baseline["trust"] > 20
    assert baseline["joy"] < 25
    assert baseline["anticipation"] > 25


def test_seed_user_preserves_existing_mood_state(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    store.save_mood_state(
        "geoff",
        MoodState(mood="hurt", trust=12, emotion_vector={"anger": 72, "sadness": 45}),
    )

    seed_user(
        store,
        initial_state=initial_mood_for_character(
            CharacterProfile(base_prompt="你是沈知栀。", identity={"mbti": "INFJ"})
        ),
    )

    state = store.get_mood_state("geoff")
    assert state.mood == "hurt"
    assert state.trust == 12
    assert state.emotion_vector["anger"] == 72


@pytest.mark.asyncio
async def test_engine_injects_image_request_context(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, "你是沈知栀。")

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张自拍看看")
    )

    sent_prompt = "\n".join(message["content"] for message in model.calls[-1])
    assert "图片请求" in sent_prompt
    assert any(row["kind"] == "image_request" for row in store.memories("geoff"))
