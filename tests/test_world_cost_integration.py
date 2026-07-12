from datetime import datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.world import WorldError, WorldKernel


def seeded_world(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    world_id = kernel.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    return kernel, world_id


def test_every_external_action_is_cost_reserved_and_terminally_settled(tmp_path: Path) -> None:
    kernel, world_id = seeded_world(tmp_path)
    scheduled = kernel.submit(
        {
            "type": "schedule_action",
            "world_id": world_id,
            "action_id": "model-call:reply-1",
            "kind": "model_call",
            "expires_at": "2026-07-11T10:00:00+08:00",
            "payload": {"purpose": "reply", "causation": "turn-1"},
            "idempotency_key": "schedule:model-call:reply-1",
        },
        expected_revision=kernel.revision(world_id),
    )

    assert [event.event_type for event in scheduled.events][:2] == [
        "CostReservationDecided",
        "ActionScheduled",
    ]
    action = kernel.snapshot(world_id)["actions"]["model-call:reply-1"]
    assert action["cost"]["category"] == "chat"
    assert action["cost"]["reservation_id"]

    settled = kernel.record_external_result(
        "model-call:reply-1",
        {"kind": "model_call", "status": "delivered", "output_hash": "abc"},
        expected_revision=scheduled.revision,
        world_id=world_id,
    )

    assert [event.event_type for event in settled.events][-1] == "CostReservationSettled"
    projection = kernel.snapshot(world_id)["cost_ledger"]
    assert projection["usage"]["2026-07-11"]["chat"]["settled_units"] > 0
    assert kernel.rebuild_projection(world_id, "world_current_state").matches_live is True


def test_cancelled_external_action_releases_its_cost_reservation(tmp_path: Path) -> None:
    kernel, world_id = seeded_world(tmp_path)
    scheduled = kernel.submit(
        {
            "type": "schedule_action",
            "world_id": world_id,
            "action_id": "media:cancelled",
            "kind": "media_generation",
            "expires_at": "2026-07-11T11:00:00+08:00",
            "payload": {"request_id": "selfie-1", "media_kind": "selfie"},
            "idempotency_key": "schedule:media:cancelled",
        },
        expected_revision=kernel.revision(world_id),
    )
    cancelled = kernel.submit(
        {
            "type": "cancel_action",
            "world_id": world_id,
            "action_id": "media:cancelled",
            "reason": "user_returned",
            "idempotency_key": "cancel:media:cancelled",
        },
        expected_revision=scheduled.revision,
    )

    assert [event.event_type for event in cancelled.events][-1] == "CostReservationReleased"
    assert kernel.snapshot(world_id)["cost_ledger"]["usage"]["2026-07-11"]["image"]["total_units"] == 0


def test_controlled_outbound_override_spends_replayable_strikes_and_has_a_cooldown(
    tmp_path: Path,
) -> None:
    kernel, world_id = seeded_world(tmp_path)
    now = datetime.fromisoformat(str(kernel.snapshot(world_id)["clock"]["logical_at"]))
    trace = {
        "world_id": world_id,
        "direction": "proactive",
        "appraisal": "autonomous_checkin",
        "expression_policy": "克制地承担一次打扰风险。",
        "allowed_facts": [],
        "observable_reason": "角色决定不完全顺从沉默压力。",
        "outbound_override": {
            "reason": "角色愿意承担关系代价表达自己的在意",
            "cost": 20,
            "strike": 1,
            "gates": ["outreach:relationship_stage_stranger"],
        },
    }
    first_delivery, _, first_action = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="我知道有点唐突，但还是想说我在意。",
        kind="proactive",
        expires_at=now + timedelta(hours=2),
        trace=trace,
    )

    first = kernel.snapshot(world_id)
    assert first["actions"][first_action]["status"] == "scheduled"
    assert first["controlled_transgressions"][-1]["strikes"] == 1
    assert first["controlled_transgressions"][-1]["relationship_cost"] == 20
    kernel.settle_outgoing_action(first_delivery, delivered=True)

    with pytest.raises(WorldError, match="transgression_cooldown"):
        kernel.queue_outgoing_action(
            canonical_user_id="geoff",
            platform="qq",
            text="我又想补一句。",
            kind="proactive",
            expires_at=now + timedelta(hours=2),
            trace={**trace, "outbound_request_id": "second-stubborn-message"},
        )
