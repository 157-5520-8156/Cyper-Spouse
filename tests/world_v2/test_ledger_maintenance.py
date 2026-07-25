from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from companion_daemon.process_lock import AlreadyRunningError, SingleInstanceLock
from companion_daemon.qq_outbound_owner import qq_outbound_owner_lock_path
from companion_daemon.world_v2.ledger_maintenance import (
    LEGACY_WORLD_ID,
    _v2_fingerprint,
    compact_retired_v1,
)


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
        connection.execute(
            "INSERT INTO world_v2_heads VALUES ('world:v2',7,9,11,'v2-state')"
        )
        connection.execute(
            "INSERT INTO world_v2_events VALUES ('world:v2',11,'v2-event-hash')"
        )
        connection.execute(
            "INSERT INTO world_v2_ledger_mutation_epochs VALUES ('world:v2',3)"
        )


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
        assert connection.execute(
            "SELECT event_hash FROM world_v2_events"
        ).fetchone()[0] == "v2-event-hash"


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
        connection.execute(
            "UPDATE world_v2_ledger_mutation_epochs SET mutation_epoch=4"
        )
        after = _v2_fingerprint(connection)

    assert after != before
