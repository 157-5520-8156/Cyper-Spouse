from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.world_v2.system_notice import (
    SYSTEM_NOTICE_TEXT,
    SQLiteSystemNoticeDispatcher,
)


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


class _Delivery:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.calls += 1
        assert recipient_id == "10001"
        assert text == SYSTEM_NOTICE_TEXT
        await self.release.wait()
        return {"status": "ok", "data": {"message_id": "notice-1"}}


@pytest.mark.asyncio
async def test_system_notice_is_effect_once_across_concurrency_and_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notice.sqlite"
    delivery = _Delivery()
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(path), world_id="world:test", delivery=delivery, now=lambda: NOW
    )
    first = asyncio.create_task(
        dispatcher.notify(
            notice_key="notice:turn:1",
            recipient_id="10001",
            failure_code="turn_technical_failure",
        )
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        dispatcher.notify(
            notice_key="notice:turn:1",
            recipient_id="10001",
            failure_code="turn_technical_failure",
        )
    )
    delivery.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.status == "provider_accepted"
    assert second_result.status == "already_attempted"
    assert delivery.calls == 1
    dispatcher.close()

    restarted = SQLiteSystemNoticeDispatcher(
        path=str(path), world_id="world:test", delivery=delivery, now=lambda: NOW
    )
    replay = await restarted.notify(
        notice_key="notice:turn:1",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )
    assert replay.status == "already_attempted"
    assert delivery.calls == 1
    restarted.close()


@pytest.mark.asyncio
async def test_system_notice_persistently_rate_limits_distinct_failures(tmp_path: Path) -> None:
    clock = {"now": NOW}
    delivery = _Delivery()
    delivery.release.set()
    dispatcher = SQLiteSystemNoticeDispatcher(
        path=str(tmp_path / "notice-rate.sqlite"),
        world_id="world:test",
        delivery=delivery,
        now=lambda: clock["now"],
        cooldown_seconds=60,
    )

    first = await dispatcher.notify(
        notice_key="notice:turn:1",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )
    clock["now"] += timedelta(seconds=30)
    second = await dispatcher.notify(
        notice_key="notice:turn:2",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )
    clock["now"] += timedelta(seconds=31)
    third = await dispatcher.notify(
        notice_key="notice:turn:3",
        recipient_id="10001",
        failure_code="turn_technical_failure",
    )

    assert (first.status, second.status, third.status) == (
        "provider_accepted",
        "suppressed",
        "provider_accepted",
    )
    assert delivery.calls == 2
    dispatcher.close()
