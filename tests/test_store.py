from pathlib import Path

from companion_daemon.db import CompanionStore


def test_platform_user_id_returns_latest_mapping(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    store.map_account("qq", "openid-1", "geoff")

    assert store.platform_user_id("geoff", "qq") == "openid-1"
    assert store.platform_user_id("geoff", "wechat") is None


def test_resolve_unknown_platform_account_uses_primary_user(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite", primary_user_id="geoff")

    canonical = store.resolve_user("qq", "qq-openid")

    assert canonical == "geoff"
    assert store.platform_user_id("geoff", "qq") == "qq-openid"


def test_upsert_memory_merges_near_duplicates(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")

    store.upsert_memory("geoff", kind="life_fact", content="我人在成都", source="a", confidence=0.6)
    store.upsert_memory("geoff", kind="life_fact", content="我现在人在成都", source="b", confidence=0.8)

    memories = store.memories("geoff")
    assert len(memories) == 1
    assert memories[0]["content"] == "我人在成都"
    assert memories[0]["confidence"] == 0.8
