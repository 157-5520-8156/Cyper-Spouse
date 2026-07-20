from __future__ import annotations

from companion_daemon.world_v2.life_content import LifeContentBudget, LifeContentCompiler
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, LifeContentDescriptorProjection, ProjectionCursor
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
    source = next(item for item in projection.committed_world_event_refs if item.event_id == "occurrence-settled")
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
    return projection.model_copy(
        update={
            "world_occurrences": (occurrence.model_copy(update={"result_payload_hash": content_hash}),),
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
    ), descriptor, text


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

    world_life = WorldLifeContextCompiler(
        life_content=LifeContentCompiler(store=store)
    ).compile(
        projection=projection,
        actor_ref="actor:companion",
        cursor=_cursor(projection),
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
