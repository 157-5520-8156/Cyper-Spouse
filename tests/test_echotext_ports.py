from pathlib import Path
import random

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.context_emotion_bleed import ContextMessage, context_emotion_deltas
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


def test_context_emotion_bleed_is_capped() -> None:
    deltas = context_emotion_deltas(
        [
            ContextMessage("我真的很开心很开心很开心，谢谢你！！", is_user=True),
            ContextMessage("我也很开心，也很信任你。", is_user=False),
        ]
    )

    assert deltas
    assert max(abs(value) for value in deltas.values()) <= 2.0
    assert sum(abs(value) for value in deltas.values()) <= 5.0


def test_emotion_reply_timing_ghosts_more_when_cold() -> None:
    warm = emotion_reply_timing(MoodState(emotion_vector={"joy": 70, "trust": 70}), rng=random.Random(1))
    cold = emotion_reply_timing(
        MoodState(emotion_vector={"anger": 85, "sadness": 70, "fear": 60}),
        rng=random.Random(1),
    )

    assert cold.read_delay_ms > warm.read_delay_ms
    assert cold.ghost_delay_ms > 0


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
