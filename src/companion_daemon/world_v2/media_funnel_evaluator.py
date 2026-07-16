"""Read-only Phase-8 diagnostics for the World v2 media lifecycle.

The evaluator deliberately consumes a projection only.  It never chooses a
candidate, reopens a terminal media item, changes a budget, or feeds a score
back into Deliberation.  This makes funnel and cost reporting useful for
offline iteration without creating a second World authority.
"""

from __future__ import annotations

from collections import Counter

from pydantic import Field

from .schema_core import FrozenModel


class MediaFunnelReport(FrozenModel):
    evaluator_version: str = "world-v2-media-funnel.1"
    candidate_status_counts: dict[str, int]
    opportunity_lane_counts: dict[str, int]
    planned_count: int = Field(ge=0)
    inspection_passed_count: int = Field(ge=0)
    inspection_failed_count: int = Field(ge=0)
    delivery_count: int = Field(ge=0)
    delivery_interaction_count: int = Field(ge=0)
    image_budget_settled_cost: int = Field(ge=0)


class MediaFunnelEvaluator:
    """Aggregate frozen media outcomes into a stable, non-authoritative report."""

    version = "world-v2-media-funnel.1"

    def evaluate(self, *, projection: object) -> MediaFunnelReport:
        candidates = tuple(getattr(projection, "photo_candidates", ()))
        opportunities = tuple(getattr(projection, "media_opportunities", ()))
        plans = tuple(getattr(projection, "media_plans", ()))
        inspections = tuple(getattr(projection, "media_inspections", ()))
        deliveries = tuple(getattr(projection, "media_deliveries", ()))
        interaction_bids = tuple(getattr(projection, "interaction_bids", ()))
        candidate_statuses = Counter(str(item.status) for item in candidates)
        opportunity_lanes = Counter(str(item.media_lane) for item in opportunities)
        delivered_plan_ids = {item.plan_id for item in deliveries}
        delivered_interactions = sum(
            1
            for bid in interaction_bids
            if getattr(bid, "source_plan_id", None) in delivered_plan_ids
            or getattr(bid, "delivery_ref", None) in {item.delivery_id for item in deliveries}
        )
        image_cost = sum(
            max(0, int(getattr(item, "settled_cost", 0) or 0))
            for item in getattr(projection, "budget_reservations", ())
            if getattr(item, "category", None) == "image"
        )
        return MediaFunnelReport(
            candidate_status_counts=dict(sorted(candidate_statuses.items())),
            opportunity_lane_counts=dict(sorted(opportunity_lanes.items())),
            planned_count=len(plans),
            inspection_passed_count=sum(bool(item.passed) for item in inspections),
            inspection_failed_count=sum(not bool(item.passed) for item in inspections),
            delivery_count=len(deliveries),
            delivery_interaction_count=delivered_interactions,
            image_budget_settled_cost=image_cost,
        )


__all__ = ["MediaFunnelEvaluator", "MediaFunnelReport"]
