from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

import companion_daemon.world_v2.ledger_maintenance as ledger_maintenance_module
from companion_daemon.world_v2 import Action, BudgetAccount, BudgetReservation
from companion_daemon.process_lock import AlreadyRunningError, SingleInstanceLock
from companion_daemon.qq_outbound_owner import qq_outbound_owner_lock_path
from companion_daemon.world_v2.ledger_maintenance import (
    LEGACY_WORLD_ID,
    _derive_rebuilt_head,
    _inspect_stale_derived_head,
    _install_rebuilt_head,
    _v2_fingerprint,
    compact_retired_v1,
    repair_stale_derived_head,
)
from companion_daemon.world_v2.errors import ConcurrencyConflict, LedgerIntegrityError
from companion_daemon.world_v2.reducers import REDUCER_BUNDLE_VERSION
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


REPAIR_WORLD_ID = "world:repair-test"
REPAIR_SOURCE_BUNDLE = "world-v2-reducers.41"
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _repair_event() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:repair:test",
        world_id=REPAIR_WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:repair",
        causation_id="cause:repair",
        correlation_id="correlation:repair",
        idempotency_key="repair:test",
        payload={"observation_id": "observation:repair:test"},
    )


def _repair_action_events() -> tuple[WorldEvent, ...]:
    account = BudgetAccount(
        account_id="budget:repair",
        category="chat",
        window_id="repair-window",
        limit=1_000_000,
    )
    reservation = BudgetReservation(
        reservation_id="reservation:repair",
        account_id=account.account_id,
        action_id="action:repair",
        category="chat",
        amount_limit=10_000,
    )
    action = Action.model_validate(
        {
            "schema_version": "world-v2.1",
            "action_id": "action:repair",
            "world_id": REPAIR_WORLD_ID,
            "logical_time": NOW,
            "created_at": NOW,
            "trace_id": "trace:repair",
            "causation_id": "acceptance:repair",
            "correlation_id": "correlation:repair",
            "kind": "reply",
            "layer": "external_action",
            "intent_ref": "intent:repair",
            "actor": "companion:girl",
            "target": "user:repair",
            "payload_ref": "payload:repair",
            "payload_hash": "sha256:repair",
            "idempotency_key": "action:repair",
            "budget_reservation_id": reservation.reservation_id,
            "state": "authorized",
            "recovery_policy": "effect_once",
        }
    )
    payloads = (
        (
            "event:repair:budget-account",
            "BudgetAccountConfigured",
            {"account": account.model_dump(mode="json")},
        ),
        (
            "event:repair:budget-reserved",
            "BudgetReserved",
            {"reservation": reservation.model_dump(mode="json")},
        ),
        (
            "event:repair:action-authorized",
            "ActionAuthorized",
            {"action": action.model_dump(mode="json")},
        ),
    )
    return tuple(
        WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=REPAIR_WORLD_ID,
            event_type=event_type,
            logical_time=NOW,
            created_at=NOW,
            actor="system:test",
            source="test",
            trace_id="trace:repair",
            causation_id="cause:repair",
            correlation_id="correlation:repair",
            idempotency_key=event_id,
            payload=payload,
        )
        for event_id, event_type, payload in payloads
    )


def _stale_derived_head_database(
    path: Path,
    *,
    split_state: bool = True,
    include_action: bool = True,
) -> tuple[ProjectionCursor, int, str, str, str]:
    ledger = SQLiteWorldLedger(path=path, world_id=REPAIR_WORLD_ID)
    ledger.commit(
        (_repair_event(),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    if include_action:
        after_observation = ledger.project()
        ledger.commit(
            _repair_action_events(),
            expected_world_revision=after_observation.world_revision,
            expected_deliberation_revision=after_observation.deliberation_revision,
        )
    projected = ledger.project()
    cursor = ProjectionCursor(
        world_revision=projected.world_revision,
        deliberation_revision=projected.deliberation_revision,
        ledger_sequence=projected.ledger_sequence,
    )
    legacy_state = ledger._state_from_projection(projected)  # noqa: SLF001
    legacy_state_json = ledger._encode_state(legacy_state)  # noqa: SLF001
    healthy_legacy_semantic_hash = ledger._legacy_semantic_hash(  # noqa: SLF001
        state_json=legacy_state_json,
        world_revision=cursor.world_revision,
        reducer_bundle_version=REPAIR_SOURCE_BUNDLE,
    )
    canonical_legacy_state = json.dumps(
        json.loads(legacy_state_json),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_state_hash = hashlib.sha256(
        ledger._state_hash_material(  # noqa: SLF001
            canonical_state=canonical_legacy_state,
            cursor=cursor,
            reducer_bundle_version=REPAIR_SOURCE_BUNDLE,
        )
    ).hexdigest()
    ledger.close()

    stale_semantic_hash = "0" * 64
    with sqlite3.connect(path) as connection:
        if split_state:
            stored_state_json = str(
                connection.execute(
                    "SELECT state_json FROM world_v2_heads WHERE world_id = ?",
                    (REPAIR_WORLD_ID,),
                ).fetchone()[0]
            )
        else:
            connection.execute(
                "DELETE FROM world_v2_head_state_items WHERE world_id = ?",
                (REPAIR_WORLD_ID,),
            )
            stored_state_json = legacy_state_json
        connection.execute(
            """UPDATE world_v2_heads
               SET state_json = ?, semantic_hash = ?, state_hash = ?,
                   reducer_bundle_version = ?
               WHERE world_id = ?""",
            (
                stored_state_json,
                stale_semantic_hash,
                legacy_state_hash,
                REPAIR_SOURCE_BUNDLE,
                REPAIR_WORLD_ID,
            ),
        )
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM world_v2_events WHERE world_id = ?",
                (REPAIR_WORLD_ID,),
            ).fetchone()[0]
        )
        latest_event_hash = str(
            connection.execute(
                """SELECT event_hash FROM world_v2_events
                   WHERE world_id = ? ORDER BY ledger_sequence DESC LIMIT 1""",
                (REPAIR_WORLD_ID,),
            ).fetchone()[0]
        )
    connection.close()
    return (
        cursor,
        event_count,
        latest_event_hash,
        stale_semantic_hash,
        healthy_legacy_semantic_hash,
    )


def _protected_world_v2_rows(
    path: Path,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    excluded = {
        "world_v2_heads",
        "world_v2_head_state_items",
        "world_v2_ledger_mutation_epochs",
    }
    with sqlite3.connect(path) as connection:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name GLOB 'world_v2_*'
                   ORDER BY name"""
            )
            if str(row[0]) not in excluded
        )
        protected: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in tables:
            columns = tuple(
                str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            if "world_id" not in columns:
                continue
            order = ", ".join(f'"{column}"' for column in columns)
            protected[table] = tuple(
                tuple(row)
                for row in connection.execute(
                    f'SELECT * FROM "{table}" WHERE world_id = ? ORDER BY {order}',
                    (REPAIR_WORLD_ID,),
                )
            )
    connection.close()
    return protected


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE world_snapshots (
              world_id TEXT NOT NULL, revision INTEGER NOT NULL,
              state_json TEXT NOT NULL, state_hash TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE world_events (
              event_id TEXT PRIMARY KEY, world_id TEXT NOT NULL, revision INTEGER NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE world_v2_heads (
              world_id TEXT PRIMARY KEY, world_revision INTEGER NOT NULL,
              deliberation_revision INTEGER NOT NULL, ledger_sequence INTEGER NOT NULL,
              state_hash TEXT NOT NULL
            );
            CREATE TABLE world_v2_events (
              world_id TEXT NOT NULL, ledger_sequence INTEGER NOT NULL, event_hash TEXT NOT NULL
            );
            CREATE TABLE world_v2_ledger_mutation_epochs (
              world_id TEXT PRIMARY KEY, mutation_epoch INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO world_snapshots VALUES (?,1,?,?,?)",
            (LEGACY_WORLD_ID, "x" * 200_000, "legacy-hash", "2026-07-01"),
        )
        connection.execute(
            "INSERT INTO world_events VALUES (?,?,1,?)",
            ("legacy-event", LEGACY_WORLD_ID, "{}"),
        )
        connection.execute("INSERT INTO world_v2_heads VALUES ('world:v2',7,9,11,'v2-state')")
        connection.execute("INSERT INTO world_v2_events VALUES ('world:v2',11,'v2-event-hash')")
        connection.execute("INSERT INTO world_v2_ledger_mutation_epochs VALUES ('world:v2',3)")


def test_retired_v1_compaction_is_dry_run_by_default(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    _database(path)

    report = compact_retired_v1(path)

    assert report.applied is False
    assert (report.snapshot_rows, report.event_rows) == (1, 1)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_snapshots").fetchone()[0] == 1


def test_retired_v1_compaction_requires_exact_confirmation_and_preserves_v2(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    _database(path)
    before = path.stat().st_size

    with pytest.raises(ValueError, match="confirm-world-id"):
        compact_retired_v1(path, apply=True)
    report = compact_retired_v1(
        path,
        apply=True,
        confirm_world_id=LEGACY_WORLD_ID,
    )

    assert report.applied is True
    assert report.bytes_after < before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == 0
        assert connection.execute(
            "SELECT world_revision,deliberation_revision,ledger_sequence,state_hash "
            "FROM world_v2_heads"
        ).fetchone() == (7, 9, 11, "v2-state")
        assert (
            connection.execute("SELECT event_hash FROM world_v2_events").fetchone()[0]
            == "v2-event-hash"
        )


def test_retired_v1_compaction_refuses_to_apply_while_daemon_owns_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.sqlite"
    _database(path)

    with SingleInstanceLock(qq_outbound_owner_lock_path(path)):
        with pytest.raises(AlreadyRunningError):
            compact_retired_v1(
                path,
                apply=True,
                confirm_world_id=LEGACY_WORLD_ID,
            )


def test_v2_fingerprint_covers_auxiliary_world_tables(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    _database(path)
    with sqlite3.connect(path) as connection:
        before = _v2_fingerprint(connection)
        connection.execute("UPDATE world_v2_ledger_mutation_epochs SET mutation_epoch=4")
        after = _v2_fingerprint(connection)

    assert after != before


def test_stale_derived_head_repair_is_verified_dry_run_by_default(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale-derived-head.sqlite"
    cursor, event_count, latest_event_hash, stale_semantic_hash, _ = _stale_derived_head_database(
        path
    )

    with pytest.raises(LedgerIntegrityError, match="legacy head semantic hash is invalid"):
        SQLiteWorldLedger(path=path, world_id=REPAIR_WORLD_ID)

    report = repair_stale_derived_head(
        path,
        world_id=REPAIR_WORLD_ID,
        expected_source_bundle=REPAIR_SOURCE_BUNDLE,
        expected_cursor=cursor,
        expected_event_count=event_count,
        expected_latest_event_hash=latest_event_hash,
    )

    assert report.applied is False
    assert report.repairable is True
    assert report.source_bundle == REPAIR_SOURCE_BUNDLE
    assert report.target_bundle == REDUCER_BUNDLE_VERSION
    assert report.cursor == cursor
    assert report.event_count == event_count
    assert report.latest_event_hash == latest_event_hash
    assert report.old_semantic_hash == stale_semantic_hash
    assert report.new_semantic_hash != stale_semantic_hash
    assert report.rollback_database is None
    with sqlite3.connect(path) as connection:
        persisted = connection.execute(
            """SELECT reducer_bundle_version, semantic_hash
               FROM world_v2_heads WHERE world_id = ?""",
            (REPAIR_WORLD_ID,),
        ).fetchone()
    assert persisted == (REPAIR_SOURCE_BUNDLE, stale_semantic_hash)


def test_stale_derived_head_dry_run_does_not_change_source_rows(tmp_path: Path) -> None:
    path = tmp_path / "stale-derived-head.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(path)
    with sqlite3.connect(path) as connection:
        source_dump = tuple(connection.iterdump())

    repair_stale_derived_head(
        path,
        world_id=REPAIR_WORLD_ID,
        expected_source_bundle=REPAIR_SOURCE_BUNDLE,
        expected_cursor=cursor,
        expected_event_count=event_count,
        expected_latest_event_hash=latest_event_hash,
    )

    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == source_dump


def test_derived_head_repair_cli_is_dry_run_without_apply(tmp_path: Path) -> None:
    path = tmp_path / "cli-dry-run.sqlite"
    cursor, event_count, latest_event_hash, stale_semantic_hash, _ = _stale_derived_head_database(
        path
    )
    command = (
        sys.executable,
        str(Path(__file__).parents[2] / "scripts" / "repair_world_v2_derived_head.py"),
        "--database",
        str(path),
        "--world-id",
        REPAIR_WORLD_ID,
        "--expected-source-bundle",
        REPAIR_SOURCE_BUNDLE,
        "--expected-world-revision",
        str(cursor.world_revision),
        "--expected-deliberation-revision",
        str(cursor.deliberation_revision),
        "--expected-ledger-sequence",
        str(cursor.ledger_sequence),
        "--expected-event-count",
        str(event_count),
        "--expected-latest-event-hash",
        latest_event_hash,
    )

    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["applied"] is False
    assert payload["repairable"] is True
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT semantic_hash FROM world_v2_heads WHERE world_id = ?",
                (REPAIR_WORLD_ID,),
            ).fetchone()[0]
            == stale_semantic_hash
        )


def test_stale_split_v41_head_is_replayed_and_installed_without_new_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "split-v41-head.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(
        path,
        split_state=True,
    )
    with sqlite3.connect(path) as connection:
        event_rows_before = tuple(
            connection.execute(
                "SELECT * FROM world_v2_events WHERE world_id = ? ORDER BY ledger_sequence",
                (REPAIR_WORLD_ID,),
            )
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM world_v2_head_state_items WHERE world_id = ?",
                (REPAIR_WORLD_ID,),
            ).fetchone()[0]
            > 0
        )
    connection.close()

    repair_stale_derived_head(
        path,
        world_id=REPAIR_WORLD_ID,
        expected_source_bundle=REPAIR_SOURCE_BUNDLE,
        expected_cursor=cursor,
        expected_event_count=event_count,
        expected_latest_event_hash=latest_event_hash,
        apply=True,
        confirm_world_id=REPAIR_WORLD_ID,
    )

    with sqlite3.connect(path) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM world_v2_events WHERE world_id = ? ORDER BY ledger_sequence",
                    (REPAIR_WORLD_ID,),
                )
            )
            == event_rows_before
        )
    ledger = SQLiteWorldLedger(path=path, world_id=REPAIR_WORLD_ID)
    try:
        assert ledger.project() == ledger.rebuild()
    finally:
        ledger.close()


def test_stale_derived_head_repair_refuses_a_healthy_legacy_head(tmp_path: Path) -> None:
    path = tmp_path / "healthy-legacy-head.sqlite"
    cursor, event_count, latest_event_hash, _, healthy_semantic_hash = _stale_derived_head_database(
        path
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_heads SET semantic_hash = ? WHERE world_id = ?",
            (healthy_semantic_hash, REPAIR_WORLD_ID),
        )
    with sqlite3.connect(path) as connection:
        source_dump = tuple(connection.iterdump())

    with pytest.raises(ValueError, match="semantic hash is healthy"):
        repair_stale_derived_head(
            path,
            world_id=REPAIR_WORLD_ID,
            expected_source_bundle=REPAIR_SOURCE_BUNDLE,
            expected_cursor=cursor,
            expected_event_count=event_count,
            expected_latest_event_hash=latest_event_hash,
        )

    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == source_dump


def test_stale_derived_head_repair_refuses_apply_while_daemon_owns_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "daemon-owned.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(path)

    with SingleInstanceLock(qq_outbound_owner_lock_path(path)):
        with pytest.raises(AlreadyRunningError):
            repair_stale_derived_head(
                path,
                world_id=REPAIR_WORLD_ID,
                expected_source_bundle=REPAIR_SOURCE_BUNDLE,
                expected_cursor=cursor,
                expected_event_count=event_count,
                expected_latest_event_hash=latest_event_hash,
                apply=True,
                confirm_world_id=REPAIR_WORLD_ID,
            )


def test_stale_derived_head_repair_refuses_any_active_sqlite_connection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active-daemon-connection.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(path)
    daemon_connection = sqlite3.connect(path)
    daemon_connection.execute("SELECT COUNT(*) FROM world_v2_events").fetchone()
    try:
        with pytest.raises(AlreadyRunningError, match="active process connection"):
            repair_stale_derived_head(
                path,
                world_id=REPAIR_WORLD_ID,
                expected_source_bundle=REPAIR_SOURCE_BUNDLE,
                expected_cursor=cursor,
                expected_event_count=event_count,
                expected_latest_event_hash=latest_event_hash,
                apply=True,
                confirm_world_id=REPAIR_WORLD_ID,
            )
    finally:
        daemon_connection.close()


@pytest.mark.parametrize("field", ["actions", "pending_actions"])
def test_stale_derived_head_repair_refuses_action_storage_byte_drift(
    tmp_path: Path,
    field: str,
) -> None:
    path = tmp_path / f"{field}-byte-drift.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """UPDATE world_v2_head_state_items
               SET item_json = ' ' || item_json
               WHERE world_id = ? AND field = ? AND idx = 0""",
            (REPAIR_WORLD_ID, field),
        )
    connection.close()

    with pytest.raises(
        LedgerIntegrityError,
        match="changed Action or pending-Action storage bytes or count",
    ):
        repair_stale_derived_head(
            path,
            world_id=REPAIR_WORLD_ID,
            expected_source_bundle=REPAIR_SOURCE_BUNDLE,
            expected_cursor=cursor,
            expected_event_count=event_count,
            expected_latest_event_hash=latest_event_hash,
        )


@pytest.mark.parametrize("corruption", ["head", "event", "commit", "prefix"])
def test_stale_derived_head_repair_refuses_corrupt_authority(
    tmp_path: Path,
    corruption: str,
) -> None:
    path = tmp_path / f"corrupt-{corruption}.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(path)
    with sqlite3.connect(path) as connection:
        if corruption == "head":
            connection.execute(
                "UPDATE world_v2_heads SET state_hash = ? WHERE world_id = ?",
                ("e" * 64, REPAIR_WORLD_ID),
            )
        elif corruption == "event":
            connection.execute(
                "UPDATE world_v2_events SET event_json = event_json || ' '",
            )
        elif corruption == "commit":
            connection.execute(
                "UPDATE world_v2_commits SET result_json = '{}'",
            )
        else:
            connection.execute(
                """UPDATE world_v2_prefix_mmr_nodes SET node_hash = zeroblob(32)
                   WHERE world_id = ? AND rowid = (
                       SELECT rowid FROM world_v2_prefix_mmr_nodes
                       WHERE world_id = ? LIMIT 1
                   )""",
                (REPAIR_WORLD_ID, REPAIR_WORLD_ID),
            )

    with pytest.raises(LedgerIntegrityError):
        repair_stale_derived_head(
            path,
            world_id=REPAIR_WORLD_ID,
            expected_source_bundle=REPAIR_SOURCE_BUNDLE,
            expected_cursor=cursor,
            expected_event_count=event_count,
            expected_latest_event_hash=latest_event_hash,
        )


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ("bundle", "expected source bundle"),
        ("cursor", "expected cursor"),
        ("count", "expected event count"),
        ("hash", "latest event hash"),
    ],
)
def test_stale_derived_head_repair_requires_exact_operator_expectations(
    tmp_path: Path,
    changed: str,
    message: str,
) -> None:
    path = tmp_path / f"mismatch-{changed}.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(path)
    source_bundle = REPAIR_SOURCE_BUNDLE
    if changed == "bundle":
        source_bundle = "world-v2-reducers.40"
    elif changed == "cursor":
        cursor = cursor.model_copy(update={"ledger_sequence": cursor.ledger_sequence + 1})
    elif changed == "count":
        event_count += 1
    else:
        latest_event_hash = "f" * 64

    with pytest.raises(ValueError, match=message):
        repair_stale_derived_head(
            path,
            world_id=REPAIR_WORLD_ID,
            expected_source_bundle=source_bundle,
            expected_cursor=cursor,
            expected_event_count=event_count,
            expected_latest_event_hash=latest_event_hash,
        )


def test_derived_head_install_cas_refuses_changed_source(tmp_path: Path) -> None:
    path = tmp_path / "cas-conflict.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(path)
    inspection = _inspect_stale_derived_head(
        path,
        world_id=REPAIR_WORLD_ID,
        expected_source_bundle=REPAIR_SOURCE_BUNDLE,
        expected_cursor=cursor,
        expected_event_count=event_count,
        expected_latest_event_hash=latest_event_hash,
    )
    rebuilt = _derive_rebuilt_head(
        path,
        world_id=REPAIR_WORLD_ID,
        inspection=inspection,
    )
    with sqlite3.connect(path) as connection:
        before_items = tuple(
            connection.execute(
                """SELECT field, idx, item_json FROM world_v2_head_state_items
                   WHERE world_id = ? ORDER BY field, idx""",
                (REPAIR_WORLD_ID,),
            )
        )
        connection.execute(
            "UPDATE world_v2_heads SET semantic_hash = ? WHERE world_id = ?",
            ("1" * 64, REPAIR_WORLD_ID),
        )

    with pytest.raises(ConcurrencyConflict, match="changed after repair inspection"):
        _install_rebuilt_head(
            path,
            world_id=REPAIR_WORLD_ID,
            inspection=inspection,
            rebuilt=rebuilt,
        )

    with sqlite3.connect(path) as connection:
        after_items = tuple(
            connection.execute(
                """SELECT field, idx, item_json FROM world_v2_head_state_items
                   WHERE world_id = ? ORDER BY field, idx""",
                (REPAIR_WORLD_ID,),
            )
        )
    assert after_items == before_items


def test_stale_derived_head_repair_applies_atomically_and_preserves_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale-derived-head.sqlite"
    cursor, event_count, latest_event_hash, stale_semantic_hash, _ = _stale_derived_head_database(
        path
    )
    protected_before = _protected_world_v2_rows(path)
    with sqlite3.connect(path) as connection:
        item_count_before = int(
            connection.execute(
                "SELECT COUNT(*) FROM world_v2_head_state_items WHERE world_id = ?",
                (REPAIR_WORLD_ID,),
            ).fetchone()[0]
        )
        mutation_epoch_before = int(
            connection.execute(
                "SELECT mutation_epoch FROM world_v2_ledger_mutation_epochs WHERE world_id = ?",
                (REPAIR_WORLD_ID,),
            ).fetchone()[0]
        )
    connection.close()

    with pytest.raises(ValueError, match="confirm-world-id"):
        repair_stale_derived_head(
            path,
            world_id=REPAIR_WORLD_ID,
            expected_source_bundle=REPAIR_SOURCE_BUNDLE,
            expected_cursor=cursor,
            expected_event_count=event_count,
            expected_latest_event_hash=latest_event_hash,
            apply=True,
        )

    report = repair_stale_derived_head(
        path,
        world_id=REPAIR_WORLD_ID,
        expected_source_bundle=REPAIR_SOURCE_BUNDLE,
        expected_cursor=cursor,
        expected_event_count=event_count,
        expected_latest_event_hash=latest_event_hash,
        apply=True,
        confirm_world_id=REPAIR_WORLD_ID,
    )

    assert report.applied is True
    assert report.action_count == 1
    assert report.pending_action_count == 1
    assert report.rollback_database is not None
    rollback = Path(report.rollback_database)
    assert rollback.is_file()
    with sqlite3.connect(rollback) as connection:
        assert connection.execute(
            """SELECT reducer_bundle_version, semantic_hash
               FROM world_v2_heads WHERE world_id = ?""",
            (REPAIR_WORLD_ID,),
        ).fetchone() == (REPAIR_SOURCE_BUNDLE, stale_semantic_hash)
    assert _protected_world_v2_rows(path) == protected_before

    repaired = SQLiteWorldLedger(path=path, world_id=REPAIR_WORLD_ID)
    try:
        projected = repaired.project()
        assert projected.reducer_bundle_version == REDUCER_BUNDLE_VERSION
        assert (
            ProjectionCursor(
                world_revision=projected.world_revision,
                deliberation_revision=projected.deliberation_revision,
                ledger_sequence=projected.ledger_sequence,
            )
            == cursor
        )
        assert tuple(action.action_id for action in projected.actions) == ("action:repair",)
        assert tuple(action.action_id for action in projected.pending_actions) == ("action:repair",)
        assert repaired.rebuild() == projected
        evidence = repaired.export_replay_evidence()
        assert evidence.projection == evidence.replay
    finally:
        repaired.close()

    # One offline repair transaction deletes the old split rows, installs the
    # replayed rows and CAS-replaces the head exactly once.  A normal startup
    # migration would touch the head several times and silently rebuild the
    # prefix cache; this observable mutation epoch pins the narrower repair
    # boundary.
    with sqlite3.connect(path) as connection:
        item_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM world_v2_head_state_items WHERE world_id = ?",
                (REPAIR_WORLD_ID,),
            ).fetchone()[0]
        )
        mutation_epoch_after = int(
            connection.execute(
                "SELECT mutation_epoch FROM world_v2_ledger_mutation_epochs WHERE world_id = ?",
                (REPAIR_WORLD_ID,),
            ).fetchone()[0]
        )
    assert mutation_epoch_after - mutation_epoch_before == item_count_before + item_count + 1


def test_stale_derived_head_repair_preserves_shared_sidecars_and_immutable_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sidecar-preservation.sqlite"
    cursor, event_count, latest_event_hash, _, _ = _stale_derived_head_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE qq_ingress_sidecar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE NOT NULL,
                payload BLOB NOT NULL
            );
            CREATE TABLE world_v2_test_sidecar (
                world_id TEXT NOT NULL,
                sidecar_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (world_id, sidecar_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO qq_ingress_sidecar (message_id, payload) VALUES (?, ?)",
            ("qq-message:1", b"\x00qq-sidecar\xff"),
        )
        connection.execute(
            "INSERT INTO world_v2_test_sidecar VALUES (?, ?, ?)",
            (REPAIR_WORLD_ID, "sidecar:repair", '{"kept":true}'),
        )
        immutable_before = {
            table: tuple(connection.execute(f'SELECT * FROM "{table}"'))
            for table in (
                "world_v2_events",
                "world_v2_commits",
                "qq_ingress_sidecar",
                "world_v2_test_sidecar",
            )
        }
    connection.close()

    report = repair_stale_derived_head(
        path,
        world_id=REPAIR_WORLD_ID,
        expected_source_bundle=REPAIR_SOURCE_BUNDLE,
        expected_cursor=cursor,
        expected_event_count=event_count,
        expected_latest_event_hash=latest_event_hash,
        apply=True,
        confirm_world_id=REPAIR_WORLD_ID,
    )

    assert report.action_count == 1
    assert report.pending_action_count == 1
    with sqlite3.connect(path) as connection:
        immutable_after = {
            table: tuple(connection.execute(f'SELECT * FROM "{table}"'))
            for table in immutable_before
        }
    assert immutable_after == immutable_before


def test_stale_derived_head_repair_rolls_back_when_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fsync-failure.sqlite"
    cursor, event_count, latest_event_hash, stale_semantic_hash, _ = _stale_derived_head_database(
        path
    )
    source_mode = stat.S_IMODE(path.stat().st_mode)
    real_fsync_directory = ledger_maintenance_module._fsync_directory  # noqa: SLF001
    calls = 0

    def fail_first_directory_fsync(directory: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected directory fsync failure")
        real_fsync_directory(directory)

    monkeypatch.setattr(
        ledger_maintenance_module,
        "_fsync_directory",
        fail_first_directory_fsync,
    )

    with pytest.raises(OSError, match="injected directory fsync failure"):
        repair_stale_derived_head(
            path,
            world_id=REPAIR_WORLD_ID,
            expected_source_bundle=REPAIR_SOURCE_BUNDLE,
            expected_cursor=cursor,
            expected_event_count=event_count,
            expected_latest_event_hash=latest_event_hash,
            apply=True,
            confirm_world_id=REPAIR_WORLD_ID,
        )

    assert calls == 2
    assert stat.S_IMODE(path.stat().st_mode) == source_mode
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """SELECT reducer_bundle_version, semantic_hash
               FROM world_v2_heads WHERE world_id = ?""",
            (REPAIR_WORLD_ID,),
        ).fetchone() == (REPAIR_SOURCE_BUNDLE, stale_semantic_hash)
    connection.close()
    assert not tuple(
        candidate
        for candidate in tmp_path.glob("*.stale-derived-head-staging-*")
        if not candidate.name.endswith(".writer.lock")
    )
    assert not tuple(
        candidate
        for candidate in tmp_path.glob("*.stale-derived-head-rollback-*")
        if not candidate.name.endswith(".writer.lock")
    )
