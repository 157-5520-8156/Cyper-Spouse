"""Opaque immutable payload storage for accepted expression beats.

The world ledger only records a descriptor permitting an Action to use a
payload.  It deliberately never receives the opaque/encrypted payload bytes.
This store is a tiny append-only capability used by the acceptance compiler and
the read-only Action resolver.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3
from threading import RLock
from typing import Literal, Protocol

from .sqlite_coordination import configure_shared_sqlite_connection, sqlite_write_lock


ExpressionPayloadKind = Literal["referenced", "inline_encrypted"]
ExpressionPayloadPrivacy = Literal["public", "shareable", "personal", "private", "withhold"]


def expression_payload_hash(encoded_payload: str) -> str:
    return "sha256:" + hashlib.sha256(encoded_payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredExpressionPayload:
    payload_ref: str
    payload_hash: str
    content_type: str
    privacy_class: ExpressionPayloadPrivacy
    payload_kind: ExpressionPayloadKind
    encoded_payload: str

    def __post_init__(self) -> None:
        if not self.payload_ref or len(self.payload_ref) > 512:
            raise ValueError("expression payload ref must contain between 1 and 512 chars")
        if not self.content_type or len(self.content_type) > 128:
            raise ValueError("expression payload content type is invalid")
        if self.payload_kind not in {"referenced", "inline_encrypted"}:
            raise ValueError("unsupported expression payload kind")
        if self.privacy_class not in {"public", "shareable", "personal", "private", "withhold"}:
            raise ValueError("unsupported expression payload privacy")
        if not self.encoded_payload or len(self.encoded_payload) > 131_072:
            raise ValueError("expression payload is empty or oversized")
        if self.payload_hash != expression_payload_hash(self.encoded_payload):
            raise ValueError("expression payload hash does not bind exact bytes")


class ImmutableExpressionPayloadStore(Protocol):
    def put_if_absent(self, record: StoredExpressionPayload) -> None: ...

    def read_exact(self, *, payload_ref: str) -> StoredExpressionPayload | None: ...


class InMemoryImmutableExpressionPayloadStore:
    def __init__(self) -> None:
        self._records: dict[str, StoredExpressionPayload] = {}
        self._lock = RLock()

    def put_if_absent(self, record: StoredExpressionPayload) -> None:
        with self._lock:
            existing = self._records.get(record.payload_ref)
            if existing is None:
                self._records[record.payload_ref] = record
            elif existing != record:
                raise ValueError("expression payload ref is already bound to different immutable bytes")

    def read_exact(self, *, payload_ref: str) -> StoredExpressionPayload | None:
        with self._lock:
            return self._records.get(payload_ref)


class SQLiteImmutableExpressionPayloadStore:
    def __init__(self, *, path: str, world_id: str) -> None:
        if not path or not world_id:
            raise ValueError("expression payload SQLite store needs path and world id")
        self._world_id = world_id
        self._lock = RLock()
        self._database_write_lock = sqlite_write_lock(path)
        # Autocommit; see SQLiteImmutableLifeContentStore for the WAL-pinning
        # rationale shared by every sidecar on this file.
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        with self._database_write_lock:
            configure_shared_sqlite_connection(self._connection)
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS world_v2_expression_payload (
                    world_id TEXT NOT NULL,
                    payload_ref TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    privacy_class TEXT NOT NULL,
                    payload_kind TEXT NOT NULL,
                    encoded_payload TEXT NOT NULL,
                    PRIMARY KEY (world_id, payload_ref)
                )
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def put_if_absent(self, record: StoredExpressionPayload) -> None:
        with self._database_write_lock, self._lock:
            row = self._connection.execute(
                "SELECT payload_hash, content_type, privacy_class, payload_kind, encoded_payload "
                "FROM world_v2_expression_payload WHERE world_id = ? AND payload_ref = ?",
                (self._world_id, record.payload_ref),
            ).fetchone()
            if row is not None:
                existing = StoredExpressionPayload(record.payload_ref, *row)
                if existing != record:
                    raise ValueError("expression payload ref is already bound to different immutable bytes")
                return
            self._connection.execute(
                "INSERT INTO world_v2_expression_payload "
                "(world_id, payload_ref, payload_hash, content_type, privacy_class, payload_kind, encoded_payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._world_id, record.payload_ref, record.payload_hash, record.content_type,
                 record.privacy_class, record.payload_kind, record.encoded_payload),
            )
            self._connection.commit()

    def read_exact(self, *, payload_ref: str) -> StoredExpressionPayload | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_hash, content_type, privacy_class, payload_kind, encoded_payload "
                "FROM world_v2_expression_payload WHERE world_id = ? AND payload_ref = ?",
                (self._world_id, payload_ref),
            ).fetchone()
        return None if row is None else StoredExpressionPayload(payload_ref, *row)


__all__ = [
    "ExpressionPayloadKind", "ExpressionPayloadPrivacy", "ImmutableExpressionPayloadStore",
    "InMemoryImmutableExpressionPayloadStore", "SQLiteImmutableExpressionPayloadStore",
    "StoredExpressionPayload", "expression_payload_hash",
]
