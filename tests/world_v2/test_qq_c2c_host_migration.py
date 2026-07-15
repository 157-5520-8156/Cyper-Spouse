from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.qq_c2c_host import (
    QQC2CIdentityResolver,
    build_qq_c2c_host,
    qq_c2c_target,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class _Delivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"qq-{len(self.sent)}"}}


@pytest.mark.asyncio
async def test_qq_c2c_host_runs_text_ingress_and_restart_recovery_without_a_legacy_sender(
    tmp_path: Path,
) -> None:
    database = tmp_path / "qq-c2c-v2.sqlite"
    first_delivery = _Delivery()
    first = build_qq_c2c_host(
        settings=Settings(database_path=database, PRIMARY_USER_ID="geoff"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=first_delivery,
    )
    try:
        result = await first.inbound_text(
            message_id="onebot-message-1",
            recipient_id="10001",
            text="我今天有点累。",
            observed_at=NOW,
        )
        duplicate = await first.inbound_text(
            message_id="onebot-message-1",
            recipient_id="10001",
            text="我今天有点累。",
            observed_at=NOW,
        )
    finally:
        await first.aclose()

    assert result.status == "action_authorized"
    assert result.action_id is not None
    assert duplicate.action_id == result.action_id
    assert len(first_delivery.sent) == 1

    # OneBot only acknowledged acceptance.  A fresh process cannot prove the
    # terminal send, so it recovers to unknown rather than emitting a duplicate.
    second_delivery = _Delivery()
    restarted = build_qq_c2c_host(
        settings=Settings(database_path=database, PRIMARY_USER_ID="geoff"),
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(seconds=1),
        model=FakeCompanionModel(),
        delivery=second_delivery,
    )
    try:
        drained = await restarted.scheduler_once(
            observed_at=NOW + timedelta(seconds=121),
            max_action_units=3,
            max_background_units=2,
        )
    finally:
        await restarted.aclose()

    assert second_delivery.sent == []
    assert drained.action_statuses
    assert any("unknown" in status for status in drained.action_statuses), drained


@pytest.mark.asyncio
async def test_qq_c2c_host_rejects_an_unconfigured_user_before_it_can_enter_the_world(
    tmp_path: Path,
) -> None:
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-c2c-v2-user.sqlite"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=_Delivery(),
    )
    try:
        with pytest.raises(ValueError, match="not configured"):
            await host.inbound_text(
                message_id="onebot-message-foreign",
                recipient_id="20002",
                text="不应被映射到默认用户",
                observed_at=NOW,
            )
    finally:
        await host.aclose()


def test_qq_c2c_identity_is_one_recipient_to_one_explicit_reply_target() -> None:
    resolver = QQC2CIdentityResolver(recipient_id="10001", canonical_user_id="geoff")

    assert resolver.resolve(platform="qq", platform_user_id="10001") == (
        "user:geoff",
        "conversation:qq:c2c:10001",
    )
    assert qq_c2c_target("10001") == "conversation:qq:c2c:10001"

    with pytest.raises(ValueError, match="not configured"):
        resolver.resolve(platform="qq", platform_user_id="20002")


def test_qq_c2c_v2_host_has_no_legacy_chat_or_coalescer_imports() -> None:
    path = Path(__file__).parents[2] / "src/companion_daemon/world_v2/qq_c2c_host.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    forbidden = (
        "companion_daemon.engine",
        "companion_daemon.world",
        "companion_daemon.runtime",
        "companion_daemon.companion_turn",
        "companion_daemon.qq_websocket",
    )
    assert not any(module.startswith(prefix) for module in imports for prefix in forbidden)


def test_napcat_v2_branch_never_builds_a_legacy_engine_and_rejects_unsupported_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.napcat_cli as napcat_cli
    import companion_daemon.world_v2.qq_c2c_onebot_app as onebot_v2

    class _Host:
        inbound_calls: list[dict[str, object]] = []

        async def inbound_text(self, **kwargs: object):
            self.inbound_calls.append(kwargs)
            return type(
                "Result",
                (),
                {
                    "status": "action_authorized",
                    "action_id": "action:v2:1",
                    "canonical_user_id": "geoff",
                },
            )()

        async def scheduler_once(self, **_kwargs: object):
            return None

        async def aclose(self) -> None:
            return None

    host = _Host()
    settings = Settings(
        QQ_ADAPTER="napcat",
        NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        NAPCAT_ACCESS_TOKEN="test-token",
        NAPCAT_ACCEPT_UNAUTHENTICATED_LOCAL_EVENTS="false",
    )
    monkeypatch.setattr(napcat_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        napcat_cli,
        "build_companion_engine",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy engine must not be built")),
    )
    monkeypatch.setattr(onebot_v2, "build_qq_c2c_host", lambda **_kwargs: host)

    app = napcat_cli.create_app(adapter="napcat", use_fake_model=True, world_v2_c2c=True)
    with TestClient(app) as client:
        text = client.post(
            "/onebot/event",
            headers={"Authorization": "Bearer test-token"},
            json={
                "post_type": "message",
                "message_type": "private",
                "user_id": "10001",
                "message_id": "onebot-text-1",
                "raw_message": "在吗？",
            },
        )
        group = client.post(
            "/onebot/event",
            headers={"Authorization": "Bearer test-token"},
            json={
                "post_type": "message",
                "message_type": "group",
                "group_id": "50001",
                "user_id": "10001",
                "message_id": "onebot-group-1",
                "raw_message": "@你 在吗？",
            },
        )
        sticker = client.post(
            "/onebot/event",
            headers={"Authorization": "Bearer test-token"},
            json={
                "post_type": "message",
                "message_type": "private",
                "user_id": "10001",
                "message_id": "onebot-sticker-1",
                "message": [{"type": "face", "data": {"id": "1"}}],
            },
        )

    assert text.json() == {
        "status": "action_authorized",
        "world_action_id": "action:v2:1",
        "canonical_user_id": "geoff",
    }
    assert group.json() == {"status": "ignored_group_v2_unsupported"}
    assert sticker.json() == {"status": "ignored_non_text_v2_unsupported"}
    assert len(host.inbound_calls) == 1
    assert host.inbound_calls[0]["message_id"] == "onebot-text-1"
    assert host.inbound_calls[0]["recipient_id"] == "10001"
    assert host.inbound_calls[0]["text"] == "在吗？"
    assert isinstance(host.inbound_calls[0]["observed_at"], datetime)


def test_qq_c2c_onebot_adapter_has_no_legacy_chat_or_coalescer_imports() -> None:
    path = Path(__file__).parents[2] / "src/companion_daemon/world_v2/qq_c2c_onebot_app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = (
        "companion_daemon.engine",
        "companion_daemon.world",
        "companion_daemon.runtime",
        "companion_daemon.companion_turn",
        "companion_daemon.qq_websocket",
    )
    assert not any(module.startswith(prefix) for module in imports for prefix in forbidden)
