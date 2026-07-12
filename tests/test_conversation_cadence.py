import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.qq_websocket import QQMessageCoalescer
from companion_daemon.turn_taking import TurnInput, TurnTakingPolicy
from companion_daemon.world_behavior import WorldBehaviorPolicy


@dataclass(frozen=True)
class _CadenceProbe:
    """Shape of the planned public cadence value, kept local while tests are red."""

    heat: str
    observed_gap_seconds: float | None
    alternating_turns: int
    reason: str


def _cold() -> _CadenceProbe:
    return _CadenceProbe("cold", None, 0, "no_recent_delivered_exchange")


def _hot() -> _CadenceProbe:
    return _CadenceProbe("hot", 20.0, 3, "active_back_and_forth")


def test_hot_complete_turn_merges_much_faster_than_a_cold_first_turn() -> None:
    policy = TurnTakingPolicy(short_wait_seconds=2.5, long_wait_seconds=5.5)
    turn = TurnInput(
        pending_count=1,
        latest_text="接着说，你觉得呢？",
        merged_text="接着说，你觉得呢？",
    )

    cold = policy.decide(turn, cadence=_cold())
    hot = policy.decide(turn, cadence=_hot())

    assert 1.5 <= cold.wait_seconds <= 3.0
    assert hot.wait_seconds <= 0.6
    assert hot.wait_seconds <= cold.wait_seconds * 0.4


def test_hot_longform_opener_is_quick_but_explicit_wait_cue_still_waits() -> None:
    policy = TurnTakingPolicy(
        short_wait_seconds=2.5,
        long_wait_seconds=5.5,
        longform_start_seconds=300.0,
    )

    opener = policy.decide(
        TurnInput(1, "我跟你说", "我跟你说"),
        cadence=_hot(),
    )
    explicit_wait = policy.decide(
        TurnInput(1, "等我一下，我组织一下语言", "等我一下，我组织一下语言"),
        cadence=_hot(),
    )

    assert opener.wait_seconds <= 2.0
    assert explicit_wait.wait_seconds >= 5.0
    assert explicit_wait.reason == "user_thinking_or_hesitating"


@pytest.mark.parametrize(
    ("state_patch", "text", "expected_attention", "reason_fragment"),
    [
        (
            {
                "agenda": {
                    "class": {
                        "status": "active",
                        "starts_at": "2026-07-12T14:00:00+08:00",
                        "ends_at": "2026-07-12T17:00:00+08:00",
                        "attention_demand": 90,
                        "interruptible": False,
                    }
                }
            },
            "刚才说到哪了？",
            "deferred",
            "active_world_activity_not_interruptible",
        ),
        (
            {
                "needs": {
                    "energy": 60,
                    "attention": 55,
                    "security": 15,
                    "boundary": 80,
                }
            },
            "你必须马上回我",
            "do_not_disturb",
            "boundary_high_under_pressure",
        ),
        (
            {
                "emotion_modulation": {
                    "behavior_tendency": "withdraw",
                    "vector": {"hurt": 75, "anger": 45},
                }
            },
            "继续聊啊",
            "deferred",
            "unresolved_hurt",
        ),
    ],
)
def test_hot_cadence_does_not_override_activity_or_negative_affect_boundaries(
    state_patch: dict[str, object],
    text: str,
    expected_attention: str,
    reason_fragment: str,
) -> None:
    state: dict[str, object] = {
        "clock": {"logical_at": "2026-07-12T15:00:00+08:00"},
        "needs": {
            "energy": 70,
            "attention": 55,
            "security": 50,
            "boundary": 0,
        },
        "agenda": {},
        "relationships": {"user:geoff": {"stage": "stranger", "trust": 0}},
        "emotion_modulation": {},
    }
    state.update(state_patch)

    decision = WorldBehaviorPolicy().communication_decision(
        state,
        text=text,
        user_id="user:geoff",
        cadence=_hot(),
    )

    assert decision.attention == expected_attention
    assert reason_fragment in decision.reason


@pytest.mark.asyncio
async def test_qq_coalescer_uses_public_cadence_seam_for_the_next_live_turn() -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.completed_turns = 0

        def conversation_cadence(self, _message: IncomingMessage) -> _CadenceProbe:
            return _hot() if self.completed_turns else _cold()

        async def handle_message(
            self, incoming: IncomingMessage, **_kwargs: object
        ) -> CompanionReply:
            self.completed_turns += 1
            return CompanionReply(
                canonical_user_id="geoff",
                mood="calm",
                text=f"收到第{self.completed_turns}轮。",
            )

    class FakeTarget:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"id": "qq-receipt"}

    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    engine = FakeEngine()
    coalescer = QQMessageCoalescer(
        engine,  # type: ignore[arg-type]
        delay_seconds=2.5,
        turn_policy=TurnTakingPolicy(short_wait_seconds=2.5, long_wait_seconds=5.5),
        sleep=record_sleep,
    )
    target = FakeTarget()

    await coalescer.add(
        "c2c:geoff",
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="cadence-first",
            text="你觉得呢？",
        ),
        target,
    )
    await asyncio.sleep(0.01)
    first_merge_wait = sleeps[0]

    sleeps.clear()
    await coalescer.add(
        "c2c:geoff",
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="cadence-second",
            text="那接着说？",
        ),
        target,
    )
    await asyncio.sleep(0.01)
    second_merge_wait = sleeps[0]

    assert 1.5 <= first_merge_wait <= 3.0
    assert second_merge_wait <= 0.6
    assert second_merge_wait <= first_merge_wait * 0.4


def test_cadence_uses_observed_time_instead_of_virtual_logical_time() -> None:
    cadence_module = import_module("companion_daemon.conversation_cadence")
    derive_conversation_cadence = cadence_module.derive_conversation_cadence
    observed_now = datetime(2026, 7, 12, 4, 0, 30, tzinfo=timezone.utc)
    state = {
        # A paused or accelerated virtual clock must not decide chat warmth.
        "clock": {"logical_at": "2042-01-01T00:00:00+08:00"},
        "recent_messages": [
            {
                "direction": "in",
                "user_id": "user:geoff",
                "text": "刚才那个话题",
                "logical_at": "1999-01-01T00:00:00+08:00",
                "observed_at": (observed_now - timedelta(seconds=25)).isoformat(),
            },
            {
                "direction": "out",
                "user_id": "user:geoff",
                "text": "嗯，你继续",
                "logical_at": "2042-01-01T00:00:00+08:00",
                "observed_at": (observed_now - timedelta(seconds=12)).isoformat(),
                "delivery_status": "delivered",
            },
        ],
    }

    cadence = derive_conversation_cadence(
        state,
        user_id="user:geoff",
        observed_at=observed_now,
    )

    assert cadence.heat == "hot"
    assert cadence.observed_gap_seconds == pytest.approx(12.0)


def test_recent_logical_time_cannot_make_an_observationally_cold_chat_hot() -> None:
    cadence_module = import_module("companion_daemon.conversation_cadence")
    derive_conversation_cadence = cadence_module.derive_conversation_cadence
    observed_now = datetime(2026, 7, 12, 4, 30, tzinfo=timezone.utc)
    state = {
        "clock": {"logical_at": "2026-07-12T12:30:00+08:00"},
        "recent_messages": [
            {
                "direction": "out",
                "user_id": "user:geoff",
                "text": "上次聊到这里",
                "logical_at": "2026-07-12T12:29:59+08:00",
                "observed_at": (observed_now - timedelta(minutes=20)).isoformat(),
                "delivery_status": "delivered",
            }
        ],
    }

    cadence = derive_conversation_cadence(
        state,
        user_id="user:geoff",
        observed_at=observed_now,
    )

    assert cadence.heat == "cold"


def test_legacy_runtime_derives_hot_cadence_from_delivered_chat_history(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "legacy-cadence.sqlite")
    seed_user(store)
    now = datetime.now(timezone.utc)
    store.save_incoming(
        "geoff",
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="legacy-cadence-in",
            text="刚才说到这里",
            sent_at=now - timedelta(seconds=20),
        ),
    )
    delivery_id = store.queue_outgoing("geoff", "qq", "嗯，你继续", kind="reply")
    store.mark_outgoing_delivered(delivery_id)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是沈知栀。")

    cadence = engine.conversation_cadence(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="legacy-cadence-next",
            text="那接着说？",
            sent_at=now,
        )
    )

    assert cadence.heat == "hot"
    assert cadence.alternating_turns >= 2
