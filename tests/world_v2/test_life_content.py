from __future__ import annotations

import pytest

from companion_daemon.world_v2.life_content import LifeContentBudget, LifeContentCompiler
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    ExperienceOccurrenceSettlementBinding,
    ExperienceOrigin,
    ExperienceProjection,
    ExperienceValues,
    LifeContentDescriptorProjection,
    ProjectionCursor,
    experience_semantic_fingerprint,
)
from companion_daemon.world_v2.world_life_context import WorldLifeContextCompiler
from companion_daemon.world_v2.context_capsule import _typed_source_authorities, _typed_source_refs
from companion_daemon.world_v2.ledger_context_resolver import (
    _typed_authority_claims as _resolver_typed_authorities,
    _typed_refs as _resolver_typed_refs,
)
from test_life_projection import WORLD_ID, commit, seed_through_proposal, settlement_batch


def _projection_with_bound_content(*, text: str = "阿林把茶端到了窗边。"):
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    projection = ledger.project()
    occurrence = projection.world_occurrences[0]
    source = next(
        item
        for item in projection.committed_world_event_refs
        if item.event_id == "occurrence-settled"
    )
    content_hash = life_content_payload_hash(text)
    descriptor_hash = "e" * 64
    descriptor_ref = "life-content-recorded:tea"
    descriptor = LifeContentDescriptorProjection(
        content_id="content:tea-result",
        content_kind="occurrence_result",
        content_ref="payload:tea-good",
        content_payload_hash=content_hash,
        privacy_class="private",
        source_kind="occurrence_settlement",
        source_event_ref=source.event_id,
        source_world_revision=source.world_revision,
        source_payload_hash=source.payload_hash,
        source_entity_id=occurrence.occurrence_id,
        source_entity_revision=occurrence.entity_revision,
        descriptor_event_ref=descriptor_ref,
        descriptor_world_revision=projection.world_revision,
        descriptor_payload_hash=descriptor_hash,
    )
    return (
        projection.model_copy(
            update={
                "world_occurrences": (
                    occurrence.model_copy(update={"result_payload_hash": content_hash}),
                ),
                "committed_world_event_refs": (
                    *projection.committed_world_event_refs,
                    CommittedWorldEventRef(
                        event_id=descriptor_ref,
                        event_type="LifeContentRecorded",
                        world_revision=projection.world_revision,
                        payload_hash=descriptor_hash,
                        logical_time=projection.logical_time,
                    ),
                ),
                "life_content_descriptors": (descriptor,),
            }
        ),
        descriptor,
        text,
    )


def _projection_with_bound_experience_content(
    *,
    text: str = "下午在旧书店躲了会儿雨，顺手翻到一本很喜欢的摄影集。",
):
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    projection = ledger.project()
    occurrence = projection.world_occurrences[0]
    settlement = next(
        item
        for item in projection.committed_world_event_refs
        if item.event_id == "occurrence-settled"
    )
    summary_hash = life_content_payload_hash(text)
    values = ExperienceValues(
        summary_ref="content:experience:bookshop-rain",
        summary_payload_hash=summary_hash,
        occurred_from=occurrence.settled_at,
        occurred_to=occurrence.settled_at,
        participant_refs=("actor:companion",),
        source_bindings=(
            ExperienceOccurrenceSettlementBinding(
                authority_event_ref=settlement.event_id,
                authority_world_revision=settlement.world_revision,
                authority_payload_hash=settlement.payload_hash,
                occurrence_id=occurrence.occurrence_id,
                occurrence_entity_revision=occurrence.entity_revision,
                result_id=occurrence.result_id,
                result_payload_ref=occurrence.result_payload_ref,
                result_payload_hash=occurrence.result_payload_hash,
            ),
        ),
        privacy_class="personal",
    )
    origin = ExperienceOrigin(
        change_id="change:experience:bookshop-rain",
        transition_id="transition:experience:bookshop-rain",
        policy_refs=("policy:experience-v1",),
        accepted_event_ref="event:experience:bookshop-rain",
    )
    experience = ExperienceProjection(
        experience_id="experience:bookshop-rain",
        semantic_fingerprint=experience_semantic_fingerprint(
            values=values,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
    )
    experience_event = CommittedWorldEventRef(
        event_id=origin.accepted_event_ref,
        event_type="ExperienceCommitted",
        world_revision=projection.world_revision,
        payload_hash="a" * 64,
        logical_time=projection.logical_time,
    )
    descriptor_event = CommittedWorldEventRef(
        event_id="event:life-content:experience:bookshop-rain",
        event_type="LifeContentRecorded",
        world_revision=projection.world_revision,
        payload_hash="b" * 64,
        logical_time=projection.logical_time,
    )
    descriptor = LifeContentDescriptorProjection(
        content_id="life-content:experience:bookshop-rain",
        content_kind="experience_summary",
        content_ref=values.summary_ref,
        content_payload_hash=values.summary_payload_hash,
        privacy_class=values.privacy_class,
        source_kind="experience",
        source_event_ref=experience_event.event_id,
        source_world_revision=experience_event.world_revision,
        source_payload_hash=experience_event.payload_hash,
        source_entity_id=experience.experience_id,
        source_entity_revision=experience.entity_revision,
        descriptor_event_ref=descriptor_event.event_id,
        descriptor_world_revision=descriptor_event.world_revision,
        descriptor_payload_hash=descriptor_event.payload_hash,
    )
    return (
        projection.model_copy(
            update={
                "experiences": (experience,),
                "committed_world_event_refs": (
                    *projection.committed_world_event_refs,
                    experience_event,
                    descriptor_event,
                ),
                "life_content_descriptors": (descriptor,),
            }
        ),
        descriptor,
        text,
    )


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def test_life_content_compiler_emits_only_descriptor_bound_sidecar_text() -> None:
    projection, descriptor, text = _projection_with_bound_content()
    store = InMemoryImmutableLifeContentStore()
    store.put_if_absent(
        StoredLifeContent(
            content_ref=descriptor.content_ref,
            content_kind="occurrence_result",
            content_payload_hash=descriptor.content_payload_hash,
            text=text,
        )
    )

    result = LifeContentCompiler(store=store).compile(
        cursor=_cursor(projection),
        actor_ref="actor:companion",
        viewer_privacy_ceiling="private",
        projection=projection,
    )

    assert result.suppressions == ()
    assert result.settled_items[0].text == text
    assert result.settled_items[0].authority_event_ref == "occurrence-settled"
    assert result.settled_items[0].descriptor_event_ref == descriptor.descriptor_event_ref

    companion_projection = projection.model_copy(
        update={
            "world_occurrences": (
                projection.world_occurrences[0].model_copy(
                    update={"participant_refs": ("actor:companion",)}
                ),
            )
        }
    )
    world_life = WorldLifeContextCompiler(life_content=LifeContentCompiler(store=store)).compile(
        projection=companion_projection,
        actor_ref="actor:companion",
        cursor=_cursor(companion_projection),
    )
    assert world_life[0].content is not None
    assert world_life[0].content.text == text
    assert _typed_source_refs("world_life", world_life[0]) == (
        "life-content-recorded:tea",
        "occurrence-settled",
    )
    assert len(_typed_source_authorities(world_life[0])) == 2
    assert _resolver_typed_refs(world_life[0], observation_aliases={}) == (
        "life-content-recorded:tea",
        "occurrence-settled",
    )
    assert len(_resolver_typed_authorities(world_life[0], observation_aliases={})) == 2


def test_life_content_compiler_fails_closed_when_bytes_or_privacy_do_not_match() -> None:
    projection, descriptor, _ = _projection_with_bound_content()
    store = InMemoryImmutableLifeContentStore()
    result = LifeContentCompiler(store=store).compile(
        cursor=_cursor(projection),
        actor_ref="actor:companion",
        viewer_privacy_ceiling="personal",
        projection=projection,
    )
    assert result.settled_items == ()
    assert result.suppressions[0].reason == "privacy_ceiling"

    visible = LifeContentCompiler(store=store).compile(
        cursor=_cursor(projection),
        actor_ref="actor:companion",
        viewer_privacy_ceiling="private",
        budget=LifeContentBudget(max_item_characters=4, max_total_characters=4),
        projection=projection,
    )
    assert visible.settled_items == ()
    assert visible.suppressions[0].reason == "content_missing"


def test_experience_content_keeps_projection_identity_and_exact_two_event_authority() -> None:
    projection, descriptor, text = _projection_with_bound_experience_content()
    store = InMemoryImmutableLifeContentStore()
    store.put_if_absent(
        StoredLifeContent(
            content_ref=descriptor.content_ref,
            content_kind=descriptor.content_kind,
            content_payload_hash=descriptor.content_payload_hash,
            text=text,
        )
    )

    result = LifeContentCompiler(store=store).compile(
        cursor=_cursor(projection),
        actor_ref="actor:companion",
        viewer_privacy_ceiling="private",
        projection=projection,
    )

    assert result.suppressions == ()
    assert len(result.experience_items) == 1
    item = result.experience_items[0]
    assert item.experience_id == "experience:bookshop-rain"
    assert item.content.text == text
    assert item.content.authority_event_ref == "event:experience:bookshop-rain"
    assert item.content.descriptor_event_ref == descriptor.descriptor_event_ref
    assert _typed_source_refs("recent_experiences", item) == (
        "event:experience:bookshop-rain",
        "event:life-content:experience:bookshop-rain",
    )
    assert {(source_kind, ref) for source_kind, ref, _, _ in _typed_source_authorities(item)} == {
        ("committed_event", "event:experience:bookshop-rain"),
        ("committed_event", "event:life-content:experience:bookshop-rain"),
    }
    assert _resolver_typed_refs(item, observation_aliases={}) == (
        "event:experience:bookshop-rain",
        "event:life-content:experience:bookshop-rain",
    )
    assert len(_resolver_typed_authorities(item, observation_aliases={})) == 2


def test_experience_without_the_companion_participant_is_not_autobiographical_context() -> None:
    projection, descriptor, text = _projection_with_bound_experience_content()
    original = projection.experiences[0]
    npc_only_values = original.values.model_copy(
        update={"participant_refs": ("npc:lin",)}
    )
    npc_only = original.model_copy(
        update={
            "values": npc_only_values,
            "semantic_fingerprint": experience_semantic_fingerprint(
                values=npc_only_values,
                policy_refs=original.origin.policy_refs,
            ),
        }
    )
    projection = projection.model_copy(update={"experiences": (npc_only,)})
    store = InMemoryImmutableLifeContentStore()
    store.put_if_absent(
        StoredLifeContent(
            content_ref=descriptor.content_ref,
            content_kind=descriptor.content_kind,
            content_payload_hash=descriptor.content_payload_hash,
            text=text,
        )
    )

    result = LifeContentCompiler(store=store).compile(
        cursor=_cursor(projection),
        actor_ref="actor:companion",
        viewer_privacy_ceiling="private",
        projection=projection,
    )

    assert result.experience_items == ()
    assert result.suppressions[0].reason == "not_related"


def test_experience_content_may_strengthen_but_not_weaken_source_privacy() -> None:
    projection, descriptor, text = _projection_with_bound_experience_content()
    descriptor = descriptor.model_copy(update={"privacy_class": "private"})
    projection = projection.model_copy(
        update={"life_content_descriptors": (descriptor,)}
    )
    store = InMemoryImmutableLifeContentStore()
    store.put_if_absent(
        StoredLifeContent(
            content_ref=descriptor.content_ref,
            content_kind=descriptor.content_kind,
            content_payload_hash=descriptor.content_payload_hash,
            text=text,
        )
    )

    result = LifeContentCompiler(store=store).compile(
        cursor=_cursor(projection),
        actor_ref="actor:companion",
        viewer_privacy_ceiling="private",
        projection=projection,
    )

    assert result.suppressions == ()
    assert result.experience_items[0].values.privacy_class == "personal"
    assert result.experience_items[0].content.privacy_class == "private"


@pytest.mark.parametrize(
    ("failure", "reason"),
    (
        ("missing", "content_missing"),
        ("hash_mismatch", "hash_mismatch"),
        ("wrong_source_event_type", "source_proof_failed"),
        ("wrong_descriptor_event_type", "source_proof_failed"),
        ("privacy_widening", "source_proof_failed"),
    ),
)
def test_experience_content_fails_closed_without_exact_authority_and_sidecar_bytes(
    failure: str,
    reason: str,
) -> None:
    projection, descriptor, text = _projection_with_bound_experience_content()
    if failure == "privacy_widening":
        descriptor = descriptor.model_copy(update={"privacy_class": "shareable"})
        projection = projection.model_copy(
            update={"life_content_descriptors": (descriptor,)}
        )
    if failure in {"wrong_source_event_type", "wrong_descriptor_event_type"}:
        tampered_ref = (
            descriptor.source_event_ref
            if failure == "wrong_source_event_type"
            else descriptor.descriptor_event_ref
        )
        projection = projection.model_copy(
            update={
                "committed_world_event_refs": tuple(
                    item.model_copy(update={"event_type": "ObservationRecorded"})
                    if item.event_id == tampered_ref
                    else item
                    for item in projection.committed_world_event_refs
                )
            }
        )
    store = InMemoryImmutableLifeContentStore()
    if failure != "missing":
        stored_text = text if failure != "hash_mismatch" else text + "（另一份正文）"
        store.put_if_absent(
            StoredLifeContent(
                content_ref=descriptor.content_ref,
                content_kind=descriptor.content_kind,
                content_payload_hash=life_content_payload_hash(stored_text),
                text=stored_text,
            )
        )

    result = LifeContentCompiler(store=store).compile(
        cursor=_cursor(projection),
        actor_ref="actor:companion",
        viewer_privacy_ceiling="private",
        projection=projection,
    )

    assert result.experience_items == ()
    assert result.suppressions[0].reason == reason


def test_experience_without_a_descriptor_is_explicitly_unavailable() -> None:
    projection, _, _ = _projection_with_bound_experience_content()
    projection = projection.model_copy(update={"life_content_descriptors": ()})

    result = LifeContentCompiler(store=InMemoryImmutableLifeContentStore()).compile(
        cursor=_cursor(projection),
        actor_ref="actor:companion",
        viewer_privacy_ceiling="private",
        projection=projection,
    )

    assert result.experience_items == ()
    assert result.suppressions[0].source_entity_id == "experience:bookshop-rain"
    assert result.suppressions[0].content_id is None
    assert result.suppressions[0].reason == "descriptor_missing"
