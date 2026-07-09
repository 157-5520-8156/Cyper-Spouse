from companion_daemon.turn_taking import ReplyTiming, TurnInput, TurnState, TurnTakingPolicy


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
