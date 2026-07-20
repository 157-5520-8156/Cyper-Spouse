"""World-owned delivery of inspection-passed media previews.

The decision to photograph and share was already made by the world: bounded
candidate selection (model + recorded draw), Acceptance (grant, budget and
relationship gating), frozen planning, render and visual inspection.  This
module adds no second deliberation.  It is the deployment's delivery policy:
once a preview passes inspection, it freezes the standard
``MediaAutomaticDeliveryApproved`` record under a **system** authority ref and
drives the existing approval-gated delivery Action.

Operational guardrails — a conservative daily delivery cap and a minimum gap
between sends — are runtime protections, not a human approval step.  There is
deliberately no operator button anywhere in this lane; the read-only observer
surface only reports what happened.

Fail-closed properties preserved from the existing seams:

- approval binds the exact inspected artifact hash and expires;
- the ActionPump re-verifies the approval revision on its final projection;
- a terminal failed delivery Action is not silently retried (it stays visible
  to the observer surface instead of looping provider sends).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Literal

from .media_v2 import MediaAutomaticDeliveryApproval, media_delivery_action_id
from .schema_core import FrozenModel


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MediaAutoDeliveryComposition:
    """Deployment facts for the world-owned delivery step.

    ``policy_actor`` is the system authority recorded as the approval's
    operator ref; it names the composed policy, never a human.
    """

    delivery_target_ref: str
    recipient_ref: str
    account_id: str
    amount_limit: int = 0
    policy_actor: str = "system:world-v2:media-delivery-policy"
    max_deliveries_per_day: int = 2
    min_gap: timedelta = timedelta(hours=2)
    approval_ttl: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if (
            not self.delivery_target_ref
            or not self.recipient_ref
            or not self.account_id
            or not self.policy_actor
            or self.amount_limit < 0
            or self.max_deliveries_per_day < 1
            or self.min_gap < timedelta(0)
            or self.approval_ttl <= timedelta(0)
        ):
            raise ValueError("media auto-delivery composition is invalid")


class MediaAutoDeliveryRunResult(FrozenModel):
    status: Literal[
        "delivered_attempted", "idle", "budget_exhausted", "min_gap", "unavailable"
    ]
    preview_id: str | None = None
    action_id: str | None = None
    action_status: str | None = None
    delivery_shared: bool = False


class MediaAutoDeliveryWorker:
    """Advance at most one inspection-passed preview into a delivery Action."""

    def __init__(
        self,
        *,
        application,  # WorldV2TurnApplication (structural; avoids an import cycle)
        ledger,
        composition: MediaAutoDeliveryComposition,
    ) -> None:
        self._application = application
        self._ledger = ledger
        self._composition = composition

    async def drain_once(
        self, *, trace_id: str, correlation_id: str
    ) -> MediaAutoDeliveryRunResult:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        if not isinstance(logical_time, datetime):
            return MediaAutoDeliveryRunResult(status="unavailable")
        plans = {item.plan_id: item for item in projection.media_plans}
        inspections = {item.inspection_id: item for item in projection.media_inspections}
        artifacts = {item.artifact_id: item for item in projection.media_artifacts}
        approvals_by_id: dict[str, list] = {}
        for approval in projection.media_delivery_approvals:
            approvals_by_id.setdefault(approval.approval_id, []).append(approval)
        delivered_plan_ids = {item.plan_id for item in projection.media_deliveries}
        actions_by_id = {item.action_id: item for item in projection.actions}

        # Guardrails count send decisions (approvals), not just confirmed
        # deliveries, so a pending/failed dispatch still consumes the slot.
        # They gate only *new* decisions below; recovering an already-made
        # decision (approval without its delivered Action) is never blocked.
        window_start = logical_time - timedelta(days=1)
        recent = tuple(
            item
            for item in projection.media_delivery_approvals
            if item.operator_ref == self._composition.policy_actor
            and window_start <= item.approved_at <= logical_time
        )
        budget_exhausted = (
            len({item.approval_id for item in recent})
            >= self._composition.max_deliveries_per_day
        )
        last_approved_at = max((item.approved_at for item in recent), default=None)
        blocked: Literal["budget_exhausted", "min_gap"] | None = None

        for preview in sorted(projection.media_previews, key=lambda item: item.preview_id):
            plan = plans.get(preview.plan_id)
            inspection = inspections.get(preview.inspection_id)
            artifact = artifacts.get(preview.artifact_id)
            if plan is None or inspection is None or artifact is None or not inspection.passed:
                continue
            if preview.plan_id in delivered_plan_ids:
                continue
            approval_id = _approval_id(preview.preview_id)
            revisions = approvals_by_id.get(approval_id, ())
            latest = max(revisions, key=lambda item: item.entity_revision, default=None)
            if latest is not None:
                action = actions_by_id.get(
                    media_delivery_action_id(
                        world_id=self._ledger.world_id,
                        approval_id=approval_id,
                        approval_revision=latest.entity_revision,
                    )
                )
                if action is not None and action.state in {
                    "failed", "cancelled", "expired", "unknown", "delivered",
                }:
                    # Terminal under the current revision: a confirmed send is
                    # done, and a terminal non-delivery stays closed rather
                    # than looping provider sends of a human-visible artifact.
                    continue
                if logical_time >= latest.expires_at:
                    if action is not None:
                        # An in-flight Action under a lapsed approval can no
                        # longer dispatch (the pump gate rejects it); leave it
                        # to recovery/observation instead of a new decision.
                        continue
                    # The prior decision lapsed un-dispatched (for example the
                    # process was down past the TTL).  It still consumed its
                    # daily slot above; a fresh revision may be issued below.
                    latest = None
                else:
                    # Approval is current and its Action is absent or still in
                    # flight: continue that exact decision instead of a new one.
                    return await self._deliver(
                        approval_id=approval_id,
                        approval_revision=latest.entity_revision,
                        preview_id=preview.preview_id,
                        plan_id=preview.plan_id,
                        logical_time=logical_time,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
            if budget_exhausted:
                blocked = "budget_exhausted"
                continue
            if (
                last_approved_at is not None
                and logical_time - last_approved_at < self._composition.min_gap
            ):
                blocked = blocked or "min_gap"
                continue
            approval = MediaAutomaticDeliveryApproval(
                approval_id=approval_id,
                entity_revision=(
                    max((item.entity_revision for item in revisions), default=0) + 1
                ),
                plan_id=plan.plan_id,
                inspection_id=inspection.inspection_id,
                artifact_id=artifact.artifact_id,
                artifact_hash=artifact.artifact_hash,
                sample_hash=artifact.artifact_hash,
                recipient_ref=self._composition.recipient_ref,
                delivery_target_ref=self._composition.delivery_target_ref,
                operator_ref=self._composition.policy_actor,
                family=plan.family,
                approved_at=logical_time,
                expires_at=logical_time + self._composition.approval_ttl,
            )
            recorded = await self._application.approve_media_automatic_delivery(
                approval=approval,
                trace_id=trace_id,
                correlation_id=correlation_id,
                causation_id=f"policy:{preview.preview_id}",
            )
            return await self._deliver(
                approval_id=recorded.approval_id,
                approval_revision=recorded.entity_revision,
                preview_id=preview.preview_id,
                plan_id=preview.plan_id,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        if blocked is not None:
            return MediaAutoDeliveryRunResult(status=blocked)
        return MediaAutoDeliveryRunResult(status="idle")

    async def _deliver(
        self,
        *,
        approval_id: str,
        approval_revision: int,
        preview_id: str,
        plan_id: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> MediaAutoDeliveryRunResult:
        result = await self._application.deliver_approved_media_once(
            approval_id=approval_id,
            approval_revision=approval_revision,
            actor=self._composition.policy_actor,
            target=self._composition.delivery_target_ref,
            account_id=self._composition.account_id,
            amount_limit=self._composition.amount_limit,
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        after = self._ledger.project()
        delivery_shared = any(item.plan_id == plan_id for item in after.media_deliveries)
        _LOG.warning(
            "world v2 media auto-delivery preview=%s revision=%d status=%s shared=%s",
            preview_id,
            approval_revision,
            getattr(result, "status", None) if result is not None else "unavailable",
            delivery_shared,
        )
        return MediaAutoDeliveryRunResult(
            status="delivered_attempted",
            preview_id=preview_id,
            action_id=getattr(result, "action_id", None) if result is not None else None,
            action_status=(
                str(getattr(result, "status", None)) if result is not None else None
            ),
            delivery_shared=delivery_shared,
        )


def _approval_id(preview_id: str) -> str:
    return f"approval:media:{preview_id}"


__all__ = [
    "MediaAutoDeliveryComposition",
    "MediaAutoDeliveryRunResult",
    "MediaAutoDeliveryWorker",
]
