from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.interactive_turn_budget import (
    InteractiveTurnBudgetPolicy,
)
from companion_daemon.world_v2.qq_c2c_host import QQC2CHost
from companion_daemon.world_v2.qq_ingress_policy import (
    MemoryQQIngressStore,
    QQIngressFragment,
)


NOW = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)


class _SerializedScheduledWorkHost:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def current_logical_time(self) -> datetime:
        return NOW

    async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return SimpleNamespace(action_statuses=(), background_statuses=())

    async def drain_background_once(self):  # type: ignore[no-untyped-def]
        return None

    def close(self) -> None:
        return None


class _PacingClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)


class _DurableDispatchHost:
    def __init__(self) -> None:
        self.dispatch_started = asyncio.Event()
        self.dispatch_finished = asyncio.Event()
        self.provider_release = asyncio.Event()
        self.cancelled_after_dispatch_started = False
        self.delivered_action_ids: list[str] = []

    async def current_logical_time(self) -> datetime:
        return NOW

    async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            status="action_authorized",
            authorized_action_ids=("action:reply:one",),
            scheduled_action_ids=(),
        )

    async def drain_action(self, action_id: str):  # type: ignore[no-untyped-def]
        # This public fake seam represents ActionPump after its durable
        # ActionDispatchStarted commit and before the provider returns.
        self.dispatch_started.set()
        try:
            await self.provider_release.wait()
        except asyncio.CancelledError:
            self.cancelled_after_dispatch_started = True
            raise
        self.delivered_action_ids.append(action_id)
        self.dispatch_finished.set()
        return SimpleNamespace(
            action_id=action_id,
            action_kind="reply",
            status="settled",
            provider_status="delivered",
        )

    def close(self) -> None:
        return None


class _CountingDurableDispatchHost(_DurableDispatchHost):
    def __init__(self) -> None:
        super().__init__()
        self.inbound_calls = 0

    async def inbound(self, inbound):  # type: ignore[no-untyped-def]
        self.inbound_calls += 1
        return await super().inbound(inbound)


class _QueuedDurableDispatchHost:
    def __init__(self) -> None:
        self.provider_release = asyncio.Event()
        self.first_dispatch_started = asyncio.Event()
        self.entered_action_ids: list[str] = []
        self.cancelled_action_ids: list[str] = []
        self.delivered_action_ids: list[str] = []

    async def current_logical_time(self) -> datetime:
        return NOW

    async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            status="action_authorized",
            authorized_action_ids=(
                "action:reply:one",
                "action:reply:two",
                "action:reply:three",
            ),
            scheduled_action_ids=(),
        )

    async def drain_action(self, action_id: str):  # type: ignore[no-untyped-def]
        self.entered_action_ids.append(action_id)
        self.first_dispatch_started.set()
        try:
            await self.provider_release.wait()
        except asyncio.CancelledError:
            self.cancelled_action_ids.append(action_id)
            raise
        self.delivered_action_ids.append(action_id)
        return SimpleNamespace(
            action_id=action_id,
            action_kind="reply",
            status="settled",
            provider_status="delivered",
        )

    def close(self) -> None:
        return None


class _DueDispatchRefreshHost:
    """Expose a due provider handoff while an ordinary ingress refreshes timers."""

    def __init__(self) -> None:
        self.logical_time = NOW
        self.action_state = "scheduled"
        self.dispatch_started = asyncio.Event()
        self.dispatch_finished = asyncio.Event()
        self.provider_release = asyncio.Event()
        self.cancelled_after_dispatch_started = False
        self.delivered_action_ids: list[str] = []

    async def action_due_projection(self) -> SimpleNamespace:
        actions = ()
        if self.action_state != "terminal":
            lease = (
                SimpleNamespace(expires_at=NOW + timedelta(minutes=2))
                if self.action_state == "dispatch_started"
                else None
            )
            actions = (
                SimpleNamespace(
                    action_id="action:due",
                    state=self.action_state,
                    not_before=NOW,
                    claim_lease=lease,
                ),
            )
        return SimpleNamespace(
            actions=actions,
            logical_time=self.logical_time,
            trigger_processes=(),
        )

    async def current_logical_time(self) -> datetime:
        return self.logical_time

    async def inbound(self, _inbound: object) -> SimpleNamespace:
        return SimpleNamespace(
            status="observed_only",
            authorized_action_ids=(),
            scheduled_action_ids=(),
        )

    async def drain_actions_once(self) -> SimpleNamespace:
        if self.action_state == "terminal":
            return SimpleNamespace(status="idle")
        self.action_state = "dispatch_started"
        self.dispatch_started.set()
        try:
            await self.provider_release.wait()
        except asyncio.CancelledError:
            self.cancelled_after_dispatch_started = True
            raise
        self.action_state = "terminal"
        self.delivered_action_ids.append("action:due")
        self.dispatch_finished.set()
        return SimpleNamespace(
            action_id="action:due",
            action_kind="reply",
            status="settled",
            provider_status="delivered",
        )

    def close(self) -> None:
        return None


class _DueWakeDuringSlowBackgroundHost:
    """Keep background cognition blocked while a newly due Action wakes."""

    def __init__(self) -> None:
        self.logical_time = NOW
        self.due_enabled = False
        self.action_state = "scheduled"
        self.initial_projection_read = asyncio.Event()
        self.background_started = asyncio.Event()
        self.background_release = asyncio.Event()
        self.action_progress = asyncio.Event()
        self.delivered_action_ids: list[str] = []

    async def action_due_projection(self) -> SimpleNamespace:
        actions = ()
        if self.due_enabled and self.action_state != "terminal":
            actions = (
                SimpleNamespace(
                    action_id="action:due-during-background",
                    state=self.action_state,
                    not_before=NOW,
                    claim_lease=None,
                ),
            )
        else:
            self.initial_projection_read.set()
        return SimpleNamespace(
            actions=actions,
            logical_time=self.logical_time,
            trigger_processes=(),
        )

    async def current_logical_time(self) -> datetime:
        return self.logical_time

    async def inbound(self, _inbound: object) -> SimpleNamespace:
        return SimpleNamespace(
            status="observed_only",
            authorized_action_ids=(),
            scheduled_action_ids=(),
        )

    async def drain_scheduled_work(self, **_kwargs: object) -> SimpleNamespace:
        self.background_started.set()
        await self.background_release.wait()
        return SimpleNamespace(action_statuses=(), background_statuses=())

    async def drain_actions_once(self) -> SimpleNamespace:
        if self.action_state == "terminal":
            return SimpleNamespace(status="idle")
        self.action_state = "terminal"
        self.delivered_action_ids.append("action:due-during-background")
        self.action_progress.set()
        return SimpleNamespace(
            action_id="action:due-during-background",
            action_kind="reply",
            status="settled",
            provider_status="delivered",
        )

    def close(self) -> None:
        return None


class _ScheduledActionPumpRaceHost(_DurableDispatchHost):
    def __init__(self) -> None:
        super().__init__()
        self.active_action_pumps = 0
        self.max_active_action_pumps = 0
        self.competitor_entered = asyncio.Event()
        self.due_wake_enabled = False

    async def action_due_projection(self) -> SimpleNamespace:
        actions = ()
        if self.due_wake_enabled:
            actions = (
                SimpleNamespace(
                    action_id="action:recovery",
                    state="scheduled",
                    not_before=NOW,
                    claim_lease=None,
                ),
            )
        return SimpleNamespace(
            actions=actions,
            logical_time=NOW,
            trigger_processes=(),
        )

    async def _enter_action_pump(self, *, wait_for_release: bool) -> None:
        self.active_action_pumps += 1
        self.max_active_action_pumps = max(
            self.max_active_action_pumps,
            self.active_action_pumps,
        )
        try:
            if wait_for_release:
                self.dispatch_started.set()
                await self.provider_release.wait()
            else:
                self.competitor_entered.set()
                await asyncio.sleep(0)
        finally:
            self.active_action_pumps -= 1

    async def drain_action(self, action_id: str) -> SimpleNamespace:
        await self._enter_action_pump(wait_for_release=True)
        self.delivered_action_ids.append(action_id)
        return SimpleNamespace(
            action_id=action_id,
            action_kind="reply",
            status="settled",
            provider_status="delivered",
        )

    async def drain_scheduled_work(self, **kwargs: object) -> SimpleNamespace:
        action_pump_once = kwargs.get("action_pump_once")
        if not callable(action_pump_once):
            raise AssertionError("QQ scheduler must supply its ActionPump-only gate")
        await action_pump_once()
        return SimpleNamespace(action_statuses=("idle",), background_statuses=())

    async def drain_actions_once(self) -> SimpleNamespace:
        await self._enter_action_pump(wait_for_release=False)
        return SimpleNamespace(status="idle")


class _DueWakeIngressLockRaceHost:
    """Coordinate the production lock interleaving without wall-clock sleeps."""

    def __init__(self) -> None:
        self.logical_time = NOW - timedelta(seconds=1)
        self.due_enabled = False
        self.block_due_projection = False
        self.initial_projection_read = asyncio.Event()
        self.due_projection_entered = asyncio.Event()
        self.release_due_projection = asyncio.Event()
        self.ingress_authorized = asyncio.Event()
        self.clock_read_after_due = asyncio.Event()
        self.first_action_progress = asyncio.Event()
        self.tick_targets: list[datetime] = []
        self.delivered_action_ids: list[str] = []
        self._due_drain_completed = False

    async def action_due_projection(self) -> SimpleNamespace:
        if not self.due_enabled:
            self.initial_projection_read.set()
            return SimpleNamespace(
                actions=(),
                logical_time=self.logical_time,
                trigger_processes=(),
            )
        if self.block_due_projection:
            self.due_projection_entered.set()
            await self.release_due_projection.wait()
        return SimpleNamespace(
            actions=(
                SimpleNamespace(
                    action_id="action:already-due",
                    state="scheduled",
                    not_before=NOW,
                    claim_lease=None,
                ),
            ),
            logical_time=self.logical_time,
            trigger_processes=(),
        )

    async def current_logical_time(self) -> datetime:
        if self.release_due_projection.is_set():
            self.clock_read_after_due.set()
        return self.logical_time

    async def inbound(self, _inbound: object) -> SimpleNamespace:
        self.ingress_authorized.set()
        return SimpleNamespace(
            status="action_authorized",
            authorized_action_ids=("action:visible-reply",),
            scheduled_action_ids=(),
        )

    async def tick(self, tick: object) -> SimpleNamespace:
        target = getattr(tick, "logical_time_to")
        self.logical_time = target
        self.tick_targets.append(target)
        return SimpleNamespace(status="observed_only")

    async def drain_action(self, action_id: str) -> SimpleNamespace:
        self.delivered_action_ids.append(action_id)
        self.first_action_progress.set()
        return SimpleNamespace(
            action_id=action_id,
            action_kind="reply",
            status="settled",
            provider_status="delivered",
        )

    async def drain_actions_once(self) -> SimpleNamespace:
        if self._due_drain_completed:
            return SimpleNamespace(status="idle")
        self._due_drain_completed = True
        self.delivered_action_ids.append("action:already-due")
        self.first_action_progress.set()
        return SimpleNamespace(
            action_id="action:already-due",
            action_kind="reply",
            status="settled",
            provider_status="delivered",
        )

    def close(self) -> None:
        return None


class _SchedulerIngressLockRaceHost:
    """Pause one scheduler unit before its clock phase to expose lock order."""

    def __init__(self) -> None:
        self.logical_time = NOW - timedelta(seconds=1)
        self.background_entered = asyncio.Event()
        self.release_background = asyncio.Event()
        self.ingress_authorized = asyncio.Event()
        self.first_action_progress = asyncio.Event()
        self.tick_targets: list[datetime] = []
        self.delivered_action_ids: list[str] = []

    async def action_due_projection(self) -> SimpleNamespace:
        return SimpleNamespace(
            actions=(),
            logical_time=self.logical_time,
            trigger_processes=(),
        )

    async def current_logical_time(self) -> datetime:
        return self.logical_time

    async def inbound(self, _inbound: object) -> SimpleNamespace:
        self.ingress_authorized.set()
        return SimpleNamespace(
            status="action_authorized",
            authorized_action_ids=("action:visible-reply",),
            scheduled_action_ids=(),
        )

    async def drain_background_once(self) -> None:
        self.background_entered.set()
        await self.release_background.wait()
        return None

    async def tick(self, tick: object) -> SimpleNamespace:
        target = getattr(tick, "logical_time_to")
        self.logical_time = target
        self.tick_targets.append(target)
        return SimpleNamespace(status="observed_only")

    async def drain_action(self, action_id: str) -> SimpleNamespace:
        self.delivered_action_ids.append(action_id)
        self.first_action_progress.set()
        return SimpleNamespace(
            action_id=action_id,
            action_kind="reply",
            status="settled",
            provider_status="delivered",
        )

    async def drain_scheduled_work(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(action_statuses=(), background_statuses=())

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_manual_drain_and_scheduler_serialize_scheduled_work() -> None:
    platform = _SerializedScheduledWorkHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=600,
    )
    manual = asyncio.create_task(
        host.drain(max_action_units=1, max_background_units=0)
    )
    try:
        await asyncio.wait_for(platform.entered.wait(), timeout=1)
        scheduler = asyncio.create_task(
            host.scheduler_once(
                observed_at=NOW,
                max_action_units=1,
                max_background_units=0,
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert platform.max_active == 1

        platform.release.set()
        await asyncio.wait_for(asyncio.gather(manual, scheduler), timeout=1)
    finally:
        platform.release.set()
        if not manual.done():
            manual.cancel()
        await host.aclose()


@pytest.mark.asyncio
async def test_scheduler_and_visible_reply_cannot_form_a_lock_cycle() -> None:
    """The scheduler releases its lane before taking the visible-world lock."""

    clock = _PacingClock()
    platform = _SchedulerIngressLockRaceHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        idle_heartbeat_seconds=600,
    )
    scheduler = asyncio.create_task(
        host.scheduler_once(
            observed_at=NOW,
            max_action_units=1,
            max_background_units=1,
        )
    )
    inbound: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(platform.background_entered.wait(), timeout=1)
        inbound = asyncio.create_task(
            host.inbound_text(
                message_id="visible-reply-races-scheduler",
                recipient_id="10001",
                text="后台运行时这条也不能卡住。",
                observed_at=NOW,
            )
        )
        await asyncio.wait_for(platform.ingress_authorized.wait(), timeout=1)

        platform.release_background.set()
        await asyncio.wait_for(platform.first_action_progress.wait(), timeout=0.1)
        await asyncio.wait_for(
            asyncio.gather(scheduler, inbound),
            timeout=1,
        )

        assert platform.tick_targets == [NOW]
        assert platform.delivered_action_ids == ["action:visible-reply"]
    finally:
        platform.release_background.set()
        for task in (scheduler, inbound):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (scheduler, inbound) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_due_wake_and_visible_reply_cannot_form_a_lock_cycle() -> None:
    """A due wake and an ingress-owned Action both make bounded progress."""

    clock = _PacingClock()
    platform = _DueWakeIngressLockRaceHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
    )
    await asyncio.wait_for(platform.initial_projection_read.wait(), timeout=1)
    platform.due_enabled = True
    platform.block_due_projection = True
    due_wake = asyncio.create_task(host._wake_due_actions())  # noqa: SLF001
    inbound: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(platform.due_projection_entered.wait(), timeout=1)
        inbound = asyncio.create_task(
            host.inbound_text(
                message_id="visible-reply-races-due-wake",
                recipient_id="10001",
                text="这条不能和到期唤醒互相卡住。",
                observed_at=NOW,
            )
        )
        await asyncio.wait_for(platform.ingress_authorized.wait(), timeout=1)

        platform.release_due_projection.set()
        await asyncio.wait_for(platform.clock_read_after_due.wait(), timeout=1)
        await asyncio.wait_for(platform.first_action_progress.wait(), timeout=0.1)
        await asyncio.wait_for(
            asyncio.gather(due_wake, inbound),
            timeout=1,
        )

        assert platform.tick_targets == [NOW]
        assert sorted(platform.delivered_action_ids) == [
            "action:already-due",
            "action:visible-reply",
        ]
    finally:
        platform.release_due_projection.set()
        for task in (due_wake, inbound):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (due_wake, inbound) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_dispatch_wait_timeout_does_not_orphan_started_reply() -> None:
    clock = _PacingClock()
    platform = _DurableDispatchHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            total_seconds=0.08,
            hedge_after_seconds=0.01,
            acceptance_dispatch_reserve_seconds=0.02,
            wall_clock=clock.now,
        ),
    )
    try:
        outcome = await asyncio.wait_for(
            host.inbound_text(
                message_id="reply-with-slow-provider",
                recipient_id="10001",
                text="这条回复不要等到我下一句话才补发。",
                observed_at=NOW,
            ),
            timeout=1,
        )

        assert outcome.status == "action_authorized"
        assert platform.dispatch_started.is_set()
        assert platform.cancelled_after_dispatch_started is False
        assert platform.delivered_action_ids == []

        platform.provider_release.set()
        for _ in range(20):
            if platform.delivered_action_ids:
                break
            await asyncio.sleep(0)
        assert platform.delivered_action_ids == ["action:reply:one"]
    finally:
        platform.provider_release.set()
        await host.aclose()


@pytest.mark.asyncio
async def test_ingress_timer_refresh_does_not_cancel_a_due_provider_handoff() -> None:
    """Refreshing the nearest due timer cannot cancel a dispatch it already woke."""

    clock = _PacingClock()
    platform = _DueDispatchRefreshHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
    )
    try:
        await asyncio.wait_for(platform.dispatch_started.wait(), timeout=1)

        outcome = await asyncio.wait_for(
            host.inbound_text(
                message_id="refresh-while-due-provider-is-running",
                recipient_id="10001",
                text="这条入站会重建到期计时器。",
                observed_at=NOW,
            ),
            timeout=1,
        )

        assert outcome.status == "observed_only"
        assert platform.cancelled_after_dispatch_started is False
        assert platform.delivered_action_ids == []

        platform.provider_release.set()
        await asyncio.wait_for(platform.dispatch_finished.wait(), timeout=1)
        assert platform.cancelled_after_dispatch_started is False
        assert platform.delivered_action_ids == ["action:due"]
    finally:
        platform.provider_release.set()
        await host.aclose()


@pytest.mark.asyncio
async def test_direct_facade_caller_cancel_does_not_cancel_owned_provider_handoff() -> None:
    """Caller cancellation ends its wait, not the durable provider attempt."""

    clock = _PacingClock()
    platform = _DurableDispatchHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        interactive_turn_budget_policy=None,
    )
    inbound = asyncio.create_task(
        host.inbound_text(
            message_id="direct-facade-caller-cancel",
            recipient_id="10001",
            text="调用者取消也不能撤销已经开始的发送。",
            observed_at=NOW,
        )
    )
    try:
        await asyncio.wait_for(platform.dispatch_started.wait(), timeout=1)
        inbound.cancel()
        await asyncio.gather(inbound, return_exceptions=True)
        await asyncio.sleep(0)

        assert platform.cancelled_after_dispatch_started is False
        assert platform.delivered_action_ids == []

        platform.provider_release.set()
        await asyncio.wait_for(platform.dispatch_finished.wait(), timeout=1)
        assert platform.cancelled_after_dispatch_started is False
        assert platform.delivered_action_ids == ["action:reply:one"]
    finally:
        platform.provider_release.set()
        if not inbound.done():
            inbound.cancel()
        await asyncio.gather(inbound, return_exceptions=True)
        await host.aclose()


@pytest.mark.asyncio
async def test_cancelling_close_wait_does_not_cancel_owned_provider_handoff() -> None:
    """Shutdown ownership survives cancellation of one lifecycle waiter."""

    clock = _PacingClock()
    platform = _DurableDispatchHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        interactive_turn_budget_policy=None,
    )
    inbound = asyncio.create_task(
        host.inbound_text(
            message_id="close-wait-cancel",
            recipient_id="10001",
            text="关闭调用者取消也不能撤销已经开始的发送。",
            observed_at=NOW,
        )
    )
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.dispatch_started.wait(), timeout=1)
        inbound.cancel()
        await asyncio.gather(inbound, return_exceptions=True)

        closing = asyncio.create_task(host.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)

        assert platform.cancelled_after_dispatch_started is False
        platform.provider_release.set()
        await asyncio.wait_for(platform.dispatch_finished.wait(), timeout=1)
        await asyncio.wait_for(host.aclose(), timeout=1)
        assert platform.delivered_action_ids == ["action:reply:one"]
    finally:
        platform.provider_release.set()
        if not inbound.done():
            inbound.cancel()
        if closing is not None and not closing.done():
            closing.cancel()
        await asyncio.gather(
            inbound,
            *(item for item in (closing,) if item is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_close_gate_rejects_new_ingress_before_it_can_create_an_action() -> None:
    clock = _PacingClock()
    platform = _CountingDurableDispatchHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        interactive_turn_budget_policy=None,
        owned_action_close_grace_seconds=0.2,
    )
    inbound = asyncio.create_task(
        host.inbound_text(
            message_id="before-close-gate",
            recipient_id="10001",
            text="这条已经进入 provider handoff。",
            observed_at=NOW,
        )
    )
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(platform.dispatch_started.wait(), timeout=1)
        inbound.cancel()
        await asyncio.gather(inbound, return_exceptions=True)
        closing = asyncio.create_task(host.aclose())
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="closing"):
            await host.inbound_text(
                message_id="after-close-gate",
                recipient_id="10001",
                text="关闭快照之后不能再创建 Action。",
                observed_at=NOW,
            )
        assert platform.inbound_calls == 1

        platform.provider_release.set()
        await asyncio.wait_for(closing, timeout=1)
    finally:
        platform.provider_release.set()
        if not inbound.done():
            inbound.cancel()
        if closing is not None and not closing.done():
            closing.cancel()
        await asyncio.gather(
            inbound,
            *(item for item in (closing,) if item is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_close_bounds_a_queue_of_owned_provider_handoffs_and_cancels_for_recovery() -> None:
    clock = _PacingClock()
    platform = _QueuedDurableDispatchHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            total_seconds=0.04,
            hedge_after_seconds=0.01,
            acceptance_dispatch_reserve_seconds=0.01,
            wall_clock=clock.now,
        ),
        owned_action_close_grace_seconds=0.01,
    )
    try:
        outcome = await asyncio.wait_for(
            host.inbound_text(
                message_id="queued-provider-handoffs",
                recipient_id="10001",
                text="三条 provider handoff 都不能让关闭无界等待。",
                observed_at=NOW,
            ),
            timeout=1,
        )
        await asyncio.wait_for(platform.first_dispatch_started.wait(), timeout=1)
        assert outcome.status == "action_authorized"
        assert platform.entered_action_ids == ["action:reply:one"]

        await asyncio.wait_for(host.aclose(), timeout=0.5)
        await asyncio.sleep(0)

        assert platform.cancelled_action_ids == ["action:reply:one"]
        assert platform.delivered_action_ids == []
        assert platform.entered_action_ids == ["action:reply:one"]
    finally:
        platform.provider_release.set()
        await host.aclose()


@pytest.mark.asyncio
async def test_due_wake_does_not_wait_for_unrelated_slow_background_work() -> None:
    """An exact Action deadline uses the ActionPump lane, not the model-work lane."""

    clock = _PacingClock()
    platform = _DueWakeDuringSlowBackgroundHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
    )
    drain = None
    try:
        await asyncio.wait_for(platform.initial_projection_read.wait(), timeout=1)
        drain = asyncio.create_task(
            host.drain(max_action_units=0, max_background_units=1)
        )
        await asyncio.wait_for(platform.background_started.wait(), timeout=1)
        platform.due_enabled = True

        outcome = await host.inbound_text(
            message_id="arm-due-action-during-background",
            recipient_id="10001",
            text="后台模型慢的时候，到期消息也得准时走。",
            observed_at=NOW,
        )
        await asyncio.wait_for(platform.action_progress.wait(), timeout=0.25)

        assert outcome.status == "observed_only"
        assert platform.delivered_action_ids == ["action:due-during-background"]
        assert not drain.done()
    finally:
        platform.background_release.set()
        if drain is not None:
            await asyncio.wait_for(drain, timeout=1)
        await host.aclose()


@pytest.mark.parametrize("competitor_kind", ["public_drain", "due_wake"])
@pytest.mark.asyncio
async def test_owned_targeted_action_serializes_with_scheduled_action_pumps(
    competitor_kind: str,
) -> None:
    clock = _PacingClock()
    platform = _ScheduledActionPumpRaceHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=MemoryQQIngressStore(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            total_seconds=0.08,
            hedge_after_seconds=0.01,
            acceptance_dispatch_reserve_seconds=0.02,
            wall_clock=clock.now,
        ),
    )
    inbound = asyncio.create_task(
        host.inbound_text(
            message_id=f"targeted-versus-{competitor_kind}",
            recipient_id="10001",
            text="这一条的 provider 还没返回。",
            observed_at=NOW,
        )
    )
    competitor: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(platform.dispatch_started.wait(), timeout=1)
        if competitor_kind == "public_drain":
            competitor = asyncio.create_task(
                host.drain(max_action_units=1, max_background_units=0)
            )
        else:
            platform.due_wake_enabled = True
            competitor = asyncio.create_task(host._wake_due_actions())  # noqa: SLF001
        for _ in range(5):
            await asyncio.sleep(0)

        assert platform.max_active_action_pumps == 1
        assert not platform.competitor_entered.is_set()

        platform.provider_release.set()
        await asyncio.wait_for(asyncio.gather(inbound, competitor), timeout=1)
        assert platform.competitor_entered.is_set()
        assert platform.max_active_action_pumps == 1
    finally:
        platform.provider_release.set()
        if competitor is not None and not competitor.done():
            competitor.cancel()
        if not inbound.done():
            inbound.cancel()
        await asyncio.gather(
            inbound,
            *(item for item in (competitor,) if item is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_scheduler_recovered_ingress_does_not_self_deadlock_owned_action() -> None:
    clock = _PacingClock()
    platform = _ScheduledActionPumpRaceHost()
    ingress_store = MemoryQQIngressStore()
    ingress_store.submit(
        QQIngressFragment(
            source_event_id="scheduler-recovered-ingress",
            recipient_id="10001",
            observed_at=NOW,
            content_shape="text",
            text="重启后由 scheduler 接手。",
        ),
        received_at=NOW,
    )
    clock.current = NOW + timedelta(seconds=1)
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=ingress_store,
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        action_due_now=clock.now,
        interactive_turn_budget_policy=None,
        idle_heartbeat_seconds=600,
    )
    scheduler = asyncio.create_task(
        host.scheduler_once(
            observed_at=clock.now(),
            max_action_units=1,
            max_background_units=0,
        )
    )
    try:
        await asyncio.wait_for(platform.dispatch_started.wait(), timeout=1)
        platform.provider_release.set()
        await asyncio.wait_for(scheduler, timeout=1)
        assert platform.delivered_action_ids == ["action:reply:one"]
        assert platform.max_active_action_pumps == 1
    finally:
        platform.provider_release.set()
        if not scheduler.done():
            scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await host.aclose()
