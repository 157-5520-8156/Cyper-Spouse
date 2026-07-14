import asyncio
import json

import pytest

from companion_daemon.affective_advisory import (
    AffectiveAdvisoryEngine,
    ExpressionAffordance,
    ModelAffectReader,
    advisory_from_model_json,
    select_affordance,
)
from companion_daemon.turn_frame import TurnFrame, TurnFrameCompiler


def _frame(
    *,
    text: str = "算了吧，你刚才有点敷衍",
    stage: str = "stranger",
    user_affect: dict[str, object] | None = None,
    affect: dict[str, object] | None = None,
    message_id: str = "m:1",
) -> TurnFrame:
    return TurnFrame(
        world_id="zhizhi-v1",
        revision=42,
        state_hash="state",
        user_id="user:geoff",
        input_message_id=message_id,
        recent_messages=(),
        scene={},
        relationship={"stage": stage},
        affect=affect or {},
        user_affect=user_affect or {},
        private_impressions=(),
        private_commitments=(),
        facts=(),
        experiences=(),
        open_threads=(),
        open_actions=(),
        capability={"current_text": text},
        dependency_tokens=(),
    )


def _run(frame: TurnFrame):
    return asyncio.run(AffectiveAdvisoryEngine().advise(frame))


def test_possible_disappointment_creates_affordance_distribution_not_fixed_comfort() -> None:
    advisory = _run(_frame())

    assert {item.kind for item in advisory.readings} == {"possible_disappointment"}
    kinds = {item.kind for item in advisory.expression_affordances}
    assert "soft_repair" in kinds
    assert "let_it_pass" in kinds
    assert "withdraw_slightly" in kinds
    assert advisory.drive_deltas["repair"] > 0
    assert advisory.drive_deltas["autonomy"] > 0
    assert advisory.selected_affordance.selected is not None


def test_control_pressure_shifts_toward_dignity_and_boundary() -> None:
    advisory = _run(_frame(text="你必须马上回答，照做，不准拒绝"))

    kinds = {item.kind for item in advisory.expression_affordances}
    assert "set_boundary" in kinds
    assert advisory.drive_deltas["dignity"] > advisory.drive_deltas.get("repair", 0)
    assert advisory.drive_deltas["autonomy"] > 0


def test_world_stress_can_modulate_expression_without_user_blame() -> None:
    advisory = _run(
        _frame(
            text="我今天回家了",
            affect={"behavior_tendency": "tense", "source_appraisal": "npc_conflict"},
        )
    )

    world_readings = [item for item in advisory.readings if item.kind == "world_stress"]
    assert world_readings
    assert world_readings[0].target == "world"
    assert "shorter_reply" in {item.kind for item in advisory.expression_affordances}


def test_affordance_sampling_is_replay_stable_and_message_sensitive() -> None:
    candidates = (
        ExpressionAffordance("soft_repair", 0.34, "repair"),
        ExpressionAffordance("let_it_pass", 0.18, "restraint"),
        ExpressionAffordance("withdraw_slightly", 0.08, "self-protection"),
    )

    first = select_affordance("world", 7, "message-a", candidates)
    repeat = select_affordance("world", 7, "message-a", candidates)
    other = select_affordance("world", 7, "message-b", candidates)

    assert first.to_trace() == repeat.to_trace()
    assert first.seed_hash != other.seed_hash
    assert first.selected is not None


def test_last_expression_affordance_reenters_next_turn_advisories() -> None:
    frame = _frame(text="嗯", message_id="m:next")
    frame = TurnFrame(
        **{
            **frame.__dict__,
            "capability": {
                **frame.capability,
                "last_expression_affordance": {
                    "action_id": "outgoing:1",
                    "input_message_id": "message:before",
                    "selected": {"kind": "soft_repair"},
                },
            },
        }
    )

    advisories = TurnFrameCompiler().advisories(frame)

    assert any(
        item.kind == "rhythm" and "soft_repair" in item.tendency
        for item in advisories
    )


def test_current_text_is_internal_to_affective_reader_not_exported_to_prompt() -> None:
    frame = _frame(text="这句话只应该给情绪机读，不应该在 TurnFrame prompt 里重复出现")

    assert frame.capability["current_text"]
    assert "current_text" not in frame.prompt_payload()["capability"]
    assert "current_text" not in frame.prompt_delta()["capability"]


def test_model_affect_reader_parser_accepts_bounded_schema_only() -> None:
    frame = _frame(text="呵呵，行吧")

    advisory = advisory_from_model_json(
        frame,
        json.dumps(
            {
                "readings": [
                    {
                        "kind": "ambiguous_tease",
                        "target": "relationship",
                        "intensity": 2,
                        "confidence": 0.66,
                        "evidence_spans": ["呵呵"],
                        "stakes": {"relationship": 0.4},
                    },
                    {
                        "kind": "invented_world_fact",
                        "target": "user",
                        "intensity": 4,
                        "confidence": 1.0,
                        "evidence_spans": ["bad"],
                    },
                ],
                "drive_deltas": {"avoidance": 0.2, "care": 2.0},
                "expression_affordances": [
                    {"kind": "let_it_pass", "weight": 0.4, "reason": "ambiguous"},
                    {"kind": "invent_new_fact", "weight": 1.0, "reason": "bad"},
                ],
            },
            ensure_ascii=False,
        ),
    )

    assert advisory is not None
    assert [item.kind for item in advisory.readings] == ["ambiguous_tease"]
    assert advisory.drive_deltas["care"] == 1.0
    assert [item.kind for item in advisory.expression_affordances] == ["let_it_pass"]


@pytest.mark.asyncio
async def test_model_affect_reader_is_optional_and_mergeable() -> None:
    class Model:
        async def complete(self, messages, *, temperature: float = 0.2) -> str:
            return json.dumps(
                {
                    "readings": [
                        {
                            "kind": "ambiguous_tease",
                            "target": "relationship",
                            "intensity": 2,
                            "confidence": 0.7,
                            "evidence_spans": ["行吧"],
                        }
                    ],
                    "drive_deltas": {"avoidance": 0.1},
                    "expression_affordances": [
                        {"kind": "let_it_pass", "weight": 0.2, "reason": "ambiguous"}
                    ],
                },
                ensure_ascii=False,
            )

    advisory = await AffectiveAdvisoryEngine(ModelAffectReader(Model())).advise(
        _frame(text="行吧")
    )

    assert "rule+model" == advisory.adapter
    assert any(item.kind == "ambiguous_tease" for item in advisory.readings)
