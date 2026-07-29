from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from legacy_migration_support import read_head_state_json
import pytest

from companion_daemon.world_v2.biographical_lifecycle import (
    BiographicalLifecycleCatalog,
    LifeArcChangedPayload,
)
from companion_daemon.world_v2.biographical_lifecycle_runtime import (
    BiographicalLifecycleRuntime,
)
from companion_daemon.world_v2.batch_invariants import (
    appraisal_trigger_identity,
    validate_commit_batch,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.life_aftermath_runtime import (
    LifeAftermathModelFailure,
    LifeAftermathRuntime,
)
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.occurrence_content_coordinator import (
    OccurrenceContentCoordinator,
)
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.life_events import (
    OutcomeObservationRecordedPayload,
    OutcomeProposalRecordedPayload,
    WorldOccurrenceActivatedPayload,
    WorldOccurrenceCommittedPayload,
    WorldOccurrenceSettledPayload,
    outcome_mutation_hash,
)
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    DynamicLifeArcContextDescriptor,
    DueWindow,
    EvidenceRef,
    FrozenLifeArcEffectDescriptor,
    LifeArcProjection,
    OutcomeCandidateDescriptor,
    OutcomeObservationProjection,
    PendingBiographicalSettlementProjection,
    ProvisionalNpcIntroductionDescriptor,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
    WorldOccurrenceProjection,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_context_v2_character_outcome_cannot_be_submitted_without_its_model_batch() -> None:
    event = _event(
        world_id="world:standalone-context-v2",
        event_id="event:standalone-context-v2",
        event_type="OutcomeProposalRecorded",
        logical_at=datetime(2026, 7, 29, tzinfo=UTC),
        payload={
            "decision_authority": "character_model",
            "context_identity_version": "life-aftermath-context.2",
        },
    )

    with pytest.raises(
        ValueError,
        match="complete pinned model-to-settlement transaction",
    ):
        validate_commit_batch((event,), expected_world_revision=0)


def test_biography_derives_age_and_academic_phase_from_logical_time() -> None:
    lifecycle = BiographicalLifecycleCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        timezone_name="Asia/Shanghai",
    )

    summer = lifecycle.context_at(
        datetime(2026, 7, 28, 10, 30, tzinfo=SHANGHAI),
        life_arcs=(),
    )
    after_graduation = lifecycle.context_at(
        datetime(2028, 7, 1, 10, 30, tzinfo=SHANGHAI),
        life_arcs=(),
    )

    assert summer.age == 21
    assert summer.academic_phase == "summer_break"
    assert summer.academic_year == 2
    assert "academic:enrolled" in summer.context_tags
    assert "calendar:summer_break" in summer.context_tags
    assert "season:summer" in summer.context_tags
    assert "calendar:classes_open" not in summer.context_tags

    assert after_graduation.age == 23
    assert after_graduation.academic_phase == "graduated"
    assert after_graduation.academic_year is None
    assert "academic:graduated" in after_graduation.context_tags
    assert "season:summer" in after_graduation.context_tags
    assert "academic:enrolled" not in after_graduation.context_tags
    assert "residence:campus_dorm" not in after_graduation.context_tags


def test_biography_derives_one_continuous_residence_and_active_arc_overrides_it() -> None:
    lifecycle = BiographicalLifecycleCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        timezone_name="Asia/Shanghai",
    )
    term_at = datetime(2026, 4, 10, 10, 30, tzinfo=SHANGHAI)
    winter_at = datetime(2027, 2, 1, 10, 30, tzinfo=SHANGHAI)
    summer_at = datetime(2026, 7, 28, 10, 30, tzinfo=SHANGHAI)
    graduated_at = datetime(2028, 7, 3, 10, 30, tzinfo=SHANGHAI)
    temporary_home = LifeArcProjection(
        arc_id="life-arc:temporary-jiaxing-home",
        entity_revision=1,
        owner_actor_ref="actor:companion",
        arc_kind="travel",
        context_pack_ref="life-context:jiaxing-family-home-stay",
        context_tags=(
            "residence:temporary_family_home_jiaxing",
            "travel:visiting_jiaxing",
        ),
        status="active",
        started_at=summer_at - timedelta(days=1),
        ends_at=summer_at + timedelta(days=2),
        source_event_ref="event:settlement:jiaxing-homecoming",
        privacy_class="private",
    )

    term = lifecycle.context_at(term_at, life_arcs=())
    winter = lifecycle.context_at(winter_at, life_arcs=())
    summer = lifecycle.context_at(summer_at, life_arcs=())
    graduated = lifecycle.context_at(graduated_at, life_arcs=())
    overridden = lifecycle.context_at(summer_at, life_arcs=(temporary_home,))

    residence = lambda context: tuple(  # noqa: E731
        item for item in context.context_tags if item.startswith("residence:")
    )
    assert residence(term) == ("residence:campus_dorm",)
    assert residence(winter) == ("residence:family_home_jiaxing",)
    assert residence(summer) == ("residence:family_home_jiaxing",)
    assert residence(graduated) == ("residence:shanghai_home",)
    assert residence(overridden) == ("residence:temporary_family_home_jiaxing",)
    assert "travel:visiting_jiaxing" in overridden.context_tags


def test_life_arc_is_event_sourced_and_cold_replays(tmp_path: Path) -> None:
    world_id = "world:biography:replay"
    logical_at = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    path = tmp_path / "biography.sqlite"
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    clock = _event(
        world_id=world_id,
        event_id="clock:internship-start",
        event_type="ClockAdvanced",
        logical_at=logical_at,
        payload={
            "logical_time_from": (logical_at - timedelta(minutes=10)).isoformat(),
            "logical_time_to": logical_at.isoformat(),
        },
    )
    ledger.commit(
        (clock,),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    settlement, effect = _commit_settled_effect_occurrence(
        ledger=ledger,
        clock=clock,
        occurrence_id="occurrence:internship-start",
        context_pack_ref="life-context:publishing-internship",
        context_tags=("role:intern", "workplace:publishing"),
        duration_days=30,
    )
    settlement_ref = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_id == settlement.event_id
    )
    evidence = EvidenceRef(
        ref_id=settlement.event_id,
        evidence_type="settled_world_event",
        claim_purpose="future_plan",
        source_world_revision=settlement_ref.world_revision,
        immutable_hash=settlement_ref.payload_hash,
    )
    arc = LifeArcProjection(
        arc_id="life-arc:publishing-internship:2026-summer",
        entity_revision=1,
        owner_actor_ref="actor:companion",
        arc_kind="employment",
        context_pack_ref="life-context:publishing-internship",
        context_tags=("role:intern", "workplace:publishing"),
        effect_descriptor_hash=effect.descriptor_hash,
        status="active",
        started_at=logical_at,
        ends_at=logical_at + timedelta(days=30),
        closed_at=None,
        source_event_ref=settlement.event_id,
        privacy_class="personal",
    )
    payload = LifeArcChangedPayload(
        change_id="change:life-arc:internship:start",
        transition_id="transition:life-arc:internship:1",
        expected_entity_revision=0,
        evidence_refs=(evidence,),
        policy_refs=("policy:biographical-lifecycle.1",),
        operation="start",
        arc_before=None,
        arc_after=arc,
    )
    legacy_payload = payload.model_dump(mode="json")
    legacy_payload["arc_after"].pop("accepted_event_ref", None)
    started = _event(
        world_id=world_id,
        event_id="event:life-arc:internship:start",
        event_type="LifeArcChanged",
        logical_at=logical_at,
        payload=legacy_payload,
    )
    head = ledger.project()
    ledger.commit(
        (started,),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )

    projected = ledger.project()
    accepted_arc = arc.model_copy(
        update={"accepted_event_ref": started.event_id}
    )
    assert projected.life_arcs == (accepted_arc,)
    ledger.close()

    # Recreate an honest .41 head. Its immutable LifeArcChanged payload,
    # cached projection, and hashes predate the reducer-owned binding. Opening
    # under .42 must verify those old bytes, then cold-replay the event.
    with sqlite3.connect(path) as connection:
        legacy_state = json.loads(read_head_state_json(connection, world_id))
        legacy_state["life_arcs"][0].pop("accepted_event_ref", None)
        canonical_state = json.dumps(
            legacy_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy_model = ReducerState.model_validate_json(
            canonical_state,
            context={"source_reducer_bundle": "world-v2-reducers.41"},
        )
        legacy_semantic = legacy_model.semantic_payload(
            world_id=world_id,
            world_revision=projected.world_revision,
            reducer_bundle_version="world-v2-reducers.41",
        )
        legacy_semantic_hash = hashlib.sha256(
            json.dumps(
                legacy_semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        cursor_json = json.dumps(
            {
                "world_revision": projected.world_revision,
                "deliberation_revision": projected.deliberation_revision,
                "ledger_sequence": projected.ledger_sequence,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy_state_hash = hashlib.sha256(
            (
                '{"cursor":'
                + cursor_json
                + ',"reducer_bundle_version":"world-v2-reducers.41","state":'
                + canonical_state
                + ',"world_id":'
                + json.dumps(world_id)
                + "}"
            ).encode()
        ).hexdigest()
        connection.execute(
            "DELETE FROM world_v2_head_state_items WHERE world_id = ?",
            (world_id,),
        )
        connection.execute(
            """UPDATE world_v2_heads
               SET state_json = ?, semantic_hash = ?,
                   reducer_bundle_version = ?, state_hash = ?
               WHERE world_id = ?""",
            (
                canonical_state,
                legacy_semantic_hash,
                "world-v2-reducers.41",
                legacy_state_hash,
                world_id,
            ),
        )

    migrated = SQLiteWorldLedger(path=path, world_id=world_id)
    reopened = migrated.project()
    assert reopened.reducer_bundle_version == "world-v2-reducers.44"
    assert reopened.life_arcs == projected.life_arcs
    assert migrated.rebuild() == reopened
    migrated.close()


def test_life_catalog_obeys_academic_phase_and_active_life_arc(
    legacy_story_seed_path: Path,
) -> None:
    lifecycle = BiographicalLifecycleCatalog.from_yaml(
        path=legacy_story_seed_path,
        timezone_name="Asia/Shanghai",
    )
    from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
    from companion_daemon.world_v2.local_chronology import LocalChronology

    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    summer_at = datetime(2026, 7, 28, 10, 30, tzinfo=SHANGHAI)
    term_at = datetime(2026, 9, 8, 10, 30, tzinfo=SHANGHAI)
    internship = LifeArcProjection(
        arc_id="life-arc:publishing-internship:2026-summer",
        entity_revision=1,
        owner_actor_ref="actor:companion",
        arc_kind="employment",
        context_pack_ref="life-context:publishing-internship",
        context_tags=("role:intern", "workplace:publishing"),
        status="active",
        started_at=summer_at - timedelta(days=1),
        ends_at=summer_at + timedelta(days=29),
        closed_at=None,
        source_event_ref="event:accepted-internship",
        privacy_class="personal",
    )

    summer = catalog.candidates_at(
        instant=summer_at,
        wake_event_ref="clock:summer",
        plans=(),
        npcs=(),
        life_arcs=(),
    )
    term = catalog.candidates_at(
        instant=term_at,
        wake_event_ref="clock:term",
        plans=(),
        npcs=(),
        life_arcs=(),
    )
    working = catalog.candidates_at(
        instant=summer_at,
        wake_event_ref="clock:working",
        plans=(),
        npcs=(),
        life_arcs=(internship,),
    )
    graduated = catalog.candidates_at(
        instant=datetime(2028, 7, 3, 10, 30, tzinfo=SHANGHAI),
        wake_event_ref="clock:graduated",
        plans=(),
        npcs=tuple(
            SimpleNamespace(npc_id=item.npc_id, status="active")
            for item in catalog.reviewed_npcs
        ),
        life_arcs=(),
    )

    assert lifecycle.context_at(summer_at, life_arcs=()).academic_phase == "summer_break"
    assert "study.attend_class" not in {item.opening.activity_kind for item in summer}
    assert "study.attend_class" in {item.opening.activity_kind for item in term}
    assert "work.publishing_shift" not in {item.opening.activity_kind for item in summer}
    assert "work.publishing_shift" in {item.opening.activity_kind for item in working}
    assert "study.attend_class" not in {item.opening.activity_kind for item in graduated}
    assert "career.publishing_job_search" in {item.opening.activity_kind for item in graduated}
    assert "routine.morning_settle" not in {item.opening.activity_kind for item in graduated}


def test_graduated_catalog_removes_student_only_life_without_flattening_city_life(
    legacy_story_seed_path: Path,
) -> None:
    from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
    from companion_daemon.world_v2.local_chronology import LocalChronology

    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    active_npcs = tuple(
        SimpleNamespace(npc_id=item.npc_id, status="active")
        for item in catalog.reviewed_npcs
    )
    offered: set[str] = set()
    for local_day in (3, 8):  # Monday and Saturday cover weekday/weekend life.
        for hour in (7, 10, 12, 15, 19, 22):
            instant = datetime(2028, 7, local_day, hour, 15, tzinfo=SHANGHAI)
            offered.update(
                item.opening.id
                for item in catalog.candidates_at(
                    instant=instant,
                    wake_event_ref=f"clock:graduated:{local_day}:{hour}",
                    plans=(),
                    npcs=active_npcs,
                    life_arcs=(),
                )
            )

    assert offered.isdisjoint(
        {
            "attend-lecture",
            "essay-deadline-push",
            "do-laundry",
            "literature-club-reading-list",
            "literature-club-admin",
            "print-shop-run",
            "pick-up-parcel",
            "canteen-meal",
        }
    )
    assert {
        "focused-reading",
        "short-walk",
        "write-reading-notes",
        "browse-old-book-stall",
    } <= offered

    future = {
        item.opening.id
        for item in catalog.future_candidates_at(
            instant=datetime(2028, 7, 3, 10, 15, tzinfo=SHANGHAI),
            plans=(),
            npcs=active_npcs,
            life_arcs=(),
            horizon_days=7,
            max_candidates=128,
        )
    }
    assert future.isdisjoint(
        {
            "future-literature-club-meetup",
            "future-library-seminar-room",
        }
    )
    assert {
        "future-lakeside-walk",
        "future-book-market-hunt",
        "future-bund-night-photo",
    } <= future


def test_life_arc_opens_and_retires_its_contextual_npc_on_clock_boundaries(
    tmp_path: Path,
    legacy_story_seed_path: Path,
) -> None:
    from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
    from companion_daemon.world_v2.local_chronology import LocalChronology

    world_id = "world:biography:contextual-npc"
    started_at = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    path = tmp_path / "contextual-npc.sqlite"
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    clock = _event(
        world_id=world_id,
        event_id="clock:contextual-npc:start",
        event_type="ClockAdvanced",
        logical_at=started_at,
        payload={
            "logical_time_from": (started_at - timedelta(minutes=10)).isoformat(),
            "logical_time_to": started_at.isoformat(),
        },
    )
    ledger.commit(
        (clock,),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    _commit_settled_effect_occurrence(
        ledger=ledger,
        clock=clock,
        occurrence_id="occurrence:contextual-internship",
        context_pack_ref="life-context:publishing-internship",
        context_tags=("role:intern", "workplace:publishing"),
        duration_days=30,
    )
    runtime = BiographicalLifecycleRuntime(
        ledger=ledger,
        catalog=ReviewedLifeSeedCatalog.from_yaml(
                path=legacy_story_seed_path,
            chronology=LocalChronology("Asia/Shanghai"),
        ),
        owner_actor_ref="actor:companion",
    )

    introduced = runtime.advance_once(
        wake_event_ref=clock.event_id,
        trace_id="trace:introduce",
        correlation_id="correlation:contextual-npc",
    )

    assert introduced.status == "transitioned"
    assert introduced.npc_ids == ("editor-qin",)
    assert next(item for item in ledger.project().npcs if item.npc_id == "editor-qin").status == (
        "active"
    )

    expired_at = started_at + timedelta(days=31)
    expiry_clock = _event(
        world_id=world_id,
        event_id="clock:contextual-npc:expiry",
        event_type="ClockAdvanced",
        logical_at=expired_at,
        payload={
            "logical_time_from": started_at.isoformat(),
            "logical_time_to": expired_at.isoformat(),
        },
    )
    head = ledger.project()
    ledger.commit(
        (expiry_clock,),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    retired = runtime.advance_once(
        wake_event_ref=expiry_clock.event_id,
        trace_id="trace:retire",
        correlation_id="correlation:contextual-npc",
    )
    replayed = SQLiteWorldLedger(path=path, world_id=world_id).project()

    assert retired.status == "transitioned"
    assert replayed.life_arcs[0].status == "completed"
    assert next(item for item in replayed.npcs if item.npc_id == "editor-qin").status == ("retired")


def test_settled_outcome_maps_to_a_reviewed_long_lived_effect(
    legacy_story_seed_path: Path,
) -> None:
    from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
    from companion_daemon.world_v2.local_chronology import LocalChronology

    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )

    effect = catalog.life_arc_effect_for_settlement(
        activity_kind="career.publishing_intern_interview",
        candidate_result_ref="candidate:any:publishing-interview-offer",
    )
    declined = catalog.life_arc_effect_for_settlement(
        activity_kind="career.publishing_intern_interview",
        candidate_result_ref="candidate:any:publishing-interview-no-fit",
    )

    assert effect is not None
    assert effect.duration_days == 30
    assert effect.context_tags == ("role:intern", "workplace:publishing")
    assert declined is None


def test_life_arc_effect_is_frozen_with_the_outcome_candidate_authority(
    legacy_story_seed_path: Path,
) -> None:
    from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
    from companion_daemon.world_v2.local_chronology import LocalChronology

    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )

    frozen = catalog.frozen_life_arc_effect_for_outcome(
        activity_kind="career.publishing_intern_interview",
        outcome_id="publishing-interview-offer",
    )

    assert frozen is not None
    assert frozen.catalog_version == catalog.version
    assert frozen.catalog_hash == catalog.catalog_hash
    assert frozen.context_pack_ref == "life-context:publishing-internship"
    assert frozen.descriptor_hash == frozen.canonical_hash()


@pytest.mark.asyncio
async def test_long_lived_outcome_model_failure_cannot_fall_back_to_random() -> None:
    effect = FrozenLifeArcEffectDescriptor.create(
        arc_kind="employment",
        context_pack_ref="life-context:test-employment",
        context_tags=("role:test",),
        duration_days=30,
        privacy_class="personal",
        catalog_version="reviewed-test.1",
        catalog_hash="c" * 64,
    )
    runtime = object.__new__(LifeAftermathRuntime)
    runtime._outcome_selection = _FailingOutcomeSelection()  # noqa: SLF001
    runtime._candidate_text = lambda *_args: "reviewed outcome"  # type: ignore[method-assign]  # noqa: SLF001
    occurrence = SimpleNamespace(
        candidate_outcomes=(
            OutcomeCandidateDescriptor(
                candidate_result_ref="candidate:accept",
                result_id="result:accept",
                result_payload_ref="content:accept",
                result_payload_hash="b" * 64,
                privacy_class="personal",
                life_arc_effect=effect,
            ),
            OutcomeCandidateDescriptor(
                candidate_result_ref="candidate:decline",
                result_id="result:decline",
                result_payload_ref="content:decline",
                result_payload_hash="d" * 64,
                privacy_class="personal",
            ),
        )
    )

    with pytest.raises(LifeAftermathModelFailure, match="model unavailable"):
        await runtime._select_long_lived_outcome(  # noqa: SLF001
            occurrence=occurrence,
            projection=SimpleNamespace(affect_episodes=()),
            decision_context={"current_self_state": {}},
        )


def test_long_lived_outcome_proposal_records_complete_pinned_context_identity() -> None:
    context_cursor = ProjectionCursor(
        world_revision=7,
        deliberation_revision=3,
        ledger_sequence=10,
    )
    change_hash = outcome_mutation_hash(
        change_id="change:long-lived",
        occurrence_id="occurrence:long-lived",
        evaluated_entity_revision=3,
        evaluated_world_revision=7,
        candidate_result_ref="candidate:accepted",
        result_id="result:accepted",
        result_payload_ref="content:accepted",
        result_payload_hash="b" * 64,
        observation_refs=("observation:long-lived",),
    )

    payload = OutcomeProposalRecordedPayload(
        outcome_proposal_id="proposal:long-lived",
        decision_proposal_id="proposal:long-lived",
        change_id="change:long-lived",
        occurrence_id="occurrence:long-lived",
        evaluated_entity_revision=3,
        evaluated_world_revision=7,
        trigger_ref="plan:long-lived",
        candidate_result_ref="candidate:accepted",
        proposed_result_id="result:accepted",
        proposed_result_payload_ref="content:accepted",
        proposed_result_payload_hash="b" * 64,
        proposed_change_hash=change_hash,
        observation_refs=("observation:long-lived",),
        evidence_refs=(
            EvidenceRef(
                ref_id="event:clock",
                evidence_type="committed_world_event",
                claim_purpose="life_transition",
                source_world_revision=1,
                immutable_hash="a" * 64,
            ),
        ),
        confidence_bp=10_000,
        expires_at=datetime(2026, 7, 29, tzinfo=UTC),
        decision_authority="character_model",
        decision_model="test-character-model",
        decision_raw_output_hash="c" * 64,
        context_identity_version="life-aftermath-context.1",
        context_capsule_id="d" * 64,
        context_model_content_hash="e" * 64,
        context_snapshot_hash="f" * 64,
        context_cursor=context_cursor,
    )

    assert payload.context_cursor == context_cursor
    assert payload.decision_model == "test-character-model"


@pytest.mark.asyncio
async def test_long_lived_outcome_is_atomic_and_restart_does_not_repeat_the_model(
    tmp_path: Path,
) -> None:
    world_id = "world:biography:consequential-outcome"
    started_at = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    ledger = SQLiteWorldLedger(
        path=tmp_path / "consequential-outcome.sqlite",
        world_id=world_id,
    )
    first_clock = _event(
        world_id=world_id,
        event_id="clock:consequential:start",
        event_type="ClockAdvanced",
        logical_at=started_at,
        payload={
            "logical_time_from": (started_at - timedelta(minutes=10)).isoformat(),
            "logical_time_to": started_at.isoformat(),
        },
    )
    _commit_event(ledger, first_clock)
    occurrence, candidate_texts = _commit_active_long_lived_occurrence(
        ledger=ledger,
        clock=first_clock,
    )
    wake_at = started_at + timedelta(minutes=10)
    wake = _event(
        world_id=world_id,
        event_id="clock:consequential:settle",
        event_type="ClockAdvanced",
        logical_at=wake_at,
        payload={
            "logical_time_from": started_at.isoformat(),
            "logical_time_to": wake_at.isoformat(),
        },
    )
    _commit_event(ledger, wake)

    content_store = InMemoryImmutableLifeContentStore()
    for candidate, text in zip(occurrence.candidate_outcomes, candidate_texts, strict=True):
        assert candidate.content_ref is not None
        assert candidate.content_payload_hash is not None
        content_store.put_if_absent(
            StoredLifeContent(
                content_ref=candidate.content_ref,
                content_kind="outcome_candidate",
                content_payload_hash=candidate.content_payload_hash,
                text=text,
            )
        )
    model = _CapturingLongLivedOutcomeModel(
        selected_ref=occurrence.candidate_outcomes[0].candidate_result_ref,
        invalid_first=True,
    )
    capsules = _PinnedCapsuleCompiler(ledger=ledger)
    runtime = LifeAftermathRuntime(
        ledger=ledger,
        catalog=SimpleNamespace(),
        occurrence_content=OccurrenceContentCoordinator(
            ledger=ledger,
            store=content_store,
        ),
        content_store=content_store,
        owner_actor_ref="actor:companion",
        capsule_compiler=capsules,
        outcome_selection_model=model,
    )

    record_model_result = runtime._record_outcome_model_result  # noqa: SLF001

    def crash_after_durable_model_result(**kwargs):  # type: ignore[no-untyped-def]
        record_model_result(**kwargs)
        raise RuntimeError("simulated crash after durable outcome model audit")

    runtime._record_outcome_model_result = crash_after_durable_model_result  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(RuntimeError, match="durable outcome model audit"):
        await runtime.advance_once(
            wake_event_ref=wake.event_id,
            trace_id="trace:consequential:first",
            correlation_id="correlation:consequential",
        )

    assert model.calls == 2
    assert ledger.project().world_occurrences[0].status == "settled"
    assert len(ledger.project().model_result_audits) == 2
    audited_calls = [
        json.loads(item.audit_json)
        for item in ledger.project().model_result_audits
    ]
    assert audited_calls[0]["request_hash"] != audited_calls[1]["request_hash"]
    assert ledger.lookup_event_commit(
        "event:life-aftermath:outcome-proposal:consequential"
    ) is not None
    assert model.last_material["current_character_context"] == capsules.context

    recovery_at = wake_at + timedelta(minutes=10)
    recovery_wake = _event(
        world_id=world_id,
        event_id="clock:consequential:recovery",
        event_type="ClockAdvanced",
        logical_at=recovery_at,
        payload={
            "logical_time_from": wake_at.isoformat(),
            "logical_time_to": recovery_at.isoformat(),
        },
    )
    _commit_event(ledger, recovery_wake)
    restarted = LifeAftermathRuntime(
        ledger=ledger,
        catalog=SimpleNamespace(),
        occurrence_content=OccurrenceContentCoordinator(
            ledger=ledger,
            store=content_store,
        ),
        content_store=content_store,
        owner_actor_ref="actor:companion",
        capsule_compiler=_PinnedCapsuleCompiler(ledger=ledger),
        outcome_selection_model=model,
    )

    recovered = await restarted.advance_once(
        wake_event_ref=recovery_wake.event_id,
        trace_id="trace:consequential:recovery",
        correlation_id="correlation:consequential",
    )

    assert recovered.status == "recovered_experience"
    assert model.calls == 2
    settled = ledger.project()
    settlement_ref = next(
        item
        for item in settled.committed_world_event_refs
        if item.event_type == "WorldOccurrenceSettled"
    )
    proposal_event, proposal_commit = ledger.lookup_event_commit(
        "event:life-aftermath:outcome-proposal:consequential"
    )
    _, settlement_commit = ledger.lookup_event_commit(settlement_ref.event_id)
    proposal = OutcomeProposalRecordedPayload.model_validate_json(
        proposal_event.payload_json
    )
    assert proposal.context_cursor is not None
    assert proposal.context_cursor.world_revision == proposal.evaluated_world_revision
    assert proposal.context_capsule_id == capsules.last_capsule.capsule_id
    assert proposal.context_identity_version == "life-aftermath-context.2"
    assert proposal.decision_model_result_ref is not None
    assert proposal.decision_model_result_event_ref is not None
    assert proposal.decision_audit_proposal_event_ref is not None
    assert proposal.decision_audit_proposal_event_payload_hash is not None
    assert proposal.decision_model == model.model
    assert proposal_commit.event_ids == settlement_commit.event_ids
    semantic_audit = json.loads(
        ledger.project().proposal_audits[-1].proposal_json
    )
    bound_choice = json.loads(semantic_audit["response_text"])
    assert bound_choice["candidate_result_ref"] == proposal.candidate_result_ref
    assert bound_choice["adopt_proposed_life_direction"] is False


@pytest.mark.parametrize(
    (
        "failure_kind",
        "model_kwargs",
        "expected_audit_statuses",
        "calls_after_failure",
    ),
    (
        ("timeout", {"fail_first": True}, ("main_timeout",), 1),
        (
            "double-invalid",
            {"invalid_responses": 2},
            ("main_invalid", "recovery_failed"),
            2,
        ),
    ),
)
@pytest.mark.asyncio
async def test_long_lived_model_failure_retries_on_a_later_wake_without_a_second_observation(
    tmp_path: Path,
    failure_kind: str,
    model_kwargs: dict[str, object],
    expected_audit_statuses: tuple[str, ...],
    calls_after_failure: int,
) -> None:
    world_id = "world:biography:consequential-retry:" + failure_kind
    started_at = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    ledger = SQLiteWorldLedger(
        path=tmp_path / "consequential-retry.sqlite",
        world_id=world_id,
    )
    first_clock = _clock_advance(
        world_id=world_id,
        event_id="clock:consequential-retry:start",
        origin=started_at - timedelta(minutes=10),
        target=started_at,
    )
    _commit_event(ledger, first_clock)
    occurrence, candidate_texts = _commit_active_long_lived_occurrence(
        ledger=ledger,
        clock=first_clock,
    )
    content_store = InMemoryImmutableLifeContentStore()
    for candidate, text in zip(occurrence.candidate_outcomes, candidate_texts, strict=True):
        assert candidate.content_ref is not None
        assert candidate.content_payload_hash is not None
        content_store.put_if_absent(
            StoredLifeContent(
                content_ref=candidate.content_ref,
                content_kind="outcome_candidate",
                content_payload_hash=candidate.content_payload_hash,
                text=text,
            )
        )
    model = _CapturingLongLivedOutcomeModel(
        selected_ref=occurrence.candidate_outcomes[0].candidate_result_ref,
        **model_kwargs,
    )
    runtime = LifeAftermathRuntime(
        ledger=ledger,
        catalog=SimpleNamespace(),
        occurrence_content=OccurrenceContentCoordinator(
            ledger=ledger,
            store=content_store,
        ),
        content_store=content_store,
        owner_actor_ref="actor:companion",
        capsule_compiler=_PinnedCapsuleCompiler(ledger=ledger),
        outcome_selection_model=model,
    )
    failed_wake = _clock_advance(
        world_id=world_id,
        event_id="clock:consequential-retry:failed",
        origin=started_at,
        target=started_at + timedelta(minutes=10),
    )
    _commit_event(ledger, failed_wake)

    with pytest.raises(LifeAftermathModelFailure, match="durable audit"):
        await runtime.advance_once(
            wake_event_ref=failed_wake.event_id,
            trace_id="trace:consequential-retry:failed",
            correlation_id="correlation:consequential-retry",
        )
    assert len(ledger.project().outcome_observations) == 1
    failed_audits = ledger.project().model_result_audits
    assert len(failed_audits) == len(expected_audit_statuses)
    decoded_audits = [json.loads(item.audit_json) for item in failed_audits]
    assert tuple(item["status"] for item in decoded_audits) == expected_audit_statuses
    assert len({item["request_hash"] for item in decoded_audits}) == len(
        decoded_audits
    )
    if failure_kind == "double-invalid":
        assert all(item["response_hash"] for item in decoded_audits)
        assert ledger.project().proposal_audits == ()

    early_wake = _clock_advance(
        world_id=world_id,
        event_id="clock:consequential-retry:early",
        origin=started_at + timedelta(minutes=10),
        target=started_at + timedelta(minutes=19),
    )
    _commit_event(ledger, early_wake)
    early = await runtime.advance_once(
        wake_event_ref=early_wake.event_id,
        trace_id="trace:consequential-retry:early",
        correlation_id="correlation:consequential-retry",
    )
    assert early.status == "retry_wait"
    assert model.calls == calls_after_failure

    retry_wake = _clock_advance(
        world_id=world_id,
        event_id="clock:consequential-retry:success",
        origin=started_at + timedelta(minutes=19),
        target=started_at + timedelta(minutes=20),
    )
    _commit_event(ledger, retry_wake)
    result = await runtime.advance_once(
        wake_event_ref=retry_wake.event_id,
        trace_id="trace:consequential-retry:success",
        correlation_id="correlation:consequential-retry",
    )

    assert result.status == "settled"
    assert model.calls == calls_after_failure + 1
    assert len(ledger.project().outcome_observations) == 1


@pytest.mark.asyncio
async def test_outcome_retry_lane_backs_off_ten_thirty_then_one_twenty_minutes(
    tmp_path: Path,
) -> None:
    world_id = "world:biography:consequential-backoff"
    started_at = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    ledger = SQLiteWorldLedger(
        path=tmp_path / "consequential-backoff.sqlite",
        world_id=world_id,
    )
    first_clock = _clock_advance(
        world_id=world_id,
        event_id="clock:consequential-backoff:start",
        origin=started_at - timedelta(minutes=10),
        target=started_at,
    )
    _commit_event(ledger, first_clock)
    occurrence, candidate_texts = _commit_active_long_lived_occurrence(
        ledger=ledger,
        clock=first_clock,
    )
    store = InMemoryImmutableLifeContentStore()
    for candidate, text in zip(
        occurrence.candidate_outcomes,
        candidate_texts,
        strict=True,
    ):
        assert candidate.content_ref is not None
        assert candidate.content_payload_hash is not None
        store.put_if_absent(
            StoredLifeContent(
                content_ref=candidate.content_ref,
                content_kind="outcome_candidate",
                content_payload_hash=candidate.content_payload_hash,
                text=text,
            )
        )
    model = _CapturingLongLivedOutcomeModel(
        selected_ref=occurrence.candidate_outcomes[0].candidate_result_ref,
        fail_calls=3,
    )
    runtime = LifeAftermathRuntime(
        ledger=ledger,
        catalog=SimpleNamespace(),
        occurrence_content=OccurrenceContentCoordinator(
            ledger=ledger,
            store=store,
        ),
        content_store=store,
        owner_actor_ref="actor:companion",
        capsule_compiler=_PinnedCapsuleCompiler(ledger=ledger),
        outcome_selection_model=model,
    )

    async def advance_at(
        minute: int,
        *,
        origin_minute: int,
    ):
        wake = _clock_advance(
            world_id=world_id,
            event_id=f"clock:consequential-backoff:{minute}",
            origin=started_at + timedelta(minutes=origin_minute),
            target=started_at + timedelta(minutes=minute),
        )
        _commit_event(ledger, wake)
        return await runtime.advance_once(
            wake_event_ref=wake.event_id,
            trace_id=f"trace:consequential-backoff:{minute}",
            correlation_id="correlation:consequential-backoff",
        )

    with pytest.raises(LifeAftermathModelFailure):
        await advance_at(10, origin_minute=0)
    assert (await advance_at(19, origin_minute=10)).status == "retry_wait"
    with pytest.raises(LifeAftermathModelFailure):
        await advance_at(20, origin_minute=19)
    assert (await advance_at(49, origin_minute=20)).status == "retry_wait"
    with pytest.raises(LifeAftermathModelFailure):
        await advance_at(50, origin_minute=49)
    assert (await advance_at(169, origin_minute=50)).status == "retry_wait"
    settled = await advance_at(170, origin_minute=169)

    assert settled.status == "settled"
    assert model.calls == 4
    failure_audits = [
        json.loads(item.audit_json)
        for item in ledger.project().model_result_audits
        if ":retry:" in item.attempt_id
    ]
    assert [item["status"] for item in failure_audits] == [
        "main_timeout",
        "main_timeout",
        "main_timeout",
    ]


def test_frozen_effect_is_excluded_from_legacy_semantic_hash_material() -> None:
    effect = FrozenLifeArcEffectDescriptor.create(
        arc_kind="employment",
        context_pack_ref="life-context:test-employment",
        context_tags=("role:test",),
        duration_days=30,
        privacy_class="personal",
        catalog_version="reviewed-test.1",
        catalog_hash="c" * 64,
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:legacy-effect-hash",
        entity_revision=1,
        trigger_ref="trigger:legacy-effect-hash",
        participant_refs=("actor:companion",),
        location_ref="location:test",
        time_window=DueWindow(
            opens_at=datetime(2026, 7, 1, tzinfo=UTC),
            closes_at=datetime(2026, 7, 2, tzinfo=UTC),
        ),
        candidate_outcome_refs=("candidate:accepted",),
        candidate_outcomes=(
            OutcomeCandidateDescriptor(
                candidate_result_ref="candidate:accepted",
                result_id="result:accepted",
                result_payload_ref="content:accepted",
                result_payload_hash="b" * 64,
                privacy_class="personal",
                life_arc_effect=effect,
            ),
        ),
        visibility="personal",
        status="committed",
    )
    state = ReducerState(world_occurrences=(occurrence,))

    legacy = state.semantic_payload(
        world_id="world:legacy-effect-hash",
        world_revision=1,
        reducer_bundle_version="world-v2-reducers.40",
    )
    v41 = state.semantic_payload(
        world_id="world:legacy-effect-hash",
        world_revision=1,
        reducer_bundle_version="world-v2-reducers.41",
    )
    current = state.semantic_payload(
        world_id="world:legacy-effect-hash",
        world_revision=1,
    )

    assert "life_arc_effect" not in legacy["world_occurrences"][0][
        "candidate_outcomes"
    ][0]
    legacy_42 = state.semantic_payload(
        world_id="world:legacy-effect-hash",
        world_revision=1,
        reducer_bundle_version="world-v2-reducers.42",
    )
    assert (
        "settled_dynamic_life_direction_adopted"
        not in legacy_42["world_occurrences"][0]
    )
    assert current["world_occurrences"][0]["candidate_outcomes"][0][
        "life_arc_effect"
    ]["descriptor_hash"] == effect.descriptor_hash
    assert v41["world_occurrences"][0]["candidate_outcomes"][0][
        "life_arc_effect"
    ]["descriptor_hash"] == effect.descriptor_hash


def test_life_arc_acceptance_is_excluded_from_v41_semantic_hash_material() -> None:
    arc = LifeArcProjection(
        arc_id="life-arc:v41-hash",
        entity_revision=1,
        owner_actor_ref="actor:companion",
        arc_kind="employment",
        context_pack_ref="life-context:v41-hash",
        context_tags=("role:v41-hash",),
        status="active",
        started_at=datetime(2026, 7, 28, 4, 0, tzinfo=UTC),
        source_event_ref="event:settlement:v41-hash",
        accepted_event_ref="event:life-arc:v41-hash:start",
        privacy_class="personal",
    )
    state = ReducerState(life_arcs=(arc,))

    legacy = state.semantic_payload(
        world_id="world:legacy-life-arc-acceptance",
        world_revision=2,
        reducer_bundle_version="world-v2-reducers.41",
    )
    current = state.semantic_payload(
        world_id="world:legacy-life-arc-acceptance",
        world_revision=2,
    )

    assert "accepted_event_ref" not in legacy["life_arcs"][0]
    assert (
        current["life_arcs"][0]["accepted_event_ref"]
        == arc.accepted_event_ref
    )


def test_recovery_orders_frozen_effects_and_preserves_their_effective_time() -> None:
    now = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)
    older_at = now - timedelta(days=60)
    newer_at = now - timedelta(days=10)
    older = _settlement_fixture(
        event_id="event:settlement:older",
        occurrence_id="occurrence:older",
        settled_at=older_at,
        duration_days=30,
        context_pack_ref="life-context:older",
    )
    newer = _settlement_fixture(
        event_id="event:settlement:newer",
        occurrence_id="occurrence:newer",
        settled_at=newer_at,
        duration_days=30,
        context_pack_ref="life-context:newer",
    )
    wake = _event(
        world_id="world:biography:backlog",
        event_id="clock:recovery",
        event_type="ClockAdvanced",
        logical_at=now,
        payload={
            "logical_time_from": (now - timedelta(minutes=10)).isoformat(),
            "logical_time_to": now.isoformat(),
        },
    )
    refs = (
        _committed_ref(older[0], world_revision=1),
        _committed_ref(newer[0], world_revision=2),
        _committed_ref(wake, world_revision=3),
    )
    projection = SimpleNamespace(
        logical_time=now,
        committed_world_event_refs=refs,
        pending_biographical_settlements=tuple(
            PendingBiographicalSettlementProjection(
                settlement_event_ref=event.event_id,
                settlement_world_revision=index,
                settlement_payload_hash=event.payload_hash,
                occurrence_id=occurrence.occurrence_id,
                candidate_result_ref=occurrence.settled_outcome_ref,
                settled_at=occurrence.settled_at,
                life_arc_effect=occurrence.candidate_outcomes[0].life_arc_effect,
            )
            for index, (event, occurrence) in enumerate((older, newer), start=1)
        ),
        life_arcs=(),
        plans=(),
        world_occurrences=(older[1], newer[1]),
        npcs=(),
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=3,
    )
    ledger = _CapturingLedger(
        world_id="world:biography:backlog",
        projection=projection,
        events={
            older[0].event_id: older[0],
            newer[0].event_id: newer[0],
            wake.event_id: wake,
        },
    )
    runtime = BiographicalLifecycleRuntime(
        ledger=ledger,
        catalog=_NoNpcCatalog(),
        owner_actor_ref="actor:companion",
    )

    result = runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:backlog",
        correlation_id="correlation:backlog",
    )

    transitions = [
        LifeArcChangedPayload.model_validate_json(item.payload_json)
        for item in ledger.committed_events
        if item.event_type == "LifeArcChanged"
    ]
    assert result.status == "transitioned"
    assert [item.arc_after.context_pack_ref for item in transitions] == [
        "life-context:older",
        "life-context:older",
        "life-context:newer",
    ]
    assert transitions[0].arc_after.started_at == older_at
    assert transitions[1].arc_after.closed_at == older_at + timedelta(days=30)
    assert transitions[2].arc_after.started_at == newer_at


def test_expired_recovered_effect_starts_and_completes_atomically(
    tmp_path: Path,
) -> None:
    world_id = "world:biography:expired-recovery"
    settled_at = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    recovered_at = settled_at + timedelta(days=60)
    ledger = SQLiteWorldLedger(
        path=tmp_path / "expired-recovery.sqlite",
        world_id=world_id,
    )
    first_clock = _event(
        world_id=world_id,
        event_id="clock:expired-recovery:settle",
        event_type="ClockAdvanced",
        logical_at=settled_at,
        payload={
            "logical_time_from": (settled_at - timedelta(minutes=10)).isoformat(),
            "logical_time_to": settled_at.isoformat(),
        },
    )
    _commit_event(ledger, first_clock)
    settlement, _ = _commit_settled_effect_occurrence(
        ledger=ledger,
        clock=first_clock,
        occurrence_id="occurrence:expired-recovery",
        context_pack_ref="life-context:expired-recovery",
        context_tags=("role:expired",),
        duration_days=30,
    )
    pending = ledger.project().pending_biographical_settlements
    assert tuple(item.settlement_event_ref for item in pending) == (settlement.event_id,)
    recovery_clock = _event(
        world_id=world_id,
        event_id="clock:expired-recovery:resume",
        event_type="ClockAdvanced",
        logical_at=recovered_at,
        payload={
            "logical_time_from": settled_at.isoformat(),
            "logical_time_to": recovered_at.isoformat(),
        },
    )
    _commit_event(ledger, recovery_clock)
    runtime = BiographicalLifecycleRuntime(
        ledger=ledger,
        catalog=_NoNpcCatalog(),
        owner_actor_ref="actor:companion",
    )

    result = runtime.advance_once(
        wake_event_ref=recovery_clock.event_id,
        trace_id="trace:expired-recovery",
        correlation_id="correlation:expired-recovery",
    )

    final_projection = ledger.project()
    arc = final_projection.life_arcs[0]
    arc_commits = [
        ledger.lookup_event_commit(item.event_id)[1]
        for item in ledger.project().committed_world_event_refs
        if item.event_type == "LifeArcChanged"
    ]
    assert result.status == "transitioned"
    assert arc.source_event_ref == settlement.event_id
    assert arc.status == "completed"
    assert arc.started_at == settled_at
    assert arc.ends_at == settled_at + timedelta(days=30)
    assert arc.closed_at == arc.ends_at
    assert len(arc_commits) == 2
    assert arc_commits[0].event_ids == arc_commits[1].event_ids
    assert len(
        [event_id for event_id in arc_commits[0].event_ids if ":life-arc:" in event_id]
    ) == 2
    assert not final_projection.pending_biographical_settlements


@pytest.mark.parametrize(
    ("adopt_direction", "expected_arc_count"),
    ((True, 1), (False, 0)),
)
def test_selected_open_outcome_registers_npc_and_only_adopted_dynamic_arc_once(
    tmp_path: Path,
    adopt_direction: bool,
    expected_arc_count: int,
) -> None:
    world_id = "world:biography:open-effects"
    logical_at = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
    ledger = SQLiteWorldLedger(
        path=tmp_path / "open-effects.sqlite",
        world_id=world_id,
    )
    clock = _clock_advance(
        world_id=world_id,
        event_id="clock:open-effects",
        origin=logical_at - timedelta(minutes=10),
        target=logical_at,
    )
    _commit_event(ledger, clock)
    store = InMemoryImmutableLifeContentStore()
    npc_text = "她是在旧书店临时认识的店员，双方此前没有共同历史。"
    arc_text = "她决定接下来几周偶尔去旧书店帮忙整理新到的旧书。"
    npc_ref = "content:open-life:npc:bookshop-clerk"
    arc_ref = "content:open-life:arc:bookshop-help"
    npc = ProvisionalNpcIntroductionDescriptor.create(
        provisional_entity_ref="provisional:npc:bookshop-clerk",
        summary_content_ref=npc_ref,
        summary_payload_hash=life_content_payload_hash(npc_text),
        narrative_tags=("narrative:bookshop", "narrative:new_acquaintance"),
        privacy_class="personal",
    )
    arc = DynamicLifeArcContextDescriptor.create(
        summary_content_ref=arc_ref,
        summary_payload_hash=life_content_payload_hash(arc_text),
        narrative_tags=("narrative:bookshop", "narrative:volunteering"),
        duration_days=21,
        privacy_class="personal",
    )
    store.put_if_absent(
        StoredLifeContent(
            content_ref=npc_ref,
            content_kind="provisional_npc_introduction",
            content_payload_hash=npc.summary_payload_hash,
            text=npc_text,
        )
    )
    store.put_if_absent(
        StoredLifeContent(
            content_ref=arc_ref,
            content_kind="dynamic_life_arc_context",
            content_payload_hash=arc.summary_payload_hash,
            text=arc_text,
        )
    )
    settlement, _ = _commit_settled_effect_occurrence(
        ledger=ledger,
        clock=clock,
        occurrence_id="occurrence:open-effects",
        context_pack_ref="unused:reviewed",
        context_tags=("unused:reviewed",),
        duration_days=1,
        dynamic_life_arc_context=arc,
        provisional_npc_introductions=(npc,),
        include_reviewed_life_arc_effect=False,
        adopt_dynamic_life_direction=adopt_direction,
    )

    assert ledger.project().npcs == ()
    assert ledger.project().life_arcs == ()
    runtime = BiographicalLifecycleRuntime(
        ledger=ledger,
        catalog=_NoNpcCatalog(),
        owner_actor_ref="actor:companion",
        content_store=store,
    )
    first = runtime.advance_once(
        wake_event_ref=settlement.event_id,
        trace_id="trace:open-effects",
        correlation_id="correlation:open-effects",
    )
    second = runtime.advance_once(
        wake_event_ref=settlement.event_id,
        trace_id="trace:open-effects:retry",
        correlation_id="correlation:open-effects",
    )

    projection = ledger.project()
    assert first.status == "transitioned"
    assert second.status == "idle"
    assert len(projection.npcs) == 1
    assert projection.npcs[0].source_event_ref == settlement.event_id
    assert projection.npcs[0].effect_descriptor_hash == npc.descriptor_hash
    assert len(projection.life_arcs) == expected_arc_count
    if adopt_direction:
        assert projection.life_arcs[0].arc_kind == "dynamic"
        assert projection.life_arcs[0].context_pack_ref == arc.summary_content_ref
        assert projection.life_arcs[0].context_tags == arc.narrative_tags
    assert not projection.pending_biographical_settlements


class _NoNpcCatalog:
    reviewed_npcs = ()
    reviewed_locations = ()

    @staticmethod
    def biographical_context_at(*, instant: datetime, life_arcs: tuple[object, ...]):
        del instant, life_arcs
        return SimpleNamespace(context_tags=())

    @staticmethod
    def contextual_npcs(_context):
        return ()


class _FailingOutcomeSelection:
    async def deliberate(self, **_kwargs):
        raise TimeoutError("provider unavailable")


class _CapturingLongLivedOutcomeModel:
    model = "test-character-consequential-outcome"

    def __init__(
        self,
        *,
        selected_ref: str,
        fail_first: bool = False,
        fail_calls: int = 0,
        invalid_first: bool = False,
        invalid_responses: int = 0,
    ) -> None:
        self._selected_ref = selected_ref
        self._fail_first = fail_first
        self._fail_calls = max(fail_calls, 1 if fail_first else 0)
        self._invalid_first = invalid_first
        self._invalid_responses = max(
            invalid_responses,
            1 if invalid_first else 0,
        )
        self.calls = 0
        self.last_material: dict[str, object] = {}

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.2
        self.calls += 1
        self.last_material = json.loads(messages[1]["content"])
        if self.calls <= self._fail_calls:
            raise TimeoutError("simulated first provider failure")
        if self.calls <= self._invalid_responses:
            return json.dumps(
                {
                    "candidate_result_ref": "candidate:not-offered",
                    "adopt_proposed_life_direction": False,
                }
            )
        return json.dumps(
            {
                "candidate_result_ref": self._selected_ref,
                "adopt_proposed_life_direction": False,
            }
        )


class _PinnedCapsuleCompiler:
    context = {
        "current_self_state": {
            "character_core": {"values": ["autonomy"]},
            "personality_state": {"availability": "present"},
        },
        "relationships": [{"subject_ref": "user:test", "stage": "friend"}],
        "active_affect": [{"dimension": "sadness", "intensity_bp": 4100}],
        "active_memory_candidates": [
            {"candidate_id": "memory:user-encouraged-publishing"}
        ],
        "aspirations": [{"aspiration_id": "aspiration:publishing"}],
        "commitments": [{"commitment_id": "commitment:user-followup"}],
    }

    def __init__(self, *, ledger: SQLiteWorldLedger) -> None:
        self._ledger = ledger
        self.last_capsule = None

    def compile_for_deliberation(self, _query):  # type: ignore[no-untyped-def]
        projection = self._ledger.project()
        self.last_capsule = SimpleNamespace(
            capsule_id="1" * 64,
            snapshot_hash="2" * 64,
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
            logical_time=projection.logical_time,
            model_content_json=json.dumps(self.context, ensure_ascii=False),
        )
        return SimpleNamespace(capsule=self.last_capsule)


class _CapturingLedger:
    def __init__(self, *, world_id: str, projection, events: dict[str, WorldEvent]) -> None:
        self.world_id = world_id
        self._projection = projection
        self._events = events
        self.committed_events: tuple[WorldEvent, ...] = ()

    def project(self):
        return self._projection

    def lookup_event_commit(self, event_id: str):
        event = self._events.get(event_id)
        ref = next(
            (
                item
                for item in self._projection.committed_world_event_refs
                if item.event_id == event_id
            ),
            None,
        )
        return (
            None
            if event is None or ref is None
            else (
                event,
                SimpleNamespace(
                    event_ids=(event_id,),
                    world_revision=ref.world_revision,
                ),
            )
        )

    def commit_at_cursor(self, events, *, expected_cursor, commit_id):  # type: ignore[no-untyped-def]
        del expected_cursor, commit_id
        self.committed_events = tuple(events)


def _commit_active_long_lived_occurrence(
    *,
    ledger: SQLiteWorldLedger,
    clock: WorldEvent,
) -> tuple[WorldOccurrenceProjection, tuple[str, ...]]:
    occurrence_id = "occurrence:life-aftermath:consequential"
    texts = (
        "聊完后她决定接下这段编辑实习。",
        "聊完后她觉得现实条件不合适，没有接下实习。",
    )
    effect = FrozenLifeArcEffectDescriptor.create(
        arc_kind="employment",
        context_pack_ref="life-context:test-publishing-internship",
        context_tags=("role:intern", "workplace:publishing"),
        duration_days=30,
        privacy_class="personal",
        catalog_version="reviewed-test.1",
        catalog_hash="c" * 64,
    )
    candidate_refs = (
        "candidate:consequential:accept",
        "candidate:consequential:decline",
    )
    candidates = tuple(
        OutcomeCandidateDescriptor(
            candidate_result_ref=candidate_ref,
            result_id=f"result:{candidate_ref}",
            result_payload_ref=f"content:result:{candidate_ref}",
            result_payload_hash=life_content_payload_hash(text),
            privacy_class="personal",
            content_ref=f"content:candidate:{candidate_ref}",
            content_payload_hash=life_content_payload_hash(text),
            life_arc_effect=effect if index == 0 else None,
        )
        for index, (candidate_ref, text) in enumerate(
            zip(candidate_refs, texts, strict=True)
        )
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id=occurrence_id,
        entity_revision=1,
        trigger_ref="plan:consequential",
        participant_refs=("actor:companion",),
        location_ref="location:test",
        time_window=DueWindow(
            opens_at=clock.logical_time - timedelta(minutes=1),
            closes_at=clock.logical_time + timedelta(hours=1),
        ),
        candidate_outcome_refs=candidate_refs,
        candidate_outcomes=candidates,
        visibility="personal",
        status="committed",
    )
    clock_ref = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_id == clock.event_id
    )
    evidence = EvidenceRef(
        ref_id=clock.event_id,
        evidence_type="committed_world_event",
        claim_purpose="life_transition",
        source_world_revision=clock_ref.world_revision,
        immutable_hash=clock_ref.payload_hash,
    )
    _commit_event(
        ledger,
        _event(
            world_id=ledger.world_id,
            event_id="event:consequential:committed",
            event_type="WorldOccurrenceCommitted",
            logical_at=clock.logical_time,
            payload=WorldOccurrenceCommittedPayload(
                change_id="change:consequential:commit",
                transition_id="transition:consequential:1",
                expected_entity_revision=0,
                evidence_refs=(evidence,),
                occurrence=occurrence,
            ).model_dump(mode="json"),
        ),
    )
    _commit_event(
        ledger,
        _event(
            world_id=ledger.world_id,
            event_id="event:consequential:activated",
            event_type="WorldOccurrenceActivated",
            logical_at=clock.logical_time,
            payload=WorldOccurrenceActivatedPayload(
                change_id="change:consequential:activate",
                transition_id="transition:consequential:2",
                expected_entity_revision=1,
                evidence_refs=(evidence,),
                occurrence_id=occurrence_id,
                activated_at=clock.logical_time,
                satisfied_precondition_refs=(),
            ).model_dump(mode="json"),
        ),
    )
    active = next(
        item
        for item in ledger.project().world_occurrences
        if item.occurrence_id == occurrence_id
    )
    return active, texts


def _commit_settled_effect_occurrence(
    *,
    ledger: SQLiteWorldLedger,
    clock: WorldEvent,
    occurrence_id: str,
    context_pack_ref: str,
    context_tags: tuple[str, ...],
    duration_days: int,
    dynamic_life_arc_context: DynamicLifeArcContextDescriptor | None = None,
    provisional_npc_introductions: tuple[
        ProvisionalNpcIntroductionDescriptor, ...
    ] = (),
    include_reviewed_life_arc_effect: bool = True,
    adopt_dynamic_life_direction: bool | None = None,
) -> tuple[WorldEvent, FrozenLifeArcEffectDescriptor | None]:
    logical_at = clock.logical_time
    clock_ref = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_id == clock.event_id
    )
    evidence = EvidenceRef(
        ref_id=clock.event_id,
        evidence_type="committed_world_event",
        claim_purpose="life_transition",
        source_world_revision=clock_ref.world_revision,
        immutable_hash=clock_ref.payload_hash,
    )
    effect = FrozenLifeArcEffectDescriptor.create(
        arc_kind="employment",
        context_pack_ref=context_pack_ref,
        context_tags=context_tags,
        duration_days=duration_days,
        privacy_class="personal",
        catalog_version="reviewed-test.1",
        catalog_hash="c" * 64,
    )
    candidate_ref = f"candidate:{occurrence_id}:accepted"
    result_id = f"result:{occurrence_id}:accepted"
    result_payload_ref = f"content:{occurrence_id}:accepted"
    result_payload_hash = "b" * 64
    occurrence = WorldOccurrenceProjection(
        occurrence_id=occurrence_id,
        entity_revision=1,
        trigger_ref=f"trigger:{occurrence_id}",
        participant_refs=("actor:companion",),
        location_ref="location:test",
        time_window=DueWindow(
            opens_at=logical_at - timedelta(minutes=1),
            closes_at=logical_at + timedelta(hours=1),
        ),
        candidate_outcome_refs=(candidate_ref,),
        candidate_outcomes=(
            OutcomeCandidateDescriptor(
                candidate_result_ref=candidate_ref,
                result_id=result_id,
                result_payload_ref=result_payload_ref,
                result_payload_hash=result_payload_hash,
                privacy_class="personal",
                life_arc_effect=(
                    effect if include_reviewed_life_arc_effect else None
                ),
                dynamic_life_arc_context=dynamic_life_arc_context,
                provisional_npc_introductions=provisional_npc_introductions,
            ),
        ),
        visibility="personal",
        status="committed",
    )
    committed_payload = WorldOccurrenceCommittedPayload(
        change_id=f"change:{occurrence_id}:commit",
        transition_id=f"transition:{occurrence_id}:1",
        expected_entity_revision=0,
        evidence_refs=(evidence,),
        occurrence=occurrence,
    )
    _commit_event(
        ledger,
        _event(
            world_id=ledger.world_id,
            event_id=f"event:{occurrence_id}:committed",
            event_type="WorldOccurrenceCommitted",
            logical_at=logical_at,
            payload=committed_payload.model_dump(mode="json"),
        ),
    )
    activated_payload = WorldOccurrenceActivatedPayload(
        change_id=f"change:{occurrence_id}:activate",
        transition_id=f"transition:{occurrence_id}:2",
        expected_entity_revision=1,
        evidence_refs=(evidence,),
        occurrence_id=occurrence_id,
        activated_at=logical_at,
        satisfied_precondition_refs=(),
    )
    _commit_event(
        ledger,
        _event(
            world_id=ledger.world_id,
            event_id=f"event:{occurrence_id}:activated",
            event_type="WorldOccurrenceActivated",
            logical_at=logical_at,
            payload=activated_payload.model_dump(mode="json"),
        ),
    )
    observation_id = f"observation:{occurrence_id}"
    observed_payload = OutcomeObservationRecordedPayload(
        change_id=f"change:{occurrence_id}:observe",
        transition_id=f"transition:{occurrence_id}:observe",
        expected_entity_revision=2,
        evidence_refs=(evidence,),
        observation=OutcomeObservationProjection(
            observation_id=observation_id,
            occurrence_id=occurrence_id,
            source_kind="committed_world_event",
            source_refs=(clock.event_id,),
            observed_payload_ref=clock.event_id,
            observed_payload_hash=clock.payload_hash,
            observed_at=logical_at,
            confidence_bp=10_000,
        ),
    )
    _commit_event(
        ledger,
        _event(
            world_id=ledger.world_id,
            event_id=f"event:outcome-observation:{observation_id}",
            event_type="OutcomeObservationRecorded",
            logical_at=logical_at,
            payload=observed_payload.model_dump(mode="json"),
        ),
    )
    proposal_id = f"proposal:{occurrence_id}"
    change_id = f"change:{occurrence_id}:settle"
    proposal_head = ledger.project()
    evaluated_world_revision = proposal_head.world_revision
    adopted_direction = (
        dynamic_life_arc_context is not None
        if adopt_dynamic_life_direction is None
        else adopt_dynamic_life_direction
    )
    accepted_hash = outcome_mutation_hash(
        change_id=change_id,
        occurrence_id=occurrence_id,
        evaluated_entity_revision=3,
        evaluated_world_revision=evaluated_world_revision,
        candidate_result_ref=candidate_ref,
        result_id=result_id,
        result_payload_ref=result_payload_ref,
        result_payload_hash=result_payload_hash,
        observation_refs=(observation_id,),
        adopt_proposed_life_direction=adopted_direction,
    )
    proposal_payload = OutcomeProposalRecordedPayload(
        outcome_proposal_id=proposal_id,
        decision_proposal_id=proposal_id,
        change_id=change_id,
        occurrence_id=occurrence_id,
        evaluated_entity_revision=3,
        evaluated_world_revision=evaluated_world_revision,
        trigger_ref=occurrence.trigger_ref,
        candidate_result_ref=candidate_ref,
        proposed_result_id=result_id,
        proposed_result_payload_ref=result_payload_ref,
        proposed_result_payload_hash=result_payload_hash,
        proposed_change_hash=accepted_hash,
        observation_refs=(observation_id,),
        evidence_refs=(evidence,),
        confidence_bp=10_000,
        expires_at=logical_at + timedelta(hours=1),
        decision_authority="character_model",
        decision_model="test-character-model",
        decision_raw_output_hash="1" * 64,
        context_identity_version="life-aftermath-context.1",
        context_capsule_id="2" * 64,
        context_model_content_hash="3" * 64,
        context_snapshot_hash="4" * 64,
        context_cursor=ProjectionCursor(
            world_revision=proposal_head.world_revision,
            deliberation_revision=proposal_head.deliberation_revision,
            ledger_sequence=proposal_head.ledger_sequence,
        ),
        adopt_proposed_life_direction=adopted_direction,
    )
    _commit_event(
        ledger,
        _event(
            world_id=ledger.world_id,
            event_id=f"event:{occurrence_id}:proposal",
            event_type="OutcomeProposalRecorded",
            logical_at=logical_at,
            payload=proposal_payload.model_dump(mode="json"),
        ),
    )
    evaluated_world_revision = ledger.project().world_revision
    acceptance_id = f"acceptance:{occurrence_id}"
    acceptance = _event(
        world_id=ledger.world_id,
        event_id=f"event:{occurrence_id}:acceptance",
        event_type="AcceptanceRecorded",
        logical_at=logical_at,
        payload={
            "status": "accepted",
            "acceptance_id": acceptance_id,
            "proposal_id": proposal_id,
            "evaluated_world_revision": evaluated_world_revision,
            "accepted_change_id": change_id,
            "accepted_change_hash": accepted_hash,
        },
    )
    appraisal_trigger_ref = appraisal_trigger_identity(occurrence_id, result_id)
    settlement_payload = WorldOccurrenceSettledPayload(
        change_id=change_id,
        transition_id=f"transition:{occurrence_id}:3",
        expected_entity_revision=3,
        evidence_refs=(evidence,),
        acceptance_id=acceptance_id,
        evaluated_world_revision=evaluated_world_revision,
        accepted_change_hash=accepted_hash,
        occurrence_id=occurrence_id,
        outcome_proposal_id=proposal_id,
        candidate_result_ref=candidate_ref,
        result_id=result_id,
        observation_refs=(observation_id,),
        result_payload_ref=result_payload_ref,
        result_payload_hash=result_payload_hash,
        settled_at=logical_at,
        appraisal_trigger_ref=appraisal_trigger_ref,
        adopt_proposed_life_direction=adopted_direction,
    )
    settlement = _event(
        world_id=ledger.world_id,
        event_id=f"event:{occurrence_id}:settled",
        event_type="WorldOccurrenceSettled",
        logical_at=logical_at,
        payload=settlement_payload.model_dump(mode="json"),
    )
    trigger = TriggerProcess(
        trigger_id=appraisal_trigger_ref,
        trigger_ref=appraisal_trigger_ref,
        process_kind="npc_world_appraisal",
        source_evidence_ref=settlement.event_id,
        state="open",
    )
    trigger_event = _event(
        world_id=ledger.world_id,
        event_id=f"event:{occurrence_id}:appraisal-trigger",
        event_type="TriggerProcessOpened",
        logical_at=logical_at,
        payload={"process": trigger.model_dump(mode="json")},
    )
    head = ledger.project()
    ledger.commit(
        (acceptance, settlement, trigger_event),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    return settlement, effect


def _commit_event(ledger: SQLiteWorldLedger, event: WorldEvent) -> None:
    head = ledger.project()
    ledger.commit(
        (event,),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )


def _clock_advance(
    *,
    world_id: str,
    event_id: str,
    origin: datetime,
    target: datetime,
) -> WorldEvent:
    return _event(
        world_id=world_id,
        event_id=event_id,
        event_type="ClockAdvanced",
        logical_at=target,
        payload={
            "logical_time_from": origin.isoformat(),
            "logical_time_to": target.isoformat(),
        },
    )


def _settlement_fixture(
    *,
    event_id: str,
    occurrence_id: str,
    settled_at: datetime,
    duration_days: int,
    context_pack_ref: str,
) -> tuple[WorldEvent, WorldOccurrenceProjection]:
    candidate_ref = f"candidate:{occurrence_id}:accepted"
    result_id = f"result:{occurrence_id}:accepted"
    result_payload_ref = f"content:{occurrence_id}:accepted"
    result_payload_hash = "b" * 64
    observation_id = f"observation:{occurrence_id}"
    change_id = f"change:{occurrence_id}:settle"
    effect = FrozenLifeArcEffectDescriptor.create(
        arc_kind="employment",
        context_pack_ref=context_pack_ref,
        context_tags=("role:test",),
        duration_days=duration_days,
        privacy_class="personal",
        catalog_version="reviewed-test.1",
        catalog_hash="c" * 64,
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id=occurrence_id,
        entity_revision=3,
        trigger_ref=f"plan:{occurrence_id}",
        participant_refs=("actor:companion",),
        location_ref="location:test",
        time_window=DueWindow(
            opens_at=settled_at - timedelta(minutes=10),
            closes_at=settled_at,
        ),
        candidate_outcome_refs=(candidate_ref,),
        candidate_outcomes=(
            OutcomeCandidateDescriptor(
                candidate_result_ref=candidate_ref,
                result_id=result_id,
                result_payload_ref=result_payload_ref,
                result_payload_hash=result_payload_hash,
                privacy_class="personal",
                life_arc_effect=effect,
            ),
        ),
        settled_outcome_ref=candidate_ref,
        observation_refs=(observation_id,),
        visibility="personal",
        status="settled",
        activated_at=settled_at - timedelta(minutes=10),
        result_id=result_id,
        result_payload_ref=result_payload_ref,
        result_payload_hash=result_payload_hash,
        settled_at=settled_at,
        settlement_event_ref=event_id,
        settlement_world_revision=1,
        settlement_payload_hash="d" * 64,
    )
    accepted_hash = outcome_mutation_hash(
        change_id=change_id,
        occurrence_id=occurrence_id,
        evaluated_entity_revision=2,
        evaluated_world_revision=0,
        candidate_result_ref=candidate_ref,
        result_id=result_id,
        result_payload_ref=result_payload_ref,
        result_payload_hash=result_payload_hash,
        observation_refs=(observation_id,),
    )
    payload = WorldOccurrenceSettledPayload(
        change_id=change_id,
        transition_id=f"transition:{occurrence_id}:settle",
        expected_entity_revision=2,
        evidence_refs=(
            EvidenceRef(
                ref_id=f"event:evidence:{occurrence_id}",
                evidence_type="committed_world_event",
                claim_purpose="life_transition",
                source_world_revision=1,
                immutable_hash="e" * 64,
            ),
        ),
        acceptance_id=f"acceptance:{occurrence_id}",
        evaluated_world_revision=0,
        accepted_change_hash=accepted_hash,
        occurrence_id=occurrence_id,
        outcome_proposal_id=f"proposal:{occurrence_id}",
        candidate_result_ref=candidate_ref,
        result_id=result_id,
        observation_refs=(observation_id,),
        result_payload_ref=result_payload_ref,
        result_payload_hash=result_payload_hash,
        settled_at=settled_at,
        appraisal_trigger_ref=f"trigger:{occurrence_id}:appraisal",
    )
    return (
        _event(
            world_id="world:biography:backlog",
            event_id=event_id,
            event_type="WorldOccurrenceSettled",
            logical_at=settled_at,
            payload=payload.model_dump(mode="json"),
        ),
        occurrence,
    )


def _committed_ref(event: WorldEvent, *, world_revision: int) -> CommittedWorldEventRef:
    return CommittedWorldEventRef(
        event_id=event.event_id,
        event_type=event.event_type,
        world_revision=world_revision,
        payload_hash=event.payload_hash,
        logical_time=event.logical_time,
    )


def _event(
    *,
    world_id: str,
    event_id: str,
    event_type: str,
    logical_at: datetime,
    payload: dict[str, object],
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id=world_id,
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type=event_type,
        logical_time=logical_at,
        created_at=logical_at,
        actor="worker:test:biography",
        source="test:biography",
        trace_id="trace:biography",
        causation_id="cause:biography",
        correlation_id="correlation:biography",
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )
