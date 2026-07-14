from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

import pytest

from companion_daemon.world_v2.errors import ConcurrencyConflict, LedgerIntegrityError
from companion_daemon.world_v2.schemas import Action, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def event(event_id: str, observation_id: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id="world-sqlite-test",
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace-1",
        causation_id="cause-1",
        correlation_id="correlation-1",
        idempotency_key=event_id,
        payload={"observation_id": observation_id},
    )


def test_sqlite_ledger_survives_restart_and_retries_atomic_commit(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    first = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    events = [event("event-1", "obs-1"), event("event-2", "obs-2")]
    committed = first.commit(
        events,
        commit_id="commit-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    first.close()

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project().observation_refs == ("obs-1", "obs-2")
    assert reopened.rebuild() == reopened.project()
    assert reopened.commit(
        events,
        commit_id="commit-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    ) == committed
    assert reopened.project().world_revision == 2
    reopened.close()


def test_sqlite_ledger_compare_and_swap_across_instances(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    left = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    right = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")

    left.commit(
        [event("event-1", "obs-1")],
        commit_id="commit-left",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    with pytest.raises(ConcurrencyConflict):
        right.commit(
            [event("event-2", "obs-2")],
            commit_id="commit-right",
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
    left.close()
    right.close()


def test_sqlite_rebuild_detects_tampered_event_envelope(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-1", "obs-1")],
        commit_id="commit-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_events SET event_json = replace(event_json, 'obs-1', 'obs-X')"
        )

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    with pytest.raises(LedgerIntegrityError):
        reopened.rebuild()
    reopened.close()


def test_sqlite_project_normalizes_malformed_head_as_integrity_error(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_heads SET world_revision = 'not-an-integer'"
        )

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    with pytest.raises(LedgerIntegrityError):
        reopened.project()
    reopened.close()


def test_sqlite_head_preserves_authorized_actions_across_restart(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    action = Action(
        schema_version="world-v2.1",
        action_id="action-1",
        world_id="world-sqlite-test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        kind="reply",
        layer="external_action",
        intent_ref="intent-1",
        actor="companion:test",
        target="user:test",
        payload_ref="payload:1",
        payload_hash="sha256:payload-1",
        idempotency_key="world-sqlite-test:intent-1:reply",
        budget_reservation_id="budget-1",
        state="authorized",
        recovery_policy="effect_once",
    )
    authorized = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event-action-authorized-1",
        world_id="world-sqlite-test",
        event_type="ActionAuthorized",
        logical_time=NOW,
        created_at=NOW,
        actor="system:acceptance",
        source="acceptance",
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        idempotency_key="action-authorized:action-1",
        payload={"action": action.model_dump(mode="json")},
    )

    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [authorized],
        commit_id="commit-action-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project().actions == (action,)
    assert reopened.rebuild() == reopened.project()
    reopened.close()
