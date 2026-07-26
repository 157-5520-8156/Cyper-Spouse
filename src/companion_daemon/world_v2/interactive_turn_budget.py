"""One absolute monotonic budget for a user-visible World-v2 turn.

The budget is process control, not World authority.  It is created from the
durable ingress claim time, then passed unchanged through cognition,
acceptance, and dispatch so no nested adapter can restart the clock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import math
import time


Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class InteractiveTurnBudgetPolicy:
    """Composition-owned timing policy for one interactive reply."""

    # 12s total gives DeepSeek's real peak-hour completion latency (measured
    # 5-10s at CN midnight on 07-24) headroom to finish inside the candidate
    # deadline.  5.5s looked great against stubs but starved every live call,
    # and a hedge to the same slow provider dies with it; the budget must
    # cover the provider's actual p95, not the latency we wish it had.
    total_seconds: float = 12.0
    hedge_after_seconds: float = 2.5
    acceptance_dispatch_reserve_seconds: float = 1.2
    clock: Clock = time.monotonic
    sleep: Sleeper = __import__("asyncio").sleep
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        values = (
            self.total_seconds,
            self.hedge_after_seconds,
            self.acceptance_dispatch_reserve_seconds,
        )
        if any(
            isinstance(value, bool) or not math.isfinite(value) or value <= 0
            for value in values
        ):
            raise ValueError("interactive turn budget values must be finite and positive")
        if self.hedge_after_seconds >= (
            self.total_seconds - self.acceptance_dispatch_reserve_seconds
        ):
            raise ValueError("hedge must start before the candidate deadline")

    def start(
        self,
        *,
        processing_started_at: datetime | None = None,
        marker: Callable[[str], None] | None = None,
    ) -> "InteractiveTurnBudget":
        """Create one immutable deadline, preserving elapsed durable queue work."""

        now = self.clock()
        elapsed = 0.0
        if processing_started_at is not None:
            if (
                processing_started_at.tzinfo is None
                or processing_started_at.utcoffset() is None
            ):
                raise ValueError("processing_started_at must be timezone-aware")
            elapsed = max(
                0.0,
                (
                    self.wall_clock().astimezone(UTC)
                    - processing_started_at.astimezone(UTC)
                ).total_seconds(),
            )
        started = now - elapsed
        return InteractiveTurnBudget(
            started_at=started,
            deadline=started + self.total_seconds,
            hedge_at=started + self.hedge_after_seconds,
            acceptance_dispatch_reserve_seconds=self.acceptance_dispatch_reserve_seconds,
            clock=self.clock,
            sleep=self.sleep,
            marker=marker,
        )


@dataclass(frozen=True, slots=True)
class InteractiveTurnBudget:
    """Absolute timing facts shared by the whole reply chain."""

    started_at: float
    deadline: float
    hedge_at: float
    acceptance_dispatch_reserve_seconds: float
    clock: Clock
    sleep: Sleeper
    marker: Callable[[str], None] | None = None

    @property
    def candidate_deadline(self) -> float:
        return self.deadline - self.acceptance_dispatch_reserve_seconds

    @property
    def hedge_after(self) -> float:
        return max(0.0, self.hedge_at - self.started_at)

    def remaining(self, *, include_reserve: bool = False) -> float:
        endpoint = self.deadline if include_reserve else self.candidate_deadline
        return max(0.0, endpoint - self.clock())

    def fit(
        self,
        requested_seconds: float,
        *,
        minimum_seconds: float = 0.0,
        include_reserve: bool = False,
    ) -> float | None:
        """Fit bounded work without crossing the selected absolute deadline."""

        available = self.remaining(include_reserve=include_reserve)
        fitted = min(max(0.0, requested_seconds), available)
        return fitted if fitted >= minimum_seconds and fitted > 0 else None

    async def wait_for_hedge(self) -> None:
        delay = self.hedge_at - self.clock()
        if delay > 0:
            await self.sleep(delay)

    def mark(self, event: str) -> None:
        if self.marker is not None:
            self.marker(event)


__all__ = [
    "InteractiveTurnBudget",
    "InteractiveTurnBudgetPolicy",
]
