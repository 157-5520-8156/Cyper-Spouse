from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.context_capsule import FactRecallItem
from companion_daemon.world_v2.memory_retrieval import (
    MemoryRetrievalItem,
    MemorySourceExcerpt,
)
from companion_daemon.world_v2.recall_corpus import (
    RecallCorpusCompiler,
    RecallCorpusSources,
)
from companion_daemon.world_v2.recall_index import RecallCursor, RecallSourceBinding
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
    ExperienceProjection,
    ExperienceValues,
    PrivateImpressionOrigin,
    PrivateImpressionProjection,
    experience_semantic_fingerprint,
)


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
    experience = ExperienceProjection(
        experience_id="experience:tea-shop",
        semantic_fingerprint=experience_semantic_fingerprint(
            values=experience_values,
            policy_refs=experience_origin.policy_refs,
        ),
        values=experience_values,
        origin=experience_origin,
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
    return RecallCorpusSources(
        recent_dialogue=(dialogue,),
        relevant_facts=(fact,),
        recent_experiences=(experience,),
        active_memory_candidates=(memory,),
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
    assert fact.source_refs == ("event:fact:1", "event:observation:1")
    assert fact.memory_kind == "semantic"
    assert fact.authority == "world_fact"

    experience = by_ref["experience:tea-shop"]
    assert experience.text == "那次在茶店里试了白瓷盖碗，香气很清楚。"
    assert experience.source_refs == ("event:experience:1",)
    assert experience.memory_kind == "episodic"


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
    assert "care" in reflection.text
    assert "user" in reflection.text
    assert reflection.source_refs == (
        "event:appraisal:1",
        "event:impression:1",
        "event:observation:1",
    )
