import pytest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.character_core_evolution import (
    CharacterCoreEvolutionError,
    CoreChangeProposal,
    evaluate_core_change,
)
from companion_daemon.world import WorldError, WorldKernel


BASE_CORE = {
    "id": "zhizhi",
    "name": "沈知栀",
    "kind": "companion",
    "location": "华东师范大学宿舍",
    "stable_traits": ["温和、敏感、观察力强"],
    "values": ["真诚比漂亮话重要"],
}


def evidence(source_id: str, *, signal: str, significant: bool = False) -> dict[str, object]:
    return {
        "source_id": source_id,
        "source_type": "experience",
        "status": "committed",
        "core_signal": signal,
        "significant": significant,
    }


def test_repeated_committed_experience_can_add_a_bounded_trait() -> None:
    proposal = CoreChangeProposal(
        proposal_id="core-change-1",
        operation="add_trait",
        field="stable_traits",
        value="遇到分歧时更愿意坦白表达",
        evidence_ids=("exp-1", "exp-2", "exp-3"),
        reason="连续三次在分歧中坦白表达并完成修复",
    )
    sources = {
        f"exp-{index}": evidence(f"exp-{index}", signal="honest_disagreement")
        for index in range(1, 4)
    }

    decision = evaluate_core_change(BASE_CORE, proposal, sources)

    assert decision.accepted is True
    assert decision.rule_version == "character-core-evolution-v1"
    assert decision.updated_core["name"] == "沈知栀"
    assert proposal.value in decision.updated_core["stable_traits"]
    assert decision.evidence_ids == ("exp-1", "exp-2", "exp-3")


def test_one_significant_goal_or_relationship_outcome_can_propose_a_change() -> None:
    proposal = CoreChangeProposal(
        proposal_id="core-change-goal",
        operation="add_value",
        field="values",
        value="答应的事要留下可核对的结果",
        evidence_ids=("goal-portfolio",),
        reason="重要目标完成后形成的稳定原则",
    )
    sources = {
        "goal-portfolio": {
            "source_id": "goal-portfolio",
            "source_type": "goal_outcome",
            "status": "completed",
            "core_signal": "reliability",
            "significant": True,
        }
    }

    assert evaluate_core_change(BASE_CORE, proposal, sources).accepted is True


@pytest.mark.parametrize("field", ["name", "id", "kind", "location", "school", "background"])
def test_identity_and_biography_fields_cannot_be_rewritten(field: str) -> None:
    proposal = CoreChangeProposal(
        proposal_id=f"rewrite-{field}",
        operation="replace",
        field=field,
        value="另一个身份",
        evidence_ids=("exp-1", "exp-2", "exp-3"),
        reason="模型想自由改角色卡",
    )
    sources = {
        f"exp-{index}": evidence(f"exp-{index}", signal="identity")
        for index in range(1, 4)
    }

    with pytest.raises(CharacterCoreEvolutionError, match="protected character field"):
        evaluate_core_change(BASE_CORE, proposal, sources)


def test_uncommitted_or_single_ordinary_experience_cannot_change_core() -> None:
    proposal = CoreChangeProposal(
        proposal_id="too-soon",
        operation="add_trait",
        field="stable_traits",
        value="从此完全变成另一个人",
        evidence_ids=("exp-1",),
        reason="一次普通经历",
    )

    decision = evaluate_core_change(
        BASE_CORE, proposal, {"exp-1": evidence("exp-1", signal="ordinary")}
    )

    assert decision.accepted is False
    assert decision.reason == "insufficient_repeated_or_significant_evidence"
    assert decision.updated_core == BASE_CORE


def test_evidence_signals_must_agree_and_values_are_length_bounded() -> None:
    proposal = CoreChangeProposal(
        proposal_id="mixed",
        operation="add_trait",
        field="stable_traits",
        value="有自己的新倾向",
        evidence_ids=("exp-1", "exp-2", "exp-3"),
        reason="混在一起的证据",
    )
    sources = {
        "exp-1": evidence("exp-1", signal="honesty"),
        "exp-2": evidence("exp-2", signal="honesty"),
        "exp-3": evidence("exp-3", signal="avoidance"),
    }
    assert evaluate_core_change(BASE_CORE, proposal, sources).accepted is False

    too_long = CoreChangeProposal(
        proposal_id="long",
        operation="add_value",
        field="values",
        value="很" * 81,
        evidence_ids=("exp-1", "exp-2", "exp-3"),
        reason="不受限的模型文本",
    )
    with pytest.raises(CharacterCoreEvolutionError, match="too long"):
        evaluate_core_change(BASE_CORE, too_long, sources)


def test_world_commits_versioned_core_change_only_after_repeated_lived_evidence(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 7, 13, 8, tzinfo=UTC)
    seed = {
        "world_id": "core-world",
        "logical_at": start.isoformat(),
        "protagonist": {
            **BASE_CORE,
            "resources": {"energy": 70, "attention": 55},
            "templates": ["honest_practice"],
        },
        "life_outcome_templates": {
            "honest_practice": {
                "location": "宿舍",
                "energy_cost": 2,
                "content": "认真整理并写下了一次真实想法。",
                "max_per_day": 1,
            }
        },
        "daily_schedule": [
            {
                "slot": "practice",
                "title": "真实表达练习",
                "template_id": "honest_practice",
                "location": "宿舍",
                "starts_hour": 9,
                "ends_hour": 10,
            }
        ],
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    kernel.advance(
        started.world_id,
        start + timedelta(days=3, hours=3),
        expected_revision=started.revision,
    )
    evidence_ids = tuple(kernel.snapshot(started.world_id)["experiences"])
    assert len(evidence_ids) >= 3

    decision = kernel.submit(
        {
            "type": "propose_character_core_change",
            "world_id": started.world_id,
            "proposal_id": "honest-expression-v1",
            "operation": "add_trait",
            "field": "stable_traits",
            "value": "遇到分歧时更愿意坦白表达",
            "evidence_ids": list(evidence_ids[:3]),
            "reason": "三次已提交的真实表达练习形成稳定倾向",
            "idempotency_key": "core-change:honest-expression-v1",
        },
        expected_revision=kernel.revision(started.world_id),
    )

    assert decision.events[0].event_type == "CharacterCoreChanged"
    context = kernel.conversation_context(started.world_id, user_id="user:geoff")
    assert "遇到分歧时更愿意坦白表达" in context["self_core"]["stable_traits"]
    assert context["self_core"]["name"] == "沈知栀"

    with pytest.raises(WorldError, match="protected character field"):
        kernel.submit(
            {
                "type": "propose_character_core_change",
                "world_id": started.world_id,
                "proposal_id": "rewrite-name",
                "operation": "replace",
                "field": "name",
                "value": "另一个人",
                "evidence_ids": list(evidence_ids[:3]),
                "reason": "不应被允许",
            },
            expected_revision=kernel.revision(started.world_id),
        )
