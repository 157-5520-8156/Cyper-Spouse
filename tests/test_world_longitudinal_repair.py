from datetime import datetime, timedelta
from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.world import WorldKernel


def _world(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "longitudinal-repair.sqlite"))
    started = kernel.submit(
        {
            "type": "start_world",
            "seed": {
                "world_id": "repair-world",
                "logical_at": "2026-07-01T09:00:00+08:00",
                "protagonist": {
                    "id": "zhizhi",
                    "name": "沈知栀",
                    "kind": "companion",
                    "stable_traits": ["温和、敏感、观察力强"],
                    "templates": [],
                },
                "life_outcome_templates": {},
                "daily_schedule": [],
                "long_term_goals": [],
                "npcs": [],
            },
        },
        expected_revision=0,
    )
    kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "repair-user",
        },
        expected_revision=started.revision,
    )
    return kernel, started.world_id


def _appraise(
    kernel: WorldKernel, world_id: str, index: int, appraisal: str
) -> None:
    kernel.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": appraisal,
            "intent_id": f"repair-intent:{index}",
            "message_id": f"repair-message:{index}",
            "user_id": "user:geoff",
            "idempotency_key": f"repair-appraise:{index}",
        },
        expected_revision=kernel.revision(world_id),
    )


def test_repair_requires_later_consistent_behavior_not_only_time_or_more_apologies(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    _appraise(kernel, world_id, 2, "repair_specific")
    _appraise(kernel, world_id, 3, "repair_specific")
    now = datetime.fromisoformat(
        str(kernel.snapshot(world_id)["clock"]["logical_at"])
    )

    kernel.advance(
        world_id,
        now + timedelta(days=2),
        expected_revision=kernel.revision(world_id),
    )

    waiting = kernel.snapshot(world_id)["emotion_modulation"]
    assert waiting["unresolved"] is True
    assert waiting["behavior_tendency"] == "repair_observing"
    assert waiting["repair_evidence_count"] == 0

    _appraise(kernel, world_id, 4, "boundary_respected")
    _appraise(kernel, world_id, 5, "boundary_respected")
    evidenced = kernel.snapshot(world_id)["emotion_modulation"]
    assert evidenced["repair_evidence_count"] == 2

    later = datetime.fromisoformat(
        str(kernel.snapshot(world_id)["clock"]["logical_at"])
    )
    kernel.advance(
        world_id,
        later + timedelta(hours=2),
        expected_revision=kernel.revision(world_id),
    )

    repaired = kernel.snapshot(world_id)["emotion_modulation"]
    assert repaired["unresolved"] is False
    assert repaired["behavior_tendency"] == "neutral"
    assert all(
        episode["appraisal"] != "boundary_violation"
        for episode in repaired["active_episodes"]
    )
