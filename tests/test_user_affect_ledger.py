import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import json

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.companion_turn import (
    CompanionTurn,
    ResponseBudget,
    TurnEnvelope,
    TurnOptions,
)
from companion_daemon.conversation_cadence import ConversationCadence, FrozenTurnContext
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage
from companion_daemon.turn_transports import CaptureTurnTransport
from companion_daemon.turn_frame import TurnFrameCompiler
from companion_daemon.world import WorldKernel
from companion_daemon.emotion_state import InteractionEvent
from companion_daemon.interaction_appraiser import (
    InteractionAppraiser,
    InteractionEvidence,
    TurnAppraisalInput,
    assess_appraisal_risk,
)


def _engine(tmp_path: Path) -> tuple[CompanionEngine, CompanionStore]:
    store = CompanionStore(tmp_path / "user-affect.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    return (
        CompanionEngine(
            store,
            FakeCompanionModel(),
            "你是知栀。",
            world_kernel=world,
            world_id=world_id,
        ),
        store,
    )


async def _respond_world_turn(
    engine: CompanionEngine,
    message: IncomingMessage,
    *,
    turn_context: FrozenTurnContext | None = None,
):
    """Exercise World generation and authoritative receipt settlement."""

    context = turn_context or engine.freeze_turn_context(message)
    transport = CaptureTurnTransport(receipt_namespace="user-affect-ledger")
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
    turn = CompanionTurn(engine, transport, cadence_delay_seconds=0)
    outcome = await turn.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=8_000, complete_by_ms=12_000),
        options=TurnOptions(turn_context=context),
    )
    await turn.wait_for_delivery_continuations()
    return outcome


def _source_message_id(message_id: str) -> str:
    """World records platform-scoped source IDs at the public turn boundary."""

    return f"qq:geoff:{message_id}"


@pytest.mark.asyncio
async def test_withdrawing_after_an_unmet_share_is_appraised_and_kept_until_repaired(
    tmp_path: Path,
) -> None:
    engine, store = _engine(tmp_path)
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="今天和朋友去玩密室了",
            message_id="share-1",
        )
    )

    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="算了，你看你的书吧",
            message_id="withdraw-1",
        )
    )

    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["appraisal"] == "user_withdrawing"
    snapshot = engine.world_kernel.snapshot(engine.world_id)
    affect = snapshot["user_affect"]["user:geoff"]
    assert affect["kind"] == "disappointment"
    assert affect["intensity"] == 3
    assert affect["unresolved"] is True
    assert any(
        event.event_type == "UserAffectAppraised"
        and event.payload["source_message_id"] == _source_message_id("withdraw-1")
        for event in engine.world_kernel.events(engine.world_id)
    )


@pytest.mark.asyncio
async def test_confusion_about_the_companion_requests_repair_instead_of_curiosity(
    tmp_path: Path,
) -> None:
    engine, store = _engine(tmp_path)
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我今天去玩密室了", message_id="share-2"
        )
    )

    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="什么意思？我没懂",
            message_id="confused-1",
        )
    )

    assert store.recent_turn_traces("geoff")[-1]["appraisal"] == "user_confused"
    affect = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert affect["kind"] == "confusion"
    assert affect["unresolved"] is True


@pytest.mark.asyncio
async def test_new_confusion_does_not_implicitly_settle_prior_disappointment(
    tmp_path: Path,
) -> None:
    engine, _ = _engine(tmp_path)
    for message_id, text in (
        ("share-mixed", "我今天去玩密室了"),
        ("withdraw-mixed", "算了，你看你的书吧"),
        ("confused-mixed", "什么意思？我没懂"),
    ):
        await _respond_world_turn(
            engine,
            IncomingMessage(
                platform="qq", platform_user_id="geoff", text=text, message_id=message_id
            )
        )

    affect = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert affect["kind"] == "confusion"
    assert affect["settles_source_message_id"] == ""
    assert {item["source_message_id"] for item in affect["active_episodes"]} == {
        _source_message_id("withdraw-mixed"),
        _source_message_id("confused-mixed"),
    }

    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="没事了，这次你接住了",
            message_id="repair-mixed",
        )
    )
    repaired = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert repaired["kind"] == "repaired"
    assert repaired["unresolved"] is False
    assert repaired["active_episodes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolved_text",
    ["我有点失望，不过没事了", "你刚才有点敷衍，不过现在没事了"],
)
async def test_mild_disappointment_resolved_in_the_same_turn_is_not_ledgered(
    tmp_path: Path, resolved_text: str,
) -> None:
    engine, _ = _engine(tmp_path)
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我今天去玩密室了", message_id="share-3"
        )
    )
    revision_before = engine.world_kernel.revision(engine.world_id)

    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text=resolved_text,
            message_id="mild-resolved-1",
        )
    )

    new_events = [
        event
        for event in engine.world_kernel.events(engine.world_id)
        if event.revision > revision_before
    ]
    assert all(event.event_type != "UserAffectAppraised" for event in new_events)
    assert "user:geoff" not in engine.world_kernel.snapshot(engine.world_id)["user_affect"]


@pytest.mark.asyncio
async def test_explicit_disappointment_with_natural_intensifiers_is_not_missed(
    tmp_path: Path,
) -> None:
    engine, _ = _engine(tmp_path)
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我想分享件事", message_id="share-intensified"
        )
    )
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="你刚才真的有点敷衍，我挺失望的。",
            message_id="disappointed-intensified",
        )
    )

    affect = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert affect["kind"] == "disappointment"
    assert affect["unresolved"] is True


@pytest.mark.asyncio
async def test_ambiguous_low_energy_reply_keeps_committed_disappointment_active(
    tmp_path: Path,
) -> None:
    engine, store = _engine(tmp_path)
    for message_id, text in (
        ("share-low-energy", "我今天去玩密室了"),
        ("withdraw-low-energy", "算了，你看你的书吧"),
        ("terse-low-energy", "还行吧"),
    ):
        await _respond_world_turn(
            engine,
            IncomingMessage(
                platform="qq", platform_user_id="geoff", text=text, message_id=message_id
            )
        )

    assert store.recent_turn_traces("geoff")[-1]["appraisal"] == "user_withdrawing"
    affect = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert affect["kind"] == "disappointment"
    assert affect["source_message_id"] == _source_message_id("terse-low-energy")
    assert affect["unresolved"] is True
    assert [item["source_message_id"] for item in affect["active_episodes"]] == [
        _source_message_id("terse-low-energy")
    ]


@pytest.mark.asyncio
async def test_hot_implicit_disappointment_is_ledgered_before_the_same_reply(
    tmp_path: Path,
) -> None:
    engine, store = _engine(tmp_path)

    class AppraisalProbe:
        calls = 0

        async def complete(self, messages, *, temperature):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise AssertionError("hot local affect should not request an appraisal model")

    appraisal_probe = AppraisalProbe()
    engine.interaction_appraisal_model = appraisal_probe
    engine.interaction_deep_appraisal_model = appraisal_probe
    first = await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="今天终于把那个等了很久的 offer 拿到了，心里特别复杂。",
            message_id="hot-share",
        )
    )
    assert first is not None

    reply = await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="行吧", message_id="hot-implicit"
        ),
        turn_context=FrozenTurnContext(
            turn_id="hot-implicit",
            world_id=engine.world_id,
            user_id="user:geoff",
            observed_at=datetime.fromisoformat(
                engine.world_kernel.snapshot(engine.world_id)["clock"]["logical_at"]
            ),
            cadence=ConversationCadence("hot", 2.0, 2, "active_back_and_forth"),
        ),
    )

    assert reply is not None
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["appraisal"] == "user_withdrawing"
    affect = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert affect["kind"] == "disappointment"
    assert affect["intensity"] == 2
    assert affect["source_message_id"] == _source_message_id("hot-implicit")
    await asyncio.gather(*tuple(engine._appraisal_tasks))
    assert appraisal_probe.calls == 0
    # The primary reply, not a trailing appraiser task, receives the repair
    # direction in the same hot turn.
    prompt = engine.model.calls[-1][1]["content"]
    assert '"kind":"repair"' in prompt


@pytest.mark.asyncio
async def test_repeated_mild_disappointment_crosses_ledger_threshold_only_on_repeat(
    tmp_path: Path,
) -> None:
    engine, _ = _engine(tmp_path)
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我想分享件事", message_id="mild-share"
        )
    )
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="有点扫兴", message_id="mild-first"
        )
    )
    first_events = [
        event
        for event in engine.world_kernel.events(engine.world_id)
        if event.event_type == "UserAffectAppraised"
    ]
    assert first_events == []

    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="还是有点扫兴", message_id="mild-repeat"
        )
    )
    affect = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert affect["intensity"] == 2
    assert affect["source_message_id"] == _source_message_id("mild-repeat")


@pytest.mark.asyncio
async def test_committed_disappointment_is_closed_by_an_explicit_repair_settlement(
    tmp_path: Path,
) -> None:
    engine, _ = _engine(tmp_path)
    for message_id, text in (
        ("share-4", "我今天去玩密室了"),
        ("withdraw-2", "算了，不说了"),
        ("settled-1", "没事了，这次你接住了"),
    ):
        await _respond_world_turn(
            engine,
            IncomingMessage(
                platform="qq", platform_user_id="geoff", text=text, message_id=message_id
            )
        )

    affect = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert affect["kind"] == "repaired"
    assert affect["unresolved"] is False
    assert affect["settles_source_message_id"] == _source_message_id("withdraw-2")
    assert (
        engine.world_kernel.rebuild_projection(engine.world_id, "world_current_state").matches_live
        is True
    )


@pytest.mark.asyncio
async def test_logical_weeks_expire_old_user_disappointment_before_an_unrelated_turn(
    tmp_path: Path,
) -> None:
    engine, _ = _engine(tmp_path)
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我想分享件事", message_id="expiry-share"
        )
    )
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="算了，不说了", message_id="expiry-withdraw"
        )
    )

    before = engine.world_kernel.snapshot(engine.world_id)
    expires_at = before["user_affect"]["user:geoff"]["expires_at"]
    assert expires_at == (
        datetime.fromisoformat(before["clock"]["logical_at"]) + timedelta(days=7)
    ).isoformat()
    engine.world_kernel.advance(
        engine.world_id,
        datetime.fromisoformat(before["clock"]["logical_at"]) + timedelta(days=21),
        expected_revision=engine.world_kernel.revision(engine.world_id),
    )

    after = engine.world_kernel.snapshot(engine.world_id)
    assert "user:geoff" not in after["user_affect"]
    assert any(
        event.event_type == "UserAffectExpired"
        and event.payload["source_message_id"] == _source_message_id("expiry-withdraw")
        for event in engine.world_kernel.events(engine.world_id)
    )
    frame = TurnFrameCompiler().compile(
        world_id=engine.world_id,
        revision=engine.world_kernel.revision(engine.world_id),
        state_hash="expiry-state",
        snapshot=after,
        user_id="user:geoff",
        message=IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我今天换了份工作", message_id="expiry-now"
        ),
    )
    assert not any(item.kind == "repair" for item in TurnFrameCompiler().advisories(frame))


@pytest.mark.asyncio
async def test_fresh_or_reinforced_user_disappointment_still_guides_the_next_turn(
    tmp_path: Path,
) -> None:
    engine, _ = _engine(tmp_path)
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我想分享件事", message_id="renew-share"
        )
    )
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="算了，不说了", message_id="renew-withdraw"
        )
    )
    first = engine.world_kernel.snapshot(engine.world_id)
    first_expires_at = first["user_affect"]["user:geoff"]["expires_at"]
    engine.world_kernel.advance(
        engine.world_id,
        datetime.fromisoformat(first["clock"]["logical_at"]) + timedelta(days=6),
        expected_revision=engine.world_kernel.revision(engine.world_id),
    )
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我还是有点失望", message_id="renew-low-energy"
        )
    )

    renewed = engine.world_kernel.snapshot(engine.world_id)
    affect = renewed["user_affect"]["user:geoff"]
    assert affect["source_message_id"] == _source_message_id("renew-low-energy")
    assert affect["expires_at"] > first_expires_at
    frame = TurnFrameCompiler().compile(
        world_id=engine.world_id,
        revision=engine.world_kernel.revision(engine.world_id),
        state_hash="renewed-state",
        snapshot=renewed,
        user_id="user:geoff",
        message=IncomingMessage(
            platform="qq", platform_user_id="geoff", text="继续说", message_id="renew-now"
        ),
    )
    assert any(item.kind == "repair" for item in TurnFrameCompiler().advisories(frame))


def test_only_ambiguous_relational_disappointment_requests_deeper_appraisal() -> None:
    ordinary = InteractionEvent("ordinary_message", 1, "ordinary_chat", "", "")

    ambiguous = assess_appraisal_risk(InteractionEvidence(text="还行吧"), ordinary)
    explicit = assess_appraisal_risk(InteractionEvidence(text="你刚才真的很敷衍"), ordinary)
    neutral = assess_appraisal_risk(InteractionEvidence(text="今天下雨了"), ordinary)

    assert ambiguous.request_model_proposal is True
    assert ambiguous.request_deeper_reasoning is True
    assert explicit.request_deeper_reasoning is False
    assert neutral.request_model_proposal is False


@pytest.mark.asyncio
async def test_deep_appraisal_can_validate_cross_turn_withdrawal_without_making_it_default() -> (
    None
):
    class Model:
        async def complete(self, messages, *, temperature):  # type: ignore[no-untyped-def]
            return json.dumps(
                {
                    "appraisal": "user_withdrawing",
                    "literal_act": "给出简短评价",
                    "implied_attitude": "对上一轮回应失望并收回分享",
                    "target": "self",
                    "agency": "companion",
                    "certainty": 82,
                    "goal_congruence": -45,
                    "controllability": 70,
                    "norm_compatibility": 0,
                    "power_delta": 0,
                    "confidence": 0.86,
                    "severity": 2,
                    "acts": ["withdrawal"],
                    "evidence_spans": ["还行吧"],
                    "alternative_appraisal": "也可能只是普通的简短肯定",
                },
                ensure_ascii=False,
            )

    decision = await InteractionAppraiser(Model()).assess(
        TurnAppraisalInput(
            evidence=InteractionEvidence(text="还行吧"),
            fallback=InteractionEvent("ordinary_message", 1, "ordinary_chat", "", ""),
            recent_messages=(
                {"direction": "in", "text": "我今天和朋友去玩密室了"},
                {"direction": "out", "text": "嗯，那挺好的。"},
            ),
            relationship_stage="acquaintance",
        )
    )

    assert decision.risk.request_deeper_reasoning is True
    assert decision.accepted.kind == "user_withdrawing"


@pytest.mark.asyncio
async def test_model_validated_implicit_withdrawal_is_committed_after_first_reply(
    tmp_path: Path,
) -> None:
    class AppraisalModel:
        async def complete(self, messages, *, temperature):  # type: ignore[no-untyped-def]
            return json.dumps(
                {
                    "appraisal": "user_withdrawing",
                    "literal_act": "给出低能量回应",
                    "implied_attitude": "上一轮没有被接住，仍在撤回分享",
                    "target": "self",
                    "agency": "companion",
                    "certainty": 82,
                    "goal_congruence": -45,
                    "controllability": 70,
                    "norm_compatibility": 0,
                    "power_delta": 0,
                    "confidence": 0.86,
                    "severity": 2,
                    "acts": ["withdrawal"],
                    "evidence_spans": ["还行吧"],
                    "alternative_appraisal": "也可能只是普通简短肯定",
                },
                ensure_ascii=False,
            )

    engine, store = _engine(tmp_path)
    appraisal_model = AppraisalModel()
    engine.interaction_appraisal_model = appraisal_model
    engine.interaction_deep_appraisal_model = appraisal_model
    second_reply = await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="我去玩密室了", message_id="share-model"
        )
    )
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="还行吧", message_id="implicit-model"
        ),
        turn_context=FrozenTurnContext(
            turn_id="implicit-model",
            world_id=engine.world_id,
            user_id="user:geoff",
            observed_at=datetime.fromisoformat(
                engine.world_kernel.snapshot(engine.world_id)["clock"]["logical_at"]
            ),
            cadence=ConversationCadence("warm", None, 0, "offline_model_appraisal"),
        ),
    )

    assert second_reply is not None
    # The visible reply is not held for this model-only reading.  Let the
    # tracked advisory settle, then verify that its only durable effect is the
    # provenance-bound user-affect ledger for a subsequent turn.
    await asyncio.gather(*tuple(engine._appraisal_tasks))
    assert store.recent_turn_traces("geoff")[-1]["appraisal"] == "ordinary_message"
    affect = engine.world_kernel.snapshot(engine.world_id)["user_affect"]["user:geoff"]
    assert affect["kind"] == "disappointment"
    assert affect["source_message_id"] == _source_message_id("implicit-model")
    assert affect["unresolved"] is True
    await _respond_world_turn(
        engine,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="继续说", message_id="after-implicit"
        )
    )
    # TurnFrame converts the settled affect into a bounded repair Advisory for
    # the next primary prompt rather than restating it as a user fact.
    prompt = engine.model.calls[-1][1]["content"]
    assert '"kind":"repair"' in prompt
    assert f'"{_source_message_id("implicit-model")}"' in prompt
