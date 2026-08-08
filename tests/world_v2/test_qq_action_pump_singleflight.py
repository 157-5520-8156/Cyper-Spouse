from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.action_due_wake import ActionDueWake
from companion_daemon.world_v2.errors import ConcurrencyConflict, IdempotencyConflict
from companion_daemon.world_v2.qq_c2c_host import QQC2CHost
from companion_daemon.world_v2.qq_ingress_policy import MemoryQQIngressStore


class _ConcurrentDueWakeHost:
    """Expose one due Action to two same-process wake callers."""

    def __init__(self) -> None:
        self.due_at = datetime.now(UTC) - timedelta(milliseconds=10)
        self.due_enabled = False
        self.action_state = "scheduled"
        self.terminal = False
        self.initial_projection_read = asyncio.Event()
        self.pump_entered = asyncio.Event()
        self.release_pump = asyncio.Event()
        self.pump_calls = 0
        self.effects = 0

    async def action_due_projection(self) -> SimpleNamespace:
        self.initial_projection_read.set()
        actions = ()
        if self.due_enabled and not self.terminal:
            actions = (
                SimpleNamespace(
                    action_id="action:single-flight",
                    state=self.action_state,
                    not_before=self.due_at,
                    claim_lease=None,
                ),
            )
        return SimpleNamespace(actions=actions)

    async def current_logical_time(self) -> datetime:
        return self.due_at

    async def drain_actions_once(self) -> SimpleNamespace:
        self.pump_calls += 1
        self.pump_entered.set()
        await self.release_pump.wait()
        if not self.terminal:
            self.terminal = True
            self.effects += 1
        return SimpleNamespace(status="idle")

    def close(self) -> None:
        return None


class _TransientActionPumpConflictHost(_ConcurrentDueWakeHost):
    async def drain_actions_once(self) -> SimpleNamespace:
        self.pump_calls += 1
        if self.pump_calls == 1:
            self.pump_entered.set()
            await self.release_pump.wait()
            raise ConcurrencyConflict("simulated stale ActionPump cursor")
        if not self.terminal:
            self.terminal = True
            self.effects += 1
        return SimpleNamespace(status="idle")

    async def drain_scheduled_work(self, **kwargs: object) -> SimpleNamespace:
        action_pump_once = kwargs.get("action_pump_once")
        if not callable(action_pump_once):
            raise AssertionError("QQ drain must provide its ActionPump gate")
        await action_pump_once()
        return SimpleNamespace(action_statuses=("idle",), background_statuses=())


class _PersistentActionPumpConflictHost(_TransientActionPumpConflictHost):
    async def drain_actions_once(self) -> SimpleNamespace:
        self.pump_calls += 1
        raise ConcurrencyConflict("simulated persistent ActionPump contention")


class _ActionPumpProgrammingFailureHost(_TransientActionPumpConflictHost):
    async def drain_actions_once(self) -> SimpleNamespace:
        self.pump_calls += 1
        raise RuntimeError("simulated ActionPump programming failure")


class _VirtualRetryClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        normalized = max(0.0, seconds)
        self.sleeps.append(normalized)
        self.current += timedelta(seconds=normalized)
        await asyncio.sleep(0)


class _FutureActionCreationHost:
    def __init__(self) -> None:
        self.due_at = datetime(2026, 7, 30, 7, 0, tzinfo=UTC)
        self.action_created = False
        self.initial_projection_read = asyncio.Event()

    async def action_due_projection(self) -> SimpleNamespace:
        if not self.action_created:
            self.initial_projection_read.set()
            actions = ()
        else:
            actions = (
                SimpleNamespace(
                    action_id="action:created-by-background",
                    state="scheduled",
                    not_before=self.due_at,
                    claim_lease=None,
                ),
            )
        return SimpleNamespace(
            actions=actions,
            logical_time=datetime(2026, 7, 30, 6, 0, tzinfo=UTC),
            trigger_processes=(),
        )

    async def drain_scheduled_work(self, **_kwargs: object) -> SimpleNamespace:
        self.action_created = True
        return SimpleNamespace(action_statuses=(), background_statuses=("processed",))

    async def drain_background_once(self) -> SimpleNamespace:
        self.action_created = True
        return SimpleNamespace(status="processed", work_status="created_action")

    async def current_logical_time(self) -> datetime:
        return datetime(2026, 7, 30, 6, 0, tzinfo=UTC)

    def close(self) -> None:
        return None


class _DueTimerProbe:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)
        self.delays: list[float] = []
        self.armed = asyncio.Event()
        self.release = asyncio.Event()

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.armed.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_public_drain_rearms_due_timer_after_background_creates_future_action() -> None:
    platform = _FutureActionCreationHost()
    clock = _DueTimerProbe()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        action_due_now=clock.now,
        action_due_sleep=clock.sleep,
    )
    try:
        await asyncio.wait_for(platform.initial_projection_read.wait(), timeout=1)

        await host.drain(max_action_units=0, max_background_units=1)
        await asyncio.wait_for(clock.armed.wait(), timeout=0.1)

        assert clock.delays == [3_600.05]
    finally:
        clock.release.set()
        await host.aclose()


@pytest.mark.asyncio
async def test_scheduler_rearms_due_timer_after_background_creates_future_action() -> None:
    platform = _FutureActionCreationHost()
    clock = _DueTimerProbe()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        action_due_now=clock.now,
        action_due_sleep=clock.sleep,
        idle_heartbeat_seconds=600,
    )
    try:
        await asyncio.wait_for(platform.initial_projection_read.wait(), timeout=1)

        await host.scheduler_once(
            observed_at=clock.now(),
            max_action_units=0,
            max_background_units=1,
        )
        await asyncio.wait_for(clock.armed.wait(), timeout=0.1)

        assert clock.delays == [3_600.05]
    finally:
        clock.release.set()
        await host.aclose()


@pytest.mark.asyncio
async def test_concurrent_real_clock_due_wakes_join_one_action_pump() -> None:
    platform = _ConcurrentDueWakeHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    first: asyncio.Task[None] | None = None
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.initial_projection_read.wait(), timeout=1)
        platform.due_enabled = True
        first = asyncio.create_task(host._wake_due_actions())  # noqa: SLF001
        second = asyncio.create_task(host._wake_due_actions())  # noqa: SLF001
        await asyncio.wait_for(platform.pump_entered.wait(), timeout=1)
        await asyncio.sleep(0)
        platform.release_pump.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

        assert platform.pump_calls == 1
        assert platform.effects == 1
    finally:
        platform.release_pump.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_state", ["authorized", "scheduled"])
@pytest.mark.asyncio
async def test_exact_due_new_action_dispatch_is_not_held_by_visible_turn(
    action_state: str,
) -> None:
    """The old-receipt gate must never become a generic Action gate."""

    platform = _ConcurrentDueWakeHost()
    platform.action_state = action_state
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    wake: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.initial_projection_read.wait(), timeout=1)
        async with host._provider_reconciliation_gate.visible_turn():  # noqa: SLF001
            platform.due_enabled = True
            wake = asyncio.create_task(host._wake_due_actions())  # noqa: SLF001
            await asyncio.wait_for(platform.pump_entered.wait(), timeout=0.1)
            assert platform.pump_calls == 1
            platform.release_pump.set()
            await asyncio.wait_for(wake, timeout=1)

        assert platform.effects == 1
    finally:
        platform.release_pump.set()
        if wake is not None and not wake.done():
            wake.cancel()
            await asyncio.gather(wake, return_exceptions=True)
        await host.aclose()


@pytest.mark.asyncio
async def test_due_wake_and_explicit_drain_share_a_transient_cas_retry() -> None:
    platform = _TransientActionPumpConflictHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    explicit: asyncio.Task[object] | None = None
    wake: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.initial_projection_read.wait(), timeout=1)
        platform.due_enabled = True
        explicit = asyncio.create_task(
            host.drain(max_action_units=1, max_background_units=0)
        )
        await asyncio.wait_for(platform.pump_entered.wait(), timeout=1)
        wake = asyncio.create_task(host._wake_due_actions())  # noqa: SLF001
        await asyncio.sleep(0)
        platform.release_pump.set()

        results = await asyncio.wait_for(
            asyncio.gather(explicit, wake, return_exceptions=True),
            timeout=1,
        )

        assert not any(isinstance(item, BaseException) for item in results)
        assert platform.pump_calls == 2
        assert platform.effects == 1
    finally:
        platform.release_pump.set()
        for task in (explicit, wake):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (explicit, wake) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_persistent_action_pump_conflict_is_bounded_and_propagated() -> None:
    platform = _PersistentActionPumpConflictHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    try:
        with pytest.raises(
            ConcurrencyConflict,
            match="persistent ActionPump contention",
        ):
            await host.drain(max_action_units=1, max_background_units=0)

        assert platform.pump_calls == 3
        assert platform.effects == 0
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_non_cas_action_pump_failure_is_not_retried_or_swallowed() -> None:
    platform = _ActionPumpProgrammingFailureHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="ActionPump programming failure",
        ):
            await host.drain(max_action_units=1, max_background_units=0)

        assert platform.pump_calls == 1
        assert platform.effects == 0
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_virtual_clock_wake_failures_back_off_once_then_wait_for_rearm() -> None:
    clock = _VirtualRetryClock()
    action = SimpleNamespace(
        state="scheduled",
        not_before=clock.now(),
        claim_lease=None,
    )
    attempts = 0
    should_fail = True

    async def wake() -> None:
        nonlocal attempts
        attempts += 1
        if should_fail:
            raise ConcurrencyConflict("simulated due wake contention")
        action.state = "delivered"

    timer = ActionDueWake(
        project=lambda: SimpleNamespace(actions=(action,)),
        wake=wake,
        now=clock.now,
        sleep=clock.sleep,
        coalesce_seconds=0,
    )
    try:
        await timer.refresh()
        for _ in range(40):
            await asyncio.sleep(0)

        assert attempts == 4
        assert [delay for delay in clock.sleeps if delay > 0] == [1.0, 5.0, 30.0]
        assert timer.diagnostics()["failure_count"] == 4
        assert timer.diagnostics()["permanent_failure_count"] == 0

        should_fail = False
        await timer.refresh()
        for _ in range(10):
            await asyncio.sleep(0)
            if action.state == "delivered":
                break

        assert attempts == 5
        assert action.state == "delivered"
    finally:
        await timer.aclose()


@pytest.mark.asyncio
async def test_due_wake_treats_idempotency_conflict_as_permanent_without_retry() -> None:
    clock = _VirtualRetryClock()
    action = SimpleNamespace(
        state="scheduled",
        not_before=clock.now(),
        claim_lease=None,
    )
    attempts = 0

    async def wake() -> None:
        nonlocal attempts
        attempts += 1
        raise IdempotencyConflict("immutable provider identity was reused")

    timer = ActionDueWake(
        project=lambda: SimpleNamespace(actions=(action,)),
        wake=wake,
        now=clock.now,
        sleep=clock.sleep,
        coalesce_seconds=0,
    )
    try:
        await timer.refresh()
        for _ in range(10):
            await asyncio.sleep(0)

        assert attempts == 1
        assert [delay for delay in clock.sleeps if delay > 0] == []
        assert timer.diagnostics()["failure_count"] == 1
        assert timer.diagnostics()["permanent_failure_count"] == 1
    finally:
        await timer.aclose()
