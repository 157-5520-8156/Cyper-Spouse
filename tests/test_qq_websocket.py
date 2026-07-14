import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import random

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import CompanionReply, IncomingMessage, MessageAttachment, MoodState
from companion_daemon.qq_websocket import (
    ActiveSend,
    AfterthoughtPlan,
    CompanionQQClient,
    QueuedQQMessage,
    QQMessageCoalescer,
    QQTurnTransport,
    QQTurnPresenter,
    _attachments_from_botpy,
    _apply_affective_afterthought_affordance,
    _clean_content,
    _afterthought_plans,
    _default_response_budget_seconds,
    _requires_world_turn_runtime,
    _world_action_affective_observation,
    classify_mid_reply_interruption,
    _send_reply_parts,
)
from companion_daemon.companion_turn import (
    CompanionTurn,
    DispatchAcceptance,
    ExternalObservation,
    TurnPresentation,
)
from companion_daemon.turn_taking import TurnState, TurnTakingPolicy
from companion_daemon.world import WorldKernel
from companion_daemon.world import WorldError


def test_clean_content_strips_message_text() -> None:
    assert _clean_content("  知栀在吗  ") == "知栀在吗"
    assert _clean_content(None) == ""


def test_reply_msg_seq_is_positive() -> None:
    from companion_daemon.qq_websocket import _reply_msg_seq

    assert _reply_msg_seq() > 0


def test_hot_default_response_budget_leaves_room_for_live_model_result() -> None:
    assert _default_response_budget_seconds("hot") == 7.0
    assert _default_response_budget_seconds("hot") > 5.0


@pytest.mark.asyncio
async def test_background_media_requires_a_durable_qq_receipt() -> None:
    class Store:
        def resolve_user(self, _platform: str, _platform_user_id: str) -> str:
            return "geoff"

    class Engine:
        store = Store()

    async def no_receipt(_incoming: IncomingMessage, _reply: CompanionReply) -> None:
        return None

    coalescer = QQMessageCoalescer(  # type: ignore[arg-type]
        Engine(), delay_seconds=0.0, on_image=no_receipt
    )
    outcome = await coalescer._deliver_background_media(
        IncomingMessage(platform="qq", platform_user_id="user", text="发一张看看"),
        Path("assets/life/example.png"),
    )

    assert outcome.status == "unknown"
    assert outcome.reason == "qq_image_returned_without_durable_receipt"

    async def with_receipt(_incoming: IncomingMessage, _reply: CompanionReply) -> dict[str, str]:
        return {"id": "qq-image-42"}

    coalescer = QQMessageCoalescer(  # type: ignore[arg-type]
        Engine(), delay_seconds=0.0, on_image=with_receipt
    )
    outcome = await coalescer._deliver_background_media(
        IncomingMessage(platform="qq", platform_user_id="user", text="发一张看看"),
        Path("assets/life/example.png"),
    )

    assert outcome == DispatchAcceptance(
        status="delivered", external_receipt="platform:id:qq-image-42"
    )


@pytest.mark.asyncio
async def test_world_failure_is_visible_to_user_and_observable_instead_of_becoming_task_noise() -> (
    None
):
    class FailingEngine:
        async def handle_message(
            self, _incoming: IncomingMessage, **_kwargs: object
        ) -> CompanionReply:
            raise WorldError("grounding audit unavailable")

    class FakeTarget:
        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply(self, **kwargs: object) -> dict[str, str]:
            self.replies.append(str(kwargs["content"]))
            return {"id": "fallback-receipt"}

    observations: list[object] = []
    clock = iter((10.0, 10.0, 12.5))
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        FailingEngine(),  # type: ignore[arg-type]
        delay_seconds=0.0,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.0, long_wait_seconds=0.0),
        on_turn_observation=observations.append,
        monotonic=lambda: next(clock),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="算了，你继续看书吧"),
        target,
    )
    await asyncio.sleep(0.02)

    assert target.replies == ["我听出来了，你对我刚才的回应有点失望。是我没接好，不该继续追着问。"]
    assert len(observations) == 1
    observation = observations[0]
    assert observation.outcome == "world_error_fallback_delivered"
    assert observation.elapsed_seconds == 2.5
    assert observation.failure_type == "WorldError"


@pytest.mark.asyncio
async def test_response_deadline_delivers_visible_fallback_instead_of_serial_waiting() -> None:
    class SlowEngine:
        async def handle_message(
            self, _incoming: IncomingMessage, **_kwargs: object
        ) -> CompanionReply:
            await asyncio.sleep(60)
            raise AssertionError("deadline should cancel the slow reply")

    class FakeTarget:
        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply(self, **kwargs: object) -> dict[str, str]:
            self.replies.append(str(kwargs["content"]))
            return {"id": "deadline-fallback-receipt"}

    observations: list[object] = []
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        SlowEngine(),  # type: ignore[arg-type]
        delay_seconds=0.0,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.2, long_wait_seconds=0.2),
        on_turn_observation=observations.append,
        response_timeout_seconds=0.3,
    )

    await coalescer.add(
        "c2c:deadline",
        IncomingMessage(platform="qq", platform_user_id="user", text="什么意思？我没懂"),
        target,
    )
    # 0.2s of input merging leaves roughly 0.1s for generation. If the
    # coalescing wait incorrectly received a separate budget, no fallback
    # would be visible until about 0.5s.
    await asyncio.sleep(0.4)

    assert len(target.replies) == 1
    assert observations[0].outcome == "deadline_fallback_delivered"
    assert observations[0].failure_type == "TimeoutError"


@pytest.mark.asyncio
async def test_incomplete_world_runtime_fails_closed_before_legacy_generation() -> None:
    order: list[str] = []

    class FakeEngine:
        world_kernel = object()
        world_id = ""

        def mark_phone_read_for_message(self, _incoming: IncomingMessage) -> None:
            order.append("read")

        def begin_world_typing(self, _incoming: IncomingMessage) -> None:
            order.append("typing")

        def stop_world_typing(self, _incoming: IncomingMessage, *, reason: str) -> None:
            order.append(f"typing_stopped:{reason}")

        async def handle_message(
            self, _incoming: IncomingMessage, **_kwargs: object
        ) -> CompanionReply:
            # World mode starts typing after the message has entered the ledger;
            # the adapter must not attempt that transition before observation.
            self.begin_world_typing(_incoming)
            order.append("model")
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="在。")

    class FakeTarget:
        async def reply(self, **_kwargs: object) -> None:
            order.append("delivered")

    coalescer = QQMessageCoalescer(FakeEngine(), delay_seconds=0.01, human_timing=False)
    with pytest.raises(WorldError, match="requires a WorldKernel"):
        await coalescer._generate_and_send(
            IncomingMessage(platform="qq", platform_user_id="user", text="在吗"),
            FakeTarget(),
            key="c2c:user",
        )

    assert order == []


def test_attachments_from_botpy() -> None:
    class RawAttachment:
        content_type = "image/png"
        filename = "photo.png"
        url = "https://example.test/photo.png"
        size = 123
        width = 10
        height = 20

    attachments = _attachments_from_botpy([RawAttachment()])

    assert len(attachments) == 1
    assert attachments[0].kind == "image"
    assert attachments[0].url == "https://example.test/photo.png"


def test_duplicate_detection_drops_near_simultaneous_same_text_even_with_distinct_ids() -> None:
    client = object.__new__(CompanionQQClient)
    client._seen_message_ids = set()
    client._recent_text_keys = {}

    assert client._is_duplicate("msg-1", "user", "哈哈") is False
    assert client._is_duplicate("msg-2", "user", "哈哈") is True
    assert client._is_duplicate("msg-2", "user", "哈哈") is True


def test_duplicate_detection_falls_back_to_text_without_message_id() -> None:
    client = object.__new__(CompanionQQClient)
    client._seen_message_ids = set()
    client._recent_text_keys = {}

    assert client._is_duplicate(None, "user", "哈哈") is False
    assert client._is_duplicate(None, "user", "哈哈") is True


def test_classifies_mid_reply_interruption() -> None:
    assert classify_mid_reply_interruption("嗯嗯") == "backchannel"
    assert classify_mid_reply_interruption("对对对") == "backchannel"
    assert classify_mid_reply_interruption("等下我不是这个意思") == "takeover"
    assert classify_mid_reply_interruption("那你觉得我应该怎么办？") == "takeover"


@pytest.mark.asyncio
async def test_semantic_companion_interruption_advisor_can_shorten_open_burst_wait() -> None:
    from companion_daemon.companion_interruption import CompanionInterruptionAdvice

    class Advisor:
        calls = 0

        async def advise(self, context):  # type: ignore[no-untyped-def]
            self.calls += 1
            assert context.latest_text == "不是，我不同意这个说法，"
            assert context.base_reason == "latest_message_continues"
            return CompanionInterruptionAdvice(
                True,
                "disagreement",
                0.82,
                0.05,
                ("不同意",),
            )

    advisor = Advisor()
    coalescer = QQMessageCoalescer(
        object(),  # type: ignore[arg-type]
        delay_seconds=2.0,
        turn_policy=TurnTakingPolicy(short_wait_seconds=2.0, long_wait_seconds=5.0),
        interruption_advisor=advisor,
    )
    coalescer._pending["c2c:user"].append(
        QueuedQQMessage(
            incoming=IncomingMessage(
                platform="qq",
                platform_user_id="user",
                text="不是，我不同意这个说法，",
            ),
            reply_target=object(),  # type: ignore[arg-type]
            received_monotonic=0.0,
        )
    )

    base = coalescer._decision_for("c2c:user")
    assert base.state == TurnState.COLLECTING

    revised = await coalescer._maybe_apply_companion_interruption_advice("c2c:user", base)

    assert advisor.calls == 1
    assert revised.state == TurnState.READY
    assert revised.wait_seconds == 0.05
    assert revised.reason == "semantic_companion_interruption:disagreement"


@pytest.mark.asyncio
async def test_semantic_companion_interruption_respects_explicit_hold_floor() -> None:
    class Advisor:
        calls = 0

        async def advise(self, context):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise AssertionError("advisor should not override explicit hold-floor text")

    advisor = Advisor()
    coalescer = QQMessageCoalescer(
        object(),  # type: ignore[arg-type]
        delay_seconds=2.0,
        turn_policy=TurnTakingPolicy(short_wait_seconds=2.0, long_wait_seconds=5.0),
        interruption_advisor=advisor,
    )
    coalescer._pending["c2c:user"].append(
        QueuedQQMessage(
            incoming=IncomingMessage(
                platform="qq",
                platform_user_id="user",
                text="先别回，我还没说完，",
            ),
            reply_target=object(),  # type: ignore[arg-type]
            received_monotonic=0.0,
        )
    )

    base = coalescer._decision_for("c2c:user")
    revised = await coalescer._maybe_apply_companion_interruption_advice("c2c:user", base)

    assert advisor.calls == 0
    assert revised.reason == "user_thinking_or_hesitating"


def test_afterthought_plans_favor_open_story_and_respect_goodbyes() -> None:
    class FixedRandom:
        def uniform(self, low: float, high: float) -> float:
            return low

    plans = _afterthought_plans(
        "我今天其实遇到一件挺奇怪的事，后来越想越不对劲。事情不大，但我到现在还不知道要不要把它当回事。",
        FixedRandom(),
    )
    assert [plan.mode for plan in plans] == ["quick_continue", "topic_drift", "silence_react"]
    assert _afterthought_plans("晚安啦", FixedRandom()) == []
    assert _afterthought_plans("？", FixedRandom()) == []
    assert _afterthought_plans("你是不是在跟别人聊天", FixedRandom()) == []


def test_affective_affordance_modulates_afterthought_opportunity_without_content() -> None:
    class FixedRandom:
        def uniform(self, low: float, high: float) -> float:
            return low

    base = [AfterthoughtPlan("quick_continue", 20, 0.26)]

    boosted = _apply_affective_afterthought_affordance(
        base, "soft_repair", FixedRandom()
    )
    assert boosted[0].mode == "quick_continue"
    assert boosted[0].probability > base[0].probability

    delayed = _apply_affective_afterthought_affordance(
        [], "delayed_afterthought", FixedRandom()
    )
    assert delayed == [AfterthoughtPlan("delayed_afterthought", 45, 0.42)]

    assert _apply_affective_afterthought_affordance(
        base, "let_it_pass", FixedRandom()
    ) == []

    guarded = _apply_affective_afterthought_affordance(
        base, "withdraw_slightly", FixedRandom()
    )
    assert guarded[0].mode == "quick_continue"
    assert guarded[0].probability < base[0].probability


def test_world_action_affective_observation_exports_only_redacted_metrics() -> None:
    class World:
        def snapshot(self, world_id: str) -> dict[str, object]:
            assert world_id == "zhizhi-v1"
            return {
                "actions": {
                    "action-1": {
                        "message_kind": "afterthought",
                        "trace": {
                            "user_affect": {"kind": "disappointment"},
                            "private_impression": {"summary": "private text"},
                            "affective_advisory": {
                                "readings": [
                                    {
                                        "kind": "possible_disappointment",
                                        "evidence_spans": ["敷衍"],
                                    }
                                ],
                                "selection": {
                                    "selected": {"kind": "soft_repair"},
                                    "candidates": [
                                        {"kind": "soft_repair"},
                                        {"kind": "let_it_pass"},
                                    ],
                                },
                            },
                        }
                    }
                }
            }

    class Engine:
        world_kernel = World()
        world_id = "zhizhi-v1"

    summary = _world_action_affective_observation(Engine(), ("action-1",))

    assert summary == {
        "message_kinds": ("afterthought",),
        "affective_reading_kinds": ("possible_disappointment",),
        "expression_affordance_candidate_kinds": ("soft_repair", "let_it_pass"),
        "selected_affordance_kind": "soft_repair",
        "user_affect_kinds": ("disappointment",),
        "user_affect_recorded": True,
        "private_impression_recorded": True,
    }
    assert "敷衍" not in json.dumps(summary, ensure_ascii=False)
    assert "private text" not in json.dumps(summary, ensure_ascii=False)


@pytest.mark.asyncio
async def test_afterthought_episode_uses_original_reply_time_and_stays_bounded() -> None:
    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.seen: list[tuple[datetime, str]] = []

        async def generate_afterthought(
            self,
            canonical_user_id: str,
            reply_sent_at: datetime,
            *,
            mode: str = "quick_continue",
        ) -> str:
            self.seen.append((reply_sent_at, mode))
            return f"{mode}:刚刚还想补一句。"

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> dict[str, str]:
            self.replies.append(kwargs["content"])
            return {"message_id": f"afterthought-episode-{len(self.replies)}"}

    class AlwaysSendRandom:
        def uniform(self, low: float, high: float) -> float:
            return low

        def random(self) -> float:
            return 0.0

    async def fake_sleep(seconds: float) -> None:
        return None

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        human_timing=True,
        sleep=fake_sleep,
        rng=AlwaysSendRandom(),
    )
    reply_sent_at = datetime(2026, 7, 10, 1, 2, 3, tzinfo=timezone.utc)

    coalescer._schedule_afterthought(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            text="我今天遇到一件挺奇怪的事，后来越想越不对劲，到现在还不知道要不要当回事。",
        ),
        target,
        reply_sent_at,
    )
    tasks = coalescer._afterthought_tasks["c2c:user"]
    await asyncio.gather(*tasks)

    assert all(seen_at == reply_sent_at for seen_at, _ in engine.seen)
    assert [mode for _, mode in engine.seen] == ["quick_continue", "topic_drift"]
    assert target.replies == [
        "quick_continue:刚刚还想补一句。",
        "topic_drift:刚刚还想补一句。",
    ]


@pytest.mark.asyncio
async def test_afterthought_uses_outbox_delivery_confirmation() -> None:
    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.queued: list[tuple[str, str, str]] = []
            self.confirmed: list[tuple[str, str, str, int | None]] = []
            self.failed: list[tuple[int | None, str]] = []

        async def generate_afterthought(
            self,
            canonical_user_id: str,
            reply_sent_at: datetime,
            *,
            mode: str = "quick_continue",
        ) -> str:
            return "补一句。"

        def queue_afterthought_delivery(
            self,
            canonical_user_id: str,
            platform: str,
            text: str,
        ) -> int:
            self.queued.append((canonical_user_id, platform, text))
            return 42

        def confirm_afterthought_delivery(
            self,
            canonical_user_id: str,
            platform: str,
            text: str,
            *,
            delivery_id: int | None = None,
        ) -> None:
            self.confirmed.append((canonical_user_id, platform, text, delivery_id))

        def fail_afterthought_delivery(self, delivery_id: int | None, reason: str) -> None:
            self.failed.append((delivery_id, reason))

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> dict[str, str]:
            self.replies.append(kwargs["content"])
            return {"message_id": "afterthought-42"}

    class AlwaysSendRandom:
        def uniform(self, low: float, high: float) -> float:
            return low

        def random(self) -> float:
            return 0.0

    async def fake_sleep(seconds: float) -> None:
        return None

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        human_timing=True,
        sleep=fake_sleep,
        rng=AlwaysSendRandom(),
    )

    coalescer._schedule_afterthought(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            text="我今天遇到一件挺奇怪的事，后来越想越不对劲，到现在还不知道要不要当回事。",
        ),
        target,
        datetime(2026, 7, 10, 1, 2, 3, tzinfo=timezone.utc),
    )
    await asyncio.gather(*coalescer._afterthought_tasks["c2c:user"])

    # The second stage was eligible, but it paraphrased the first exactly and
    # was withheld before it could become agent self-talk.
    assert target.replies == ["补一句。"]
    assert engine.queued == [("geoff", "qq", "补一句。")]
    assert engine.confirmed == [("geoff", "qq", "补一句。", 42)]
    assert engine.failed == []


@pytest.mark.asyncio
async def test_world_afterthought_without_a_durable_receipt_stays_unknown(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "afterthought-unknown.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )

    async def generated(*_args, **_kwargs) -> str:
        return "哦对，补一句。"

    engine.generate_afterthought = generated  # type: ignore[method-assign]

    class ReceiptlessTarget:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"status": "ok"}

    async def no_wait(_seconds: float) -> None:
        return None

    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        sleep=no_wait,
    )
    result = await coalescer._fire_afterthought(
        "c2c:user",
        AfterthoughtPlan("quick_continue", 0.0, 1.0),
        IncomingMessage(
            platform="qq", platform_user_id="geoff", message_id="afterthought-source", text="刚才那事"
        ),
        ReceiptlessTarget(),
        datetime.now(timezone.utc),
    )

    assert result is None
    actions = world.snapshot(world_id)["actions"]
    afterthoughts = [
        action for action in actions.values()
        if isinstance(action, dict) and action.get("message_kind") == "afterthought"
    ]
    assert len(afterthoughts) == 1
    assert afterthoughts[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_world_afterthought_uses_world_affect_not_legacy_mood(tmp_path: Path) -> None:
    """A stale pre-World mood row cannot veto a World continuation."""

    store = CompanionStore(tmp_path / "afterthought-world-affect.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    store.save_mood_state("geoff", MoodState(mood="hurt", boundary_level=80))

    async def generated(*_args: object, **_kwargs: object) -> str:
        return "哦对，补一句。"

    engine.generate_afterthought = generated  # type: ignore[method-assign]

    class Target:
        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply(self, *, content: str, **_kwargs: object) -> dict[str, str]:
            self.replies.append(content)
            return {"message_id": "afterthought-world-affect"}

    class AlwaysSendRandom:
        def uniform(self, low: float, _high: float) -> float:
            return low

        def random(self) -> float:
            return 0.0

    async def no_wait(_seconds: float) -> None:
        return None

    target = Target()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        human_timing=True,
        sleep=no_wait,
        rng=AlwaysSendRandom(),
    )
    coalescer._schedule_afterthought(
        "c2c:geoff",
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="afterthought-world-affect-source",
            text="我今天遇到一件挺奇怪的事，后来越想越不对劲，到现在还不知道要不要当回事。",
        ),
        target,
        datetime.now(timezone.utc),
    )
    await asyncio.gather(*coalescer._afterthought_tasks["c2c:geoff"])

    assert target.replies == ["哦对，补一句。"]


@pytest.mark.asyncio
async def test_world_afterthought_suppresses_from_world_negative_affect(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "afterthought-world-negative.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {
            "type": "register_user",
            "world_id": world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "register:afterthought-world-negative",
        },
        expected_revision=world.revision(world_id),
    )
    world.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": "boundary_violation",
            "intent_id": "afterthought-world-negative",
            "message_id": "afterthought-world-negative-source",
            "user_id": "user:geoff",
            "idempotency_key": "appraise:afterthought-world-negative",
        },
        expected_revision=world.revision(world_id),
    )
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )

    class Target:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            raise AssertionError("negative World affect must suppress the afterthought")

    class AlwaysSendRandom:
        def random(self) -> float:
            return 0.0

    coalescer = QQMessageCoalescer(engine, delay_seconds=0.01, rng=AlwaysSendRandom())
    coalescer._schedule_afterthought(
        "c2c:geoff",
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="afterthought-world-negative-input",
            text="我今天遇到一件挺奇怪的事，后来越想越不对劲，到现在还不知道要不要当回事。",
        ),
        Target(),
        datetime.now(timezone.utc),
    )

    assert coalescer._afterthought_tasks == {}


def test_world_afterthought_projection_failure_suppresses_scheduling(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "afterthought-world-projection-failure.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )

    def unavailable_snapshot(_world_id: str) -> dict[str, object]:
        raise RuntimeError("world temporarily unavailable")

    world.snapshot = unavailable_snapshot  # type: ignore[method-assign]
    coalescer = QQMessageCoalescer(engine, delay_seconds=0.01)
    coalescer._schedule_afterthought(
        "c2c:geoff",
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="afterthought-world-projection-failure-input",
            text="我今天遇到一件挺奇怪的事，后来越想越不对劲，到现在还不知道要不要当回事。",
        ),
        object(),  # type: ignore[arg-type]
        datetime.now(timezone.utc),
    )

    assert coalescer._afterthought_tasks == {}


def test_world_turn_detection_accepts_a_delegating_runtime() -> None:
    class DelegatingRuntime:
        world_kernel = object()
        world_id = "world-proxy"

    assert _requires_world_turn_runtime(DelegatingRuntime()) is True


def test_missing_world_id_still_fails_closed_instead_of_enabling_legacy_qq() -> None:
    class IncompleteRuntime:
        world_kernel = object()
        world_id = ""

    assert _requires_world_turn_runtime(IncompleteRuntime()) is True


@pytest.mark.asyncio
async def test_new_user_message_cancels_pending_afterthoughts() -> None:
    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.afterthought_called = False

        async def generate_afterthought(
            self,
            canonical_user_id: str,
            reply_sent_at: datetime,
            *,
            mode: str = "quick_continue",
        ) -> str:
            self.afterthought_called = True
            return "不该发。"

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="新回复。")

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    class SlowRandom:
        def uniform(self, low: float, high: float) -> float:
            return high

        def random(self) -> float:
            return 0.0

    async def fake_sleep(seconds: float) -> None:
        if seconds > 10:
            await asyncio.Event().wait()
        return None

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        human_timing=True,
        sleep=fake_sleep,
        rng=SlowRandom(),
    )
    coalescer._schedule_afterthought(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="刚才那事"),
        target,
        datetime(2026, 7, 10, 1, 2, 3, tzinfo=timezone.utc),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="我回来了"),
        target,
    )
    await asyncio.sleep(0.03)

    assert engine.afterthought_called is False
    assert target.replies == ["新回复。"]


@pytest.mark.asyncio
async def test_coalescer_batches_rapid_messages() -> None:
    class FakeEngine:
        def __init__(self):
            self.seen_texts: list[str] = []
            self.seen_source_ids: list[list[str]] = []
            self.seen_source_messages: list[list[dict[str, object]]] = []

        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            self.seen_texts.append(incoming.text)
            self.seen_source_ids.append(incoming.source_message_ids)
            self.seen_source_messages.append(
                [item.model_dump(mode="json") for item in incoming.source_messages]
            )
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="收到")

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])
            assert kwargs["msg_seq"] > 0

    engine = FakeEngine()
    first = FakeTarget()
    second = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            message_id="qq:1",
            text="第一句",
            emoji=["qq-face:178"],
            sticker_kind="[无语]",
            reply_target="reply:prior-message",
        ),
        first,
    )
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", message_id="qq:2", text="第二句"),
        second,
    )
    await asyncio.sleep(0.03)

    assert engine.seen_texts == ["第一句\n第二句"]
    assert engine.seen_source_ids == [["qq:1", "qq:2"]]
    assert engine.seen_source_messages == [
        [
            {
                "message_id": "qq:1",
                "text": "第一句",
                "emoji": ["qq-face:178"],
                "sticker_kind": "[无语]",
                "reply_target": "reply:prior-message",
                "attachments": [],
            },
            {
                "message_id": "qq:2",
                "text": "第二句",
                "emoji": [],
                "sticker_kind": None,
                "reply_target": None,
                "attachments": [],
            },
        ]
    ]
    assert first.replies == []
    assert second.replies == ["收到"]


@pytest.mark.asyncio
async def test_coalescer_flushes_six_messages_before_starting_a_seventh_batch() -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.seen_texts: list[str] = []

        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            self.seen_texts.append(incoming.text)
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="收到")

    class FakeTarget:
        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    for index in range(1, 8):
        await coalescer.add(
            "c2c:user",
            IncomingMessage(platform="qq", platform_user_id="user", text=f"第{index}句，"),
            target,
        )
    await asyncio.sleep(0.03)

    assert engine.seen_texts == [
        "\n".join(f"第{index}句，" for index in range(1, 7)),
        "第7句，",
    ]
    assert target.replies == ["收到", "收到"]


@pytest.mark.asyncio
async def test_coalescer_preserves_attachments() -> None:
    class FakeEngine:
        def __init__(self):
            self.attachments = []

        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            self.attachments = incoming.attachments
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="看到了")

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            return None

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            text="这张图",
            attachments=[MessageAttachment(kind="image", filename="photo.png")],
        ),
        FakeTarget(),
    )
    await asyncio.sleep(0.03)

    assert len(engine.attachments) == 1
    assert engine.attachments[0].filename == "photo.png"


@pytest.mark.asyncio
async def test_coalescer_sends_sticker_after_text_reply() -> None:
    class FakeEngine:
        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="好呀",
                sticker_path="assets/stickers/rin-happy.png",
            )

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    sent: list[tuple[str, str]] = []

    async def send_sticker(incoming: IncomingMessage, reply: CompanionReply) -> None:
        sent.append((incoming.platform_user_id, reply.sticker_path or ""))

    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        on_sticker=send_sticker,
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="哈哈"),
        target,
    )
    await asyncio.sleep(0.03)

    assert target.replies == ["好呀"]
    assert sent == [("user", "assets/stickers/rin-happy.png")]


@pytest.mark.asyncio
async def test_coalescer_fires_emoji_reaction_before_reply_text() -> None:
    order: list[str] = []

    class FakeEngine:
        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="哈哈哈被你笑到",
                suggested_reaction="haha",
            )

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            order.append("text")

    async def send_reaction(incoming: IncomingMessage, reply: CompanionReply) -> None:
        order.append(f"reaction:{reply.suggested_reaction}:{incoming.message_id}")

    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        on_reaction=send_reaction,
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(
            platform="qq", platform_user_id="user", text="超好笑的事", message_id="777"
        ),
        FakeTarget(),
    )
    await asyncio.sleep(0.03)

    assert order == ["reaction:haha:777", "text"]


@pytest.mark.asyncio
async def test_coalescer_sends_reply_parts_in_order() -> None:
    class FakeEngine:
        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="我在。刚刚想到你。",
                text_parts=["我在。", "刚刚想到你。"],
            )

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="在吗"),
        target,
    )
    await asyncio.sleep(1.0)

    assert target.replies == ["我在。", "刚刚想到你。"]


@pytest.mark.asyncio
async def test_backchannel_during_split_reply_does_not_interrupt() -> None:
    class FakeEngine:
        def __init__(self):
            self.recorded_without_reply: list[str] = []

        async def handle_message(
            self, incoming: IncomingMessage, **kwargs
        ) -> CompanionReply | None:
            if kwargs.get("skip_reply"):
                self.recorded_without_reply.append(incoming.text)
                return None
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="第一句。第二句。",
                text_parts=["第一句。", "第二句。"],
            )

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        human_timing=True,
        rng=random.Random(1),
    )
    inserted = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal inserted
        if target.replies == ["第一句。"] and not inserted:
            inserted = True
            await coalescer.add(
                "c2c:user",
                IncomingMessage(platform="qq", platform_user_id="user", text="嗯嗯"),
                target,
            )

    coalescer.sleep = fake_sleep
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="你继续说"),
        target,
    )
    await asyncio.sleep(0.03)

    assert target.replies == ["第一句。", "第二句。"]
    assert engine.recorded_without_reply == ["嗯嗯"]


@pytest.mark.asyncio
async def test_world_backchannel_uses_observe_only_turn_seam(tmp_path: Path) -> None:
    class FakeTarget:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"message_id": "should-not-send"}

    store = CompanionStore(tmp_path / "world-backchannel.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    coalescer = QQMessageCoalescer(engine, delay_seconds=0.01, human_timing=False)
    target = FakeTarget()
    key = "c2c:geoff"
    original = IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="original-send", text="继续说"
    )
    coalescer._active_sends[key] = ActiveSend(
        incoming=original,
        reply_target=target,
        text_dispatch_started=True,
    )
    coalescer._active_turns[key] = CompanionTurn(engine, QQTurnTransport(target))

    handled = await coalescer._handle_mid_reply_interruption(
        key,
        IncomingMessage(
            platform="qq", platform_user_id="geoff", message_id="backchannel-1", text="嗯嗯"
        ),
        target,
    )

    assert handled is True
    assert world.snapshot(world_id)["turns"]["qq:geoff:backchannel-1"]["status"] == "deferred"


@pytest.mark.asyncio
async def test_takeover_during_split_reply_stops_remaining_parts_and_replies_again() -> None:
    class FakeEngine:
        def __init__(self):
            self.seen_texts: list[str] = []

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            self.seen_texts.append(incoming.text)
            if len(self.seen_texts) == 1:
                return CompanionReply(
                    canonical_user_id="geoff",
                    mood="happy",
                    text="第一句。第二句。第三句。",
                    text_parts=["第一句。", "第二句。", "第三句。"],
                )
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="哦，那我换个说法。")

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        human_timing=True,
        rng=random.Random(1),
    )
    inserted = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal inserted
        if target.replies == ["第一句。"] and not inserted:
            inserted = True
            await coalescer.add(
                "c2c:user",
                IncomingMessage(platform="qq", platform_user_id="user", text="等下我不是这个意思"),
                target,
            )

    coalescer.sleep = fake_sleep
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="你继续说"),
        target,
    )
    await asyncio.sleep(0.05)

    assert engine.seen_texts == ["你继续说", "等下我不是这个意思"]
    assert target.replies == ["第一句。", "哦，那我换个说法。"]


@pytest.mark.asyncio
async def test_coalescer_human_timing_reads_current_mood_state() -> None:
    from companion_daemon.models import MoodState

    class FakeStore:
        def get_mood_state(self, canonical_user_id: str) -> MoodState:
            assert canonical_user_id == "geoff"
            return MoodState(emotion_vector={"anger": 90, "sadness": 70, "fear": 50})

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()

        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(canonical_user_id="geoff", mood="hurt", text="嗯。")

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            return None

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        human_timing=True,
        sleep=fake_sleep,
        rng=random.Random(1),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="随便你"),
        FakeTarget(),
    )
    await asyncio.sleep(0.01)

    assert any(seconds > 10 for seconds in sleeps)


@pytest.mark.asyncio
async def test_coalescer_can_defer_long_story_then_reply() -> None:
    class DeferRandom(random.Random):
        def random(self) -> float:
            return 0.0

        def uniform(self, a: float, b: float) -> float:
            return a

    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

        def get_mood_state(self, canonical_user_id: str):
            from companion_daemon.models import MoodState

            return MoodState()

        def recent_messages(self, canonical_user_id: str, limit: int = 6):
            return []

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.seen_texts: list[str] = []
            self.context_hints: list[str | None] = []

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            self.seen_texts.append(incoming.text)
            self.context_hints.append(kwargs.get("context_hint"))
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="刚看到。")

    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    engine = FakeEngine()
    target = FakeTarget()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        enable_reply_decision=True,
        sleep=fake_sleep,
        rng=DeferRandom(),
    )

    opener = "我跟你说"
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text=opener),
        target,
    )
    await asyncio.sleep(0.01)

    assert engine.seen_texts == [opener]
    assert engine.context_hints == [None]
    assert target.replies == ["刚看到。"]
    assert any(10 <= seconds <= 20 for seconds in sleeps)


def test_qq_emoji_id_maps_known_reactions() -> None:
    from companion_daemon.emotion_reactions import qq_emoji_id

    assert qq_emoji_id("haha") == "128514"
    assert qq_emoji_id("heart") == "10084"
    assert qq_emoji_id(None) is None
    assert qq_emoji_id("unknown") is None


@pytest.mark.asyncio
async def test_reply_decision_defer_persists_a_read_later_task() -> None:
    class DeferRandom(random.Random):
        def random(self) -> float:
            return 0.0

        def uniform(self, a: float, b: float) -> float:
            return a

    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

        def get_mood_state(self, canonical_user_id: str):
            from companion_daemon.models import MoodState

            return MoodState()

        def recent_messages(self, canonical_user_id: str, limit: int = 6):
            return []

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.read_later_reasons: list[str] = []
            self.deferred_reasons: list[str] = []

        def create_read_later_task(
            self, message: IncomingMessage, *, defer_minutes: float, reason: str
        ) -> int:
            self.read_later_reasons.append(reason)
            return 11

        def create_deferred_reply_task(
            self, message: IncomingMessage, *, defer_minutes: float, reason: str
        ) -> int:
            self.deferred_reasons.append(reason)
            return 12

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(canonical_user_id="geoff", mood="calm", text="刚看到。")

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            return None

    async def fake_sleep(seconds: float) -> None:
        return None

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        enable_reply_decision=True,
        sleep=fake_sleep,
        rng=DeferRandom(),
    )

    long_story = (
        "今天在食堂排队的时候，前面有个人点了三份一样的菜，然后又全部退掉重新点了一遍，"
        "我就这样看着队伍慢慢变长，最后干脆换了一个窗口，结果那边的阿姨手更慢。"
    )
    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text=long_story),
        FakeTarget(),
    )
    await asyncio.sleep(0.01)

    # The reply-decision defer happens after she already read the message, so it
    # must persist through the read-but-sidetracked entrance, not an unread defer.
    assert engine.read_later_reasons
    assert engine.deferred_reasons == []


@pytest.mark.asyncio
async def test_upset_state_ghosts_through_read_later_and_uses_ghost_context_hint() -> None:
    class GhostRandom(random.Random):
        def random(self) -> float:
            return 0.0

        def uniform(self, a: float, b: float) -> float:
            return a

    class FakeStore:
        def resolve_user(self, platform: str, platform_user_id: str) -> str:
            return "geoff"

        def get_mood_state(self, canonical_user_id: str):
            return MoodState(
                mood="hurt",
                emotion_vector={"anger": 75, "sadness": 55, "joy": 5, "trust": 10},
            )

        def recent_messages(self, canonical_user_id: str, limit: int = 6):
            return []

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.read_later_reasons: list[str] = []
            self.context_hints: list[str | None] = []

        def create_read_later_task(
            self, message: IncomingMessage, *, defer_minutes: float, reason: str
        ) -> int:
            self.read_later_reasons.append(reason)
            return 9

        def create_deferred_reply_task(
            self, message: IncomingMessage, *, defer_minutes: float, reason: str
        ) -> int:
            raise AssertionError("ghost must not use unread defer")

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            self.context_hints.append(kwargs.get("context_hint"))
            return CompanionReply(canonical_user_id="geoff", mood="hurt", text="嗯。")

        def complete_deferred_reply_task(self, task_id: int | None) -> None:
            return None

    async def fake_sleep(seconds: float) -> None:
        return None

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        enable_reply_decision=True,
        sleep=fake_sleep,
        rng=GhostRandom(),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="我今天去买了那个键盘"),
        type("T", (), {"reply": staticmethod(lambda **k: None)})(),
    )
    # Let the in-memory ghost timer fire immediately (defer_minutes * 60 with fake_sleep).
    await asyncio.sleep(0.02)

    assert engine.read_later_reasons == ["emotional_ghost"]
    assert "故意没有马上回" in (engine.context_hints[0] or "")


@pytest.mark.asyncio
async def test_send_reply_parts_uses_human_delay_between_parts() -> None:
    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    target = FakeTarget()
    sent = await _send_reply_parts(
        target,
        ["嗯。", "刚刚其实还想补一句。"],
        sleep=fake_sleep,
        rng=random.Random(1),
        human_timing=True,
    )

    assert sent is True
    assert target.replies == ["嗯。", "刚刚其实还想补一句。"]
    assert len(delays) == 1
    assert delays[0] >= 0.9


@pytest.mark.asyncio
async def test_send_reply_parts_honors_a_model_selected_bounded_delay() -> None:
    class FakeTarget:
        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    sent = await _send_reply_parts(
        FakeTarget(),
        ["先说。", "再补一句。"],
        part_delays_ms=[0, 1200],
        sleep=fake_sleep,
        human_timing=True,
    )

    assert sent is True
    assert delays == [1.2]


@pytest.mark.asyncio
async def test_send_reply_parts_reports_interruption_after_a_partial_delivery() -> None:
    class FakeTarget:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, **kwargs) -> None:
            self.replies.append(kwargs["content"])

    checks = iter([True, False])
    target = FakeTarget()
    sent = await _send_reply_parts(
        target,
        ["第一句。", "第二句。"],
        sleep=lambda _: asyncio.sleep(0),
        human_timing=False,
        should_continue=lambda: next(checks),
    )

    assert sent is False
    assert target.replies == ["第一句。"]


@pytest.mark.asyncio
async def test_coalescer_persists_platform_message_id_before_settling_segment() -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.confirmed: list[tuple[str, str | None]] = []

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="calm",
                text="在呢。",
                text_parts=["在呢。"],
                delivery_id=17,
                world_action_id="outgoing:17",
            )

        def begin_reply_part_delivery(self, reply: CompanionReply, *, position: int) -> str:
            assert position == 0
            return "outgoing:17:segment:0"

        def confirm_reply_part_delivery(
            self,
            reply: CompanionReply,
            *,
            segment_id: str,
            external_receipt: str | None = None,
        ) -> None:
            self.confirmed.append((segment_id, external_receipt))

        def confirm_reply_delivery(self, reply: CompanionReply) -> None:
            return None

    class FakeTarget:
        async def reply(self, **kwargs) -> dict[str, str]:
            return {"message_id": "qq-message-701"}

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="在吗"),
        FakeTarget(),
    )
    await asyncio.sleep(0.03)

    assert engine.confirmed == [("outgoing:17:segment:0", "platform:message_id:qq-message-701")]


@pytest.mark.asyncio
async def test_coalescer_marks_claimed_segment_unknown_when_reply_io_is_ambiguous() -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.unknown: list[tuple[str, str]] = []
            self.failed: list[str] = []

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="calm",
                text="在呢。",
                text_parts=["在呢。"],
                delivery_id=18,
                world_action_id="outgoing:18",
            )

        def begin_reply_part_delivery(self, reply: CompanionReply, *, position: int) -> str:
            return "outgoing:18:segment:0"

        def mark_reply_part_unknown(
            self, reply: CompanionReply, *, segment_id: str, reason: str
        ) -> None:
            self.unknown.append((segment_id, reason))

        def fail_reply_delivery(self, reply: CompanionReply, reason: str) -> None:
            self.failed.append(reason)

    class AmbiguousTarget:
        async def reply(self, **kwargs) -> None:
            raise ConnectionResetError("connection lost after request dispatch")

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="在吗"),
        AmbiguousTarget(),
    )
    await asyncio.sleep(0.03)

    assert engine.unknown == [
        ("outgoing:18:segment:0", "QQ adapter call ended without durable delivery evidence")
    ]
    assert engine.failed == []


@pytest.mark.asyncio
async def test_coalescer_keeps_segment_unknown_when_success_response_has_no_receipt() -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.unknown: list[str] = []
            self.confirmed: list[str] = []

        async def handle_message(self, incoming: IncomingMessage, **kwargs) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="calm",
                text="在呢。",
                text_parts=["在呢。"],
                delivery_id=19,
                world_action_id="outgoing:19",
            )

        def begin_reply_part_delivery(self, reply: CompanionReply, *, position: int) -> str:
            return "outgoing:19:segment:0"

        def confirm_reply_part_delivery(self, reply: CompanionReply, **kwargs) -> None:
            self.confirmed.append(kwargs["segment_id"])

        def mark_reply_part_unknown(
            self, reply: CompanionReply, *, segment_id: str, reason: str
        ) -> None:
            self.unknown.append(segment_id)

    class ReceiptlessTarget:
        async def reply(self, **kwargs) -> dict[str, str]:
            return {"status": "ok"}

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="在吗"),
        ReceiptlessTarget(),
    )
    await asyncio.sleep(0.03)

    assert engine.unknown == ["outgoing:19:segment:0"]
    assert engine.confirmed == []


@pytest.mark.asyncio
async def test_legacy_coalescer_does_not_confirm_outbox_delivery_without_receipt() -> None:
    """A legacy outbox row remains unsettled when QQ only acknowledges the request."""

    class FakeEngine:
        def __init__(self) -> None:
            self.confirmed: list[int] = []
            self.failed: list[str] = []

        async def handle_message(self, _incoming: IncomingMessage, **_kwargs: object) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="calm",
                text="在呢。",
                delivery_id=20,
            )

        def confirm_reply_delivery(self, reply: CompanionReply) -> None:
            assert reply.delivery_id is not None
            self.confirmed.append(reply.delivery_id)

        def fail_reply_delivery(self, _reply: CompanionReply, reason: str) -> None:
            self.failed.append(reason)

    class ReceiptlessTarget:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"status": "accepted"}

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(engine, delay_seconds=0.01, human_timing=False)

    delivered = await coalescer._generate_and_send(
        IncomingMessage(platform="qq", platform_user_id="user", text="在吗"),
        ReceiptlessTarget(),
        key="c2c:user",
    )

    assert delivered is False
    assert engine.confirmed == []
    assert engine.failed == []


@pytest.mark.asyncio
async def test_legacy_afterthought_does_not_confirm_without_receipt() -> None:
    class Store:
        def resolve_user(self, _platform: str, _platform_user_id: str) -> str:
            return "geoff"

    class Engine:
        store = Store()

        async def generate_afterthought(
            self, _user_id: str, _reply_sent_at: datetime, *, mode: str
        ) -> str:
            assert mode == "quick_continue"
            return "补一句。"

        def queue_afterthought_delivery(self, *_args: object) -> int:
            return 21

        def confirm_afterthought_delivery(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("receiptless afterthought must not be confirmed")

    class ReceiptlessTarget:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"status": "accepted"}

    coalescer = QQMessageCoalescer(Engine(), delay_seconds=0.01, human_timing=False)
    result = await coalescer._fire_afterthought(
        "c2c:user",
        AfterthoughtPlan("quick_continue", 0.0, 1.0),
        IncomingMessage(platform="qq", platform_user_id="user", text="刚刚那件事"),
        ReceiptlessTarget(),
        datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    assert result is None


@pytest.mark.asyncio
async def test_legacy_expression_does_not_confirm_without_receipt() -> None:
    class Engine:
        async def handle_message(self, _incoming: IncomingMessage, **_kwargs: object) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="calm",
                text="给你。",
                media_action_id="media:22",
                image_path="assets/life/example.png",
            )

        def confirm_media_delivery(self, _reply: CompanionReply) -> None:
            raise AssertionError("receiptless image must not be confirmed")

    class Target:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"message_id": "text-22"}

    async def image_without_receipt(
        _incoming: IncomingMessage, _reply: CompanionReply
    ) -> dict[str, str]:
        return {"status": "accepted"}

    coalescer = QQMessageCoalescer(
        Engine(), delay_seconds=0.01, human_timing=False, on_image=image_without_receipt
    )

    delivered = await coalescer._generate_and_send(
        IncomingMessage(platform="qq", platform_user_id="user", text="发一张"),
        Target(),
        key="c2c:user",
    )

    assert delivered is True


@pytest.mark.asyncio
async def test_coalescer_sends_generated_image_after_text_reply() -> None:
    class FakeEngine:
        async def handle_message(self, incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(
                canonical_user_id="geoff",
                mood="happy",
                text="发你啦",
                image_path="assets/life/reply.png",
            )

    class FakeTarget:
        async def reply(self, **kwargs) -> None:
            return None

    sent: list[tuple[str, str]] = []

    async def send_image(incoming: IncomingMessage, reply: CompanionReply) -> None:
        sent.append((incoming.platform_user_id, reply.image_path or ""))

    coalescer = QQMessageCoalescer(
        FakeEngine(),
        delay_seconds=0.01,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
        on_image=send_image,
    )

    await coalescer.add(
        "c2c:user",
        IncomingMessage(platform="qq", platform_user_id="user", text="发张自拍"),
        FakeTarget(),
    )
    await asyncio.sleep(0.03)

    assert sent == [("user", "assets/life/reply.png")]


@pytest.mark.asyncio
async def test_turn_presenter_requires_a_durable_receipt_for_expression_delivery() -> None:
    """An adapter return is not proof that a sticker or image was delivered."""

    class Engine:
        world_id = "world-1"

        @staticmethod
        def begin_reaction_delivery(
            _incoming: IncomingMessage, _reply: CompanionReply
        ) -> str:
            return "reaction-action"

    observations: list[ExternalObservation] = []

    async def settle(observation: ExternalObservation):
        observations.append(observation)

    async def send_sticker(_incoming: IncomingMessage, _reply: CompanionReply) -> dict[str, str]:
        return {"message_id": "sticker-receipt"}

    async def send_image_without_receipt(
        _incoming: IncomingMessage, _reply: CompanionReply
    ) -> dict[str, str]:
        return {"status": "ok"}

    async def send_reaction(
        _incoming: IncomingMessage, _reply: CompanionReply
    ) -> DispatchAcceptance:
        return DispatchAcceptance(status="delivered", external_receipt="onebot-reaction:1:2")

    terminal: list[bool] = []
    presenter = QQTurnPresenter(
        Engine(),  # type: ignore[arg-type]
        on_reply=None,
        on_sticker=send_sticker,
        on_image=send_image_without_receipt,
        on_reaction=send_reaction,
        after_delivered=None,
        after_terminal=lambda: terminal.append(True),
        settle_external=settle,
    )
    incoming = IncomingMessage(
        platform="qq", platform_user_id="user", message_id="message-1", text="看看"
    )
    presentation = TurnPresentation(
        action_id="text-action",
        incoming=incoming,
        canonical_user_id="user",
        suggested_reaction="haha",
        sticker_path="sticker.png",
        image_path="image.png",
        media_action_id="media-action",
        sticker_action_id="sticker-action",
    )

    await presenter.before_text(presentation)
    await presenter.after_text(presentation, "delivered")

    assert terminal == [True]
    assert [(item.action_id, item.kind, item.status) for item in observations] == [
        ("reaction-action", "media_result", "delivered"),
        ("sticker-action", "media_result", "delivered"),
        ("media-action", "timeout", None),
    ]
    assert observations[1].external_receipt is None
    assert observations[1].payload["external_receipt"] == "platform:message_id:sticker-receipt"
    assert observations[2].payload["reason"] == "qq_image_returned_without_durable_receipt"


@pytest.mark.asyncio
async def test_turn_presenter_settles_unreceipted_reaction_as_unknown() -> None:
    class Engine:
        world_id = "world-1"

        @staticmethod
        def begin_reaction_delivery(
            _incoming: IncomingMessage, _reply: CompanionReply
        ) -> str:
            return "reaction-action"

    observations: list[ExternalObservation] = []

    async def settle(observation: ExternalObservation) -> None:
        observations.append(observation)

    async def send_reaction(
        _incoming: IncomingMessage, _reply: CompanionReply
    ) -> DispatchAcceptance:
        return DispatchAcceptance(
            status="unknown",
            reason="onebot_reaction_returned_without_durable_receipt",
        )

    presenter = QQTurnPresenter(
        Engine(),  # type: ignore[arg-type]
        on_reply=None,
        on_sticker=None,
        on_image=None,
        on_reaction=send_reaction,
        after_delivered=None,
        after_terminal=lambda: None,
        settle_external=settle,
    )
    incoming = IncomingMessage(
        platform="qq", platform_user_id="user", message_id="message-1", text="看看"
    )
    presentation = TurnPresentation(
        action_id="text-action",
        incoming=incoming,
        canonical_user_id="user",
        suggested_reaction="haha",
        sticker_path=None,
        image_path=None,
        media_action_id=None,
        sticker_action_id=None,
    )

    await presenter.before_text(presentation)

    assert [(item.action_id, item.kind, item.status) for item in observations] == [
        ("reaction-action", "timeout", None),
    ]
    assert observations[0].payload["reason"] == "onebot_reaction_returned_without_durable_receipt"


@pytest.mark.asyncio
async def test_turn_presenter_settles_onebot_image_rejection_as_failed() -> None:
    class Engine:
        world_id = "world-1"

        @staticmethod
        def begin_reaction_delivery(
            _incoming: IncomingMessage, _reply: CompanionReply
        ) -> str:
            return "reaction-action"

    observations: list[ExternalObservation] = []

    async def settle(observation: ExternalObservation) -> None:
        observations.append(observation)

    async def reject_image(
        _incoming: IncomingMessage, _reply: CompanionReply
    ) -> DispatchAcceptance:
        return DispatchAcceptance(status="failed", reason="permission denied")

    presenter = QQTurnPresenter(
        Engine(),  # type: ignore[arg-type]
        on_reply=None,
        on_sticker=None,
        on_image=reject_image,
        on_reaction=None,
        after_delivered=None,
        after_terminal=lambda: None,
        settle_external=settle,
    )
    incoming = IncomingMessage(
        platform="qq", platform_user_id="user", message_id="message-1", text="看看"
    )
    presentation = TurnPresentation(
        action_id="text-action",
        incoming=incoming,
        canonical_user_id="user",
        suggested_reaction=None,
        sticker_path=None,
        image_path="image.png",
        media_action_id="media-action",
        sticker_action_id=None,
    )

    await presenter.after_text(presentation, "delivered")

    assert [(item.action_id, item.kind, item.status) for item in observations] == [
        ("media-action", "media_result", "failed"),
    ]
    assert observations[0].payload["reason"] == "permission denied"


@pytest.mark.asyncio
async def test_new_hot_message_cancels_and_settles_claimed_turn_before_merge_recovery(
    tmp_path: Path,
) -> None:
    class BlockingFirstModel:
        def __init__(self) -> None:
            self.calls = 0
            self.first_started = asyncio.Event()

        async def complete(self, _messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                self.first_started.set()
                await asyncio.Event().wait()
            return json.dumps(
                {
                    "reply_text": "收到。",
                    "mentioned_event_ids": [],
                    "proposed_action_ids": [],
                    "claims": [],
                },
                ensure_ascii=False,
            )

    class FakeTarget:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"message_id": "qq-race-receipt"}

    store = CompanionStore(tmp_path / "cancelled-hot-turn.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    model = BlockingFirstModel()
    engine = CompanionEngine(
        store,
        model,
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.01,
        human_timing=False,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
    )
    target = FakeTarget()

    await coalescer.add(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            message_id="race-1",
            text="第一条",
        ),
        target,
    )
    await asyncio.wait_for(model.first_started.wait(), timeout=2)
    await coalescer.add(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            message_id="race-2",
            text="第二条",
        ),
        target,
    )
    await asyncio.sleep(0.3)

    snapshot = world.snapshot(world_id)
    assert snapshot["turns"]["qq:user:race-1"]["status"] == "failed"
    assert snapshot["turns"]["qq:user:race-1"]["reason"] == "adapter_generation_cancelled"
    assert any(
        action.get("kind") == "model_call" and action.get("status") == "failed"
        for action in snapshot["actions"].values()
    )
    assert any(
        item.get("message_id") == "qq:user:race-2" and item.get("text") == "第一条\n第二条"
        for item in snapshot["recent_messages"]
    )


@pytest.mark.asyncio
async def test_world_mode_uses_companion_turn_for_receipted_text_delivery(
    tmp_path: Path,
) -> None:
    class ReplyModel:
        async def complete(self, _messages, *, temperature: float) -> str:
            return json.dumps(
                {
                    "reply_text": "我听见了。",
                    "mentioned_event_ids": [],
                    "proposed_action_ids": [],
                    "claims": [],
                },
                ensure_ascii=False,
            )

    class Target:
        def __init__(self) -> None:
            self.contents: list[str] = []

        async def reply(self, *, content: str, **_kwargs: object) -> dict[str, str]:
            self.contents.append(content)
            return {"message_id": "qq-v2-receipt"}

    store = CompanionStore(tmp_path / "qq-v2.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        ReplyModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    coalescer = QQMessageCoalescer(engine, delay_seconds=0.01, human_timing=False)
    target = Target()

    delivered = await coalescer._generate_and_send(
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            message_id="qq-v2-inbound",
            text="我今天有点累。",
        ),
        target,
        key="c2c:user",
    )

    assert delivered is True
    await asyncio.sleep(0.35)
    assert len(target.contents) >= 1
    assert all(content.strip() for content in target.contents)
    action = next(
        item
        for item in world.snapshot(world_id)["actions"].values()
        if item.get("kind") == "outgoing_message"
    )
    assert action["status"] == "delivered"
    assert all(
        segment["external_receipt"] == "platform:message_id:qq-v2-receipt"
        for segment in action["segment_state"]["segments"]
    )
    assert coalescer._active_turns == {}


@pytest.mark.asyncio
async def test_production_world_engine_fails_closed_before_legacy_qq_confirm_helpers(
    tmp_path: Path,
) -> None:
    """A malformed World runtime must not revive the legacy QQ outbox seam."""

    store = CompanionStore(tmp_path / "qq-world-legacy-gate.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    # Simulate a damaged/misconfigured production runtime.  Before the gate,
    # this exact shape selected the legacy ``handle_message`` + ``confirm_*``
    # code below the v2 branch.
    engine.world_kernel = object()  # type: ignore[assignment]

    async def legacy_generation_must_not_run(*_args: object, **_kwargs: object) -> CompanionReply:
        raise AssertionError("World QQ delivery must not enter legacy handle_message")

    def legacy_confirm_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("World QQ delivery must not enter legacy confirm helpers")

    engine.handle_message = legacy_generation_must_not_run  # type: ignore[method-assign]
    engine.confirm_reply_delivery = legacy_confirm_must_not_run  # type: ignore[method-assign]
    engine.confirm_reply_part_delivery = legacy_confirm_must_not_run  # type: ignore[method-assign]

    class Target:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            raise AssertionError("World QQ delivery must not send adapter fallback text")

    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.0,
        human_timing=False,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.0, long_wait_seconds=0.0),
    )
    with pytest.raises(WorldError, match="requires a WorldKernel"):
        await coalescer._generate_and_send(
            IncomingMessage(
                platform="qq",
                platform_user_id="user",
                message_id="world-legacy-gate",
                text="你在吗？",
            ),
            Target(),
            key="c2c:user",
        )


@pytest.mark.asyncio
async def test_world_turn_failure_never_falls_back_to_legacy_qq_confirms(tmp_path: Path) -> None:
    """A failed CompanionTurn leaves the Action seam; it does not emit legacy text."""

    store = CompanionStore(tmp_path / "qq-world-turn-failure.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )

    def legacy_confirm_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("World turn failure must not invoke legacy confirm helpers")

    engine.confirm_reply_delivery = legacy_confirm_must_not_run  # type: ignore[method-assign]
    engine.confirm_reply_part_delivery = legacy_confirm_must_not_run  # type: ignore[method-assign]

    class Target:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            raise AssertionError("World turn failure must not send adapter fallback text")

    observations: list[object] = []
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0.0,
        human_timing=False,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.0, long_wait_seconds=0.0),
        on_turn_observation=observations.append,
    )

    async def fail_v2(*_args: object, **_kwargs: object) -> bool:
        raise WorldError("simulated CompanionTurn failure")

    coalescer._generate_and_send_v2 = fail_v2  # type: ignore[method-assign]
    await coalescer.add(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            message_id="world-turn-failure",
            text="还在吗？",
        ),
        Target(),
    )
    await asyncio.sleep(0.03)

    assert len(observations) == 1
    assert observations[0].outcome == "companion_turn_failed"
    assert observations[0].failure_type == "WorldError"


@pytest.mark.asyncio
async def test_world_mode_does_not_start_a_turn_after_the_visible_budget_is_spent(
    tmp_path: Path,
) -> None:
    class ReplyModel:
        async def complete(self, _messages, *, temperature: float) -> str:
            raise AssertionError("model must not run after the visible deadline")

    class Target:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            raise AssertionError("transport must not run after the visible deadline")

    store = CompanionStore(tmp_path / "qq-expired-budget.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        ReplyModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    clock = iter((0.0, 2.0, 2.0))

    async def no_wait(_seconds: float) -> None:
        return None

    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=0,
        human_timing=False,
        response_timeout_seconds=1.0,
        sleep=no_wait,
        monotonic=lambda: next(clock),
    )
    await coalescer.add(
        "c2c:user",
        IncomingMessage(
            platform="qq",
            platform_user_id="user",
            message_id="budget-expired",
            text="还在吗？",
        ),
        Target(),
    )
    await asyncio.sleep(0.01)

    snapshot = world.snapshot(world_id)
    assert "qq:user:budget-expired" in snapshot["turns"]
    assert not any(
        action.get("kind") == "outgoing_message" for action in snapshot["actions"].values()
    )
