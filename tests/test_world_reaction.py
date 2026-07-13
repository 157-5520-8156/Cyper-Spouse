from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.qq_websocket import CompanionQQClient
from companion_daemon.world import WorldKernel


def engine_with_observed_message(
    tmp_path: Path, *, message_id: str
) -> tuple[CompanionEngine, WorldKernel, str, IncomingMessage, CompanionReply]:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    incoming = IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id=message_id, text="哈哈"
    )
    world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": message_id,
            "text": incoming.text,
            "sent_at": incoming.sent_at.isoformat(),
            "idempotency_key": f"observe:{message_id}",
        },
        expected_revision=world.revision(world_id),
    )
    engine = CompanionEngine(
        store, FakeCompanionModel(), "你是知栀。", world_kernel=world, world_id=world_id
    )
    reply = CompanionReply(
        canonical_user_id="geoff", mood="happy", text="被你逗到了。", suggested_reaction="haha"
    )
    return engine, world, world_id, incoming, reply


def test_reaction_is_selected_scheduled_and_shared_only_after_adapter_receipt(
    tmp_path: Path,
) -> None:
    engine, world, world_id, incoming, reply = engine_with_observed_message(
        tmp_path, message_id="reaction-ok"
    )

    action_id = engine.begin_reaction_delivery(incoming, reply)
    assert action_id
    assert world.snapshot(world_id)["reactions"][action_id]["status"] == "selected"
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "scheduled"

    engine.settle_reaction_delivery(
        action_id,
        status="delivered",
        external_receipt="onebot-reaction:reaction-ok:128514",
    )

    snapshot = world.snapshot(world_id)
    assert snapshot["actions"][action_id]["status"] == "delivered"
    assert snapshot["reactions"][action_id]["status"] == "shared"
    assert snapshot["reactions"][action_id]["external_receipt"].endswith("128514")
    assert [
        event.event_type
        for event in world.events(world_id)
        if event.event_type in {"ReactionSelected", "ActionScheduled", "ActionSettled", "ReactionShared"}
    ][-4:] == ["ReactionSelected", "ActionScheduled", "ActionSettled", "ReactionShared"]


def test_uncertain_reaction_delivery_never_becomes_shared_history(tmp_path: Path) -> None:
    engine, world, world_id, incoming, reply = engine_with_observed_message(
        tmp_path, message_id="reaction-unknown"
    )
    action_id = engine.begin_reaction_delivery(incoming, reply)

    engine.settle_reaction_delivery(
        action_id,
        status="unknown",
        reason="connection_lost_after_dispatch",
    )

    snapshot = world.snapshot(world_id)
    assert snapshot["actions"][action_id]["status"] == "unknown"
    assert snapshot["reactions"][action_id]["status"] == "unknown"
    assert not any(event.event_type == "ReactionShared" for event in world.events(world_id))
    assert world.rebuild_projection(world_id, "world_current_state").matches_live is True


def test_late_reaction_receipt_reconciles_unknown_without_inventing_earlier_success(
    tmp_path: Path,
) -> None:
    engine, world, world_id, incoming, reply = engine_with_observed_message(
        tmp_path, message_id="reaction-late-receipt"
    )
    action_id = engine.begin_reaction_delivery(incoming, reply)
    engine.settle_reaction_delivery(
        action_id, status="unknown", reason="connection_lost_after_dispatch"
    )

    engine.settle_reaction_delivery(
        action_id,
        status="delivered",
        external_receipt="onebot-late:reaction-late-receipt:128514",
    )

    snapshot = world.snapshot(world_id)
    assert snapshot["actions"][action_id]["status"] == "delivered"
    assert snapshot["reactions"][action_id]["status"] == "shared"
    assert snapshot["reactions"][action_id]["external_receipt"].startswith("onebot-late")
    assert world.rebuild_projection(world_id, "world_current_state").matches_live is True


@pytest.mark.asyncio
async def test_official_qq_reports_unsupported_reaction_without_direct_world_write(
    tmp_path: Path,
) -> None:
    engine, world, world_id, incoming, reply = engine_with_observed_message(
        tmp_path, message_id="reaction-official-unsupported"
    )
    client = object.__new__(CompanionQQClient)
    client.engine = engine

    acceptance = await client._reject_unsupported_reply_reaction(incoming, reply)

    snapshot = world.snapshot(world_id)
    assert acceptance.status == "failed"
    assert acceptance.reason == "official_qq_reaction_unsupported"
    assert not snapshot["actions"]
    assert not any(event.event_type == "ReactionShared" for event in world.events(world_id))
    assert world.rebuild_projection(world_id, "world_current_state").matches_live is True
