from companion_daemon.proactive_scheduler import _minutes_since


def test_minutes_since_none() -> None:
    assert _minutes_since(None) is None
