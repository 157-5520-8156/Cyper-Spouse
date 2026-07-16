from __future__ import annotations

from types import SimpleNamespace

from companion_daemon.world_v2.media_funnel_evaluator import MediaFunnelEvaluator


def test_media_funnel_is_read_only_and_counts_lifecycle_outcomes() -> None:
    projection = SimpleNamespace(
        photo_candidates=(
            SimpleNamespace(status="available"),
            SimpleNamespace(status="unrenderable"),
            SimpleNamespace(status="shared"),
        ),
        media_opportunities=(
            SimpleNamespace(media_lane="ordinary_life"),
            SimpleNamespace(media_lane="alluring_life"),
        ),
        media_plans=(SimpleNamespace(plan_id="plan:delivered"),),
        media_inspections=(SimpleNamespace(passed=True), SimpleNamespace(passed=False)),
        media_deliveries=(SimpleNamespace(delivery_id="delivery:1", plan_id="plan:delivered"),),
        interaction_bids=(SimpleNamespace(source_plan_id="plan:delivered"),),
        budget_reservations=(
            SimpleNamespace(category="image", settled_cost=7),
            SimpleNamespace(category="chat", settled_cost=99),
        ),
    )

    report = MediaFunnelEvaluator().evaluate(projection=projection)

    assert report.candidate_status_counts == {"available": 1, "shared": 1, "unrenderable": 1}
    assert report.opportunity_lane_counts == {"alluring_life": 1, "ordinary_life": 1}
    assert report.inspection_passed_count == report.inspection_failed_count == 1
    assert report.delivery_count == report.delivery_interaction_count == 1
    assert report.image_budget_settled_cost == 7
