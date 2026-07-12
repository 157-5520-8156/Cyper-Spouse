from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.conversation_commitments import (
    ConversationCommitmentError,
    create_conversation_thread,
    evaluate_waiting_response,
)


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "kind",
    (
        "question",
        "comfort",
        "promise",
        "contradiction",
        "life_share",
        "reply_reconsider",
        "pulse",
    ),
)
def test_every_conversation_commitment_has_one_traceable_lifecycle(kind: str) -> None:
    thread = create_conversation_thread(
        thread_id=f"thread:{kind}",
        kind=kind,
        user_id="user:geoff",
        origin={"kind": "world_event", "reference": "UserMessageObserved:m-1"},
        reason="这件事还需要一个自然的后续",
        due_at=NOW + timedelta(minutes=30),
        expires_at=NOW + timedelta(hours=8),
        cancel_conditions=("user_returned", "topic_superseded"),
        owner="world:conversation",
    )

    assert thread.as_payload() == {
        "thread_id": f"thread:{kind}",
        "kind": kind,
        "user_id": "user:geoff",
        "origin": {"kind": "world_event", "reference": "UserMessageObserved:m-1"},
        "reason": "这件事还需要一个自然的后续",
        "due_at": "2026-07-11T09:30:00+00:00",
        "expires_at": "2026-07-11T17:00:00+00:00",
        "cancel_conditions": ["user_returned", "topic_superseded"],
        "owner": "world:conversation",
        "status": "open",
        "terminal_state": None,
        "waiting_phase": "not_due",
        "waiting_changed_at": None,
        "rule_version": "conversation-commitments-v1",
    }


def test_commitment_rejects_an_unowned_or_impossible_lifecycle() -> None:
    with pytest.raises(ConversationCommitmentError, match="owner"):
        create_conversation_thread(
            thread_id="thread:bad-owner",
            kind="question",
            user_id="user:geoff",
            origin={"kind": "world_event", "reference": "m-1"},
            reason="awaiting answer",
            due_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            cancel_conditions=("user_answered",),
            owner="",
        )
    with pytest.raises(ConversationCommitmentError, match="expires_at"):
        create_conversation_thread(
            thread_id="thread:bad-time",
            kind="question",
            user_id="user:geoff",
            origin={"kind": "world_event", "reference": "m-1"},
            reason="awaiting answer",
            due_at=NOW + timedelta(hours=2),
            expires_at=NOW + timedelta(hours=1),
            cancel_conditions=("user_answered",),
            owner="world:conversation",
        )


def test_stranger_waiting_curve_holds_back_then_lets_go_without_romantic_grievance() -> None:
    thread = create_conversation_thread(
        thread_id="thread:stranger-pulse",
        kind="pulse",
        user_id="user:geoff",
        origin={"kind": "action", "reference": "outgoing:1"},
        reason="留了一点对话余韵",
        due_at=NOW,
        expires_at=NOW + timedelta(hours=24),
        cancel_conditions=("user_returned", "newer_outbound"),
        owner="world:conversation",
    )

    early = evaluate_waiting_response(
        thread, relationship={"stage": "stranger", "reliability": 0}, logical_at=NOW + timedelta(minutes=15)
    )
    middle = evaluate_waiting_response(
        thread, relationship={"stage": "stranger", "reliability": 0}, logical_at=NOW + timedelta(hours=2)
    )
    late = evaluate_waiting_response(
        thread, relationship={"stage": "stranger", "reliability": 0}, logical_at=NOW + timedelta(hours=8)
    )

    assert early.phase == "anticipating"
    assert middle.phase == "holding_back"
    assert late.phase == "letting_go"
    assert late.relationship_deltas == {}
    assert late.expression_policy == "收住这次等待，不索取解释，也不使用亲密关系式委屈。"
    assert "mildly_hurt" not in {early.phase, middle.phase, late.phase}


def test_close_relationship_can_be_mildly_hurt_by_an_ignored_promise() -> None:
    thread = create_conversation_thread(
        thread_id="thread:close-promise",
        kind="promise",
        user_id="user:geoff",
        origin={"kind": "world_event", "reference": "UserMessageObserved:promise-1"},
        reason="对方说晚点会回来继续说",
        due_at=NOW,
        expires_at=NOW + timedelta(days=2),
        cancel_conditions=("promise_fulfilled", "promise_cancelled"),
        owner="world:conversation",
    )

    outcome = evaluate_waiting_response(
        thread,
        relationship={"stage": "close_friend", "reliability": 70},
        logical_at=NOW + timedelta(hours=16),
    )

    assert outcome.phase == "mildly_hurt"
    assert outcome.reason == "close_relationship_reliable_promise_unanswered"
    assert outcome.relationship_deltas == {"reliability": -1}
    assert outcome.expression_policy == "可以承认一点失落，但不指控、不惩罚，也不把沉默解释成拒绝。"


def test_message_kind_and_reliability_change_the_curve_instead_of_using_one_timer() -> None:
    pulse = create_conversation_thread(
        thread_id="thread:pulse",
        kind="pulse",
        user_id="user:geoff",
        origin={"kind": "action", "reference": "outgoing:2"},
        reason="短暂余韵",
        due_at=NOW,
        expires_at=NOW + timedelta(days=2),
        cancel_conditions=("user_returned",),
        owner="world:conversation",
    )
    contradiction = create_conversation_thread(
        thread_id="thread:contradiction",
        kind="contradiction",
        user_id="user:geoff",
        origin={"kind": "world_event", "reference": "UserMessageObserved:m-2"},
        reason="前后信息仍待澄清",
        due_at=NOW,
        expires_at=NOW + timedelta(days=2),
        cancel_conditions=("clarified",),
        owner="world:conversation",
    )

    low_reliability_pulse = evaluate_waiting_response(
        pulse,
        relationship={"stage": "friend", "reliability": -50},
        logical_at=NOW + timedelta(hours=5),
    )
    high_reliability_contradiction = evaluate_waiting_response(
        contradiction,
        relationship={"stage": "friend", "reliability": 70},
        logical_at=NOW + timedelta(hours=5),
    )

    assert low_reliability_pulse.phase == "letting_go"
    assert high_reliability_contradiction.phase == "confused"
    assert low_reliability_pulse.next_review_at != high_reliability_contradiction.next_review_at

