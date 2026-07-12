from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.outbound_policy import (
    OutboundKind,
    OutboundPolicy,
    OutboundProjection,
    OutboundRequest,
    RecentOutbound,
    evaluate_outbound,
)


NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)


def test_global_cooldown_blocks_a_second_proactive_kind_with_explanation() -> None:
    result = evaluate_outbound(
        OutboundRequest(
            request_id="pulse-2",
            kind=OutboundKind.PULSE,
            trigger="open_thread",
            text="刚才的话，我后来又想到一点。",
            now=NOW,
        ),
        OutboundProjection(last_outbound_at=NOW - timedelta(minutes=2)),
        OutboundPolicy(global_cooldown=timedelta(minutes=5)),
    )

    assert result.allowed is False
    assert result.reasons == ("global_cooldown",)
    assert result.retry_at == NOW + timedelta(minutes=3)
    assert result.check("global_cooldown").detail == "3m remaining"


def test_user_reply_uses_shared_policy_without_inheriting_proactive_cooldown() -> None:
    result = evaluate_outbound(
        OutboundRequest(
            request_id="reply-1",
            kind=OutboundKind.REPLY,
            trigger="user_message",
            text="我在。",
            now=NOW,
        ),
        OutboundProjection(last_outbound_at=NOW - timedelta(seconds=1)),
        OutboundPolicy(global_cooldown=timedelta(minutes=5)),
    )

    assert result.allowed is True
    assert result.check("global_cooldown").detail == "kind exempt"


def test_trigger_cooldown_is_independent_from_global_cooldown() -> None:
    result = evaluate_outbound(
        OutboundRequest(
            request_id="followup-2",
            kind=OutboundKind.FOLLOWUP,
            trigger="comfort_followup",
            text="今天有没有好一点？",
            now=NOW,
        ),
        OutboundProjection(
            last_outbound_at=NOW - timedelta(hours=2),
            trigger_last_outbound_at={"comfort_followup": NOW - timedelta(minutes=20)},
        ),
        OutboundPolicy(
            global_cooldown=timedelta(minutes=5),
            trigger_cooldowns={"comfort_followup": timedelta(hours=1)},
        ),
    )

    assert result.reasons == ("trigger_cooldown",)
    assert result.retry_at == NOW + timedelta(minutes=40)


def test_unanswered_budget_blocks_proactive_actions_but_not_a_reply() -> None:
    policy = OutboundPolicy(max_unanswered=2)
    projection = OutboundProjection(unanswered_outbound_count=2)
    proactive = OutboundRequest(
        request_id="share-3",
        kind=OutboundKind.LIFE_SHARE,
        trigger="life_event",
        text="今天画完了一张小稿。",
        now=NOW,
    )
    reply = OutboundRequest(
        request_id="reply-2",
        kind=OutboundKind.REPLY,
        trigger="user_message",
        text="欢迎回来。",
        now=NOW,
    )

    assert evaluate_outbound(proactive, projection, policy).reasons == ("unanswered_budget",)
    assert evaluate_outbound(reply, projection, policy).allowed is True


def test_generation_lock_reports_owner_and_expiry_and_all_failures() -> None:
    result = evaluate_outbound(
        OutboundRequest(
            request_id="media-2",
            kind=OutboundKind.MEDIA,
            trigger="selfie",
            text="给你看刚拍的照片。",
            now=NOW,
        ),
        OutboundProjection(
            last_outbound_at=NOW - timedelta(minutes=1),
            unanswered_outbound_count=2,
            generation_lock_owner="pulse-1",
            generation_lock_expires_at=NOW + timedelta(minutes=2),
        ),
        OutboundPolicy(global_cooldown=timedelta(minutes=5), max_unanswered=2),
    )

    assert result.reasons == (
        "global_cooldown",
        "unanswered_budget",
        "generation_lock",
    )
    assert result.retry_at == NOW + timedelta(minutes=4)
    assert result.check("generation_lock").detail == "held by pulse-1"


def test_generation_lock_is_reentrant_for_the_same_request_and_expired_locks_are_ignored() -> None:
    request = OutboundRequest(
        request_id="tool-1",
        kind=OutboundKind.TOOL,
        trigger="confirmed_tool",
        text="正在创建提醒。",
        now=NOW,
    )

    same_owner = OutboundProjection(
        generation_lock_owner="tool-1", generation_lock_expires_at=NOW + timedelta(minutes=2)
    )
    expired = OutboundProjection(
        generation_lock_owner="other", generation_lock_expires_at=NOW - timedelta(seconds=1)
    )

    assert evaluate_outbound(request, same_owner).allowed is True
    assert evaluate_outbound(request, expired).allowed is True


def test_exact_request_or_normalized_text_duplicate_is_blocked() -> None:
    recent = RecentOutbound(
        request_id="reaction-1",
        kind=OutboundKind.REACTION,
        trigger="user_message",
        text=" 好耶！ ",
        topic_key=None,
        occurred_at=NOW - timedelta(minutes=10),
    )
    projection = OutboundProjection(recent_outbounds=(recent,))

    same_id = OutboundRequest(
        request_id="reaction-1",
        kind=OutboundKind.REACTION,
        trigger="user_message",
        text=None,
        now=NOW,
    )
    same_text = OutboundRequest(
        request_id="reaction-2",
        kind=OutboundKind.REACTION,
        trigger="user_message",
        text="好耶!",
        now=NOW,
    )

    assert evaluate_outbound(same_id, projection).reasons == ("duplicate",)
    text_result = evaluate_outbound(same_text, projection)
    assert text_result.reasons == ("duplicate",)
    assert text_result.check("duplicate").detail == "matches reaction-1 text"


def test_same_topic_is_blocked_inside_similarity_window_but_expires() -> None:
    recent = RecentOutbound(
        request_id="share-old",
        kind=OutboundKind.LIFE_SHARE,
        trigger="life_event",
        text="画稿终于收尾了。",
        topic_key="drawing:final",
        occurred_at=NOW - timedelta(hours=2),
    )
    request = OutboundRequest(
        request_id="pulse-new",
        kind=OutboundKind.PULSE,
        trigger="afterthought",
        text="对了，画稿最后那点也改好了。",
        topic_key="drawing:final",
        now=NOW,
    )

    blocked = evaluate_outbound(
        request,
        OutboundProjection(recent_outbounds=(recent,)),
        OutboundPolicy(similarity_window=timedelta(hours=4)),
    )
    expired = evaluate_outbound(
        request,
        OutboundProjection(recent_outbounds=(recent,)),
        OutboundPolicy(similarity_window=timedelta(hours=1)),
    )

    assert blocked.reasons == ("topic_similarity",)
    assert blocked.check("topic_similarity").detail == "same topic as share-old"
    assert expired.allowed is True


def test_near_duplicate_text_is_blocked_without_a_topic_key() -> None:
    recent = RecentOutbound(
        request_id="followup-old",
        kind=OutboundKind.FOLLOWUP,
        trigger="comfort_followup",
        text="你今天感觉好一点了吗",
        topic_key=None,
        occurred_at=NOW - timedelta(hours=1),
    )
    request = OutboundRequest(
        request_id="followup-new",
        kind=OutboundKind.FOLLOWUP,
        trigger="comfort_followup",
        text="你今天感觉好一点了吗？",
        now=NOW,
    )

    result = evaluate_outbound(request, OutboundProjection(recent_outbounds=(recent,)))

    assert result.reasons == ("duplicate",)


def test_new_user_turn_reply_is_not_suppressed_only_for_reusing_short_wording() -> None:
    recent = RecentOutbound(
        request_id="reply:old",
        kind=OutboundKind.REPLY,
        trigger="ordinary_message",
        text="我记住了。",
        topic_key=None,
        occurred_at=NOW - timedelta(minutes=1),
    )
    request = OutboundRequest(
        request_id="reply:new-user-message",
        kind=OutboundKind.REPLY,
        trigger="ordinary_message",
        text="我记住了。",
        now=NOW,
    )

    result = evaluate_outbound(request, OutboundProjection(recent_outbounds=(recent,)))

    assert result.allowed is True
    assert result.check("duplicate").detail == "kind exempt"


@pytest.mark.parametrize("kind", list(OutboundKind))
def test_every_outbound_kind_passes_through_all_shared_guards(kind: OutboundKind) -> None:
    result = evaluate_outbound(
        OutboundRequest(
            request_id=f"{kind}-1",
            kind=kind,
            trigger=f"{kind}_trigger",
            text=f"unique {kind} content",
            now=NOW,
        ),
        OutboundProjection(),
    )

    assert [check.name for check in result.checks] == [
        "global_cooldown",
        "trigger_cooldown",
        "unanswered_budget",
        "generation_lock",
        "duplicate",
        "topic_similarity",
    ]
