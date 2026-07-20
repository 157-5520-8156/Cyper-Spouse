from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.activity_timing import (
    activity_completion_allowed,
    activity_minimum_completion_delta,
    activity_window_completion_allowed,
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


def test_replay_keeps_the_persisted_v2_operation_matrix_while_v3_is_stricter() -> None:
    plan = _active_plan(started_at=NOW - timedelta(seconds=20))

    legacy_operations = ActivityOpeningCatalog._operations_for(  # noqa: SLF001
        plan,
        logical_time=NOW,
        catalog_version="activity-opening.2",
    )

    assert legacy_operations == ("pause", "complete", "abandon")


def test_incomplete_active_projection_does_not_offer_completion() -> None:
    plan = _active_plan(started_at=NOW).model_copy(update={"last_transitioned_at": None})

    assert not activity_completion_allowed(plan, logical_time=NOW)
    operations = ActivityOpeningCatalog._operations_for(plan, logical_time=NOW)  # noqa: SLF001
    assert operations == ("pause", "abandon")


def test_current_catalog_keeps_an_activity_running_through_most_of_its_window() -> None:
    started_at = NOW - timedelta(minutes=5)
    plan = _active_plan(started_at=started_at)  # 40-minute window

    # Five minutes into a forty-minute activity is not ordinary completion,
    # even though the old one-minute floor has long elapsed.
    assert not activity_window_completion_allowed(plan, logical_time=NOW)
    operations = ActivityOpeningCatalog._operations_for(plan, logical_time=NOW)  # noqa: SLF001
    assert operations == ("pause", "abandon")


def test_current_catalog_offers_completion_after_most_of_the_planned_duration() -> None:
    started_at = NOW - timedelta(minutes=33)
    plan = _active_plan(started_at=started_at)  # 40-minute window, 4/5 = 32min

    assert activity_window_completion_allowed(plan, logical_time=NOW)
    operations = ActivityOpeningCatalog._operations_for(plan, logical_time=NOW)  # noqa: SLF001
    assert operations == ("pause", "complete", "abandon")


def test_current_catalog_offers_completion_once_the_window_has_closed() -> None:
    started_at = NOW - timedelta(minutes=45)
    plan = _active_plan(started_at=started_at)  # closed 6 minutes ago

    assert activity_window_completion_allowed(plan, logical_time=NOW)
    operations = ActivityOpeningCatalog._operations_for(plan, logical_time=NOW)  # noqa: SLF001
    assert operations == ("pause", "complete", "abandon")


def test_replayed_v3_proposals_keep_the_elapsed_only_completion_rule() -> None:
    plan = _active_plan(started_at=NOW - timedelta(minutes=5))

    operations = ActivityOpeningCatalog._operations_for(  # noqa: SLF001
        plan,
        logical_time=NOW,
        catalog_version="activity-opening.3",
    )

    assert operations == ("pause", "complete", "abandon")


def test_future_commitments_are_not_offered_to_ordinary_wakes() -> None:
    """A three-days-away plan must not surface "abandon" every 30 seconds."""

    future = _active_plan(started_at=NOW).model_copy(update={
        "status": "planned",
        "last_transitioned_at": None,
        "scheduled_window": DueWindow(
            opens_at=NOW + timedelta(days=3),
            closes_at=NOW + timedelta(days=3, hours=1),
        ),
    })

    assert ActivityOpeningCatalog._operations_for(future, logical_time=NOW) == ()  # noqa: SLF001
    # Replay of committed v4 proposals keeps the old exposed matrix.
    assert ActivityOpeningCatalog._operations_for(  # noqa: SLF001
        future, logical_time=NOW, catalog_version="activity-opening.4"
    ) == ("abandon",)
    # An overdue plan is a stalled present, not a protected future: it stays
    # abandonnable so the lifecycle can clear it.
    overdue = future.model_copy(update={
        "scheduled_window": DueWindow(
            opens_at=NOW - timedelta(hours=2), closes_at=NOW - timedelta(hours=1)
        ),
    })
    assert ActivityOpeningCatalog._operations_for(overdue, logical_time=NOW) == (  # noqa: SLF001
        "abandon",
    )


def test_fresh_transitions_cannot_be_reversed_until_the_dwell_elapses() -> None:
    """Ordinary pause and plan-authority resume wait out the dwell gate.

    Cause-bound pauses (interruption/user influence) stay immediate, so the
    gate is checked at the catalog level where the authority shape is known.
    """

    catalog = ActivityOpeningCatalog(owner_actor_ref="actor:companion")
    just_started = _active_plan(started_at=NOW - timedelta(seconds=60))
    settled_in = _active_plan(started_at=NOW - timedelta(minutes=6))
    just_paused = just_started.model_copy(update={"status": "paused"})
    long_paused = settled_in.model_copy(update={"status": "paused"})

    def suppressed(plan, operation, opening_kind):  # type: ignore[no-untyped-def]
        return catalog._dwell_suppressed(  # noqa: SLF001
            plan=plan, operation=operation, opening_kind=opening_kind, logical_time=NOW
        )

    assert suppressed(just_started, "pause", "ordinary")
    assert not suppressed(settled_in, "pause", "ordinary")
    # In production ``.5`` every started activity was abandoned on the next
    # scheduler wake because "abandon" was the only ordinary operation left
    # during the dwell.  A just-started activity simply is not up for
    # ordinary revision at all.
    assert suppressed(just_started, "abandon", "ordinary")
    assert suppressed(just_paused, "abandon", "ordinary")
    assert not suppressed(settled_in, "abandon", "ordinary")
    assert not suppressed(just_started, "abandon", "interruption")
    # A missed/overdue *planned* plan stays clearable at any age.
    missed = just_started.model_copy(update={"status": "planned"})
    assert not suppressed(missed, "abandon", "ordinary")
    # A real interruption (user message / clock conflict) is never throttled,
    # and neither is the repair-resume after it: pausing for the user and
    # picking the book back up a minute later is the human sequence.  The
    # ordinary-pause gate alone bounds oscillation to the dwell period.
    assert not suppressed(just_started, "pause", "interruption")
    assert not suppressed(just_started, "pause", "user_influence")
    assert not suppressed(just_paused, "resume", "repair")
    assert not suppressed(long_paused, "resume", "repair")
    # Committed v4 proposals replay against the un-throttled matrix.
    v4 = ActivityOpeningCatalog(
        owner_actor_ref="actor:companion", catalog_version="activity-opening.4"
    )
    assert not v4._dwell_suppressed(  # noqa: SLF001
        plan=just_started, operation="pause", opening_kind="ordinary", logical_time=NOW
    )
    # Committed v5 proposals replay with the pause dwell but without the
    # abandon dwell, exactly as they were offered.
    v5 = ActivityOpeningCatalog(
        owner_actor_ref="actor:companion", catalog_version="activity-opening.5"
    )
    assert v5._dwell_suppressed(  # noqa: SLF001
        plan=just_started, operation="pause", opening_kind="ordinary", logical_time=NOW
    )
    assert not v5._dwell_suppressed(  # noqa: SLF001
        plan=just_started, operation="abandon", opening_kind="ordinary", logical_time=NOW
    )
