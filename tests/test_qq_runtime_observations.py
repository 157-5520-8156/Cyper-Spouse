import json
import stat
from datetime import datetime, timezone

from companion_daemon.qq_runtime_observations import QQTurnObservationJSONLExporter
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
            observed_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
            adapter="napcat",
            action_ids=("action-7",),
            segment_ids=("segment-3",),
            durable_receipt_status="delivered",
            recovery_result="receipt_lookup_delivered",
            failure_reason="this must not become an exported free-form string",
        )
    )

    rows = [json.loads(line) for line in report_path.read_text().splitlines()]
    assert rows == [
        {
            "schema_version": 1,
            "observed_at": "2026-07-13T08:00:00+00:00",
            "adapter": "napcat",
            "outcome": "reply_delivered",
            "cadence": "hot",
            "input_count": 2,
            "elapsed_ms": 1234,
            "coalescing_wait_ms": None,
            "first_visible_elapsed_ms": 456,
            "failure_type": None,
            "action_ids": ["action-7"],
            "segment_ids": ["segment-3"],
            "durable_receipt_status": "delivered",
            "recovery_result": "receipt_lookup_delivered",
        }
    ]
    assert "private-user-id" not in report_path.read_text()
    assert "free-form" not in report_path.read_text()
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
