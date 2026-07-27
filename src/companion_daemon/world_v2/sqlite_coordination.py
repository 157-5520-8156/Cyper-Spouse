"""Cross-process coordination for SQLite stores sharing one World-v2 file.

World-v2 deliberately keeps the ledger and opaque sidecars as separate
modules, but production composes several of them over the same SQLite path.
SQLite serializes individual transactions at the file level.  Some World-v2
protocols deliberately use two adjacent transactions, however, so a stable
sidecar file lock also keeps those sequences adjacent across daemon processes.
SQLite transactions and CAS checks remain the authority for every individual
commit.
"""

import fcntl
import os
from pathlib import Path
import sqlite3
from threading import RLock
from types import TracebackType
from typing import Self


_LOCKS_GUARD = RLock()
_WRITE_LOCKS: dict[str, "_SQLiteWriteLock"] = {}


class _SQLiteWriteLock:
    """One re-entrant thread lock backed by an inter-process advisory lock."""

    def __init__(self, database_path: Path) -> None:
        self._thread_lock = RLock()
        self._lock_path = Path(f"{database_path}.writer.lock")
        self._owner_pid: int | None = None
        self._depth = 0
        self._fd: int | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._thread_lock.acquire(blocking, timeout)
        if not acquired:
            return False
        try:
            pid = os.getpid()
            if self._depth and self._owner_pid == pid:
                self._depth += 1
                return True
            # A process created by fork must not share the parent's open file
            # description: flock locks attach to that description on supported
            # Unix platforms.  Opening a new descriptor restores isolation.
            if self._owner_pid != pid and self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            if self._fd is None:
                self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(self._fd, operation)
            except BlockingIOError:
                self._thread_lock.release()
                return False
            self._owner_pid = pid
            self._depth = 1
            return True
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if self._depth < 1 or self._owner_pid != os.getpid():
            raise RuntimeError("cannot release an un-acquired SQLite writer lock")
        self._depth -= 1
        try:
            if self._depth == 0:
                if self._fd is None:
                    raise RuntimeError("SQLite writer lock lost its file descriptor")
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._owner_pid = None
        finally:
            self._thread_lock.release()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def sqlite_write_lock(path: str | Path) -> _SQLiteWriteLock:
    """Return the stable cross-process writer lock for ``path``."""

    key = str(Path(path).expanduser().absolute())
    with _LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = _SQLiteWriteLock(Path(key))
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
