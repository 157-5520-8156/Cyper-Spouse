import json
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.memory_consolidation import (
    build_self_core,
    consolidate_memories,
    load_self_core,
    should_consolidate,
)
from companion_daemon.models import IncomingMessage, MoodState


class ConsolidationFakeModel(FakeCompanionModel):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        joined = "\n".join(m["content"] for m in messages)
        if "合并" in joined and "JSON" in joined:
            return json.dumps(
                [
                    {"kind": "consolidated", "content": "用户在成都理工大学读书"},
                    {"kind": "consolidated", "content": "用户读过《我与地坛》，喜欢散文"},
                ],
                ensure_ascii=False,
            )
        if "自我认知" in joined:
            return (
                "我叫沈知栀，通过读书群认识了用户。\n"
                "---\n他在成都。\n"
                "---\n刚认识，聊过书和天气。\n"
                "---\n不知道他的真名和具体专业。\n"
                "---\n他上次说考试快到了"
            )
        return await super().complete(messages, temperature=temperature)


def test_should_consolidate_new_user_returns_false(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    assert not should_consolidate(store, "geoff")


def test_should_consolidate_after_threshold(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    for i in range(25):
        store.save_incoming(
            "geoff",
            IncomingMessage(platform="qq", platform_user_id="geoff", text=f"消息{i}"),
        )
    assert should_consolidate(store, "geoff")


@pytest.mark.asyncio
async def test_consolidate_memories_merges_entries(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.upsert_memory("geoff", kind="life_fact", content="用户在成都", source="test", confidence=0.7)
    store.upsert_memory("geoff", kind="life_fact", content="用户读计算机专业", source="test", confidence=0.7)
    store.upsert_memory("geoff", kind="favorite_thing", content="用户读过《我与地坛》", source="test", confidence=0.7)
    store.upsert_memory("geoff", kind="hobby", content="用户喜欢深夜听歌", source="test", confidence=0.6)
    store.upsert_memory("geoff", kind="recent_event", content="用户明天考试", source="test", confidence=0.65)
    model = ConsolidationFakeModel()
    count = await consolidate_memories(store, model, "geoff")
    assert count >= 2
    consolidated = [
        r for r in store.memories("geoff", limit=50)
        if r["kind"] == "consolidated"
    ]
    assert len(consolidated) >= 2


@pytest.mark.asyncio
async def test_build_self_core_creates_entry(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.upsert_memory("geoff", kind="life_fact", content="用户在成都", source="test", confidence=0.7)
    store.upsert_memory("geoff", kind="favorite_thing", content="用户读过《我与地坛》", source="test", confidence=0.7)
    store.upsert_memory("geoff", kind="recent_event", content="用户明天考试", source="test", confidence=0.65)
    model = ConsolidationFakeModel()
    core = await build_self_core(store, model, "geoff", MoodState())
    assert core is not None
    assert "沈知栀" in core.identity
    assert "成都" in core.user_profile
    loaded = load_self_core(store, "geoff")
    assert loaded is not None
    assert "沈知栀" in loaded.identity


def test_load_self_core_returns_none_when_empty(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    assert load_self_core(store, "geoff") is None


@pytest.mark.asyncio
async def test_consolidate_memories_rejects_non_json_output(tmp_path: Path) -> None:
    class BadJsonModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            return "我整理好了：用户在成都。"

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    for index in range(5):
        store.upsert_memory("geoff", kind="life_fact", content=f"用户事实{index}", source="test", confidence=0.7)

    count = await consolidate_memories(store, BadJsonModel(), "geoff")

    assert count == 0
    assert not [r for r in store.memories("geoff", limit=50) if r["kind"] == "consolidated"]


@pytest.mark.asyncio
async def test_build_self_core_rejects_unsupported_specifics(tmp_path: Path) -> None:
    class HallucinatedCoreModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            return (
                "我叫沈知栀。\n"
                "---\n他在成都，好像在读理工类大学。\n"
                "---\n刚认识。\n"
                "---\n不知道他的真名。\n"
                "---\n"
            )

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.upsert_memory("geoff", kind="life_fact", content="用户在成都", source="test", confidence=0.7)
    store.upsert_memory("geoff", kind="favorite_thing", content="用户读过《我与地坛》", source="test", confidence=0.7)
    store.upsert_memory("geoff", kind="recent_event", content="用户明天考试", source="test", confidence=0.65)

    core = await build_self_core(store, HallucinatedCoreModel(), "geoff", MoodState())

    assert core is not None
    assert "理工" not in core.user_profile
    loaded = load_self_core(store, "geoff")
    assert loaded is not None
    assert "角色档案" in loaded.identity


@pytest.mark.asyncio
async def test_build_self_core_persists_a_minimal_grounded_fallback_for_sparse_memory(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.upsert_memory("geoff", kind="life_fact", content="用户在成都", source="test", confidence=0.8)

    core = await build_self_core(store, FakeCompanionModel(), "geoff", MoodState())

    assert core is not None
    assert "用户在成都" in core.user_profile
    row = next(row for row in store.memories("geoff", limit=20) if row["kind"] == "self_core")
    assert "角色档案" in row["content"]
    assert "理工" not in row["content"]
