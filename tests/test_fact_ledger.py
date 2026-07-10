from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage


def test_fact_ledger_supersedes_current_value_without_deleting_history(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.record_fact_observation(
        "geoff",
        subject="user",
        predicate="life_fact",
        fact_key="location:current",
        value="我在成都",
        source="qq:one",
        confidence=0.8,
    )
    store.record_fact_observation(
        "geoff",
        subject="user",
        predicate="life_fact",
        fact_key="location:current",
        value="我现在住在上海",
        source="qq:two",
        confidence=0.9,
    )

    active = store.active_fact_lines("geoff")
    history = store.fact_history("geoff")

    assert any("我现在住在上海" in line for line in active)
    assert not any("我在成都" in line for line in active)
    assert any(row["value"] == "我在成都" and row["status"] == "superseded" for row in history)


@pytest.mark.asyncio
async def test_engine_records_only_explicit_user_facts_in_the_verified_block(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是沈知栀。")

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我在成都读书，我喜欢桂花乌龙")
    )
    snapshot = engine.debug_snapshot("geoff", preview_text="我在哪来着？")
    facts = snapshot["context_package"]["verified_user_fact_lines"]

    assert any("成都" in fact for fact in facts)
    assert any("桂花乌龙" in fact for fact in facts)
    assert "相关记忆线索（不单独构成事实）" in "\n".join(
        item["content"] for item in snapshot["preview_prompt"]
    )
