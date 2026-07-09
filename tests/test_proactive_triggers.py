from datetime import datetime, timedelta
import random

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.proactive_triggers import evaluate_proactive_trigger
from companion_daemon.time import utc_now


def _row(direction: str, text: str, hours_ago: float) -> dict[str, str]:
    return {
        "direction": direction,
        "platform": "qq",
        "text": text,
        "sent_at": (utc_now() - timedelta(hours=hours_ago)).isoformat(),
    }


def test_proactive_trigger_selects_hanging_question() -> None:
    trigger = evaluate_proactive_trigger(
        state=MoodState(emotion_vector={"trust": 45, "anticipation": 45}),
        recent_messages=[_row("in", "所以你觉得呢？", 1)],
        trigger_history={},
        now=utc_now(),
        rng=random.Random(1),
    )

    assert trigger
    assert trigger.type == "pregnant_pause"


def test_proactive_trigger_respects_anger_ghost_window() -> None:
    trigger = evaluate_proactive_trigger(
        state=MoodState(emotion_vector={"anger": 80, "disgust": 40}),
        recent_messages=[_row("in", "你怎么不说话", 4)],
        trigger_history={},
        now=utc_now(),
        rng=random.Random(1),
    )

    assert trigger is None


def test_proactive_trigger_category_cooldown_blocks_similar_outreach() -> None:
    now = utc_now()
    trigger = evaluate_proactive_trigger(
        state=MoodState(emotion_vector={"joy": 80, "trust": 80, "anticipation": 70}),
        recent_messages=[_row("in", "我去忙了", 3)],
        trigger_history={"sharing_impulse": now - timedelta(hours=1)},
        now=now,
        rng=random.Random(1),
    )

    assert trigger
    assert trigger.category != "happy_outreach"


def test_proactive_trigger_stops_after_unanswered_outgoing_messages() -> None:
    trigger = evaluate_proactive_trigger(
        state=MoodState(emotion_vector={"joy": 80, "trust": 80, "anticipation": 70}),
        recent_messages=[
            _row("in", "我去忙了", 4),
            _row("out", "那你先忙。", 3),
            _row("out", "我刚刚又想到一句。", 2),
        ],
        trigger_history={},
        now=utc_now(),
        rng=random.Random(1),
    )

    assert trigger is None


def test_proactive_trigger_detects_her_unanswered_question() -> None:
    trigger = evaluate_proactive_trigger(
        state=MoodState(emotion_vector={"trust": 45, "anticipation": 45}),
        recent_messages=[
            _row("in", "我先忙一会儿", 2),
            _row("out", "你刚刚是不是在忙？", 1),
        ],
        trigger_history={},
        now=utc_now(),
        rng=random.Random(4),
    )

    assert trigger
    assert trigger.type in {"her_question_unanswered", "double_text", "seen_no_reply_soft"}


def test_proactive_trigger_can_continue_open_thread() -> None:
    now = datetime.fromisoformat("2026-07-10T10:00:00+00:00")
    trigger = evaluate_proactive_trigger(
        state=MoodState(emotion_vector={"trust": 45, "anger": 10}),
        recent_messages=[
            {
                "direction": "in",
                "platform": "qq",
                "text": "我刚到家",
                "sent_at": (now - timedelta(hours=0.4)).isoformat(),
            },
            {
                "direction": "out",
                "platform": "qq",
                "text": "那你先歇一会儿。",
                "sent_at": (now - timedelta(hours=0.3)).isoformat(),
            },
        ],
        trigger_history={},
        now=now,
        rng=random.Random(5),
    )

    assert trigger
    assert trigger.type == "open_thread_afterthought"


def test_proactive_trigger_includes_daily_life_rhythm() -> None:
    now = datetime.fromisoformat("2026-07-10T15:00:00+00:00")
    trigger = evaluate_proactive_trigger(
        state=MoodState(emotion_vector={"joy": 20, "trust": 20, "anticipation": 20}),
        recent_messages=[
            {
                "direction": "in",
                "platform": "qq",
                "text": "下午还要忙一会儿",
                "sent_at": (now - timedelta(hours=3)).isoformat(),
            }
        ],
        trigger_history={},
        now=now,
        rng=random.Random(2),
    )

    assert trigger
    assert trigger.type in {"afternoon_slump", "random_thought", "boredom_break", "craving_share"}


def test_proactive_trigger_follows_up_after_emotion_shift() -> None:
    now = datetime.fromisoformat("2026-07-10T10:00:00+00:00")
    trigger = evaluate_proactive_trigger(
        state=MoodState(
            emotion_vector={"trust": 45, "sadness": 45},
            last_emotion_impact={"sadness": 20, "trust": -8},
        ),
        recent_messages=[
            {
                "direction": "in",
                "platform": "qq",
                "text": "刚才说的话有点重",
                "sent_at": (now - timedelta(hours=3)).isoformat(),
            }
        ],
        trigger_history={},
        now=now,
        rng=random.Random(3),
    )

    assert trigger
    assert trigger.type == "mood_follow_up"


@pytest.mark.asyncio
async def test_engine_persists_selected_proactive_trigger(tmp_path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是沈知栀。")

    await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="所以你觉得呢？",
            sent_at=utc_now() - timedelta(hours=1),
        )
    )
    decision = await engine.proactive_tick("geoff")

    assert decision.trigger_type == "pregnant_pause"
    assert store.recent_proactive_trigger_history("geoff")["pregnant_pause"]
