from dataclasses import dataclass
from enum import StrEnum

from companion_daemon.conversation_cadence import ConversationCadence


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
        long_burst_seconds: float = 15.0,
        longform_start_seconds: float = 300.0,
    ):
        self.immediate_seconds = immediate_seconds
        self.short_wait_seconds = short_wait_seconds
        self.long_wait_seconds = long_wait_seconds
        self.long_burst_seconds = long_burst_seconds
        self.longform_start_seconds = longform_start_seconds

    def decide(
        self,
        turn: TurnInput,
        *,
        cadence: ConversationCadence | None = None,
    ) -> TurnDecision:
        latest = turn.latest_text.strip()
        merged = turn.merged_text.strip()
        heat = str(getattr(cadence, "heat", "cold"))
        short_wait = (
            min(self.short_wait_seconds, 0.6)
            if heat == "hot"
            else min(self.short_wait_seconds, 1.4)
            if heat == "warm"
            else self.short_wait_seconds
        )
        conversational_wait = (
            min(self.long_wait_seconds, 2.0)
            if heat == "hot"
            else min(self.long_wait_seconds, 3.5)
            if heat == "warm"
            else self.long_wait_seconds
        )

        if not latest:
            return TurnDecision(TurnState.COLLECTING, ReplyTiming.SHORT_WAIT, 1.0, "empty")

        if _looks_like_interruption(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.IMMEDIATE,
                self.immediate_seconds,
                "explicit_stop_or_urgent",
            )

        if turn.pending_count >= 6:
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.IMMEDIATE,
                0.0,
                "batch_limit_reached",
            )

        if _looks_like_user_thinking_or_hesitating(latest):
            return TurnDecision(
                TurnState.COLLECTING,
                ReplyTiming.LONG_WAIT,
                self.long_wait_seconds,
                "user_thinking_or_hesitating",
            )

        if _looks_like_affective_pause(latest):
            return TurnDecision(
                TurnState.COLLECTING,
                ReplyTiming.LONG_WAIT,
                self.long_wait_seconds,
                "affective_pause_waiting_for_next_turn",
            )

        if turn.pending_count == 1 and _looks_like_longform_opener(latest):
            return TurnDecision(
                TurnState.COLLECTING,
                ReplyTiming.LONG_WAIT,
                min(
                    self.longform_start_seconds,
                    2.0 if heat == "hot" else 8.0 if heat == "warm" else 20.0,
                ),
                "longform_opener_waiting_for_user",
            )

        if heat == "hot" and turn.pending_count == 1 and _looks_like_terse_complete_turn(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.SHORT_WAIT,
                max(0.4, min(0.8, short_wait)),
                "hot_terse_complete_turn",
            )

        if turn.pending_count == 1 and _looks_like_complete_short_turn(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.SHORT_WAIT,
                short_wait,
                "single_complete_turn",
            )

        if _looks_like_continuation(latest):
            wait = self.long_burst_seconds if turn.pending_count >= 3 else conversational_wait
            return TurnDecision(
                TurnState.COLLECTING,
                ReplyTiming.LONG_WAIT,
                wait,
                "latest_message_continues",
            )

        if turn.pending_count >= 5:
            return TurnDecision(
                TurnState.COLLECTING,
                ReplyTiming.LONG_WAIT,
                self.long_burst_seconds,
                "long_burst_still_going",
            )

        if turn.pending_count >= 3 and not _ends_in_open_continuation(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.SHORT_WAIT,
                short_wait,
                "several_messages_probably_complete",
            )

        if len(merged) >= 120 or _ends_like_question_or_completion(latest):
            return TurnDecision(
                TurnState.READY,
                ReplyTiming.SHORT_WAIT,
                short_wait,
                "complete_enough",
            )

        return TurnDecision(
            TurnState.COLLECTING,
            ReplyTiming.LONG_WAIT,
            conversational_wait,
            "probably_still_typing",
        )


def _looks_like_complete_short_turn(text: str) -> bool:
    if len(text) <= 6 and not _ends_like_question_or_completion(text):
        return False
    return _ends_like_question_or_completion(text) or len(text) >= 18


def _looks_like_terse_complete_turn(text: str) -> bool:
    """Recognise brief floor-yielding replies common in an active IM exchange."""
    stripped = text.strip()
    if not stripped or len(stripped) > 14 or _looks_like_continuation(stripped):
        return False
    if stripped in {"嗯", "嗯嗯", "好", "好的", "行", "还行", "知道了", "明白了"}:
        return True
    if any(token in stripped for token in ("没懂", "不明白", "什么意思")):
        return True
    return stripped.endswith(("吧", "呢", "呀", "啊", "哦", "啦", "了"))


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


def _looks_like_longform_opener(text: str) -> bool:
    stripped = text.strip()
    if _ends_like_question_or_completion(stripped) and len(stripped) >= 32:
        return False
    opener_tokens = (
        "我跟你说",
        "我和你说",
        "跟你说个事",
        "跟你讲个事",
        "我想跟你说",
        "我想跟你讲",
        "有个事",
        "有件事",
        "说来话长",
        "我今天真的有点离谱",
        "今天真的有点离谱",
        "我今天遇到个事",
        "我刚刚遇到个事",
        "我有点不知道怎么说",
        "其实吧",
        "怎么说呢",
    )
    if any(token in stripped for token in opener_tokens):
        return True
    return stripped.endswith(("我跟你说", "我想说", "说来话长", "有点离谱"))


def _looks_like_user_thinking_or_hesitating(text: str) -> bool:
    stripped = text.strip()
    thinking_tokens = (
        "我想想",
        "让我想想",
        "等下",
        "等一下",
        "等等",
        "我组织一下语言",
        "让我组织一下语言",
        "我不知道怎么说",
        "不知道怎么说",
        "不知道该不该说",
        "我有点不知道怎么说",
        "先别回",
        "你等我一下",
    )
    return any(token in stripped for token in thinking_tokens)


def _looks_like_affective_pause(text: str) -> bool:
    stripped = text.strip()
    pause_tokens = (
        "额",
        "呃",
        "啊这",
        "……",
        "...",
        "有点无语",
        "无语了",
        "我真服了",
        "算了",
        "没事",
        "不说了",
        "懒得说了",
    )
    return stripped in pause_tokens or any(stripped.endswith(token) for token in pause_tokens)


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
