from dataclasses import dataclass
from enum import StrEnum


class TurnState(StrEnum):
    IDLE = "idle"
    COLLECTING = "collecting"
    READY = "ready"


class ReplyTiming(StrEnum):
    IMMEDIATE = "immediate"
    SHORT_WAIT = "short_wait"
    LONG_WAIT = "long_wait"


@dataclass(frozen=True)
class TurnDecision:
    state: TurnState
    timing: ReplyTiming
    wait_seconds: float
    reason: str


@dataclass(frozen=True)
class TurnInput:
    pending_count: int
    latest_text: str
    merged_text: str


class TurnTakingPolicy:
    """Decides whether a burst of IM messages looks complete enough to answer."""

    def __init__(
        self,
        *,
        immediate_seconds: float = 0.4,
        short_wait_seconds: float = 2.5,
        long_wait_seconds: float = 5.5,
    ):
        self.immediate_seconds = immediate_seconds
        self.short_wait_seconds = short_wait_seconds
        self.long_wait_seconds = long_wait_seconds

    def decide(self, turn: TurnInput) -> TurnDecision:
        latest = turn.latest_text.strip()
        merged = turn.merged_text.strip()

        if not latest:
            return TurnDecision(TurnState.COLLECTING, ReplyTiming.SHORT_WAIT, 1.0, "empty")

        if _looks_like_interruption(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.IMMEDIATE,
                self.immediate_seconds,
                "explicit_stop_or_urgent",
            )

        if turn.pending_count == 1 and _looks_like_complete_short_turn(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.SHORT_WAIT,
                self.short_wait_seconds,
                "single_complete_turn",
            )

        if _looks_like_continuation(latest):
            return TurnDecision(
                TurnState.COLLECTING,
                ReplyTiming.LONG_WAIT,
                self.long_wait_seconds,
                "latest_message_continues",
            )

        if turn.pending_count >= 3 and not _ends_in_open_continuation(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.SHORT_WAIT,
                self.short_wait_seconds,
                "several_messages_probably_complete",
            )

        if len(merged) >= 120 or _ends_like_question_or_completion(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.SHORT_WAIT,
                self.short_wait_seconds,
                "complete_enough",
            )

        return TurnDecision(
            TurnState.COLLECTING,
            ReplyTiming.LONG_WAIT,
            self.long_wait_seconds,
            "probably_still_typing",
        )


def _looks_like_complete_short_turn(text: str) -> bool:
    if len(text) <= 6 and not _ends_like_question_or_completion(text):
        return False
    return _ends_like_question_or_completion(text) or len(text) >= 18


def _looks_like_continuation(text: str) -> bool:
    lowered = text.lower()
    continuation_starts = (
        "还有",
        "然后",
        "而且",
        "另外",
        "就是",
        "其实",
        "因为",
        "但是",
        "不过",
        "以及",
        "比如",
        "或者",
        "and ",
        "also",
        "but ",
    )
    if lowered.startswith(continuation_starts):
        return True
    return _ends_in_open_continuation(text)


def _ends_in_open_continuation(text: str) -> bool:
    stripped = text.strip()
    return stripped.endswith(("，", ",", "、", "：", ":", "…", "...", "然后", "因为", "就是"))


def _ends_like_question_or_completion(text: str) -> bool:
    return text.strip().endswith(("。", "！", "？", ".", "!", "?", "～", "~"))


def _looks_like_interruption(text: str) -> bool:
    stripped = text.strip()
    return stripped in {"先回这个", "别等了", "就这些", "说完了", "你先说", "回我"} or stripped.endswith(
        ("急", "急急急")
    )
