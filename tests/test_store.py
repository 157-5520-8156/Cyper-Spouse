from concurrent.futures import ThreadPoolExecutor
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


def test_resolve_unknown_platform_account_is_concurrency_safe(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite", primary_user_id="geoff")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: store.resolve_user("qq", "qq-openid"), range(8)))

    assert results == ["geoff"] * 8
    assert store.platform_user_id("geoff", "qq") == "qq-openid"


def test_upsert_memory_merges_near_duplicates(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")

    store.upsert_memory("geoff", kind="life_fact", content="我人在成都", source="a", confidence=0.6)
    store.upsert_memory("geoff", kind="life_fact", content="我现在人在成都", source="b", confidence=0.8)

    memories = store.memories("geoff")
    assert len(memories) == 1
    assert memories[0]["content"] == "我人在成都"
    assert memories[0]["confidence"] == 0.8


def test_store_records_tool_proposals(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")

    store.record_tool_proposal(
        "geoff",
        kind="computer_assist",
        risk="confirmation_required",
        summary="用户请求打开浏览器。",
    )

    proposals = store.recent_tool_proposals("geoff")
    assert len(proposals) == 1
    assert proposals[0]["kind"] == "computer_assist"
    assert proposals[0]["status"] == "proposed"
