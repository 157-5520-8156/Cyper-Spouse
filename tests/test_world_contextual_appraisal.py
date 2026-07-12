import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.contextual_appraisal import validate_contextual_appraisal
from companion_daemon.conversation_cadence import ConversationCadence, FrozenTurnContext
from companion_daemon.engine import (
    CompanionEngine,
    contextual_history_for_user,
    seed_user,
)
from companion_daemon.models import IncomingMessage
from companion_daemon.llm import ProviderCircuitBreaker
from companion_daemon.world import WorldKernel


class BoundaryReplyModel:
    async def complete(self, messages, *, temperature: float) -> str:
        return json.dumps(
            {
                "reply_text": "这种说法让我不舒服，我不接受这样贬低我。",
                "mentioned_event_ids": [],
                "proposed_action_ids": [],
                "claims": [],
            },
            ensure_ascii=False,
        )


class AppraisalModel:
    def __init__(self, *, confidence: float) -> None:
        self.confidence = confidence

    async def complete(self, messages, *, temperature: float) -> str:
        return json.dumps(
            {
                "appraisal": "boundary_violation",
                "literal_act": "表面称赞",
                "implied_attitude": "用反讽贬低能力",
                "target": "companion",
                "agency": "user",
                "certainty": 82,
                "goal_congruence": -55,
                "controllability": 45,
                "norm_compatibility": -70,
                "power_delta": -35,
                "confidence": self.confidence,
                "severity": 2,
                "acts": ["sarcasm", "insult"],
                "evidence_spans": ["你可真聪明，连这都做不好"],
                "alternative_appraisal": "可能只是熟人间的玩笑",
            },
            ensure_ascii=False,
        )


def test_contextual_harm_requires_user_agency_and_companion_target() -> None:
    proposal = {
        "appraisal": "boundary_violation", "literal_act": "反讽",
        "implied_attitude": "贬低", "target": "self", "agency": "user",
        "certainty": 90, "goal_congruence": -50, "controllability": 50,
        "norm_compatibility": -50, "power_delta": -20, "confidence": 0.95,
        "severity": 3, "acts": ["sarcasm"], "evidence_spans": ["我可真聪明"],
        "alternative_appraisal": "自嘲",
    }
    accepted = validate_contextual_appraisal(
        json.dumps(proposal, ensure_ascii=False), text="我可真聪明，又做错了。"
    )
    assert accepted.proposed_appraisal == "boundary_violation"
    assert accepted.appraisal == "ordinary_message"


def test_one_character_evidence_cannot_support_contextual_harm() -> None:
    proposal = {
        "appraisal": "boundary_violation", "literal_act": "称呼",
        "implied_attitude": "贬低", "target": "companion", "agency": "user",
        "certainty": 90, "goal_congruence": -50, "controllability": 50,
        "norm_compatibility": -50, "power_delta": -20, "confidence": 0.95,
        "severity": 3, "acts": ["sarcasm"], "evidence_spans": ["你"],
        "alternative_appraisal": "普通称呼",
    }
    accepted = validate_contextual_appraisal(
        json.dumps(proposal, ensure_ascii=False), text="你可真聪明。"
    )
    assert accepted.appraisal == "ordinary_message"


def test_contextual_history_is_private_to_the_active_world_user() -> None:
    history = [
        {"direction": "in", "user_id": "user:alice", "text": "alice-private"},
        {"direction": "out", "user_id": "user:alice", "text": "alice-reply"},
        {"direction": "in", "user_id": "user:bob", "text": "bob-private"},
        {"direction": "out", "user_id": "user:bob", "text": "bob-reply"},
    ]

    scoped = contextual_history_for_user(history, "user:alice")

    assert [item["text"] for item in scoped] == ["alice-private", "alice-reply"]


def _engine(
    tmp_path: Path, *, appraisal_confidence: float
) -> tuple[WorldKernel, str, CompanionEngine]:
    store = CompanionStore(tmp_path / "contextual-appraisal.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        BoundaryReplyModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
        interaction_appraisal_model=AppraisalModel(
            confidence=appraisal_confidence
        ),
    )
    return world, world_id, engine


@pytest.mark.asyncio
async def test_high_confidence_sarcastic_degradation_becomes_sourced_affect(
    tmp_path: Path,
) -> None:
    world, world_id, engine = _engine(tmp_path, appraisal_confidence=0.91)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="sarcasm-high",
            text="你可真聪明，连这都做不好。",
        )
    )

    snapshot = world.snapshot(world_id)
    appraisal = snapshot["last_appraisal"]
    assert appraisal["appraisal"] == "boundary_violation"
    assert appraisal["confidence"] == pytest.approx(0.91)
    assert appraisal["implied_attitude"] == "用反讽贬低能力"
    assert appraisal["power_delta"] == -35
    assert snapshot["emotion_modulation"]["vector"]["hurt"] > 0
    assert snapshot["last_deliberation"]["stance"] in {
        "set_boundary",
        "refuse_to_affirm",
    }
    assert reply is not None
    assert "不接受" in reply.text
    assert any(
        event.event_type == "ModelProposalRecorded"
        and event.payload.get("template_id")
        == "model_output:interaction_appraisal"
        for event in world.events(world_id)
    )


@pytest.mark.asyncio
async def test_low_confidence_sarcasm_does_not_accumulate_relationship_harm(
    tmp_path: Path,
) -> None:
    world, world_id, engine = _engine(tmp_path, appraisal_confidence=0.54)
    before = world.snapshot(world_id)["relationships"].get("user:geoff", {})

    await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="sarcasm-low",
            text="你可真聪明，连这都做不好。",
        )
    )

    snapshot = world.snapshot(world_id)
    assert snapshot["last_appraisal"]["appraisal"] == "ordinary_message"
    assert snapshot["last_appraisal"]["confidence"] == pytest.approx(0.54)
    assert snapshot["last_appraisal"]["alternative_appraisal"]
    after = snapshot["relationships"]["user:geoff"]
    assert int(after.get("respect", 0)) == int(before.get("respect", 0))
    assert snapshot["emotion_modulation"]["violation_count"] == 0


@pytest.mark.asyncio
async def test_explicit_harm_uses_local_rule_without_spending_semantic_model_call(
    tmp_path: Path,
) -> None:
    class MustNotRun:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("explicit insult must not invoke ambiguity model")

    store = CompanionStore(tmp_path / "explicit-local-appraisal.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        BoundaryReplyModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
        interaction_appraisal_model=MustNotRun(),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="explicit-local",
            text="滚，你就是个废物。",
        )
    )

    assert reply is not None
    assert world.snapshot(world_id)["last_appraisal"]["appraisal"] == "boundary_violation"


@pytest.mark.asyncio
async def test_open_real_provider_circuit_skips_appraisal_and_reply_models(
    tmp_path: Path,
) -> None:
    class MustNotRun:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("open provider circuit must use local fallback")

    breaker = ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    breaker.record_failure()
    main_model = MustNotRun()
    main_model.circuit_breaker = breaker
    store = CompanionStore(tmp_path / "open-appraisal-circuit.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        main_model,
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
        interaction_appraisal_model=MustNotRun(),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="open-circuit",
            text="你可真聪明，连这都做不好。",
        )
    )

    assert reply is not None
    assert engine.provider_circuit_state().status == "open"


@pytest.mark.asyncio
async def test_hot_turn_omits_slow_contextual_appraisal_before_reply_generation(
    tmp_path: Path,
) -> None:
    class SlowAppraisalModel:
        cancelled = False

        async def complete(self, messages, *, temperature: float) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    slow = SlowAppraisalModel()
    store = CompanionStore(tmp_path / "bounded-appraisal.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        BoundaryReplyModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
        interaction_appraisal_model=slow,
    )
    observed_at = datetime.now(timezone.utc)
    frozen = FrozenTurnContext(
        turn_id="bounded-appraisal",
        world_id=world_id,
        user_id="user:geoff",
        observed_at=observed_at,
        cadence=ConversationCadence("hot", 2.0, 4, "test_hot_turn"),
    )

    started = monotonic()
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="bounded-appraisal",
            sent_at=observed_at,
            text="你可真聪明，连这都做不好。",
        ),
        turn_context=frozen,
    )

    assert monotonic() - started < 2
    assert slow.cancelled is False
    assert reply is not None
