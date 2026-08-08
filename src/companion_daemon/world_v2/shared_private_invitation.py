"""Read-only projection helpers for historical shared-private plans.

The catalog/random/model invitation Runtime was retired with the unified
CharacterInterior cutover.  Accepted historical plans remain ordinary ledger
facts and therefore stay visible through these deterministic, source-bound
views.  This module has no model, draw, write, scheduling, or Action authority.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from .schemas import PlanStateProjection


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def shared_private_pending_plans(
    projection,
    *,
    user_participant_ref: str,
) -> tuple[PlanStateProjection, ...]:
    """Return still-planned private activities with one exact user participant."""

    return tuple(
        sorted(
            (
                plan
                for plan in getattr(projection, "plans", ())
                if plan.status == "planned"
                and plan.participant_refs == (user_participant_ref,)
                and plan.privacy_class in {"private", "withhold"}
            ),
            key=lambda plan: (plan.scheduled_window.opens_at, plan.plan_id),
        )
    )


def pending_shared_private_invitation_advisories(projection) -> tuple:
    """Compile a source-bound advisory from accepted, unexpired plan state."""

    from .context_capsule import InnerAdvisoryCandidate, InnerAdvisoryProjection

    logical_time = getattr(projection, "logical_time", None)
    if not isinstance(logical_time, datetime):
        return ()
    pending = tuple(
        plan
        for plan in getattr(projection, "plans", ())
        if plan.status == "planned"
        and len(plan.participant_refs) == 1
        and plan.participant_refs[0].startswith("user:")
        and plan.privacy_class in {"private", "withhold"}
        and plan.authority_origin is not None
        and plan.scheduled_window.closes_at > logical_time
    )[:2]
    if not pending:
        return ()
    candidates = tuple(
        InnerAdvisoryCandidate(
            candidate_ref="shared-private-invitation:" + _digest(plan.plan_id),
            value=(
                f"尚未终结的共同私密计划：{plan.activity_kind}；"
                f"计划开始时间 {plan.scheduled_window.opens_at.isoformat()}。"
            )[:256],
            weight_bp=10_000,
            confidence_bp=10_000,
        )
        for plan in pending
    )
    return (
        InnerAdvisoryProjection(
            advisory_id="advisory:shared-private-invitations:"
            + _digest(tuple(plan.plan_id for plan in pending)),
            kind="pending_shared_private_invitation",
            source_refs=tuple(
                dict.fromkeys(
                    plan.authority_origin.accepted_event_ref for plan in pending
                )
            ),
            candidate_refs=tuple(item.candidate_ref for item in candidates),
            candidates=candidates,
            confidence_bp=6_000,
            expiry=logical_time + timedelta(days=1),
            producer_version="shared-private-invitation-view.2",
        ),
    )


__all__ = [
    "pending_shared_private_invitation_advisories",
    "shared_private_pending_plans",
]
