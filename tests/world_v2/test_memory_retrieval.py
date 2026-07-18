from __future__ import annotations

from datetime import UTC, datetime
import hashlib

import pytest

from companion_daemon.world_v2.ledger import HistoricalLedgerEvent
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.memory_retrieval import MemoryRetrievalCompiler
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    EvidenceRef,
    FactAssertionBinding,
    LifeContentDescriptorProjection,
    MessageObservationRef,
    Observation,
    ProjectionCursor,
    WorldEvent,
    fact_semantic_fingerprint,
)

import test_memory_candidate_authority as authority


NOW = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)


class _MemoryReadLedger:
    def __init__(self, *, projection, event: WorldEvent, cursor: ProjectionCursor) -> None:  # type: ignore[no-untyped-def]
        self.world_id = authority.WORLD
        self._projection = projection
        self._event = event
        self._cursor = cursor

    def project_at(self, cursor: ProjectionCursor):  # type: ignore[no-untyped-def]
        assert cursor == self._cursor
        return self._projection

    def observation_events_at(self, _locators, *, cursor: ProjectionCursor):  # type: ignore[no-untyped-def]
        assert cursor == self._cursor
        return (
            HistoricalLedgerEvent(
                event=self._event,
                event_cursor=self._cursor,
                event_envelope_hash=self._event.payload_hash,
            ),
        )


def _message_fact_read_ledger() -> tuple[_MemoryReadLedger, object, ProjectionCursor]:
    ledger, _ = authority.initialized_ledger_with_fact()
    base = ledger.project()
    cursor = ProjectionCursor(
        world_revision=base.world_revision,
        deliberation_revision=base.deliberation_revision,
        ledger_sequence=base.ledger_sequence,
    )
    text = "我最近开始很喜欢喝乌龙茶。"
    payload_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:memory-source",
        world_id=authority.WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:memory-source",
        causation_id="cause:memory-source",
        correlation_id="correlation:memory-source",
        source="platform:test",
        source_event_id="test:memory-source",
        actor="user:primary",
        channel="test",
        payload_ref="payload:memory-source",
        payload_hash=payload_hash,
        text=text,
        received_at=NOW,
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:memory-source",
        world_id=authority.WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key="observation:memory-source",
        payload=observation.model_dump(mode="json"),
    )
    evidence = EvidenceRef(
        ref_id=observation.observation_id,
        evidence_type="observed_message",
        claim_purpose="current_fact",
        source_world_revision=1,
        immutable_hash=event.payload_hash,
    )
    original = base.facts[0]
    values = original.values.model_copy(
        update={
            "assertion_binding": FactAssertionBinding(
                source_kind="observed_message",
                source_ref=observation.observation_id,
                asserted_subject_ref=original.values.subject_ref,
                actor_ref=observation.actor,
                channel=observation.channel,
                payload_ref=observation.payload_ref,
                content_payload_hash=observation.payload_hash,
            ),
            "anchor_evidence_refs": (evidence,),
            "source_evidence_refs": (evidence,),
        }
    )
    fact = original.model_copy(
        update={
            "values": values,
            "semantic_fingerprint": fact_semantic_fingerprint(
                subject_ref=values.subject_ref,
                predicate_code=values.predicate_code,
                cardinality=values.cardinality,
                conflict_key=values.conflict_key,
                value_hash=values.value_hash,
                assertion_binding=values.assertion_binding,
                anchor_evidence_refs=values.anchor_evidence_refs,
                policy_refs=original.origin.policy_refs,
            ),
        }
    )
    transition = base.fact_transitions[0].model_copy(
        update={"values_after": values, "semantic_fingerprint_after": fact.semantic_fingerprint}
    )
    committed = base.committed_world_event_refs[-1].model_copy(
        update={
            "event_id": fact.origin.accepted_event_ref,
            "event_type": "FactCommitted",
            "world_revision": 1,
            "payload_hash": "1" * 64,
        }
    )
    binding = authority.binding(fact, transition, committed)
    candidate = authority.candidate(
        binding,
        status="active",
        reviewed_at=NOW,
        opened_at=NOW,
        updated_at=NOW,
        accepted_event_ref="event:memory:active",
    )
    projection = base.model_copy(
        update={
            "facts": (fact,),
            "fact_transitions": (transition,),
            "memory_candidates": (candidate,),
            "message_observations": (
                MessageObservationRef(
                    observation_id=observation.observation_id,
                    source=observation.source,
                    source_event_id=observation.source_event_id,
                    content_payload_hash=observation.payload_hash,
                    event_payload_hash=event.payload_hash,
                    world_revision=1,
                    actor=observation.actor,
                    channel=observation.channel,
                    payload_ref=observation.payload_ref,
                ),
            ),
            "committed_world_event_refs": (committed,),
        }
    )
    return _MemoryReadLedger(projection=projection, event=event, cursor=cursor), candidate, cursor


def test_fact_backed_memory_retrieval_uses_only_the_exact_persisted_assertion_text() -> None:
    ledger, candidate, cursor = _message_fact_read_ledger()

    result = MemoryRetrievalCompiler(ledger=ledger).compile(
        cursor=cursor,
        candidates=(candidate,),
        viewer_privacy_ceiling="private",
    )

    assert result.suppressions == ()
    item = result.items[0]
    assert item.source_excerpts[0].text == "我最近开始很喜欢喝乌龙茶。"
    assert item.source_excerpts[0].excerpt_ref == "observation:memory-source"
    assert item.source_excerpts[0].authority_event_ref == candidate.values.source_bindings[0].authority_event_ref


def test_memory_retrieval_does_not_turn_an_operator_fact_ref_into_model_content() -> None:
    ledger, binding = authority.initialized_ledger_with_fact()
    candidate = authority.candidate(binding, status="active", reviewed_at=authority.NOW)
    cursor = ProjectionCursor(
        world_revision=ledger.project().world_revision,
        deliberation_revision=ledger.project().deliberation_revision,
        ledger_sequence=ledger.project().ledger_sequence,
    )

    result = MemoryRetrievalCompiler(ledger=ledger).compile(
        cursor=cursor,
        candidates=(candidate,),
        viewer_privacy_ceiling="private",
    )

    assert result.items == ()
    assert result.suppressions[0].reasons == ("content_unavailable",)


def _experience_memory_read_fixture() -> tuple[object, object, ProjectionCursor, object, WorldEvent]:
    experience, transition, committed, source = authority.hardened_experience_authority()
    text = "今天下班路上遇到一场很短的雨，反而觉得很轻松。"
    summary_hash = life_content_payload_hash(text)
    values = experience.values.model_copy(update={"summary_payload_hash": summary_hash})
    semantic_fingerprint = authority.experience_semantic_fingerprint(
        values=values, policy_refs=experience.origin.policy_refs
    )
    experience = experience.model_copy(
        update={"values": values, "semantic_fingerprint": semantic_fingerprint}
    )
    transition = transition.model_copy(
        update={
            "values_after": values,
            "semantic_fingerprint_after": semantic_fingerprint,
        }
    )
    source = source.model_copy(
        update={"source_values_hash": authority.canonical_hash(values)}
    )
    candidate = authority.candidate(
        source,
        candidate_id="memory:experience",
        status="active",
        reviewed_at=NOW,
        opened_at=NOW,
        updated_at=NOW,
    )
    descriptor_payload_hash = "9" * 64
    descriptor = LifeContentDescriptorProjection(
        content_id="life-content:experience:1",
        content_kind="experience_summary",
        content_ref=experience.values.summary_ref,
        content_payload_hash=summary_hash,
        privacy_class=experience.values.privacy_class,
        source_kind="experience",
        source_event_ref=committed.event_id,
        source_world_revision=committed.world_revision,
        source_payload_hash=committed.payload_hash,
        source_entity_id=experience.experience_id,
        source_entity_revision=experience.entity_revision,
        descriptor_event_ref="event:life-content:experience:1",
        descriptor_world_revision=2,
        descriptor_payload_hash=descriptor_payload_hash,
    )
    base_ledger, _ = authority.initialized_ledger_with_fact()
    base = base_ledger.project()
    cursor = ProjectionCursor(
        world_revision=2,
        deliberation_revision=base.deliberation_revision,
        ledger_sequence=base.ledger_sequence,
    )
    projection = base.model_copy(
        update={
            "world_revision": cursor.world_revision,
            "experiences": (experience,),
            "experience_transitions": (transition,),
            "memory_candidates": (candidate,),
            "life_content_descriptors": (descriptor,),
            "committed_world_event_refs": (
                committed,
                CommittedWorldEventRef(
                    event_id=descriptor.descriptor_event_ref,
                    event_type="LifeContentRecorded",
                    world_revision=descriptor.descriptor_world_revision,
                    payload_hash=descriptor.descriptor_payload_hash,
                    logical_time=NOW,
                ),
            ),
        }
    )
    store = InMemoryImmutableLifeContentStore()
    store.put_if_absent(
        StoredLifeContent(
            content_ref=experience.values.summary_ref,
            content_kind="experience_summary",
            content_payload_hash=summary_hash,
            text=text,
        )
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:unused",
        world_id=authority.WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="actor:companion",
        source="test",
        trace_id="trace:unused",
        causation_id="cause:unused",
        correlation_id="correlation:unused",
        idempotency_key="unused",
        payload={"observation_id": "unused"},
    )
    return projection, candidate, cursor, store, event


def test_experience_backed_memory_retrieval_reads_the_exact_life_summary_sidecar() -> None:
    projection, candidate, cursor, store, event = _experience_memory_read_fixture()
    ledger = _MemoryReadLedger(projection=projection, event=event, cursor=cursor)

    result = MemoryRetrievalCompiler(
        ledger=ledger,
        life_content_store=store,
    ).compile(
        cursor=cursor,
        candidates=(candidate,),
        viewer_privacy_ceiling="private",
    )

    assert result.suppressions == ()
    assert result.items[0].source_excerpts[0].source_kind == "experience"
    assert result.items[0].source_excerpts[0].text == "今天下班路上遇到一场很短的雨，反而觉得很轻松。"


@pytest.mark.parametrize(
    "broken_link",
    ("transition", "descriptor_event", "descriptor_source", "sidecar"),
)
def test_experience_backed_memory_retrieval_fails_closed_on_any_missing_or_stale_link(
    broken_link: str,
) -> None:
    projection, candidate, cursor, store, event = _experience_memory_read_fixture()
    if broken_link == "transition":
        projection = projection.model_copy(update={"experience_transitions": ()})
    elif broken_link == "descriptor_event":
        descriptor = projection.life_content_descriptors[0]
        projection = projection.model_copy(
            update={
                "committed_world_event_refs": tuple(
                    item
                    for item in projection.committed_world_event_refs
                    if item.event_id != descriptor.descriptor_event_ref
                )
            }
        )
    elif broken_link == "descriptor_source":
        descriptor = projection.life_content_descriptors[0].model_copy(
            update={"source_payload_hash": "7" * 64}
        )
        projection = projection.model_copy(update={"life_content_descriptors": (descriptor,)})
    else:
        store = InMemoryImmutableLifeContentStore()
        wrong_text = "另一段摘要"
        store.put_if_absent(
            StoredLifeContent(
                content_ref=projection.experiences[0].values.summary_ref,
                content_kind="experience_summary",
                content_payload_hash=life_content_payload_hash(wrong_text),
                text=wrong_text,
            )
        )
    ledger = _MemoryReadLedger(projection=projection, event=event, cursor=cursor)

    result = MemoryRetrievalCompiler(ledger=ledger, life_content_store=store).compile(
        cursor=cursor,
        candidates=(candidate,),
        viewer_privacy_ceiling="private",
    )

    assert result.items == ()
    assert result.suppressions[0].reasons in {
        ("content_unavailable",),
        ("stale_source",),
    }
