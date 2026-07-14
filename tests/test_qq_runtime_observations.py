import json
import stat
from datetime import datetime, timezone

from companion_daemon.qq_runtime_observations import (
    QQTurnObservationJSONLExporter,
    _main,
    load_qq_turn_observation_jsonl,
    summarize_qq_turn_experience,
)
from companion_daemon.qq_websocket import TurnRuntimeObservation


def test_jsonl_exporter_persists_a_redacted_turn_observation(tmp_path) -> None:
    report_path = tmp_path / "private" / "qq-turns.jsonl"
    exporter = QQTurnObservationJSONLExporter(report_path)

    exporter(
        TurnRuntimeObservation(
            key="c2c:private-user-id",
            outcome="reply_delivered",
            elapsed_seconds=1.234,
            cadence="hot",
            input_count=2,
            first_visible_elapsed_seconds=0.456,
            seen_elapsed_seconds=0.101,
            typing_elapsed_seconds=0.123,
            model_returned_elapsed_seconds=0.321,
            candidate_accepted_elapsed_seconds=0.389,
            delivery_settled_elapsed_seconds=0.456,
            observed_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
            adapter="napcat",
            action_ids=("action-7",),
            message_kinds=("reply",),
            segment_ids=("segment-3",),
            affective_reading_kinds=("possible_disappointment",),
            expression_affordance_candidate_kinds=("soft_repair", "let_it_pass"),
            selected_affordance_kind="soft_repair",
            user_affect_kinds=("disappointment",),
            user_affect_recorded=True,
            private_impression_recorded=True,
            durable_receipt_status="delivered",
            recovery_result="receipt_lookup_delivered",
            failure_reason="this must not become an exported free-form string",
        )
    )

    rows = [json.loads(line) for line in report_path.read_text().splitlines()]
    assert rows == [
        {
            "schema_version": 2,
            "observed_at": "2026-07-13T08:00:00+00:00",
            "adapter": "napcat",
            "outcome": "reply_delivered",
            "cadence": "hot",
            "input_count": 2,
            "elapsed_ms": 1234,
            "coalescing_wait_ms": None,
            "first_visible_elapsed_ms": 456,
            "seen_elapsed_ms": 101,
            "typing_elapsed_ms": 123,
            "model_returned_elapsed_ms": 321,
            "candidate_accepted_elapsed_ms": 389,
            "delivery_settled_elapsed_ms": 456,
            "failure_type": None,
            "action_ids": ["action-7"],
            "message_kinds": ["reply"],
            "segment_ids": ["segment-3"],
            "segment_count": 1,
            "multi_segment": False,
            "affective_reading_kinds": ["possible_disappointment"],
            "expression_affordance_candidate_kinds": ["soft_repair", "let_it_pass"],
            "selected_affordance_kind": "soft_repair",
            "user_affect_kinds": ["disappointment"],
            "user_affect_recorded": True,
            "private_impression_recorded": True,
            "durable_receipt_status": "delivered",
            "recovery_result": "receipt_lookup_delivered",
        }
    ]
    assert "private-user-id" not in report_path.read_text()
    assert "free-form" not in report_path.read_text()
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600


def test_redacted_turn_observation_summary_counts_experience_signals(tmp_path) -> None:
    report_path = tmp_path / "private" / "qq-turns.jsonl"
    exporter = QQTurnObservationJSONLExporter(report_path)

    exporter(
        TurnRuntimeObservation(
            key="c2c:private-user-id",
            outcome="reply_delivered",
            elapsed_seconds=1.0,
            cadence="hot",
            input_count=1,
            observed_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
            adapter="napcat",
            message_kinds=("reply",),
            segment_ids=("segment-1", "segment-2"),
            affective_reading_kinds=("possible_disappointment",),
            selected_affordance_kind="soft_repair",
            user_affect_kinds=("disappointment",),
            user_affect_recorded=True,
            private_impression_recorded=True,
        )
    )
    exporter(
        TurnRuntimeObservation(
            key="c2c:private-user-id",
            outcome="reply_delivered",
            elapsed_seconds=0.8,
            cadence="hot",
            input_count=1,
            observed_at=datetime(2026, 7, 13, 8, 1, tzinfo=timezone.utc),
            adapter="napcat",
            message_kinds=("afterthought",),
            segment_ids=("segment-3",),
            affective_reading_kinds=(),
            selected_affordance_kind="let_it_pass",
            user_affect_kinds=("repaired",),
            user_affect_recorded=False,
            private_impression_recorded=False,
        )
    )
    exporter(
        TurnRuntimeObservation(
            key="c2c:private-user-id",
            outcome="reply_delivered",
            elapsed_seconds=0.7,
            cadence="warm",
            input_count=1,
            observed_at=datetime(2026, 7, 13, 8, 2, tzinfo=timezone.utc),
            adapter="napcat",
            message_kinds=("reply",),
            segment_ids=("segment-4",),
        )
    )

    rows = load_qq_turn_observation_jsonl(report_path)
    summary = summarize_qq_turn_experience(rows)

    assert summary == {
        "schema_version": 1,
        "sample_count": 3,
        "outcome_counts": {"reply_delivered": 3},
        "cadence_counts": {"hot": 2, "warm": 1},
        "multi_segment_count": 1,
        "multi_segment_rate": 0.3333,
        "single_bubble_reply_count": 1,
        "single_bubble_reply_rate": 0.3333,
        "message_kind_counts": {"afterthought": 1, "reply": 2},
        "afterthought_count": 1,
        "afterthought_rate": 0.3333,
        "selected_affordance_counts": {"let_it_pass": 1, "soft_repair": 1},
        "top_selected_affordance_rate": 0.3333,
        "fixed_pattern_diagnostic": "insufficient_sample",
        "affective_reading_counts": {"possible_disappointment": 1},
        "user_affect_kind_counts": {"disappointment": 1, "repaired": 1},
        "user_affect_recorded_count": 1,
        "user_affect_recorded_rate": 0.3333,
        "affective_correction_signal_count": 1,
        "affective_correction_signal_rate": 0.3333,
        "private_impression_recorded_count": 1,
        "private_impression_recorded_rate": 0.3333,
        "privacy": {
            "contains_message_text": False,
            "contains_user_or_platform_identifier": False,
            "contains_external_receipts": False,
            "contains_free_form_failure_reason": False,
        },
    }
    encoded = json.dumps(summary, ensure_ascii=False)
    assert "private-user-id" not in encoded


def test_redacted_turn_observation_summary_flags_repetitive_affordance_pattern() -> None:
    rows = [
        {
            "outcome": "reply_delivered",
            "cadence": "hot",
            "segment_count": 1,
            "message_kinds": ["reply"],
            "selected_affordance_kind": "soft_repair" if index < 8 else "let_it_pass",
            "user_affect_kinds": [],
        }
        for index in range(10)
    ]

    summary = summarize_qq_turn_experience(rows)

    assert summary["sample_count"] == 10
    assert summary["selected_affordance_counts"] == {
        "let_it_pass": 2,
        "soft_repair": 8,
    }
    assert summary["top_selected_affordance_rate"] == 0.8
    assert summary["fixed_pattern_diagnostic"] == "possible_fixed_pattern"


def test_redacted_turn_observation_cli_prints_summary_without_private_content(
    tmp_path, capsys
) -> None:
    report_path = tmp_path / "private" / "qq-turns.jsonl"
    exporter = QQTurnObservationJSONLExporter(report_path)
    exporter(
        TurnRuntimeObservation(
            key="c2c:private-user-id",
            outcome="reply_delivered",
            elapsed_seconds=0.9,
            cadence="hot",
            input_count=1,
            observed_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
            adapter="napcat",
            message_kinds=("reply",),
            segment_ids=("segment-1",),
            affective_reading_kinds=("possible_disappointment",),
            selected_affordance_kind="soft_repair",
            user_affect_kinds=("disappointment",),
            user_affect_recorded=True,
            private_impression_recorded=True,
        )
    )

    assert _main((str(report_path), "--pretty")) == 0

    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["sample_count"] == 1
    assert summary["selected_affordance_counts"] == {"soft_repair": 1}
    assert summary["user_affect_kind_counts"] == {"disappointment": 1}
    assert "private-user-id" not in output
