from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import sqlite3

from companion_daemon.ledger_storage_health import ledger_storage_snapshot


def test_storage_health_warns_when_idle_audit_dominates_recent_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "health.sqlite"
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE world_v2_events (ledger_sequence INTEGER, event_json TEXT)"
        )
        for index in range(7):
            connection.execute(
                "INSERT INTO world_v2_events VALUES (?,?)",
                (
                    index,
                    json.dumps(
                        {
                            "event_type": "ClockAdvanced",
                            "source": "scheduler",
                            "created_at": (now - timedelta(minutes=index)).isoformat(),
                        }
                    ),
                ),
            )
        for index in range(3):
            connection.execute(
                "INSERT INTO world_v2_events VALUES (?,?)",
                (
                    10 + index,
                    json.dumps(
                        {
                            "event_type": "ObservationRecorded",
                            "source": "qq:c2c",
                            "created_at": now.isoformat(),
                        }
                    ),
                ),
            )

    result = ledger_storage_snapshot(path, now=now)

    assert result["status"] == "warning"
    assert result["event_count_24h"] == 10
    assert result["clock_life_trigger_ratio_24h"] == 0.7
    assert "clock_trigger_ratio_over_60pct" in result["warning_reasons"]
