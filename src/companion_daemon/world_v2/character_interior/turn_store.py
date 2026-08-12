"""Bounded technical sidecar for durable CharacterInterior effect-once turns.

Rows in this store are coordination state only.  They never enter World V2
events, reducer heads, prefix proofs, or domain authority projections.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import secrets
from threading import RLock
from typing import Literal, Protocol

from ..sqlite_coordination import configure_shared_sqlite_connection, sqlite_write_lock


@dataclass(frozen=True, slots=True)
class _TurnCoordinationRequest:
    world_id: str
    actor_ref: str
    inner_turn_id: str
    phase: Literal["experience", "consider"]
    purpose: str
    subject_ref: str
    trigger_ref: str
    cursor_json: str
    request_hash: str
    snapshot_id: str
    snapshot_hash: str
    capability_hash: str


@dataclass(frozen=True, slots=True)
class _TurnCoordinationRecord:
    request: _TurnCoordinationRequest
    state: Literal["claimed", "checkpointed", "terminal"]
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    attempt_ordinal: int
    authored_state_json: str | None
    authored_state_hash: str | None
    terminal_result_json: str | None
    terminal_result_hash: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class _TurnAcquisition:
    status: Literal["acquired", "recovered", "terminal", "owned_elsewhere"]
    record: _TurnCoordinationRecord


class _CharacterInteriorTurnStore(Protocol):
    def acquire(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> _TurnAcquisition: ...

    def checkpoint(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        lease_token: str,
        attempt_ordinal: int,
        authored_state_json: str,
        authored_state_hash: str,
        now: datetime,
        expected_authored_state_hash: str | None = None,
    ) -> _TurnCoordinationRecord: ...

    def complete(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        lease_token: str,
        attempt_ordinal: int,
        terminal_result_json: str,
        terminal_result_hash: str,
        now: datetime,
    ) -> _TurnCoordinationRecord: ...

    def health(
        self,
        *,
        world_id: str,
        actor_ref: str,
        now: datetime | None = None,
    ) -> dict[str, object]: ...

    def prune_terminal(
        self, *, world_id: str, before: datetime, limit: int = 256
    ) -> int: ...

    async def aclose(self) -> None: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("CharacterInterior technical clock must be timezone-aware")
    return value.astimezone(UTC)


def _same_request(
    left: _TurnCoordinationRequest, right: _TurnCoordinationRequest
) -> bool:
    return left == right


def _health_scope(*, world_id: str, actor_ref: str) -> str:
    if world_id and actor_ref:
        return "world_actor"
    if world_id:
        return "world"
    if actor_ref:
        return "actor"
    return "all"


def _health_payload(
    rows: tuple[_TurnCoordinationRecord, ...],
    *,
    world_id: str,
    actor_ref: str,
    now: datetime,
) -> dict[str, object]:
    return {
        "scope": _health_scope(world_id=world_id, actor_ref=actor_ref),
        "pending_claim_count": sum(row.state != "terminal" for row in rows),
        "checkpointed_claim_count": sum(row.state == "checkpointed" for row in rows),
        "terminal_turn_count": sum(row.state == "terminal" for row in rows),
        "expired_claim_count": sum(
            row.state != "terminal"
            and row.lease_expires_at is not None
            and row.lease_expires_at <= now
            for row in rows
        ),
        "recovered_attempt_count": sum(row.attempt_ordinal > 1 for row in rows),
    }


class _InMemoryCharacterInteriorTurnStore:
    """Same CAS semantics as the SQLite sidecar for non-persistent ledgers."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._rows: dict[tuple[str, str, str], _TurnCoordinationRecord] = {}

    @staticmethod
    def _key(request: _TurnCoordinationRequest) -> tuple[str, str, str]:
        return request.world_id, request.actor_ref, request.inner_turn_id

    def acquire(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> _TurnAcquisition:
        now = _utc(now)
        if not owner_id or lease_seconds < 1:
            raise ValueError("CharacterInterior lease configuration is invalid")
        key = self._key(request)
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                row = _TurnCoordinationRecord(
                    request=request,
                    state="claimed",
                    lease_owner=owner_id,
                    lease_token=secrets.token_urlsafe(24),
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempt_ordinal=1,
                    authored_state_json=None,
                    authored_state_hash=None,
                    terminal_result_json=None,
                    terminal_result_hash=None,
                    updated_at=now,
                )
                self._rows[key] = row
                return _TurnAcquisition(status="acquired", record=row)
            if not _same_request(row.request, request):
                raise ValueError("CharacterInterior turn identity has conflicting request bytes")
            if row.state == "terminal":
                return _TurnAcquisition(status="terminal", record=row)
            if row.lease_expires_at is not None and now < row.lease_expires_at:
                return _TurnAcquisition(status="owned_elsewhere", record=row)
            row = replace(
                row,
                lease_owner=owner_id,
                lease_token=secrets.token_urlsafe(24),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_ordinal=row.attempt_ordinal + 1,
                updated_at=now,
            )
            self._rows[key] = row
            return _TurnAcquisition(
                status="recovered" if row.authored_state_json is not None else "acquired",
                record=row,
            )

    def checkpoint(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        lease_token: str,
        attempt_ordinal: int,
        authored_state_json: str,
        authored_state_hash: str,
        now: datetime,
        expected_authored_state_hash: str | None = None,
    ) -> _TurnCoordinationRecord:
        now = _utc(now)
        with self._lock:
            row = self._owned(request, owner_id, lease_token, attempt_ordinal, now)
            if row.authored_state_json is not None:
                if (
                    row.authored_state_json == authored_state_json
                    and row.authored_state_hash == authored_state_hash
                ):
                    return row
                if row.authored_state_hash != expected_authored_state_hash:
                    raise RuntimeError("CharacterInterior checkpoint CAS lost")
            elif expected_authored_state_hash is not None:
                raise RuntimeError("CharacterInterior checkpoint CAS lost")
            row = replace(
                row,
                state="checkpointed",
                authored_state_json=authored_state_json,
                authored_state_hash=authored_state_hash,
                updated_at=now,
            )
            self._rows[self._key(request)] = row
            return row

    def complete(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        lease_token: str,
        attempt_ordinal: int,
        terminal_result_json: str,
        terminal_result_hash: str,
        now: datetime,
    ) -> _TurnCoordinationRecord:
        now = _utc(now)
        with self._lock:
            row = self._rows.get(self._key(request))
            if row is None or not _same_request(row.request, request):
                raise ValueError("CharacterInterior terminal turn is unknown")
            if row.state == "terminal":
                if (
                    row.terminal_result_json != terminal_result_json
                    or row.terminal_result_hash != terminal_result_hash
                ):
                    raise ValueError("CharacterInterior terminal result bytes changed")
                return row
            row = self._owned(request, owner_id, lease_token, attempt_ordinal, now)
            if row.authored_state_json is None:
                raise ValueError("CharacterInterior terminal result lacks authored checkpoint")
            row = replace(
                row,
                state="terminal",
                lease_owner=None,
                lease_expires_at=None,
                terminal_result_json=terminal_result_json,
                terminal_result_hash=terminal_result_hash,
                updated_at=now,
            )
            self._rows[self._key(request)] = row
            return row

    def _owned(
        self,
        request: _TurnCoordinationRequest,
        owner_id: str,
        lease_token: str,
        attempt_ordinal: int,
        now: datetime,
    ) -> _TurnCoordinationRecord:
        row = self._rows.get(self._key(request))
        if (
            row is None
            or not _same_request(row.request, request)
            or row.state == "terminal"
            or row.lease_owner != owner_id
            or row.lease_token != lease_token
            or row.attempt_ordinal != attempt_ordinal
            or row.lease_expires_at is None
            or now >= row.lease_expires_at
        ):
            raise RuntimeError("CharacterInterior turn lease is no longer owned")
        return row

    def health(
        self,
        *,
        world_id: str,
        actor_ref: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        now = _utc(now or datetime.now(UTC))
        with self._lock:
            rows = tuple(
                row
                for key, row in self._rows.items()
                if (not world_id or key[0] == world_id)
                and (not actor_ref or key[1] == actor_ref)
            )
        return _health_payload(
            rows,
            world_id=world_id,
            actor_ref=actor_ref,
            now=now,
        )

    def prune_terminal(self, *, world_id: str, before: datetime, limit: int = 256) -> int:
        before = _utc(before)
        if limit < 1:
            raise ValueError("CharacterInterior prune limit must be positive")
        with self._lock:
            keys = [
                key
                for key, row in sorted(
                    self._rows.items(), key=lambda item: item[1].updated_at
                )
                if row.state == "terminal" and row.updated_at < before
                and row.request.world_id == world_id
            ][:limit]
            for key in keys:
                del self._rows[key]
        return len(keys)

    async def aclose(self) -> None:
        return None


class _SQLiteCharacterInteriorTurnStore:
    """SQLite BEGIN IMMEDIATE/CAS implementation outside the immutable ledger."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        world_id: str,
        database_write_lock: object,
        thread_lock: RLock,
    ) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._world_id = world_id
        self._database_write_lock = database_write_lock
        self._thread_lock = thread_lock

    @staticmethod
    def create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS world_v2_character_interior_turns (
                world_id TEXT NOT NULL,
                actor_ref TEXT NOT NULL,
                inner_turn_id TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (phase IN ('experience', 'consider')),
                purpose TEXT NOT NULL,
                subject_ref TEXT NOT NULL,
                trigger_ref TEXT NOT NULL,
                cursor_json TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                capability_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('claimed', 'checkpointed', 'terminal')),
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
                authored_state_json TEXT,
                authored_state_hash TEXT,
                terminal_result_json TEXT,
                terminal_result_hash TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (world_id, actor_ref, inner_turn_id),
                CHECK ((authored_state_json IS NULL) = (authored_state_hash IS NULL)),
                CHECK ((terminal_result_json IS NULL) = (terminal_result_hash IS NULL)),
                CHECK (
                    (state = 'terminal' AND lease_owner IS NULL AND lease_expires_at IS NULL
                     AND terminal_result_json IS NOT NULL)
                    OR
                    (state != 'terminal' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
                     AND terminal_result_json IS NULL)
                )
            )
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(world_v2_character_interior_turns)"
            )
        }
        if "lease_token" not in columns:
            # The sidecar was introduced after the first WIP checkpoint.  This
            # additive migration keeps old coordination rows recoverable while
            # ensuring every newly acquired lease has a distinct token.
            connection.execute(
                "ALTER TABLE world_v2_character_interior_turns ADD COLUMN lease_token TEXT"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS world_v2_character_interior_turns_retention
            ON world_v2_character_interior_turns (state, updated_at)
            """
        )

    def acquire(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> _TurnAcquisition:
        now = _utc(now)
        if request.world_id != self._world_id or not owner_id or lease_seconds < 1:
            raise ValueError("CharacterInterior SQLite lease request is invalid")
        with self._database_write_lock, self._thread_lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select(request)
                if row is None:
                    expires = now + timedelta(seconds=lease_seconds)
                    lease_token = secrets.token_urlsafe(24)
                    self._connection.execute(
                        """INSERT INTO world_v2_character_interior_turns
                           (world_id, actor_ref, inner_turn_id, phase, purpose, subject_ref,
                            trigger_ref, cursor_json, request_hash, snapshot_id, snapshot_hash,
                            capability_hash, state, lease_owner, lease_token,
                            lease_expires_at, attempt_ordinal, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, 1, ?)""",
                        self._request_values(request)
                        + (owner_id, lease_token, expires.isoformat(), now.isoformat()),
                    )
                    record = self._select_required(request)
                    self._connection.commit()
                    return _TurnAcquisition(status="acquired", record=record)
                record = self._record(row)
                if record.request != request:
                    raise ValueError("CharacterInterior turn identity has conflicting request bytes")
                if record.state == "terminal":
                    self._connection.commit()
                    return _TurnAcquisition(status="terminal", record=record)
                if record.lease_expires_at is not None and now < record.lease_expires_at:
                    self._connection.commit()
                    return _TurnAcquisition(status="owned_elsewhere", record=record)
                expires = now + timedelta(seconds=lease_seconds)
                lease_token = secrets.token_urlsafe(24)
                changed = self._connection.execute(
                    """UPDATE world_v2_character_interior_turns
                       SET lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                           attempt_ordinal = attempt_ordinal + 1, updated_at = ?
                       WHERE world_id = ? AND actor_ref = ? AND inner_turn_id = ?
                         AND state != 'terminal' AND attempt_ordinal = ?""",
                    (
                        owner_id,
                        lease_token,
                        expires.isoformat(),
                        now.isoformat(),
                        request.world_id,
                        request.actor_ref,
                        request.inner_turn_id,
                        record.attempt_ordinal,
                    ),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("CharacterInterior lease CAS lost")
                recovered = self._select_required(request)
                self._connection.commit()
                return _TurnAcquisition(
                    status=(
                        "recovered"
                        if recovered.authored_state_json is not None
                        else "acquired"
                    ),
                    record=recovered,
                )
            except Exception:
                self._connection.rollback()
                raise

    def checkpoint(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        lease_token: str,
        attempt_ordinal: int,
        authored_state_json: str,
        authored_state_hash: str,
        now: datetime,
        expected_authored_state_hash: str | None = None,
    ) -> _TurnCoordinationRecord:
        return self._write_owned(
            request=request,
            owner_id=owner_id,
            lease_token=lease_token,
            attempt_ordinal=attempt_ordinal,
            now=now,
            statement="""UPDATE world_v2_character_interior_turns
                         SET state = 'checkpointed', authored_state_json = ?,
                             authored_state_hash = ?, updated_at = ?
                         WHERE world_id = ? AND actor_ref = ? AND inner_turn_id = ?
                           AND state != 'terminal' AND lease_owner = ? AND lease_token = ?
                           AND attempt_ordinal = ? AND lease_expires_at > ?
                           AND ((authored_state_json = ? AND authored_state_hash = ?)
                                OR ((? IS NULL AND authored_state_hash IS NULL)
                                    OR authored_state_hash = ?))""",
            values=(authored_state_json, authored_state_hash),
            repeated=(
                authored_state_json,
                authored_state_hash,
                expected_authored_state_hash,
                expected_authored_state_hash,
            ),
        )

    def complete(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        lease_token: str,
        attempt_ordinal: int,
        terminal_result_json: str,
        terminal_result_hash: str,
        now: datetime,
    ) -> _TurnCoordinationRecord:
        now = _utc(now)
        with self._database_write_lock, self._thread_lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._select_required(request)
                if existing.request != request:
                    raise ValueError("CharacterInterior terminal request bytes changed")
                if existing.state == "terminal":
                    if (
                        existing.terminal_result_json != terminal_result_json
                        or existing.terminal_result_hash != terminal_result_hash
                    ):
                        raise ValueError("CharacterInterior terminal result bytes changed")
                    self._connection.commit()
                    return existing
                if existing.authored_state_json is None:
                    raise ValueError("CharacterInterior terminal result lacks authored checkpoint")
                changed = self._connection.execute(
                    """UPDATE world_v2_character_interior_turns
                       SET state = 'terminal', lease_owner = NULL, lease_expires_at = NULL,
                           terminal_result_json = ?, terminal_result_hash = ?, updated_at = ?
                       WHERE world_id = ? AND actor_ref = ? AND inner_turn_id = ?
                           AND state = 'checkpointed' AND lease_owner = ? AND lease_token = ?
                         AND attempt_ordinal = ? AND lease_expires_at > ?""",
                    (
                        terminal_result_json,
                        terminal_result_hash,
                        now.isoformat(),
                        request.world_id,
                        request.actor_ref,
                        request.inner_turn_id,
                        owner_id,
                        lease_token,
                        attempt_ordinal,
                        now.isoformat(),
                    ),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("CharacterInterior terminal CAS lost")
                record = self._select_required(request)
                self._connection.commit()
                return record
            except Exception:
                self._connection.rollback()
                raise

    def _write_owned(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        lease_token: str,
        attempt_ordinal: int,
        now: datetime,
        statement: str,
        values: tuple[object, ...],
        repeated: tuple[object, ...],
    ) -> _TurnCoordinationRecord:
        now = _utc(now)
        with self._database_write_lock, self._thread_lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._select_required(request)
                if existing.request != request:
                    raise ValueError("CharacterInterior checkpoint request bytes changed")
                changed = self._connection.execute(
                    statement,
                    values
                    + (
                        now.isoformat(),
                        request.world_id,
                        request.actor_ref,
                        request.inner_turn_id,
                        owner_id,
                        lease_token,
                        attempt_ordinal,
                        now.isoformat(),
                    )
                    + repeated,
                )
                if changed.rowcount != 1:
                    raise RuntimeError("CharacterInterior checkpoint CAS lost")
                record = self._select_required(request)
                self._connection.commit()
                return record
            except Exception:
                self._connection.rollback()
                raise

    def health(
        self,
        *,
        world_id: str,
        actor_ref: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        now = _utc(now or datetime.now(UTC))
        clauses: list[str] = []
        values: list[object] = []
        if world_id:
            clauses.append("world_id = ?")
            values.append(world_id)
        if actor_ref:
            clauses.append("actor_ref = ?")
            values.append(actor_ref)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._thread_lock:
            rows = tuple(
                self._connection.execute(
                    f"""SELECT state, attempt_ordinal, lease_expires_at
                       FROM world_v2_character_interior_turns{where}""",
                    tuple(values),
                )
            )
        pending = sum(row["state"] != "terminal" for row in rows)
        return {
            "scope": _health_scope(world_id=world_id, actor_ref=actor_ref),
            "pending_claim_count": pending,
            "checkpointed_claim_count": sum(row["state"] == "checkpointed" for row in rows),
            "terminal_turn_count": sum(row["state"] == "terminal" for row in rows),
            "expired_claim_count": sum(
                row["state"] != "terminal"
                and row["lease_expires_at"] is not None
                and datetime.fromisoformat(str(row["lease_expires_at"])) <= now
                for row in rows
            ),
            "recovered_attempt_count": sum(int(row["attempt_ordinal"]) > 1 for row in rows),
        }

    def prune_terminal(self, *, world_id: str, before: datetime, limit: int = 256) -> int:
        before = _utc(before)
        if limit < 1:
            raise ValueError("CharacterInterior prune limit must be positive")
        with self._database_write_lock, self._thread_lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                changed = self._connection.execute(
                    """DELETE FROM world_v2_character_interior_turns
                       WHERE rowid IN (
                           SELECT rowid FROM world_v2_character_interior_turns
                           WHERE world_id = ? AND state = 'terminal' AND updated_at < ?
                           ORDER BY updated_at LIMIT ?
                       )""",
                    (world_id, before.isoformat(), limit),
                )
                self._connection.commit()
                return int(changed.rowcount)
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._database_write_lock, self._thread_lock:
            self._connection.close()

    async def aclose(self) -> None:
        self.close()

    @staticmethod
    def _request_values(request: _TurnCoordinationRequest) -> tuple[object, ...]:
        return (
            request.world_id,
            request.actor_ref,
            request.inner_turn_id,
            request.phase,
            request.purpose,
            request.subject_ref,
            request.trigger_ref,
            request.cursor_json,
            request.request_hash,
            request.snapshot_id,
            request.snapshot_hash,
            request.capability_hash,
        )

    def _select(self, request: _TurnCoordinationRequest):  # type: ignore[no-untyped-def]
        return self._connection.execute(
            """SELECT * FROM world_v2_character_interior_turns
               WHERE world_id = ? AND actor_ref = ? AND inner_turn_id = ?""",
            (request.world_id, request.actor_ref, request.inner_turn_id),
        ).fetchone()

    def _select_required(
        self, request: _TurnCoordinationRequest
    ) -> _TurnCoordinationRecord:
        row = self._select(request)
        if row is None:
            raise RuntimeError("CharacterInterior coordination row disappeared")
        return self._record(row)

    @staticmethod
    def _record(row) -> _TurnCoordinationRecord:  # type: ignore[no-untyped-def]
        request = _TurnCoordinationRequest(
            world_id=str(row["world_id"]),
            actor_ref=str(row["actor_ref"]),
            inner_turn_id=str(row["inner_turn_id"]),
            phase=str(row["phase"]),
            purpose=str(row["purpose"]),
            subject_ref=str(row["subject_ref"]),
            trigger_ref=str(row["trigger_ref"]),
            cursor_json=str(row["cursor_json"]),
            request_hash=str(row["request_hash"]),
            snapshot_id=str(row["snapshot_id"]),
            snapshot_hash=str(row["snapshot_hash"]),
            capability_hash=str(row["capability_hash"]),
        )
        return _TurnCoordinationRecord(
            request=request,
            state=str(row["state"]),
            lease_owner=(str(row["lease_owner"]) if row["lease_owner"] is not None else None),
            lease_token=(str(row["lease_token"]) if row["lease_token"] is not None else None),
            lease_expires_at=(
                datetime.fromisoformat(str(row["lease_expires_at"]))
                if row["lease_expires_at"] is not None
                else None
            ),
            attempt_ordinal=int(row["attempt_ordinal"]),
            authored_state_json=(
                str(row["authored_state_json"])
                if row["authored_state_json"] is not None
                else None
            ),
            authored_state_hash=(
                str(row["authored_state_hash"])
                if row["authored_state_hash"] is not None
                else None
            ),
            terminal_result_json=(
                str(row["terminal_result_json"])
                if row["terminal_result_json"] is not None
                else None
            ),
            terminal_result_hash=(
                str(row["terminal_result_hash"])
                if row["terminal_result_hash"] is not None
                else None
            ),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )


def open_sqlite_character_interior_turn_store(
    *, path: str | Path, world_id: str
) -> _SQLiteCharacterInteriorTurnStore:
    """Open the technical CharacterInterior sidecar over one World database.

    The connection is independent from the immutable ledger connection, while
    sharing its file-level writer lock and WAL policy.  No sidecar row is part
    of a World event, reducer head, or prefix proof.
    """

    database_path = Path(path).expanduser().absolute()
    connection = sqlite3.connect(
        str(database_path), isolation_level=None, check_same_thread=False
    )
    thread_lock = RLock()
    writer_lock = sqlite_write_lock(database_path)
    try:
        with writer_lock, thread_lock:
            configure_shared_sqlite_connection(connection)
            store = _SQLiteCharacterInteriorTurnStore(
                connection=connection,
                world_id=world_id,
                database_write_lock=writer_lock,
                thread_lock=thread_lock,
            )
            store.create_schema(connection)
        return store
    except BaseException:
        connection.close()
        raise


__all__ = [
    "open_sqlite_character_interior_turn_store",
]
