from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.world import (
    AcceptedTurn,
    CommittedAppraisal,
    WorldError,
    WorldKernel,
)


def test_expression_plan_is_compiled_from_current_revision_not_stale_last_display(
    tmp_path: Path,
) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "expression-plan.sqlite"))
    now = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    started = kernel.submit(
        {
            "type": "start_world",
            "seed": {
                "world_id": "expression-plan-world",
                "logical_at": now.isoformat(),
                "protagonist": {
                    "id": "zhizhi",
                    "name": "沈知栀",
                    "kind": "companion",
                    "stable_traits": ["温和、敏感"],
                    "templates": ["roommate_conflict"],
                },
                "life_outcome_templates": {
                    "roommate_conflict": {
                        "location": "宿舍",
                        "npc_id": "roommate",
                        "energy_cost": 2,
                        "content": "和室友发生了争执。",
                        "affect_appraisal": "npc_conflict",
                        "affect_intensity": 70,
                    }
                },
                "daily_schedule": [
                    {
                        "slot": "conflict",
                        "title": "室友争执",
                        "template_id": "roommate_conflict",
                        "location": "宿舍",
                        "starts_hour": 9,
                        "ends_hour": 10,
                    }
                ],
                "long_term_goals": [],
                "npcs": [
                    {
                        "id": "roommate",
                        "name": "室友",
                        "kind": "roommate",
                        "location": "宿舍",
                        "availability": ["00:00-23:59"],
                        "templates": ["roommate_conflict"],
                    }
                ],
            },
        },
        expected_revision=0,
    )
    user_id = "user:geoff"
    kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": user_id,
            "name": "geoff",
            "idempotency_key": "register-expression-user",
        },
        expected_revision=started.revision,
    )
    before_revision = kernel.revision(started.world_id)
    before = kernel.expression_plan(
        started.world_id,
        user_id=user_id,
        purpose="reply",
        intent_id="before-npc-conflict",
        expected_revision=before_revision,
    )

    kernel.advance(
        started.world_id,
        now + timedelta(hours=2),
        expected_revision=before_revision,
    )

    after_revision = kernel.revision(started.world_id)
    after = kernel.expression_plan(
        started.world_id,
        user_id=user_id,
        purpose="reply",
        intent_id="after-npc-conflict",
        expected_revision=after_revision,
    )

    assert before.policy_spec.regulation_strategy == "natural_expression"
    assert after.policy_spec.regulation_strategy == "contain_spillover"
    assert after.policy_spec.attribution_target == "npc:roommate"
    assert after.revision == after_revision
    assert before.plan_hash != after.plan_hash
    with pytest.raises(WorldError, match="revision"):
        kernel.expression_plan(
            started.world_id,
            user_id=user_id,
            purpose="reply",
            expected_revision=before_revision,
        )


def test_typed_accept_turn_hides_the_world_command_schema(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "accepted-turn.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "accept-turn-user",
        },
        expected_revision=started.revision,
    )

    decision = kernel.accept_turn(
        AcceptedTurn(
            world_id=started.world_id,
            user_id="user:geoff",
            message_id="typed-turn",
            intent_id="turn:typed-turn",
            appraisal=CommittedAppraisal(
                kind="boundary_violation",
                severity=3,
                target="companion",
                acts=("insult",),
                evidence_spans=("你真蠢",),
            ),
            expected_revision=kernel.revision(started.world_id),
        )
    )

    assert decision.events[0].event_type == "TurnAppraised"
    assert kernel.snapshot(started.world_id)["last_appraisal"]["appraisal"] == "boundary_violation"
