from companion_daemon.emotion_core import apply_emotion_deltas
from companion_daemon.impression import apply_repeated_interaction_drift
from companion_daemon.models import MoodState
from companion_daemon.personality_drift import (
    affinity_aversion_score,
    affinity_warmth_score,
    apply_personality_drift,
)


def test_warmth_score_reflects_sustained_positive_affinity() -> None:
    state = MoodState()
    for _ in range(6):
        state = apply_emotion_deltas(
            state,
            {"trust": 8, "joy": 6, "love": 4},
            source="user_message",
            update_affinity=True,
        )

    assert affinity_warmth_score(state.emotion_affinity) >= 4.0


def test_personality_drift_relaxes_after_sustained_warmth() -> None:
    state = MoodState(security=45, curiosity=40, boundary_level=10)
    for _ in range(6):
        state = apply_emotion_deltas(
            state,
            {"trust": 8, "joy": 6, "love": 4},
            source="user_message",
            update_affinity=True,
        )

    drifted = apply_personality_drift(state)

    assert drifted.security == state.security + 1
    assert drifted.curiosity == state.curiosity + 1
    assert drifted.boundary_level == state.boundary_level - 1


def test_personality_drift_tightens_after_repeated_aversion() -> None:
    state = apply_repeated_interaction_drift(
        MoodState(security=45, patience=50, boundary_level=10),
        [
            {"event_kind": "boundary_violation"},
            {"event_kind": "control_pressure"},
            {"event_kind": "boundary_violation"},
        ],
    )

    assert affinity_aversion_score(state.emotion_affinity) >= 1.4
    drifted = apply_personality_drift(state)

    assert drifted.security == state.security - 1
    assert drifted.patience == state.patience - 1
    assert drifted.boundary_level == state.boundary_level + 1


def test_neutral_affinity_does_not_drift() -> None:
    state = MoodState(security=45, patience=50, boundary_level=10)

    assert apply_personality_drift(state) == state
