from __future__ import annotations

from datetime import timedelta

import pytest

from companion_daemon.world_v2.activity_lifecycle_draft import (
    ActivityLifecycleDraftCapsule,
    ActivityLifecycleOpening,
    materialize_activity_lifecycle_draft,
)
from companion_daemon.world_v2.activity_lifecycle_proposal import (
    ActivityLifecycleProposalCompiler,
    ActivityLifecycleProposalError,
)
from companion_daemon.world_v2.life_ecology_activity import ActivityOpeningCatalog
from companion_daemon.world_v2.life_ecology_contract import (
    life_ecology_trigger_id,
    life_ecology_trigger_ref,
)
from companion_daemon.world_v2.schemas import ClaimLease, MessageObservationRef, TriggerProcess

from test_life_ecology_activity import NOW, OWNER, WAKE_REF, _plan, _projection


ECOLOGY_CATALOG_VERSION = "life-ecology.1"


def _catalog() -> ActivityOpeningCatalog:
    return ActivityOpeningCatalog(owner_actor_ref=OWNER)


def _claimed_projection(*, revision: int = 2):
    projection = _projection(_plan("reading", entity_revision=revision))
    trigger_id = life_ecology_trigger_id(
        world_id=projection.world_id,
        wake_event_ref=WAKE_REF,
        catalog_version=ECOLOGY_CATALOG_VERSION,
    )
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=life_ecology_trigger_ref(
            wake_event_ref=WAKE_REF,
            catalog_version=ECOLOGY_CATALOG_VERSION,
        ),
        process_kind="life_ecology",
        source_evidence_ref=WAKE_REF,
        state="claimed",
        claim_lease=ClaimLease(
            owner_id="worker:life-ecology",
            attempt_id="attempt:life-ecology:1",
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        ),
        attempt_ids=("attempt:life-ecology:1",),
    )
    return projection.model_copy(update={"trigger_processes": (process,)}), trigger_id


def _selected_draft(*, projection):
    catalog_result = _catalog().openings_for(projection=projection, wake_event_ref=WAKE_REF)
    selected = catalog_result.openings[0]
    capsule = ActivityLifecycleDraftCapsule(
        situation_summary="角色有一个可继续推进的已安排活动。",
        openings=(
            ActivityLifecycleOpening(
                opening_token=selected.opening_token,
                safe_summary=selected.safe_summary,
            ),
        ),
    )
    return materialize_activity_lifecycle_draft(
        raw='{"decision":"select","opening_token":"' + selected.opening_token + '"}',
        capsule=capsule,
        model="test-flash",
    )


def test_compiler_derives_all_authority_from_the_exact_catalog_token_and_claimed_wake() -> None:
    projection, trigger_id = _claimed_projection()
    draft = _selected_draft(projection=projection)

    proposal = ActivityLifecycleProposalCompiler(
        catalog=_catalog(), ecology_catalog_version=ECOLOGY_CATALOG_VERSION
    ).compile(
        projection=projection,
        wake_event_ref=WAKE_REF,
        ecology_trigger_id=trigger_id,
        draft=draft,
    )

    assert proposal is not None
    assert proposal.plan_id == "reading"
    assert proposal.expected_plan_revision == 2
    assert proposal.operation == "start"
    assert proposal.effect_event_type == "ActivityStarted"
    assert proposal.evidence_refs[0].evidence_type == "active_plan"
    assert proposal.evidence_refs[1].ref_id == WAKE_REF
    assert proposal.evidence_refs[1].source_world_revision == 9
    assert proposal.model == "test-flash"


def test_compiler_rejects_an_unclaimed_or_wrong_ecology_trigger() -> None:
    projection, _ = _claimed_projection()
    draft = _selected_draft(projection=projection)

    with pytest.raises(ActivityLifecycleProposalError, match="ecology_trigger_not_claimed"):
        ActivityLifecycleProposalCompiler(
            catalog=_catalog(), ecology_catalog_version=ECOLOGY_CATALOG_VERSION
        ).compile(
            projection=projection,
            wake_event_ref=WAKE_REF,
            ecology_trigger_id="trigger:forged",
            draft=draft,
        )


def test_compiler_fails_closed_when_the_token_is_replayed_at_a_revised_plan() -> None:
    original, trigger_id = _claimed_projection(revision=2)
    draft = _selected_draft(projection=original)
    revised, revised_trigger_id = _claimed_projection(revision=3)
    assert trigger_id == revised_trigger_id

    with pytest.raises(ActivityLifecycleProposalError, match="opening_token_not_current"):
        ActivityLifecycleProposalCompiler(
            catalog=_catalog(), ecology_catalog_version=ECOLOGY_CATALOG_VERSION
        ).compile(
            projection=revised,
            wake_event_ref=WAKE_REF,
            ecology_trigger_id=revised_trigger_id,
            draft=draft,
        )


def test_interruption_opening_carries_the_exact_message_authority_into_the_proposal() -> None:
    projection, trigger_id = _claimed_projection(revision=2)
    observation = MessageObservationRef(
        observation_id="observation:user:interrupt",
        source="test", source_event_id="message:interrupt",
        content_payload_hash="b" * 64, event_payload_hash="c" * 64,
        world_revision=8, actor="user:geoff", channel="direct",
        payload_ref="payload:interrupt",
    )
    active = projection.plans[0].model_copy(update={
        "status": "active",
        "authority_origin": type("Origin", (), {
            "accepted_world_revision": 7,
        })(),
    })
    projection = projection.model_copy(update={
        "plans": (active,), "message_observations": (observation,),
    })
    draft = _selected_draft(projection=projection)

    proposal = ActivityLifecycleProposalCompiler(
        catalog=_catalog(), ecology_catalog_version=ECOLOGY_CATALOG_VERSION
    ).compile(
        projection=projection, wake_event_ref=WAKE_REF,
        ecology_trigger_id=trigger_id, draft=draft,
    )

    assert proposal is not None
    assert proposal.opening_kind == "interruption"
    assert tuple(item.evidence_type for item in proposal.evidence_refs) == (
        "active_plan", "committed_world_event", "observed_message",
    )
    assert proposal.evidence_refs[-1].ref_id == observation.observation_id
    assert proposal.evidence_refs[-1].immutable_hash == observation.event_payload_hash
