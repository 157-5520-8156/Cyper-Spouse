from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from companion_daemon.world_v2.sqlite_coordination import sqlite_write_lock


_TRY_LOCK = """
from pathlib import Path
import sys
from companion_daemon.world_v2.sqlite_coordination import sqlite_write_lock

lock = sqlite_write_lock(Path(sys.argv[1]))
acquired = lock.acquire(blocking=False)
print("acquired" if acquired else "busy")
if acquired:
    lock.release()
"""


def _try_lock_in_child(path: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _TRY_LOCK, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_sqlite_writer_lock_serializes_independent_processes(tmp_path: Path) -> None:
    database_path = tmp_path / "world.sqlite3"
    lock = sqlite_write_lock(database_path)

    assert lock.acquire()
    try:
        assert _try_lock_in_child(database_path) == "busy"
    finally:
        lock.release()

    assert _try_lock_in_child(database_path) == "acquired"
