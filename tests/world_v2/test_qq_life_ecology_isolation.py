from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.qq_c2c_host import QQC2CHost
from companion_daemon.world_v2.qq_ingress_policy import SQLiteQQIngressStore


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


class _BlockingLifePlatformHost:
    """Tiny platform seam that makes an inline Life call observably block."""

    def __init__(self) -> None:
        self.logical_time = NOW
        self.entered_life = asyncio.Event()
        self.release_life = asyncio.Event()
        self.entered_clock_tick = asyncio.Event()
        self.release_clock_tick = asyncio.Event()
        self.entered_scheduled_drain = asyncio.Event()
        self.release_scheduled_drain = asyncio.Event()
        self.life_cancelled = asyncio.Event()
        self.block_clock_tick = False
        self.block_scheduled_drain = False
        self.ticks: list[object] = []
        self.life_wakes: list[tuple[str, str, str]] = []
        self.closed = False

    async def current_logical_time(self) -> datetime:
        return self.logical_time

    async def tick(self, tick):  # type: ignore[no-untyped-def]
        self.ticks.append(tick)
        self.logical_time = tick.logical_time_to
        if self.block_clock_tick:
            self.entered_clock_tick.set()
            await self.release_clock_tick.wait()
        # This reproduces the old WorldV2PlatformHost contract: a QQ tick that
        # forwards run_life_ecology=True does not return until the model-backed
        # Life lane returns.
        if tick.run_life_ecology:
            self.entered_life.set()
            await self.release_life.wait()
        return SimpleNamespace(
            status="observed_only",
            authorized_action_ids=(),
            scheduled_action_ids=(),
        )

    async def advance_life_ecology_once(
        self,
        *,
        wake_event_ref: str,
        trace_id: str,
        correlation_id: str,
    ) -> object:
        self.life_wakes.append((wake_event_ref, trace_id, correlation_id))
        self.entered_life.set()
        try:
            await self.release_life.wait()
        except asyncio.CancelledError:
            self.life_cancelled.set()
            raise
        return SimpleNamespace(status="completed")

    async def inbound(self, _message):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            status="observed_only",
            authorized_action_ids=(),
            scheduled_action_ids=(),
        )

    async def drain_background_once(self) -> None:
        return None

    async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
        if self.block_scheduled_drain:
            self.entered_scheduled_drain.set()
            await self.release_scheduled_drain.wait()
        return SimpleNamespace(action_statuses=(), background_statuses=())

    def close(self) -> None:
        self.closed = True


def _qq_host(
    *,
    platform: _BlockingLifePlatformHost,
    database_path: Path,
    close_grace_seconds: float = 1.0,
) -> QQC2CHost:
    wall = {"now": NOW}

    async def advance_wall(delay: float) -> None:
        wall["now"] += timedelta(seconds=max(delay, 0.001))
        await asyncio.sleep(0)

    return QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(database_path),
        ingress_now=lambda: wall["now"],
        ingress_sleep=advance_wall,
        idle_heartbeat_seconds=0,
        owned_action_close_grace_seconds=close_grace_seconds,
    )


@pytest.mark.asyncio
async def test_direct_qq_tick_releases_clock_mutex_before_exact_life_wake(
    tmp_path: Path,
) -> None:
    platform = _BlockingLifePlatformHost()
    host = _qq_host(platform=platform, database_path=tmp_path / "direct-life.sqlite")
    life_tick = asyncio.create_task(
        host.tick(
            tick_id="tick:direct-life",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=10),
            observed_at=NOW + timedelta(minutes=10),
            reason="test_direct_life",
            run_life_ecology=True,
        )
    )
    try:
        await asyncio.wait_for(platform.entered_life.wait(), timeout=1)

        # A Life provider may remain pending for many seconds.  That must not
        # retain QQ's short clock-CAS mutex or prevent another exact clock
        # observation from entering the adapter.
        clock_only = await asyncio.wait_for(
            host.tick(
                tick_id="tick:while-life-pending",
                logical_time_from=NOW + timedelta(minutes=10),
                logical_time_to=NOW + timedelta(minutes=11),
                observed_at=NOW + timedelta(minutes=11),
                reason="test_clock_only",
                run_life_ecology=False,
            ),
            timeout=0.25,
        )
        assert clock_only == "observed_only"

        # Visible ingress has a separate lane and must remain responsive while
        # the same Life reviewer is still suspended.
        inbound = await asyncio.wait_for(
            host.inbound_text(
                message_id="message:while-life-pending",
                recipient_id="10001",
                text="你在吗？",
                observed_at=NOW + timedelta(minutes=10, seconds=1),
            ),
            timeout=1,
        )
        assert inbound.status == "observed_only"
    finally:
        platform.release_life.set()
        await asyncio.wait_for(life_tick, timeout=1)
        await host.aclose()

    assert all(tick.run_life_ecology is False for tick in platform.ticks)
    assert platform.life_wakes == [
        (
            "event:trigger:clock:tick:direct-life",
            "trace:qq-c2c-v2:tick:tick:direct-life",
            "clock:qq-c2c-v2:10001",
        )
    ]


@pytest.mark.asyncio
async def test_scheduler_heartbeat_releases_clock_mutex_before_exact_life_wake(
    tmp_path: Path,
) -> None:
    platform = _BlockingLifePlatformHost()
    host = _qq_host(platform=platform, database_path=tmp_path / "scheduler-life.sqlite")
    scheduler = asyncio.create_task(
        host.scheduler_once(
            observed_at=NOW + timedelta(minutes=10),
            max_action_units=0,
            max_background_units=0,
        )
    )
    try:
        await asyncio.wait_for(platform.entered_life.wait(), timeout=1)

        clock_only = await asyncio.wait_for(
            host.tick(
                tick_id="tick:parallel-clock",
                logical_time_from=NOW + timedelta(minutes=10),
                logical_time_to=NOW + timedelta(minutes=11),
                observed_at=NOW + timedelta(minutes=11),
                reason="test_parallel_clock",
                run_life_ecology=False,
            ),
            timeout=0.25,
        )
        assert clock_only == "observed_only"

        inbound = await asyncio.wait_for(
            host.inbound_text(
                message_id="message:during-scheduler-life",
                recipient_id="10001",
                text="刚才说到哪了？",
                observed_at=NOW + timedelta(minutes=10, seconds=1),
            ),
            timeout=1,
        )
        assert inbound.status == "observed_only"
    finally:
        platform.release_life.set()
        await asyncio.wait_for(scheduler, timeout=1)
        await host.aclose()

    assert all(tick.run_life_ecology is False for tick in platform.ticks)
    assert platform.life_wakes == [
        (
            "event:trigger:clock:tick:qq-c2c-v2:2026-07-31T12:10:00+00:00",
            "trace:qq-c2c-v2:tick:qq-c2c-v2:2026-07-31T12:10:00+00:00",
            "clock:qq-c2c-v2:10001",
        )
    ]


@pytest.mark.asyncio
async def test_qq_close_waits_for_an_admitted_direct_life_wake(
    tmp_path: Path,
) -> None:
    platform = _BlockingLifePlatformHost()
    host = _qq_host(platform=platform, database_path=tmp_path / "close-direct-life.sqlite")
    tick = asyncio.create_task(
        host.tick(
            tick_id="tick:close-direct-life",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=10),
            observed_at=NOW + timedelta(minutes=10),
            reason="test_close_direct_life",
        )
    )
    close: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.entered_life.wait(), timeout=1)
        close = asyncio.create_task(host.aclose())
        await asyncio.sleep(0.05)

        assert close.done() is False
        assert platform.closed is False
    finally:
        platform.release_life.set()
        await asyncio.wait_for(tick, timeout=1)
        if close is not None:
            await asyncio.wait_for(close, timeout=1)

    assert platform.closed is True


@pytest.mark.asyncio
async def test_qq_close_cannot_overtake_an_admitted_scheduler_between_lanes(
    tmp_path: Path,
) -> None:
    platform = _BlockingLifePlatformHost()
    platform.block_clock_tick = True
    platform.block_scheduled_drain = True
    platform.release_life.set()
    host = _qq_host(platform=platform, database_path=tmp_path / "close-scheduler.sqlite")
    scheduler = asyncio.create_task(
        host.scheduler_once(
            observed_at=NOW + timedelta(minutes=10),
            max_action_units=0,
            max_background_units=0,
        )
    )
    drain: asyncio.Task[object] | None = None
    close: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.entered_clock_tick.wait(), timeout=1)
        # Hold the scheduler lane with an independently admitted pass while the
        # heartbeat owns only the clock lane. Once the tick returns, the
        # heartbeat is precisely in the old lock-to-lock shutdown gap.
        drain = asyncio.create_task(
            host.drain(max_action_units=0, max_background_units=0)
        )
        await asyncio.wait_for(platform.entered_scheduled_drain.wait(), timeout=1)
        platform.release_clock_tick.set()
        await asyncio.sleep(0)

        close = asyncio.create_task(host.aclose())
        await asyncio.sleep(0.05)

        assert close.done() is False
        assert platform.closed is False
    finally:
        platform.release_clock_tick.set()
        platform.release_scheduled_drain.set()
        if drain is not None:
            await asyncio.wait_for(drain, timeout=1)
        await asyncio.wait_for(scheduler, timeout=1)
        if close is not None:
            await asyncio.wait_for(close, timeout=1)

    assert platform.closed is True


@pytest.mark.asyncio
async def test_qq_close_gate_rejects_new_scheduler_lane_work(
    tmp_path: Path,
) -> None:
    platform = _BlockingLifePlatformHost()
    host = _qq_host(platform=platform, database_path=tmp_path / "close-gate.sqlite")
    tick = asyncio.create_task(
        host.tick(
            tick_id="tick:hold-close-gate",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=10),
            observed_at=NOW + timedelta(minutes=10),
            reason="test_close_gate",
        )
    )
    close: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.entered_life.wait(), timeout=1)
        close = asyncio.create_task(host.aclose())
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="QQ C2C host is closing"):
            await host.tick(
                tick_id="tick:after-close",
                logical_time_from=NOW + timedelta(minutes=10),
                logical_time_to=NOW + timedelta(minutes=11),
                observed_at=NOW + timedelta(minutes=11),
                reason="test_after_close",
                run_life_ecology=False,
            )
        with pytest.raises(RuntimeError, match="QQ C2C host is closing"):
            await host.scheduler_once(
                observed_at=NOW + timedelta(minutes=11),
                max_action_units=0,
                max_background_units=0,
            )
        with pytest.raises(RuntimeError, match="QQ C2C host is closing"):
            await host.drain_ingress_once()
        with pytest.raises(RuntimeError, match="QQ C2C host is closing"):
            await host.drain(max_action_units=0, max_background_units=0)
        with pytest.raises(RuntimeError, match="QQ C2C host is closing"):
            await host.maintain_wal_once()
    finally:
        platform.release_life.set()
        await asyncio.wait_for(tick, timeout=1)
        if close is not None:
            await asyncio.wait_for(close, timeout=1)


@pytest.mark.asyncio
async def test_qq_close_bounds_an_unreturned_life_wake_for_durable_recovery(
    tmp_path: Path,
) -> None:
    platform = _BlockingLifePlatformHost()
    host = _qq_host(
        platform=platform,
        database_path=tmp_path / "close-life-grace.sqlite",
        close_grace_seconds=0.01,
    )
    tick = asyncio.create_task(
        host.tick(
            tick_id="tick:close-life-grace",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=10),
            observed_at=NOW + timedelta(minutes=10),
            reason="test_close_life_grace",
        )
    )
    await asyncio.wait_for(platform.entered_life.wait(), timeout=1)

    await asyncio.wait_for(host.aclose(), timeout=1)
    result = await asyncio.gather(tick, return_exceptions=True)

    assert platform.life_cancelled.is_set()
    assert platform.closed is True
    assert isinstance(result[0], asyncio.CancelledError)
