"""Process-local wake timer for the nearest ledger-authorized Action due time."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import logging
from threading import Lock


logger = logging.getLogger(__name__)


class ActionDueWakeDiagnostics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._wake_latency_ms: list[float] = []
        self._failure_count = 0

    def record(self, value: float) -> None:
        with self._lock:
            self._wake_latency_ms.append(max(0.0, value))
            if len(self._wake_latency_ms) > 2_048:
                del self._wake_latency_ms[: len(self._wake_latency_ms) - 2_048]

    def snapshot(self) -> dict[str, float | int | None]:
        with self._lock:
            values = sorted(self._wake_latency_ms)
            failure_count = self._failure_count
        def percentile(fraction: float) -> float | None:
            return (
                values[min(len(values) - 1, int((len(values) - 1) * fraction))]
                if values
                else None
            )
        return {
            "wake_count": len(values),
            "wake_latency_ms_p50": percentile(0.5),
            "wake_latency_ms_p95": percentile(0.95),
            "failure_count": failure_count,
        }

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1


class ActionDueWake:
    """Wake the ActionPump near due; projection checks remain authoritative."""

    def __init__(
        self,
        *,
        project: Callable[[], object | Awaitable[object]],
        wake: Callable[[], Awaitable[object]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        coalesce_seconds: float = 0.05,
        diagnostics: ActionDueWakeDiagnostics | None = None,
    ) -> None:
        if coalesce_seconds < 0 or coalesce_seconds > 1:
            raise ValueError("Action due wake coalescing must be between zero and one second")
        self._project = project
        self._wake = wake
        self._now = now
        self._sleep = sleep
        self._coalesce_seconds = coalesce_seconds
        self._diagnostics = diagnostics or ActionDueWakeDiagnostics()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._generation = 0

    async def refresh(self) -> datetime | None:
        """Rebuild the nearest timer solely from the current projection."""

        if self._closed:
            return None
        self._generation += 1
        generation = self._generation
        old = self._task
        if (
            old is not None
            and old is not asyncio.current_task()
            and not old.done()
        ):
            old.cancel()
        self._task = None
        try:
            due = await self._read_nearest_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._diagnostics.record_failure()
            logger.exception("action due projection failed; rebuilding timer")
            if self._closed or generation != self._generation:
                return None
            self._task = asyncio.create_task(
                self._retry_projection(generation=generation),
                name="action-due-wake:projection-retry",
            )
            return None
        if self._closed or generation != self._generation:
            return due
        if due is not None:
            self._task = asyncio.create_task(
                self._wait_and_wake(due=due, generation=generation),
                name=f"action-due-wake:{due.isoformat()}",
            )
        return due

    async def _read_nearest_due(self) -> datetime | None:
        projection = self._project()
        if isinstance(projection, Awaitable):
            projection = await projection
        return self.nearest_due(projection)

    async def _retry_projection(self, *, generation: int) -> None:
        for delay in (1.0, 5.0, 30.0):
            try:
                await self._sleep(delay)
                if self._closed or generation != self._generation:
                    return
                due = await self._read_nearest_due()
                if due is not None:
                    self._task = asyncio.create_task(
                        self._wait_and_wake(due=due, generation=generation),
                        name=f"action-due-wake:{due.isoformat()}",
                    )
                else:
                    self._task = None
                return
            except asyncio.CancelledError:
                return
            except Exception:
                self._diagnostics.record_failure()
                logger.exception(
                    "action due projection retry failed; retrying in %.1fs",
                    delay,
                )
        logger.error("action due projection retries exhausted")

    @staticmethod
    def nearest_due(projection: object) -> datetime | None:
        actions = getattr(projection, "actions", ())
        due = [
            action.not_before
            for action in actions
            if action.state in {"authorized", "scheduled"}
            and action.not_before is not None
        ]
        due.extend(
            action.claim_lease.expires_at
            for action in actions
            if action.state in {"claimed", "dispatch_started", "provider_accepted"}
            and action.claim_lease is not None
        )
        return min(due) if due else None

    async def _wait_and_wake(self, *, due: datetime, generation: int) -> None:
        try:
            delay = max(0.0, (due - self._now()).total_seconds())
            await self._sleep(delay + self._coalesce_seconds)
            if self._closed or generation != self._generation:
                return
            observed = self._now()
            self._diagnostics.record((observed - due).total_seconds() * 1_000)
            await self._wake()
            if generation == self._generation:
                await self.refresh()
        except asyncio.CancelledError:
            return
        except Exception:
            self._diagnostics.record_failure()
            logger.exception("action due wake failed; rebuilding timer")
            for delay in (1.0, 5.0, 30.0):
                if self._closed or generation != self._generation:
                    return
                try:
                    await self._sleep(delay)
                    if self._closed or generation != self._generation:
                        return
                    await self.refresh()
                    return
                except asyncio.CancelledError:
                    return
                except Exception:
                    self._diagnostics.record_failure()
                    logger.exception(
                        "action due wake timer rebuild failed; retrying in %.1fs",
                        delay,
                    )
            logger.error("action due wake timer rebuild exhausted bounded retries")

    async def aclose(self) -> None:
        self._closed = True
        self._generation += 1
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def diagnostics(self) -> dict[str, float | int | None]:
        return self._diagnostics.snapshot()


__all__ = ["ActionDueWake", "ActionDueWakeDiagnostics"]
