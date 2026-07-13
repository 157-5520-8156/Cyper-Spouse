from types import SimpleNamespace
from datetime import timedelta
from pathlib import Path

import pytest

from companion_daemon import proactive_cli
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.world import WorldKernel


@pytest.mark.asyncio
async def test_proactive_run_closes_engine_after_early_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        closed = False

        async def proactive_tick(self, _user_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                private_thought="不发。",
                should_send=False,
                platform=None,
                message_type="none",
                message=None,
                sticker_path=None,
                image_path=None,
            )

        async def aclose(self) -> None:
            self.closed = True

    engine = FakeEngine()
    monkeypatch.setattr(proactive_cli, "build_companion_engine", lambda: engine)

    await proactive_cli.run("geoff", send=False, sandbox=True)

    assert engine.closed is True


@pytest.mark.asyncio
async def test_proactive_run_closes_engine_when_generation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        closed = False

        async def proactive_tick(self, _user_id: str) -> None:
            raise RuntimeError("provider failed")

        async def aclose(self) -> None:
            self.closed = True

    engine = FakeEngine()
    monkeypatch.setattr(proactive_cli, "build_companion_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="provider failed"):
        await proactive_cli.run("geoff", send=False, sandbox=True)

    assert engine.closed is True


@pytest.mark.asyncio
async def test_legacy_proactive_send_is_retired_without_delivery_or_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = SimpleNamespace(
        private_thought="想说一句。",
        should_send=True,
        platform="qq",
        message_type="text",
        message="刚刚想起一件小事。",
        sticker_path=None,
        image_path=None,
        world_action_id=None,
        delivery_id=42,
    )

    class FakeEngine:
        world_kernel = None

        async def proactive_tick(self, _user_id: str) -> SimpleNamespace:
            return decision

        def confirm_proactive_delivery(self, _decision: object) -> None:
            raise AssertionError("retired legacy path must not confirm delivery")

        def fail_proactive_delivery(self, _decision: object, _reason: str) -> None:
            raise AssertionError("retired legacy path must not mutate delivery state")

    class FakeDelivery:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("retired legacy path must not construct a delivery adapter")

    monkeypatch.setattr(proactive_cli, "QQDelivery", FakeDelivery)

    await proactive_cli._run_with_engine(FakeEngine(), user_id="geoff", send=True, sandbox=True)


@pytest.mark.asyncio
async def test_world_proactive_cli_settles_through_turn_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    store.map_account("qq", "openid", "geoff")
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        object(),  # The test only dispatches an already-authorized Action.
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    delivery_id, _trace_id, action_id = world.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="刚刚想起一件小事。",
        kind="proactive",
        expires_at=engine._world_logical_now() + timedelta(hours=1),
        trace={
            "world_id": world_id,
            "direction": "proactive",
            "appraisal": "test",
            "expression_policy": "test",
            "observable_reason": "test",
        },
    )

    class FakeDelivery:
        sent: list[str] = []

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def proactive_recipient_id(self) -> None:
            return None

        async def send_text(self, recipient_id: str, text: str) -> dict[str, str]:
            assert recipient_id == "openid"
            self.sent.append(text)
            return {"message_id": "proactive-receipt-1"}

    monkeypatch.setattr(proactive_cli, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(proactive_cli, "get_settings", lambda: SimpleNamespace())
    # A World decision must not use the old direct confirmation hook.
    engine.confirm_proactive_delivery = lambda _decision: (_ for _ in ()).throw(AssertionError())
    engine.proactive_tick = lambda _user_id: _async_value(
        SimpleNamespace(
            canonical_user_id="geoff",
            private_thought="想说一句。",
            should_send=True,
            platform="qq",
            message_type="text",
            message="刚刚想起一件小事。",
            sticker_path=None,
            image_path=None,
            world_action_id=action_id,
            delivery_id=delivery_id,
        )
    )

    await proactive_cli._run_with_engine(engine, user_id="geoff", send=True, sandbox=True)

    action = world.snapshot(world_id)["actions"][action_id]
    assert FakeDelivery.sent == ["刚刚想起一件小事。"]
    assert action["status"] == "delivered"
    assert action["segment_state"]["segments"][0]["external_receipt"] == "platform:message_id:proactive-receipt-1"


@pytest.mark.asyncio
async def test_world_proactive_cli_keeps_receiptless_send_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    store.map_account("qq", "openid", "geoff")
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, object(), "你是沈知栀。", world_kernel=world, world_id=world_id)
    delivery_id, _trace_id, action_id = world.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="在吗。", kind="proactive",
        expires_at=engine._world_logical_now() + timedelta(hours=1),
        trace={
            "world_id": world_id,
            "direction": "proactive",
            "appraisal": "test",
            "expression_policy": "test",
            "observable_reason": "test",
        },
    )

    class ReceiptlessDelivery:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def proactive_recipient_id(self) -> None:
            return None

        async def send_text(self, _recipient_id: str, _text: str) -> dict[str, str]:
            return {"status": "ok"}

    monkeypatch.setattr(proactive_cli, "QQDelivery", ReceiptlessDelivery)
    monkeypatch.setattr(proactive_cli, "get_settings", lambda: SimpleNamespace())
    engine.proactive_tick = lambda _user_id: _async_value(
        SimpleNamespace(
            canonical_user_id="geoff", private_thought="想问一句。", should_send=True,
            platform="qq", message_type="text", message="在吗。", sticker_path=None,
            image_path=None, world_action_id=action_id, delivery_id=delivery_id,
        )
    )

    await proactive_cli._run_with_engine(engine, user_id="geoff", send=True, sandbox=True)

    assert world.snapshot(world_id)["actions"][action_id]["status"] == "unknown"


async def _async_value(value):
    return value
