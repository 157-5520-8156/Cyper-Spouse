"""Offline, fail-closed compaction for the retired World-v1 authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from companion_daemon.process_lock import SingleInstanceLock
from companion_daemon.qq_outbound_owner import qq_outbound_owner_lock_path


LEGACY_WORLD_ID = "zhizhi-world-v1"


@dataclass(frozen=True, slots=True)
class LedgerCompactionReport:
    database: str
    applied: bool
    legacy_world_id: str
    snapshot_rows: int
    event_rows: int
    bytes_before: int
    bytes_after: int
    integrity: str
    v2_fingerprint: tuple[tuple[Any, ...], ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _integrity(connection: sqlite3.Connection) -> str:
    rows = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
    return "ok" if rows == ("ok",) else "; ".join(rows)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _legacy_counts(
    connection: sqlite3.Connection, *, legacy_world_id: str
) -> tuple[int, int]:
    snapshots = (
        int(
            connection.execute(
                "SELECT COUNT(*) FROM world_snapshots WHERE world_id=?",
                (legacy_world_id,),
            ).fetchone()[0]
        )
        if _table_exists(connection, "world_snapshots")
        else 0
    )
    events = (
        int(
            connection.execute(
                "SELECT COUNT(*) FROM world_events WHERE world_id=?",
                (legacy_world_id,),
            ).fetchone()[0]
        )
        if _table_exists(connection, "world_events")
        else 0
    )
    return snapshots, events


def _v2_fingerprint(connection: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
    if not _table_exists(connection, "world_v2_heads"):
        return ()
    core_tables = tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
                WHERE type='table' AND name GLOB 'world_v2_*'
                ORDER BY name"""
        )
    )

    def table_digest(table: str, world_id: str) -> tuple[str, int, str]:
        if not _table_exists(connection, table):
            return table, 0, ""
        columns = tuple(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        )
        if "world_id" not in columns:
            return table, 0, ""
        digest = hashlib.sha256()
        count = 0
        order = ", ".join(f'"{column}"' for column in columns)
        for row in connection.execute(
            f'SELECT * FROM "{table}" WHERE world_id=? ORDER BY {order}',
            (world_id,),
        ):
            digest.update(
                json.dumps(
                    tuple(row),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=lambda value: bytes(value).hex(),
                ).encode("utf-8")
            )
            digest.update(b"\n")
            count += 1
        return table, count, digest.hexdigest()

    return tuple(
        (
            *tuple(row),
            tuple(table_digest(table, str(row[0])) for table in core_tables),
        )
        for row in connection.execute(
            """SELECT h.world_id, h.world_revision, h.deliberation_revision,
                      h.ledger_sequence, h.state_hash,
                      (SELECT COUNT(*) FROM world_v2_events e
                        WHERE e.world_id=h.world_id),
                      (SELECT event_hash FROM world_v2_events e
                        WHERE e.world_id=h.world_id
                        ORDER BY e.ledger_sequence DESC LIMIT 1)
                 FROM world_v2_heads h ORDER BY h.world_id"""
        )
    )


def compact_retired_v1(
    database: Path,
    *,
    apply: bool = False,
    confirm_world_id: str | None = None,
    legacy_world_id: str = LEGACY_WORLD_ID,
) -> LedgerCompactionReport:
    """Remove only retired V1 rows and atomically replace with a vacuumed DB."""

    path = database.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if apply and confirm_world_id != legacy_world_id:
        raise ValueError(f"--apply requires --confirm-world-id {legacy_world_id}")

    bytes_before = path.stat().st_size
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source:
        integrity = _integrity(source)
        if integrity != "ok":
            raise RuntimeError(f"source database integrity failed: {integrity}")
        snapshot_rows, event_rows = _legacy_counts(
            source, legacy_world_id=legacy_world_id
        )
        fingerprint = _v2_fingerprint(source)
    if not apply:
        return LedgerCompactionReport(
            database=str(path),
            applied=False,
            legacy_world_id=legacy_world_id,
            snapshot_rows=snapshot_rows,
            event_rows=event_rows,
            bytes_before=bytes_before,
            bytes_after=bytes_before,
            integrity=integrity,
            v2_fingerprint=fingerprint,
        )

    staging = path.with_name(path.name + f".v1-prune-staging-{os.getpid()}")
    compacted = path.with_name(path.name + f".v1-prune-compacted-{os.getpid()}")
    rollback = path.with_name(path.name + f".v1-prune-rollback-{os.getpid()}")
    offline_lock = SingleInstanceLock(qq_outbound_owner_lock_path(path))
    offline_lock.__enter__()
    try:
        for temporary in (staging, compacted, rollback):
            if temporary.exists():
                raise FileExistsError(temporary)
        with sqlite3.connect(path) as source, sqlite3.connect(staging) as target:
            source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            source.backup(target)
        with sqlite3.connect(staging) as work:
            work.execute("PRAGMA foreign_keys=ON")
            work.execute("BEGIN IMMEDIATE")
            if _table_exists(work, "world_snapshots"):
                work.execute(
                    "DELETE FROM world_snapshots WHERE world_id=?", (legacy_world_id,)
                )
            if _table_exists(work, "world_events"):
                work.execute("DELETE FROM world_events WHERE world_id=?", (legacy_world_id,))
            work.commit()
            work.execute("VACUUM INTO ?", (str(compacted),))
        with sqlite3.connect(f"file:{compacted}?mode=ro", uri=True) as candidate:
            candidate_integrity = _integrity(candidate)
            candidate_counts = _legacy_counts(
                candidate, legacy_world_id=legacy_world_id
            )
            candidate_fingerprint = _v2_fingerprint(candidate)
        if candidate_integrity != "ok":
            raise RuntimeError(f"compacted database integrity failed: {candidate_integrity}")
        if candidate_counts != (0, 0):
            raise RuntimeError("compacted database still contains retired V1 rows")
        if candidate_fingerprint != fingerprint:
            raise RuntimeError("World V2 fingerprint changed during V1 compaction")

        os.replace(path, rollback)
        try:
            os.replace(compacted, path)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as installed:
                if _integrity(installed) != "ok" or _v2_fingerprint(installed) != fingerprint:
                    raise RuntimeError("installed compacted database failed verification")
        except Exception:
            if path.exists():
                path.unlink()
            os.replace(rollback, path)
            raise
        rollback.unlink()
        return LedgerCompactionReport(
            database=str(path),
            applied=True,
            legacy_world_id=legacy_world_id,
            snapshot_rows=snapshot_rows,
            event_rows=event_rows,
            bytes_before=bytes_before,
            bytes_after=path.stat().st_size,
            integrity="ok",
            v2_fingerprint=fingerprint,
        )
    finally:
        staging.unlink(missing_ok=True)
        compacted.unlink(missing_ok=True)
        offline_lock.__exit__(None, None, None)


__all__ = ["LEGACY_WORLD_ID", "LedgerCompactionReport", "compact_retired_v1"]
