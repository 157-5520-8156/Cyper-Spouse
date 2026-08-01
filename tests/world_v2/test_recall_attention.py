from __future__ import annotations

from datetime import UTC, datetime

from companion_daemon.world_v2.recall_attention import (
    MAX_RECALL_QUERY_CHARACTERS,
    build_automatic_recall_request,
    select_recent_dialogue_for_automatic_recall,
)


def test_automatic_attention_packet_is_bounded_before_request_validation() -> None:
    request = build_automatic_recall_request(
        observation_text="上午那件事后来怎么样了？" + "很长的用户原话" * 300,
        affect_values=(
            {
                "components": [
                    {
                        "dimension": "hurt",
                        "intensity_bp": 6200,
                        "source_cluster_ref": "cluster:opaque-affect-ref",
                    }
                ]
            },
        ),
        appraisal_values=(
            {
                "subject_ref": "user:primary",
                "source_cluster_ref": "cluster:opaque-appraisal-ref",
                "confidence_bp": 8800,
                "hypotheses": [
                    {
                        "meaning": "dismissal",
                        "attribution": "user",
                        "severity": "moderate",
                        "weight_bp": 10000,
                    }
                ],
            },
        ),
        relationship_values=(
            {
                "stage": "close_friend",
                "temperature": "strained",
                "variables": {"trust_bp": 7600, "closeness_bp": 8100},
            },
        ),
        situation_value={
            "time_segment": "morning",
            "activity_slices": [
                {"activity_kind": f"study.task.{index}", "phase": "active"}
                for index in range(100)
            ],
            "attention_slice": {"state": "focused"},
            "social_environment": {"relation": "alone"},
        },
        open_thread_values=(
            {
                "thread_id": "thread:morning-follow-up",
                "kind": "topic_open",
                "importance_bp": 8300,
            },
        ),
        link_refs=(
            "cluster:opaque-affect-ref",
            "cluster:opaque-appraisal-ref",
            "thread:morning-follow-up",
        ),
        occurred_from=datetime(2026, 7, 28, 0, tzinfo=UTC),
        occurred_to=datetime(2026, 7, 28, 4, tzinfo=UTC),
        memory_kinds=("semantic", "episodic", "semantic"),
        limit=4,
    )

    assert len(request.query_text) <= MAX_RECALL_QUERY_CHARACTERS
    assert request.lexical_text is not None
    assert len(request.lexical_text) <= MAX_RECALL_QUERY_CHARACTERS
    assert request.lexical_text.startswith("上午那件事后来怎么样了？")
    assert "当前感受" in request.query_text
    assert "当前解读" in request.query_text
    assert "close_friend" in request.query_text
    assert "cluster:opaque" not in request.query_text
    assert request.link_refs == (
        "cluster:opaque-affect-ref",
        "cluster:opaque-appraisal-ref",
        "thread:morning-follow-up",
    )
    assert request.occurred_from == datetime(2026, 7, 28, 0, tzinfo=UTC)
    assert request.occurred_to == datetime(2026, 7, 28, 4, tzinfo=UTC)
    assert request.memory_kinds == ("episodic", "semantic")


def test_short_return_message_uses_verified_recent_dialogue_as_semantic_attention() -> None:
    request = build_automatic_recall_request(
        observation_text="来了",
        recent_dialogue_values=(
            {
                "speaker": "counterpart",
                "text": "好累，下午又要学雅思了",
                "occurred_at": "2026-07-28T08:41:50Z",
            },
            {
                "speaker": "companion",
                "text": "雅思啊，确实挺磨人的。下午加油吧。",
                "occurred_at": "2026-07-28T08:43:50Z",
            },
        ),
    )

    assert "最近对话" in request.query_text
    assert "下午又要学雅思了" in request.query_text
    assert "下午加油吧" in request.query_text
    # A lone two-character overlap made “来了” score 10,000 against the old
    # and unrelated “回来了”.  Dense attention still sees the exact inbound
    # text above; only the brittle lexical-only lane is withheld.
    assert request.lexical_text is None


def test_automatic_recall_dialogue_attention_reserves_recent_counterpart_turns() -> None:
    """Several companion bubbles cannot evict the other person's recent turns."""

    dialogue = (
        {"speaker": "counterpart", "text": "摊贩那件事让我很烦"},
        {"speaker": "counterpart", "text": "我不是来问怎么处理的"},
        {"speaker": "counterpart", "text": "我只是想找你吐槽"},
        {"speaker": "companion", "text": "嗯？"},
        {"speaker": "companion", "text": "那你想说什么？"},
        {"speaker": "companion", "text": "我在听。"},
        {"speaker": "companion", "text": "慢慢讲。"},
    )

    selected = select_recent_dialogue_for_automatic_recall(dialogue)

    assert selected == (
        dialogue[1],
        dialogue[2],
        dialogue[5],
        dialogue[6],
    )
    request = build_automatic_recall_request(
        observation_text="你刚才要是只顾着问细节，我会觉得你根本没在听。",
        recent_dialogue_values=selected,
    )
    assert "我不是来问怎么处理的" in request.query_text
    assert "我只是想找你吐槽" in request.query_text
    assert "嗯？" not in request.query_text
    assert "慢慢讲。" in request.query_text
    # The selection is a pure replayable attention policy, not a semantic
    # interpretation of either person's text.
    assert select_recent_dialogue_for_automatic_recall(dialogue) == selected
