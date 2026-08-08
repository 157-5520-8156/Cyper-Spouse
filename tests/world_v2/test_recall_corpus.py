from datetime import UTC, datetime, timedelta
import json

import pytest

from companion_daemon.world_v2.character_interior.snapshot_compiler import (
    compile_inner_life_snapshot,
)
from companion_daemon.world_v2.context_capsule import FactRecallItem
from companion_daemon.world_v2.life_content import (
    LifeContentExcerpt,
    RecentExperienceContextItem,
)
from companion_daemon.world_v2.memory_retrieval import (
    MemoryRetrievalItem,
    MemorySourceExcerpt,
)
from companion_daemon.world_v2.recall_audit import CharacterRecallRequest
from companion_daemon.world_v2.recall_corpus import (
    AffectOpeningRecallItem,
    NpcIdentityRecallItem,
    RecallCorpusCompiler,
    RecallCorpusSources,
    select_recall_authority_bindings,
)
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallSourceBinding,
)
from companion_daemon.world_v2.recall_runtime import (
    RecallCoordinator,
    augment_model_content_with_recall,
    verify_trusted_recall_trace,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
)
from companion_daemon.world_v2.recent_dialogue import (
    DialogueSourceClaim,
    RecentDialogueItem,
)
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import (
    AppraisalHypothesis,
    AppraisalOrigin,
    AppraisalProjection,
    ExperienceOccurrenceSettlementBinding,
    ExperienceOrigin,
    ExperienceValues,
    PrivateImpressionOrigin,
    PrivateImpressionProjection,
    experience_semantic_fingerprint,
)
from companion_daemon.world_v2.world_life_context import (
    WorldLifeContextItem,
    WorldLifeSourceBinding,
)

import test_affect_module as affect


NOW = datetime(2026, 7, 27, 14, 30, tzinfo=UTC)
CURSOR = RecallCursor(
    world_revision=27,
    deliberation_revision=11,
    ledger_sequence=51,
)


def _sources() -> RecallCorpusSources:
    dialogue = RecentDialogueItem(
        dialogue_id="dialogue:observation:message-1",
        speaker="counterpart",
        speaker_ref="user:primary",
        text="我最近开始用盖碗泡凤凰单丛。",
        occurred_at=NOW - timedelta(days=2),
        delivery_state="observed",
        sequence=18,
        privacy_class="private",
        source_claims=(
            DialogueSourceClaim(
                authority_event_ref="event:observation:1",
                authority_world_revision=18,
                authority_payload_hash="1" * 64,
            ),
        ),
    )
    fact = FactRecallItem(
        fact_id="fact:tea-preference",
        subject_ref="user:primary",
        predicate_code="preference.tea",
        source_excerpt="我最近开始用盖碗泡凤凰单丛。",
        confidence_bp=8_600,
        privacy_class="personal",
        occurred_at=NOW - timedelta(days=3),
        committed_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(days=2),
        accepted_fact_event_ref="event:fact:1",
        accepted_fact_world_revision=19,
        accepted_fact_payload_hash="2" * 64,
        observation_event_ref="event:observation:1",
        observation_world_revision=18,
        observation_event_payload_hash="1" * 64,
        source_observation_id="message-1",
        assertion_payload_ref="payload:message-1",
        assertion_payload_hash="3" * 64,
    )
    memory = MemoryRetrievalItem(
        candidate_id="memory:tea-session",
        cue_kind="conversation_association",
        retention_rationales=("user_interest",),
        privacy_ceiling="personal",
        retrieval_strength_bp=7_800,
        source_excerpts=(
            MemorySourceExcerpt(
                source_kind="fact",
                source_id="fact:tea-preference",
                source_entity_revision=1,
                authority_event_ref="event:fact:1",
                authority_world_revision=19,
                authority_payload_hash="2" * 64,
                source_values_hash="9" * 64,
                excerpt_ref="payload:message-1",
                excerpt_payload_hash="3" * 64,
                text="我最近开始用盖碗泡凤凰单丛。",
                truncated=False,
            ),
            MemorySourceExcerpt(
                source_kind="experience",
                source_id="experience:tea-shop",
                source_entity_revision=1,
                authority_event_ref="event:experience:1",
                authority_world_revision=21,
                authority_payload_hash="4" * 64,
                source_values_hash="5" * 64,
                excerpt_ref="content:experience:1",
                excerpt_payload_hash="6" * 64,
                text="那次在茶店里试了白瓷盖碗，香气很清楚。",
                truncated=False,
            ),
        ),
        truncated=False,
    )
    experience_values = ExperienceValues(
        summary_ref="content:experience:1",
        summary_payload_hash="6" * 64,
        occurred_from=NOW - timedelta(days=5),
        occurred_to=NOW - timedelta(days=5) + timedelta(minutes=40),
        participant_refs=("agent:companion",),
        source_bindings=(
            ExperienceOccurrenceSettlementBinding(
                authority_event_ref="event:experience-source:1",
                authority_world_revision=20,
                authority_payload_hash="a" * 64,
                occurrence_id="occurrence:tea-shop",
                occurrence_entity_revision=1,
                result_id="result:tea-shop",
                result_payload_ref="result-payload:tea-shop",
                result_payload_hash="b" * 64,
            ),
        ),
        privacy_class="personal",
    )
    experience_origin = ExperienceOrigin(
        change_id="change:experience:1",
        transition_id="transition:experience:1",
        policy_refs=("policy:experience.1",),
        accepted_event_ref="event:experience:1",
    )
    experience = RecentExperienceContextItem(
        experience_id="experience:tea-shop",
        semantic_fingerprint=experience_semantic_fingerprint(
            values=experience_values,
            policy_refs=experience_origin.policy_refs,
        ),
        values=experience_values,
        origin=experience_origin,
        content=LifeContentExcerpt(
            content_id="life-content:experience:tea-shop",
            content_kind="experience_summary",
            content_ref=experience_values.summary_ref,
            content_payload_hash=experience_values.summary_payload_hash,
            text="那次在茶店里试了白瓷盖碗，香气很清楚。",
            truncated=False,
            privacy_class=experience_values.privacy_class,
            source_entity_id="experience:tea-shop",
            source_entity_revision=1,
            authority_event_ref="event:experience:1",
            authority_world_revision=21,
            authority_payload_hash="4" * 64,
            descriptor_event_ref="event:life-content:experience:1",
            descriptor_world_revision=22,
            descriptor_payload_hash="f" * 64,
        ),
    )
    appraisal = AppraisalProjection(
        appraisal_id="appraisal:care",
        entity_revision=1,
        subject_ref="user:primary",
        source_cluster_ref="cluster:message-1",
        origin=AppraisalOrigin(
            change_id="change:appraisal:1",
            transition_id="transition:appraisal:1",
            policy_refs=("policy:appraisal.1",),
            matrix_catalog_version="matrix.1",
            clustering_policy_version="cluster.1",
            accepted_event_ref="event:appraisal:1",
        ),
        hypotheses=(
            AppraisalHypothesis(
                hypothesis_id="hypothesis:care",
                meaning="care",
                attribution="user",
                controllability="partly_controllable",
                severity="low",
                weight_bp=10_000,
            ),
        ),
        evidence_refs=(
            EvidenceRef(
                ref_id="event:observation:1",
                evidence_type="observed_message",
                claim_purpose="private_hypothesis",
            ),
        ),
        confidence_bp=7_200,
        accepted_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=6),
        status="active",
    )
    impression = PrivateImpressionProjection(
        impression_id="impression:care",
        subject_ref="user:primary",
        interpretation_refs=("appraisal:appraisal:care:hypothesis:care",),
        source_refs=("event:observation:1",),
        reflection_summary="我觉得他的关心是认真的，但这仍只是我现在的理解。",
        confidence_bp=6_900,
        first_seen=NOW - timedelta(days=1),
        last_supported=NOW - timedelta(days=1),
        expiry_condition="until_counter_evidence",
        status="active",
        origin=PrivateImpressionOrigin(
            change_id="change:impression:1",
            transition_id="transition:impression:1",
            policy_refs=("policy:private-impression.1",),
            accepted_event_ref="event:impression:1",
        ),
    )
    affect_opening = affect.episode(
        episode_id="affect:remembered-rain",
        at=NOW - timedelta(hours=18),
    )
    return RecallCorpusSources(
        recent_dialogue=(dialogue,),
        relevant_facts=(fact,),
        recent_experiences=(experience,),
        active_memory_candidates=(memory,),
        affect_openings=(
            AffectOpeningRecallItem(
                episode=affect_opening,
                subject_refs=("user:primary",),
                subject_authority_refs=("event:appraisal:1",),
            ),
        ),
        appraisals=(appraisal,),
        private_impressions=(impression,),
        authority_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="AppraisalAccepted",
                ref="event:appraisal:1",
                source_world_revision=22,
                immutable_hash="7" * 64,
            ),
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="PrivateImpressionAccepted",
                ref="event:impression:1",
                source_world_revision=23,
                immutable_hash="8" * 64,
            ),
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="ObservationRecorded",
                ref="event:observation:1",
                source_world_revision=18,
                immutable_hash="1" * 64,
            ),
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="AffectEpisodeOpened",
                ref="event:affect:remembered-rain",
                source_world_revision=24,
                immutable_hash="c" * 64,
            ),
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="ObservationRecorded",
                ref="message:1",
                source_world_revision=17,
                immutable_hash="d" * 64,
            ),
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="ExperienceCommitted",
                ref="event:experience:1",
                source_world_revision=21,
                immutable_hash="4" * 64,
            ),
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="LifeContentRecorded",
                ref="event:life-content:experience:1",
                source_world_revision=22,
                immutable_hash="f" * 64,
            ),
        ),
    )


def test_corpus_preserves_exact_source_text_and_memory_authority() -> None:
    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=_sources(),
    )
    by_ref = {document.source_item_ref: document for document in documents}

    fact = by_ref["fact:tea-preference"]
    assert fact.text == "我最近开始用盖碗泡凤凰单丛。"
    # This fixture deliberately uses an uninstalled predicate; the compiler
    # must not invent ontology metadata for it.
    assert fact.retrieval_text is None
    assert fact.source_refs == ("event:fact:1", "event:observation:1")
    assert fact.memory_kind == "semantic"
    assert fact.authority == "world_fact"
    assert "memory:tea-session" in fact.link_refs
    assert "conversation_association" in fact.link_refs
    assert (
        len(
            [
                document
                for document in documents
                if document.source_item_ref == "fact:tea-preference"
            ]
        )
        == 1
    )

    experience = by_ref["experience:tea-shop"]
    assert experience.text == "那次在茶店里试了白瓷盖碗，香气很清楚。"
    assert experience.source_refs == (
        "event:experience:1",
        "event:life-content:experience:1",
    )
    assert experience.memory_kind == "episodic"
    assert "memory:tea-session" in experience.link_refs


def test_recent_experience_is_searchable_before_memory_consolidation() -> None:
    sources = _sources().model_copy(update={"active_memory_candidates": ()})

    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=sources,
    )

    experience = next(
        document
        for document in documents
        if document.source_item_ref == "experience:tea-shop"
    )
    assert experience.text == "那次在茶店里试了白瓷盖碗，香气很清楚。"
    assert experience.source_slice == "recent_experiences"
    assert experience.source_refs == (
        "event:experience:1",
        "event:life-content:experience:1",
    )
    assert "memory:tea-session" not in experience.link_refs


def test_npc_identity_is_recallable_without_exposing_private_npc_state() -> None:
    registration = RecallSourceBinding(
        source_kind="committed_event",
        authority_type="NpcRegistered",
        ref="event:npc:lin:registered",
        source_world_revision=25,
        immutable_hash="a" * 64,
    )
    descriptor = RecallSourceBinding(
        source_kind="immutable_payload",
        authority_type="NpcIdentityDescriptor",
        ref="content:npc:lin",
        source_world_revision=25,
        immutable_hash="b" * 64,
    )
    identity = NpcIdentityRecallItem(
        npc_ref="npc:lin",
        descriptor="林嘉，角色在实习团队里认识的设计师。",
        descriptor_content_ref="content:npc:lin",
        lifecycle_state="active",
        occurred_at=NOW - timedelta(days=4),
        privacy_class="personal",
        bindings=tuple(sorted((registration, descriptor), key=lambda item: item.source_kind)),
        link_refs=("experience:met-lin", "organization:internship"),
    )
    sources = RecallCorpusSources(
        npc_identities=(identity,),
        authority_bindings=identity.bindings,
    )

    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=sources,
    )

    assert len(documents) == 1
    document = documents[0]
    assert document.text == identity.descriptor
    assert document.subject_refs == ("agent:companion", "npc:lin")
    assert document.source_refs == ("content:npc:lin", "event:npc:lin:registered")
    assert "inner_state" not in document.text


def test_foreign_recent_experience_does_not_enter_companion_recall() -> None:
    sources = _sources()
    experience = sources.recent_experiences[0]
    foreign_values = experience.values.model_copy(
        update={"participant_refs": ("npc:lin",)}
    )
    foreign_experience = experience.model_copy(
        update={
            "values": foreign_values,
            "semantic_fingerprint": experience_semantic_fingerprint(
                values=foreign_values,
                policy_refs=experience.origin.policy_refs,
            ),
        }
    )

    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=RecallCorpusSources(
            recent_experiences=(foreign_experience,),
            authority_bindings=sources.authority_bindings,
        ),
    )

    assert documents == ()


def test_foreign_world_life_does_not_enter_companion_recall() -> None:
    foreign_life = WorldLifeContextItem(
        occurrence_id="occurrence:npc-only",
        occurrence_entity_revision=2,
        participant_refs=("npc:lin",),
        location_ref="room:kitchen",
        result_id="result:npc-only",
        result_payload_ref="result-payload:npc-only",
        result_payload_hash="8" * 64,
        settled_at=NOW - timedelta(hours=2),
        privacy_class="personal",
        source=WorldLifeSourceBinding(
            authority_event_ref="event:occurrence:npc-only",
            authority_world_revision=25,
            authority_payload_hash="9" * 64,
        ),
        content=LifeContentExcerpt(
            content_id="life-content:occurrence:npc-only",
            content_kind="world_occurrence_result",
            content_ref="content:occurrence:npc-only",
            content_payload_hash="a" * 64,
            text="林一个人在厨房试了新的茶具。",
            truncated=False,
            privacy_class="personal",
            source_entity_id="occurrence:npc-only",
            source_entity_revision=2,
            authority_event_ref="event:occurrence:npc-only",
            authority_world_revision=25,
            authority_payload_hash="9" * 64,
            descriptor_event_ref="event:life-content:npc-only",
            descriptor_world_revision=26,
            descriptor_payload_hash="b" * 64,
        ),
    )

    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=RecallCorpusSources(world_life=(foreign_life,)),
    )

    assert documents == ()


@pytest.mark.parametrize(
    ("event_ref", "authority_type", "source_kind"),
    (
        ("event:experience:1", "ObservationRecorded", "committed_event"),
        (
            "event:life-content:experience:1",
            "ExperienceCommitted",
            "committed_event",
        ),
        ("event:experience:1", "ExperienceCommitted", "immutable_payload"),
    ),
)
def test_recent_experience_recall_rejects_wrong_authority_bindings(
    event_ref: str,
    authority_type: str,
    source_kind: str,
) -> None:
    sources = _sources()
    sources = sources.model_copy(
        update={
            "authority_bindings": tuple(
                item.model_copy(
                    update={
                        "authority_type": authority_type,
                        "source_kind": source_kind,
                    }
                )
                if item.ref == event_ref
                else item
                for item in sources.authority_bindings
            )
        }
    )

    with pytest.raises(ValueError, match="wrong authority type"):
        RecallCorpusCompiler().compile(
            cursor=CURSOR,
            actor_ref="agent:companion",
            subject_refs=("agent:companion", "user:primary"),
            sources=sources,
        )


def test_counterpart_observation_is_report_only_dialogue_authority() -> None:
    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=_sources(),
    )

    dialogue = next(
        document
        for document in documents
        if document.source_item_ref == "dialogue:observation:message-1"
    )

    # The immutable Observation proves that the counterpart said these
    # words.  It does not prove that the companion lived the reported event,
    # nor that every proposition inside the report is an objective World fact.
    assert dialogue.authority == "dialogue_record"
    assert dialogue.epistemic_scope == "counterpart_report_only"
    assert dialogue.speaker_ref == "user:primary"
    assert dialogue.subject_refs == ("user:primary",)


def test_recalled_counterpart_report_keeps_its_speaker_and_epistemic_scope_in_model_context() -> None:
    sources = _sources()
    vendor_report = sources.recent_dialogue[0].model_copy(
        update={"text": "我今天跟学校门口那个摊贩吵了一架，烦死了。"}
    )
    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=sources.model_copy(update={"recent_dialogue": (vendor_report,)}),
    )
    dialogue = next(
        document
        for document in documents
        if document.source_item_ref == "dialogue:observation:message-1"
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=CURSOR, documents=(dialogue,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref="event:observation:current",
    )
    trace = verify_trusted_recall_trace(
        coordinator.recall(
                request=CharacterRecallRequest(
                    query_text="学校门口那个摊贩",
                    lexical_text="我今天跟学校门口那个摊贩吵了一架",
                ),
            accessibility_seed="draw:counterpart-report",
            expected_cursor=CURSOR,
            trigger_ref="event:observation:current",
        )
    )
    augmented = augment_model_content_with_recall(
        json.dumps(
            {
                "world_id": "world:test",
                "actor_ref": "agent:companion",
                "trigger_ref": "event:observation:current",
                "world_revision": CURSOR.world_revision,
                "logical_time": NOW.isoformat(),
                "slices": {},
            },
            ensure_ascii=False,
        ),
        trace,
    )
    compact = json.loads(compact_chat_model_facing_context(augmented))
    recalled = compact["slices"]["recent_dialogue"]["items"][0]["value"]

    assert recalled["authority"] == "dialogue_record"
    assert recalled["epistemic_scope"] == "counterpart_report_only"
    assert recalled["speaker_ref"] == "user:primary"
    assert recalled["subject_refs"] == ["user:primary"]
    assert recalled["text"] == "我今天跟学校门口那个摊贩吵了一架，烦死了。"
    assert "inner_life_snapshot" not in compact
    inner_life = compile_inner_life_snapshot(compact).model_view()
    assert inner_life["availability"] == "available"
    assert dialogue.source_item_ref in inner_life["source_refs"]
    assert inner_life["materials"]["recent_dialogue"][0]["speaker_ref"] == (
        "user:primary"
    )
    assert inner_life["materials"]["recent_self_experiences"] == {
        "availability": "unavailable"
    }


def test_installed_fact_adds_search_metadata_without_changing_source_text() -> None:
    sources = _sources()
    fact = sources.relevant_facts[0].model_copy(
        update={
            "predicate_code": "profile.occupation",
            "source_excerpt": "我是软件工程师。",
        }
    )

    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=sources.model_copy(update={"relevant_facts": (fact,)}),
    )
    document = next(item for item in documents if item.source_item_ref == fact.fact_id)

    assert document.text == "我是软件工程师。"
    assert document.retrieval_text is not None
    assert "用户工作、职业、做什么" in document.retrieval_text


def test_authority_selection_does_not_scale_with_unrelated_ledger_history() -> None:
    sources = _sources()
    unbound = sources.model_copy(update={"authority_bindings": ()})
    unrelated = tuple(
        RecallSourceBinding(
            source_kind="committed_event",
            authority_type="ClockAdvanced",
            ref=f"event:unrelated:{index}",
            source_world_revision=index % CURSOR.world_revision,
            immutable_hash=f"{index:064x}",
        )
        for index in range(5_000)
    )

    selected = select_recall_authority_bindings(
        sources=unbound,
        candidates=(*unrelated, *sources.authority_bindings),
    )
    bounded_sources = unbound.model_copy(update={"authority_bindings": selected})
    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=bounded_sources,
    )

    assert {item.ref for item in selected} == {item.ref for item in sources.authority_bindings}
    assert len(selected) == len(sources.authority_bindings)
    assert documents


def test_private_reflection_is_resolved_from_appraisal_and_never_fact_authority() -> None:
    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=_sources(),
    )
    reflection = next(
        document for document in documents if document.source_item_ref == "impression:care"
    )

    assert reflection.memory_kind == "reflective"
    assert reflection.authority == "defeasible_interpretation"
    assert reflection.text == "我觉得他的关心是认真的，但这仍只是我现在的理解。"
    assert reflection.source_refs == (
        "event:appraisal:1",
        "event:impression:1",
        "event:observation:1",
    )


def test_superseded_private_impression_is_not_production_recall_history() -> None:
    sources = _sources()
    previous = sources.private_impressions[0].model_copy(
        update={
            "impression_id": "impression:old-negative",
            "reflection_summary": "旧负面印象：他不会认真听我说话。",
            "status": "superseded",
        }
    )
    successor_event_ref = "event:impression:successor"
    successor = sources.private_impressions[0].model_copy(
        update={
            "impression_id": "impression:current",
            "reflection_summary": "现在的理解：他其实愿意认真听我说话。",
            "first_seen": NOW,
            "last_supported": NOW,
            "origin": sources.private_impressions[0].origin.model_copy(
                update={
                    "change_id": "change:impression:successor",
                    "transition_id": "transition:impression:successor",
                    "accepted_event_ref": successor_event_ref,
                }
            ),
        }
    )
    sources = sources.model_copy(
        update={
            "private_impressions": (previous, successor),
            "authority_bindings": (
                *sources.authority_bindings,
                RecallSourceBinding(
                    source_kind="committed_event",
                    authority_type="PrivateImpressionAccepted",
                    ref=successor_event_ref,
                    source_world_revision=25,
                    immutable_hash="e" * 64,
                ),
            ),
        }
    )
    coordinator = RecallCoordinator(
        index=InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    )
    coordinator.refresh(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        sources=sources,
        trigger_ref="trigger:private-impression-recall",
    )

    historical = verify_trusted_recall_trace(
        coordinator.recall(
            request=CharacterRecallRequest(
                query_text=previous.reflection_summary,
                memory_kinds=("reflective",),
                include_historical=True,
                limit=6,
            ),
            accessibility_seed="draw:private-impression:historical",
            expected_cursor=CURSOR,
            trigger_ref="trigger:private-impression-recall",
        )
    )
    current = verify_trusted_recall_trace(
        coordinator.recall(
            request=CharacterRecallRequest(
                query_text=successor.reflection_summary,
                memory_kinds=("reflective",),
                include_historical=True,
                limit=6,
            ),
            accessibility_seed="draw:private-impression:current",
            expected_cursor=CURSOR,
            trigger_ref="trigger:private-impression-recall",
        )
    )
    coordinator.close()

    assert all(
        hit.document.source_item_ref != previous.impression_id
        for hit in historical.hits
    )
    assert any(
        hit.document.source_item_ref == successor.impression_id
        for hit in current.hits
    )


def test_appraisal_becomes_source_bound_affective_reflective_recall() -> None:
    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=_sources(),
    )
    emotion = next(
        document for document in documents if document.source_item_ref == "appraisal:care"
    )

    assert emotion.memory_kind == "reflective"
    assert emotion.source_slice == "recalled_emotional_associations"
    assert emotion.authority == "defeasible_interpretation"
    assert "care" in emotion.text
    assert emotion.source_refs == (
        "event:appraisal:1",
        "event:observation:1",
    )


def test_exact_affect_opening_dimension_is_source_bound_recall() -> None:
    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=_sources(),
    )
    emotion = next(
        document
        for document in documents
        if document.source_item_ref == "affect-opening:affect:remembered-rain"
    )

    assert emotion.memory_kind == "reflective"
    assert emotion.authority == "defeasible_interpretation"
    assert "hurt=4000bp" in emotion.text
    assert emotion.source_refs == (
        "event:affect:remembered-rain",
        "event:appraisal:1",
        "message:1",
    )


def test_affect_opening_from_another_subject_is_not_relabelled_to_current_user() -> None:
    sources = _sources()
    foreign = sources.affect_openings[0].model_copy(
        update={"subject_refs": ("npc:someone-else",)}
    )

    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=sources.model_copy(update={"affect_openings": (foreign,)}),
    )

    assert all(
        document.source_item_ref
        != "affect-opening:affect:remembered-rain"
        for document in documents
    )


def test_oversized_appraisal_is_skipped_instead_of_truncating_source_closure() -> None:
    sources = _sources()
    evidence = tuple(
        EvidenceRef(
            ref_id=f"event:oversized-appraisal-evidence:{index}",
            evidence_type="observed_message",
            claim_purpose="private_hypothesis",
        )
        for index in range(16)
    )
    appraisal = sources.appraisals[0].model_copy(
        update={"evidence_refs": evidence}
    )
    bindings = (
        *sources.authority_bindings,
        *(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="ObservationRecorded",
                ref=item.ref_id,
                source_world_revision=10 + index,
                immutable_hash=f"{index + 100:064x}",
            )
            for index, item in enumerate(evidence)
        ),
    )

    documents = RecallCorpusCompiler().compile(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        sources=sources.model_copy(
            update={
                "appraisals": (appraisal,),
                "private_impressions": (),
                "authority_bindings": bindings,
            }
        ),
    )

    assert all(
        document.source_item_ref != appraisal.appraisal_id
        for document in documents
    )
