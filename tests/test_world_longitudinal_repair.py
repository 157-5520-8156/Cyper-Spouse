from datetime import datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.world import WorldError, WorldKernel


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
    kernel: WorldKernel,
    world_id: str,
    index: int,
    appraisal: str,
    *,
    opportunity_id: str = "",
) -> None:
    kernel.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": appraisal,
            "interaction": (
                {
                    "repair_evidence": {
                        "violation_id": "message:repair-message:1",
                        "commitment_id": "commitment:message:repair-message:1",
                        "opportunity_id": opportunity_id,
                        "behavior_key": "honor_boundary",
                    }
                }
                if opportunity_id
                else {}
            ),
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

    _appraise(
        kernel, world_id, 4, "boundary_respected", opportunity_id="opportunity:1"
    )
    _appraise(
        kernel, world_id, 5, "boundary_respected", opportunity_id="opportunity:1"
    )
    duplicate = kernel.snapshot(world_id)["emotion_modulation"]
    assert duplicate["repair_evidence_count"] == 1
    _appraise(
        kernel, world_id, 6, "boundary_respected", opportunity_id="opportunity:2"
    )
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


def _followthrough(
    kernel: WorldKernel,
    world_id: str,
    *,
    index: int,
    violation_id: str,
    commitment_id: str,
    opportunity_id: str,
    behavior_key: str = "honor_boundary",
) -> None:
    kernel.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": "boundary_respected",
            "interaction": {
                "repair_evidence": {
                    "violation_id": violation_id,
                    "commitment_id": commitment_id,
                    "opportunity_id": opportunity_id,
                    "behavior_key": behavior_key,
                }
            },
            "intent_id": f"followthrough-intent:{index}",
            "message_id": f"followthrough-message:{index}",
            "user_id": "user:geoff",
            "idempotency_key": f"followthrough:{index}",
        },
        expected_revision=kernel.revision(world_id),
    )


def test_world_rejects_a_forged_or_uncommitted_repair_id(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")

    with pytest.raises(WorldError, match="committed repair commitment"):
        _followthrough(
            kernel,
            world_id,
            index=2,
            violation_id="message:repair-message:1",
            commitment_id="commitment:forged",
            opportunity_id="opportunity:forged",
        )


def test_repair_commitment_and_opportunity_cannot_cross_violations(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    _appraise(kernel, world_id, 2, "repair_specific")
    _appraise(kernel, world_id, 3, "boundary_violation")

    with pytest.raises(WorldError, match="active violation"):
        _followthrough(
            kernel,
            world_id,
            index=4,
            violation_id="message:repair-message:1",
            commitment_id="commitment:message:repair-message:1",
            opportunity_id="opportunity:crossed",
        )


def test_repair_events_are_ordered_and_opportunity_reuse_is_idempotent_only_when_exact(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    _appraise(kernel, world_id, 1, "boundary_violation")
    _appraise(kernel, world_id, 2, "repair_specific")
    _followthrough(
        kernel,
        world_id,
        index=3,
        violation_id="message:repair-message:1",
        commitment_id="commitment:message:repair-message:1",
        opportunity_id="opportunity:ordered",
    )
    relationship_after_first = dict(
        kernel.snapshot(world_id)["relationships"]["user:geoff"]
    )
    _followthrough(
        kernel,
        world_id,
        index=4,
        violation_id="message:repair-message:1",
        commitment_id="commitment:message:repair-message:1",
        opportunity_id="opportunity:ordered",
    )

    repair_events = [
        event
        for event in kernel.events(world_id)
        if event.event_type
        in {
            "RepairViolationCommitted",
            "RepairCommitmentCommitted",
            "RepairOpportunityObserved",
            "RepairFollowthroughCommitted",
        }
    ]
    assert [event.event_type for event in repair_events] == [
        "RepairViolationCommitted",
        "RepairCommitmentCommitted",
        "RepairOpportunityObserved",
        "RepairFollowthroughCommitted",
    ]
    assert [event.revision for event in repair_events] == sorted(
        event.revision for event in repair_events
    )
    assert kernel.snapshot(world_id)["emotion_modulation"]["repair_evidence_count"] == 1
    relationship_after_duplicate = kernel.snapshot(world_id)["relationships"]["user:geoff"]
    for dimension in ("respect", "reliability", "trust"):
        assert relationship_after_duplicate.get(dimension) == relationship_after_first.get(
            dimension
        )

    with pytest.raises(WorldError, match="opportunity.*different behavior"):
        _followthrough(
            kernel,
            world_id,
            index=5,
            violation_id="message:repair-message:1",
            commitment_id="commitment:message:repair-message:1",
            opportunity_id="opportunity:ordered",
            behavior_key="different_behavior",
        )
    assert kernel.rebuild_projection(world_id, "world_current_state").matches_live is True
