import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

import companion_daemon.napcat_cli as napcat_cli
from companion_daemon.config import Settings
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.companion_turn import DispatchAcceptance
from companion_daemon.models import IncomingMessage
from companion_daemon.napcat_cli import (
    _parse_id_list,
    _private_sender_is_allowed,
    onebot_image_dispatch_acceptance,
    onebot_reaction_dispatch_acceptance,
    send_onebot_image_with_acceptance,
)
from companion_daemon.qq_outbound_owner import QQOutboundConfigurationError
from companion_daemon.turn_taking import TurnInput
from companion_daemon.world import WorldKernel


def test_napcat_settings_use_new_names() -> None:
    settings = Settings(NAPCAT_API_URL="http://127.0.0.1:3000", NAPCAT_ACCESS_TOKEN="secret")
    assert settings.napcat_api_url == "http://127.0.0.1:3000"
    assert settings.napcat_access_token == "secret"


def test_settings_reject_unknown_qq_adapter_before_any_process_starts() -> None:
    with pytest.raises(ValidationError, match="QQ_ADAPTER"):
        Settings(QQ_ADAPTER="auto")


def test_napcat_settings_accept_legacy_snowluma_names() -> None:
    settings = Settings(SNOWLUMA_API_URL="http://127.0.0.1:5700", SNOWLUMA_ACCESS_TOKEN="legacy")
    assert settings.onebot_api_url == "http://127.0.0.1:5700"
    assert settings.onebot_access_token == "legacy"


def test_napcat_and_generic_onebot_have_separate_settings() -> None:
    settings = Settings(
        NAPCAT_API_URL="http://127.0.0.1:3000",
        ONEBOT_API_URL="http://127.0.0.1:5700",
        ONEBOT_PROACTIVE_USER_ID="123456789",
    )
    assert settings.napcat_api_url == "http://127.0.0.1:3000"
    assert settings.onebot_api_url == "http://127.0.0.1:5700"
    assert settings.onebot_proactive_user_id == "123456789"


def test_napcat_group_messages_are_opt_in() -> None:
    assert Settings().napcat_allow_group_messages is False
    assert Settings(NAPCAT_ALLOW_GROUP_MESSAGES="true").napcat_allow_group_messages is True


def test_napcat_private_message_allowlist() -> None:
    settings = Settings(NAPCAT_ALLOWED_PRIVATE_USER_IDS="123, 456")
    allowed_ids = _parse_id_list(settings.napcat_allowed_private_user_ids)
    assert _private_sender_is_allowed("123", allowed_ids)
    assert _private_sender_is_allowed("456", allowed_ids)
    assert not _private_sender_is_allowed("789", allowed_ids)
    assert _private_sender_is_allowed("789", set())


@pytest.mark.parametrize(
    "result, expected_receipt",
    [
        ({"status": "ok", "retcode": 0, "data": {"message_id": "image-42"}}, "platform:message_id:image-42"),
        ({"status": "ok", "retcode": 0, "data": {"id": "image-43"}}, "platform:id:image-43"),
        ({"status": "ok", "retcode": 0, "data": {"msg_id": "image-44"}}, "platform:msg_id:image-44"),
    ],
)
def test_napcat_image_result_is_delivered_only_with_a_message_receipt(
    result: dict[str, object], expected_receipt: str
) -> None:
    assert onebot_image_dispatch_acceptance(result) == DispatchAcceptance(
        status="delivered", external_receipt=expected_receipt
    )


@pytest.mark.parametrize(
    "result",
    [
        {"status": "failed", "retcode": 0, "message": "permission denied"},
        {"status": "ok", "retcode": 100, "message": "send rejected"},
    ],
)
def test_napcat_image_result_marks_explicit_onebot_rejection_failed(
    result: dict[str, object],
) -> None:
    outcome = onebot_image_dispatch_acceptance(result)

    assert outcome.status == "failed"
    assert outcome.reason


def test_napcat_image_result_without_a_message_receipt_stays_unknown() -> None:
    assert onebot_image_dispatch_acceptance({"status": "ok", "retcode": 0, "data": {}}) == (
        DispatchAcceptance(
            status="unknown",
            reason="onebot_image_returned_without_durable_receipt",
        )
    )


@pytest.mark.parametrize(
    "result, expected_receipt",
    [
        (
            {"status": "ok", "retcode": 0, "data": {"message_id": "reaction-42"}},
            "platform:message_id:reaction-42",
        ),
        (
            {"status": "ok", "retcode": 0, "data": {"id": "reaction-43"}},
            "platform:id:reaction-43",
        ),
    ],
)
def test_napcat_reaction_result_uses_only_platform_issued_receipts(
    result: dict[str, object], expected_receipt: str
) -> None:
    assert onebot_reaction_dispatch_acceptance(result) == DispatchAcceptance(
        status="delivered", external_receipt=expected_receipt
    )


def test_napcat_reaction_success_without_a_durable_receipt_stays_unknown() -> None:
    """The requested incoming id/emoji must not be forged into a receipt."""
    assert onebot_reaction_dispatch_acceptance({"status": "ok", "retcode": 0}) == (
        DispatchAcceptance(
            status="unknown",
            reason="onebot_reaction_returned_without_durable_receipt",
        )
    )


def test_napcat_reaction_explicit_rejection_is_failed() -> None:
    outcome = onebot_reaction_dispatch_acceptance(
        {"status": "failed", "retcode": 0, "message": "emoji rejected"}
    )

    assert outcome.status == "failed"
    assert outcome.reason == "emoji rejected"


@pytest.mark.asyncio
async def test_napcat_image_send_exception_stays_unknown() -> None:
    class FailingTarget:
        async def send_image(self, _image_path: Path) -> dict[str, object]:
            raise httpx.ConnectError("NapCat unavailable")

    outcome = await send_onebot_image_with_acceptance(
        FailingTarget(),  # type: ignore[arg-type]
        Path("unused.png"),
    )

    assert outcome == DispatchAcceptance(
        status="unknown", reason="onebot_image_exception:ConnectError"
    )


def test_napcat_process_refuses_to_start_when_another_qq_adapter_is_configured(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        napcat_cli,
        "get_settings",
        lambda: Settings(QQ_ADAPTER="official"),
    )

    with pytest.raises(QQOutboundConfigurationError, match="only the configured adapter"):
        napcat_cli.create_app(adapter="napcat", use_fake_model=True)


def test_napcat_run_script_defaults_to_hotter_batch_window() -> None:
    script = Path("scripts/run_napcat_adapter.sh").read_text()

    assert "QQ_MESSAGE_BATCH_SECONDS:=0.8" in script


def test_napcat_adapter_caps_single_turn_continuation_wait(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCoalescer:
        def __init__(self, *_args, turn_policy, **_kwargs) -> None:
            captured["turn_policy"] = turn_policy

    monkeypatch.setattr(napcat_cli, "get_settings", lambda: Settings(QQ_ADAPTER="napcat"))
    monkeypatch.setattr(napcat_cli, "build_companion_engine", lambda **_kwargs: SimpleNamespace())
    monkeypatch.setattr(napcat_cli, "QQMessageCoalescer", FakeCoalescer)

    napcat_cli.create_app(
        adapter="napcat", use_fake_model=True, world_v2_c2c=False
    )

    policy = captured["turn_policy"]
    decision = policy.decide(  # type: ignore[attr-defined]
        TurnInput(pending_count=1, latest_text="我刚到家，", merged_text="我刚到家，")
    )
    assert decision.wait_seconds <= 2.0


@pytest.mark.asyncio
async def test_napcat_recovers_world_due_reply_later_through_companion_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    message = IncomingMessage(
        platform="qq",
        platform_user_id="10001",
        text="晚点回我",
        message_id="defer-napcat-1",
        sent_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )
    action = {
        "kind": "reply_later",
        "action_id": "reply_later:defer-napcat-1",
        "payload": {"message": message.model_dump(mode="json")},
    }

    class FakeWorld:
        def revision(self, world_id: str) -> int:
            assert world_id == "world-1"
            return 7

        def snapshot(self, world_id: str) -> dict[str, object]:
            assert world_id == "world-1"
            return {"clock": {"logical_at": "2026-07-14T09:02:00+00:00"}}

        def due_actions(self, world_id: str, *, now: datetime) -> list[dict[str, object]]:
            assert world_id == "world-1"
            assert now.isoformat() == "2026-07-14T09:02:00+00:00"
            return [action]

    class FakeClockDriver:
        def __init__(self, world) -> None:
            assert isinstance(world, FakeWorld)

        def tick(self, world_id: str, *, observed_now, expected_revision: int) -> None:
            assert world_id == "world-1"
            assert expected_revision == 7

    calls: list[object] = []

    class FakeCompanionTurn:
        def __init__(self, engine, transport) -> None:
            calls.append(("transport", transport))

        async def resume_scheduled_reply(self, frame, *, budget, context_hint):
            calls.append((frame, budget, context_hint))
            return SimpleNamespace(visible_status="delivered")

    engine = SimpleNamespace(
        world_kernel=FakeWorld(),
        world_id="world-1",
        store=SimpleNamespace(resolve_user=lambda platform, user_id: f"{platform}:{user_id}"),
    )
    target = object()
    monkeypatch.setattr(napcat_cli, "WorldClockDriver", FakeClockDriver)
    monkeypatch.setattr(napcat_cli, "_target_for", lambda *_args: target)
    monkeypatch.setattr(napcat_cli, "CompanionTurn", FakeCompanionTurn)

    recovered = await napcat_cli._recover_due_world_scheduled_actions(
        engine, api_url="http://127.0.0.1:3000", access_token="test-token"
    )

    assert recovered == 1
    frame, budget, context_hint = calls[1]  # type: ignore[misc]
    assert frame.source_action_id == "reply_later:defer-napcat-1"
    assert frame.canonical_user_id == "qq:10001"
    assert frame.message == message
    assert frame.kind == "reply_later"
    assert budget == napcat_cli.NAPCAT_SCHEDULED_BUDGET
    assert "岔开" in context_hint


@pytest.mark.asyncio
async def test_napcat_http_event_reaches_companion_turn_and_settles_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protect the production HTTP entrypoint rather than only its helpers."""
    store = CompanionStore(tmp_path / "napcat.sqlite")
    seed_user(store)
    store.map_account("qq", "10001", "geoff")
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store, FakeCompanionModel(), "你是沈知栀。", world_kernel=world, world_id=world_id
    )

    class Target:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def reply(self, **kwargs: object) -> dict[str, object]:
            self.messages.append(str(kwargs["content"]))
            return {"data": {"message_id": "onebot-r1"}}

    target = Target()
    observation_path = tmp_path / "evidence" / "napcat-turns.jsonl"
    settings = Settings(
        QQ_ADAPTER="napcat",
        QQ_MESSAGE_BATCH_SECONDS="0",
        QQ_TURN_OBSERVATION_PATH=str(observation_path),
        NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        NAPCAT_ACCEPT_UNAUTHENTICATED_LOCAL_EVENTS="true",
    )
    monkeypatch.setattr(napcat_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(napcat_cli, "build_companion_engine", lambda **_kwargs: engine)
    monkeypatch.setattr(napcat_cli, "_target_for", lambda *_args: target)
    app = napcat_cli.create_app(
        adapter="napcat", use_fake_model=True, world_v2_c2c=False
    )
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "message_id": "incoming-1",
        "raw_message": "今天有点累。",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/onebot/event", json=event)
        duplicate = await client.post("/onebot/event", json=event)

    assert response.json() == {"status": "ok"}
    assert duplicate.json() == {"status": "duplicate"}
    # The coalescer deliberately owns a task so the HTTP webhook can return
    # promptly.  Yield until its zero-delay merge and CompanionTurn complete.
    for _ in range(20):
        if target.messages and observation_path.exists():
            break
        await asyncio.sleep(0.01)
    assert target.messages
    observations = [json.loads(line) for line in observation_path.read_text().splitlines()]
    assert len(observations) == 1
    assert observations[0]["adapter"] == "napcat"
    assert observations[0]["durable_receipt_status"] == "delivered"
    assert observations[0]["action_ids"]
    assert observations[0]["segment_ids"]
    assert "10001" not in observation_path.read_text()
    assert "今天有点累" not in observation_path.read_text()
    snapshot = world.snapshot(world_id)
    actions = [
        action for action in snapshot["actions"].values()
        if action["kind"] == "outgoing_message"
    ]
    assert len(actions) == 1
    assert actions[0]["status"] == "delivered"
    assert actions[0]["segment_state"]["segments"][0]["external_receipt"] == (
        "platform:message_id:onebot-r1"
    )
    assert len(target.messages) == 1
    await engine.aclose()
