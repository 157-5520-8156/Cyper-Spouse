from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.life_ecology_activity import ActivityOpeningCatalog
from companion_daemon.world_v2.clock_authority import CLOCK_AUTHORITY_POLICY_DIGEST
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import (
    ClockTransitionProjection,
    CommittedWorldEventRef,
    DueWindow,
    LedgerProjection,
    PlanStateProjection,
)


NOW = datetime(2026, 7, 16, 16, tzinfo=UTC)
WORLD_ID = "world:life-ecology-activity"
WAKE_REF = "event:clock:opening"
WAKE_HASH = "a" * 64
POLICY_DIGEST = CLOCK_AUTHORITY_POLICY_DIGEST
OWNER = "actor:companion"


def _plan(
    plan_id: str,
    *,
    status: str = "planned",
    entity_revision: int = 1,
    location_ref: str | None = None,
    participant_refs: tuple[str, ...] = (),
    scheduled_window: DueWindow | None = None,
    owner_actor_ref: str = OWNER,
) -> PlanStateProjection:
    return PlanStateProjection(
        plan_id=plan_id,
        activity_id=f"activity:{plan_id}",
        entity_revision=entity_revision,
        activity_kind="quiet_reading",
        evidence_refs=(
            EvidenceRef(
                ref_id=f"observation:{plan_id}",
                evidence_type="observed_message",
                claim_purpose="future_plan",
            ),
        ),
        status=status,  # type: ignore[arg-type]
        importance_bp=4000,
        scheduled_window=scheduled_window,
        participant_refs=participant_refs,
        location_ref=location_ref,
        privacy_class="private",
        owner_actor_ref=owner_actor_ref,
    )


def _projection(
    *plans: PlanStateProjection,
    wake_ref: str = WAKE_REF,
    wake_hash: str = WAKE_HASH,
    clock_payload_hash: str | None = None,
    wake_type: str = "ClockAdvanced",
) -> LedgerProjection:
    # The catalog is a read-only projection consumer.  Production projections
    # have already passed the reducer's cross-entity Plan authority validator;
    # these compact fixtures construct only the immutable fields the catalog
    # is allowed to read.
    return LedgerProjection.model_construct(
        world_id=WORLD_ID,
        world_revision=9,
        deliberation_revision=3,
        ledger_sequence=12,
        semantic_hash="d" * 64,
        logical_time=NOW,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=wake_ref,
                event_type=wake_type,
                world_revision=9,
                payload_hash=clock_payload_hash or wake_hash,
                logical_time=NOW,
            ),
        ),
        clock_transition_history=(
            ClockTransitionProjection(
                clock_event_ref=wake_ref,
                computed_world_revision=9,
                payload_hash=wake_hash,
                logical_time_from=NOW - timedelta(minutes=5),
                logical_time_to=NOW,
                installed_policy_version="world-clock-authority.1",
                installed_policy_digest=POLICY_DIGEST,
            ),
        ),
        plans=plans,
    )


def _catalog() -> ActivityOpeningCatalog:
    return ActivityOpeningCatalog(owner_actor_ref=OWNER, catalog_version="activity-opening.1")


def test_catalog_enumerates_the_exact_state_successor_matrix_with_opaque_tokens() -> None:
    result = _catalog().openings_for(
        projection=_projection(
            _plan("planned", status="planned"),
            _plan("active", status="active", entity_revision=2),
            _plan("paused", status="paused", entity_revision=3),
        ),
        wake_event_ref=WAKE_REF,
    )

    assert result.status == "openings_available"
    assert [opening.operation for opening in result.openings] == [
        "pause",
        "complete",
        "abandon",
        "resume",
        "abandon",
        "start",
        "abandon",
    ]
    assert all(len(opening.opening_token) == 64 for opening in result.openings)
    assert all("plan:" not in opening.safe_summary for opening in result.openings)
    assert result.blocked_plan_count == 0


def test_catalog_is_deterministic_and_binds_tokens_to_the_pinned_plan_revision() -> None:
    original = _projection(_plan("reading", entity_revision=1))
    repeated = _projection(_plan("reading", entity_revision=1))
    revised = _projection(_plan("reading", entity_revision=2))

    first = _catalog().openings_for(projection=original, wake_event_ref=WAKE_REF)
    again = _catalog().openings_for(projection=repeated, wake_event_ref=WAKE_REF)
    changed = _catalog().openings_for(projection=revised, wake_event_ref=WAKE_REF)

    assert first == again
    assert first.catalog_hash != changed.catalog_hash
    assert first.openings[0].opening_token != changed.openings[0].opening_token


def test_catalog_allows_only_abstract_companion_owned_plans_on_the_first_vertical() -> None:
    result = _catalog().openings_for(
        projection=_projection(
            _plan("abstract"),
            _plan("somewhere", location_ref="location:library"),
            _plan("with-npc", participant_refs=("npc:lin",)),
            _plan("with-other-actor", participant_refs=("actor:other",)),
            _plan("other-owner", owner_actor_ref="actor:someone-else"),
        ),
        wake_event_ref=WAKE_REF,
    )

    assert result.status == "openings_available"
    assert len(result.openings) == 2
    assert result.blocked_plan_count == 3
    assert result.blocked_capabilities == (
        "location_authority_binding",
        "npc_availability",
        "participant_availability",
    )


@pytest.mark.parametrize(
    ("plan", "expected_capability"),
    [
        (_plan("location", location_ref="location:library"), "location_authority_binding"),
        (_plan("npc", participant_refs=("npc:lin",)), "npc_availability"),
    ],
)
def test_catalog_reports_missing_capability_not_idle_when_every_live_plan_is_excluded(
    plan: PlanStateProjection, expected_capability: str
) -> None:
    result = _catalog().openings_for(
        projection=_projection(plan), wake_event_ref=WAKE_REF
    )

    assert result.status == "blocked_by_missing_capability"
    assert result.openings == ()
    assert result.blocked_plan_count == 1
    assert result.blocked_capabilities == (expected_capability,)


def test_catalog_reports_no_openings_for_terminal_or_foreign_plans_without_claiming_missing_capability() -> None:
    result = _catalog().openings_for(
        projection=_projection(
            _plan("done", status="completed"),
            _plan("foreign", owner_actor_ref="actor:other"),
        ),
        wake_event_ref=WAKE_REF,
    )

    assert result.status == "no_openings"
    assert result.openings == ()
    assert result.blocked_plan_count == 0
    assert result.blocked_capabilities == ()


@pytest.mark.parametrize(
    "window",
    [
        DueWindow(opens_at=NOW + timedelta(minutes=1), closes_at=NOW + timedelta(minutes=5)),
        DueWindow(opens_at=NOW - timedelta(minutes=5), closes_at=NOW),
    ],
)
def test_catalog_never_offers_start_outside_the_accepted_plan_window(window: DueWindow) -> None:
    result = _catalog().openings_for(
        projection=_projection(_plan("windowed", scheduled_window=window)),
        wake_event_ref=WAKE_REF,
    )

    assert result.status == "openings_available"
    assert [opening.operation for opening in result.openings] == ["abandon"]


def test_catalog_rejects_a_wake_without_an_exact_clock_authority_binding() -> None:
    projection = _projection(_plan("reading"), clock_payload_hash="c" * 64)
    result = _catalog().openings_for(projection=projection, wake_event_ref=WAKE_REF)

    assert result.status == "rejected_wake"
    assert result.reason_code == "activity_opening.wake_not_exact_clock_authority"
    assert result.openings == ()


def test_catalog_is_pure_and_never_mutates_the_supplied_projection() -> None:
    projection = _projection(_plan("reading"))
    before = projection.model_dump(mode="json")

    _catalog().openings_for(projection=projection, wake_event_ref=WAKE_REF)

    assert projection.model_dump(mode="json") == before
