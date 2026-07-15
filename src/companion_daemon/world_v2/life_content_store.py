"""Immutable text sidecar for source-bound lived-world content.

The ledger decides *whether* a content record is visible.  This module only
stores and retrieves exact UTF-8 bytes by an immutable reference; it knows no
occurrence, Experience, Context, or proposal semantics.  Keeping that policy
out of the store is what lets :mod:`life_content` remain the one read module
that validates a descriptor against a pinned ledger cursor.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3
from threading import RLock
from typing import Literal, Protocol


LifeContentKind = Literal["occurrence_result", "experience_summary"]


def life_content_payload_hash(text: str) -> str:
    """Return the exact unprefixed SHA-256 hash for UTF-8 content."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredLifeContent:
    """One immutable complete content value.

    The descriptor authority, privacy gate and historical visibility live in
    the ledger-side compiler.  This record intentionally carries just enough
    data to prevent a ref from being rebound to different bytes or a different
    semantic content lane.
    """

    content_ref: str
    content_kind: LifeContentKind
    content_payload_hash: str
    text: str

    def __post_init__(self) -> None:
        if not self.content_ref or len(self.content_ref) > 512:
            raise ValueError("life content ref must contain between 1 and 512 chars")
        if self.content_kind not in {"occurrence_result", "experience_summary"}:
            raise ValueError("unsupported life content kind")
        if len(self.text) > 12_000:
            raise ValueError("life content exceeds the maximum size")
        if self.content_payload_hash != life_content_payload_hash(self.text):
            raise ValueError("life content hash does not match exact UTF-8 text")


class ImmutableLifeContentStore(Protocol):
    """Append-only content seam; a duplicate must be byte-for-byte identical."""

    def put_if_absent(self, record: StoredLifeContent) -> None: ...

    def read_exact(self, *, content_ref: str) -> StoredLifeContent | None: ...


class InMemoryImmutableLifeContentStore:
    """Thread-safe adapter used by interface-level compiler tests."""

    def __init__(self) -> None:
        self._records: dict[str, StoredLifeContent] = {}
        self._lock = RLock()

    def put_if_absent(self, record: StoredLifeContent) -> None:
        with self._lock:
            existing = self._records.get(record.content_ref)
            if existing is None:
                self._records[record.content_ref] = record
            elif existing != record:
                raise ValueError("life content ref is already bound to different immutable bytes")

    def read_exact(self, *, content_ref: str) -> StoredLifeContent | None:
        with self._lock:
            return self._records.get(content_ref)


class SQLiteImmutableLifeContentStore:
    """Durable adapter deliberately independent from ``SQLiteWorldLedger`` internals."""

    def __init__(self, *, path: str, world_id: str) -> None:
        if not path or not world_id:
            raise ValueError("life content SQLite store needs path and world id")
        self._world_id = world_id
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS world_v2_life_content (
                world_id TEXT NOT NULL,
                content_ref TEXT NOT NULL,
                content_kind TEXT NOT NULL,
                content_payload_hash TEXT NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY (world_id, content_ref)
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def put_if_absent(self, record: StoredLifeContent) -> None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT content_kind, content_payload_hash, text
                FROM world_v2_life_content
                WHERE world_id = ? AND content_ref = ?
                """,
                (self._world_id, record.content_ref),
            ).fetchone()
            if row is not None:
                existing = StoredLifeContent(
                    content_ref=record.content_ref,
                    content_kind=row[0],
                    content_payload_hash=row[1],
                    text=row[2],
                )
                if existing != record:
                    raise ValueError(
                        "life content ref is already bound to different immutable bytes"
                    )
                return
            self._connection.execute(
                """
                INSERT INTO world_v2_life_content
                    (world_id, content_ref, content_kind, content_payload_hash, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._world_id,
                    record.content_ref,
                    record.content_kind,
                    record.content_payload_hash,
                    record.text,
                ),
            )
            self._connection.commit()

    def read_exact(self, *, content_ref: str) -> StoredLifeContent | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT content_kind, content_payload_hash, text
                FROM world_v2_life_content
                WHERE world_id = ? AND content_ref = ?
                """,
                (self._world_id, content_ref),
            ).fetchone()
        if row is None:
            return None
        return StoredLifeContent(
            content_ref=content_ref,
            content_kind=row[0],
            content_payload_hash=row[1],
            text=row[2],
        )


__all__ = [
    "ImmutableLifeContentStore",
    "InMemoryImmutableLifeContentStore",
    "LifeContentKind",
    "SQLiteImmutableLifeContentStore",
    "StoredLifeContent",
    "life_content_payload_hash",
]
