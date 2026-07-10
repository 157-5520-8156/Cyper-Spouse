import random

from companion_daemon.models import MoodState
from companion_daemon.reply_decision import ReplyAction, classify_message, decide_reply


class AlwaysGhostRandom(random.Random):
    def random(self) -> float:
        return 0.0

    def uniform(self, a: float, b: float) -> float:
        return a


def test_classifies_core_reply_timing_messages() -> None:
    assert classify_message("在吗") == "urgent"
    assert classify_message("你今天怎么样？") == "question"
    assert classify_message("我今天好累") == "emotional"
    assert classify_message("嗯嗯") == "minimal_response"
    assert classify_message("晚安") == "farewell"
    assert classify_message("我想想") == "thinking"
    assert classify_message("算了") == "withdrawal"
    assert classify_message("啊这") == "reaction_pause"
    assert classify_message("我刚刚想了很久，" * 8) == "story"


def test_upset_state_can_leave_a_message_on_read() -> None:
    upset = MoodState(mood="hurt", emotion_vector={"anger": 75, "sadness": 55, "joy": 5, "trust": 10})

    decision = decide_reply("我今天去买了那个键盘", state=upset, rng=AlwaysGhostRandom())

    assert decision.action == ReplyAction.DEFER
    assert decision.reason == "emotional_ghost"
    # She read it and is deliberately silent, so it must not look unread.
    assert decision.mark_unread is False
    assert decision.defer_minutes and decision.defer_minutes >= 10


def test_urgent_message_breaks_through_the_ghost_window() -> None:
    upset = MoodState(mood="hurt", emotion_vector={"anger": 75, "sadness": 55, "joy": 5, "trust": 10})

    decision = decide_reply("在吗？怎么不回我", state=upset, rng=random.Random(1))

    assert decision.action == ReplyAction.REPLY_NOW
    assert decision.reason == "urgent_interrupt"


def test_calm_state_never_ghosts() -> None:
    from companion_daemon.reply_timing import emotional_ghost_minutes

    assert emotional_ghost_minutes(MoodState(), rng=random.Random(1)) is None


def test_ack_can_be_skipped_without_marking_unread() -> None:
    decision = decide_reply("嗯嗯", rng=random.Random(1))

    assert decision.action == ReplyAction.SKIP
    assert decision.mark_unread is False


def test_ack_can_be_deferred_when_it_looks_low_energy_in_context() -> None:
    state = MoodState(mood="hurt", unresolved_emotion="用户刚才有点敷衍")

    decision = decide_reply("嗯", state=state, rng=random.Random(4))

    assert decision.action == ReplyAction.DEFER
    assert decision.mark_unread is True
    assert decision.reason == "minimal_response_low_energy_needs_space"
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
    assert decision.reason == "minimal_response_leaves_open_thread"
    assert decision.defer_minutes is not None


def test_minimal_response_after_open_context_defers_without_marking_unread() -> None:
    decision = decide_reply("嗯", recent_context_open=True, rng=random.Random(1))

    assert decision.action == ReplyAction.DEFER
    assert decision.mark_unread is False
    assert decision.reason == "minimal_response_context_open"


def test_farewell_can_have_afterglow_without_marking_unread() -> None:
    state = MoodState(
        mood="miss_you",
        relationship_stage="lover",
    )

    decision = decide_reply("晚安", state=state, rng=random.Random(4))

    assert decision.action == ReplyAction.DEFER
    assert decision.mark_unread is False
    assert decision.reason == "farewell_afterglow"


def test_withdrawal_and_thinking_tokens_defer_without_immediate_pressure() -> None:
    withdrawal = decide_reply("算了", rng=random.Random(1))
    thinking = decide_reply("我想想", rng=random.Random(1))
    pause = decide_reply("啊这", rng=random.Random(1))

    assert withdrawal.action == ReplyAction.DEFER
    assert withdrawal.mark_unread is True
    assert withdrawal.reason == "withdrawal_needs_space"
    assert thinking.action == ReplyAction.DEFER
    assert thinking.reason == "thinking_wait_for_user"
    assert pause.action == ReplyAction.DEFER
    assert pause.reason == "reaction_pause_wait_for_user"


def test_unread_or_pending_message_makes_ack_replyable() -> None:
    assert decide_reply("嗯嗯", has_unread=True).action == ReplyAction.REPLY_NOW
    assert decide_reply("晚安", has_unread=True).action == ReplyAction.REPLY_NOW
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
