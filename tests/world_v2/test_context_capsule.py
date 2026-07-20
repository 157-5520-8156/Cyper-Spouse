from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.context_capsule import (
    ContextCapsuleBudgetPolicy,
    ContextCapsuleRequest,
    InnerAdvisoryCandidate,
    InnerAdvisoryProjection,
    MAX_INPUT_ITEMS_PER_SLICE,
    RESOLUTION_POLICY_DIGEST,
    ResolvedItemMetadata,
    ResolvedSlice,
    ResolvedSourceBinding,
    ResolverProof,
    SliceBudget,
    authority_refs_digest,
    canonical_value_hash,
    _compile_resolved_context,
    resolved_result_set_hash,
    source_bindings_hash,
)
from companion_daemon.world_v2.memory_retrieval import (
    MemoryRetrievalItem,
    MemorySourceExcerpt,
)
from companion_daemon.world_v2.recent_dialogue import (
    DialogueSourceClaim,
    RecentDialogueItem,
)
from companion_daemon.world_v2.schemas import (
    AffectEpisodeProjection,
    BudgetAccount,
    CapabilityStateProjection,
    FactProjection,
    MemoryCandidateProjection,
    PrivateImpressionProjection,
    ThreadProjection,
)
from companion_daemon.world_v2.situation_compiler import (
    AttentionSlice,
    LocationSlice,
    PlanRelationSlice,
    PressureSlice,
    SituationProjection,
    SocialEnvironmentSlice,
)


NOW = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64

compile_context_capsule = _compile_resolved_context


def _situation(*, revision: int = 7) -> SituationProjection:
    situation = SituationProjection.model_construct(
        world_id="world:capsule",
        authority_snapshot_hash=HASH_B,
        situation_policy_input_hash=HASH_A,
        compiled_at_world_revision=revision,
        actor_ref="actor:companion",
        logical_time=NOW,
        time_segment="morning",
        location_slice=LocationSlice(availability="unavailable", reason="no_authority"),
        activity_slices=(),
        goal_slices=(),
        resource_slices=(),
        resource_pressure=PressureSlice(availability="unavailable", reason="no_authority"),
        attention_slice=AttentionSlice(availability="unavailable", reason="no_authority"),
        social_environment=SocialEnvironmentSlice(
            availability="unavailable", reason="no_authority"
        ),
        plan_relation=PlanRelationSlice(availability="unavailable", reason="no_authority"),
        commitment_slices=(),
        scene_visibility=None,
        source_revisions=(),
        policy_versions=("situation-policy.1",),
        internal_semantic_hash="0" * 64,
    )
    material = situation.model_dump(mode="json", exclude={"internal_semantic_hash"})
    semantic_hash = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return situation.model_copy(update={"internal_semantic_hash": semantic_hash})


def _item_ref(value) -> str:
    if isinstance(value, SituationProjection):
        return value.actor_ref
    if isinstance(value, BudgetAccount):
        return f"{value.account_id}:{value.window_id}"
    for field in (
        "dialogue_id",
        "advisory_id",
        "candidate_id",
        "episode_id",
        "thread_id",
        "fact_id",
        "grant_id",
        "impression_id",
    ):
        if hasattr(value, field):
            return str(getattr(value, field))
    raise AssertionError(f"test fixture has no Capsule identity: {value!r}")


def _typed_bound(value, *, slice_name: str, source_refs_by_item: tuple[tuple[str, ...], ...]):
    items = value if isinstance(value, tuple) else (value,)
    metadata = []
    for item, refs in zip(items, source_refs_by_item, strict=True):
        bindings = tuple(
            ResolvedSourceBinding(
                source_kind="committed_event",
                authority_type="ObservationRecorded",
                ref=ref,
                source_world_revision=7,
                immutable_hash=hashlib.sha256(ref.encode()).hexdigest(),
            )
            for ref in refs
        )
        metadata.append(ResolvedItemMetadata(
            item_ref=_item_ref(item), rank_score_bp=5_000, privacy_class="private",
            source_bindings=bindings, source_hash=source_bindings_hash(bindings),
            value_hash=canonical_value_hash(item),
        ))
    metadata_tuple = tuple(metadata)
    authority_refs = tuple(sorted({ref for refs in source_refs_by_item for ref in refs}))
    return ResolvedSlice.model_construct(
        world_id="world:capsule", snapshot_id="snapshot:7", snapshot_hash=HASH_A,
        pinned_world_revision=7, value=items,
        resolver_proof=ResolverProof(
            resolver_id="context-capsule-resolver",
            resolver_version="context-capsule-resolver.1",
            policy_digest=RESOLUTION_POLICY_DIGEST,
            world_id="world:capsule", snapshot_id="snapshot:7", snapshot_hash=HASH_A,
            pinned_world_revision=7, slice_name=slice_name, query_ref="query:test",
            window_ref="window:test", policy_version="context-capsule-resolution-policy.1",
            completeness="complete", privacy_floor="private",
            explicit_authority_refs=authority_refs,
            authority_refs_digest=authority_refs_digest(authority_refs),
            result_set_hash=resolved_result_set_hash(slice_name, metadata_tuple),
        ),
        item_metadata=metadata_tuple,
    )


def _bound(
    value,
    *,
    revision: int = 7,
    source_ref: str = "event:source:1",
    ranks: tuple[int, ...] | None = None,
    privacies: tuple[str, ...] | None = None,
    slice_name: str | None = None,
):
    items = value if isinstance(value, tuple) else (value,)
    ranks = ranks or tuple(5000 for _ in items)
    if privacies is None:
        privacies = tuple(
            "withhold"
            if isinstance(item, (BudgetAccount, PrivateImpressionProjection))
            else "private"
            for item in items
        )
    metadata = tuple(
        (
            lambda bindings: ResolvedItemMetadata(
                item_ref=_item_ref(item),
                rank_score_bp=rank,
                privacy_class=privacy,
                source_bindings=bindings,
                source_hash=source_bindings_hash(bindings),
                value_hash=canonical_value_hash(item),
            )
        )(
            (
                ResolvedSourceBinding(
                    source_kind=(
                        "projection_snapshot"
                        if isinstance(item, SituationProjection)
                        else "committed_event"
                    ),
                    authority_type=(
                        "LedgerProjection"
                        if isinstance(item, SituationProjection)
                        else "ObservationRecorded"
                    ),
                    ref=("snapshot:7" if isinstance(item, SituationProjection) else source_ref),
                    source_world_revision=revision,
                    immutable_hash=(
                        item.authority_snapshot_hash
                        if isinstance(item, SituationProjection)
                        else HASH_B
                    ),
                ),
            )
        )
        for item, rank, privacy in zip(items, ranks, privacies, strict=True)
    )
    if slice_name is None:
        if items and isinstance(items[0], SituationProjection):
            slice_name = "current_situation"
        elif items and isinstance(items[0], InnerAdvisoryProjection):
            slice_name = "advisories"
        elif items and isinstance(items[0], PrivateImpressionProjection):
            slice_name = "private_impressions"
        else:
            slice_name = "action_budget"
    authority_refs = tuple(
        sorted(
            {binding.ref for item_metadata in metadata for binding in item_metadata.source_bindings}
        )
    )
    return ResolvedSlice.model_construct(
        world_id="world:capsule",
        snapshot_id="snapshot:7",
        snapshot_hash=HASH_A,
        pinned_world_revision=revision,
        value=value,
        resolver_proof=ResolverProof(
            resolver_id="context-capsule-resolver",
            resolver_version="context-capsule-resolver.1",
            policy_digest=RESOLUTION_POLICY_DIGEST,
            world_id="world:capsule",
            snapshot_id="snapshot:7",
            snapshot_hash=HASH_A,
            pinned_world_revision=revision,
            slice_name=slice_name,
            query_ref="query:test",
            window_ref="window:test",
            policy_version="context-capsule-resolution-policy.1",
            completeness="complete",
            privacy_floor="private",
            explicit_authority_refs=authority_refs,
            authority_refs_digest=authority_refs_digest(authority_refs),
            result_set_hash=resolved_result_set_hash(slice_name, metadata),
        ),
        item_metadata=metadata,
    )


def _request(**updates) -> ContextCapsuleRequest:
    values = {
        "world_id": "world:capsule",
        "snapshot_id": "snapshot:7",
        "snapshot_hash": HASH_A,
        "actor_ref": "actor:companion",
        "consumer_scope": "deliberation_internal",
        "trigger_ref": "event:observation:1",
        "world_revision": 7,
        "deliberation_revision": 3,
        "ledger_sequence": 11,
        "logical_time": NOW,
        "situation": _bound(_situation()),
    }
    values.update(updates)
    # Domain fixtures use model_construct so these tests exercise the Capsule
    # seam, rather than repeating every upstream authority model's own tests.
    return ContextCapsuleRequest.model_construct(**values)


def test_compile_is_stable_source_bound_and_marks_every_missing_domain_unavailable() -> None:
    first = compile_context_capsule(_request())
    second = compile_context_capsule(_request())

    assert first.model_dump_json() == second.model_dump_json()
    assert first.capsule_id == second.capsule_id
    assert first.provenance_kind == "test_only_untrusted"
    assert first.compiler_result_tag is None
    forged = _compile_resolved_context(_request(), _authority=object())
    assert forged.provenance_kind == "test_only_untrusted"
    assert forged.compiler_result_tag is None
    assert first.current_situation.availability == "available"
    assert first.current_situation.source_refs == ("snapshot:7",)
    assert first.current_situation.source_hash is not None
    assert first.current_situation.slice_hash is not None
    for name in (
        "character_core",
        "relationship_slice",
        "affect_episodes",
        "open_threads",
        "relevant_facts",
        "recent_experiences",
        "active_memory_candidates",
        "available_capabilities",
        "action_budget",
        "private_impressions",
        "advisories",
    ):
        missing = getattr(first, name)
        assert missing.availability == "unavailable"
        assert missing.unavailable_reason == "authority_unavailable"
        assert missing.source_refs == ()
        assert missing.source_hash is None


def test_situation_projection_binding_cannot_be_relabeled_as_settled_event() -> None:
    request = _request()
    metadata = request.situation.item_metadata[0]
    binding = metadata.source_bindings[0].model_copy(
        update={
            "source_kind": "committed_event",
            "authority_type": "WorldOccurrenceSettled",
        }
    )
    bindings = (binding,)
    changed_metadata = metadata.model_copy(
        update={
            "source_bindings": bindings,
            "source_hash": source_bindings_hash(bindings),
        }
    )
    proof = request.situation.resolver_proof
    assert proof is not None
    changed_proof = proof.model_copy(
        update={
            "explicit_authority_refs": (binding.ref,),
            "authority_refs_digest": authority_refs_digest((binding.ref,)),
            "result_set_hash": resolved_result_set_hash("current_situation", (changed_metadata,)),
        }
    )
    changed_situation = request.situation.model_copy(
        update={"item_metadata": (changed_metadata,), "resolver_proof": changed_proof}
    )

    with pytest.raises(ValueError, match="authority snapshot"):
        compile_context_capsule(request.model_copy(update={"situation": changed_situation}))


def test_compile_rejects_any_slice_not_pinned_to_capsule_revision() -> None:
    stale_budget = BudgetAccount(
        account_id="budget:chat",
        category="chat",
        window_id="window:1",
        limit=100,
    )
    with pytest.raises(ValueError, match="pinned world revision"):
        compile_context_capsule(_request(action_budget=_bound((stale_budget,), revision=6)))

    with pytest.raises(ValueError, match="Situation revision"):
        compile_context_capsule(_request(situation=_bound(_situation(revision=6), revision=7)))


def test_required_situation_is_never_reduced_to_a_partial_typed_claim() -> None:
    tiny = SliceBudget(max_items=1, max_fields=2, max_characters=55)
    policy = ContextCapsuleBudgetPolicy(
        hard_max_characters=500,
        current_situation=tiny,
    )

    with pytest.raises(ValueError, match="minimum whole-item budget"):
        compile_context_capsule(_request(), policy=policy)


def test_proactive_model_view_compacts_proof_but_keeps_whole_situation_authority() -> None:
    capsule = _compile_resolved_context(
        _request(), model_content_profile="proactive_decision"
    )

    full_value = json.loads(capsule.current_situation.items[0].payload_json)
    model_slice = json.loads(capsule.current_situation.model_content_json)
    model_item = model_slice["items"][0]
    assert "source_revisions" in full_value
    assert "internal_semantic_hash" in full_value
    assert "source_revisions" not in model_item["value"]
    assert "internal_semantic_hash" not in model_item["value"]
    assert "source_bindings" not in model_item
    assert "activity_slices" in model_item["value"]
    assert capsule.current_situation.items[0].value_hash == canonical_value_hash(
        _situation()
    )


def test_optional_typed_item_is_wholly_omitted_when_its_envelope_does_not_fit() -> None:
    account = BudgetAccount(
        account_id="budget:chat", category="chat", window_id="window:1", limit=100
    )
    policy = ContextCapsuleBudgetPolicy(
        action_budget=SliceBudget(max_items=1, max_fields=20, max_characters=900)
    )

    capsule = compile_context_capsule(_request(action_budget=_bound((account,))), policy=policy)

    assert capsule.action_budget.items == ()
    assert "account_id" not in capsule.action_budget.model_content_json
    assert any(
        entry.slice_name == "action_budget"
        and entry.reason == "character_budget"
        and entry.omitted_count == 1
        for entry in capsule.budget.truncation_log
    )


def test_collection_contracts_accept_only_active_memory_and_affect_episodes() -> None:
    inactive_memory = MemoryCandidateProjection.model_construct(
        candidate_id="memory:1",
        values=type("Values", (), {"status": "forgotten"})(),
    )
    inactive_affect = AffectEpisodeProjection.model_construct(
        episode_id="affect:1", status="resolved"
    )

    with pytest.raises(ValueError):
        compile_context_capsule(_request(active_memory_candidates=_bound((inactive_memory,))))
    with pytest.raises(ValueError):
        compile_context_capsule(_request(affect_episodes=_bound((inactive_affect,))))


def test_global_hard_cap_is_enforced_across_available_slices() -> None:
    account = BudgetAccount(
        account_id="budget:chat",
        category="chat",
        window_id="window:1",
        limit=100,
    )
    policy = ContextCapsuleBudgetPolicy(
        hard_max_characters=4_700,
        action_budget=SliceBudget(max_items=8, max_fields=80, max_characters=3_000),
    )

    capsule = compile_context_capsule(_request(action_budget=_bound((account,))), policy=policy)

    assert capsule.budget.used_characters <= 4_700
    assert capsule.budget.used_characters == len(capsule.model_content_json)
    assert capsule.budget.used_characters == (
        capsule.budget.slice_content_characters + capsule.budget.framing_characters
    )
    assert all(
        slice_.budget.used_characters <= slice_.budget.max_characters
        for slice_ in (
            capsule.current_situation,
            capsule.action_budget,
        )
    )
    assert capsule.action_budget.items == ()
    assert any(
        entry.slice_name == "action_budget"
        and entry.reason == "global_character_budget"
        and entry.omitted_count == 1
        for entry in capsule.budget.truncation_log
    )


def test_global_budget_retains_the_single_requested_advisory_matrix() -> None:
    account = BudgetAccount(
        account_id="budget:chat",
        category="chat",
        window_id="window:1",
        limit=100,
    )
    advisory = InnerAdvisoryProjection(
        advisory_id="advisory:proactive:1",
        kind="proactive_opportunity",
        source_refs=("event:observation:1",),
        candidate_refs=("spontaneous_contact:observation:1",),
        candidates=(
            InnerAdvisoryCandidate(
                candidate_ref="spontaneous_contact:observation:1",
                value="The latest inbound message left a live conversational opening.",
                weight_bp=10_000,
                confidence_bp=10_000,
            ),
        ),
        confidence_bp=10_000,
        expiry=NOW + timedelta(minutes=1),
        producer_version="test-proactive-matrix.1",
    )
    policy = ContextCapsuleBudgetPolicy(hard_max_characters=7_000)

    capsule = compile_context_capsule(
        _request(
            action_budget=_bound((account,)),
            advisories=_bound((advisory,), source_ref="event:observation:1"),
        ),
        policy=policy,
    )

    assert capsule.budget.used_characters <= policy.hard_max_characters
    assert [item.item_ref for item in capsule.advisories.items] == [advisory.advisory_id]
    assert capsule.action_budget.items == ()


def test_advisory_pressure_retains_the_exact_proactive_source_binding() -> None:
    trigger_ref = "event:observation:proactive-trigger"
    proactive = InnerAdvisoryProjection(
        advisory_id="advisory:zz-proactive",
        kind="proactive_opportunity",
        source_refs=(trigger_ref,),
        candidate_refs=("spontaneous_contact:observation:trigger",),
        candidates=(
            InnerAdvisoryCandidate(
                candidate_ref="spontaneous_contact:observation:trigger",
                value="Verified latest inbound message before the idle gap.",
                weight_bp=10_000,
                confidence_bp=10_000,
            ),
        ),
        confidence_bp=100,
        expiry=NOW + timedelta(minutes=1),
        producer_version="test-proactive-matrix.1",
    )
    optional = tuple(
        InnerAdvisoryProjection(
            advisory_id=f"advisory:{index:02d}-optional",
            kind="appraisal_candidate",
            source_refs=(trigger_ref,),
            candidate_refs=(f"candidate:optional:{index}",),
            confidence_bp=10_000,
            expiry=NOW + timedelta(minutes=1),
            producer_version="test-optional-matrix.1",
        )
        for index in range(8)
    )
    policy = ContextCapsuleBudgetPolicy(
        advisories=SliceBudget(max_items=3, max_fields=96, max_characters=8_000)
    )

    capsule = compile_context_capsule(
        _request(
            advisories=_bound(
                (*optional, proactive),
                source_ref=trigger_ref,
                ranks=(*((10_000,) * len(optional)), 100),
            )
        ),
        policy=policy,
    )

    retained = next(
        item for item in capsule.advisories.items if item.item_ref == proactive.advisory_id
    )
    assert [binding.ref for binding in retained.source_bindings] == [trigger_ref]


def test_verified_dialogue_proof_does_not_evict_two_active_memory_excerpts() -> None:
    dialogue: list[RecentDialogueItem] = []
    dialogue_refs: list[tuple[str, ...]] = []
    for index in range(8):
        refs = tuple(f"event:dialogue:{index}:{claim}" for claim in range(4))
        dialogue_refs.append(refs)
        dialogue.append(RecentDialogueItem(
            dialogue_id=f"dialogue:{index}",
            speaker="counterpart" if index % 2 == 0 else "companion",
            text=f"第 {index + 1} 条已经核验送达的近期对话。",
            occurred_at=NOW,
            delivery_state="observed" if index % 2 == 0 else "delivered",
            sequence=index + 1,
            source_claims=tuple(
                DialogueSourceClaim(
                    authority_event_ref=ref,
                    authority_world_revision=7,
                    authority_payload_hash=hashlib.sha256(ref.encode()).hexdigest(),
                )
                for ref in refs
            ),
        ))
    memories: list[MemoryRetrievalItem] = []
    memory_refs: list[tuple[str, ...]] = []
    for index, text in enumerate(("我叫丁奥轩。", "我最喜欢喝乌龙茶。")):
        ref = f"event:memory-source:{index}"
        digest = hashlib.sha256(ref.encode()).hexdigest()
        memory_refs.append((ref,))
        memories.append(MemoryRetrievalItem(
            candidate_id=f"memory:{index}", cue_kind="future_utility",
            retention_rationales=("future_utility",), privacy_ceiling="personal",
            retrieval_strength_bp=5_000,
            source_excerpts=(MemorySourceExcerpt(
                source_kind="fact", source_id=f"fact:{index}", source_entity_revision=1,
                authority_event_ref=ref, authority_world_revision=7,
                authority_payload_hash=digest, source_values_hash=HASH_A,
                excerpt_ref=f"observation:{index}",
                excerpt_payload_hash=hashlib.sha256(text.encode()).hexdigest(),
                text=text, truncated=False,
            ),),
            truncated=False,
        ))

    capsule = compile_context_capsule(
        _request(
            recent_dialogue=_typed_bound(
                tuple(dialogue), slice_name="recent_dialogue",
                source_refs_by_item=tuple(dialogue_refs),
            ),
            active_memory_candidates=_typed_bound(
                tuple(memories), slice_name="active_memory_candidates",
                source_refs_by_item=tuple(memory_refs),
            ),
        ),
        policy=ContextCapsuleBudgetPolicy(hard_max_characters=15_000),
    )

    model_context = json.loads(capsule.model_content_json)
    assert len(capsule.recent_dialogue.items) == 8
    assert len(capsule.active_memory_candidates.items) == 2
    assert "source_claims" not in capsule.recent_dialogue.model_content_json
    assert "sidecar_hash" not in capsule.recent_dialogue.model_content_json
    memory_text = capsule.active_memory_candidates.model_content_json
    assert "丁奥轩" in memory_text and "乌龙茶" in memory_text
    assert model_context["slices"]["recent_dialogue"]["source_ref_count"] == 32


def test_collection_order_does_not_change_capsule_and_items_are_identity_sorted() -> None:
    first = BudgetAccount(account_id="budget:a", category="chat", window_id="window:1", limit=100)
    second = BudgetAccount(
        account_id="budget:b", category="repair", window_id="window:1", limit=100
    )

    forward = compile_context_capsule(_request(action_budget=_bound((first, second))))
    reverse = compile_context_capsule(_request(action_budget=_bound((second, first))))

    assert forward.model_dump_json() == reverse.model_dump_json()
    assert tuple(item.item_ref for item in forward.action_budget.items) == ("budget:a:window:1",)


def test_empty_collection_and_zero_item_budget_remain_available_but_audited() -> None:
    empty = compile_context_capsule(_request(action_budget=_bound(())))
    zero_policy = ContextCapsuleBudgetPolicy(
        action_budget=SliceBudget(max_items=0, max_fields=10, max_characters=900)
    )
    account = BudgetAccount(
        account_id="budget:chat",
        category="chat",
        window_id="window:1",
        limit=100,
    )
    zero = compile_context_capsule(_request(action_budget=_bound((account,))), policy=zero_policy)

    assert empty.action_budget.availability == "available"
    assert empty.action_budget.items == ()
    assert empty.action_budget.truncated is False
    assert zero.action_budget.availability == "available"
    assert zero.action_budget.items == ()
    assert zero.action_budget.truncated is True
    assert any(
        entry.slice_name == "action_budget"
        and entry.reason == "item_budget"
        and entry.omitted_count == 1
        for entry in zero.budget.truncation_log
    )


def test_advisory_must_still_be_valid_at_capsule_logical_time() -> None:
    expired = InnerAdvisoryProjection(
        advisory_id="advisory:1",
        kind="appraisal_candidate",
        source_refs=("event:observation:1",),
        candidate_refs=("candidate:notice-disappointment",),
        confidence_bp=7000,
        expiry=NOW,
        producer_version="advisory-test.1",
    )

    with pytest.raises(ValueError, match="expired advisory"):
        compile_context_capsule(
            _request(advisories=_bound((expired,), source_ref="event:observation:1"))
        )


def test_tampered_value_authority_is_rejected() -> None:
    bound = _bound(_situation())
    tampered = bound.model_copy(
        update={
            "item_metadata": (bound.item_metadata[0].model_copy(update={"value_hash": HASH_B}),),
        }
    )
    tampered = tampered.model_copy(
        update={
            "resolver_proof": tampered.resolver_proof.model_copy(
                update={
                    "result_set_hash": resolved_result_set_hash(
                        "current_situation", tampered.item_metadata
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="value hash"):
        compile_context_capsule(_request(situation=tampered))


def test_same_source_ref_with_different_immutable_payload_hash_changes_identity() -> None:
    account = BudgetAccount(
        account_id="budget:chat", category="chat", window_id="window:1", limit=100
    )
    original = _bound((account,))
    original_meta = original.item_metadata[0]
    changed_bindings = (
        original_meta.source_bindings[0].model_copy(update={"immutable_hash": HASH_A}),
    )
    changed = original.model_copy(
        update={
            "item_metadata": (
                original_meta.model_copy(
                    update={
                        "source_bindings": changed_bindings,
                        "source_hash": source_bindings_hash(changed_bindings),
                    }
                ),
            )
        }
    )
    changed = changed.model_copy(
        update={
            "resolver_proof": changed.resolver_proof.model_copy(
                update={
                    "result_set_hash": resolved_result_set_hash(
                        "action_budget", changed.item_metadata
                    )
                }
            )
        }
    )

    first = compile_context_capsule(_request(action_budget=original))
    second = compile_context_capsule(_request(action_budget=changed))

    assert first.action_budget.source_refs == second.action_budget.source_refs
    assert first.action_budget.source_hash != second.action_budget.source_hash
    assert first.capsule_id != second.capsule_id

    wrong_kind_bindings = (
        original_meta.source_bindings[0].model_copy(update={"source_kind": "projection_snapshot"}),
    )
    wrong_kind_meta = original_meta.model_copy(
        update={
            "source_bindings": wrong_kind_bindings,
            "source_hash": source_bindings_hash(wrong_kind_bindings),
        }
    )
    wrong_kind = original.model_copy(
        update={
            "item_metadata": (wrong_kind_meta,),
            "resolver_proof": original.resolver_proof.model_copy(
                update={
                    "result_set_hash": resolved_result_set_hash("action_budget", (wrong_kind_meta,))
                }
            ),
        }
    )
    with pytest.raises(ValueError, match="reserved for Situation"):
        compile_context_capsule(_request(action_budget=wrong_kind))


def test_full_projection_snapshot_and_situation_authority_hash_are_distinct() -> None:
    capsule = compile_context_capsule(_request())

    assert capsule.snapshot_hash == HASH_A
    assert _situation().authority_snapshot_hash == HASH_B
    assert capsule.snapshot_hash != _situation().authority_snapshot_hash


def test_cross_world_snapshot_and_actor_are_rejected() -> None:
    cross_world = _bound(_situation()).model_copy(update={"world_id": "world:other"})
    with pytest.raises(ValueError, match="world and snapshot identity"):
        compile_context_capsule(_request(situation=cross_world))
    with pytest.raises(ValueError, match="different actor"):
        compile_context_capsule(_request(actor_ref="actor:other"))
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        compile_context_capsule(_request(snapshot_hash="g" * 64))
    impression = PrivateImpressionProjection(
        impression_id="impression:private",
        subject_ref="user:1",
        interpretation_refs=("interpretation:1",),
        source_refs=("event:observation:1",),
        confidence_bp=7000,
        first_seen=NOW,
        last_supported=NOW,
        expiry_condition="until contradicted",
        status="active",
    )
    with pytest.raises(ValueError, match="consumer_scope"):
        compile_context_capsule(
            _request(
                consumer_scope="external",
                private_impressions=_bound((impression,)),
            )
        )


def test_constructed_invalid_typed_budget_is_revalidated_at_compile_entry() -> None:
    invalid = BudgetAccount.model_construct(
        account_id="budget:invalid",
        category="chat",
        window_id="window:1",
        limit=-1,
        reserved=0,
        spent=0,
        overrun=0,
    )

    with pytest.raises(ValueError, match="greater than or equal to 0"):
        compile_context_capsule(_request(action_budget=_bound((invalid,))))

    invalid_policy = ContextCapsuleBudgetPolicy.model_construct(hard_max_characters=-1)
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        compile_context_capsule(_request(), policy=invalid_policy)


def test_resolver_result_set_and_typed_authority_refs_are_exact() -> None:
    budget = BudgetAccount(
        account_id="budget:chat", category="chat", window_id="window:1", limit=100
    )
    bound = _bound((budget,))
    bad_result = bound.model_copy(
        update={
            "resolver_proof": bound.resolver_proof.model_copy(update={"result_set_hash": HASH_B})
        }
    )
    with pytest.raises(ValueError, match="result set hash"):
        compile_context_capsule(_request(action_budget=bad_result))

    impression = PrivateImpressionProjection(
        impression_id="impression:1",
        subject_ref="user:1",
        interpretation_refs=("interpretation:1",),
        source_refs=("event:observation:actual",),
        confidence_bp=7000,
        first_seen=NOW,
        last_supported=NOW,
        expiry_condition="until contradicted",
        status="active",
    )
    unrelated = _bound(
        (impression,),
        source_ref="event:observation:unrelated",
        slice_name="private_impressions",
    )
    with pytest.raises(ValueError, match="typed authority refs"):
        compile_context_capsule(_request(private_impressions=unrelated))


def test_authoritative_empty_collection_requires_complete_resolver_proof() -> None:
    empty_bound = _bound(())
    empty = compile_context_capsule(_request(action_budget=empty_bound))

    assert empty.action_budget.availability == "available"
    assert empty.action_budget.items == ()
    assert empty.action_budget.source_refs == ()
    assert empty.action_budget.resolver_proof is not None
    assert empty.action_budget.resolver_proof.completeness == "complete"

    incomplete = empty_bound.model_copy(
        update={
            "resolver_proof": empty_bound.resolver_proof.model_copy(
                update={"completeness": "incomplete"}
            )
        }
    )
    with pytest.raises(ValueError, match="completeness"):
        compile_context_capsule(_request(action_budget=incomplete))


def test_rank_metadata_drives_selection_and_privacy_cannot_be_self_downgraded() -> None:
    low = BudgetAccount(account_id="budget:low", category="chat", window_id="window:1", limit=10)
    relevant = BudgetAccount(
        account_id="budget:relevant", category="repair", window_id="window:1", limit=10
    )
    highest = BudgetAccount(
        account_id="budget:highest", category="audit", window_id="window:1", limit=10
    )
    bound = _bound(
        (low, relevant, highest),
        ranks=(100, 9000, 10_000),
        privacies=("withhold", "withhold", "withhold"),
    )
    policy = ContextCapsuleBudgetPolicy(
        action_budget=SliceBudget(max_items=1, max_fields=20, max_characters=2_000)
    )
    capsule = compile_context_capsule(
        _request(action_budget=bound),
        policy=policy,
    )

    assert tuple(item.item_ref for item in capsule.action_budget.items) == (
        "budget:highest:window:1",
    )
    assert capsule.action_budget.items[0].rank_score_bp == 10_000
    assert capsule.action_budget.items[0].privacy_class == "withhold"

    downgraded = _bound((highest,), privacies=("public",))
    with pytest.raises(ValueError, match="privacy downgrades"):
        compile_context_capsule(_request(action_budget=downgraded))

    public_situation = _bound(_situation(), privacies=("public",))
    public_situation = public_situation.model_copy(
        update={
            "resolver_proof": public_situation.resolver_proof.model_copy(
                update={
                    "privacy_floor": "public",
                    "result_set_hash": resolved_result_set_hash(
                        "current_situation", public_situation.item_metadata
                    ),
                }
            )
        }
    )
    with pytest.raises(ValueError, match="privacy downgrades"):
        compile_context_capsule(_request(situation=public_situation))


def test_input_cardinality_is_bounded_before_truncation_and_logs_are_aggregated() -> None:
    account = BudgetAccount(
        account_id="budget:base", category="chat", window_id="window:1", limit=10
    )
    oversized = ResolvedSlice.model_construct(
        world_id="world:capsule",
        snapshot_id="snapshot:7",
        snapshot_hash=HASH_A,
        pinned_world_revision=7,
        value=tuple(account for _ in range(20_000)),
        resolver_proof=ResolverProof(
            resolver_id="context-capsule-resolver",
            resolver_version="context-capsule-resolver.1",
            policy_digest=RESOLUTION_POLICY_DIGEST,
            world_id="world:capsule",
            snapshot_id="snapshot:7",
            snapshot_hash=HASH_A,
            pinned_world_revision=7,
            slice_name="action_budget",
            query_ref="query:oversized",
            window_ref="window:test",
            policy_version="context-capsule-resolution-policy.1",
            completeness="complete",
            privacy_floor="withhold",
            explicit_authority_refs=(),
            authority_refs_digest=authority_refs_digest(()),
            result_set_hash=resolved_result_set_hash("action_budget", ()),
        ),
        item_metadata=(),
    )
    with pytest.raises(ValueError, match="input item limit"):
        compile_context_capsule(_request(action_budget=oversized))

    accounts = tuple(
        account.model_copy(update={"account_id": f"budget:{index}"})
        for index in range(MAX_INPUT_ITEMS_PER_SLICE)
    )
    bounded = compile_context_capsule(
        _request(action_budget=_bound(accounts)),
        policy=ContextCapsuleBudgetPolicy(
            action_budget=SliceBudget(max_items=1, max_fields=20, max_characters=2_000)
        ),
    )
    item_logs = [
        entry
        for entry in bounded.budget.truncation_log
        if entry.slice_name == "action_budget" and entry.reason == "item_budget"
    ]
    assert len(item_logs) == 1
    assert item_logs[0].omitted_count == MAX_INPUT_ITEMS_PER_SLICE - 1


def test_malformed_metadata_identity_sets_fail_as_value_errors() -> None:
    first = BudgetAccount(account_id="budget:a", category="chat", window_id="window:1", limit=10)
    second = BudgetAccount(account_id="budget:b", category="chat", window_id="window:1", limit=10)
    valid = _bound((first, second))
    duplicated_metadata = (valid.item_metadata[0], valid.item_metadata[0])
    duplicate = valid.model_copy(
        update={
            "item_metadata": duplicated_metadata,
            "resolver_proof": valid.resolver_proof.model_copy(
                update={
                    "result_set_hash": resolved_result_set_hash(
                        "action_budget", duplicated_metadata
                    )
                }
            ),
        }
    )
    with pytest.raises(ValueError, match="duplicate metadata identities"):
        compile_context_capsule(_request(action_budget=duplicate))

    missing = valid.model_copy(update={"item_metadata": (valid.item_metadata[0],)})
    with pytest.raises(ValueError, match="metadata does not cover"):
        compile_context_capsule(_request(action_budget=missing))


@pytest.mark.parametrize(
    ("field", "projection", "message"),
    [
        (
            "open_threads",
            ThreadProjection.model_construct(
                thread_id="thread:1", values=SimpleNamespace(status="resolved")
            ),
            "open threads",
        ),
        (
            "relevant_facts",
            FactProjection.model_construct(
                fact_id="fact:1", values=SimpleNamespace(status="withdrawn")
            ),
            "active facts",
        ),
        (
            "available_capabilities",
            CapabilityStateProjection.model_construct(
                grant_id="grant:1", values=SimpleNamespace(state="revoked")
            ),
            "active capabilities",
        ),
        (
            "private_impressions",
            PrivateImpressionProjection.model_construct(
                impression_id="impression:1", status="expired"
            ),
            "active private impressions",
        ),
    ],
)
def test_terminal_or_inactive_projection_cannot_enter_active_capsule_slice(
    field: str, projection, message: str
) -> None:
    with pytest.raises(ValueError):
        compile_context_capsule(_request(**{field: _bound((projection,))}))
