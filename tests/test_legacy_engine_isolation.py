from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import pytest

from companion_daemon.companion_turn import CompanionTurn, ResponseBudget, TurnEnvelope
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import CompanionReply, IncomingMessage, ProactiveDecision
from companion_daemon.turn_transports import CaptureTurnTransport
from companion_daemon.world import WorldError, WorldKernel


TEST_PROMPT = "你是凛，用户的赛博女友。"


def _world_engine(tmp_path: Path) -> tuple[CompanionEngine, WorldKernel, str]:
    store = CompanionStore(tmp_path / "legacy-confirmations.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    return (
        CompanionEngine(
            store,
            FakeCompanionModel(),
            TEST_PROMPT,
            world_kernel=world,
            world_id=world_id,
        ),
        world,
        world_id,
    )


def _legacy_reply() -> CompanionReply:
    return CompanionReply(
        canonical_user_id="geoff",
        mood="calm",
        text="我在。",
        delivery_id=1,
        turn_trace_id=1,
        world_action_id="outgoing:legacy-test",
        media_action_id="media:legacy-test",
        sticker_action_id="sticker:legacy-test",
    )


def _legacy_decision() -> ProactiveDecision:
    return ProactiveDecision(
        canonical_user_id="geoff",
        private_thought="想问候一下。",
        should_send=True,
        platform="qq",
        message_type="text",
        message="今天还好吗？",
        delivery_id=1,
        turn_trace_id=1,
        world_action_id="outgoing:legacy-proactive",
    )


LegacyConfirmation = Callable[[CompanionEngine], object]


@pytest.mark.parametrize(
    ("api", "confirm"),
    [
        ("confirm_reply_delivery", lambda engine: engine.confirm_reply_delivery(_legacy_reply())),
        (
            "confirm_reply_part_delivery",
            lambda engine: engine.confirm_reply_part_delivery(
                _legacy_reply(), segment_id="segment:legacy-test", external_receipt="qq:1"
            ),
        ),
        ("confirm_media_delivery", lambda engine: engine.confirm_media_delivery(_legacy_reply())),
        ("confirm_sticker_delivery", lambda engine: engine.confirm_sticker_delivery(_legacy_reply())),
        (
            "confirm_afterthought_delivery",
            lambda engine: engine.confirm_afterthought_delivery(
                "geoff", "qq", "哦对，补一句。", delivery_id=1
            ),
        ),
        (
            "confirm_life_event_delivery",
            lambda engine: engine.confirm_life_event_delivery("geoff", "qq"),
        ),
        ("confirm_proactive_delivery", lambda engine: engine.confirm_proactive_delivery(_legacy_decision())),
    ],
    ids=lambda item: item if isinstance(item, str) else "call",
)
def test_world_rejects_legacy_engine_confirmation_without_mutating_the_ledger(
    tmp_path: Path,
    api: str,
    confirm: LegacyConfirmation,
) -> None:
    engine, world, world_id = _world_engine(tmp_path)
    revision_before = world.revision(world_id)
    event_count_before = len(world.events(world_id))

    with pytest.raises(WorldError, match=rf"{api}.*CompanionTurn"):
        confirm(engine)

    assert world.revision(world_id) == revision_before
    assert len(world.events(world_id)) == event_count_before


@pytest.mark.asyncio
async def test_world_turn_still_settles_through_the_transport_receipt(tmp_path: Path) -> None:
    engine, world, world_id = _world_engine(tmp_path)
    message = IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id="legacy-isolation-turn",
        text="今天有点累。",
        sent_at=datetime(2026, 7, 14, tzinfo=UTC),
    )
    context = engine.freeze_turn_context(message)
    turn = CompanionTurn(engine, CaptureTurnTransport(receipt_namespace="isolation-test"))

    outcome = await turn.respond(
        TurnEnvelope.from_message(
            message,
            idempotency_key="qq:geoff:legacy-isolation-turn",
            world_id=world_id,
            canonical_user_id="geoff",
            frozen_cadence=context.cadence.heat,
        ),
        budget=ResponseBudget(first_visible_by_ms=8_000, complete_by_ms=12_000),
    )

    assert outcome.visible_status == "delivered"
    action = world.snapshot(world_id)["actions"][outcome.action_ids[0]]
    assert action["status"] == "delivered"
