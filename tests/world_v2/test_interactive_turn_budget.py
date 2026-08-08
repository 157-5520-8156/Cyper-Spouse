"""Unit tests for absolute interactive turn budget policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.interactive_turn_budget import InteractiveTurnBudgetPolicy


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_default_validation_window_contains_bounded_author_recovery() -> None:
    policy = InteractiveTurnBudgetPolicy()

    # Interactive source review is retired (2026-08-07): the author call is
    # the only semantic provider, so recovery windows cover one re-author
    # call plus a retry instead of review retry chains.
    assert policy.validation_recovery_seconds >= 8.0
    assert policy.validation_reselection_seconds >= 20.0


def test_first_provider_entry_budget_starts_at_ingress_not_after_coalescing() -> None:
    clock = _Clock()
    wall_now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=12.0,
        hedge_after_seconds=2.5,
        acceptance_dispatch_reserve_seconds=1.2,
        first_provider_entry_seconds=0.5,
        clock=clock,
        wall_clock=lambda: wall_now,
    ).start(
        processing_started_at=wall_now - timedelta(milliseconds=20),
        ingress_started_at=wall_now - timedelta(milliseconds=280),
    )

    assert budget.first_provider_entry_remaining() == pytest.approx(0.22)
    assert budget.author_remaining() == pytest.approx(10.78)
    clock.now += 0.1
    assert budget.first_provider_entry_remaining() == pytest.approx(0.12)


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


def test_technical_recovery_opens_one_independent_bounded_window() -> None:
    clock = _Clock()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=12.0,
        hedge_after_seconds=2.5,
        acceptance_dispatch_reserve_seconds=1.2,
        technical_recovery_seconds=8.0,
        clock=clock,
    ).start()

    clock.now = budget.candidate_deadline
    recovery_deadline = budget.begin_technical_recovery()

    assert recovery_deadline == pytest.approx(clock.now + 8.0)
    assert budget.candidate_deadline == pytest.approx(recovery_deadline)
    assert budget.remaining() == pytest.approx(8.0)
    assert budget.remaining(include_reserve=True) == pytest.approx(9.2)
    assert budget.begin_technical_recovery() is None


def test_validation_recovery_has_its_own_bounded_window_and_dispatch_reserve() -> None:
    clock = _Clock()
    marks: list[str] = []
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=12.0,
        hedge_after_seconds=2.5,
        acceptance_dispatch_reserve_seconds=1.2,
        technical_recovery_seconds=8.0,
        validation_recovery_seconds=3.5,
        clock=clock,
    ).start(marker=marks.append)

    clock.now = budget.candidate_deadline
    recovery_deadline = budget.begin_validation_recovery(
        candidate_key="model-call:primary"
    )

    assert recovery_deadline == pytest.approx(clock.now + 3.5)
    assert budget.candidate_deadline == pytest.approx(recovery_deadline)
    assert budget.remaining() == pytest.approx(3.5)
    assert budget.remaining(include_reserve=True) == pytest.approx(4.7)
    assert (
        budget.begin_validation_recovery(candidate_key="model-call:primary")
        is None
    )
    assert marks == ["validation_recovery_started"]
    assert "technical_recovery_started" not in marks


def test_validation_reselection_has_one_candidate_bound_completion_window() -> None:
    clock = _Clock()
    marks: list[str] = []
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=12.0,
        hedge_after_seconds=2.5,
        acceptance_dispatch_reserve_seconds=1.2,
        validation_recovery_seconds=3.5,
        validation_reselection_seconds=7.0,
        clock=clock,
    ).start(marker=marks.append)

    review_deadline = budget.begin_validation_recovery(
        candidate_key="model-call:primary"
    )
    clock.now += 3.0
    reselection_deadline = budget.begin_validation_reselection(
        candidate_key="model-call:primary"
    )

    assert review_deadline == pytest.approx(103.5)
    assert reselection_deadline == pytest.approx(110.0)
    assert budget.candidate_deadline == pytest.approx(reselection_deadline)
    assert budget.remaining() == pytest.approx(7.0)
    assert (
        budget.begin_validation_reselection(candidate_key="model-call:primary")
        is None
    )
    assert marks == [
        "validation_recovery_started",
        "validation_reselection_started",
    ]


def test_validation_recovery_is_scoped_to_each_authored_candidate() -> None:
    clock = _Clock()
    marks: list[str] = []
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=12.0,
        hedge_after_seconds=2.5,
        acceptance_dispatch_reserve_seconds=1.2,
        validation_recovery_seconds=3.5,
        clock=clock,
    ).start(marker=marks.append)
    ordinary_author_deadline = budget.author_candidate_deadline

    primary_deadline = budget.begin_validation_recovery(
        candidate_key="model-call:primary"
    )
    clock.now += 0.5
    backup_deadline = budget.begin_validation_recovery(
        candidate_key="model-call:backup"
    )

    assert primary_deadline == pytest.approx(103.5)
    assert backup_deadline == pytest.approx(104.0)
    assert budget.candidate_deadline == pytest.approx(backup_deadline)
    assert budget.author_candidate_deadline == pytest.approx(
        ordinary_author_deadline
    )
    assert marks == [
        "validation_recovery_started",
        "validation_recovery_started",
    ]


def test_later_real_author_failure_activates_technical_window_after_validation() -> None:
    clock = _Clock()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=12.0,
        hedge_after_seconds=2.5,
        acceptance_dispatch_reserve_seconds=1.2,
        technical_recovery_seconds=8.0,
        validation_recovery_seconds=3.5,
        clock=clock,
    ).start()

    validation_deadline = budget.begin_validation_recovery(
        candidate_key="model-call:primary"
    )
    clock.now += 0.5
    technical_deadline = budget.begin_technical_recovery()

    assert validation_deadline == pytest.approx(103.5)
    assert technical_deadline == pytest.approx(108.5)
    assert budget.candidate_deadline == pytest.approx(technical_deadline)
    assert budget.remaining() == pytest.approx(8.0)
