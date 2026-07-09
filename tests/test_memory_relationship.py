from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.memory import extract_memories
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.relationship import (
    life_event_probability,
    proactive_cooldown_minutes,
    stage_for_scores,
)


TEST_PROMPT = "你是沈知栀。"


def test_extracts_user_memories() -> None:
    memories = extract_memories(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我叫小周，我喜欢桂花乌龙")
    )

    assert ("name", "小周") in {(memory.kind, memory.content) for memory in memories}
    assert ("preference", "桂花乌龙") in {
        (memory.kind, memory.content) for memory in memories
    }


def test_relationship_stage_advances_conservatively() -> None:
    assert stage_for_scores(5, 15, 1) == "stranger"
    assert stage_for_scores(20, 30, 12) == "friend"
    assert stage_for_scores(80, 80, 130) == "lover"


def test_relationship_state_drives_proactive_policy() -> None:
    stranger = MoodState()
    lover = MoodState(
        mood="miss_you",
        intimacy=85,
        trust=80,
        attachment=70,
        relationship_stage="lover",
    )

    assert proactive_cooldown_minutes(lover, 45) < proactive_cooldown_minutes(stranger, 45)
    assert life_event_probability(lover) > life_event_probability(stranger)


@pytest.mark.asyncio
async def test_engine_persists_memory_and_relationship_state(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我叫小周，我喜欢桂花乌龙")
    )

    rows = store.memories("geoff")
    assert any(row["kind"] == "name" and row["content"] == "小周" for row in rows)
    assert store.get_mood_state("geoff").relationship_stage == "stranger"
