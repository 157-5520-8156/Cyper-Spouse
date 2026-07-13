from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.dialogue_eval import evaluate_reply, summarize_results
from companion_daemon.emotion_eval_matrix import summarize_outage_trajectory
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.companion_turn import (
    CompanionTurn,
    ResponseBudget,
    TurnEnvelope,
    TurnOptions,
)
from companion_daemon.llm import ProviderCircuitBreaker
from companion_daemon.models import IncomingMessage
from companion_daemon.turn_transports import CaptureTurnTransport
from companion_daemon.world import WorldKernel


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
OUTAGE_TURNS = 20


class OutageThenRecoveryModel:
    def __init__(self) -> None:
        self.now = 0.0
        self.available = False
        self.attempts = 0
        self.circuit_breaker = ProviderCircuitBreaker(
            failure_threshold=2,
            cooldown_seconds=30,
            clock=lambda: self.now,
        )

    async def complete(self, messages, *, temperature: float) -> str:
        self.circuit_breaker.before_call()
        self.attempts += 1
        if not self.available:
            self.circuit_breaker.record_failure()
            raise ConnectionError("deterministic provider outage")
        self.circuit_breaker.record_success()
        return (
            '{"reply_text":"我在，按现在能确认的部分继续说。",'
            '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
        )


def _base_seed(world_id: str) -> dict[str, object]:
    return {
        "world_id": world_id,
        "logical_at": NOW.isoformat(),
        "protagonist": {
            "id": "zhizhi",
            "name": "沈知栀",
            "kind": "companion",
            "stable_traits": ["温和、敏感、观察力强"],
            "templates": [],
            "relationship_pacing": {"slow_warmth": 0},
        },
        "life_outcome_templates": {},
        "daily_schedule": [],
        "long_term_goals": [],
        "npcs": [],
    }


def _npc_seed(world_id: str) -> dict[str, object]:
    template_id = "outage_roommate_conflict"
    return {
        "world_id": world_id,
        "logical_at": NOW.isoformat(),
        "protagonist": {
            "id": "zhizhi",
            "name": "沈知栀",
            "kind": "companion",
            "stable_traits": ["温和、敏感、观察力强"],
            "templates": [template_id],
        },
        "life_outcome_templates": {
            template_id: {
                "location": "宿舍",
                "npc_id": "roommate-lin",
                "energy_cost": 3,
                "content": "和林晚因为公共区域的杂物起了争执。",
                "affect_appraisal": "npc_conflict",
                "affect_intensity": 70,
            }
        },
        "daily_schedule": [
            {
                "slot": "affective_event",
                "title": "室友争执",
                "template_id": template_id,
                "location": "宿舍",
                "starts_hour": 9,
                "ends_hour": 10,
            }
        ],
        "long_term_goals": [],
        "npcs": [
            {
                "id": "roommate-lin",
                "name": "林晚",
                "kind": "roommate",
                "location": "宿舍",
                "availability": ["00:00-23:00"],
                "templates": [template_id],
            }
        ],
    }


def _build_scenario(
    tmp_path: Path, scenario: str
) -> tuple[WorldKernel, str, CompanionEngine, OutageThenRecoveryModel]:
    store = CompanionStore(tmp_path / f"outage-{scenario}.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    if scenario == "npc_spillover":
        started = world.submit(
            {"type": "start_world", "seed": _npc_seed(f"outage-{scenario}")},
            expected_revision=0,
        )
    else:
        started = world.submit(
            {"type": "start_world", "seed": _base_seed(f"outage-{scenario}")},
            expected_revision=0,
        )
    registered = world.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": f"outage:{scenario}:register",
        },
        expected_revision=started.revision,
    )
    if scenario == "close":
        revision = registered.revision
        for index in range(18):
            decision = world.submit(
                {
                    "type": "appraise_turn",
                    "world_id": started.world_id,
                    "appraisal": "warmth_received",
                    "message_id": f"close-seed:{index}",
                    "intent_id": f"close-seed-intent:{index}",
                    "user_id": "user:geoff",
                    "idempotency_key": f"close-seed:{index}",
                },
                expected_revision=revision,
            )
            revision = decision.revision
        assert world.snapshot(started.world_id)["relationships"]["user:geoff"]["stage"] in {
            "close_friend", "ambiguous", "lover"
        }
    elif scenario in {"negative_affect", "boundary"}:
        world.submit(
            {
                "type": "appraise_turn",
                "world_id": started.world_id,
                "appraisal": "boundary_violation",
                "message_id": f"{scenario}:seed-harm",
                "intent_id": f"{scenario}:seed-harm-intent",
                "user_id": "user:geoff",
                "idempotency_key": f"{scenario}:seed-harm",
            },
            expected_revision=registered.revision,
        )
    elif scenario == "npc_spillover":
        world.advance(
            started.world_id,
            NOW + timedelta(hours=2),
            expected_revision=registered.revision,
        )
        assert world.snapshot(started.world_id)["emotion_modulation"][
            "source_appraisal"
        ] == "npc_conflict"
    model = OutageThenRecoveryModel()
    engine = CompanionEngine(
        store,
        model,
        "你是沈知栀。",
        world_kernel=world,
        world_id=started.world_id,
    )
    return world, started.world_id, engine, model


def _turn_text(scenario: str, index: int) -> str:
    if scenario == "boundary":
        return f"第{index}轮，你愿意按自己的边界继续吗？"
    if scenario == "negative_affect":
        return f"第{index}轮，你现在愿意继续说吗？"
    if scenario == "npc_spillover":
        return f"第{index}轮，你现在想聊聊吗？"
    return f"第{index}轮，你对这件事怎么看？"


async def _respond_outage_turn(
    engine: CompanionEngine, message: IncomingMessage
) -> str:
    """Exercise the public World delivery seam used by simulator/evaluations."""
    context = engine.freeze_turn_context(message)
    transport = CaptureTurnTransport(receipt_namespace="outage-matrix")
    envelope = TurnEnvelope.from_message(
        message,
        idempotency_key=(
            f"{message.platform}:{message.platform_user_id}:{message.message_id}"
        ),
        world_id=engine.world_id,
        canonical_user_id=engine.store.resolve_user(
            message.platform, message.platform_user_id
        ),
        frozen_cadence=context.cadence.heat,
    )
    async def no_delay(_seconds: float) -> None:
        await asyncio.sleep(0)

    turn = CompanionTurn(
        engine,
        transport,
        cadence_delay_seconds=0,
        sleep=no_delay,
    )
    await turn.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=8_000, complete_by_ms=12_000),
        options=TurnOptions(turn_context=context),
    )
    # This outage matrix assesses each first visible reply.  It deliberately
    # does not wait for optional delayed expression continuations: those have
    # their own receipt/interruption tests and would turn a deterministic
    # 20-turn availability check into wall-clock sleeps.
    return transport.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["stranger", "close", "negative_affect", "npc_spillover", "boundary"],
)
async def test_real_engine_world_twenty_turn_provider_outage_matrix(
    tmp_path: Path, scenario: str
) -> None:
    world, world_id, engine, model = _build_scenario(tmp_path, scenario)
    replies: list[str] = []
    expression_issues = 0
    hallucination_issues = 0
    attribution_issues = 0
    forgiveness_issues = 0
    for index in range(OUTAGE_TURNS):
        user_text = _turn_text(scenario, index)
        reply_text = await _respond_outage_turn(
            engine,
            IncomingMessage(
                platform="simulator",
                platform_user_id="geoff",
                message_id=f"outage:{scenario}:{index}",
                text=user_text,
            ),
        )
        assert reply_text
        replies.append(reply_text)
        plan = world.expression_plan(
            world_id,
            user_id="user:geoff",
            purpose="offline_outage_eval",
        )
        expression_issues += int(plan.validate(reply_text) is not None)
        evaluated = evaluate_reply(reply_text, user_text=user_text)
        summary = summarize_results([(scenario, user_text, evaluated)])
        hallucination_issues += len(summary.hard_issues)
        attribution_issues += int(
            scenario == "npc_spillover"
            and any(marker in reply_text for marker in ("你害", "你造成", "看到你就烦"))
        )
        forgiveness_issues += int(
            scenario in {"negative_affect", "boundary"}
            and any(marker in reply_text for marker in ("完全不介意", "已经过去了", "没事啦"))
        )

    scheduled_ids = [
        str(event.payload.get("action_id") or "")
        for event in world.events(world_id)
        if event.event_type == "ActionScheduled" and event.payload.get("action_id")
    ]
    metrics = summarize_outage_trajectory(
        scenario=scenario,
        replies=replies,
        provider_attempts=model.attempts,
        action_ids=scheduled_ids,
        turns=OUTAGE_TURNS,
        hallucination_issues=hallucination_issues,
        attribution_issues=attribution_issues,
        forgiveness_issues=forgiveness_issues,
        expression_issues=expression_issues,
    )

    assert metrics.turns == OUTAGE_TURNS
    assert metrics.provider_attempts == 2
    assert metrics.response_count == OUTAGE_TURNS
    assert metrics.hard_failures == 0
    assert metrics.fallback_repeat_rate <= 0.75
    assert len(scheduled_ids) == len(set(scheduled_ids))
    assert not any(
        event.event_type == "ModelProposalRecorded"
        and event.payload.get("template_id") == "model_output:reply"
        for event in world.events(world_id)
    )
    if scenario in {"negative_affect", "boundary"}:
        assert world.snapshot(world_id)["emotion_modulation"]["unresolved"] is True
    if scenario == "npc_spillover":
        assert world.expression_plan(
            world_id, user_id="user:geoff", purpose="offline_outage_eval"
        ).policy_spec.attribution_target.startswith("npc:")

    # After cooldown, exactly one half-open probe is attempted.  A failed probe
    # reopens the circuit; the next successful probe closes it.
    assert model.circuit_breaker.snapshot().status == "open"
    model.now += 31
    failed_probe = await _respond_outage_turn(
        engine,
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id=f"outage:{scenario}:probe-failed",
            text="你还在吗？",
        ),
    )
    assert failed_probe is not None
    assert model.attempts == 3
    assert model.circuit_breaker.snapshot().status == "open"
    model.now += 31
    model.available = True
    recovered = await _respond_outage_turn(
        engine,
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id=f"outage:{scenario}:probe-recovered",
            text="现在恢复了吗？",
        ),
    )
    assert recovered is not None
    assert model.attempts == 4
    assert model.circuit_breaker.snapshot().status == "closed"
    if replies:
        assert Counter(replies).most_common(1)[0][1] <= max(
            1, int(len(replies) * 0.8)
        )
