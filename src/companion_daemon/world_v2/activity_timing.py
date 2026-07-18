"""Deterministic timing rules for source-bound activity lifecycles.

An activity may be completed early when its owner has a real reason to do so,
but a scheduler tick must not make a multi-minute plan disappear after one
short wake.  This module is intentionally pure so the catalog and the reducer
share the same rule without either side calling the model or the ledger.
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
    """Whether an active plan has enough elapsed authority for completion."""

    if plan.status != "active" or plan.last_transitioned_at is None:
        # A missing transition timestamp is retained for legacy projections and
        # compact authority fixtures; it cannot prove an early completion.
        return True
    if logical_time < plan.last_transitioned_at:
        return False
    return logical_time - plan.last_transitioned_at >= activity_minimum_completion_delta(plan)


__all__ = [
    "MIN_ACTIVITY_COMPLETION_SECONDS",
    "activity_completion_allowed",
    "activity_minimum_completion_delta",
]
