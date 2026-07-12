import os

import pytest

from companion_daemon.process_lock import AlreadyRunningError, SingleInstanceLock
from companion_daemon.qq_outbound_owner import (
    QQOutboundConfigurationError,
    QQOutboundOwnerLease,
    qq_outbound_owner_lock_path,
    validate_qq_outbound_configuration,
)


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


def test_single_instance_lock_never_steals_a_fresh_lock_that_is_still_initializing(
    tmp_path,
) -> None:
    lock_path = tmp_path / "companion.lock"
    lock_path.touch()

    with pytest.raises(AlreadyRunningError, match="initialized"):
        with SingleInstanceLock(lock_path):
            pass

    assert lock_path.exists()


def test_every_qq_adapter_uses_the_same_outbound_owner_lease(tmp_path) -> None:
    lock_path = qq_outbound_owner_lock_path(tmp_path / "companion.sqlite")

    with QQOutboundOwnerLease(lock_path, adapter="official"):
        with pytest.raises(AlreadyRunningError, match="outbound owner"):
            with QQOutboundOwnerLease(lock_path, adapter="napcat"):
                pass
        with pytest.raises(AlreadyRunningError, match="outbound owner"):
            with QQOutboundOwnerLease(lock_path, adapter="onebot"):
                pass

    assert not lock_path.exists()


@pytest.mark.parametrize("adapter", ["official", "napcat", "onebot"])
def test_qq_outbound_configuration_requires_the_launched_adapter_to_own_delivery(adapter) -> None:
    validate_qq_outbound_configuration(configured_adapter=adapter, launched_adapter=adapter)

    other = next(item for item in ("official", "napcat", "onebot") if item != adapter)
    with pytest.raises(QQOutboundConfigurationError, match="QQ_ADAPTER"):
        validate_qq_outbound_configuration(configured_adapter=adapter, launched_adapter=other)


def test_qq_outbound_configuration_rejects_unknown_adapter() -> None:
    with pytest.raises(QQOutboundConfigurationError, match="unsupported"):
        validate_qq_outbound_configuration(configured_adapter="auto", launched_adapter="official")
