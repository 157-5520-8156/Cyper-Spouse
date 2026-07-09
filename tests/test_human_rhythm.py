from datetime import UTC, datetime

from companion_daemon.human_rhythm import (
    apply_expression_after_reply,
    human_rhythm_context_line,
    human_rhythm_snapshot,
    proactive_rhythm_context_line,
)
from companion_daemon.models import MoodState


def test_human_rhythm_uses_chengdu_local_time() -> None:
    snapshot = human_rhythm_snapshot(
        MoodState(),
        datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
    )

    assert snapshot.local_hour == 20
    assert snapshot.phase == "evening_unwind"
    assert "分享欲" in snapshot.private_activity
    assert "分享小近况" in snapshot.proactive_guidance


def test_human_rhythm_context_discourages_stage_directions() -> None:
    line = human_rhythm_context_line(
        MoodState(mood="miss_you"),
        datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
    )

    assert "生活节律" in line
    assert "不要写舞台动作" in line
    assert "想起你但又不想显得太黏" in line


def test_proactive_rhythm_respects_guarded_boundary() -> None:
    line = proactive_rhythm_context_line(
        MoodState(mood="guarded", boundary_level=40),
        datetime(2026, 7, 9, 13, 0, tzinfo=UTC),
    )

    assert "多数情况下不主动" in line


def test_expression_after_proactive_reduces_initiative_and_charge() -> None:
    state = MoodState(mood="miss_you", initiative=70, attachment=30, emotional_charge=20)

    updated = apply_expression_after_reply(state, was_proactive=True, sent_image=True)

    assert updated.initiative < state.initiative
    assert updated.attachment < state.attachment
    assert updated.emotional_charge < state.emotional_charge
