import pytest

from companion_daemon.action_coordinator import (
    DeliveryStatus,
    SegmentTransitionError,
    SegmentedActionCoordinator,
    UserInterjectionKind,
)


def test_outgoing_action_plans_ordered_segments_and_claims_only_the_first() -> None:
    coordinator = SegmentedActionCoordinator()

    action = coordinator.plan_action(
        action_id="outgoing:42",
        texts=("我先说第一句。", "然后再补一句。", "最后这一句可以等。"),
    )

    assert action.status is DeliveryStatus.PLANNED
    assert [segment.status for segment in action.segments] == [
        DeliveryStatus.PLANNED,
        DeliveryStatus.PLANNED,
        DeliveryStatus.PLANNED,
    ]

    action, claimed = coordinator.claim_next(action)

    assert claimed.segment_id == "outgoing:42:segment:0"
    assert claimed.text == "我先说第一句。"
    assert action.status is DeliveryStatus.SENDING
    assert [segment.status for segment in action.segments] == [
        DeliveryStatus.SENDING,
        DeliveryStatus.PLANNED,
        DeliveryStatus.PLANNED,
    ]


def test_substantive_user_interjection_cancels_unsent_segments_after_first_delivery() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:43",
        texts=("我先说第一句。", "第二句还没发。", "第三句也先不发。"),
    )
    action, first = coordinator.claim_next(action)
    action = coordinator.confirm_delivered(
        action,
        segment_id=first.segment_id,
        external_receipt="qq:message-101",
    )

    action, cancelled = coordinator.observe_user_interjection(
        action,
        kind=UserInterjectionKind.SUBSTANTIVE,
        user_message_id="qq:user-202",
    )

    assert cancelled == (
        "outgoing:43:segment:1",
        "outgoing:43:segment:2",
    )
    assert action.status is DeliveryStatus.CANCELLED
    assert [segment.status for segment in action.segments] == [
        DeliveryStatus.DELIVERED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.CANCELLED,
    ]
    assert [entry.text for entry in coordinator.chat_history_entries(action)] == [
        "我先说第一句。"
    ]


def test_backchannel_after_first_delivery_keeps_the_next_segment_available() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:44",
        texts=("我先说第一句。", "这句稍后继续。"),
    )
    action, first = coordinator.claim_next(action)
    action = coordinator.confirm_delivered(action, segment_id=first.segment_id)

    action, cancelled = coordinator.observe_user_interjection(
        action,
        kind=UserInterjectionKind.BACKCHANNEL,
        user_message_id="qq:user-203",
    )
    action, second = coordinator.claim_next(action)

    assert cancelled == ()
    assert second.segment_id == "outgoing:44:segment:1"
    assert [segment.status for segment in action.segments] == [
        DeliveryStatus.DELIVERED,
        DeliveryStatus.SENDING,
    ]


def test_uncertain_segment_is_unknown_and_does_not_become_chat_history() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:45",
        texts=("这句可能发出去了。", "后一句不能冒险接着发。"),
    )
    action, first = coordinator.claim_next(action)

    action = coordinator.mark_unknown(
        action,
        segment_id=first.segment_id,
        reason="adapter disconnected before receipt",
    )

    assert action.status is DeliveryStatus.UNKNOWN
    assert action.segments[0].status is DeliveryStatus.UNKNOWN
    assert action.segments[0].terminal_reason == "adapter disconnected before receipt"
    assert coordinator.chat_history_entries(action) == ()


def test_unknown_segment_blocks_later_segments_until_receipt_reconciliation() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:46",
        texts=("可能已送达。", "不能盲目续发。"),
    )
    action, first = coordinator.claim_next(action)
    action = coordinator.mark_unknown(
        action,
        segment_id=first.segment_id,
        reason="receipt timeout",
    )

    with pytest.raises(SegmentTransitionError, match="unknown segment"):
        coordinator.claim_next(action)


def test_unknown_segment_enters_history_only_after_external_receipt_reconciliation() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:47",
        texts=("可能已送达。", "确认后才能继续。"),
    )
    action, first = coordinator.claim_next(action)
    action = coordinator.mark_unknown(
        action,
        segment_id=first.segment_id,
        reason="receipt timeout",
    )

    with pytest.raises(SegmentTransitionError, match="external receipt"):
        coordinator.confirm_delivered(action, segment_id=first.segment_id)

    action = coordinator.confirm_delivered(
        action,
        segment_id=first.segment_id,
        external_receipt="qq:message-303",
    )

    assert action.segments[0].terminal_reason is None
    assert [entry.text for entry in coordinator.chat_history_entries(action)] == ["可能已送达。"]
    action, second = coordinator.claim_next(action)
    assert second.segment_id == "outgoing:47:segment:1"


def test_only_one_segment_can_be_sending_at_a_time() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:48",
        texts=("第一句正在发送。", "第二句必须等。"),
    )
    action, _ = coordinator.claim_next(action)

    with pytest.raises(SegmentTransitionError, match="already sending"):
        coordinator.claim_next(action)


def test_planned_segment_cannot_be_confirmed_without_a_send_claim() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(action_id="outgoing:49", texts=("还没有发送。",))

    with pytest.raises(SegmentTransitionError, match="planned.*delivered"):
        coordinator.confirm_delivered(
            action,
            segment_id="outgoing:49:segment:0",
            external_receipt="qq:impossible",
        )


def test_action_requires_an_id_and_at_least_one_nonblank_segment() -> None:
    coordinator = SegmentedActionCoordinator()

    with pytest.raises(ValueError, match="action_id"):
        coordinator.plan_action(action_id="", texts=("一句话。",))
    with pytest.raises(ValueError, match="at least one"):
        coordinator.plan_action(action_id="outgoing:50", texts=())
    with pytest.raises(ValueError, match="blank"):
        coordinator.plan_action(action_id="outgoing:50", texts=("一句话。", "  "))


def test_only_a_claimed_segment_can_become_unknown() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(action_id="outgoing:51", texts=("还没开始发。",))

    with pytest.raises(SegmentTransitionError, match="planned.*unknown"):
        coordinator.mark_unknown(
            action,
            segment_id="outgoing:51:segment:0",
            reason="there was no send attempt",
        )


def test_action_is_delivered_only_when_every_segment_is_confirmed() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(action_id="outgoing:52", texts=("只有一句。",))
    action, segment = coordinator.claim_next(action)
    action = coordinator.confirm_delivered(action, segment_id=segment.segment_id)

    assert action.status is DeliveryStatus.DELIVERED
    with pytest.raises(SegmentTransitionError, match="no planned segment"):
        coordinator.claim_next(action)


def test_action_projection_round_trip_preserves_delivery_evidence() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:53",
        texts=("已确认送达。", "仍在计划。"),
    )
    action, first = coordinator.claim_next(action)
    action = coordinator.confirm_delivered(
        action,
        segment_id=first.segment_id,
        external_receipt="qq:message-404",
    )

    projection = coordinator.to_projection(action)

    assert projection == {
        "action_id": "outgoing:53",
        "status": "planned",
        "segments": [
            {
                "segment_id": "outgoing:53:segment:0",
                "position": 0,
                "text": "已确认送达。",
                "status": "delivered",
                "external_receipt": "qq:message-404",
                "terminal_reason": None,
            },
            {
                "segment_id": "outgoing:53:segment:1",
                "position": 1,
                "text": "仍在计划。",
                "status": "planned",
                "external_receipt": None,
                "terminal_reason": None,
            },
        ],
    }
    assert coordinator.from_projection(projection) == action


def test_planned_segments_expose_a_stable_world_event_payload() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:54",
        texts=("先发这一句。", "再发这一句。"),
    )

    assert coordinator.planned_world_event(action) == (
        "ActionSegmentsPlanned",
        {
            "action_id": "outgoing:54",
            "segments": [
                {
                    "segment_id": "outgoing:54:segment:0",
                    "position": 0,
                    "text": "先发这一句。",
                    "status": "planned",
                    "external_receipt": None,
                    "terminal_reason": None,
                },
                {
                    "segment_id": "outgoing:54:segment:1",
                    "position": 1,
                    "text": "再发这一句。",
                    "status": "planned",
                    "external_receipt": None,
                    "terminal_reason": None,
                },
            ],
        },
    )


def test_claimed_segment_exposes_a_world_dispatch_event_payload() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(action_id="outgoing:55", texts=("准备发送。",))
    action, segment = coordinator.claim_next(action)

    assert coordinator.claimed_world_event(action, segment) == (
        "ActionSegmentDispatchClaimed",
        {
            "action_id": "outgoing:55",
            "segment_id": "outgoing:55:segment:0",
            "position": 0,
        },
    )


def test_delivered_segment_exposes_a_world_settlement_event_payload() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(action_id="outgoing:56", texts=("已经送达。",))
    action, segment = coordinator.claim_next(action)
    action = coordinator.confirm_delivered(
        action,
        segment_id=segment.segment_id,
        external_receipt="qq:message-505",
    )

    assert coordinator.settled_world_event(action, segment_id=segment.segment_id) == (
        "ActionSegmentSettled",
        {
            "action_id": "outgoing:56",
            "segment_id": "outgoing:56:segment:0",
            "position": 0,
            "result": {
                "kind": "delivery",
                "status": "delivered",
                "external_receipt": "qq:message-505",
            },
        },
    )


def test_unknown_segment_exposes_a_world_uncertainty_event_payload() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(action_id="outgoing:57", texts=("回执中断。",))
    action, segment = coordinator.claim_next(action)
    action = coordinator.mark_unknown(
        action,
        segment_id=segment.segment_id,
        reason="adapter disconnected",
    )

    assert coordinator.unknown_world_event(action, segment_id=segment.segment_id) == (
        "ActionSegmentDeliveryUncertain",
        {
            "action_id": "outgoing:57",
            "segment_id": "outgoing:57:segment:0",
            "position": 0,
            "reason": "adapter disconnected",
        },
    )


def test_cancelled_segments_expose_the_interrupting_user_turn_in_world_payload() -> None:
    coordinator = SegmentedActionCoordinator()
    action = coordinator.plan_action(
        action_id="outgoing:58",
        texts=("已送达。", "被用户新话题取消。", "这一句也取消。"),
    )
    action, first = coordinator.claim_next(action)
    action = coordinator.confirm_delivered(action, segment_id=first.segment_id)
    action, cancelled = coordinator.observe_user_interjection(
        action,
        kind=UserInterjectionKind.SUBSTANTIVE,
        user_message_id="qq:user-606",
    )

    assert coordinator.cancelled_world_event(
        action,
        segment_ids=cancelled,
        user_message_id="qq:user-606",
    ) == (
        "ActionSegmentsCancelled",
        {
            "action_id": "outgoing:58",
            "segment_ids": [
                "outgoing:58:segment:1",
                "outgoing:58:segment:2",
            ],
            "reason": "substantive_user_interjection",
            "user_message_id": "qq:user-606",
        },
    )
