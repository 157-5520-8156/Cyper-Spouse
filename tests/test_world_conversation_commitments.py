from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.world import WorldError, WorldKernel


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def _seed() -> dict[str, object]:
    return {
        "world_id": "commitment-world",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion"},
        "daily_schedule": [],
        "npcs": [],
    }


def _world(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "commitments.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": _seed()}, expected_revision=0)
    kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=started.revision,
    )
    return kernel, started.world_id


def test_delivered_legacy_question_is_upgraded_to_a_complete_commitment(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="你今天还好吗？",
        kind="reply",
        expires_at=NOW + timedelta(hours=2),
        trace={
            "world_id": world_id,
            "appraisal": "ordinary_message",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
            "conversation_thread": {
                "thread_id": "thread:question",
                "user_id": "user:geoff",
                "question": "你今天还好吗？",
                "expires_at": (NOW + timedelta(hours=24)).isoformat(),
            },
        },
    )

    kernel.settle_outgoing_action(delivery_id, delivered=True)
    thread = kernel.snapshot(world_id)["conversation_threads"]["thread:question"]

    assert thread == {
        "thread_id": "thread:question",
        "kind": "question",
        "user_id": "user:geoff",
        "origin": {"kind": "action", "reference": action_id},
        "reason": "awaiting_user_response",
        "due_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=24)).isoformat(),
        "cancel_conditions": ["user_answered", "user_declined", "topic_superseded"],
        "owner": "world:conversation",
        "status": "open",
        "terminal_state": None,
        "waiting_phase": "not_due",
        "waiting_changed_at": None,
        "rule_version": "conversation-commitments-v1",
        "question": "你今天还好吗？",
        "source_action_id": action_id,
    }


@pytest.mark.parametrize(
    "kind",
    ("question", "comfort", "promise", "contradiction", "life_share", "reply_reconsider", "pulse"),
)
def test_world_can_open_every_commitment_kind_through_one_command(
    tmp_path: Path, kind: str
) -> None:
    kernel, world_id = _world(tmp_path)
    decision = kernel.submit(
        {
            "type": "open_conversation_thread",
            "world_id": world_id,
            "thread": {
                "thread_id": f"thread:{kind}",
                "kind": kind,
                "user_id": "user:geoff",
                "origin": {"kind": "world_event", "reference": "UserMessageObserved:m-1"},
                "reason": "仍有一个自然后续",
                "due_at": (NOW + timedelta(minutes=20)).isoformat(),
                "expires_at": (NOW + timedelta(hours=8)).isoformat(),
                "cancel_conditions": ["user_returned", "topic_superseded"],
                "owner": "world:conversation",
            },
        },
        expected_revision=kernel.revision(world_id),
    )

    assert [event.event_type for event in decision.events] == ["ConversationThreadOpened"]
    thread = kernel.snapshot(world_id)["conversation_threads"][f"thread:{kind}"]
    assert thread["kind"] == kind
    assert thread["owner"] == "world:conversation"
    assert thread["terminal_state"] is None


def test_commitment_cancel_condition_and_terminal_state_are_audited(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    kernel.submit(
        {
            "type": "open_conversation_thread",
            "world_id": world_id,
            "thread": {
                "thread_id": "thread:pulse",
                "kind": "pulse",
                "user_id": "user:geoff",
                "origin": {"kind": "action", "reference": "outgoing:1"},
                "reason": "对话还有一点余韵",
                "due_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(hours=24)).isoformat(),
                "cancel_conditions": ["user_returned", "newer_outbound"],
                "owner": "world:conversation",
            },
        },
        expected_revision=kernel.revision(world_id),
    )

    with pytest.raises(WorldError, match="cancel condition"):
        kernel.submit(
            {
                "type": "cancel_conversation_thread",
                "world_id": world_id,
                "thread_id": "thread:pulse",
                "condition": "arbitrary_reason",
                "reason": "should not pass",
            },
            expected_revision=kernel.revision(world_id),
        )

    cancelled = kernel.submit(
        {
            "type": "cancel_conversation_thread",
            "world_id": world_id,
            "thread_id": "thread:pulse",
            "condition": "user_returned",
            "reason": "用户已回来，旧余韵不再追发",
        },
        expected_revision=kernel.revision(world_id),
    )
    thread = kernel.snapshot(world_id)["conversation_threads"]["thread:pulse"]
    assert [event.event_type for event in cancelled.events] == ["ConversationThreadCancelled"]
    assert thread["status"] == "cancelled"
    assert thread["terminal_state"] == "cancelled"
    assert thread["terminal_reason"] == "用户已回来，旧余韵不再追发"
    assert thread["terminal_condition"] == "user_returned"


def test_world_waiting_phase_uses_logical_time_and_never_sulks_at_a_stranger(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    kernel.submit(
        {
            "type": "open_conversation_thread",
            "world_id": world_id,
            "thread": {
                "thread_id": "thread:stranger-pulse",
                "kind": "pulse",
                "user_id": "user:geoff",
                "origin": {"kind": "action", "reference": "outgoing:2"},
                "reason": "短暂余韵",
                "due_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(hours=24)).isoformat(),
                "cancel_conditions": ["user_returned", "newer_outbound"],
                "owner": "world:conversation",
            },
        },
        expected_revision=kernel.revision(world_id),
    )

    kernel.advance(
        world_id,
        NOW + timedelta(hours=8),
        expected_revision=kernel.revision(world_id),
    )
    thread = kernel.snapshot(world_id)["conversation_threads"]["thread:stranger-pulse"]
    assert thread["waiting_phase"] == "letting_go"
    assert thread["waiting_expression_policy"] == "收住这次等待，不索取解释，也不使用亲密关系式委屈。"
    waiting_events = [
        event for event in kernel.events(world_id)
        if event.event_type == "ConversationThreadWaitingChanged"
    ]
    assert len(waiting_events) == 1
    assert waiting_events[0].payload["relationship_deltas"] == {}

    kernel.advance(
        world_id,
        NOW + timedelta(hours=9),
        expected_revision=kernel.revision(world_id),
    )
    assert len([
        event for event in kernel.events(world_id)
        if event.event_type == "ConversationThreadWaitingChanged"
    ]) == 1


def test_resolved_question_keeps_outcome_but_records_one_terminal_state(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    kernel.submit(
        {
            "type": "open_conversation_thread",
            "world_id": world_id,
            "thread": {
                "thread_id": "thread:answer",
                "kind": "question",
                "user_id": "user:geoff",
                "origin": {"kind": "action", "reference": "outgoing:3"},
                "reason": "等待问题回应",
                "due_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(hours=12)).isoformat(),
                "cancel_conditions": ["user_answered", "user_declined"],
                "owner": "world:conversation",
            },
        },
        expected_revision=kernel.revision(world_id),
    )
    kernel.submit(
        {
            "type": "resolve_conversation_thread",
            "world_id": world_id,
            "thread_id": "thread:answer",
            "outcome": "answered",
            "reason": "用户明确回答",
        },
        expected_revision=kernel.revision(world_id),
    )

    thread = kernel.snapshot(world_id)["conversation_threads"]["thread:answer"]
    assert thread["status"] == "answered"
    assert thread["terminal_state"] == "resolved"
    assert thread["terminal_outcome"] == "answered"


def test_close_reliable_relationship_waiting_cost_is_applied_once(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    for index in range(70):
        kernel.submit(
            {
                "type": "appraise_turn",
                "world_id": world_id,
                "intent_id": f"warm:{index}",
                "message_id": f"warm:{index}",
                "user_id": "user:geoff",
                "appraisal": "warmth_received",
            },
            expected_revision=kernel.revision(world_id),
        )
    relationship = kernel.snapshot(world_id)["relationships"]["user:geoff"]
    assert relationship["stage"] == "ambiguous"
    assert relationship["reliability"] == 70
    kernel.submit(
        {
            "type": "open_conversation_thread",
            "world_id": world_id,
            "thread": {
                "thread_id": "thread:reliable-promise",
                "kind": "promise",
                "user_id": "user:geoff",
                "origin": {"kind": "world_event", "reference": "UserMessageObserved:promise"},
                "reason": "对方说晚点会回来继续说",
                "due_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(days=2)).isoformat(),
                "cancel_conditions": ["promise_fulfilled", "promise_cancelled"],
                "owner": "world:conversation",
            },
        },
        expected_revision=kernel.revision(world_id),
    )

    kernel.advance(
        world_id,
        NOW + timedelta(hours=16),
        expected_revision=kernel.revision(world_id),
    )
    snapshot = kernel.snapshot(world_id)
    assert snapshot["conversation_threads"]["thread:reliable-promise"]["waiting_phase"] == "mildly_hurt"
    assert snapshot["relationships"]["user:geoff"]["reliability"] == 69
    waiting_costs = [
        event for event in kernel.events(world_id)
        if event.event_type == "RelationshipChanged"
        and event.payload.get("thread_id") == "thread:reliable-promise"
    ]
    assert len(waiting_costs) == 1
    assert waiting_costs[0].payload["reason"] == "close_relationship_reliable_promise_unanswered"

    kernel.advance(
        world_id,
        NOW + timedelta(hours=17),
        expected_revision=kernel.revision(world_id),
    )
    assert kernel.snapshot(world_id)["relationships"]["user:geoff"]["reliability"] == 69
