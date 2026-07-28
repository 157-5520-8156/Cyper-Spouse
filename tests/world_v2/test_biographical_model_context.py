from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from companion_daemon.world_v2.biographical_lifecycle import (
    BiographicalLifecycleCatalog,
)
from companion_daemon.world_v2.biographical_timeline_authority import (
    BiographicalTimelineConfiguredPayload,
)
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger_context_resolver import (
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.life_content import LifeContentCompiler
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
)
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    DueWindow,
    DynamicLifeArcContextDescriptor,
    LedgerProjection,
    LifeArcProjection,
    OutcomeCandidateDescriptor,
    ProjectionCursor,
    WorldEvent,
    WorldOccurrenceProjection,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.world_life_context import (
    BiographicalWorldContextItem,
    WorldLifeContextCompiler,
    WorldLifeSourceBinding,
)


def test_pinned_chat_context_tracks_biography_arc_and_graduation(
    tmp_path: Path,
) -> None:
    world_id = "world:biographical-chat-context"
    path = tmp_path / "biographical-chat.sqlite"
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    summer = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    started = _event(
        world_id=world_id,
        event_id="event:world-started:biographical-chat",
        event_type="WorldStarted",
        logical_at=summer - timedelta(days=1),
        payload={},
    )
    clock = _event(
        world_id=world_id,
        event_id="event:clock:biographical-chat:summer",
        event_type="ClockAdvanced",
        logical_at=summer,
        payload={
            "logical_time_from": (summer - timedelta(days=1)).isoformat(),
            "logical_time_to": summer.isoformat(),
        },
    )
    timeline = BiographicalTimelineConfiguredPayload.from_yaml(
        path=Path("configs/world_seed.yaml"),
        timezone_name="Asia/Shanghai",
    )
    assert timeline is not None
    timeline_event = _event(
        world_id=world_id,
        event_id="event:biographical-timeline:configured",
        event_type="BiographicalTimelineConfigured",
        logical_at=summer - timedelta(days=1),
        payload=timeline.model_dump(mode="json"),
    )
    ledger.commit(
        (started, timeline_event, clock),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    catalog = BiographicalLifecycleCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        timezone_name="Asia/Shanghai",
    )
    compiler = context_capsule_compiler_from_ledger(
        ledger=ledger,
        biographical_catalog=catalog,
        biographical_timezone_name="Asia/Shanghai",
        biographical_timeline=timeline,
    )

    summer_projection = ledger.project()
    summer_capsule = compiler.compile(
        query_from_projection(
            summer_projection,
            actor_ref="agent:companion",
            trigger_ref=clock.event_id,
        )
    )
    summer_context = json.loads(
        compact_chat_model_facing_context(summer_capsule.model_content_json)
    )
    summer_biography = _biography_value(summer_context)

    assert summer_biography["age"] == 21
    assert summer_biography["academic_phase"] == "summer_break"
    assert summer_biography["season"] == "summer"
    assert summer_biography["current_residence_context_tags"] == [
        "residence:family_home_jiaxing"
    ]
    assert summer_biography["active_life_arcs"] == []
    assert summer_context["current_self_state"]["biographical_context"][0][
        "age"
    ] == 21
    full_item = next(
        item
        for item in summer_capsule.world_life.items
        if json.loads(item.payload_json).get("context_kind")
        == "biographical_context"
    )
    assert {binding.authority_type for binding in full_item.source_bindings} == {
        "BiographicalTimelineConfigured",
        "ClockAdvanced",
    }
    assert full_item.rank_score_bp == 10_000
    assert summer_biography["timeline_source_event_ref"] == timeline_event.event_id

    proactive = compiler.compile_for_deliberation_with_advisories(
        query_from_projection(
            summer_projection,
            actor_ref="agent:companion",
            trigger_ref=clock.event_id,
        ),
        (),
        model_content_profile="proactive_decision",
    ).capsule
    proactive_context = json.loads(proactive.model_content_json)
    assert _biography_value(proactive_context)["age"] == 21

    graduated_at = datetime(2028, 7, 28, 4, 0, tzinfo=UTC)
    graduated_clock = _event(
        world_id=world_id,
        event_id="event:clock:biographical-chat:graduated",
        event_type="ClockAdvanced",
        logical_at=graduated_at,
        payload={
            "logical_time_from": summer.isoformat(),
            "logical_time_to": graduated_at.isoformat(),
        },
    )
    head = ledger.project()
    ledger.commit(
        (graduated_clock,),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    graduated_projection = ledger.project()
    graduated_capsule = compiler.compile(
        query_from_projection(
            graduated_projection,
            actor_ref="agent:companion",
            trigger_ref=graduated_clock.event_id,
        )
    )
    graduated_context = json.loads(
        compact_chat_model_facing_context(graduated_capsule.model_content_json)
    )
    graduated_biography = _biography_value(graduated_context)

    assert graduated_biography["age"] == 23
    assert graduated_biography["academic_phase"] == "graduated"
    assert graduated_biography["academic_year"] is None
    assert graduated_biography["current_residence_context_tags"] == [
        "residence:shanghai_home"
    ]
    assert graduated_biography["active_life_arcs"] == []


def test_biographical_context_binds_active_arc_to_acceptance_and_settlement() -> None:
    logical_at = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
    timeline_ref = CommittedWorldEventRef(
        event_id="event:timeline",
        event_type="BiographicalTimelineConfigured",
        world_revision=1,
        payload_hash="a" * 64,
        logical_time=logical_at - timedelta(days=1),
    )
    clock_ref = CommittedWorldEventRef(
        event_id="event:clock",
        event_type="ClockAdvanced",
        world_revision=2,
        payload_hash="b" * 64,
        logical_time=logical_at,
    )
    settlement_ref = CommittedWorldEventRef(
        event_id="event:occurrence:settled",
        event_type="WorldOccurrenceSettled",
        world_revision=3,
        payload_hash="c" * 64,
        logical_time=logical_at,
    )
    accepted_ref = CommittedWorldEventRef(
        event_id="event:life-arc:publishing-internship:start",
        event_type="LifeArcChanged",
        world_revision=4,
        payload_hash="e" * 64,
        logical_time=logical_at,
    )
    projection = LedgerProjection(
        world_id="world:biography:arc-context",
        world_revision=4,
        deliberation_revision=0,
        ledger_sequence=4,
        logical_time=logical_at,
        committed_world_event_refs=(
            timeline_ref,
            clock_ref,
            settlement_ref,
            accepted_ref,
        ),
        life_arcs=(
            LifeArcProjection(
                arc_id="life-arc:publishing-internship",
                entity_revision=1,
                owner_actor_ref="agent:companion",
                arc_kind="employment",
                context_pack_ref="life-context:publishing-internship",
                context_tags=("role:intern", "workplace:publishing"),
                started_at=logical_at,
                ends_at=logical_at + timedelta(days=30),
                source_event_ref=settlement_ref.event_id,
                accepted_event_ref=accepted_ref.event_id,
                privacy_class="personal",
            ),
        ),
        semantic_hash="d" * 64,
    )
    catalog = BiographicalLifecycleCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        timezone_name="Asia/Shanghai",
    )

    item = WorldLifeContextCompiler(
        biography=catalog,
        biography_timezone=ZoneInfo("Asia/Shanghai"),
    ).compile(
        projection=projection,
        actor_ref="agent:companion",
        biographical_timeline_source=WorldLifeSourceBinding(
            authority_event_ref=timeline_ref.event_id,
            authority_world_revision=timeline_ref.world_revision,
            authority_payload_hash=timeline_ref.payload_hash,
        ),
    )[0]

    assert isinstance(item, BiographicalWorldContextItem)
    assert item.context_kind == "biographical_context"
    assert item.active_life_arcs[0].context_pack_ref == (
        "life-context:publishing-internship"
    )
    assert {
        binding.authority_event_ref for binding in item.source_bindings
    } == {
        timeline_ref.event_id,
        clock_ref.event_id,
        settlement_ref.event_id,
        accepted_ref.event_id,
    }
    assert (
        item.active_life_arcs[0].accepted_event_ref
        == accepted_ref.event_id
    )

    impossible_acceptance = accepted_ref.model_copy(
        update={"world_revision": settlement_ref.world_revision}
    )
    impossible_projection = projection.model_copy(
        update={
            "committed_world_event_refs": (
                timeline_ref,
                clock_ref,
                settlement_ref,
                impossible_acceptance,
            )
        }
    )
    assert (
        WorldLifeContextCompiler(
            biography=catalog,
            biography_timezone=ZoneInfo("Asia/Shanghai"),
        ).compile(
            projection=impossible_projection,
            actor_ref="agent:companion",
            biographical_timeline_source=WorldLifeSourceBinding(
                authority_event_ref=timeline_ref.event_id,
                authority_world_revision=timeline_ref.world_revision,
                authority_payload_hash=timeline_ref.payload_hash,
            ),
        )
        == ()
    )


def test_dynamic_life_arc_context_exposes_only_hash_bound_summary() -> None:
    logical_at = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
    timeline_ref = CommittedWorldEventRef(
        event_id="event:timeline:dynamic",
        event_type="BiographicalTimelineConfigured",
        world_revision=1,
        payload_hash="a" * 64,
        logical_time=logical_at - timedelta(days=1),
    )
    clock_ref = CommittedWorldEventRef(
        event_id="event:clock:dynamic",
        event_type="ClockAdvanced",
        world_revision=2,
        payload_hash="b" * 64,
        logical_time=logical_at,
    )
    settlement_ref = CommittedWorldEventRef(
        event_id="event:settlement:dynamic",
        event_type="WorldOccurrenceSettled",
        world_revision=3,
        payload_hash="c" * 64,
        logical_time=logical_at,
    )
    arc_event_ref = CommittedWorldEventRef(
        event_id="event:life-arc:dynamic:start",
        event_type="LifeArcChanged",
        world_revision=4,
        payload_hash="d" * 64,
        logical_time=logical_at,
    )
    summary = "她接下来几周会偶尔去旧书店帮忙，具体怎么安排仍由她自己决定。"
    summary_ref = "content:dynamic-arc:bookshop"
    descriptor = DynamicLifeArcContextDescriptor.create(
        summary_content_ref=summary_ref,
        summary_payload_hash=life_content_payload_hash(summary),
        narrative_tags=("narrative:bookshop", "narrative:volunteering"),
        duration_days=21,
        privacy_class="personal",
    )
    candidate = OutcomeCandidateDescriptor(
        candidate_result_ref="candidate:dynamic",
        result_id="result:dynamic",
        result_payload_ref="content:result:dynamic",
        result_payload_hash="e" * 64,
        privacy_class="personal",
        dynamic_life_arc_context=descriptor,
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:dynamic",
        entity_revision=4,
        trigger_ref="plan:dynamic",
        participant_refs=("agent:companion",),
        time_window=DueWindow(
            opens_at=logical_at - timedelta(minutes=5),
            closes_at=logical_at + timedelta(minutes=5),
        ),
        candidate_outcome_refs=(candidate.candidate_result_ref,),
        candidate_outcomes=(candidate,),
        settled_outcome_ref=candidate.candidate_result_ref,
        visibility="personal",
        status="settled",
        activated_at=logical_at - timedelta(minutes=1),
        result_id=candidate.result_id,
        result_payload_ref=candidate.result_payload_ref,
        result_payload_hash=candidate.result_payload_hash,
        settled_at=logical_at,
        settlement_event_ref=settlement_ref.event_id,
        settlement_world_revision=settlement_ref.world_revision,
        settlement_payload_hash=settlement_ref.payload_hash,
    )
    projection = LedgerProjection(
        world_id="world:dynamic-context",
        world_revision=4,
        deliberation_revision=0,
        ledger_sequence=4,
        logical_time=logical_at,
        committed_world_event_refs=(
            timeline_ref,
            clock_ref,
            settlement_ref,
            arc_event_ref,
        ),
        world_occurrences=(occurrence,),
        life_arcs=(
            LifeArcProjection(
                arc_id="open-life-arc:dynamic",
                entity_revision=1,
                owner_actor_ref="agent:companion",
                arc_kind="dynamic",
                context_pack_ref=summary_ref,
                context_tags=descriptor.narrative_tags,
                effect_descriptor_hash=descriptor.descriptor_hash,
                status="active",
                started_at=logical_at,
                ends_at=logical_at + timedelta(days=21),
                source_event_ref=settlement_ref.event_id,
                accepted_event_ref=arc_event_ref.event_id,
                privacy_class="personal",
            ),
        ),
        semantic_hash="f" * 64,
    )
    store = InMemoryImmutableLifeContentStore()
    store.put_if_absent(
        StoredLifeContent(
            content_ref=summary_ref,
            content_kind="dynamic_life_arc_context",
            content_payload_hash=descriptor.summary_payload_hash,
            text=summary,
        )
    )
    catalog = BiographicalLifecycleCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        timezone_name="Asia/Shanghai",
    )

    context = WorldLifeContextCompiler(
        biography=catalog,
        biography_timezone=ZoneInfo("Asia/Shanghai"),
        life_content=LifeContentCompiler(store=store),
    ).compile(
        projection=projection,
        actor_ref="agent:companion",
        cursor=ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        ),
        biographical_timeline_source=WorldLifeSourceBinding(
            authority_event_ref=timeline_ref.event_id,
            authority_world_revision=timeline_ref.world_revision,
            authority_payload_hash=timeline_ref.payload_hash,
        ),
    )[0]

    assert isinstance(context, BiographicalWorldContextItem)
    active = context.active_life_arcs[0]
    assert active.context_summary == summary
    assert active.context_summary_ref == summary_ref
    assert active.context_summary_payload_hash == descriptor.summary_payload_hash


def _biography_value(context: dict[str, object]) -> dict[str, object]:
    slices = context["slices"]
    assert isinstance(slices, dict)
    world_life = slices["world_life"]
    assert isinstance(world_life, dict)
    items = world_life["items"]
    assert isinstance(items, list)
    return next(
        item["value"]
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("value"), dict)
        and item["value"].get("context_kind") == "biographical_context"
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
        actor="worker:test:biographical-context",
        source="test:biographical-context",
        trace_id="trace:biographical-context",
        causation_id="cause:biographical-context",
        correlation_id="correlation:biographical-context",
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )
