from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

import pytest

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:sqlite-prefix"


def _observation(event_id: str, observation_id: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:prefix",
        causation_id="cause:prefix",
        correlation_id="correlation:prefix",
        idempotency_key=event_id,
        payload={"observation_id": observation_id},
    )


def _commit(ledger: SQLiteWorldLedger | WorldLedger, event: WorldEvent, number: int) -> None:
    projection = ledger.project()
    ledger.commit(
        [event],
        commit_id=f"commit:prefix:{number}",
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def test_sqlite_prefix_state_is_incremental_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "prefix.sqlite3"
    sqlite_ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    memory_ledger = WorldLedger(world_id=WORLD)
    for number in (1, 2):
        event = _observation(f"event:prefix:{number}", f"obs:prefix:{number}")
        _commit(sqlite_ledger, event, number)
        _commit(memory_ledger, event, number)

    assert sqlite_ledger._prefix_mmr.root == memory_ledger._prefix_mmr.root
    assert sqlite_ledger._prefix_locator_map.root == memory_ledger._prefix_locator_map.root
    with sqlite3.connect(path) as connection:
        head = connection.execute(
            "SELECT mmr_leaf_count, checkpoint_count FROM world_v2_prefix_heads WHERE world_id = ?",
            (WORLD,),
        ).fetchone()
        assert head == (4, 2)  # event + checkpoint leaf for each commit
        assert connection.execute(
            "SELECT COUNT(*) FROM world_v2_prefix_locator_values WHERE world_id = ?", (WORLD,)
        ).fetchone()[0] == 2
    sqlite_ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened._prefix_mmr.root == memory_ledger._prefix_mmr.root
    assert reopened._prefix_locator_map.root == memory_ledger._prefix_locator_map.root
    reopened.close()


def test_sqlite_legacy_prefix_migration_rebuilds_only_when_all_v1_rows_absent(tmp_path) -> None:
    path = tmp_path / "legacy-prefix.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _commit(ledger, _observation("event:legacy", "obs:legacy"), 1)
    expected_root = ledger._prefix_mmr.root
    ledger.close()

    with sqlite3.connect(path) as connection:
        for table in (
            "world_v2_prefix_mmr_nodes",
            "world_v2_prefix_locator_nodes",
            "world_v2_prefix_locator_values",
            "world_v2_prefix_checkpoints",
            "world_v2_prefix_heads",
        ):
            connection.execute(f"DELETE FROM {table}")

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert migrated._prefix_mmr.root == expected_root
    migrated.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM world_v2_prefix_checkpoints WHERE world_id = ?", (WORLD,)
        )
    with pytest.raises(LedgerIntegrityError, match="checkpoint count"):
        SQLiteWorldLedger(path=path, world_id=WORLD)


def test_sqlite_prefix_state_fails_closed_when_persisted_peak_is_tampered(tmp_path) -> None:
    path = tmp_path / "tampered-prefix.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _commit(ledger, _observation("event:tampered", "obs:tampered"), 1)
    ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            """UPDATE world_v2_prefix_mmr_nodes SET node_hash = zeroblob(32)
               WHERE world_id = ? AND height = 1 AND node_index = 0""",
            (WORLD,),
        )
    with pytest.raises(LedgerIntegrityError, match="MMR root"):
        SQLiteWorldLedger(path=path, world_id=WORLD)
