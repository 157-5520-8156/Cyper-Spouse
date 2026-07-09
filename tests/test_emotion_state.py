from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.emotion_state import interpret_interaction, transition_emotional_state
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.relationship import life_event_probability, proactive_cooldown_minutes


def test_rude_message_creates_hurt_boundary_state() -> None:
    previous = MoodState(trust=30, intimacy=20, patience=70, security=50)
    message = IncomingMessage(platform="qq", platform_user_id="geoff", text="闭嘴，别烦我")

    event = interpret_interaction(message, previous)
    state = transition_emotional_state(previous, event)

    assert event.kind == "boundary_violation"
    assert state.mood == "hurt"
    assert state.trust < previous.trust
    assert state.boundary_level > previous.boundary_level
    assert "边界" in state.reply_style_hint


def test_apology_repairs_but_does_not_reset_everything() -> None:
    previous = MoodState(mood="hurt", trust=20, patience=40, security=30, boundary_level=2)
    message = IncomingMessage(platform="qq", platform_user_id="geoff", text="对不起，刚才我不该那样说")

    event = interpret_interaction(message, previous)
    state = transition_emotional_state(previous, event)

    assert event.kind == "repair_attempt"
    assert state.mood == "calm"
    assert state.trust > previous.trust
    assert state.boundary_level == 1
    assert state.unresolved_emotion


def test_boundary_state_reduces_proactive_behavior() -> None:
    calm = MoodState(relationship_stage="friend", intimacy=40, trust=50, initiative=40)
    hurt = calm.model_copy(update={"mood": "hurt", "boundary_level": 20, "emotional_charge": 60})

    assert proactive_cooldown_minutes(hurt, 45) > proactive_cooldown_minutes(calm, 45)
    assert life_event_probability(hurt) < life_event_probability(calm)


@pytest.mark.asyncio
async def test_engine_records_interaction_events(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是沈知栀。")

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你必须听我的")
    )

    state = store.get_mood_state("geoff")
    events = store.recent_interaction_events("geoff")
    assert state.mood == "guarded"
    assert state.last_interaction_event == "control_pressure"
    assert events[-1]["event_kind"] == "control_pressure"
