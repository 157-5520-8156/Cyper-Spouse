from companion_daemon.affective_advisory import (
    AffectAdvisory,
    AffectiveAdvisoryEngine,
    ExpressionAffordance,
    ModelAffectReader,
    SelectedAffordance,
    advisory_from_model_json,
    select_affordance,
)
from companion_daemon.companion_turn import (
    CompanionTurn,
    ResponseBudget,
    TurnEnvelope,
    TurnOptions,
)
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage
from companion_daemon.turn_frame import TurnFrame, TurnFrameCompiler
from companion_daemon.turn_transports import CaptureTurnTransport
from companion_daemon.world import WorldKernel

from pathlib import Path

import pytest
import json


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


def test_companion_targeted_degradation_creates_hurt_boundary_affordances() -> None:
    advisory = _run(_frame(text="滚，你就是个废物。"))

    readings = {item.kind: item for item in advisory.readings}
    assert "companion_targeted_degradation" in readings
    assert readings["companion_targeted_degradation"].target == "companion"
    assert advisory.drive_deltas["hurt"] > 0
    assert advisory.drive_deltas["anger"] > 0
    assert advisory.drive_deltas["dignity"] > advisory.drive_deltas.get("care", 0)
    kinds = {item.kind for item in advisory.expression_affordances}
    assert "set_boundary" in kinds
    assert "withdraw_slightly" in kinds
    assert "care_despite_hurt" in kinds
    assert "approach" not in kinds


def test_quoted_or_self_targeted_degradation_is_not_treated_as_attack_on_companion() -> None:
    quoted = _run(_frame(text="他说我蠢，我听完挺难受的。"))
    self_blame = _run(_frame(text="我真蠢，居然又忘记保存了。"))

    assert "companion_targeted_degradation" not in {
        item.kind for item in quoted.readings
    }
    assert "companion_targeted_degradation" not in {
        item.kind for item in self_blame.readings
    }
    assert "warmth_received" in {item.kind for item in quoted.readings}


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
    frame = _frame(
        text="嗯",
        message_id="m:next",
    )
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


def test_selected_affordance_shapes_expression_choreography() -> None:
    soft = _advisory_with_selected("soft_repair")
    parts, delays = CompanionEngine._apply_affective_expression_choreography(
        "我刚才确实接得急了。你可以慢慢说。",
        ["我刚才确实接得急了。", "你可以慢慢说。"],
        [],
        affective_advisory=soft,
    )

    assert parts == ["我刚才确实接得急了。", "你可以慢慢说。"]
    assert delays == [0, 650]

    withdrawn = _advisory_with_selected("withdraw_slightly")
    parts, delays = CompanionEngine._apply_affective_expression_choreography(
        "我不接受这种说法。先到这里。",
        ["我不接受这种说法。", "先到这里。"],
        [],
        affective_advisory=withdrawn,
    )

    assert parts == ["我不接受这种说法。先到这里。"]
    assert delays == [0]


def test_strong_quote_bound_advisory_can_promote_to_user_affect_but_mild_cannot() -> None:
    strong = _run(_frame(text="你刚才有点敷衍。", message_id="m:strong"))

    promoted = CompanionEngine._material_user_affect_from_advisory(
        message=IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="m:strong",
            text="你刚才有点敷衍。",
        ),
        affective_advisory=strong,
        existing_user_affect=None,
    )

    assert promoted is not None
    assert promoted["kind"] == "disappointment"
    assert promoted["intensity"] == 3
    assert promoted["evidence_spans"] == ["敷衍"]

    mild = _run(_frame(text="算了吧", message_id="m:mild"))
    assert (
        CompanionEngine._material_user_affect_from_advisory(
            message=IncomingMessage(
                platform="qq",
                platform_user_id="geoff",
                message_id="m:mild",
                text="算了吧",
            ),
            affective_advisory=mild,
            existing_user_affect=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_world_reply_action_trace_records_affective_affordance_selection(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "affective-trace.sqlite")
    seed_user(store)
    store.map_account("qq", "geoff", "geoff")
    kernel = WorldKernel(store)
    world_id = kernel.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=kernel,
        world_id=world_id,
    )
    message = IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id="affective-trace",
        text="你刚才有点敷衍，我有点失望。",
    )

    context = engine.freeze_turn_context(message)
    transport = CaptureTurnTransport(receipt_namespace="affective-trace")
    envelope = TurnEnvelope.from_message(
        message,
        idempotency_key="qq:geoff:affective-trace",
        world_id=world_id,
        canonical_user_id="geoff",
        frozen_cadence=context.cadence.heat,
    )
    turn = CompanionTurn(engine, transport, cadence_delay_seconds=0)
    await turn.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=8_000, complete_by_ms=12_000),
        options=TurnOptions(turn_context=context),
    )
    await turn.wait_for_delivery_continuations()

    action = next(
        item
        for item in kernel.snapshot(world_id)["actions"].values()
        if item.get("trace", {}).get("input_message_id") == "qq:geoff:affective-trace"
    )
    advisory = action["trace"]["affective_advisory"]
    assert advisory["selection"]["selected"]["kind"] in {
        "soft_repair",
        "gentle_check_in",
        "let_it_pass",
        "playful_deflect",
        "withdraw_slightly",
    }
    assert advisory["selection"]["seed_hash"]
    assert any(
        item["kind"] == "possible_disappointment"
        for item in advisory["readings"]
    )
    assert any(
        item["kind"] == "possible_disappointment"
        for item in kernel.snapshot(world_id)["private_impressions"].values()
    )
    affect = kernel.snapshot(world_id)["user_affect"]["user:geoff"]
    assert affect["kind"] == "disappointment"
    assert affect["source"] == "affective_advisory"
    assert "敷衍" in affect["evidence_spans"]
    event_types = [event.event_type for event in kernel.events(world_id)]
    assert "ExpressionAffordanceSelected" in event_types
    assert "UserAffectAppraised" in event_types
    projected = kernel.snapshot(world_id)["last_expression_affordance"]
    assert projected["action_id"] == action["action_id"]
    assert projected["selected"]["kind"] == advisory["selection"]["selected"]["kind"]


def _run(frame: TurnFrame):
    import asyncio

    return asyncio.run(AffectiveAdvisoryEngine().advise(frame))


def _advisory_with_selected(kind: str) -> AffectAdvisory:
    selected = ExpressionAffordance(kind, 1.0, "test")
    return AffectAdvisory(
        readings=(),
        drive_deltas={},
        expression_affordances=(selected,),
        persistence_candidates=(),
        confidence=0,
        evidence_spans=(),
        adapter="test",
        selected_affordance=SelectedAffordance(selected, (selected,), "seed"),
    )
