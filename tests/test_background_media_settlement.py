from __future__ import annotations

from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.world import WorldKernel


class _UnusedModel:
    async def complete(self, _messages, *, temperature: float) -> str:
        del temperature
        return '{"reply_text":"我在。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'


def _engine_with_requested_media(tmp_path: Path) -> tuple[CompanionEngine, WorldKernel, str]:
    store = CompanionStore(tmp_path / "media-settlement.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {
            "type": "register_user",
            "world_id": world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=world.revision(world_id),
    )
    world.submit(
        {
            "type": "request_media",
            "world_id": world_id,
            "request_id": "background-settlement",
            "user_id": "user:geoff",
            "media_kind": "creative_image",
            "topic": "一张插画",
            "reason": "user_requested_creative_image",
        },
        expected_revision=world.revision(world_id),
    )
    return (
        CompanionEngine(store, _UnusedModel(), "test", world_kernel=world, world_id=world_id),
        world,
        world_id,
    )


@pytest.mark.asyncio
async def test_background_media_without_a_receipt_stays_unknown(tmp_path: Path) -> None:
    engine, world, world_id = _engine_with_requested_media(tmp_path)
    generation_action = "media-generation:background-settlement"
    await engine._settle_background_media_result(
        action_id=generation_action,
        result_kind="media_generation",
        status="delivered",
        payload={"artifact_path": "assets/life/background.png", "artifact_hash": "abc123"},
        idempotency_key="background-generation:1",
    )
    world.submit(
        {
            "type": "schedule_media_delivery",
            "world_id": world_id,
            "request_id": "background-settlement",
            "idempotency_key": "background-delivery:scheduled",
        },
        expected_revision=world.revision(world_id),
    )

    await engine._settle_background_media_result(
        action_id="media-delivery:background-settlement",
        result_kind="media_delivery",
        status="unknown",
        reason="qq_image_returned_without_durable_receipt",
        idempotency_key="background-delivery:unknown",
    )

    action = world.snapshot(world_id)["actions"]["media-delivery:background-settlement"]
    media = world.snapshot(world_id)["media"]["background-settlement"]
    assert action["status"] == "unknown"
    assert media["status"] == "generated"


@pytest.mark.asyncio
async def test_background_media_with_a_receipt_is_shared_once(tmp_path: Path) -> None:
    engine, world, world_id = _engine_with_requested_media(tmp_path)
    await engine._settle_background_media_result(
        action_id="media-generation:background-settlement",
        result_kind="media_generation",
        status="delivered",
        payload={"artifact_path": "assets/life/background.png", "artifact_hash": "abc123"},
        idempotency_key="background-generation:1",
    )
    world.submit(
        {
            "type": "schedule_media_delivery",
            "world_id": world_id,
            "request_id": "background-settlement",
            "idempotency_key": "background-delivery:scheduled",
        },
        expected_revision=world.revision(world_id),
    )
    before = world.revision(world_id)
    kwargs = {
        "action_id": "media-delivery:background-settlement",
        "result_kind": "media_delivery",
        "status": "delivered",
        "payload": {"external_receipt": "qq:image:42"},
        "idempotency_key": "background-delivery:receipt:42",
    }
    await engine._settle_background_media_result(**kwargs)
    after_first = world.revision(world_id)
    await engine._settle_background_media_result(**kwargs)

    action = world.snapshot(world_id)["actions"]["media-delivery:background-settlement"]
    assert action["status"] == "delivered"
    assert world.snapshot(world_id)["media"]["background-settlement"]["status"] == "shared"
    assert after_first > before
    assert world.revision(world_id) == after_first
