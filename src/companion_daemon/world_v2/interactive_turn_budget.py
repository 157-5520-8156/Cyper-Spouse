"""One absolute monotonic budget for a user-visible World-v2 turn.

The budget is process control, not World authority.  It is created from the
durable ingress claim time, then passed unchanged through cognition,
acceptance, and dispatch so no nested adapter can restart the clock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
import time


Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
FIRST_PROVIDER_ENTRY_RESERVE_SECONDS = 0.05


@dataclass(slots=True)
class _TechnicalRecoveryState:
    """One-shot extension activated only after a real candidate failure."""

    candidate_deadline: float | None = None


@dataclass(slots=True)
class _ValidationRecoveryState:
    """Truth-boundary extensions keyed by the authored candidate they validate."""

    candidate_deadlines: dict[str, float] = field(default_factory=dict)
    reselection_deadlines: dict[str, float] = field(default_factory=dict)
    active_candidate_key: str | None = None
    active_phase: str | None = None


@dataclass(slots=True)
class _ActiveRecoveryState:
    lane: str | None = None


@dataclass(frozen=True, slots=True)
class InteractiveTurnBudgetPolicy:
    """Composition-owned timing policy for one interactive reply."""

    # Normal latency is still whatever the winning provider actually takes;
    # this is only the cancellation ceiling. When visible chat source review
    # is explicitly installed, its bounded review/reselection windows are
    # tracked separately below; they do not silently renew the ordinary
    # author deadline.
    total_seconds: float = 12.0
    hedge_after_seconds: float = 2.0
    acceptance_dispatch_reserve_seconds: float = 1.0
    # One soft ingress-to-first-provider budget for API-external work. QQ's
    # durable sender-rhythm window consumes part of this same interval; Recall
    # may use only what remains instead of adding another independent wait.
    first_provider_entry_seconds: float = 0.5
    # The configured independent role provider also showed cold completions
    # above 8s. This window opens only after a real author failure, so raising
    # the ceiling does not slow an ordinary successful turn.
    technical_recovery_seconds: float = 12.0
    # One candidate-level source-review validation phase.  The independent
    # reviewer has a 22s call ceiling and may retry one real transport/wire
    # failure, so the default window must not collapse the second attempt.
    validation_recovery_seconds: float = 46.0
    # A rejected candidate receives one same-role re-selection plus its final
    # source review.  This is the fixed 100s window from ADR0014; it is a
    # ceiling opened only after a real semantic rejection, not normal latency.
    validation_reselection_seconds: float = 100.0
    clock: Clock = time.monotonic
    sleep: Sleeper = __import__("asyncio").sleep
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        values = (
            self.total_seconds,
            self.hedge_after_seconds,
            self.acceptance_dispatch_reserve_seconds,
            self.first_provider_entry_seconds,
            self.technical_recovery_seconds,
            self.validation_recovery_seconds,
            self.validation_reselection_seconds,
        )
        if any(
            isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in values
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
        ingress_started_at: datetime | None = None,
        marker: Callable[[str], None] | None = None,
    ) -> "InteractiveTurnBudget":
        """Create immutable author and first-provider timing deadlines."""

        now = self.clock()
        wall_now = self.wall_clock().astimezone(UTC)

        def elapsed_since(value: datetime | None, *, label: str) -> float:
            if value is None:
                return 0.0
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{label} must be timezone-aware")
            return max(
                0.0,
                (wall_now - value.astimezone(UTC)).total_seconds(),
            )

        processing_elapsed = elapsed_since(
            processing_started_at,
            label="processing_started_at",
        )
        ingress_elapsed = elapsed_since(
            ingress_started_at or processing_started_at,
            label="ingress_started_at",
        )
        started = now - processing_elapsed
        return InteractiveTurnBudget(
            started_at=started,
            deadline=started + self.total_seconds,
            hedge_at=started + self.hedge_after_seconds,
            first_provider_entry_deadline=(
                now - ingress_elapsed + self.first_provider_entry_seconds
            ),
            acceptance_dispatch_reserve_seconds=self.acceptance_dispatch_reserve_seconds,
            technical_recovery_seconds=self.technical_recovery_seconds,
            validation_recovery_seconds=self.validation_recovery_seconds,
            validation_reselection_seconds=self.validation_reselection_seconds,
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
    first_provider_entry_deadline: float
    acceptance_dispatch_reserve_seconds: float
    technical_recovery_seconds: float
    validation_recovery_seconds: float
    validation_reselection_seconds: float
    clock: Clock
    sleep: Sleeper
    marker: Callable[[str], None] | None = None
    _technical_recovery: _TechnicalRecoveryState = field(
        default_factory=_TechnicalRecoveryState,
        repr=False,
        compare=False,
    )
    _validation_recovery: _ValidationRecoveryState = field(
        default_factory=_ValidationRecoveryState,
        repr=False,
        compare=False,
    )
    _active_recovery: _ActiveRecoveryState = field(
        default_factory=_ActiveRecoveryState,
        repr=False,
        compare=False,
    )

    @property
    def candidate_deadline(self) -> float:
        if self._active_recovery.lane == "validation":
            candidate_key = self._validation_recovery.active_candidate_key
            assert candidate_key is not None
            if self._validation_recovery.active_phase == "reselection":
                return self._validation_recovery.reselection_deadlines[candidate_key]
            return self._validation_recovery.candidate_deadlines[candidate_key]
        if self._active_recovery.lane == "technical":
            technical_deadline = self._technical_recovery.candidate_deadline
            assert technical_deadline is not None
            return technical_deadline
        return self.deadline - self.acceptance_dispatch_reserve_seconds

    @property
    def author_candidate_deadline(self) -> float:
        """Return the deadline available to a newly authored candidate.

        A validation extension belongs only to the already-authored bytes that
        opened it.  A later Deliberation phase or recovery author must never
        inherit that reviewer-only deadline.  The separate technical-recovery
        lane does authorize one configured role author, so it remains visible
        here while active.
        """

        technical_deadline = self._technical_recovery.candidate_deadline
        if technical_deadline is not None and technical_deadline > self.clock():
            return technical_deadline
        return self.deadline - self.acceptance_dispatch_reserve_seconds

    def begin_author_candidate(self) -> float:
        """Activate the applicable role-author lane for a new candidate.

        This clears only a previous candidate's validation lane. It neither
        renews the ordinary deadline nor opens another technical recovery.
        """

        deadline = self.author_candidate_deadline
        technical_deadline = self._technical_recovery.candidate_deadline
        self._active_recovery.lane = (
            "technical"
            if technical_deadline is not None
            and technical_deadline > self.clock()
            and deadline == technical_deadline
            else None
        )
        return deadline

    @property
    def hedge_after(self) -> float:
        return max(0.0, self.hedge_at - self.started_at)

    def remaining(self, *, include_reserve: bool = False) -> float:
        recovery_active = self._active_recovery.lane is not None
        endpoint = self.candidate_deadline
        if include_reserve:
            endpoint = (
                endpoint + self.acceptance_dispatch_reserve_seconds
                if recovery_active
                else self.deadline
            )
        return max(0.0, endpoint - self.clock())

    def author_remaining(self) -> float:
        """Return time left for authorship without activating or renewing a lane."""

        return max(0.0, self.author_candidate_deadline - self.clock())

    def first_provider_entry_remaining(self) -> float:
        """Return the unspent ingress-to-first-provider preparation interval."""

        return max(0.0, self.first_provider_entry_deadline - self.clock())

    def begin_technical_recovery(self) -> float | None:
        """Open the one model-recovery window after an observed candidate failure.

        Deliberation is the sole caller and only invokes this after the primary
        candidate has actually failed or timed out.  The mutable one-shot cell
        lets the same budget instance expose the renewed acceptance/dispatch
        reserve to the host without letting ordinary successful turns exceed
        their original absolute deadline.
        """

        if self._technical_recovery.candidate_deadline is not None:
            return None
        deadline = self.clock() + self.technical_recovery_seconds
        self._technical_recovery.candidate_deadline = deadline
        self._active_recovery.lane = "technical"
        self.mark("technical_recovery_started")
        return deadline

    def begin_validation_recovery(self, *, candidate_key: str) -> float | None:
        """Open one fixed reviewer window for one authored candidate.

        This is deliberately independent from generic role-author recovery.
        It starts before the first reviewer dispatch and contains that call plus
        at most one complete retry after a real first failure. The distinct
        reselection window covers any later doctrine-authorized same-role
        correction plus final review. Neither phase can grant open-ended
        authorship, Recall, or a hedge. Different candidates each receive one
        window so an expired primary review cannot starve a later pinned
        candidate.
        """

        if not candidate_key:
            raise ValueError("validation recovery candidate key is required")
        if candidate_key in self._validation_recovery.candidate_deadlines:
            return None
        deadline = self.clock() + self.validation_recovery_seconds
        self._validation_recovery.candidate_deadlines[candidate_key] = deadline
        self._validation_recovery.active_candidate_key = candidate_key
        self._validation_recovery.active_phase = "review_retry"
        self._active_recovery.lane = "validation"
        self.mark("validation_recovery_started")
        return deadline

    def begin_validation_reselection(self, *, candidate_key: str) -> float | None:
        """Open the candidate's only correction plus final-review window."""

        if not candidate_key:
            raise ValueError("validation reselection candidate key is required")
        if candidate_key in self._validation_recovery.reselection_deadlines:
            return None
        deadline = self.clock() + self.validation_reselection_seconds
        self._validation_recovery.reselection_deadlines[candidate_key] = deadline
        self._validation_recovery.active_candidate_key = candidate_key
        self._validation_recovery.active_phase = "reselection"
        self._active_recovery.lane = "validation"
        self.mark("validation_reselection_started")
        return deadline

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
