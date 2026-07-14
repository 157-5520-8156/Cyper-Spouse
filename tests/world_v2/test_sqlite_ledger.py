from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from companion_daemon.world_v2.errors import ConcurrencyConflict, LedgerIntegrityError
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    BudgetSettlement,
    WorldEvent,
)
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


def test_sqlite_rebuild_upcasts_verified_legacy_event_bytes(tmp_path) -> None:
    path = tmp_path / "world-v2-legacy.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-legacy", "obs-legacy")],
        commit_id="commit-legacy",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        current_json = connection.execute(
            "SELECT event_json FROM world_v2_events WHERE event_id = 'event-legacy'"
        ).fetchone()[0]
        legacy = json.loads(current_json)
        legacy["schema_version"] = "world-v2.0"
        legacy_payload = json.dumps(
            {"observation_ref": "obs-legacy"},
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy["payload_json"] = legacy_payload
        legacy["payload_hash"] = hashlib.sha256(legacy_payload.encode()).hexdigest()
        legacy_json = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE world_v2_events SET event_json = ?, event_hash = ? "
            "WHERE event_id = 'event-legacy'",
            (legacy_json, hashlib.sha256(legacy_json.encode()).hexdigest()),
        )

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.rebuild() == expected
    reopened.close()


def test_sqlite_rebuild_selects_only_installed_replay_artifacts(tmp_path) -> None:
    path = tmp_path / "world-v2-replay-target.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-replay-target", "obs-replay-target")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    assert ledger.rebuild(
        target_schema_version="world-v2.1",
        reducer_bundle_version="world-v2-reducers.1",
    ) == ledger.project()
    with pytest.raises(ValueError, match="not installed"):
        ledger.rebuild(reducer_bundle_version="world-v1-reducers.9")
    with pytest.raises(ValueError, match="target schema.*not installed"):
        ledger.rebuild(target_schema_version="world-v3.0")
    ledger.close()


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
    reservation = BudgetReservation(
        reservation_id="budget-1",
        account_id="budget-account-chat",
        action_id="action-1",
        category="chat",
        amount_limit=10_000,
    )
    account = BudgetAccount(
        account_id="budget-account-chat",
        category="chat",
        window_id="test-window",
        limit=1_000_000,
    )
    configured = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event-budget-account-configured",
        world_id="world-sqlite-test",
        event_type="BudgetAccountConfigured",
        logical_time=NOW,
        created_at=NOW,
        actor="system:acceptance",
        source="acceptance",
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        idempotency_key="budget-account:chat:test-window",
        payload={"account": account.model_dump(mode="json")},
    )
    reserved = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event-budget-reserved-1",
        world_id="world-sqlite-test",
        event_type="BudgetReserved",
        logical_time=NOW,
        created_at=NOW,
        actor="system:acceptance",
        source="acceptance",
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        idempotency_key="budget-reserved:budget-1",
        payload={"reservation": reservation.model_dump(mode="json")},
    )

    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [configured, reserved, authorized],
        commit_id="commit-action-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project().actions == (action,)
    assert reopened.rebuild() == reopened.project()
    reopened.close()


def test_budget_overrun_with_other_reservations_survives_restart(tmp_path) -> None:
    path = tmp_path / "world-v2-budget-overrun.sqlite3"

    def domain_event(event_id: str, event_type: str, payload: dict[str, object]):
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id="world-sqlite-test",
            event_type=event_type,
            logical_time=NOW,
            created_at=NOW,
            actor="system:test",
            source="test",
            trace_id="trace-budget-overrun",
            causation_id="acceptance-budget-overrun",
            correlation_id="conversation-budget-overrun",
            idempotency_key=event_id,
            payload=payload,
        )

    account = BudgetAccount(
        account_id="account-chat",
        category="chat",
        window_id="window-1",
        limit=100,
    )
    first = BudgetReservation(
        reservation_id="reservation-1",
        account_id=account.account_id,
        action_id="action-1",
        category="chat",
        amount_limit=60,
    )
    second = BudgetReservation(
        reservation_id="reservation-2",
        account_id=account.account_id,
        action_id="action-2",
        category="chat",
        amount_limit=40,
    )
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [
            domain_event("event-account", "BudgetAccountConfigured", {
                "account": account.model_dump(mode="json")
            }),
            domain_event("event-reservation-1", "BudgetReserved", {
                "reservation": first.model_dump(mode="json")
            }),
            domain_event("event-reservation-2", "BudgetReserved", {
                "reservation": second.model_dump(mode="json")
            }),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    settlement = BudgetSettlement(
        settlement_id="settlement-1",
        reservation_id=first.reservation_id,
        action_id=first.action_id,
        result_id="result-1",
        state="settled",
        cost_actual=120,
        cost_delta=120,
    )
    ledger.commit(
        [domain_event("event-settlement", "BudgetSettled", {
            "settlement": settlement.model_dump(mode="json")
        })],
        expected_world_revision=3,
        expected_deliberation_revision=0,
    )
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    projection = reopened.project()
    assert projection.budget_accounts[0].spent == 120
    assert projection.budget_accounts[0].reserved == 40
    assert projection.budget_accounts[0].overrun == 20
    assert reopened.rebuild() == projection
    reopened.close()
