from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

import pytest

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.ledger import ObservationEventLocator, WorldLedger
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import (
    PinnedObservationHistoryHandle,
    SQLiteProofBackedObservationReader,
    SQLiteWorldLedger,
)


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


def test_sqlite_proof_reader_authenticates_historical_membership_and_absence(tmp_path) -> None:
    path = tmp_path / "proof-reader.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    old = _observation("event:proof:old", "obs:proof:old")
    _commit(ledger, old, 1)
    old_projection = ledger.project()
    old_cursor = ProjectionCursor(
        world_revision=old_projection.world_revision,
        deliberation_revision=old_projection.deliberation_revision,
        ledger_sequence=old_projection.ledger_sequence,
    )
    new = _observation("event:proof:new", "obs:proof:new")
    _commit(ledger, new, 2)

    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    handle = reader.pin(world_id=WORLD, cursor=old_cursor)
    old_locator = ObservationEventLocator(
        observation_id="obs:proof:old",
        event_type="ObservationRecorded",
        idempotency_key=old.idempotency_key,
    )
    new_locator = ObservationEventLocator(
        observation_id="obs:proof:new",
        event_type="ObservationRecorded",
        idempotency_key=new.idempotency_key,
    )

    found = reader.read(
        handle=handle,
        locators=tuple(sorted((old_locator, new_locator), key=lambda item: (item.observation_id, item.event_type, item.idempotency_key))),
    )
    by_observation = {item.locator.observation_id: item.event for item in found}
    status_by_observation = {item.locator.observation_id: item.status for item in found}

    assert by_observation["obs:proof:old"] is not None
    assert by_observation["obs:proof:old"].event == old
    assert status_by_observation["obs:proof:old"] == "found"
    assert by_observation["obs:proof:new"] is None
    assert status_by_observation["obs:proof:new"] == "locator_missing"
    ledger.close()


def test_sqlite_proof_reader_handle_cannot_be_mutated_or_forged(tmp_path) -> None:
    path = tmp_path / "proof-reader-handle.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    event = _observation("event:proof:handle", "obs:proof:handle")
    _commit(ledger, event, 1)
    projection = ledger.project()
    cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    handle = reader.pin(world_id=WORLD, cursor=cursor)
    locator = ObservationEventLocator(
        observation_id="obs:proof:handle",
        event_type="ObservationRecorded",
        idempotency_key=event.idempotency_key,
    )

    with pytest.raises(AttributeError):
        handle.cursor = ProjectionCursor(  # type: ignore[misc]
            world_revision=0, deliberation_revision=0, ledger_sequence=0
        )
    assert not hasattr(handle, "cursor")
    assert not hasattr(handle, "checkpoint")
    assert not hasattr(handle, "anchor_mmr_root")
    assert reader.read(handle=handle, locators=(locator,))[0].event is not None

    forged = PinnedObservationHistoryHandle()
    with pytest.raises(ValueError, match="not owned"):
        reader.read(handle=forged, locators=(locator,))

    other_reader = SQLiteProofBackedObservationReader(ledger=ledger)
    with pytest.raises(ValueError, match="not owned"):
        other_reader.read(handle=handle, locators=(locator,))
    ledger.close()


def test_sqlite_proof_reader_rejects_tampered_historical_sibling_path(tmp_path) -> None:
    path = tmp_path / "proof-reader-tampered.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    old = _observation("event:proof:path", "obs:proof:path")
    _commit(ledger, old, 1)
    # A second key makes one historical sibling non-empty; changing the
    # journal then has a cryptographically observable effect on the first key.
    _commit(ledger, _observation("event:proof:path:peer", "obs:proof:path:peer"), 2)
    projection = ledger.project()
    cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    handle = reader.pin(world_id=WORLD, cursor=cursor)
    locator = ObservationEventLocator(
        observation_id="obs:proof:path",
        event_type="ObservationRecorded",
        idempotency_key=old.idempotency_key,
    )
    # Any changed sibling on the one historical path makes the root check fail.
    ledger._connection.execute(
        """UPDATE world_v2_prefix_locator_node_history
           SET node_hash = zeroblob(32)
           WHERE world_id = ? AND ledger_sequence = ?""",
        (WORLD, cursor.ledger_sequence),
    )
    with pytest.raises(ValueError, match="SMT proof does not verify"):
        reader.read(handle=handle, locators=(locator,))
    ledger.close()


def test_sqlite_proof_reader_uses_addressed_mmr_reads_not_a_full_node_scan(tmp_path) -> None:
    path = tmp_path / "proof-reader-mmr-point-reads.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    first: WorldEvent | None = None
    for number in range(16):
        event = _observation(
            f"event:proof:many:{number}", f"obs:proof:many:{number}"
        )
        _commit(ledger, event, number + 1)
        if first is None:
            first = event
    assert first is not None
    projection = ledger.project()
    cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    statements: list[str] = []
    ledger._connection.set_trace_callback(statements.append)
    try:
        handle = reader.pin(world_id=WORLD, cursor=cursor)
        found = reader.read(
            handle=handle,
            locators=(
                ObservationEventLocator(
                    observation_id="obs:proof:many:0",
                    event_type="ObservationRecorded",
                    idempotency_key=first.idempotency_key,
                ),
            ),
        )
    finally:
        ledger._connection.set_trace_callback(None)
    assert found[0].event is not None

    mmr_selects = tuple(
        statement.upper()
        for statement in statements
        if "WORLD_V2_PREFIX_MMR_NODES" in statement.upper()
        and statement.lstrip().upper().startswith("SELECT")
    )
    # The point lookup is keyed by the table's full primary-key address.  A
    # reader never asks SQLite for all nodes and reconstructs an MMR in memory.
    assert mmr_selects
    assert all("HEIGHT =" in statement and "NODE_INDEX =" in statement for statement in mmr_selects)
    # At 32 leaves, root validation plus two inclusion proofs remain bounded
    # by a few logarithmic paths (and far below the 63 stored MMR nodes).
    assert len(mmr_selects) <= 4 * 6
    history_reads = tuple(
        statement.upper()
        for statement in statements
        if "WORLD_V2_PREFIX_LOCATOR_NODE_HISTORY" in statement.upper()
        and statement.lstrip().upper().startswith("WITH REQUESTED")
    )
    # One locator's 256 sibling addresses are fetched as a single bounded
    # query, rather than issuing 256 SQLite round-trips.
    assert len(history_reads) == 1
    ledger.close()


def test_sqlite_cold_verification_rejects_untouched_event_tampering(tmp_path) -> None:
    path = tmp_path / "cold-verified-reader.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    old = _observation("event:proof:cold:old", "obs:proof:cold:old")
    other = _observation("event:proof:cold:other", "obs:proof:cold:other")
    _commit(ledger, old, 1)
    _commit(ledger, other, 2)
    ledger.close()

    # The proof cache for ``old`` could still be internally consistent if a
    # different immutable event is edited.  Startup must reject that ledger
    # before any proof-backed reader can pin an apparently valid old cursor.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_events SET event_json = '{}' WHERE world_id = ? AND event_id = ?",
            (WORLD, other.event_id),
        )
    with pytest.raises(LedgerIntegrityError):
        SQLiteWorldLedger(path=path, world_id=WORLD)


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


def test_legacy_prefix_backfill_rejects_an_orphan_commit(tmp_path) -> None:
    path = tmp_path / "orphan-prefix.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _commit(ledger, _observation("event:orphan-source", "obs:orphan-source"), 1)
    ledger.close()
    with sqlite3.connect(path) as connection:
        request_hash, result_json = connection.execute(
            "SELECT request_hash, result_json FROM world_v2_commits WHERE world_id = ?",
            (WORLD,),
        ).fetchone()
        connection.execute(
            "INSERT INTO world_v2_commits (world_id, commit_id, request_hash, result_json) VALUES (?, ?, ?, ?)",
            (WORLD, "commit:orphan", request_hash, result_json),
        )
        for table in (
            "world_v2_prefix_mmr_nodes",
            "world_v2_prefix_locator_nodes",
            "world_v2_prefix_locator_values",
            "world_v2_prefix_checkpoints",
            "world_v2_prefix_heads",
        ):
            connection.execute(f"DELETE FROM {table}")
    with pytest.raises(LedgerIntegrityError, match="empty or orphaned commit"):
        SQLiteWorldLedger(path=path, world_id=WORLD)
