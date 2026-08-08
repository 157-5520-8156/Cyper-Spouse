"""Bounded technical sidecar for durable CharacterInterior effect-once turns.

Rows in this store are coordination state only.  They never enter World V2
events, reducer heads, prefix proofs, or domain authority projections.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Literal, Protocol


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
        attempt_ordinal: int,
        authored_state_json: str,
        authored_state_hash: str,
        now: datetime,
    ) -> _TurnCoordinationRecord: ...

    def complete(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
        attempt_ordinal: int,
        terminal_result_json: str,
        terminal_result_hash: str,
        now: datetime,
    ) -> _TurnCoordinationRecord: ...

    def health(self, *, world_id: str, actor_ref: str) -> dict[str, int]: ...

    def prune_terminal(self, *, before: datetime, limit: int = 256) -> int: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("CharacterInterior technical clock must be timezone-aware")
    return value.astimezone(UTC)


def _same_request(
    left: _TurnCoordinationRequest, right: _TurnCoordinationRequest
) -> bool:
    return left == right


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
            if row.lease_owner == owner_id and row.lease_expires_at is not None and now < row.lease_expires_at:
                return _TurnAcquisition(
                    status="recovered" if row.authored_state_json is not None else "acquired",
                    record=row,
                )
            if row.lease_expires_at is not None and now < row.lease_expires_at:
                return _TurnAcquisition(status="owned_elsewhere", record=row)
            row = replace(
                row,
                lease_owner=owner_id,
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
        attempt_ordinal: int,
        authored_state_json: str,
        authored_state_hash: str,
        now: datetime,
    ) -> _TurnCoordinationRecord:
        now = _utc(now)
        with self._lock:
            row = self._owned(request, owner_id, attempt_ordinal, now)
            if row.authored_state_json is not None:
                if (
                    row.authored_state_json != authored_state_json
                    or row.authored_state_hash != authored_state_hash
                ):
                    raise ValueError("CharacterInterior authored checkpoint bytes changed")
                return row
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
            row = self._owned(request, owner_id, attempt_ordinal, now)
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
        attempt_ordinal: int,
        now: datetime,
    ) -> _TurnCoordinationRecord:
        row = self._rows.get(self._key(request))
        if (
            row is None
            or not _same_request(row.request, request)
            or row.state == "terminal"
            or row.lease_owner != owner_id
            or row.attempt_ordinal != attempt_ordinal
            or row.lease_expires_at is None
            or now >= row.lease_expires_at
        ):
            raise RuntimeError("CharacterInterior turn lease is no longer owned")
        return row

    def health(self, *, world_id: str, actor_ref: str) -> dict[str, int]:
        with self._lock:
            rows = tuple(
                row
                for key, row in self._rows.items()
                if key[0] == world_id and key[1] == actor_ref
            )
        return {
            "pending_claim_count": sum(row.state != "terminal" for row in rows),
            "checkpointed_claim_count": sum(row.state == "checkpointed" for row in rows),
            "terminal_turn_count": sum(row.state == "terminal" for row in rows),
        }

    def prune_terminal(self, *, before: datetime, limit: int = 256) -> int:
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
            ][:limit]
            for key in keys:
                del self._rows[key]
        return len(keys)


class _SQLiteCharacterInteriorTurnStore:
    """SQLite BEGIN IMMEDIATE/CAS implementation outside the immutable ledger."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        world_id: str,
        database_write_lock: RLock,
        thread_lock: RLock,
    ) -> None:
        self._connection = connection
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
                    self._connection.execute(
                        """INSERT INTO world_v2_character_interior_turns
                           (world_id, actor_ref, inner_turn_id, phase, purpose, subject_ref,
                            trigger_ref, cursor_json, request_hash, snapshot_id, snapshot_hash,
                            capability_hash, state, lease_owner, lease_expires_at,
                            attempt_ordinal, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, 1, ?)""",
                        self._request_values(request)
                        + (owner_id, expires.isoformat(), now.isoformat()),
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
                if (
                    record.lease_owner == owner_id
                    and record.lease_expires_at is not None
                    and now < record.lease_expires_at
                ):
                    self._connection.commit()
                    return _TurnAcquisition(
                        status=(
                            "recovered"
                            if record.authored_state_json is not None
                            else "acquired"
                        ),
                        record=record,
                    )
                if record.lease_expires_at is not None and now < record.lease_expires_at:
                    self._connection.commit()
                    return _TurnAcquisition(status="owned_elsewhere", record=record)
                expires = now + timedelta(seconds=lease_seconds)
                changed = self._connection.execute(
                    """UPDATE world_v2_character_interior_turns
                       SET lease_owner = ?, lease_expires_at = ?,
                           attempt_ordinal = attempt_ordinal + 1, updated_at = ?
                       WHERE world_id = ? AND actor_ref = ? AND inner_turn_id = ?
                         AND state != 'terminal' AND attempt_ordinal = ?""",
                    (
                        owner_id,
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
        attempt_ordinal: int,
        authored_state_json: str,
        authored_state_hash: str,
        now: datetime,
    ) -> _TurnCoordinationRecord:
        return self._write_owned(
            request=request,
            owner_id=owner_id,
            attempt_ordinal=attempt_ordinal,
            now=now,
            statement="""UPDATE world_v2_character_interior_turns
                         SET state = 'checkpointed', authored_state_json = ?,
                             authored_state_hash = ?, updated_at = ?
                         WHERE world_id = ? AND actor_ref = ? AND inner_turn_id = ?
                           AND state != 'terminal' AND lease_owner = ?
                           AND attempt_ordinal = ? AND lease_expires_at > ?
                           AND (authored_state_json IS NULL OR
                                (authored_state_json = ? AND authored_state_hash = ?))""",
            values=(authored_state_json, authored_state_hash),
            repeated=(authored_state_json, authored_state_hash),
        )

    def complete(
        self,
        *,
        request: _TurnCoordinationRequest,
        owner_id: str,
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
                         AND state = 'checkpointed' AND lease_owner = ?
                         AND attempt_ordinal = ? AND lease_expires_at > ?""",
                    (
                        terminal_result_json,
                        terminal_result_hash,
                        now.isoformat(),
                        request.world_id,
                        request.actor_ref,
                        request.inner_turn_id,
                        owner_id,
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

    def health(self, *, world_id: str, actor_ref: str) -> dict[str, int]:
        with self._thread_lock:
            rows = tuple(
                self._connection.execute(
                    """SELECT state, COUNT(*) AS count
                       FROM world_v2_character_interior_turns
                       WHERE world_id = ? AND actor_ref = ? GROUP BY state""",
                    (world_id, actor_ref),
                )
            )
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        return {
            "pending_claim_count": counts.get("claimed", 0)
            + counts.get("checkpointed", 0),
            "checkpointed_claim_count": counts.get("checkpointed", 0),
            "terminal_turn_count": counts.get("terminal", 0),
        }

    def prune_terminal(self, *, before: datetime, limit: int = 256) -> int:
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
                           WHERE state = 'terminal' AND updated_at < ?
                           ORDER BY updated_at LIMIT ?
                       )""",
                    (before.isoformat(), limit),
                )
                self._connection.commit()
                return int(changed.rowcount)
            except Exception:
                self._connection.rollback()
                raise

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


__all__: list[str] = []
