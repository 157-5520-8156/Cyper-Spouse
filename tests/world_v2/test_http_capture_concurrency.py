from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.http_capture_host import (
    HttpCaptureTransport,
    HttpV2CaptureHost,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class _BlockingHttpPlatformHost:
    def __init__(self) -> None:
        self.first_inbound_entered = asyncio.Event()
        self.release_first_inbound = asyncio.Event()
        self.life_entered = asyncio.Event()
        self.release_life = asyncio.Event()
        self.drain_entered = asyncio.Event()
        self.release_drain = asyncio.Event()
        self.target_drain_entered = asyncio.Event()
        self.release_target_drain = asyncio.Event()
        self.target_drain_calls = 0
        self.wal_entered = asyncio.Event()
        self.release_wal = asyncio.Event()
        self.ticks: list[object] = []
        self.life_wakes: list[tuple[str, str, str]] = []
        self.closed = False

    async def inbound(self, message):  # type: ignore[no-untyped-def]
        if message.platform_message_id == "message:first":
            self.first_inbound_entered.set()
            await self.release_first_inbound.wait()
        action_ids = (
            ("action:http-blocked",)
            if message.platform_message_id == "message:action"
            else ()
        )
        return SimpleNamespace(
            status="observed_only",
            authorized_action_ids=action_ids,
            scheduled_action_ids=(),
        )

    async def drain_action(self, action_id: str) -> object:
        self.target_drain_calls += 1
        self.target_drain_entered.set()
        await self.release_target_drain.wait()
        return SimpleNamespace(action_id=action_id, status="settled")

    async def tick(self, tick):  # type: ignore[no-untyped-def]
        self.ticks.append(tick)
        if tick.run_life_ecology:
            self.life_entered.set()
            await self.release_life.wait()
        return SimpleNamespace(status="observed_only")

    async def advance_life_ecology_once(
        self,
        *,
        wake_event_ref: str,
        trace_id: str,
        correlation_id: str,
    ) -> object:
        self.life_wakes.append((wake_event_ref, trace_id, correlation_id))
        self.life_entered.set()
        await self.release_life.wait()
        return SimpleNamespace(status="completed")

    async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.drain_entered.set()
        await self.release_drain.wait()
        return SimpleNamespace(action_statuses=(), background_statuses=())

    async def maintain_wal_once(self) -> object:
        self.wal_entered.set()
        await self.release_wal.wait()
        return SimpleNamespace(status="skipped")

    def close(self) -> None:
        self.closed = True


def _capture(platform: _BlockingHttpPlatformHost) -> HttpV2CaptureHost:
    return HttpV2CaptureHost(
        host=platform,  # type: ignore[arg-type]
        transport=HttpCaptureTransport(),
        primary_user_id="geoff",
    )


@pytest.mark.asyncio
async def test_slow_http_inbound_does_not_serialize_an_independent_inbound() -> None:
    platform = _BlockingHttpPlatformHost()
    capture = _capture(platform)
    first = asyncio.create_task(
        capture.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:first",
            text="第一条",
            observed_at=NOW,
        )
    )
    try:
        await asyncio.wait_for(platform.first_inbound_entered.wait(), timeout=1)
        second = await asyncio.wait_for(
            capture.respond(
                platform="simulator",
                platform_user_id="geoff",
                platform_message_id="message:second",
                text="第二条",
                observed_at=NOW + timedelta(seconds=1),
            ),
            timeout=0.25,
        )
        assert second.status == "observed_only"
    finally:
        platform.release_first_inbound.set()
        await asyncio.wait_for(first, timeout=1)
        await capture.aclose()


@pytest.mark.asyncio
async def test_slow_targeted_http_action_does_not_serialize_next_inbound() -> None:
    platform = _BlockingHttpPlatformHost()
    capture = _capture(platform)
    first = asyncio.create_task(
        capture.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:action",
            text="第一条会等自己的 Action",
            observed_at=NOW,
        )
    )
    try:
        await asyncio.wait_for(platform.target_drain_entered.wait(), timeout=1)
        second = await asyncio.wait_for(
            capture.respond(
                platform="simulator",
                platform_user_id="geoff",
                platform_message_id="message:second",
                text="第二条仍然能进入",
                observed_at=NOW + timedelta(seconds=1),
            ),
            timeout=0.25,
        )
        assert second.status == "observed_only"
    finally:
        platform.release_target_drain.set()
        await asyncio.wait_for(first, timeout=1)
        await capture.aclose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_http_ingress_joins_one_targeted_action() -> None:
    platform = _BlockingHttpPlatformHost()
    capture = _capture(platform)
    first = asyncio.create_task(
        capture.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:action",
            text="同一条消息",
            observed_at=NOW,
        )
    )
    second: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(platform.target_drain_entered.wait(), timeout=1)
        second = asyncio.create_task(
            capture.respond(
                platform="simulator",
                platform_user_id="geoff",
                platform_message_id="message:action",
                text="同一条消息",
                observed_at=NOW,
            )
        )
        await asyncio.sleep(0.05)

        assert platform.target_drain_calls == 1
    finally:
        platform.release_target_drain.set()
        await asyncio.wait_for(first, timeout=1)
        if second is not None:
            await asyncio.wait_for(second, timeout=1)
        await capture.aclose()


@pytest.mark.asyncio
async def test_http_tick_runs_exact_life_wake_outside_ingress_mutex() -> None:
    platform = _BlockingHttpPlatformHost()
    capture = _capture(platform)
    tick = asyncio.create_task(
        capture.tick(
            tick_id="tick:http-life",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=10),
            observed_at=NOW + timedelta(minutes=10),
            trace_id="trace:http-life",
            causation_id="scheduler:http-life",
            correlation_id="clock:http-life",
            reason="test_http_life",
        )
    )
    try:
        await asyncio.wait_for(platform.life_entered.wait(), timeout=1)
        inbound = await asyncio.wait_for(
            capture.respond(
                platform="simulator",
                platform_user_id="geoff",
                platform_message_id="message:during-life",
                text="你还在吗？",
                observed_at=NOW + timedelta(minutes=10, seconds=1),
            ),
            timeout=0.25,
        )
        assert inbound.status == "observed_only"
    finally:
        platform.release_life.set()
        await asyncio.wait_for(tick, timeout=1)
        await capture.aclose()

    assert len(platform.ticks) == 1
    assert platform.ticks[0].run_life_ecology is False
    assert platform.life_wakes == [
        (
            "event:trigger:clock:tick:http-life",
            "trace:http-life",
            "clock:http-life",
        )
    ]


@pytest.mark.asyncio
async def test_http_close_closes_ingress_gate_then_joins_owned_visible_response() -> None:
    platform = _BlockingHttpPlatformHost()
    capture = _capture(platform)
    response = asyncio.create_task(
        capture.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:first",
            text="还没说完",
            observed_at=NOW,
        )
    )
    close: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.first_inbound_entered.wait(), timeout=1)
        close = asyncio.create_task(capture.aclose())
        # Give the close owner enough event-loop turns to reach its host-close
        # boundary.  It must remain parked on the already-owned response.
        await asyncio.sleep(0.05)

        assert not close.done()
        assert platform.closed is False
        with pytest.raises(RuntimeError, match="HTTP capture host is closing"):
            await capture.respond(
                platform="simulator",
                platform_user_id="geoff",
                platform_message_id="message:after-close",
                text="这条不能再进来了",
                observed_at=NOW + timedelta(seconds=1),
            )
    finally:
        platform.release_first_inbound.set()
        await asyncio.wait_for(response, timeout=1)
        if close is not None:
            await asyncio.wait_for(close, timeout=1)

    assert platform.closed is True


@pytest.mark.asyncio
async def test_http_close_waits_for_admitted_life_tick_before_closing_world() -> None:
    platform = _BlockingHttpPlatformHost()
    capture = _capture(platform)
    tick = asyncio.create_task(
        capture.tick(
            tick_id="tick:close-life",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=10),
            observed_at=NOW + timedelta(minutes=10),
            trace_id="trace:close-life",
            causation_id="scheduler:close-life",
            correlation_id="clock:close-life",
            reason="test_close_life",
        )
    )
    close: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.life_entered.wait(), timeout=1)
        close = asyncio.create_task(capture.aclose())
        await asyncio.sleep(0.05)

        assert not close.done()
        assert platform.closed is False
        with pytest.raises(RuntimeError, match="HTTP capture host is closing"):
            await asyncio.wait_for(
                capture.drain(max_action_units=0, max_background_units=0),
                timeout=0.1,
            )
    finally:
        platform.release_drain.set()
        platform.release_life.set()
        await asyncio.wait_for(tick, timeout=1)
        if close is not None:
            await asyncio.wait_for(close, timeout=1)

    assert platform.closed is True


@pytest.mark.asyncio
async def test_http_close_waits_for_admitted_direct_drain() -> None:
    platform = _BlockingHttpPlatformHost()
    capture = _capture(platform)
    drain = asyncio.create_task(
        capture.drain(max_action_units=0, max_background_units=0)
    )
    close: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.drain_entered.wait(), timeout=1)
        close = asyncio.create_task(capture.aclose())
        await asyncio.sleep(0.05)

        assert not close.done()
        assert platform.closed is False
        with pytest.raises(RuntimeError, match="HTTP capture host is closing"):
            capture.schedule_background_drain(
                max_action_units=0,
                max_background_units=0,
            )

        platform.release_drain.set()
        await asyncio.wait_for(drain, timeout=1)
        await asyncio.wait_for(platform.wal_entered.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert not close.done()
        assert platform.closed is False
    finally:
        platform.release_drain.set()
        platform.release_wal.set()
        if not drain.done():
            await asyncio.wait_for(drain, timeout=1)
        if close is not None:
            await asyncio.wait_for(close, timeout=1)

    assert platform.closed is True
