from __future__ import annotations

import os
import time
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class SingleInstanceLock:
    _INITIALIZATION_GRACE_SECONDS = 2.0

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self) -> SingleInstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                existing_pid = self._read_pid()
                if existing_pid and _pid_is_alive(existing_pid):
                    raise AlreadyRunningError(
                        f"another process is already running with pid {existing_pid}"
                    )
                if existing_pid is None and self._is_fresh():
                    raise AlreadyRunningError("another process lock is still being initialized")
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            self.acquired = True
            return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            if self._read_pid() == os.getpid():
                self.path.unlink(missing_ok=True)
            self.acquired = False

    def _read_pid(self) -> int | None:
        try:
            raw = self.path.read_text().strip()
        except OSError:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _is_fresh(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        return age < self._INITIALIZATION_GRACE_SECONDS


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
