import random

from companion_daemon.models import MoodState
from companion_daemon.reply_decision import ReplyAction, classify_message, decide_reply


def test_classifies_core_reply_timing_messages() -> None:
    assert classify_message("在吗") == "urgent"
    assert classify_message("你今天怎么样？") == "question"
    assert classify_message("我今天好累") == "emotional"
    assert classify_message("嗯嗯") == "ack"
    assert classify_message("我刚刚想了很久，" * 8) == "story"


def test_ack_can_be_skipped_without_marking_unread() -> None:
    decision = decide_reply("嗯嗯", rng=random.Random(1))

    assert decision.action == ReplyAction.SKIP
    assert decision.mark_unread is False


def test_ack_can_be_deferred_when_it_looks_low_energy_in_context() -> None:
    state = MoodState(mood="hurt", unresolved_emotion="用户刚才有点敷衍")

    decision = decide_reply("嗯", state=state, rng=random.Random(4))

    assert decision.action == ReplyAction.DEFER
    assert decision.mark_unread is True
    assert decision.reason == "low_energy_ack_needs_space"
    assert decision.defer_minutes is not None


def test_ack_can_leave_open_thread_without_marking_unread() -> None:
    state = MoodState(
        mood="curious",
        relationship_stage="close_friend",
        initiative=50,
    )

    decision = decide_reply("嗯", state=state, rng=random.Random(4))

    assert decision.action == ReplyAction.DEFER
    assert decision.mark_unread is False
    assert decision.reason == "ack_leaves_open_thread"
    assert decision.defer_minutes is not None


def test_unread_or_pending_message_makes_ack_replyable() -> None:
    assert decide_reply("嗯嗯", has_unread=True).action == ReplyAction.REPLY_NOW
    assert decide_reply("嗯嗯", has_pending_reply=True).action == ReplyAction.REPLY_NOW
    assert decide_reply("嗯嗯", state=MoodState(has_unread=True)).action == ReplyAction.REPLY_NOW


def test_questions_and_urgent_interrupts_reply_immediately() -> None:
    assert decide_reply("你还在吗", phase="morning_focus").action == ReplyAction.REPLY_NOW
    assert decide_reply("这个怎么弄？", phase="morning_focus").action == ReplyAction.REPLY_NOW


def test_busy_long_story_can_be_deferred_and_marked_unread() -> None:
    decision = decide_reply(
        "我刚刚想了很久，" * 8,
        phase="morning_focus",
        rng=random.Random(4),
    )

    assert decision.action == ReplyAction.DEFER
    assert decision.defer_minutes is not None
    assert decision.mark_unread is True


def test_plain_statement_is_not_randomly_skipped() -> None:
    decision = decide_reply("我到家了", phase="morning_focus", rng=random.Random(4))

    assert decision.action == ReplyAction.REPLY_NOW
