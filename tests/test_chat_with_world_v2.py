from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "chat_with_world_v2.py"
_SPEC = importlib.util.spec_from_file_location("chat_with_world_v2_script", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_chat_turn_record = _MODULE.build_chat_turn_record
build_burst_result_records = _MODULE.build_burst_result_records
build_burst_drain_record = _MODULE.build_burst_drain_record
build_session_failure_record = _MODULE.build_session_failure_record
json_safe = _MODULE.json_safe


@dataclass(frozen=True)
class _Value:
    name: str
    count: int


def test_json_safe_preserves_nested_observability_values() -> None:
    observed_at = datetime(2026, 8, 9, 12, 34, 56, tzinfo=UTC)

    value = json_safe(
        {
            "when": observed_at,
            "path": Path("scratch.sqlite"),
            "tuple": ("a", _Value("b", 2)),
        }
    )

    assert value == {
        "when": "2026-08-09T12:34:56+00:00",
        "path": "scratch.sqlite",
        "tuple": ["a", {"name": "b", "count": 2}],
    }
    json.dumps(value, ensure_ascii=False)


def test_build_chat_turn_record_keeps_user_role_latency_and_evidence() -> None:
    started = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    finished = datetime(2026, 8, 9, 12, 0, 5, 123000, tzinfo=UTC)
    result = type("Result", (), {"status": "action_authorized", "action_id": "action:1"})()
    drain = type(
        "Drain",
        (),
        {"action_statuses": ("delivered",), "background_statuses": ("appraisal:accepted",)},
    )()

    record = build_chat_turn_record(
        turn_index=3,
        message_id="message:3",
        user_text="你好",
        started_at=started,
        finished_at=finished,
        result=result,
        drain_result=drain,
        visible=(
            ("text", "user", "你好呀"),
            ("sticker", "user", "sticker:1"),
        ),
        evidence={"cursor": {"sequence": 9}, "semantic_hash": "a" * 64},
        health={"status": "healthy"},
    )

    assert record["record_type"] == "turn"
    assert record["turn_index"] == 3
    assert record["user"] == {"message_id": "message:3", "text": "你好"}
    assert record["role_units"] == [
        {"kind": "text", "recipient_id": "user", "body": "你好呀"},
        {"kind": "sticker", "recipient_id": "user", "body": "sticker:1"},
    ]
    assert record["latency_ms"] == 5123.0
    assert record["result"] == {"status": "action_authorized", "action_id": "action:1"}
    assert record["drain"] == {
        "action_statuses": ["delivered"],
        "background_statuses": ["appraisal:accepted"],
    }
    assert record["evidence"]["semantic_hash"] == "a" * 64


def test_burst_records_keep_message_ids_when_one_task_fails() -> None:
    users = [
        {"turn_index": 1, "message_id": "message:1", "text": "一"},
        {"turn_index": 2, "message_id": "message:2", "text": "二"},
    ]
    result = type("Result", (), {"status": "action_authorized", "action_id": "action:1"})()

    records = build_burst_result_records(
        burst_users=users,
        results=[result, RuntimeError("provider timeout")],
    )

    assert records == [
        {
            "turn_index": 1,
            "message_id": "message:1",
            "status": "action_authorized",
            "action_id": "action:1",
        },
        {
            "turn_index": 2,
            "message_id": "message:2",
            "status": "error",
            "error": {"type": "RuntimeError", "message": "provider timeout"},
        },
    ]


def test_session_failure_record_is_json_safe_and_does_not_hide_error() -> None:
    record = build_session_failure_record(
        finished_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        error=ValueError("database is unavailable"),
    )

    assert record["record_type"] == "session_failed"
    assert record["error"] == {
        "type": "ValueError",
        "message": "database is unavailable",
    }
    json.dumps(json_safe(record), ensure_ascii=False)


def test_burst_drain_record_preserves_drain_failure() -> None:
    record = build_burst_drain_record(
        drain_result=None, error=RuntimeError("drain failed")
    )

    assert record == {
        "status": "error",
        "error": {"type": "RuntimeError", "message": "drain failed"},
    }
