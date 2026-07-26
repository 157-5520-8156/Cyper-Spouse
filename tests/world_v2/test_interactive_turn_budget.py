"""Unit tests for absolute interactive turn budget policy."""

from __future__ import annotations

import pytest

from companion_daemon.world_v2.interactive_turn_budget import InteractiveTurnBudgetPolicy


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_budget_exposes_absolute_deadlines_and_reserve() -> None:
    clock = _Clock()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    budget = InteractiveTurnBudgetPolicy(
        total_seconds=12.0,
        hedge_after_seconds=2.5,
        acceptance_dispatch_reserve_seconds=1.2,
        clock=clock,
        sleep=sleep,
    ).start()

    assert budget.remaining() == pytest.approx(10.8)
    assert budget.remaining(include_reserve=True) == pytest.approx(12.0)
    assert budget.hedge_after == pytest.approx(2.5)
    await budget.wait_for_hedge()
    assert sleeps == [2.5]
    assert budget.remaining() == pytest.approx(8.3)


def test_budget_rejects_hedge_after_candidate_deadline() -> None:
    with pytest.raises(ValueError, match="hedge must start before"):
        InteractiveTurnBudgetPolicy(
            total_seconds=5.0,
            hedge_after_seconds=4.0,
            acceptance_dispatch_reserve_seconds=1.2,
        )
