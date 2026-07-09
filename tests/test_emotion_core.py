from datetime import timedelta

from companion_daemon.emotion_core import (
    apply_emotion_decay,
    apply_emotion_deltas,
    emotion_context_line,
    emotion_snapshot,
    enforce_opposites,
    text_emotion_deltas,
)
from companion_daemon.models import MoodState
from companion_daemon.relationship import emotion_ghost_window_hours, life_event_probability
from companion_daemon.time import utc_now


def test_text_emotion_deltas_detect_chinese_warmth() -> None:
    deltas = text_emotion_deltas("谢谢你还记得，我真的很开心！！")

    assert deltas["trust"] > 0
    assert deltas["joy"] > 0


def test_emotion_decay_returns_toward_baseline() -> None:
    past = utc_now() - timedelta(hours=2)
    state = MoodState(
        emotion_vector={"anger": 80, "joy": 5},
        emotion_baseline={"anger": 8, "joy": 25},
        updated_at=past,
    )

    decayed = apply_emotion_decay(state, utc_now())

    assert decayed.emotion_vector["anger"] < 80
    assert decayed.emotion_vector["joy"] > 5


def test_opposite_emotions_suppress_each_other() -> None:
    vector = enforce_opposites({"joy": 70, "sadness": 40, "love": 10, "disgust": 8, "trust": 20, "fear": 12, "surprise": 15, "anger": 8, "anticipation": 25})

    assert vector["sadness"] < 40


def test_affinity_drift_learns_from_repeated_warmth() -> None:
    state = MoodState()
    for _ in range(6):
        state = apply_emotion_deltas(
            state,
            {"trust": 8, "joy": 6, "love": 4},
            source="user_message",
            update_affinity=True,
        )

    assert state.emotion_affinity["trust"] > 0
    assert state.emotion_baseline["trust"] > 20


def test_emotion_context_contains_behavioral_guidance() -> None:
    state = MoodState(emotion_vector={"sadness": 58, "trust": 40})

    line = emotion_context_line(state)

    assert "情绪向量" in line
    assert "情绪指导" in line
    assert emotion_snapshot(state).dominant == "sadness"


def test_aversion_emotion_suppresses_proactive_probability() -> None:
    warm = MoodState(relationship_stage="friend", emotion_vector={"love": 50, "trust": 55, "anticipation": 50})
    angry = MoodState(relationship_stage="friend", emotion_vector={"anger": 75, "disgust": 55})

    assert emotion_ghost_window_hours(angry) > 0
    assert life_event_probability(angry) < life_event_probability(warm)
