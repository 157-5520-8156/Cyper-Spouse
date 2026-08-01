"""Offline, fail-closed maintenance for retired and derived ledger state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from typing import Any

from companion_daemon.process_lock import AlreadyRunningError, SingleInstanceLock
from companion_daemon.qq_outbound_owner import qq_outbound_owner_lock_path
from companion_daemon.world_v2.errors import ConcurrencyConflict, LedgerIntegrityError
from companion_daemon.world_v2.reducers import REDUCER_BUNDLE_VERSION, ReducerState
from companion_daemon.world_v2.schemas import ProjectionCursor
from companion_daemon.world_v2.sqlite_coordination import sqlite_write_lock
from companion_daemon.world_v2.sqlite_ledger import (
    SQLiteWorldLedger,
    _assemble_state_json_from_fragments,
    _head_state_fragments_from_item_rows,
)


LEGACY_WORLD_ID = "zhizhi-world-v1"
STALE_DERIVED_HEAD_SOURCE_BUNDLE = "world-v2-reducers.41"
_HEAD_STATE_SENTINEL = "world-v2-head-state-items.1"
_REPAIR_DERIVED_TABLES = frozenset(
    {
        "world_v2_heads",
        "world_v2_head_state_items",
        "world_v2_ledger_mutation_epochs",
    }
)


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


@dataclass(frozen=True, slots=True)
class DerivedHeadRepairReport:
    database: str
    applied: bool
    repairable: bool
    world_id: str
    source_bundle: str
    target_bundle: str
    cursor: ProjectionCursor
    event_count: int
    latest_event_hash: str
    old_semantic_hash: str
    new_semantic_hash: str
    rollback_database: str | None
    integrity: str
    protected_fingerprint: str
    action_count: int
    pending_action_count: int
    action_fingerprint: str

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["cursor"] = self.cursor.model_dump(mode="json")
        return value


@dataclass(frozen=True, slots=True)
class _StaleHeadInspection:
    cursor: ProjectionCursor
    source_bundle: str
    state_json: str
    state_hash: str
    storage_epoch: int
    semantic_hash: str
    repaired_legacy_semantic_hash: str
    mutation_epoch: int
    head_state_items_fingerprint: str
    event_count: int
    latest_event_hash: str
    action_count: int
    pending_action_count: int
    action_fingerprint: str


@dataclass(frozen=True, slots=True)
class _RebuiltDerivedHead:
    cursor: ProjectionCursor
    state_json: str
    semantic_hash: str
    reducer_bundle_version: str
    state_hash: str
    state_items: tuple[tuple[str, int, str], ...]
    action_count: int
    pending_action_count: int
    action_fingerprint: str


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


def _legacy_counts(connection: sqlite3.Connection, *, legacy_world_id: str) -> tuple[int, int]:
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
        columns = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
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


def _world_table_digest(
    connection: sqlite3.Connection,
    *,
    table: str,
    world_id: str,
) -> tuple[str, int, str]:
    columns = tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))
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


def _repair_protected_fingerprint(
    connection: sqlite3.Connection,
    *,
    world_id: str,
) -> tuple[tuple[str, int, str], ...]:
    tables = tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name GLOB 'world_v2_*'
               ORDER BY name"""
        )
        if str(row[0]) not in _REPAIR_DERIVED_TABLES
    )
    return tuple(
        _world_table_digest(connection, table=table, world_id=world_id) for table in tables
    )


def _fingerprint_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _head_state_json(connection: sqlite3.Connection, *, world_id: str) -> str:
    row = connection.execute(
        "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (world_id,)
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise LedgerIntegrityError("world head state is unavailable")
    if row[0] != _HEAD_STATE_SENTINEL:
        return str(row[0])
    state_rows = tuple(
        (
            str(item[0]),
            int(item[1]),
            str(item[2]),
        )
        for item in connection.execute(
            """SELECT field, idx, item_json FROM world_v2_head_state_items
               WHERE world_id = ? ORDER BY field, idx""",
            (world_id,),
        )
    )
    fragments, _ = _head_state_fragments_from_item_rows(state_rows)
    if not fragments:
        raise LedgerIntegrityError("split world head state is empty")
    return _assemble_state_json_from_fragments(fragments)


def _top_level_json_fragments(
    document: str,
    *,
    fields: frozenset[str],
) -> dict[str, str]:
    decoder = json.JSONDecoder()
    length = len(document)

    def skip_space(index: int) -> int:
        while index < length and document[index].isspace():
            index += 1
        return index

    index = skip_space(0)
    if index >= length or document[index] != "{":
        raise LedgerIntegrityError("world head state is not a JSON object")
    index = skip_space(index + 1)
    found: dict[str, str] = {}
    seen: set[str] = set()
    while index < length and document[index] != "}":
        key, key_end = decoder.raw_decode(document, index)
        if not isinstance(key, str) or key in seen:
            raise LedgerIntegrityError("world head state has an invalid or duplicate field")
        seen.add(key)
        index = skip_space(key_end)
        if index >= length or document[index] != ":":
            raise LedgerIntegrityError("world head state field has no value")
        value_start = skip_space(index + 1)
        _, value_end = decoder.raw_decode(document, value_start)
        if key in fields:
            found[key] = document[value_start:value_end]
        index = skip_space(value_end)
        if index < length and document[index] == ",":
            index = skip_space(index + 1)
            continue
        if index >= length or document[index] != "}":
            raise LedgerIntegrityError("world head state object is not terminated")
    if index >= length or document[index] != "}" or skip_space(index + 1) != length:
        raise LedgerIntegrityError("world head state has trailing bytes")
    return found


def _action_fingerprint(
    connection: sqlite3.Connection,
    *,
    world_id: str,
) -> tuple[int, int, str]:
    try:
        head = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?",
            (world_id,),
        ).fetchone()
        if head is None or not isinstance(head[0], str):
            raise LedgerIntegrityError("world head action state is unavailable")
        if head[0] == _HEAD_STATE_SENTINEL:
            state_rows = tuple(
                (str(row[0]), int(row[1]), str(row[2]))
                for row in connection.execute(
                    """SELECT field, idx, item_json FROM world_v2_head_state_items
                       WHERE world_id = ? ORDER BY field, idx""",
                    (world_id,),
                )
            )
            fragments, _ = _head_state_fragments_from_item_rows(state_rows)
            actions_json = fragments.get("actions")
            pending_actions_json = fragments.get("pending_actions")
            if actions_json is None or pending_actions_json is None:
                raise LedgerIntegrityError("world head Action fragments are unavailable")
        else:
            fragments = _top_level_json_fragments(
                head[0],
                fields=frozenset({"actions", "pending_actions"}),
            )
            actions_json = fragments.get("actions")
            pending_actions_json = fragments.get("pending_actions")
            if actions_json is None or pending_actions_json is None:
                raise LedgerIntegrityError("legacy head Action fragments are unavailable")
        actions = json.loads(actions_json)
        pending_actions = json.loads(pending_actions_json)
    except LedgerIntegrityError:
        raise
    except Exception as exc:
        raise LedgerIntegrityError("world head action state is invalid") from exc
    if not isinstance(actions, list) or not isinstance(pending_actions, list):
        raise LedgerIntegrityError("world head action state is invalid")
    digest = hashlib.sha256()
    digest.update(b"actions\x00")
    digest.update(actions_json.encode("utf-8"))
    digest.update(b"\x00pending_actions\x00")
    digest.update(pending_actions_json.encode("utf-8"))
    return len(actions), len(pending_actions), digest.hexdigest()


def _encoded_sqlite_row(row: tuple[object, ...]) -> bytes:
    def encode(value: object) -> object:
        if isinstance(value, bytes):
            return ["blob", value.hex()]
        if value is None:
            return ["null", None]
        if isinstance(value, bool):
            return ["integer", int(value)]
        if isinstance(value, int):
            return ["integer", value]
        if isinstance(value, float):
            return ["real", repr(value)]
        if isinstance(value, str):
            return ["text", value]
        if isinstance(value, (list, tuple)):
            return ["sequence", [encode(item) for item in value]]
        raise LedgerIntegrityError(
            f"unsupported SQLite value in repair fingerprint: {type(value).__name__}"
        )

    return json.dumps(
        tuple(encode(value) for value in row),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _database_content_fingerprint(
    connection: sqlite3.Connection,
    *,
    world_id: str | None = None,
    protect_repair_target: bool = False,
) -> str:
    """Hash all schema and row content, optionally excluding the repair target.

    Only the selected world's derived head, split-state rows, and mutation
    counter may differ after a repair.  Every other row—including shared QQ,
    Action sidecar, and non-``world_v2_*`` tables—is covered here.  Row hashes
    are sorted in memory so physical row order and SQLite backup page layout do
    not affect the comparison.
    """

    digest = hashlib.sha256()
    schema_rows = tuple(
        tuple(row)
        for row in connection.execute(
            """SELECT type, name, tbl_name, COALESCE(sql, '')
               FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%' OR name = 'sqlite_sequence'
               ORDER BY type, name"""
        )
    )
    digest.update(_encoded_sqlite_row(("schema", json.dumps(schema_rows))))
    tables = tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type = 'table'
                 AND (name NOT LIKE 'sqlite_%' OR name = 'sqlite_sequence')
               ORDER BY name"""
        )
    )
    for table in tables:
        columns = tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))
        if not columns:
            raise LedgerIntegrityError(f"database table {table!r} has no readable columns")
        sql = f'SELECT * FROM "{table}"'
        parameters: tuple[object, ...] = ()
        if (
            protect_repair_target
            and world_id is not None
            and table in _REPAIR_DERIVED_TABLES
            and "world_id" in columns
        ):
            sql += ' WHERE "world_id" <> ? OR "world_id" IS NULL'
            parameters = (world_id,)
        row_hashes = sorted(
            hashlib.sha256(_encoded_sqlite_row(tuple(row))).digest()
            for row in connection.execute(sql, parameters)
        )
        digest.update(_encoded_sqlite_row(("table", table, columns, len(row_hashes))))
        for row_hash in row_hashes:
            digest.update(row_hash)
    return digest.hexdigest()


def _checked_connection_fingerprints(
    connection: sqlite3.Connection,
    *,
    world_id: str,
) -> tuple[str, str, tuple[int, int, str]]:
    integrity = _integrity(connection)
    if integrity != "ok":
        raise LedgerIntegrityError(f"database integrity failed: {integrity}")
    if tuple(connection.execute("PRAGMA foreign_key_check")):
        raise LedgerIntegrityError("database foreign-key integrity failed")
    return (
        _database_content_fingerprint(connection),
        _database_content_fingerprint(
            connection,
            world_id=world_id,
            protect_repair_target=True,
        ),
        _action_fingerprint(connection, world_id=world_id),
    )


def _checked_database_fingerprints(
    database: Path,
    *,
    world_id: str,
) -> tuple[str, str, tuple[int, int, str]]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        return _checked_connection_fingerprints(connection, world_id=world_id)


def _canonical_legacy_state_hash(
    *,
    canonical_state: str,
    cursor: ProjectionCursor,
    reducer_bundle_version: str,
    world_id: str,
) -> str:
    cursor_json = json.dumps(
        cursor.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    material = (
        '{"cursor":'
        + cursor_json
        + ',"reducer_bundle_version":'
        + json.dumps(reducer_bundle_version)
        + ',"state":'
        + canonical_state
        + ',"world_id":'
        + json.dumps(world_id, ensure_ascii=False)
        + "}"
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _head_state_items_fingerprint(
    connection: sqlite3.Connection,
    *,
    world_id: str,
) -> str:
    rows = tuple(
        (str(row[0]), int(row[1]), str(row[2]))
        for row in connection.execute(
            """SELECT field, idx, item_json FROM world_v2_head_state_items
               WHERE world_id = ? ORDER BY field, idx""",
            (world_id,),
        )
    )
    return _fingerprint_digest(rows)


def _inspect_stale_derived_head(
    database: Path,
    *,
    world_id: str,
    expected_source_bundle: str,
    expected_cursor: ProjectionCursor,
    expected_event_count: int,
    expected_latest_event_hash: str,
) -> _StaleHeadInspection:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        integrity = _integrity(connection)
        if integrity != "ok":
            raise LedgerIntegrityError(f"database integrity failed: {integrity}")
        foreign_key_rows = tuple(connection.execute("PRAGMA foreign_key_check"))
        if foreign_key_rows:
            raise LedgerIntegrityError("database foreign-key integrity failed")
        head = connection.execute(
            "SELECT * FROM world_v2_heads WHERE world_id = ?", (world_id,)
        ).fetchone()
        if head is None:
            raise ValueError(f"world head {world_id!r} does not exist")
        source_bundle = str(head["reducer_bundle_version"])
        if source_bundle != expected_source_bundle:
            raise ValueError(
                f"expected source bundle {expected_source_bundle!r}, found {source_bundle!r}"
            )
        if source_bundle != STALE_DERIVED_HEAD_SOURCE_BUNDLE:
            raise ValueError("stale-derived-head repair is restricted to world-v2-reducers.41")
        actual_cursor = ProjectionCursor(
            world_revision=int(head["world_revision"]),
            deliberation_revision=int(head["deliberation_revision"]),
            ledger_sequence=int(head["ledger_sequence"]),
        )
        if actual_cursor != expected_cursor:
            raise ValueError(
                f"expected cursor {expected_cursor.model_dump()}, "
                f"found {actual_cursor.model_dump()}"
            )
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM world_v2_events WHERE world_id = ?",
                (world_id,),
            ).fetchone()[0]
        )
        if event_count != expected_event_count:
            raise ValueError(f"expected event count {expected_event_count}, found {event_count}")
        latest = connection.execute(
            """SELECT event_hash FROM world_v2_events
               WHERE world_id = ? ORDER BY ledger_sequence DESC LIMIT 1""",
            (world_id,),
        ).fetchone()
        latest_event_hash = "" if latest is None else str(latest["event_hash"])
        if latest_event_hash != expected_latest_event_hash:
            raise ValueError("expected latest event hash does not match the immutable ledger")
        try:
            raw_state = _head_state_json(connection, world_id=world_id)
            canonical_state = json.dumps(
                json.loads(raw_state),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            state = ReducerState.model_validate_json(
                canonical_state,
                context={"source_reducer_bundle": source_bundle},
            )
        except Exception as exc:
            raise LedgerIntegrityError("legacy head state is invalid") from exc
        expected_state_hash = _canonical_legacy_state_hash(
            canonical_state=canonical_state,
            cursor=actual_cursor,
            reducer_bundle_version=source_bundle,
            world_id=world_id,
        )
        if not hmac.compare_digest(expected_state_hash, str(head["state_hash"])):
            raise LedgerIntegrityError("legacy head state hash is invalid")
        expected_semantic_hash = hashlib.sha256(
            json.dumps(
                state.semantic_payload(
                    world_id=world_id,
                    world_revision=actual_cursor.world_revision,
                    reducer_bundle_version=source_bundle,
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        old_semantic_hash = str(head["semantic_hash"])
        if hmac.compare_digest(expected_semantic_hash, old_semantic_hash):
            raise ValueError("world head semantic hash is healthy; repair refused")
        try:
            storage_epoch = int(head["storage_epoch"])
            mutation_row = connection.execute(
                """SELECT mutation_epoch FROM world_v2_ledger_mutation_epochs
                   WHERE world_id = ?""",
                (world_id,),
            ).fetchone()
            if mutation_row is None:
                raise ValueError("ledger mutation epoch is unavailable")
            mutation_epoch = int(mutation_row[0])
        except Exception as exc:
            raise LedgerIntegrityError("world head CAS identity is invalid") from exc
        action_count, pending_action_count, action_fingerprint = _action_fingerprint(
            connection,
            world_id=world_id,
        )
        return _StaleHeadInspection(
            cursor=actual_cursor,
            source_bundle=source_bundle,
            state_json=str(head["state_json"]),
            state_hash=expected_state_hash,
            storage_epoch=storage_epoch,
            semantic_hash=old_semantic_hash,
            repaired_legacy_semantic_hash=expected_semantic_hash,
            mutation_epoch=mutation_epoch,
            head_state_items_fingerprint=_head_state_items_fingerprint(
                connection,
                world_id=world_id,
            ),
            event_count=event_count,
            latest_event_hash=latest_event_hash,
            action_count=action_count,
            pending_action_count=pending_action_count,
            action_fingerprint=action_fingerprint,
        )


def _authorize_validation_copy(
    database: Path,
    *,
    world_id: str,
    inspection: _StaleHeadInspection,
) -> None:
    """Repair only the copied legacy hash so ordinary migration can validate it."""

    with sqlite3.connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            updated = connection.execute(
                """UPDATE world_v2_heads SET semantic_hash = ?
                   WHERE world_id = ? AND world_revision = ?
                     AND deliberation_revision = ? AND ledger_sequence = ?
                     AND reducer_bundle_version = ? AND state_json = ?
                     AND state_hash = ? AND semantic_hash = ? AND storage_epoch = ?
                     AND (SELECT mutation_epoch FROM world_v2_ledger_mutation_epochs
                          WHERE world_id = ?) = ?""",
                (
                    inspection.repaired_legacy_semantic_hash,
                    world_id,
                    inspection.cursor.world_revision,
                    inspection.cursor.deliberation_revision,
                    inspection.cursor.ledger_sequence,
                    inspection.source_bundle,
                    inspection.state_json,
                    inspection.state_hash,
                    inspection.semantic_hash,
                    inspection.storage_epoch,
                    world_id,
                    inspection.mutation_epoch,
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict(
                    "validation-copy head changed after stale-head inspection"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _backup_database(source_path: Path, target_path: Path) -> None:
    if target_path.exists():
        raise FileExistsError(target_path)
    source_uri = f"file:{source_path}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
    os.chmod(target_path, 0o600)


def _database_paths_with_open_handles(database: Path) -> tuple[Path, ...]:
    candidates = (
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    )
    return tuple(path for path in candidates if path.exists())


def _lsof_database_open_pids(database: Path) -> frozenset[int] | None:
    executable = shutil.which("lsof")
    if executable is None:
        return None
    paths = _database_paths_with_open_handles(database)
    if not paths:
        return frozenset()
    try:
        result = subprocess.run(
            (executable, "-nP", "-t", "--", *(str(path) for path in paths)),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not prove the SQLite database is offline with lsof") from exc
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            "could not prove the SQLite database is offline with lsof: " + result.stderr.strip()
        )
    try:
        return frozenset(int(line.strip()) for line in result.stdout.splitlines() if line.strip())
    except ValueError as exc:
        raise RuntimeError("lsof returned an invalid process identifier") from exc


def _proc_database_open_pids(database: Path) -> frozenset[int] | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    targets: set[tuple[int, int]] = set()
    for path in _database_paths_with_open_handles(database):
        try:
            status = path.stat()
        except OSError:
            continue
        targets.add((status.st_dev, status.st_ino))
    if not targets:
        return frozenset()
    found: set[int] = set()
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            descriptors = tuple((process / "fd").iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for descriptor in descriptors:
            try:
                status = descriptor.stat()
            except (FileNotFoundError, PermissionError):
                continue
            if (status.st_dev, status.st_ino) in targets:
                found.add(int(process.name))
                break
    return frozenset(found)


def _database_open_pids(database: Path) -> frozenset[int]:
    pids = _lsof_database_open_pids(database)
    if pids is None:
        pids = _proc_database_open_pids(database)
    if pids is None:
        raise RuntimeError(
            "offline SQLite verification requires lsof or a readable /proc filesystem"
        )
    return pids


def _assert_database_offline(
    database: Path,
    *,
    allowed_pids: frozenset[int] = frozenset(),
) -> None:
    active = _database_open_pids(database).difference(allowed_pids)
    if active:
        rendered = ",".join(str(pid) for pid in sorted(active))
        raise AlreadyRunningError(f"SQLite database has active process connection(s): {rendered}")


def _checkpoint_seal_and_backup_database(
    source_path: Path,
    *target_paths: Path,
    world_id: str,
) -> tuple[int, tuple[str, str, tuple[int, int, str]]]:
    """Prove offline state, permission-seal the source, and make exact backups.

    The source stays mode ``000`` after success.  This prevents a daemon from
    opening a new SQLite connection during the potentially long cold replay.
    The caller either atomically replaces it or restores ``source_mode``.
    """

    if any(target.exists() for target in target_paths):
        raise FileExistsError("staging or rollback database already exists")
    _assert_database_offline(source_path)
    source_mode = stat.S_IMODE(source_path.stat().st_mode)
    source: sqlite3.Connection | None = None
    sealed = False
    try:
        source = sqlite3.connect(source_path)
        checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise RuntimeError("source WAL could not be checkpointed for offline repair")
        source_snapshot = _checked_connection_fingerprints(source, world_id=world_id)
        os.chmod(source_path, 0)
        sealed = True
        _assert_database_offline(
            source_path,
            allowed_pids=frozenset({os.getpid()}),
        )
        for target_path in target_paths:
            with sqlite3.connect(target_path) as target:
                source.backup(target)
            os.chmod(target_path, source_mode)
        source.close()
        source = None
        _assert_database_offline(source_path)
        return source_mode, source_snapshot
    except Exception:
        if source is not None:
            source.close()
        for target_path in target_paths:
            target_path.unlink(missing_ok=True)
            Path(str(target_path) + "-wal").unlink(missing_ok=True)
            Path(str(target_path) + "-shm").unlink(missing_ok=True)
        if sealed and source_path.exists():
            os.chmod(source_path, source_mode)
        raise


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seal_database_for_replacement(path: Path) -> None:
    """Fold WAL bytes into one self-contained main file before ``os.replace``."""

    with sqlite3.connect(path) as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise RuntimeError(f"database WAL could not be sealed: {path}")
    wal_path = Path(str(path) + "-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise RuntimeError(f"database WAL still contains frames after checkpoint: {path}")
    wal_path.unlink(missing_ok=True)
    Path(str(path) + "-shm").unlink(missing_ok=True)
    _fsync_file(path)


def _extract_rebuilt_head(
    database: Path,
    *,
    world_id: str,
    expected_cursor: ProjectionCursor,
) -> _RebuiltDerivedHead:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        head = connection.execute(
            "SELECT * FROM world_v2_heads WHERE world_id = ?",
            (world_id,),
        ).fetchone()
        if head is None:
            raise LedgerIntegrityError("replayed world head is unavailable")
        cursor = ProjectionCursor(
            world_revision=int(head["world_revision"]),
            deliberation_revision=int(head["deliberation_revision"]),
            ledger_sequence=int(head["ledger_sequence"]),
        )
        if cursor != expected_cursor:
            raise LedgerIntegrityError("cold replay changed the expected head cursor")
        if str(head["reducer_bundle_version"]) != REDUCER_BUNDLE_VERSION:
            raise LedgerIntegrityError("cold replay did not install the current reducer")
        if str(head["state_json"]) != _HEAD_STATE_SENTINEL:
            raise LedgerIntegrityError("cold replay did not produce split head storage")
        state_items = tuple(
            (str(row[0]), int(row[1]), str(row[2]))
            for row in connection.execute(
                """SELECT field, idx, item_json FROM world_v2_head_state_items
                   WHERE world_id = ? ORDER BY field, idx""",
                (world_id,),
            )
        )
        if not state_items:
            raise LedgerIntegrityError("cold replay produced no split head state")
        action_count, pending_action_count, action_fingerprint = _action_fingerprint(
            connection,
            world_id=world_id,
        )
        return _RebuiltDerivedHead(
            cursor=cursor,
            state_json=_HEAD_STATE_SENTINEL,
            semantic_hash=str(head["semantic_hash"]),
            reducer_bundle_version=str(head["reducer_bundle_version"]),
            state_hash=str(head["state_hash"]),
            state_items=state_items,
            action_count=action_count,
            pending_action_count=pending_action_count,
            action_fingerprint=action_fingerprint,
        )


def _derive_rebuilt_head(
    database: Path,
    *,
    world_id: str,
    inspection: _StaleHeadInspection,
) -> _RebuiltDerivedHead:
    """Cold-replay a disposable copy and return only its verified derived head."""

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        source_protected = _repair_protected_fingerprint(
            connection,
            world_id=world_id,
        )
    with tempfile.TemporaryDirectory(
        prefix=f".{database.name}.derived-head-replay-",
        dir=database.parent,
    ) as temporary:
        validation = Path(temporary) / database.name
        _backup_database(database, validation)
        _authorize_validation_copy(
            validation,
            world_id=world_id,
            inspection=inspection,
        )
        ledger = SQLiteWorldLedger(path=validation, world_id=world_id)
        try:
            projection = ledger.project()
            rebuilt = ledger.rebuild()
            if projection != rebuilt:
                raise LedgerIntegrityError(
                    "validation-copy projection does not match immutable cold replay"
                )
            cursor = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
            if cursor != inspection.cursor:
                raise LedgerIntegrityError("immutable cold replay changed the head cursor")
            evidence = ledger.export_replay_evidence()
            if evidence.projection != evidence.replay:
                raise LedgerIntegrityError("cold replay evidence is internally inconsistent")
        finally:
            ledger.close()
        with sqlite3.connect(f"file:{validation}?mode=ro", uri=True) as connection:
            replay_protected = _repair_protected_fingerprint(
                connection,
                world_id=world_id,
            )
        if replay_protected != source_protected:
            raise LedgerIntegrityError(
                "persisted prefix proofs or immutable compatibility rows do not match "
                "their cold reconstruction"
            )
        return _extract_rebuilt_head(
            validation,
            world_id=world_id,
            expected_cursor=inspection.cursor,
        )


def _install_rebuilt_head(
    database: Path,
    *,
    world_id: str,
    inspection: _StaleHeadInspection,
    rebuilt: _RebuiltDerivedHead,
) -> None:
    """CAS-replace exactly one world's head and split-state rows."""

    if rebuilt.cursor != inspection.cursor:
        raise LedgerIntegrityError("replayed head cursor does not match inspected source")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            head = connection.execute(
                "SELECT * FROM world_v2_heads WHERE world_id = ?",
                (world_id,),
            ).fetchone()
            mutation_row = connection.execute(
                """SELECT mutation_epoch FROM world_v2_ledger_mutation_epochs
                   WHERE world_id = ?""",
                (world_id,),
            ).fetchone()
            if (
                head is None
                or mutation_row is None
                or int(head["world_revision"]) != inspection.cursor.world_revision
                or int(head["deliberation_revision"]) != inspection.cursor.deliberation_revision
                or int(head["ledger_sequence"]) != inspection.cursor.ledger_sequence
                or str(head["reducer_bundle_version"]) != inspection.source_bundle
                or str(head["state_json"]) != inspection.state_json
                or str(head["state_hash"]) != inspection.state_hash
                or str(head["semantic_hash"]) != inspection.semantic_hash
                or int(head["storage_epoch"]) != inspection.storage_epoch
                or int(mutation_row[0]) != inspection.mutation_epoch
                or _head_state_items_fingerprint(connection, world_id=world_id)
                != inspection.head_state_items_fingerprint
            ):
                raise ConcurrencyConflict("world head changed after repair inspection")
            updated = connection.execute(
                """UPDATE world_v2_heads
                   SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?,
                       state_hash = ?, storage_epoch = storage_epoch + 1
                   WHERE world_id = ? AND world_revision = ?
                     AND deliberation_revision = ? AND ledger_sequence = ?
                     AND reducer_bundle_version = ? AND state_json = ?
                     AND state_hash = ? AND semantic_hash = ? AND storage_epoch = ?
                     AND (SELECT mutation_epoch FROM world_v2_ledger_mutation_epochs
                          WHERE world_id = ?) = ?""",
                (
                    rebuilt.state_json,
                    rebuilt.semantic_hash,
                    rebuilt.reducer_bundle_version,
                    rebuilt.state_hash,
                    world_id,
                    inspection.cursor.world_revision,
                    inspection.cursor.deliberation_revision,
                    inspection.cursor.ledger_sequence,
                    inspection.source_bundle,
                    inspection.state_json,
                    inspection.state_hash,
                    inspection.semantic_hash,
                    inspection.storage_epoch,
                    world_id,
                    inspection.mutation_epoch,
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict("world head CAS failed during offline repair")
            connection.execute(
                "DELETE FROM world_v2_head_state_items WHERE world_id = ?",
                (world_id,),
            )
            connection.executemany(
                """INSERT INTO world_v2_head_state_items
                   (world_id, field, idx, item_json) VALUES (?, ?, ?, ?)""",
                (
                    (world_id, field, index, item_json)
                    for field, index, item_json in rebuilt.state_items
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _verify_repaired_staging(
    database: Path,
    *,
    world_id: str,
    expected_cursor: ProjectionCursor,
) -> str:
    ledger = SQLiteWorldLedger(path=database, world_id=world_id)
    try:
        projected = ledger.project()
        rebuilt = ledger.rebuild()
        if projected != rebuilt:
            raise LedgerIntegrityError("repaired projection does not match cold replay")
        actual_cursor = ProjectionCursor(
            world_revision=projected.world_revision,
            deliberation_revision=projected.deliberation_revision,
            ledger_sequence=projected.ledger_sequence,
        )
        if actual_cursor != expected_cursor:
            raise LedgerIntegrityError("repaired head cursor changed")
        if projected.reducer_bundle_version != REDUCER_BUNDLE_VERSION:
            raise LedgerIntegrityError("repaired head did not install the current reducer")
        ledger.export_replay_evidence()
        return projected.semantic_hash
    finally:
        ledger.close()


def _repair_staging_database(
    database: Path,
    *,
    world_id: str,
    expected_source_bundle: str,
    expected_cursor: ProjectionCursor,
    expected_event_count: int,
    expected_latest_event_hash: str,
) -> tuple[str, str, str, int, int, str]:
    inspection = _inspect_stale_derived_head(
        database,
        world_id=world_id,
        expected_source_bundle=expected_source_bundle,
        expected_cursor=expected_cursor,
        expected_event_count=expected_event_count,
        expected_latest_event_hash=expected_latest_event_hash,
    )
    _, protected_before, action_before = _checked_database_fingerprints(
        database,
        world_id=world_id,
    )
    if action_before != (
        inspection.action_count,
        inspection.pending_action_count,
        inspection.action_fingerprint,
    ):
        raise ConcurrencyConflict("Action projection changed after repair inspection")
    rebuilt = _derive_rebuilt_head(
        database,
        world_id=world_id,
        inspection=inspection,
    )
    if (
        rebuilt.action_count,
        rebuilt.pending_action_count,
        rebuilt.action_fingerprint,
    ) != action_before:
        raise LedgerIntegrityError(
            "cold replay changed Action or pending-Action storage bytes or count"
        )
    _install_rebuilt_head(
        database,
        world_id=world_id,
        inspection=inspection,
        rebuilt=rebuilt,
    )
    new_semantic_hash = _verify_repaired_staging(
        database,
        world_id=world_id,
        expected_cursor=expected_cursor,
    )
    if new_semantic_hash != rebuilt.semantic_hash:
        raise LedgerIntegrityError("installed head semantic hash changed after cold replay")
    _, protected_after, action_after = _checked_database_fingerprints(
        database,
        world_id=world_id,
    )
    if protected_after != protected_before:
        raise LedgerIntegrityError(
            "repair changed immutable history, commit, prefix, or sidecar rows"
        )
    if action_after != action_before:
        raise LedgerIntegrityError("repair changed Action bytes or count")
    action_count, pending_action_count, action_fingerprint = action_after
    return (
        inspection.semantic_hash,
        new_semantic_hash,
        protected_after,
        action_count,
        pending_action_count,
        action_fingerprint,
    )


def repair_stale_derived_head(
    database: Path,
    *,
    world_id: str,
    expected_source_bundle: str,
    expected_cursor: ProjectionCursor,
    expected_event_count: int,
    expected_latest_event_hash: str,
    apply: bool = False,
    confirm_world_id: str | None = None,
) -> DerivedHeadRepairReport:
    """Verify one known stale derived head on an isolated SQLite backup."""

    path = database.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if not world_id:
        raise ValueError("world_id must not be empty")
    if expected_event_count < 0:
        raise ValueError("expected_event_count must not be negative")
    if apply and confirm_world_id != world_id:
        raise ValueError(f"--apply requires --confirm-world-id {world_id}")

    if not apply:
        with tempfile.TemporaryDirectory(
            prefix=f".{path.name}.stale-derived-head-dry-run-",
            dir=path.parent,
        ) as temporary:
            staging = Path(temporary) / path.name
            _backup_database(path, staging)
            (
                old_semantic_hash,
                new_semantic_hash,
                protected_fingerprint,
                action_count,
                pending_action_count,
                action_fingerprint,
            ) = _repair_staging_database(
                staging,
                world_id=world_id,
                expected_source_bundle=expected_source_bundle,
                expected_cursor=expected_cursor,
                expected_event_count=expected_event_count,
                expected_latest_event_hash=expected_latest_event_hash,
            )
        return DerivedHeadRepairReport(
            database=str(path),
            applied=False,
            repairable=True,
            world_id=world_id,
            source_bundle=expected_source_bundle,
            target_bundle=REDUCER_BUNDLE_VERSION,
            cursor=expected_cursor,
            event_count=expected_event_count,
            latest_event_hash=expected_latest_event_hash,
            old_semantic_hash=old_semantic_hash,
            new_semantic_hash=new_semantic_hash,
            rollback_database=None,
            integrity="ok",
            protected_fingerprint=protected_fingerprint,
            action_count=action_count,
            pending_action_count=pending_action_count,
            action_fingerprint=action_fingerprint,
        )

    suffix = f"{os.getpid()}"
    staging = path.with_name(path.name + f".stale-derived-head-staging-{suffix}")
    rollback = path.with_name(path.name + f".stale-derived-head-rollback-{suffix}")
    offline_lock = SingleInstanceLock(qq_outbound_owner_lock_path(path))
    installed = False
    staging_created = False
    rollback_created = False
    source_path_sealed = False
    source_mode: int | None = None
    offline_lock.__enter__()
    try:
        with sqlite_write_lock(path):
            source_mode, source_snapshot = _checkpoint_seal_and_backup_database(
                path,
                rollback,
                staging,
                world_id=world_id,
            )
            source_path_sealed = True
            rollback_created = True
            staging_created = True
            _seal_database_for_replacement(rollback)
            if _checked_database_fingerprints(rollback, world_id=world_id) != source_snapshot:
                raise LedgerIntegrityError("rollback backup does not match source database")
            if _checked_database_fingerprints(staging, world_id=world_id) != source_snapshot:
                raise LedgerIntegrityError("staging backup does not match source database")
            (
                old_semantic_hash,
                new_semantic_hash,
                protected_fingerprint,
                action_count,
                pending_action_count,
                action_fingerprint,
            ) = _repair_staging_database(
                staging,
                world_id=world_id,
                expected_source_bundle=expected_source_bundle,
                expected_cursor=expected_cursor,
                expected_event_count=expected_event_count,
                expected_latest_event_hash=expected_latest_event_hash,
            )
            _seal_database_for_replacement(staging)
            # Validate once more after folding the candidate WAL.  The exact
            # inode below is then installed while mode 000 prevents a daemon
            # from opening it before replacement durability is established.
            installed_new_semantic_hash = _verify_repaired_staging(
                staging,
                world_id=world_id,
                expected_cursor=expected_cursor,
            )
            _, installed_protected, installed_action = _checked_database_fingerprints(
                staging,
                world_id=world_id,
            )
            if (
                installed_new_semantic_hash != new_semantic_hash
                or installed_protected != protected_fingerprint
                or installed_action != (action_count, pending_action_count, action_fingerprint)
            ):
                raise LedgerIntegrityError("sealed repaired database failed invariant verification")
            _seal_database_for_replacement(staging)
            _fsync_file(rollback)
            candidate_identity = (
                staging.stat().st_dev,
                staging.stat().st_ino,
                staging.stat().st_size,
            )
            os.chmod(staging, 0)
            candidate_installed = False
            try:
                os.replace(staging, path)
                candidate_installed = True
                _fsync_directory(path.parent)
                installed_status = path.stat()
                if (
                    installed_status.st_dev,
                    installed_status.st_ino,
                    installed_status.st_size,
                ) != candidate_identity:
                    raise LedgerIntegrityError(
                        "installed repaired database is not the validated candidate inode"
                    )
                os.chmod(path, source_mode)
                source_path_sealed = False
                installed = True
            except Exception:
                if candidate_installed:
                    os.chmod(rollback, 0)
                    for suffix_path in ("-wal", "-shm"):
                        Path(str(path) + suffix_path).unlink(missing_ok=True)
                    os.replace(rollback, path)
                    _fsync_directory(path.parent)
                raise
        return DerivedHeadRepairReport(
            database=str(path),
            applied=True,
            repairable=True,
            world_id=world_id,
            source_bundle=expected_source_bundle,
            target_bundle=REDUCER_BUNDLE_VERSION,
            cursor=expected_cursor,
            event_count=expected_event_count,
            latest_event_hash=expected_latest_event_hash,
            old_semantic_hash=old_semantic_hash,
            new_semantic_hash=new_semantic_hash,
            rollback_database=str(rollback),
            integrity="ok",
            protected_fingerprint=protected_fingerprint,
            action_count=action_count,
            pending_action_count=pending_action_count,
            action_fingerprint=action_fingerprint,
        )
    finally:
        if source_path_sealed and source_mode is not None and path.exists():
            os.chmod(path, source_mode)
        if staging_created:
            staging.unlink(missing_ok=True)
            Path(str(staging) + "-wal").unlink(missing_ok=True)
            Path(str(staging) + "-shm").unlink(missing_ok=True)
        if rollback_created and not installed:
            rollback.unlink(missing_ok=True)
        if rollback_created:
            Path(str(rollback) + "-wal").unlink(missing_ok=True)
            Path(str(rollback) + "-shm").unlink(missing_ok=True)
        offline_lock.__exit__(None, None, None)


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
        snapshot_rows, event_rows = _legacy_counts(source, legacy_world_id=legacy_world_id)
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
                work.execute("DELETE FROM world_snapshots WHERE world_id=?", (legacy_world_id,))
            if _table_exists(work, "world_events"):
                work.execute("DELETE FROM world_events WHERE world_id=?", (legacy_world_id,))
            work.commit()
            work.execute("VACUUM INTO ?", (str(compacted),))
        with sqlite3.connect(f"file:{compacted}?mode=ro", uri=True) as candidate:
            candidate_integrity = _integrity(candidate)
            candidate_counts = _legacy_counts(candidate, legacy_world_id=legacy_world_id)
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


__all__ = [
    "DerivedHeadRepairReport",
    "LEGACY_WORLD_ID",
    "LedgerCompactionReport",
    "STALE_DERIVED_HEAD_SOURCE_BUNDLE",
    "compact_retired_v1",
    "repair_stale_derived_head",
]
