from companion_daemon.turn_taking import ReplyTiming, TurnInput, TurnState, TurnTakingPolicy
from companion_daemon.conversation_cadence import ConversationCadence


def _hot_cadence() -> ConversationCadence:
    return ConversationCadence(
        heat="hot",
        observed_gap_seconds=12.0,
        alternating_turns=4,
        reason="active_back_and_forth",
    )


def test_hot_terse_but_complete_turn_is_not_mistaken_for_unfinished_typing() -> None:
    decision = TurnTakingPolicy().decide(
        TurnInput(pending_count=1, latest_text="还行吧", merged_text="还行吧"),
        cadence=_hot_cadence(),
    )

    assert decision.state == TurnState.READY
    assert 0.4 <= decision.wait_seconds <= 0.8
    assert decision.reason == "hot_terse_complete_turn"


def test_hot_open_continuation_still_leaves_room_for_the_next_bubble() -> None:
    decision = TurnTakingPolicy().decide(
        TurnInput(pending_count=1, latest_text="但是我觉得，", merged_text="但是我觉得，"),
        cadence=_hot_cadence(),
    )

    assert decision.state == TurnState.COLLECTING
    assert decision.wait_seconds >= 1.5
    assert decision.reason == "latest_message_continues"


def test_waits_longer_for_continuation_fragment() -> None:
    policy = TurnTakingPolicy(short_wait_seconds=2.0, long_wait_seconds=5.0)

    decision = policy.decide(
        TurnInput(pending_count=2, latest_text="还有一个问题，", merged_text="我今天遇到个事\n还有一个问题，")
    )

    assert decision.state == TurnState.COLLECTING
    assert decision.timing == ReplyTiming.LONG_WAIT
    assert decision.wait_seconds == 5.0


def test_short_wait_for_complete_question() -> None:
    policy = TurnTakingPolicy(short_wait_seconds=2.0, long_wait_seconds=5.0)

    decision = policy.decide(
        TurnInput(pending_count=1, latest_text="你叫什么名字？", merged_text="你叫什么名字？")
    )

    assert decision.state == TurnState.READY
    assert decision.timing == ReplyTiming.SHORT_WAIT
    assert decision.wait_seconds == 2.0


def test_cold_longform_opener_wait_is_bounded_instead_of_five_minutes() -> None:
    policy = TurnTakingPolicy(short_wait_seconds=2.0, long_wait_seconds=5.0, longform_start_seconds=300.0)

    decision = policy.decide(
        TurnInput(
            pending_count=1,
            latest_text="我今天真的有点离谱",
            merged_text="我今天真的有点离谱",
        )
    )

    assert decision.state == TurnState.COLLECTING
    assert decision.timing == ReplyTiming.LONG_WAIT
    assert decision.wait_seconds == 20.0
    assert decision.reason == "longform_opener_waiting_for_user"


def test_waits_when_user_is_thinking_or_hesitating() -> None:
    policy = TurnTakingPolicy(short_wait_seconds=2.0, long_wait_seconds=8.0)

    decision = policy.decide(
        TurnInput(
            pending_count=1,
            latest_text="我想想，等我组织一下语言",
            merged_text="我想想，等我组织一下语言",
        )
    )

    assert decision.state == TurnState.COLLECTING
    assert decision.timing == ReplyTiming.LONG_WAIT
    assert decision.wait_seconds == 8.0
    assert decision.reason == "user_thinking_or_hesitating"


def test_waits_on_affective_pause_instead_of_pressing() -> None:
    policy = TurnTakingPolicy(short_wait_seconds=2.0, long_wait_seconds=8.0)

    decision = policy.decide(
        TurnInput(
            pending_count=1,
            latest_text="啊这",
            merged_text="啊这",
        )
    )

    assert decision.state == TurnState.COLLECTING
    assert decision.timing == ReplyTiming.LONG_WAIT
    assert decision.reason == "affective_pause_waiting_for_next_turn"


def test_several_messages_become_ready_when_not_open_ended() -> None:
    policy = TurnTakingPolicy(short_wait_seconds=2.0, long_wait_seconds=5.0)

    decision = policy.decide(
        TurnInput(
            pending_count=3,
            latest_text="大概就是这样",
            merged_text="我今天有点累\n课也很多\n大概就是这样",
        )
    )

    assert decision.state == TurnState.READY
    assert decision.timing == ReplyTiming.SHORT_WAIT


def test_explicit_stop_replies_quickly() -> None:
    policy = TurnTakingPolicy(immediate_seconds=0.2)

    decision = policy.decide(
        TurnInput(pending_count=2, latest_text="你先说", merged_text="我有点乱\n你先说")
    )

    assert decision.timing == ReplyTiming.IMMEDIATE
    assert decision.wait_seconds == 0.2


def test_batch_limit_forces_a_replayable_immediate_flush() -> None:
    decision = TurnTakingPolicy(long_wait_seconds=30).decide(
        TurnInput(
            pending_count=6,
            latest_text="还有第六句，",
            merged_text="\n".join(f"第{index}句" for index in range(1, 7)),
        )
    )

    assert decision.state == TurnState.READY
    assert decision.timing == ReplyTiming.IMMEDIATE
    assert decision.wait_seconds == 0
    assert decision.reason == "batch_limit_reached"
