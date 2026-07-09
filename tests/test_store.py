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
