import os

import pytest

from companion_daemon.process_lock import AlreadyRunningError, SingleInstanceLock


def test_single_instance_lock_rejects_second_live_owner(tmp_path) -> None:
    lock_path = tmp_path / "companion.lock"

    with SingleInstanceLock(lock_path):
        assert lock_path.read_text().strip() == str(os.getpid())
        with pytest.raises(AlreadyRunningError):
            with SingleInstanceLock(lock_path):
                pass

    assert not lock_path.exists()


def test_single_instance_lock_recovers_stale_lock(tmp_path) -> None:
    lock_path = tmp_path / "companion.lock"
    lock_path.write_text("999999999")

    with SingleInstanceLock(lock_path):
        assert lock_path.read_text().strip() == str(os.getpid())
