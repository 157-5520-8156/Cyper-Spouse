from __future__ import annotations

from datetime import UTC, datetime

from companion_daemon.world_v2.recall_attention import (
    MAX_RECALL_QUERY_CHARACTERS,
    build_automatic_recall_request,
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
