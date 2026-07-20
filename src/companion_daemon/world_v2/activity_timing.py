"""Deterministic timing rules for source-bound activity lifecycles.

An activity may be completed early when its owner has a real reason to do so,
but a scheduler tick must not make a multi-minute plan disappear after one
short wake.  This module is intentionally pure so the catalog and the reducer
share the same rule without either side calling the model or the ledger.

Each rule here is frozen per activity-opening catalog version: replay of an
old proposal must see exactly the rule that produced it, so a strengthened
rule is a new function selected by a bumped catalog version rather than an
edit of the old one.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .schemas import PlanStateProjection


# These are policy values, not provider timing values.  They keep ordinary
# scheduler wakes from treating a plan opening as an immediate completion
# while still allowing a genuinely short activity to finish promptly.
MIN_ACTIVITY_COMPLETION_SECONDS = 60


def activity_minimum_completion_delta(plan: PlanStateProjection) -> timedelta:
    """Return the minimum elapsed time before ordinary completion is offered.

    Plans without a scheduled window are legacy/explicit-host plans.  They do
    not have enough timing authority for this policy, so the caller may keep
    their historical behavior.  Production life-author plans always carry a
    window and therefore receive the bounded minimum below.
    """

    window = plan.scheduled_window
    if window is None:
        return timedelta(0)
    # The first production safeguard is deliberately conservative and stable:
    # one short scheduler wake cannot complete a plan, while the existing
    # time-window semantics still allow a normal one-minute transition in
    # deterministic replays.  Richer activity-specific duration policy can be
    # added later without moving this rule into a model prompt.
    del window
    return timedelta(seconds=MIN_ACTIVITY_COMPLETION_SECONDS)


def activity_completion_allowed(
    plan: PlanStateProjection, *, logical_time: datetime
) -> bool:
    """Frozen ``activity-opening.3`` rule: a bounded minimum elapsed time.

    Under this rule a sixty-minute plan could be completed after one minute,
    which erased the companion's "currently doing" state for almost the whole
    hour.  It is retained verbatim because committed ``activity-opening.3``
    proposals replay against it; new wakes use the window-based rule below.
    """

    if plan.status != "active":
        return False
    if plan.last_transitioned_at is None:
        # An active projection without a durable start transition cannot prove
        # that enough real time has elapsed.  Failing closed keeps a scheduler
        # wake from manufacturing an immediate completion out of an incomplete
        #/legacy projection; the plan can still be paused or abandoned.
        return False
    if logical_time < plan.last_transitioned_at:
        return False
    return logical_time - plan.last_transitioned_at >= activity_minimum_completion_delta(plan)


def activity_window_completion_allowed(
    plan: PlanStateProjection, *, logical_time: datetime
) -> bool:
    """Current rule: ordinary completion tracks the accepted schedule window.

    An activity is a lived duration, not a checkbox.  Ordinary completion is
    offered once the plan's accepted window has closed, or once most of the
    planned duration (with the historical minimum floor) has actually been
    spent in the active state.  Cause-driven early exits remain available
    through pause/abandon and the interruption opening kinds, so this rule
    never traps the companion inside an activity.
    """

    if plan.status != "active":
        return False
    if plan.last_transitioned_at is None:
        return False
    if logical_time < plan.last_transitioned_at:
        return False
    elapsed = logical_time - plan.last_transitioned_at
    if elapsed < activity_minimum_completion_delta(plan):
        return False
    window = plan.scheduled_window
    if window is None:
        # Windowless legacy/host plans keep the elapsed-only floor above.
        return True
    if logical_time >= window.closes_at:
        return True
    # Deterministic integer arithmetic: four fifths of the planned duration.
    duration = window.closes_at - window.opens_at
    return elapsed * 5 >= duration * 4


# A pause or resume is a lived decision, not a scheduler flag.  Requiring a
# bounded dwell time between opposite transitions keeps a 30-second wake
# cadence from turning one ambivalent mood into a pause/resume oscillation.
MIN_ACTIVITY_TRANSITION_DWELL_SECONDS = 300


def activity_transition_dwell_elapsed(
    plan: PlanStateProjection, *, logical_time: datetime
) -> bool:
    """Whether enough time passed since the last transition to reverse it."""

    if plan.last_transitioned_at is None:
        # Without a durable transition instant the dwell cannot be proved;
        # failing closed keeps legacy projections from oscillating.
        return False
    if logical_time < plan.last_transitioned_at:
        return False
    return (
        logical_time - plan.last_transitioned_at
    ).total_seconds() >= MIN_ACTIVITY_TRANSITION_DWELL_SECONDS


__all__ = [
    "MIN_ACTIVITY_COMPLETION_SECONDS",
    "MIN_ACTIVITY_TRANSITION_DWELL_SECONDS",
    "activity_completion_allowed",
    "activity_minimum_completion_delta",
    "activity_transition_dwell_elapsed",
    "activity_window_completion_allowed",
]
