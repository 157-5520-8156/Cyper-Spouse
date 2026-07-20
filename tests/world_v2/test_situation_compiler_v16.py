from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import inspect
import json

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.attention_authority_schemas import (
    V2AttentionOrigin,
    V2AttentionProjection,
    V2AttentionValues,
    v2_attention_semantic_fingerprint,
)
from companion_daemon.world_v2.goal_situation_schemas import (
    V2GoalOrigin,
    V2GoalProjection,
    V2GoalValues,
    v2_goal_semantic_fingerprint,
)
from companion_daemon.world_v2.location_authority_schemas import (
    V2LocationOrigin,
    V2LocationProjection,
    V2LocationValues,
    v2_location_semantic_fingerprint,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.resource_authority_schemas import (
    V2ResourceOrigin,
    V2ResourceProjection,
    V2ResourceValues,
    v2_resource_semantic_fingerprint,
)
from companion_daemon.world_v2.resource_authority_reducers import (
    RESOURCE_BAND_POLICY_DIGEST,
    RESOURCE_BAND_POLICY_VERSION,
)
from companion_daemon.world_v2.situation_compiler import (
    AttentionExpiryDueBinding,
    BoundAttentionHead,
    BoundCommitmentHead,
    BoundGoalHead,
    BoundLocationHead,
    BoundPlanHead,
    BoundResourceHead,
    SituationAuthoritySnapshot,
    SituationCompileRequest,
    SituationCompileCache,
    SituationCompiler,
    SourceBinding,
    default_internal_viewer_scope,
    default_situation_policy,
    request_from_ledger_projection,
    viewer_scope,
)
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import (
    CommitmentFulfillmentContract,
    CommitmentOrigin,
    CommitmentProjection,
    CommitmentValues,
    CommittedWorldEventRef,
    DueWindow,
    LedgerProjection,
    PlanStateProjection,
    commitment_semantic_fingerprint,
)
import companion_daemon.world_v2.situation_compiler as situation_module
from companion_daemon.world_v2.ledger import WorldLedger


NOW = datetime(2026, 7, 14, 11, 30, tzinfo=UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source(name: str, revision: int) -> SourceBinding:
    return SourceBinding(
        world_id="world:1",
        world_revision=revision,
        event_ref=f"event:{name}",
        payload_hash=_hash(f"payload:{name}"),
    )


def _goal(goal_id: str, importance: int, *, privacy: str = "private") -> BoundGoalHead:
    values = V2GoalValues(
        outcome_ref=f"outcome:{goal_id}",
        importance_bp=importance,
        progress_bp=1200,
        privacy_class=privacy,
        status="active",
    )
    policy_refs = ("goal-policy.16",)
    head = V2GoalProjection(
        goal_id=goal_id,
        actor_ref="actor:1",
        entity_revision=1,
        semantic_fingerprint=v2_goal_semantic_fingerprint(
            goal_id=goal_id,
            actor_ref="actor:1",
            values=values,
            policy_refs=policy_refs,
        ),
        values=values,
        origin=V2GoalOrigin(
            change_id=f"change:{goal_id}",
            transition_id=f"transition:{goal_id}",
            policy_refs=policy_refs,
            accepted_event_ref=f"event:{goal_id}",
        ),
        opened_at=NOW,
        updated_at=NOW,
    )
    return BoundGoalHead(source=_source(goal_id, 4), head=head)


def _location() -> BoundLocationHead:
    values = V2LocationValues(
        location_ref="location:studio",
        zone_ref="zone:desk",
        scene_visibility="shareable",
        privacy_class="personal",
        since=NOW,
    )
    policy_refs = ("location-policy.16",)
    head = V2LocationProjection(
        actor_ref="actor:1",
        entity_revision=2,
        semantic_fingerprint=v2_location_semantic_fingerprint(
            actor_ref="actor:1", values=values, policy_refs=policy_refs
        ),
        values=values,
        origin=V2LocationOrigin(
            change_id="change:location",
            transition_id="transition:location",
            policy_refs=policy_refs,
            accepted_event_ref="event:location",
        ),
        updated_at=NOW,
    )
    return BoundLocationHead(source=_source("location", 5), head=head)


def _resource(kind: str, value: int, band: str) -> BoundResourceHead:
    values = V2ResourceValues(
        value_bp=value,
        derived_band=band,
        band_policy_version=RESOURCE_BAND_POLICY_VERSION,
        band_policy_digest=RESOURCE_BAND_POLICY_DIGEST,
        privacy_class="private",
    )
    policy_refs = ("resource-policy.16",)
    head = V2ResourceProjection(
        actor_ref="actor:1",
        resource_kind=kind,
        entity_revision=1,
        semantic_fingerprint=v2_resource_semantic_fingerprint(
            actor_ref="actor:1",
            resource_kind=kind,
            values=values,
            policy_refs=policy_refs,
        ),
        values=values,
        origin=V2ResourceOrigin(
            change_id=f"change:{kind}",
            transition_id=f"transition:{kind}",
            policy_refs=policy_refs,
            accepted_event_ref=f"event:{kind}",
        ),
        updated_at=NOW,
    )
    return BoundResourceHead(source=_source(kind, 6), head=head)


def _attention() -> BoundAttentionHead:
    values = V2AttentionValues(
        mode="available",
        allocation_bp=7000,
        interruptibility_bp=8000,
        since=NOW,
        expires_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC),
        privacy_class="private",
    )
    policy_refs = ("attention-policy.16",)
    head = V2AttentionProjection(
        actor_ref="actor:1",
        entity_revision=3,
        semantic_fingerprint=v2_attention_semantic_fingerprint(
            actor_ref="actor:1", values=values, policy_refs=policy_refs
        ),
        values=values,
        origin=V2AttentionOrigin(
            change_id="change:attention",
            transition_id="transition:attention",
            policy_refs=policy_refs,
            accepted_event_ref="event:attention",
        ),
        updated_at=NOW,
    )
    return BoundAttentionHead(source=_source("attention", 7), head=head)


def _plan(plan_id: str, importance: int, participant: str) -> BoundPlanHead:
    head = PlanStateProjection(
        plan_id=plan_id,
        activity_id=f"activity:{plan_id}",
        entity_revision=1,
        activity_kind="focused_work",
        evidence_refs=(
            EvidenceRef(
                ref_id=f"evidence:{plan_id}",
                evidence_type="active_plan",
                claim_purpose="future_plan",
            ),
        ),
        status="active",
        importance_bp=importance,
        participant_refs=(participant,),
        location_ref="location:studio",
        privacy_class="private",
    )
    projection_hash = _hash(
        json.dumps(head.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    )
    return BoundPlanHead(
        source=_source(plan_id, 8),
        actor_ref="actor:1",
        projection_hash=projection_hash,
        head=head,
    )


def _commitment() -> BoundCommitmentHead:
    evidence = EvidenceRef(
        ref_id="event:commitment-anchor",
        evidence_type="committed_world_event",
        claim_purpose="conversation_continuity",
        source_world_revision=1,
        immutable_hash=_hash("commitment-anchor"),
    )
    contract = CommitmentFulfillmentContract(
        contract_kind="thread_resolution",
        evidence_type="committed_world_event",
        expected_event_type="ThreadResolved",
        expected_thread_id="thread:promise",
        contract_version="commitment-fulfillment-contract.1",
    )
    values = CommitmentValues(
        subject_ref="user:1",
        content_ref="commitment:content",
        content_hash=_hash("commitment-content"),
        anchor_evidence_refs=(evidence,),
        source_evidence_refs=(evidence,),
        importance_bp=8500,
        due_window=DueWindow(
            opens_at=datetime(2026, 7, 14, 10, tzinfo=UTC),
            closes_at=datetime(2026, 7, 14, 13, tzinfo=UTC),
        ),
        persistence_level="durable",
        fulfillment_contract=contract,
        privacy_class="private",
        status="open",
    )
    policy_refs = ("commitment-policy.1",)
    head = CommitmentProjection(
        commitment_id="commitment:1",
        entity_revision=1,
        semantic_fingerprint=commitment_semantic_fingerprint(
            owner_ref=values.owner_ref,
            subject_ref=values.subject_ref,
            content_ref=values.content_ref,
            content_hash=values.content_hash,
            anchor_evidence_refs=values.anchor_evidence_refs,
            fulfillment_contract=values.fulfillment_contract,
            policy_refs=policy_refs,
        ),
        values=values,
        origin=CommitmentOrigin(
            change_id="change:commitment:1",
            transition_id="transition:commitment:1",
            policy_refs=policy_refs,
            accepted_event_ref="event:commitment:1",
        ),
        opened_at=NOW,
        updated_at=NOW,
    )
    return BoundCommitmentHead(
        source=SourceBinding(
            world_id="world:1",
            world_revision=4,
            event_ref="event:commitment:1",
            payload_hash=_hash("payload:commitment:1"),
        ),
        actor_ref="actor:companion",
        head=head,
    )


def _request(
    *, goals: tuple[BoundGoalHead, ...] = (), plans: tuple[BoundPlanHead, ...] = ()
) -> SituationCompileRequest:
    location = _location()
    resources = (
        _resource("physical_energy", 3200, "low"),
        _resource("cognitive_capacity", 6000, "moderate"),
    )
    attention = _attention()
    logical_source = SourceBinding(
        world_id="world:1",
        world_revision=3,
        event_ref="event:world-started",
        payload_hash=_hash("payload:world-started"),
    )
    source_types = {
        **{item.source.event_ref: "V2GoalOpened" for item in goals},
        location.source.event_ref: "V2LocationChanged",
        **{item.source.event_ref: "V2ResourceStateAdjusted" for item in resources},
        attention.source.event_ref: "V2AttentionChanged",
        logical_source.event_ref: "WorldStarted",
    }
    sources = (
        *(item.source for item in goals),
        location.source,
        *(item.source for item in resources),
        attention.source,
        logical_source,
    )
    snapshot = SituationAuthoritySnapshot(
        world_id="world:1",
        actor_ref="actor:1",
        pinned_world_revision=9,
        logical_time=NOW,
        logical_time_source=logical_source,
        committed_events=tuple(
            CommittedWorldEventRef(
                event_id=item.event_ref,
                event_type=source_types[item.event_ref],
                world_revision=item.world_revision,
                payload_hash=item.payload_hash,
                logical_time=NOW,
            )
            for item in sources
        ),
        goals=goals,
        location=location,
        resources=resources,
        attention=attention,
        plans=plans,
    )
    return SituationCompileRequest(
        world_id="world:1",
        actor_ref="actor:1",
        pinned_world_revision=9,
        logical_time=NOW,
        authority_snapshot=snapshot,
        policy=default_situation_policy(),
        viewer_scope=default_internal_viewer_scope(),
    )


def test_compile_is_order_invariant_source_bound_and_explicit_about_missing_heads() -> None:
    compiler = SituationCompiler()
    first = compiler.compile(_request(goals=(_goal("goal:b", 8000), _goal("goal:a", 8000))))
    second = compiler.compile(_request(goals=(_goal("goal:a", 8000), _goal("goal:b", 8000))))

    assert first == second
    assert first.internal is not None
    assert [item.goal_id for item in first.internal.goal_slices] == ["goal:a", "goal:b"]
    assert first.internal.location_slice.availability == "available"
    assert first.internal.resource_pressure.value == "high"
    by_kind = {item.resource_kind: item for item in first.internal.resource_slices}
    assert by_kind["social_capacity"].availability == "unavailable"
    assert by_kind["social_capacity"].reason == "no_authority"
    assert first.internal.attention_slice.transition_due is False
    assert first.internal.internal_semantic_hash == second.internal.internal_semantic_hash
    assert tuple(item.identity for item in first.internal.source_revisions) == tuple(
        sorted(item.identity for item in first.internal.source_revisions)
    )


def test_compile_expresses_current_time_and_daypart_in_local_chronology() -> None:
    result = SituationCompiler(
        local_chronology=LocalChronology("Asia/Shanghai")
    ).compile(_request())

    assert result.internal is not None
    assert result.internal.logical_time.isoformat() == "2026-07-14T19:30:00+08:00"
    assert result.internal.time_segment == "evening"


def test_snapshot_rejects_cross_world_future_and_wrong_actor_heads() -> None:
    base = _request().authority_snapshot
    location = _location()
    with pytest.raises(ValidationError, match="another world"):
        SituationAuthoritySnapshot.model_validate(
            base.model_dump(mode="python")
            | {
                "location": location.model_copy(
                    update={
                        "source": location.source.model_copy(update={"world_id": "world:2"})
                    }
                )
            }
        )
    with pytest.raises(ValidationError, match="future revision"):
        SituationAuthoritySnapshot.model_validate(
            base.model_dump(mode="python") | {"pinned_world_revision": 4}
        )
    with pytest.raises(ValidationError, match="another actor"):
        SituationAuthoritySnapshot.model_validate(
            base.model_dump(mode="python") | {"actor_ref": "actor:other"}
        )


def test_attention_due_authority_remains_uninstalled_in_v16() -> None:
    request = _request()
    attention = request.authority_snapshot.attention
    assert attention is not None
    due = AttentionExpiryDueBinding(
        trigger_ref="trigger:attention-expiry",
        trigger_world_revision=9,
        trigger_payload_hash=_hash("attention-expiry-trigger"),
        actor_ref="actor:1",
        attention_entity_revision=attention.head.entity_revision,
        attention_semantic_fingerprint=attention.head.semantic_fingerprint,
        attention_event_ref=attention.source.event_ref,
        clock_event_ref="event:clock",
        clock_entity_revision=2,
        clock_world_revision=8,
        clock_payload_hash=_hash("clock"),
        clock_projection_hash=_hash("clock-projection"),
        policy_digest=_hash("attention-expiry-policy.1"),
    )
    snapshot = request.authority_snapshot.model_copy(update={"attention_expiry_due": (due,)})
    with pytest.raises(ValueError, match="attention_expiry_authority_not_installed"):
        SituationCompiler().compile(
            request.model_copy(update={"authority_snapshot": snapshot})
        )


def test_viewer_projection_redacts_private_domains_without_changing_internal_identity() -> None:
    internal = SituationCompiler().compile(_request(goals=(_goal("goal:private", 9000),)))
    public_request = _request(goals=(_goal("goal:private", 9000),)).model_copy(
        update={
            "viewer_scope": viewer_scope(
                viewer_ref="viewer:room",
                allowed_privacy_classes=("public", "shareable"),
                max_items_per_collection=8,
            )
        }
    )
    public = SituationCompiler().compile(public_request)

    assert internal.internal is not None
    assert public.internal is None
    assert public.viewer_projection.source_internal_semantic_hash == (
        internal.internal.internal_semantic_hash
    )
    assert public.viewer_projection.location_slice.availability == "redacted"
    assert public.viewer_projection.goal_slices[0].availability == "redacted"
    assert public.viewer_projection.resource_slices[0].availability == "redacted"
    assert public.viewer_projection.attention_slice.availability == "redacted"
    assert public.viewer_projection.activity_slices == ()
    assert public.viewer_projection.social_environment.availability == "unavailable"
    assert public.viewer_projection.plan_relation.availability == "unavailable"
    assert public.viewer_projection.viewer_projection_hash != (
        internal.viewer_projection.viewer_projection_hash
    )


def test_tampered_policy_and_request_snapshot_mismatch_fail_closed() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="installed Situation policy"):
        SituationCompileRequest(
            **request.model_dump(exclude={"policy"}),
            policy=request.policy.model_copy(update={"ordering_policy_digest": "f" * 64}),
        )
    with pytest.raises(ValidationError, match="request does not match"):
        SituationCompileRequest(
            **request.model_dump(exclude={"pinned_world_revision"}),
            pinned_world_revision=8,
        )


def test_output_is_canonical_json_byte_stable() -> None:
    result = SituationCompiler().compile(_request(goals=(_goal("goal:a", 7000),)))
    one = json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    two = json.dumps(
        SituationCompiler().compile(_request(goals=(_goal("goal:a", 7000),))).model_dump(
            mode="json"
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert one == two


def test_legacy_plans_fail_closed_until_owner_authority_is_installed() -> None:
    plans = (
        _plan("plan:b", 7000, "npc:b"),
        _plan("plan:a", 7000, "npc:a"),
    )
    with pytest.raises(ValidationError, match="Plan lacks current owner authority"):
        _request(plans=plans)


def test_tampered_cache_is_discarded_and_viewer_is_always_reprojected() -> None:
    cache = SituationCompileCache(signing_key=b"situation-test-cache-key-32-bytes!!")
    compiler = SituationCompiler(cache)
    request = _request(goals=(_goal("goal:cache", 6000),))
    expected = compiler.compile(request)
    assert expected.internal is not None

    internal_key = next(iter(cache._internal_values))
    corrupted = json.loads(cache._internal_values[internal_key])
    corrupted["value"]["actor_ref"] = "actor:tampered"
    cache._internal_values[internal_key] = json.dumps(corrupted, separators=(",", ":"))
    repaired = compiler.compile(request)
    assert repaired == expected
    assert json.loads(cache._internal_values[internal_key])["value"]["actor_ref"] == "actor:1"

    public_request = request.model_copy(
        update={
            "viewer_scope": viewer_scope(
                viewer_ref="viewer:public-cache",
                allowed_privacy_classes=("public",),
                max_items_per_collection=8,
            )
        }
    )
    public = compiler.compile(public_request)
    assert public.internal is None
    assert public.viewer_projection.goal_slices[0].availability == "redacted"

    # A checksum is not authority: even a self-consistent, re-hashed cache
    # value must lose against a pure recompilation of the pinned request.
    envelope = json.loads(cache._internal_values[internal_key])
    envelope["value"]["location_slice"]["location_ref"] = "location:poisoned"
    material = dict(envelope["value"])
    material.pop("internal_semantic_hash")
    envelope["value"]["internal_semantic_hash"] = _json_hash(material)
    cache._internal_values[internal_key] = json.dumps(envelope, separators=(",", ":"))
    assert compiler.compile(request) == expected
    repaired_public = compiler.compile(public_request)
    assert repaired_public.viewer_projection.location_slice.availability == "redacted"
    assert repaired_public.viewer_projection.location_slice.location_ref is None


def test_external_viewer_cannot_self_authorize_private_slices() -> None:
    with pytest.raises(ValidationError, match="private external"):
        viewer_scope(
            viewer_ref="viewer:self-authorized",
            allowed_privacy_classes=("public", "private"),
            max_items_per_collection=8,
        )


def test_authenticated_internal_cache_hit_skips_aggregation_but_not_privacy_projection() -> None:
    class CountingCompiler(SituationCompiler):
        internal_calls = 0
        viewer_calls = 0

        def _compile_internal(self, request: SituationCompileRequest):
            self.internal_calls += 1
            return super()._compile_internal(request)

        def _project_viewer(self, internal, scope, policy):
            self.viewer_calls += 1
            return super()._project_viewer(internal, scope, policy)

    cache = SituationCompileCache(signing_key=b"situation-cache-hit-key-32-bytes!!!")
    compiler = CountingCompiler(cache)
    request = _request(goals=(_goal("goal:hit", 5000),))
    assert compiler.compile(request) == compiler.compile(request)
    assert compiler.internal_calls == 1
    assert compiler.viewer_calls == 2


def test_internal_cache_is_bounded_lru() -> None:
    cache = SituationCompileCache(
        signing_key=b"situation-bounded-cache-key-32bytes", max_entries=2
    )
    compiler = SituationCompiler(cache)
    for suffix in ("a", "b", "c"):
        compiler.compile(_request(goals=(_goal(f"goal:{suffix}", 5000),)))
    assert len(cache._internal_values) == 2


def test_empty_ledger_projection_compiles_to_explicit_unavailable_constituents() -> None:
    projection = LedgerProjection(
        world_id="world:empty",
        world_revision=0,
        deliberation_revision=0,
        ledger_sequence=0,
        semantic_hash="0" * 64,
    )
    request = request_from_ledger_projection(
        projection,
        actor_ref="actor:companion",
        event_resolver=WorldLedger.in_memory(world_id="world:empty"),
    )
    result = SituationCompiler().compile(request)
    assert result.internal is not None
    assert result.internal.logical_time is None
    assert result.internal.time_segment is None
    assert result.internal.location_slice.reason == "no_authority"
    assert result.internal.attention_slice.reason == "no_authority"
    assert all(item.reason == "no_authority" for item in result.internal.resource_slices)


def test_projection_adapter_carries_only_consumed_event_authority_not_full_history() -> None:
    events = tuple(
        CommittedWorldEventRef(
            event_id=f"event:{revision}",
            event_type="WorldStarted" if revision == 1 else "ObservationRecorded",
            world_revision=revision,
            payload_hash=_hash(f"event:{revision}"),
            logical_time=NOW,
        )
        for revision in range(1, 101)
    )
    projection = LedgerProjection(
        world_id="world:history",
        world_revision=100,
        deliberation_revision=0,
        ledger_sequence=100,
        logical_time=NOW,
        committed_world_event_refs=events,
        semantic_hash="0" * 64,
    )
    class IndexedResolver:
        def __init__(self) -> None:
            self.index = {item.event_id: item for item in events}

        def resolve_committed_event_refs(self, event_ids, *, at_world_revision):
            return {item: self.index[item] for item in event_ids}

        def resolve_initial_world_event_ref(self, *, at_world_revision):
            return self.index["event:1"]

    request = request_from_ledger_projection(
        projection,
        actor_ref="actor:companion",
        event_resolver=IndexedResolver(),
    )
    assert tuple(item.event_id for item in request.authority_snapshot.committed_events) == (
        "event:1",
    )


def test_commitment_slice_is_source_bound_sorted_and_redacted_for_external_viewer() -> None:
    commitment = _commitment()
    logical = SourceBinding(
        world_id="world:1",
        world_revision=1,
        event_ref="event:world-started:companion",
        payload_hash=_hash("world-started:companion"),
    )
    events = (
        CommittedWorldEventRef(
            event_id=logical.event_ref,
            event_type="WorldStarted",
            world_revision=logical.world_revision,
            payload_hash=logical.payload_hash,
            logical_time=NOW,
        ),
        CommittedWorldEventRef(
            event_id=commitment.source.event_ref,
            event_type="PrivateCommitmentOpened",
            world_revision=commitment.source.world_revision,
            payload_hash=commitment.source.payload_hash,
            logical_time=NOW,
        ),
    )
    snapshot = SituationAuthoritySnapshot(
        world_id="world:1",
        actor_ref="actor:companion",
        pinned_world_revision=4,
        logical_time=NOW,
        logical_time_source=logical,
        committed_events=events,
        commitments=(commitment,),
    )
    request = SituationCompileRequest(
        world_id="world:1",
        actor_ref="actor:companion",
        pinned_world_revision=4,
        logical_time=NOW,
        authority_snapshot=snapshot,
        policy=default_situation_policy(),
        viewer_scope=default_internal_viewer_scope(),
    )
    internal = SituationCompiler().compile(request)
    assert internal.internal is not None
    assert internal.internal.commitment_slices[0].commitment_id == "commitment:1"
    assert internal.internal.commitment_slices[0].due_relation == "open"
    external = SituationCompiler().compile(
        request.model_copy(
            update={
                "viewer_scope": viewer_scope(
                    viewer_ref="viewer:commitment",
                    allowed_privacy_classes=("public",),
                    max_items_per_collection=8,
                )
            }
        )
    )
    assert external.viewer_projection.commitment_slices[0].availability == "redacted"


def test_viewer_budget_truncates_stable_prefix_without_changing_internal_hash() -> None:
    goals = (
        _goal("goal:a", 9000, privacy="public"),
        _goal("goal:b", 8000, privacy="public"),
    )
    internal = SituationCompiler().compile(_request(goals=goals))
    external = SituationCompiler().compile(
        _request(goals=goals).model_copy(
            update={
                "viewer_scope": viewer_scope(
                    viewer_ref="viewer:budget",
                    allowed_privacy_classes=("public",),
                    max_items_per_collection=1,
                )
            }
        )
    )
    assert internal.internal is not None
    assert external.viewer_projection.source_internal_semantic_hash == (
        internal.internal.internal_semantic_hash
    )
    assert [item.goal_id for item in external.viewer_projection.goal_slices] == ["goal:a"]
    assert external.viewer_projection.truncation_reasons == (
        "goal_slices:budget_truncated",
        "resource_slices:budget_truncated",
    )


def test_compiler_module_has_no_wall_clock_random_model_or_network_dependency() -> None:
    source = inspect.getsource(situation_module)
    assert "datetime.now(" not in source
    assert "import random" not in source
    assert "httpx" not in source
    assert "openai" not in source.lower()
