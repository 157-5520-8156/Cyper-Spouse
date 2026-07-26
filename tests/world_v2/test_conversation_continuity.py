from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

from companion_daemon.world_v2.conversation_continuity import (
    ContinuityRetrievalCandidate,
    ConversationContinuityCompiler,
)
from companion_daemon.world_v2.recent_dialogue import DialogueSourceClaim, RecentDialogueItem


NOW = datetime(2026, 7, 25, 17, 8, tzinfo=UTC)


def _dialogue(
    suffix: str,
    text: str,
    *,
    speaker: str,
    at: datetime,
    sequence: int,
    acknowledges: tuple[str, ...] = (),
) -> RecentDialogueItem:
    ref = f"event:dialogue:{suffix}"
    return RecentDialogueItem(
        dialogue_id=f"dialogue:{suffix}",
        speaker=speaker,
        text=text,
        occurred_at=at,
        delivery_state="observed" if speaker == "counterpart" else "delivered",
        sequence=sequence,
        source_claims=(
            DialogueSourceClaim(
                authority_event_ref=ref,
                authority_world_revision=sequence,
                authority_payload_hash=hashlib.sha256(ref.encode()).hexdigest(),
            ),
        ),
        acknowledges_observation_event_refs=acknowledges,
    )


def test_compile_separates_pending_interaction_from_replied_history_and_memory() -> None:
    first = _dialogue(
        "first",
        "从深圳回来啦",
        speaker="counterpart",
        at=NOW - timedelta(minutes=4),
        sequence=1,
    )
    reply = _dialogue(
        "reply",
        "回来啦！深圳怎么样？",
        speaker="companion",
        at=NOW - timedelta(minutes=3),
        sequence=2,
        acknowledges=("event:dialogue:first",),
    )
    pending = _dialogue(
        "pending",
        "深圳说实话不是很好玩哈哈哈哈",
        speaker="counterpart",
        at=NOW - timedelta(minutes=1),
        sequence=3,
    )
    current = _dialogue(
        "current",
        "👀",
        speaker="counterpart",
        at=NOW,
        sequence=4,
    )

    result = ConversationContinuityCompiler().compile(
        dialogue=(first, reply, pending, current),
        trigger_ref="event:dialogue:current",
        retrieval_candidates=(
            ContinuityRetrievalCandidate(
                slice_name="active_memory_candidates",
                item_ref="memory:shenzhen-trip",
                texts=("上午说过深圳旅行，回来以后觉得不太好玩。",),
            ),
            ContinuityRetrievalCandidate(
                slice_name="active_memory_candidates",
                item_ref="memory:coffee",
                texts=("用户平时喜欢喝手冲咖啡。",),
            ),
        ),
    )

    by_id = {item.dialogue_id: item for item in result.dialogue}
    assert "pending_interaction" in by_id[pending.dialogue_id].continuity_reasons
    assert "pending_interaction" not in by_id[first.dialogue_id].continuity_reasons
    # Retrieval candidates are already source-bound Context.  Lexical overlap
    # must not secretly promote one of them into the model's working set.
    assert result.rank_overrides == frozenset()


def test_current_cue_prefetches_only_the_source_bound_associative_memory() -> None:
    earlier = _dialogue(
        "earlier",
        "我最近开始很喜欢喝乌龙茶。",
        speaker="counterpart",
        at=NOW - timedelta(hours=5),
        sequence=1,
    )
    current = _dialogue(
        "current",
        "你还记得我之前说过喜欢乌龙茶吗？",
        speaker="counterpart",
        at=NOW,
        sequence=2,
    )
    candidates = (
        ContinuityRetrievalCandidate(
            slice_name="active_memory_candidates",
            item_ref="memory:oolong",
            texts=("用户最近开始喜欢喝乌龙茶。",),
        ),
        ContinuityRetrievalCandidate(
            slice_name="active_memory_candidates",
            item_ref="memory:coffee",
            texts=("用户平时喜欢喝手冲咖啡。",),
        ),
    )
    compiler = ConversationContinuityCompiler()

    first = compiler.compile(
        dialogue=(earlier, current),
        trigger_ref="event:dialogue:current",
        retrieval_candidates=candidates,
    )
    replayed = compiler.compile(
        dialogue=(earlier, current),
        trigger_ref="event:dialogue:current",
        retrieval_candidates=tuple(reversed(candidates)),
    )

    expected = frozenset({("active_memory_candidates", "memory:oolong")})
    assert first.rank_overrides == expected
    assert replayed.rank_overrides == expected


def test_one_generic_two_character_overlap_does_not_prefetch_a_memory() -> None:
    current = _dialogue(
        "current",
        "今天有点忙。",
        speaker="counterpart",
        at=NOW,
        sequence=1,
    )

    result = ConversationContinuityCompiler().compile(
        dialogue=(current,),
        trigger_ref="event:dialogue:current",
        retrieval_candidates=(
            ContinuityRetrievalCandidate(
                slice_name="active_memory_candidates",
                item_ref="memory:coffee",
                texts=("今天去喝咖啡。",),
            ),
        ),
    )

    assert result.rank_overrides == frozenset()


def test_delayed_reply_does_not_acknowledge_a_newer_stuck_message() -> None:
    replied = _dialogue(
        "replied",
        "上午那件事我处理好了。",
        speaker="counterpart",
        at=NOW - timedelta(minutes=4),
        sequence=1,
    )
    stuck = _dialogue(
        "stuck",
        "第三条你是不是没看到？",
        speaker="counterpart",
        at=NOW - timedelta(minutes=2),
        sequence=2,
    )
    delayed_reply = _dialogue(
        "delayed-reply",
        "看到了，上午那件事辛苦了。",
        speaker="companion",
        at=NOW - timedelta(minutes=1),
        sequence=3,
        acknowledges=("event:dialogue:replied",),
    )
    emoji = _dialogue(
        "emoji",
        "👀",
        speaker="counterpart",
        at=NOW,
        sequence=4,
    )

    result = ConversationContinuityCompiler().compile(
        dialogue=(replied, stuck, delayed_reply, emoji),
        trigger_ref="event:dialogue:emoji",
    )

    by_id = {item.dialogue_id: item for item in result.dialogue}
    assert "pending_interaction" in by_id[stuck.dialogue_id].continuity_reasons
    assert "pending_interaction" not in by_id[replied.dialogue_id].continuity_reasons


def test_common_time_word_alone_does_not_reactivate_an_unrelated_topic() -> None:
    coffee = _dialogue(
        "coffee",
        "今天去喝咖啡。",
        speaker="counterpart",
        at=NOW - timedelta(hours=3),
        sequence=1,
    )
    current = _dialogue(
        "busy",
        "今天有点忙。",
        speaker="counterpart",
        at=NOW,
        sequence=2,
    )

    result = ConversationContinuityCompiler().compile(
        dialogue=(coffee, current),
        trigger_ref="event:dialogue:busy",
    )

    by_id = {item.dialogue_id: item for item in result.dialogue}
    assert "topic_reactivation" not in by_id[coffee.dialogue_id].continuity_reasons


def test_clarification_keeps_user_context_behind_recent_companion_questions() -> None:
    dashboard = _dialogue(
        "dashboard",
        "都是些工作上的事情，需要一些看板工具。",
        speaker="counterpart",
        at=NOW - timedelta(minutes=4),
        sequence=1,
    )
    tool_question = _dialogue(
        "tool-question",
        "你平时用什么？Trello 还是 Notion 那种？",
        speaker="companion",
        at=NOW - timedelta(minutes=3),
        sequence=2,
        acknowledges=("event:dialogue:dashboard",),
    )
    assignment = _dialogue(
        "assignment",
        "然后就问我能不能做。",
        speaker="counterpart",
        at=NOW - timedelta(minutes=2),
        sequence=3,
    )
    accept_question = _dialogue(
        "accept-question",
        "你妈又给你派活了？那你打算接吗？",
        speaker="companion",
        at=NOW - timedelta(minutes=1),
        sequence=4,
        acknowledges=("event:dialogue:assignment",),
    )
    current = _dialogue(
        "current-tool",
        "是飞书那种啦，用的 OpenClaw 接入的。",
        speaker="counterpart",
        at=NOW,
        sequence=5,
    )

    result = ConversationContinuityCompiler(
        max_items=4,
        max_companion_items=2,
    ).compile(
        dialogue=(dashboard, tool_question, assignment, accept_question, current),
        trigger_ref="event:dialogue:current-tool",
    )

    retained = {item.dialogue_id for item in result.dialogue}
    assert dashboard.dialogue_id in retained
