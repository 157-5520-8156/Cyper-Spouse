"""Process-local coordination for SQLite stores sharing one World-v2 file.

World-v2 deliberately keeps the ledger and opaque sidecars as separate
modules, but production composes several of them over the same SQLite path.
SQLite serializes those writers at the file level; a shared lock makes that
serialization explicit and prevents a sidecar commit from arriving halfway
through a large ledger snapshot transaction.

This is only an in-process optimization.  SQLite's own transactions and CAS
checks remain the cross-process authority.
"""

from pathlib import Path
import sqlite3
from threading import RLock


_LOCKS_GUARD = RLock()
_WRITE_LOCKS: dict[str, RLock] = {}


def sqlite_write_lock(path: str | Path) -> RLock:
    """Return the stable process-local writer lock for ``path``."""

    key = str(Path(path).expanduser().absolute())
    with _LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _WRITE_LOCKS[key] = lock
        return lock


def configure_shared_sqlite_connection(connection: sqlite3.Connection) -> None:
    """Apply the shared-file WAL policy to a sidecar connection.

    The ledger owns the maintenance checkpoint.  Sidecars must therefore not
    independently auto-checkpoint a multi-megabyte WAL on a visible reply.
    Callers invoke this while holding :func:`sqlite_write_lock` during
    construction, before any write transaction is opened.
    """

    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")


__all__ = ["configure_shared_sqlite_connection", "sqlite_write_lock"]
