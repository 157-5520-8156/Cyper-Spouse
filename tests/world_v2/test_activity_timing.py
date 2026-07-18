from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.activity_timing import (
    activity_completion_allowed,
    activity_minimum_completion_delta,
)
from companion_daemon.world_v2.life_ecology_activity import ActivityOpeningCatalog
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import DueWindow, PlanStateProjection


NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


def _active_plan(*, started_at: datetime) -> PlanStateProjection:
    return PlanStateProjection(
        plan_id="plan:reading",
        activity_id="activity:reading",
        entity_revision=2,
        activity_kind="study.reading",
        evidence_refs=(EvidenceRef(
            ref_id="event:plan:reading",
            evidence_type="committed_world_event",
            claim_purpose="life_transition",
            source_world_revision=1,
            immutable_hash="a" * 64,
        ),),
        status="active",
        importance_bp=5_000,
        scheduled_window=DueWindow(
            opens_at=started_at - timedelta(minutes=1),
            closes_at=started_at + timedelta(minutes=39),
        ),
        last_transitioned_at=started_at,
        owner_actor_ref="actor:companion",
    )


def test_activity_completion_requires_elapsed_time_from_start() -> None:
    plan = _active_plan(started_at=NOW - timedelta(seconds=20))

    assert activity_minimum_completion_delta(plan) == timedelta(minutes=1)
    assert not activity_completion_allowed(plan, logical_time=NOW)

    after_minimum = plan.last_transitioned_at + activity_minimum_completion_delta(plan)
    assert activity_completion_allowed(plan, logical_time=after_minimum)


def test_activity_catalog_does_not_offer_early_completion() -> None:
    plan = _active_plan(started_at=NOW - timedelta(seconds=20))

    operations = ActivityOpeningCatalog._operations_for(plan, logical_time=NOW)  # noqa: SLF001
    assert operations == ("pause", "abandon")
