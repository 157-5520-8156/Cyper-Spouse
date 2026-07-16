from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
import hashlib
import json
from typing import Any

from pydantic import ValidationInfo, model_validator

from .action_lifecycle import TERMINAL_ACTION_STATES, transition_action
from .affect_events import (
    AFFECT_PAYLOAD_MODELS,
    AffectBaselineAdjustedPayload,
    AffectAuthorizedMutationPayload,
    AffectEpisodeDecayedPayload,
    AffectEpisodeOpenedPayload,
    AffectEpisodeResolvedPayload,
    AffectEpisodeSupersededPayload,
    AffectEpisodeUpdatedPayload,
)
from .affect_reducers import (
    adjust_affect_baseline,
    decay_affect_episode,
    open_affect_episode,
    resolve_affect_episode,
    supersede_affect_episode,
    update_affect_episode,
)
from .appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalExpiredPayload,
    AppraisalSupersededPayload,
)
from .appraisal_acceptance_manifest import (
    APPRAISAL_ACCEPTANCE_MANIFEST_VERSION,
    AppraisalAcceptanceManifest,
    canonical_appraisal_acceptance_value_hash,
)
from .affect_acceptance_manifest import (
    AFFECT_ACCEPTANCE_MANIFEST_VERSION,
    AffectAcceptanceManifest,
    canonical_affect_acceptance_value_hash,
)
from .outcome_acceptance_manifest import (
    OUTCOME_ACCEPTANCE_MANIFEST_VERSION,
    OutcomeAcceptanceManifest,
)
from .interaction_bid_acceptance_manifest import (
    INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION,
    InteractionBidAcceptanceManifest,
)
from .interaction_bid_events import (
    InteractionBidOpenedPayload,
    InteractionBidProposalRecordedPayload,
)
from .media_thread_acceptance_manifest import (
    MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
    MediaDeliveryThreadAcceptanceManifest,
)
from .media_thread_events import (
    MediaDeliveryThreadChangedPayload,
    MediaDeliveryThreadProposalRecordedPayload,
)
from .media_v2 import (
    MediaArtifact,
    MediaAutomaticDeliveryApproval,
    MediaAutomaticDeliveryApprovedPayload,
    MediaDeliveryShared,
    MediaDeliverySharedPayload,
    MediaInspectionRecord,
    MediaPreview,
    MediaRenderArtifactRecordedPayload,
    MediaInspectionRecordedPayload,
    MediaPreviewGeneratedPayload,
    MediaPreviewFailedPayload,
    MediaRepairAuthorizedPayload,
    MediaNotRenderableRecordedPayload,
    MediaOpportunityFrozenPayload,
    MediaPlanRecordedPayload,
    MediaOpportunity,
    MediaPlan,
    PhotoCandidate,
    PhotoCandidateExpiredPayload,
    PhotoCandidateOpenedPayload,
    PhotoCandidateUnrenderablePayload,
    continuation_trigger_id,
    media_repair_attempt_id,
    media_repair_action_id,
    media_repair_reservation_id,
    media_repair_trigger_id,
    media_delivery_action_id,
    media_delivery_id,
    media_delivery_reservation_id,
    media_digest,
    planning_request_id,
)
from .image_evidence_contract import (
    DECLARABLE_SOURCE_EVENT_TYPES,
    ImageEvidenceDeclaredPayload,
)
from .private_image_evidence_contract import RecipientScopedImageEvidenceDeclaredPayload
from .appearance_state import (
    APPEARANCE_SOURCE_EVENT_TYPES,
    AppearanceStateProjection,
    AppearanceStateRecordedPayload,
)
from .visible_physical_state import (
    VisiblePhysicalStateProjection,
    VisiblePhysicalStateRecordedPayload,
)
from .random_authority import RandomDrawRecordedPayload
from .appraisal_reducers import (
    accept_appraisal,
    contradict_appraisal,
    expire_appraisal,
    supersede_appraisal,
)
from .attention_authority_contract import V2_ATTENTION_MUTATION_EVENT_TYPES
from .attention_authority_events import V2AttentionChangedPayload
from .attention_authority_reducers import V2_ATTENTION_POLICY_REFS, reduce_v2_attention
from .attention_authority_schemas import (
    V2AttentionProjection,
    V2AttentionProposalProjection,
    V2AttentionTransitionProjection,
    validate_v2_attention_authority_state,
)
from .actor_authority_events import ActorAuthorityMutationPayload
from .actor_authority_reducers import reduce_actor_authority
from .authorization_events import AUTHORIZATION_PAYLOAD_MODELS, authorization_domain
from .authorization_reducers import reduce_authorization
from .accepted_effect_contracts import (
    AcceptanceManifestRefV3,
    rehydrate_acceptance_manifest_v3,
)
from .acceptance_manifest import (
    AcceptanceManifestRefV2,
    derive_acceptance_manifest_proposal_v2,
    parse_acceptance_manifest_v2,
)
from .batch_invariants import interaction_appraisal_trigger_identity
from .fact_trigger import interaction_fact_trigger_identity
from .commitment_events import (
    COMMITMENT_ACCEPTED_PAYLOAD_MODELS,
    CommitmentAuthorizedMutationPayload,
    CommitmentChangedPayload,
    CommitmentClockTransitionPayload,
)
from .commitment_reducers import reduce_commitment, reduce_commitment_clock
from .character_core_events import CHARACTER_CORE_PAYLOAD_MODELS, CharacterCoreChangedPayload
from .character_core_reducers import CHARACTER_CORE_POLICY_REFS, reduce_character_core
from .clock_authority import (
    append_clock_transition,
    validate_clock_history,
)
from .fact_events import FACT_PAYLOAD_MODELS, FactAuthorizedMutationPayload, FactChangedPayload
from .fact_proposal_audit_v2 import FactCommitProposalAuditRefV2
from .fact_accepted_contracts import rehydrate_fact_commit_materialized_v2_json
from .fact_reducers import reduce_fact
from .fact_v2_reducers import materialized_fact_v2_as_projection_change
from .minimal_reply_events import (
    ExpressionBeatAuthorizedPayload,
    ExpressionBeatSettledPayload,
    ExpressionPlanAcceptedPayload,
    ExpressionPlanCompletedPayload,
    MessagePayloadStoredPayload,
)
from .life_content_events import LifeContentRecordedPayload
from .life_ecology_contract import (
    LIFE_ECOLOGY_WAKE_EVENT_TYPES,
    life_ecology_trigger_id,
    parse_life_ecology_trigger_ref,
)
from .life_ecology_activity import ActivityOpeningCatalog
from .activity_lifecycle_acceptance_manifest import (
    ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
    ActivityLifecycleAcceptanceManifest,
)
from .activity_lifecycle_proposal import (
    ACTIVITY_LIFECYCLE_PROPOSAL_POLICY_DIGEST,
)
from .expression_payload_events import ExpressionPayloadDescriptorRecordedPayload
from .minimal_reply_manifest import (
    MINIMAL_REPLY_MANIFEST_VERSION,
    MinimalReplyManifest,
    canonical_minimal_reply_value_hash,
)
from .expression_plan_manifest import (
    EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION,
    ExpressionPlanAcceptanceManifest,
    canonical_expression_plan_value_hash,
)
from .proposal_envelope import DecisionProposal, MinimalProposal, validate_proposal_envelope
from .proposal_envelope_v2 import (
    canonical_full_change_authority_hash_v2,
    validate_fact_commit_proposal_v2,
)
from .goal_authority_events import (
    V2_GOAL_MECHANICAL_PAYLOAD_MODELS,
    V2_GOAL_PAYLOAD_MODELS,
    V2GoalChangedPayload,
    V2GoalExpiredPayload,
)
from .goal_authority_reducers import (
    V2_GOAL_POLICY_REFS,
    reduce_v2_goal,
    reduce_v2_goal_expiry,
)
from .goal_situation_schemas import (
    V2GoalProjection,
    V2GoalProposalProjection,
    V2GoalTransitionProjection,
    validate_v2_goal_authority_state,
)
from .location_authority_contract import V2_LOCATION_MUTATION_EVENT_TYPES
from .location_authority_events import V2LocationChangedPayload
from .location_authority_reducers import V2_LOCATION_POLICY_REFS, reduce_v2_location
from .location_authority_schemas import (
    V2LocationProjection,
    V2LocationProposalProjection,
    V2LocationTransitionProjection,
    validate_v2_location_authority_state,
)
from .resource_authority_contract import V2_RESOURCE_EVENT_TYPES
from .resource_authority_events import (
    V2ResourceChangedPayload,
    V2ResourceClockAdjustedPayload,
    reduce_v2_resource_clock_adjustment,
)
from .resource_authority_reducers import V2_RESOURCE_POLICY_REFS, reduce_v2_resource
from .resource_authority_schemas import (
    V2ResourceProjection,
    V2ResourceProposalProjection,
    V2ResourceTransitionProjection,
    validate_v2_resource_authority_state,
)
from .experience_events import (
    ExperienceAuthorizedMutationPayload,
    ExperienceCommittedPayload,
    LegacyExperienceCommittedPayload,
)
from .errors import UnknownEventType
from .event_catalog import event_contract
from .life_events import (
    ActivityLifecycleProposalRecordedPayload,
    ActivityPlannedPayload,
    ActivityTransitionPayload,
    NpcRegisteredPayload,
    OutcomeObservationRecordedPayload,
    OutcomeProposalRecordedPayload,
    WorldOccurrenceActivatedPayload,
    WorldOccurrenceCommittedPayload,
    WorldOccurrenceSettledPayload,
    WorldOccurrenceTerminalPayload,
)
from .media_selection_proposal import (
    MediaSelectionProposalRecordedPayload,
    media_candidate_authority_hash,
)
from .media_selection_acceptance_manifest import (
    MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION,
    MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION,
    MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS,
    parse_media_selection_acceptance_manifest,
)
from .life_reducers import (
    activate_occurrence,
    commit_experience,
    commit_legacy_experience,
    commit_occurrence,
    plan_activity,
    record_outcome_observation,
    record_outcome_proposal,
    register_npc,
    settle_occurrence,
    terminate_occurrence,
    transition_activity,
)
from .plan_evidence import canonical_plan_evidence_hash
from .media_provider_grants import (
    ProviderMediaGrantRecordedPayload,
    is_provider_media_action,
    require_provider_media_grant,
    validate_provider_media_grant_record,
)
from .memory_events import (
    MEMORY_CANDIDATE_PAYLOAD_MODELS,
    MemoryCandidateAuthorizedMutationPayload,
    MemoryCandidateChangedPayload,
    MemoryEvidenceForgetAuthority,
)
from .memory_reducers import MEMORY_POLICY_REFS, reduce_memory_candidate
from .proposal_audit_schemas import (
    ModelResultAuditProjection,
    ModelResultRecordedPayload,
    ProposalAuditProjection,
    ProposalRecordedV2Payload,
    RecordedModelResultAudit,
    validate_recorded_attempt_lineage,
)
from .relationship_events import (
    RELATIONSHIP_PAYLOAD_MODELS,
    BoundaryChangedPayload,
    RelationshipAuthorizedMutationPayload,
    RelationshipSignalAcceptedPayload,
    RelationshipSlowVariableAdjustedPayload,
)
from .relationship_reducers import (
    accept_relationship_signal,
    adjust_relationship_slow_variables,
    change_boundary,
)
from .private_impression_events import (
    PrivateImpressionAcceptedPayload,
    PrivateImpressionAuthorizedPayload,
)
from .private_impression_reducers import accept_private_impression
from .thread_events import (
    THREAD_PAYLOAD_MODELS,
    ThreadAuthorizedMutationPayload,
    ThreadChangedPayload,
    ThreadExpiredPayload,
)
from .thread_reducers import expire_thread, reduce_thread
from .read_only_tool import (
    ToolRequestAcceptedPayload,
    ToolResultAcceptedPayload,
    external_result_trigger_id,
)
from .perception import (
    PerceptionRequestAcceptedPayload,
    PerceptionResultAcceptedPayload,
)
from .typed_proposal_families import INSTALLED_TYPED_PROPOSAL_FAMILIES
from .typed_proposals import (
    TypedProposalRegistration,
    TypedProposalRegistry,
)
from .schemas import (
    Action,
    ActionDispatchClaim,
    ActionReconciliation,
    ActionState,
    ActorAuthorityProjection,
    ActorAuthorityTransitionProjection,
    CapabilityStateProjection,
    CapabilityTransitionProjection,
    ConsentStateProjection,
    ConsentTransitionProjection,
    AffectBaselineProjection,
    AffectEpisodeProjection,
    AffectProposalProjection,
    BoundaryProjection,
    AppraisalProjection,
    AppraisalMeaningRef,
    AppraisalProposalProjection,
    AcceptanceDecisionRef,
    BudgetAccount,
    BudgetReservation,
    BudgetSettlement,
    ClaimLease,
    DispatchPending,
    CharacterCoreProjection,
    CharacterCoreProposalProjection,
    CharacterCoreTransitionProjection,
    ClockTransitionProjection,
    CommittedWorldEventRef,
    CommitmentProjection,
    CommitmentProposalProjection,
    CommitmentTransitionProjection,
    ExecutionReceipt,
    EvidenceRef,
    ExternalObservation,
    FrozenModel,
    ExperienceProjection,
    ExperienceAuthorityProjection,
    ExperienceTransitionProjection,
    ExperienceProposalProjection,
    ExperienceOccurrenceSettlementBinding,
    LegacyExperienceProjection,
    LifeContentDescriptorProjection,
    MinimalReplyManifestRef,
    ExpressionPlanManifestRef,
    ExpressionPlanManifestBeatRef,
    StoredMessagePayloadProjection,
    ExpressionPayloadDescriptorProjection,
    ExpressionPlanProjection,
    ExpressionBeatProjection,
    ExpressionPlanLifecycleEntry,
    ExpressionBeatLifecycleEntry,
    FactProjection,
    FactProposalProjection,
    FactTransitionProjection,
    InteractionBidProjection,
    InteractionBidProposalProjection,
    MediaDeliveryThreadProposalProjection,
    LedgerProjection,
    MessageObservationRef,
    MemoryCandidateProjection,
    MemoryCandidateProposalProjection,
    MemoryCandidateTransitionProjection,
    NpcProjection,
    Observation,
    OutcomeObservationProjection,
    PrivacyPolicyProjection,
    PrivacyTransitionProjection,
    ProviderMediaGrant,
    OutcomeProposalProjection,
    OperatorObservationRef,
    PlanStateProjection,
    ProposalRevisionRef,
    RelationshipAdjustmentProjection,
    RelationshipProposalProjection,
    RelationshipSignalProjection,
    RelationshipStateProjection,
    ReadOnlyToolRequestProjection,
    PerceptionRequestProjection,
    PerceptionResultProjection,
    PrivateImpressionProjection,
    PrivateImpressionProposalProjection,
    ThreadProjection,
    ThreadProposalProjection,
    ThreadTransitionProjection,
    TriggerProcess,
    ToolResultProjection,
    WorldOccurrenceProjection,
    WorldEvent,
    validate_actor_authority_event_bindings,
    validate_plan_authority_state,
)


REDUCER_BUNDLE_VERSION = "world-v2-reducers.32"
_LEGACY_ACTOR_BINDING_BUNDLES = frozenset(
    f"world-v2-reducers.{version}" for version in (1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
)
INSTALLED_APPRAISAL_POLICY_REFS = ("policy:appraisal-v1",)
INSTALLED_APPRAISAL_MATRIX_VERSION = "appraisal-matrix.1"
INSTALLED_SOURCE_CLUSTERING_VERSION = "source-clustering.1"
INSTALLED_AFFECT_POLICY_REFS = ("policy:affect-v1",)
INSTALLED_AFFECT_BASELINE_POLICY_REFS = ("policy:affect-baseline-v1",)
INSTALLED_AFFECT_MATRIX_VERSION = "affect-matrix.1"
INSTALLED_AFFECT_MERGE_WINDOW_SECONDS = 900
INSTALLED_RELATIONSHIP_SIGNAL_POLICY_REFS = ("policy:relationship-signal-v1",)
INSTALLED_RELATIONSHIP_POLICY_REFS = ("policy:relationship-v1",)
INSTALLED_BOUNDARY_POLICY_REFS = ("policy:boundary-v1",)
INSTALLED_THREAD_POLICY_REFS = ("policy:thread-v1",)
INSTALLED_COMMITMENT_POLICY_REFS = ("policy:commitment-v1",)
INSTALLED_FACT_POLICY_REFS = ("policy:fact-v1",)
INSTALLED_EXPERIENCE_POLICY_REFS = ("policy:experience-v1",)


def _experience_semantic_dump(
    experience: ExperienceProjection | LegacyExperienceProjection,
    *,
    reducer_bundle_version: str,
) -> dict[str, Any]:
    dumped = experience.model_dump(mode="json")
    if reducer_bundle_version not in {
        "world-v2-reducers.13",
        "world-v2-reducers.14",
        "world-v2-reducers.15",
        "world-v2-reducers.16",
        "world-v2-reducers.17",
        "world-v2-reducers.18",
        "world-v2-reducers.19",
        "world-v2-reducers.20",
        "world-v2-reducers.21",
        "world-v2-reducers.24",
        REDUCER_BUNDLE_VERSION,
    } and isinstance(experience, LegacyExperienceProjection):
        dumped.pop("authority_contract_version", None)
        dumped["status"] = "committed"
    return dumped


def _actor_authority_transition_semantic_dump(
    transition: ActorAuthorityTransitionProjection,
    *,
    reducer_bundle_version: str,
) -> dict[str, Any]:
    dumped = transition.model_dump(mode="json")
    if reducer_bundle_version not in {
        "world-v2-reducers.16",
        "world-v2-reducers.17",
        "world-v2-reducers.18",
        "world-v2-reducers.19",
        "world-v2-reducers.20",
        "world-v2-reducers.21",
        REDUCER_BUNDLE_VERSION,
    }:
        dumped.pop("accepted_event_ref", None)
        dumped.pop("accepted_world_revision", None)
        dumped.pop("accepted_payload_hash", None)
    return dumped


def _action_semantic_dump(action: Action, *, reducer_bundle_version: str) -> dict[str, Any]:
    dumped = action.model_dump(mode="json")
    if reducer_bundle_version not in {
        "world-v2-reducers.22",
        "world-v2-reducers.23",
        "world-v2-reducers.24",
        "world-v2-reducers.25",
        "world-v2-reducers.26",
        "world-v2-reducers.27",
        "world-v2-reducers.28",
        "world-v2-reducers.29",
        "world-v2-reducers.30",
        REDUCER_BUNDLE_VERSION,
    }:
        dumped.pop("expression_plan_id", None)
        dumped.pop("expression_beat_id", None)
    if reducer_bundle_version not in {
        "world-v2-reducers.25",
        "world-v2-reducers.26",
        "world-v2-reducers.27",
        "world-v2-reducers.28",
        "world-v2-reducers.29",
        "world-v2-reducers.30",
        REDUCER_BUNDLE_VERSION,
    }:
        dumped.pop("provider_media_grant", None)
    if reducer_bundle_version != REDUCER_BUNDLE_VERSION:
        dumped.pop("media_delivery_approval", None)
    return dumped


def _expression_plan_semantic_dump(
    plan: ExpressionPlanProjection, *, reducer_bundle_version: str
) -> dict[str, Any]:
    dumped = plan.model_dump(mode="json")
    if reducer_bundle_version not in {
        "world-v2-reducers.22",
        "world-v2-reducers.23",
        "world-v2-reducers.24",
        "world-v2-reducers.25",
        "world-v2-reducers.26",
        "world-v2-reducers.27",
        REDUCER_BUNDLE_VERSION,
    }:
        dumped.pop("state", None)
        dumped.pop("history", None)
    return dumped


def _expression_beat_semantic_dump(
    beat: ExpressionBeatProjection, *, reducer_bundle_version: str
) -> dict[str, Any]:
    dumped = beat.model_dump(mode="json")
    if reducer_bundle_version not in {
        "world-v2-reducers.22",
        "world-v2-reducers.23",
        "world-v2-reducers.24",
        "world-v2-reducers.25",
        "world-v2-reducers.26",
        "world-v2-reducers.27",
        REDUCER_BUNDLE_VERSION,
    }:
        dumped.pop("action_id", None)
        dumped.pop("state", None)
        dumped.pop("history", None)
    return dumped


def _expression_plan_manifest_semantic_dump(
    manifest: ExpressionPlanManifestRef,
) -> dict[str, Any]:
    """Preserve .24 hashes for ordinary inline plans.

    Descriptor authority is semantically visible only when a beat really uses
    the sidecar; adding default-sidecar metadata to historic inline manifests
    must not rewrite their replay hash.
    """
    dumped = manifest.model_dump(mode="json")
    for beat in dumped["beats"]:
        if beat["storage_kind"] == "inline_text":
            beat.pop("storage_kind")
            beat.pop("sidecar_kind")
            beat.pop("privacy_class")
    return dumped


class RevisionClass(StrEnum):
    WORLD = "world"
    DELIBERATION = "deliberation"


class ReducerState(FrozenModel):
    actor_authorities: tuple[ActorAuthorityProjection, ...] = ()
    actor_authority_transitions: tuple[ActorAuthorityTransitionProjection, ...] = ()
    consumed_actor_root_nonces: tuple[str, ...] = ()
    capability_grants: tuple[CapabilityStateProjection, ...] = ()
    capability_transitions: tuple[CapabilityTransitionProjection, ...] = ()
    consent_grants: tuple[ConsentStateProjection, ...] = ()
    consent_transitions: tuple[ConsentTransitionProjection, ...] = ()
    privacy_policies: tuple[PrivacyPolicyProjection, ...] = ()
    privacy_transitions: tuple[PrivacyTransitionProjection, ...] = ()
    provider_media_grants: tuple[ProviderMediaGrant, ...] = ()
    consumed_authorization_root_nonces: tuple[str, ...] = ()
    consumed_authorization_challenge_ids: tuple[str, ...] = ()
    consumed_authorization_source_ids: tuple[str, ...] = ()
    observation_refs: tuple[str, ...] = ()
    message_observations: tuple[MessageObservationRef, ...] = ()
    operator_observations: tuple[OperatorObservationRef, ...] = ()
    committed_world_event_refs: tuple[CommittedWorldEventRef, ...] = ()
    clock_transition_history: tuple[ClockTransitionProjection, ...] = ()
    goals: tuple[V2GoalProjection, ...] = ()
    goal_transitions: tuple[V2GoalTransitionProjection, ...] = ()
    goal_proposals: tuple[V2GoalProposalProjection, ...] = ()
    goal_proposal_ids: tuple[str, ...] = ()
    locations: tuple[V2LocationProjection, ...] = ()
    location_transitions: tuple[V2LocationTransitionProjection, ...] = ()
    location_proposals: tuple[V2LocationProposalProjection, ...] = ()
    location_proposal_ids: tuple[str, ...] = ()
    resources: tuple[V2ResourceProjection, ...] = ()
    resource_transitions: tuple[V2ResourceTransitionProjection, ...] = ()
    resource_proposals: tuple[V2ResourceProposalProjection, ...] = ()
    resource_proposal_ids: tuple[str, ...] = ()
    attentions: tuple[V2AttentionProjection, ...] = ()
    attention_transitions: tuple[V2AttentionTransitionProjection, ...] = ()
    attention_proposals: tuple[V2AttentionProposalProjection, ...] = ()
    attention_proposal_ids: tuple[str, ...] = ()
    logical_time: datetime | None = None
    actions: tuple[Action, ...] = ()
    pending_actions: tuple[Action, ...] = ()
    read_only_tool_requests: tuple[ReadOnlyToolRequestProjection, ...] = ()
    tool_results: tuple[ToolResultProjection, ...] = ()
    perception_requests: tuple[PerceptionRequestProjection, ...] = ()
    perception_results: tuple[PerceptionResultProjection, ...] = ()
    appearance_states: tuple[AppearanceStateProjection, ...] = ()
    visible_physical_states: tuple[VisiblePhysicalStateProjection, ...] = ()
    photo_candidates: tuple[PhotoCandidate, ...] = ()
    media_opportunities: tuple[MediaOpportunity, ...] = ()
    media_plans: tuple[MediaPlan, ...] = ()
    media_unrenderable_opportunity_ids: tuple[str, ...] = ()
    media_artifacts: tuple[MediaArtifact, ...] = ()
    media_inspections: tuple[MediaInspectionRecord, ...] = ()
    media_previews: tuple[MediaPreview, ...] = ()
    media_failed_plan_ids: tuple[str, ...] = ()
    media_delivery_approvals: tuple[MediaAutomaticDeliveryApproval, ...] = ()
    media_deliveries: tuple[MediaDeliveryShared, ...] = ()
    interaction_bids: tuple[InteractionBidProjection, ...] = ()
    interaction_bid_proposals: tuple[InteractionBidProposalProjection, ...] = ()
    media_thread_proposals: tuple[MediaDeliveryThreadProposalProjection, ...] = ()
    budget_accounts: tuple[BudgetAccount, ...] = ()
    budget_reservations: tuple[BudgetReservation, ...] = ()
    trigger_processes: tuple[TriggerProcess, ...] = ()
    pending_external_observations: tuple[ExternalObservation, ...] = ()
    execution_receipts: tuple[ExecutionReceipt, ...] = ()
    budget_settlements: tuple[BudgetSettlement, ...] = ()
    reconciliations: tuple[ActionReconciliation, ...] = ()
    completed_trigger_ids: tuple[str, ...] = ()
    npcs: tuple[NpcProjection, ...] = ()
    plans: tuple[PlanStateProjection, ...] = ()
    world_occurrences: tuple[WorldOccurrenceProjection, ...] = ()
    outcome_observations: tuple[OutcomeObservationProjection, ...] = ()
    experiences: tuple[ExperienceAuthorityProjection, ...] = ()
    experience_transitions: tuple[ExperienceTransitionProjection, ...] = ()
    outcome_proposals: tuple[OutcomeProposalProjection, ...] = ()
    appraisals: tuple[AppraisalProjection, ...] = ()
    affect_baselines: tuple[AffectBaselineProjection, ...] = ()
    affect_episodes: tuple[AffectEpisodeProjection, ...] = ()
    appraisal_proposals: tuple[AppraisalProposalProjection, ...] = ()
    appraisal_proposal_ids: tuple[str, ...] = ()
    affect_proposals: tuple[AffectProposalProjection, ...] = ()
    affect_proposal_ids: tuple[str, ...] = ()
    relationship_signals: tuple[RelationshipSignalProjection, ...] = ()
    relationship_adjustments: tuple[RelationshipAdjustmentProjection, ...] = ()
    relationship_states: tuple[RelationshipStateProjection, ...] = ()
    boundaries: tuple[BoundaryProjection, ...] = ()
    relationship_proposals: tuple[RelationshipProposalProjection, ...] = ()
    relationship_proposal_ids: tuple[str, ...] = ()
    private_impressions: tuple[PrivateImpressionProjection, ...] = ()
    private_impression_proposals: tuple[PrivateImpressionProposalProjection, ...] = ()
    private_impression_proposal_ids: tuple[str, ...] = ()
    threads: tuple[ThreadProjection, ...] = ()
    thread_transitions: tuple[ThreadTransitionProjection, ...] = ()
    thread_proposals: tuple[ThreadProposalProjection, ...] = ()
    thread_proposal_ids: tuple[str, ...] = ()
    commitments: tuple[CommitmentProjection, ...] = ()
    commitment_transitions: tuple[CommitmentTransitionProjection, ...] = ()
    commitment_proposals: tuple[CommitmentProposalProjection, ...] = ()
    commitment_proposal_ids: tuple[str, ...] = ()
    facts: tuple[FactProjection, ...] = ()
    fact_transitions: tuple[FactTransitionProjection, ...] = ()
    fact_proposals: tuple[FactProposalProjection, ...] = ()
    fact_proposal_ids: tuple[str, ...] = ()
    experience_proposals: tuple[ExperienceProposalProjection, ...] = ()
    experience_proposal_ids: tuple[str, ...] = ()
    memory_candidates: tuple[MemoryCandidateProjection, ...] = ()
    memory_candidate_transitions: tuple[MemoryCandidateTransitionProjection, ...] = ()
    memory_candidate_proposals: tuple[MemoryCandidateProposalProjection, ...] = ()
    memory_candidate_proposal_ids: tuple[str, ...] = ()
    character_core: CharacterCoreProjection | None = None
    character_core_transitions: tuple[CharacterCoreTransitionProjection, ...] = ()
    character_core_proposals: tuple[CharacterCoreProposalProjection, ...] = ()
    character_core_proposal_ids: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    proposal_revisions: tuple[ProposalRevisionRef, ...] = ()
    model_result_audits: tuple[ModelResultAuditProjection, ...] = ()
    proposal_audits: tuple[ProposalAuditProjection, ...] = ()
    acceptance_manifests_v2: tuple[AcceptanceManifestRefV2, ...] = ()
    fact_commit_proposal_audits_v2: tuple[FactCommitProposalAuditRefV2, ...] = ()
    acceptance_manifests_v3: tuple[AcceptanceManifestRefV3, ...] = ()
    minimal_reply_manifests: tuple[MinimalReplyManifestRef, ...] = ()
    expression_plan_manifests: tuple[ExpressionPlanManifestRef, ...] = ()
    stored_message_payloads: tuple[StoredMessagePayloadProjection, ...] = ()
    expression_payload_descriptors: tuple[ExpressionPayloadDescriptorProjection, ...] = ()
    life_content_descriptors: tuple[LifeContentDescriptorProjection, ...] = ()
    expression_plans: tuple[ExpressionPlanProjection, ...] = ()
    expression_beats: tuple[ExpressionBeatProjection, ...] = ()
    acceptance_decisions: tuple[AcceptanceDecisionRef, ...] = ()

    @model_validator(mode="after")
    def pending_index_matches_actions(self, info: ValidationInfo) -> ReducerState:
        expected = tuple(
            action for action in self.actions if action.state not in TERMINAL_ACTION_STATES
        )
        if self.pending_actions != expected:
            raise ValueError("pending_actions must equal the non-terminal action index")
        validate_actor_authority_event_bindings(
            self.actor_authorities,
            self.actor_authority_transitions,
            self.committed_world_event_refs,
            allow_legacy_missing=(info.context or {}).get("source_reducer_bundle")
            in _LEGACY_ACTOR_BINDING_BUNDLES,
        )
        validate_clock_history(
            self.clock_transition_history,
            current_logical_time=self.logical_time,
        )
        validate_v2_goal_authority_state(
            self.goals,
            self.goal_transitions,
            self.goal_proposals,
            self.goal_proposal_ids,
            global_proposal_ids=self.proposal_ids,
        )
        validate_v2_location_authority_state(
            self.locations,
            self.location_transitions,
            self.location_proposals,
            self.location_proposal_ids,
            global_proposal_ids=self.proposal_ids,
            actor_authority_transitions=self.actor_authority_transitions,
            committed_events=self.committed_world_event_refs,
            logical_time=self.logical_time,
        )
        validate_v2_resource_authority_state(
            self.resources,
            self.resource_transitions,
            self.resource_proposals,
            self.resource_proposal_ids,
            global_proposal_ids=self.proposal_ids,
            actor_authority_transitions=self.actor_authority_transitions,
            committed_events=self.committed_world_event_refs,
            logical_time=self.logical_time,
            require_operator_bindings=True,
        )
        validate_v2_attention_authority_state(
            self.attentions,
            self.attention_transitions,
            self.attention_proposals,
            self.attention_proposal_ids,
            global_proposal_ids=self.proposal_ids,
            actor_authority_transitions=self.actor_authority_transitions,
            committed_events=self.committed_world_event_refs,
        )
        validate_plan_authority_state(
            self.plans,
            self.committed_world_event_refs,
            logical_time=self.logical_time,
            allow_legacy_missing=(info.context or {}).get("source_reducer_bundle")
            in _LEGACY_ACTOR_BINDING_BUNDLES,
        )
        dimensions = tuple(item.dimension for item in self.affect_baselines)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("affect baseline dimensions must be unique")
        if len(self.relationship_states) > 1:
            raise ValueError("world v2.1 permits one primary relationship state")
        for subject_ref in {item.subject_ref for item in self.appearance_states}:
            history = tuple(item for item in self.appearance_states if item.subject_ref == subject_ref)
            if tuple(item.entity_revision for item in history) != tuple(range(1, len(history) + 1)):
                raise ValueError("appearance state revisions must be contiguous")
            if len({item.appearance_state_id for item in history}) != 1:
                raise ValueError("appearance state subject must have one authority identity")
            if any(current.valid_from <= previous.valid_from for previous, current in zip(history, history[1:])):
                raise ValueError("appearance state versions must move logical time forward")
            if any(
                previous.valid_until is None or previous.valid_until > current.valid_from
                for previous, current in zip(history, history[1:])
            ):
                raise ValueError("appearance state histories must not overlap")
        for subject_ref in {item.subject_ref for item in self.visible_physical_states}:
            history = tuple(
                item for item in self.visible_physical_states if item.subject_ref == subject_ref
            )
            if tuple(item.entity_revision for item in history) != tuple(range(1, len(history) + 1)):
                raise ValueError("visible physical state revisions must be contiguous")
            if len({item.physical_state_id for item in history}) != 1:
                raise ValueError("visible physical state subject must have one authority identity")
            if any(current.valid_from <= previous.valid_from for previous, current in zip(history, history[1:])):
                raise ValueError("visible physical state versions must move logical time forward")
            if any(previous.valid_until > current.valid_from for previous, current in zip(history, history[1:])):
                raise ValueError("visible physical state histories must not overlap")
        authority_ids = tuple(item.authority_id for item in self.actor_authorities)
        if len(authority_ids) != len(set(authority_ids)):
            raise ValueError("actor authority ids must be unique")
        active_principals = tuple(
            item.values.principal_ref
            for item in self.actor_authorities
            if item.values.status == "active"
        )
        if len(active_principals) != len(set(active_principals)):
            raise ValueError("active actor authority principals must be unique")
        active_credentials = tuple(
            item.values.credential_ref
            for item in self.actor_authorities
            if item.values.status == "active"
        )
        if len(active_credentials) != len(set(active_credentials)):
            raise ValueError("active actor authority credentials must be unique")
        transition_ids = tuple(item.transition_id for item in self.actor_authority_transitions)
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("actor authority transition ids must be unique")
        if len(self.consumed_actor_root_nonces) != len(set(self.consumed_actor_root_nonces)):
            raise ValueError("consumed actor root nonces must be unique")
        if len(self.consumed_actor_root_nonces) != len(self.actor_authority_transitions):
            raise ValueError("actor authority transitions must consume one root nonce")
        projected_ids = set(authority_ids)
        if any(item.authority_id not in projected_ids for item in self.actor_authority_transitions):
            raise ValueError("actor authority transition has no projected authority")
        for authority in self.actor_authorities:
            lineage = tuple(
                item
                for item in self.actor_authority_transitions
                if item.authority_id == authority.authority_id
            )
            if not lineage or lineage[0].operation != "bootstrap":
                raise ValueError("actor authority lineage must begin with bootstrap")
            if tuple(item.authority_revision for item in lineage) != tuple(
                range(1, len(lineage) + 1)
            ):
                raise ValueError("actor authority lineage revisions must be contiguous")
            if lineage[0].values_before is not None:
                raise ValueError("actor authority bootstrap lineage has prior values")
            if any(
                current.values_before != previous.values_after
                for previous, current in zip(lineage, lineage[1:])
            ):
                raise ValueError("actor authority lineage before values are discontinuous")
            latest = lineage[-1]
            if (
                authority.entity_revision != latest.authority_revision
                or authority.values != latest.values_after
                or authority.origin.transition_id != latest.transition_id
            ):
                raise ValueError("actor authority projection does not match lineage head")
        authorization_transitions = (
            *self.capability_transitions,
            *self.consent_transitions,
            *self.privacy_transitions,
        )
        if (
            len(self.consumed_authorization_root_nonces) != len(authorization_transitions)
            or len(self.consumed_authorization_challenge_ids) != len(authorization_transitions)
            or len(self.consumed_authorization_source_ids) != len(authorization_transitions)
        ):
            raise ValueError(
                "authorization transitions require one root nonce and evidence identity"
            )
        if (
            len(self.consumed_authorization_root_nonces)
            != len(set(self.consumed_authorization_root_nonces))
            or len(self.consumed_authorization_challenge_ids)
            != len(set(self.consumed_authorization_challenge_ids))
            or len(self.consumed_authorization_source_ids)
            != len(set(self.consumed_authorization_source_ids))
        ):
            raise ValueError("authorization nonce and evidence identities must be unique")
        transition_ids = tuple(item.transition_id for item in authorization_transitions)
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("authorization transition ids must be unique")
        for projections, transitions, id_field, create_operation in (
            (
                self.capability_grants,
                self.capability_transitions,
                "grant_id",
                "grant",
            ),
            (self.consent_grants, self.consent_transitions, "consent_id", "grant"),
            (self.privacy_policies, self.privacy_transitions, "policy_id", "revise"),
        ):
            entity_ids = tuple(getattr(item, id_field) for item in projections)
            if len(entity_ids) != len(set(entity_ids)):
                raise ValueError("authorization projection ids must be unique")
            if any(getattr(item, id_field) not in set(entity_ids) for item in transitions):
                raise ValueError("authorization transition has no projection")
            for projection in projections:
                entity_id = getattr(projection, id_field)
                lineage = tuple(
                    item for item in transitions if getattr(item, id_field) == entity_id
                )
                if not lineage or lineage[0].operation != create_operation:
                    raise ValueError("authorization lineage has invalid origin")
                if tuple(item.entity_revision for item in lineage) != tuple(
                    range(1, len(lineage) + 1)
                ):
                    raise ValueError("authorization lineage revisions must be contiguous")
                if any(
                    current.values_before != previous.values_after
                    for previous, current in zip(lineage, lineage[1:])
                ):
                    raise ValueError("authorization lineage values are discontinuous")
                latest = lineage[-1]
                if (
                    projection.entity_revision != latest.entity_revision
                    or projection.values != latest.values_after
                    or projection.origin.transition_id != latest.transition_id
                ):
                    raise ValueError("authorization projection does not match lineage head")
        thread_ids = tuple(item.thread_id for item in self.threads)
        if len(thread_ids) != len(set(thread_ids)):
            raise ValueError("thread ids must be unique")
        thread_transition_ids = tuple(item.transition_id for item in self.thread_transitions)
        if len(thread_transition_ids) != len(set(thread_transition_ids)):
            raise ValueError("thread transition ids must be unique")
        if any(item.thread_id not in set(thread_ids) for item in self.thread_transitions):
            raise ValueError("thread transition has no projected thread")
        authority_transition_ids = tuple(
            item.transition_id
            for item in (
                *self.actor_authority_transitions,
                *self.capability_transitions,
                *self.consent_transitions,
                *self.privacy_transitions,
                *self.thread_transitions,
            )
        )
        if len(authority_transition_ids) != len(set(authority_transition_ids)):
            raise ValueError("authority transition ids must be globally unique")
        if len(self.thread_proposal_ids) != len(set(self.thread_proposal_ids)):
            raise ValueError("thread proposal ids must be unique")
        if any(
            item.proposal_id not in set(self.thread_proposal_ids) for item in self.thread_proposals
        ):
            raise ValueError("pending thread proposal is absent from its durable index")
        active_fingerprints = tuple(
            item.semantic_fingerprint for item in self.threads if item.values.status == "open"
        )
        if len(active_fingerprints) != len(set(active_fingerprints)):
            raise ValueError("active thread semantic fingerprints must be unique")
        for thread in self.threads:
            lineage = tuple(
                item for item in self.thread_transitions if item.thread_id == thread.thread_id
            )
            if not lineage or lineage[0].operation != "open":
                raise ValueError("thread lineage must begin with open")
            if tuple(item.entity_revision for item in lineage) != tuple(range(1, len(lineage) + 1)):
                raise ValueError("thread lineage revisions must be contiguous")
            if lineage[0].values_before is not None or any(
                current.values_before != previous.values_after
                for previous, current in zip(lineage, lineage[1:])
            ):
                raise ValueError("thread lineage before values are discontinuous")
            latest = lineage[-1]
            if (
                thread.entity_revision != latest.entity_revision
                or thread.values != latest.values_after
                or thread.origin.transition_id != latest.transition_id
            ):
                raise ValueError("thread projection does not match lineage head")
        commitment_ids = tuple(item.commitment_id for item in self.commitments)
        if len(commitment_ids) != len(set(commitment_ids)):
            raise ValueError("commitment ids must be unique")
        transition_ids = tuple(item.transition_id for item in self.commitment_transitions)
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("commitment transition ids must be unique")
        if any(
            item.commitment_id not in set(commitment_ids) for item in self.commitment_transitions
        ):
            raise ValueError("commitment transition has no projected commitment")
        if len(self.commitment_proposal_ids) != len(set(self.commitment_proposal_ids)):
            raise ValueError("commitment proposal ids must be unique")
        active = tuple(
            item.semantic_fingerprint
            for item in self.commitments
            if item.values.status in {"open", "due"}
        )
        if len(active) != len(set(active)):
            raise ValueError("active commitment semantic fingerprints must be unique")
        for commitment in self.commitments:
            lineage = tuple(
                item
                for item in self.commitment_transitions
                if item.commitment_id == commitment.commitment_id
            )
            if not lineage or lineage[0].operation != "open":
                raise ValueError("commitment lineage must begin with open")
            if tuple(item.entity_revision for item in lineage) != tuple(range(1, len(lineage) + 1)):
                raise ValueError("commitment lineage revisions must be contiguous")
            if lineage[0].values_before is not None or any(
                current.values_before != previous.values_after
                for previous, current in zip(lineage, lineage[1:])
            ):
                raise ValueError("commitment lineage values are discontinuous")
            latest = lineage[-1]
            if (
                commitment.entity_revision != latest.entity_revision
                or commitment.values != latest.values_after
                or commitment.origin.transition_id != latest.transition_id
            ):
                raise ValueError("commitment projection does not match lineage head")
            predecessor_ref = commitment.values.predecessor_commitment_ref
            if predecessor_ref is not None and predecessor_ref not in set(commitment_ids):
                raise ValueError("commitment predecessor is absent from authority")
            visited: set[str] = set()
            cursor = commitment
            while cursor.values.predecessor_commitment_ref is not None:
                if cursor.commitment_id in visited:
                    raise ValueError("commitment predecessor cycle is forbidden")
                visited.add(cursor.commitment_id)
                next_item = next(
                    (
                        item
                        for item in self.commitments
                        if item.commitment_id == cursor.values.predecessor_commitment_ref
                    ),
                    None,
                )
                if next_item is None:
                    break
                cursor = next_item
        fact_ids = tuple(item.fact_id for item in self.facts)
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact ids must be unique")
        fact_transition_ids = tuple(item.transition_id for item in self.fact_transitions)
        if len(fact_transition_ids) != len(set(fact_transition_ids)):
            raise ValueError("fact transition ids must be unique")
        if any(item.fact_id not in set(fact_ids) for item in self.fact_transitions):
            raise ValueError("fact transition has no projected fact")
        if len(self.fact_proposal_ids) != len(set(self.fact_proposal_ids)):
            raise ValueError("fact proposal ids must be unique")
        if any(item.proposal_id not in set(self.fact_proposal_ids) for item in self.fact_proposals):
            raise ValueError("pending fact proposal is absent from its durable index")
        active_content = tuple(
            (
                item.values.conflict_key,
                item.values.cardinality,
                item.values.value_hash,
            )
            for item in self.facts
            if item.values.status == "active"
        )
        if len(active_content) != len(set(active_content)):
            raise ValueError("active fact content identities must be unique")
        cardinalities: dict[str, str] = {}
        for transition in self.fact_transitions:
            slot = transition.values_after.conflict_key
            prior = cardinalities.setdefault(slot, transition.values_after.cardinality)
            if prior != transition.values_after.cardinality:
                raise ValueError("fact slot cardinality cannot change across history")
        for fact in self.facts:
            lineage = tuple(item for item in self.fact_transitions if item.fact_id == fact.fact_id)
            if not lineage or lineage[0].operation != "commit":
                raise ValueError("fact lineage must begin with commit")
            if tuple(item.entity_revision for item in lineage) != tuple(range(1, len(lineage) + 1)):
                raise ValueError("fact lineage revisions must be contiguous")
            if lineage[0].values_before is not None or any(
                current.values_before != previous.values_after
                for previous, current in zip(lineage, lineage[1:])
            ):
                raise ValueError("fact lineage before values are discontinuous")
            compensated_targets: set[str] = set()
            for index, transition in enumerate(lineage):
                target_id = transition.compensates_transition_id
                if transition.operation != "compensate":
                    continue
                target = next(
                    (
                        candidate
                        for candidate in lineage[:index]
                        if candidate.transition_id == target_id
                    ),
                    None,
                )
                if target is None or target.operation != "correct":
                    raise ValueError("fact compensation target must be an earlier correction")
                if target.transition_id in compensated_targets:
                    raise ValueError("fact correction cannot be compensated twice")
                compensated_targets.add(target.transition_id)
            latest = lineage[-1]
            if (
                fact.entity_revision != latest.entity_revision
                or fact.values != latest.values_after
                or fact.origin.transition_id != latest.transition_id
                or fact.semantic_fingerprint != latest.semantic_fingerprint_after
            ):
                raise ValueError("fact projection does not match lineage head")
        experience_ids = tuple(item.experience_id for item in self.experiences)
        if len(experience_ids) != len(set(experience_ids)):
            raise ValueError("experience ids must be unique")
        transition_ids = tuple(item.transition_id for item in self.experience_transitions)
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("experience transition ids must be unique")
        hardened_ids = {
            item.experience_id
            for item in self.experiences
            if isinstance(item, ExperienceProjection)
        }
        if any(item.experience_id not in hardened_ids for item in self.experience_transitions):
            raise ValueError("experience transition has no hardened projection")
        for experience in self.experiences:
            transitions = tuple(
                item
                for item in self.experience_transitions
                if item.experience_id == experience.experience_id
            )
            if isinstance(experience, LegacyExperienceProjection):
                if transitions:
                    raise ValueError("legacy experience cannot gain fabricated lineage")
                continue
            if len(transitions) != 1:
                raise ValueError("immutable experience requires exactly one commit transition")
            transition = transitions[0]
            if (
                transition.transition_id != experience.origin.transition_id
                or transition.values_after != experience.values
                or transition.semantic_fingerprint_after != experience.semantic_fingerprint
                or transition.accepted_event_ref != experience.origin.accepted_event_ref
            ):
                raise ValueError("experience projection does not match commit lineage")
        if len(self.experience_proposal_ids) != len(set(self.experience_proposal_ids)):
            raise ValueError("experience proposal ids must be unique")
        if any(
            item.proposal_id not in set(self.experience_proposal_ids)
            for item in self.experience_proposals
        ):
            raise ValueError("pending experience proposal is absent from durable index")
        candidate_ids = tuple(item.candidate_id for item in self.memory_candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("memory candidate ids must be unique")
        memory_transition_ids = tuple(
            item.transition_id for item in self.memory_candidate_transitions
        )
        if len(memory_transition_ids) != len(set(memory_transition_ids)):
            raise ValueError("memory candidate transition ids must be unique")
        if any(
            item.candidate_id not in set(candidate_ids)
            for item in self.memory_candidate_transitions
        ):
            raise ValueError("memory candidate transition has no projected head")
        occupied_clusters: set[str] = set()
        for candidate in self.memory_candidates:
            if occupied_clusters & set(candidate.source_cluster_lineage):
                raise ValueError("memory source cluster lineage has multiple owners")
            occupied_clusters.update(candidate.source_cluster_lineage)
            lineage = tuple(
                item
                for item in self.memory_candidate_transitions
                if item.candidate_id == candidate.candidate_id
            )
            if not lineage or lineage[0].operation != "open":
                raise ValueError("memory candidate lineage must begin with open")
            if tuple(item.entity_revision for item in lineage) != tuple(range(1, len(lineage) + 1)):
                raise ValueError("memory candidate lineage revisions must be contiguous")
            if lineage[0].values_before is not None or any(
                current.values_before != previous.values_after
                for previous, current in zip(lineage, lineage[1:])
            ):
                raise ValueError("memory candidate lineage values are discontinuous")
            latest = lineage[-1]
            if (
                candidate.entity_revision != latest.entity_revision
                or candidate.values != latest.values_after
                or candidate.origin.transition_id != latest.transition_id
                or candidate.origin.accepted_event_ref != latest.accepted_event_ref
            ):
                raise ValueError("memory candidate projection does not match lineage head")
        if len(self.memory_candidate_proposal_ids) != len(set(self.memory_candidate_proposal_ids)):
            raise ValueError("memory candidate proposal ids must be unique")
        if any(
            item.proposal_id not in set(self.memory_candidate_proposal_ids)
            for item in self.memory_candidate_proposals
        ):
            raise ValueError("pending memory proposal is absent from durable index")
        core_transition_ids = tuple(item.transition_id for item in self.character_core_transitions)
        if len(core_transition_ids) != len(set(core_transition_ids)):
            raise ValueError("character core transition ids must be unique")
        if self.character_core is None:
            if self.character_core_transitions:
                raise ValueError("character core transition has no projected head")
        else:
            lineage = self.character_core_transitions
            if not lineage or lineage[0].operation != "initialize":
                raise ValueError("character core lineage must begin with initialize")
            if any(item.core_id != self.character_core.core_id for item in lineage):
                raise ValueError("character core lineage belongs to another core")
            if tuple(item.entity_revision for item in lineage) != tuple(range(1, len(lineage) + 1)):
                raise ValueError("character core lineage revisions must be contiguous")
            if lineage[0].values_before is not None or any(
                current.values_before != previous.values_after
                for previous, current in zip(lineage, lineage[1:])
            ):
                raise ValueError("character core lineage values are discontinuous")
            latest = lineage[-1]
            if (
                self.character_core.entity_revision != latest.entity_revision
                or self.character_core.values != latest.values_after
                or self.character_core.origin.transition_id != latest.transition_id
                or self.character_core.origin.accepted_event_ref != latest.accepted_event_ref
            ):
                raise ValueError("character core projection does not match lineage head")
        if len(self.character_core_proposal_ids) != len(set(self.character_core_proposal_ids)):
            raise ValueError("character core proposal ids must be unique")
        if any(
            item.proposal_id not in set(self.character_core_proposal_ids)
            for item in self.character_core_proposals
        ):
            raise ValueError("pending character core proposal is absent from durable index")
        return self

    def semantic_payload(
        self,
        *,
        world_id: str,
        world_revision: int,
        reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
    ) -> dict[str, Any]:
        # .25 adds executable provider-media grants.  The earlier expression
        # manifest layouts remain byte-for-byte stable when reconstructing old
        # heads.
        declared_reducer_bundle_version = reducer_bundle_version
        if reducer_bundle_version in {"world-v2-reducers.22", "world-v2-reducers.23"}:
            reducer_bundle_version = "world-v2-reducers.24"
        payload = {
            "reducer_bundle_version": declared_reducer_bundle_version,
            "schema_version": "world-v2.1",
            "world_id": world_id,
            "world_revision": world_revision,
            "actor_authorities": tuple(
                item.model_dump(mode="json") for item in self.actor_authorities
            ),
            "actor_authority_transitions": tuple(
                _actor_authority_transition_semantic_dump(
                    item, reducer_bundle_version=reducer_bundle_version
                )
                for item in self.actor_authority_transitions
            ),
            "consumed_actor_root_nonces": self.consumed_actor_root_nonces,
            "capability_grants": tuple(
                item.model_dump(mode="json") for item in self.capability_grants
            ),
            "capability_transitions": tuple(
                item.model_dump(mode="json") for item in self.capability_transitions
            ),
            "consent_grants": tuple(item.model_dump(mode="json") for item in self.consent_grants),
            "consent_transitions": tuple(
                item.model_dump(mode="json") for item in self.consent_transitions
            ),
            "privacy_policies": tuple(
                item.model_dump(mode="json") for item in self.privacy_policies
            ),
            "privacy_transitions": tuple(
                item.model_dump(mode="json") for item in self.privacy_transitions
            ),
            "consumed_authorization_root_nonces": self.consumed_authorization_root_nonces,
            "consumed_authorization_challenge_ids": self.consumed_authorization_challenge_ids,
            "consumed_authorization_source_ids": self.consumed_authorization_source_ids,
            "observation_refs": self.observation_refs,
            "message_observations": tuple(
                (
                    item.model_dump(mode="json")
                    if reducer_bundle_version
                    in {
                        "world-v2-reducers.12",
                        "world-v2-reducers.13",
                        "world-v2-reducers.14",
                        "world-v2-reducers.15",
                        "world-v2-reducers.16",
                        "world-v2-reducers.17",
                        "world-v2-reducers.18",
                        "world-v2-reducers.19",
                        "world-v2-reducers.20",
                        "world-v2-reducers.21",
                        "world-v2-reducers.22",
                        "world-v2-reducers.23",
                        "world-v2-reducers.24",
                        REDUCER_BUNDLE_VERSION,
                    }
                    else item.model_dump(mode="json", exclude={"actor", "channel", "payload_ref"})
                )
                for item in self.message_observations
            ),
            "operator_observations": tuple(
                item.model_dump(mode="json") for item in self.operator_observations
            ),
            "committed_world_event_refs": tuple(
                ref.model_dump(mode="json") for ref in self.committed_world_event_refs
            ),
            "logical_time": self.logical_time.isoformat() if self.logical_time else None,
            **(
                {"appearance_states": tuple(item.model_dump(mode="json") for item in self.appearance_states)}
                if self.appearance_states
                else {}
            ),
            **(
                {
                    "visible_physical_states": tuple(
                        item.model_dump(mode="json") for item in self.visible_physical_states
                    )
                }
                if self.visible_physical_states
                else {}
            ),
            "actions": tuple(
                _action_semantic_dump(action, reducer_bundle_version=reducer_bundle_version)
                for action in self.actions
            ),
            "pending_actions": tuple(
                _action_semantic_dump(action, reducer_bundle_version=reducer_bundle_version)
                for action in self.pending_actions
            ),
            "budget_reservations": tuple(
                reservation.model_dump(mode="json") for reservation in self.budget_reservations
            ),
            "budget_accounts": tuple(
                account.model_dump(mode="json") for account in self.budget_accounts
            ),
            "execution_receipts": tuple(
                receipt.model_dump(mode="json") for receipt in self.execution_receipts
            ),
            "budget_settlements": tuple(
                settlement.model_dump(mode="json") for settlement in self.budget_settlements
            ),
            "reconciliations": tuple(
                reconciliation.model_dump(mode="json") for reconciliation in self.reconciliations
            ),
            "npcs": tuple(npc.model_dump(mode="json") for npc in self.npcs),
            "plans": tuple(
                (
                    plan.model_dump(mode="json")
                    if reducer_bundle_version
                    in {
                        "world-v2-reducers.16",
                        "world-v2-reducers.17",
                        "world-v2-reducers.18",
                        "world-v2-reducers.19",
                        "world-v2-reducers.20",
                        "world-v2-reducers.21",
                        REDUCER_BUNDLE_VERSION,
                    }
                    else plan.model_dump(
                        mode="json", exclude={"owner_actor_ref", "authority_origin"}
                    )
                )
                for plan in self.plans
            ),
            "world_occurrences": tuple(
                (
                    occurrence.model_dump(mode="json")
                    if reducer_bundle_version in {"world-v2-reducers.21", REDUCER_BUNDLE_VERSION}
                    else occurrence.model_dump(mode="json", exclude={"candidate_outcomes"})
                    if reducer_bundle_version
                    in {
                        "world-v2-reducers.16",
                        "world-v2-reducers.17",
                        "world-v2-reducers.18",
                        "world-v2-reducers.19",
                        "world-v2-reducers.20",
                    }
                    else occurrence.model_dump(
                        mode="json", exclude={"settled_outcome_ref", "candidate_outcomes"}
                    )
                    if reducer_bundle_version
                    in {
                        "world-v2-reducers.13",
                        "world-v2-reducers.14",
                        "world-v2-reducers.15",
                    }
                    else occurrence.model_dump(
                        mode="json",
                        exclude={
                            "settled_outcome_ref",
                            "settlement_event_ref",
                            "settlement_world_revision",
                            "settlement_payload_hash",
                            "candidate_outcomes",
                        },
                    )
                )
                for occurrence in self.world_occurrences
            ),
            "outcome_observations": tuple(
                observation.model_dump(mode="json") for observation in self.outcome_observations
            ),
            "experiences": tuple(
                _experience_semantic_dump(experience, reducer_bundle_version=reducer_bundle_version)
                for experience in self.experiences
            ),
            "appraisals": tuple(appraisal.model_dump(mode="json") for appraisal in self.appraisals),
            "affect_baselines": tuple(
                baseline.model_dump(mode="json") for baseline in self.affect_baselines
            ),
            "affect_episodes": tuple(
                episode.model_dump(mode="json") for episode in self.affect_episodes
            ),
            "relationship_signals": tuple(
                item.model_dump(mode="json") for item in self.relationship_signals
            ),
            "relationship_adjustments": tuple(
                item.model_dump(mode="json") for item in self.relationship_adjustments
            ),
            "relationship_states": tuple(
                item.model_dump(mode="json") for item in self.relationship_states
            ),
            "boundaries": tuple(item.model_dump(mode="json") for item in self.boundaries),
        }
        if declared_reducer_bundle_version in {
            "world-v2-reducers.26",
            "world-v2-reducers.27",
            "world-v2-reducers.28",
            "world-v2-reducers.29",
            "world-v2-reducers.30",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["provider_media_grants"] = tuple(
                item.model_dump(mode="json") for item in self.provider_media_grants
            )
            payload["photo_candidates"] = tuple(
                item.model_dump(mode="json") for item in self.photo_candidates
            )
            payload["media_opportunities"] = tuple(
                item.model_dump(mode="json") for item in self.media_opportunities
            )
            payload["media_plans"] = tuple(
                item.model_dump(mode="json") for item in self.media_plans
            )
            payload["media_unrenderable_opportunity_ids"] = self.media_unrenderable_opportunity_ids
        if declared_reducer_bundle_version in {
            "world-v2-reducers.27",
            "world-v2-reducers.28",
            "world-v2-reducers.29",
            "world-v2-reducers.30",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["media_artifacts"] = tuple(
                item.model_dump(mode="json") for item in self.media_artifacts
            )
            payload["media_inspections"] = tuple(
                item.model_dump(mode="json")
                if declared_reducer_bundle_version == REDUCER_BUNDLE_VERSION
                else item.model_dump(mode="json", exclude={"repairable", "repair_scope"})
                for item in self.media_inspections
            )
            payload["media_previews"] = tuple(
                item.model_dump(mode="json") for item in self.media_previews
            )
            payload["media_failed_plan_ids"] = self.media_failed_plan_ids
        if declared_reducer_bundle_version in {"world-v2-reducers.31", REDUCER_BUNDLE_VERSION}:
            payload["media_delivery_approvals"] = tuple(
                item.model_dump(mode="json") for item in self.media_delivery_approvals
            )
            payload["media_deliveries"] = tuple(
                item.model_dump(mode="json") for item in self.media_deliveries
            )
            payload["interaction_bids"] = tuple(
                item.model_dump(mode="json") for item in self.interaction_bids
            )
            if declared_reducer_bundle_version == REDUCER_BUNDLE_VERSION:
                payload["media_thread_proposals"] = tuple(
                    item.model_dump(mode="json") for item in self.media_thread_proposals
                )
        if reducer_bundle_version in {
            "world-v2-reducers.10",
            "world-v2-reducers.11",
            "world-v2-reducers.12",
            "world-v2-reducers.13",
            "world-v2-reducers.14",
            "world-v2-reducers.15",
            "world-v2-reducers.16",
            "world-v2-reducers.17",
            "world-v2-reducers.18",
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["threads"] = tuple(item.model_dump(mode="json") for item in self.threads)
            payload["thread_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.thread_transitions
            )
        if reducer_bundle_version in {
            "world-v2-reducers.11",
            "world-v2-reducers.12",
            "world-v2-reducers.13",
            "world-v2-reducers.14",
            "world-v2-reducers.15",
            "world-v2-reducers.16",
            "world-v2-reducers.17",
            "world-v2-reducers.18",
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["commitments"] = tuple(
                item.model_dump(mode="json") for item in self.commitments
            )
            payload["commitment_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.commitment_transitions
            )
        if reducer_bundle_version in {
            "world-v2-reducers.12",
            "world-v2-reducers.13",
            "world-v2-reducers.14",
            "world-v2-reducers.15",
            "world-v2-reducers.16",
            "world-v2-reducers.17",
            "world-v2-reducers.18",
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["facts"] = tuple(item.model_dump(mode="json") for item in self.facts)
            payload["fact_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.fact_transitions
            )
        if reducer_bundle_version in {
            "world-v2-reducers.13",
            "world-v2-reducers.14",
            "world-v2-reducers.15",
            "world-v2-reducers.16",
            "world-v2-reducers.17",
            "world-v2-reducers.18",
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["experience_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.experience_transitions
            )
        if reducer_bundle_version in {
            "world-v2-reducers.14",
            "world-v2-reducers.15",
            "world-v2-reducers.16",
            "world-v2-reducers.17",
            "world-v2-reducers.18",
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["memory_candidates"] = tuple(
                item.model_dump(mode="json") for item in self.memory_candidates
            )
            payload["memory_candidate_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.memory_candidate_transitions
            )
        if reducer_bundle_version in {
            "world-v2-reducers.15",
            "world-v2-reducers.16",
            "world-v2-reducers.17",
            "world-v2-reducers.18",
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["character_core"] = (
                self.character_core.model_dump(mode="json")
                if self.character_core is not None
                else None
            )
            payload["character_core_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.character_core_transitions
            )
        if reducer_bundle_version in {
            "world-v2-reducers.16",
            "world-v2-reducers.17",
            "world-v2-reducers.18",
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["clock_transition_history"] = tuple(
                item.model_dump(mode="json") for item in self.clock_transition_history
            )
            payload["goals"] = tuple(item.model_dump(mode="json") for item in self.goals)
            payload["goal_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.goal_transitions
            )
            payload["locations"] = tuple(item.model_dump(mode="json") for item in self.locations)
            payload["resources"] = tuple(item.model_dump(mode="json") for item in self.resources)
            payload["resource_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.resource_transitions
            )
            payload["location_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.location_transitions
            )
            payload["attentions"] = tuple(item.model_dump(mode="json") for item in self.attentions)
            payload["attention_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.attention_transitions
            )
        if reducer_bundle_version in {
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["fact_commit_proposal_audits_v2"] = tuple(
                item.model_dump(mode="json") for item in self.fact_commit_proposal_audits_v2
            )
            payload["acceptance_manifests_v3"] = tuple(
                item.model_dump(mode="json") for item in self.acceptance_manifests_v3
            )
        if reducer_bundle_version in {
            "world-v2-reducers.21",
            "world-v2-reducers.22",
            "world-v2-reducers.23",
            "world-v2-reducers.24",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["life_content_descriptors"] = tuple(
                item.model_dump(mode="json") for item in self.life_content_descriptors
            )
            payload["minimal_reply_manifests"] = tuple(
                item.model_dump(mode="json") for item in self.minimal_reply_manifests
            )
            if declared_reducer_bundle_version in {"world-v2-reducers.24", REDUCER_BUNDLE_VERSION}:
                payload["expression_plan_manifests"] = tuple(
                    _expression_plan_manifest_semantic_dump(item)
                    for item in self.expression_plan_manifests
                )
            payload["stored_message_payloads"] = tuple(
                item.model_dump(mode="json") for item in self.stored_message_payloads
            )
            if self.expression_payload_descriptors:
                payload["expression_payload_descriptors"] = tuple(
                    item.model_dump(mode="json") for item in self.expression_payload_descriptors
                )
            payload["expression_plans"] = tuple(
                _expression_plan_semantic_dump(item, reducer_bundle_version=reducer_bundle_version)
                for item in self.expression_plans
            )
            payload["expression_beats"] = tuple(
                _expression_beat_semantic_dump(item, reducer_bundle_version=reducer_bundle_version)
                for item in self.expression_beats
            )
        return payload


Reducer = Callable[[ReducerState, WorldEvent], ReducerState]


@dataclass(frozen=True, slots=True)
class EventDefinition:
    event_type: str
    revision_class: RevisionClass
    reducer: Reducer


class _LegacyAppraisalProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        raise NotImplementedError("legacy appraisal record dispatch is not registry-routed")

    def find(self, state: ReducerState, proposal_id: str) -> AppraisalProposalProjection | None:
        return next(
            (item for item in state.appraisal_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "appraisal_proposals": tuple(
                    item for item in state.appraisal_proposals if item.proposal_id != proposal_id
                )
            }
        )


class _LegacyAffectProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        raise NotImplementedError("legacy affect record dispatch is not registry-routed")

    def find(self, state: ReducerState, proposal_id: str) -> AffectProposalProjection | None:
        return next(
            (item for item in state.affect_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "affect_proposals": tuple(
                    item for item in state.affect_proposals if item.proposal_id != proposal_id
                )
            }
        )


class _LegacyOutcomeProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        raise NotImplementedError("legacy outcome record dispatch is not registry-routed")

    def find(self, state: ReducerState, proposal_id: str) -> OutcomeProposalProjection | None:
        return next(
            (item for item in state.outcome_proposals if item.outcome_proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        # Outcome proposals are a durable deliberation audit used to explain a
        # later settlement or rejection; deciding one does not erase that audit.
        return state


class _InteractionBidProposalStore:
    """Dedicated records are reduced by their event handler, never ProposalRecorded."""

    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        raise NotImplementedError("interaction bid record dispatch is not registry-routed")

    def find(
        self, state: ReducerState, proposal_id: str
    ) -> InteractionBidProposalProjection | None:
        return next(
            (
                item
                for item in state.interaction_bid_proposals
                if item.interaction_bid_proposal_id == proposal_id
            ),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state


class _RelationshipProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, RelationshipProposalProjection
        ):
            raise TypeError("relationship proposal adapter received incompatible values")
        return _relationship_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> RelationshipProposalProjection | None:
        return next(
            (item for item in state.relationship_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "relationship_proposals": tuple(
                    item for item in state.relationship_proposals if item.proposal_id != proposal_id
                )
            }
        )


class _PrivateImpressionProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, PrivateImpressionProposalProjection
        ):
            raise TypeError("private impression proposal adapter received incompatible values")
        return _private_impression_proposal_recorded(state, event, proposal=proposal)

    def find(
        self, state: ReducerState, proposal_id: str
    ) -> PrivateImpressionProposalProjection | None:
        return next(
            (
                item
                for item in state.private_impression_proposals
                if item.proposal_id == proposal_id
            ),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "private_impression_proposals": tuple(
                    item
                    for item in state.private_impression_proposals
                    if item.proposal_id != proposal_id
                )
            }
        )


class _ThreadProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(proposal, ThreadProposalProjection):
            raise TypeError("thread proposal adapter received incompatible values")
        return _thread_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> ThreadProposalProjection | None:
        return next(
            (item for item in state.thread_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "thread_proposals": tuple(
                    item for item in state.thread_proposals if item.proposal_id != proposal_id
                )
            }
        )


class _CommitmentProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, CommitmentProposalProjection
        ):
            raise TypeError("commitment proposal adapter received incompatible values")
        return _commitment_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> CommitmentProposalProjection | None:
        return next(
            (item for item in state.commitment_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "commitment_proposals": tuple(
                    item for item in state.commitment_proposals if item.proposal_id != proposal_id
                )
            }
        )


class _FactProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(proposal, FactProposalProjection):
            raise TypeError("fact proposal adapter received incompatible values")
        return _fact_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> FactProposalProjection | None:
        return next(
            (item for item in state.fact_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "fact_proposals": tuple(
                    item for item in state.fact_proposals if item.proposal_id != proposal_id
                )
            }
        )


class _ExperienceProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, ExperienceProposalProjection
        ):
            raise TypeError("experience proposal adapter received incompatible values")
        return _experience_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> ExperienceProposalProjection | None:
        return next(
            (item for item in state.experience_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "experience_proposals": tuple(
                    item for item in state.experience_proposals if item.proposal_id != proposal_id
                )
            }
        )


class _MemoryCandidateProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, MemoryCandidateProposalProjection
        ):
            raise TypeError("memory proposal adapter received incompatible values")
        return _memory_candidate_proposal_recorded(state, event, proposal=proposal)

    def find(
        self, state: ReducerState, proposal_id: str
    ) -> MemoryCandidateProposalProjection | None:
        return next(
            (item for item in state.memory_candidate_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "memory_candidate_proposals": tuple(
                    item
                    for item in state.memory_candidate_proposals
                    if item.proposal_id != proposal_id
                )
            }
        )


class _CharacterCoreProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, CharacterCoreProposalProjection
        ):
            raise TypeError("character core proposal adapter received incompatible values")
        return _character_core_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> CharacterCoreProposalProjection | None:
        return next(
            (item for item in state.character_core_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "character_core_proposals": tuple(
                    item
                    for item in state.character_core_proposals
                    if item.proposal_id != proposal_id
                )
            }
        )


class _V2GoalProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(proposal, V2GoalProposalProjection):
            raise TypeError("Goal proposal adapter received incompatible values")
        return _v2_goal_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> V2GoalProposalProjection | None:
        return next(
            (item for item in state.goal_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        remaining = tuple(item for item in state.goal_proposals if item.proposal_id != proposal_id)
        return state.model_copy(
            update={
                "goal_proposals": remaining,
                "goal_proposal_ids": tuple(item.proposal_id for item in remaining),
            }
        )


class _V2LocationProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, V2LocationProposalProjection
        ):
            raise TypeError("Location proposal adapter received incompatible values")
        return _v2_location_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> V2LocationProposalProjection | None:
        return next(
            (item for item in state.location_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        remaining = tuple(
            item for item in state.location_proposals if item.proposal_id != proposal_id
        )
        return state.model_copy(
            update={
                "location_proposals": remaining,
                "location_proposal_ids": tuple(item.proposal_id for item in remaining),
            }
        )


class _V2ResourceProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, V2ResourceProposalProjection
        ):
            raise TypeError("Resource proposal adapter received incompatible values")
        return _v2_resource_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> V2ResourceProposalProjection | None:
        return next(
            (item for item in state.resource_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        remaining = tuple(
            item for item in state.resource_proposals if item.proposal_id != proposal_id
        )
        return state.model_copy(
            update={
                "resource_proposals": remaining,
                "resource_proposal_ids": tuple(item.proposal_id for item in remaining),
            }
        )


class _V2AttentionProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, V2AttentionProposalProjection
        ):
            raise TypeError("Attention proposal adapter received incompatible values")
        return _v2_attention_proposal_recorded(state, event, proposal=proposal)

    def find(self, state: ReducerState, proposal_id: str) -> V2AttentionProposalProjection | None:
        return next(
            (item for item in state.attention_proposals if item.proposal_id == proposal_id),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        remaining = tuple(
            item for item in state.attention_proposals if item.proposal_id != proposal_id
        )
        return state.model_copy(
            update={
                "attention_proposals": remaining,
                "attention_proposal_ids": tuple(item.proposal_id for item in remaining),
            }
        )


_TYPED_PROPOSAL_STORES = {
    "proposal-contract:appraisal-legacy.1": _LegacyAppraisalProposalStore(),
    "proposal-contract:affect-legacy.1": _LegacyAffectProposalStore(),
    "proposal-contract:outcome-legacy.1": _LegacyOutcomeProposalStore(),
    "proposal-contract:interaction-bid.1": _InteractionBidProposalStore(),
    "proposal-contract:relationship.1": _RelationshipProposalStore(),
    "proposal-contract:private-impression.1": _PrivateImpressionProposalStore(),
    "proposal-contract:thread.1": _ThreadProposalStore(),
    "proposal-contract:commitment.1": _CommitmentProposalStore(),
    "proposal-contract:fact.1": _FactProposalStore(),
    "proposal-contract:experience.1": _ExperienceProposalStore(),
    "proposal-contract:memory-candidate.1": _MemoryCandidateProposalStore(),
    "proposal-contract:character-core.1": _CharacterCoreProposalStore(),
    "proposal-contract:v2-goal.1": _V2GoalProposalStore(),
    "proposal-contract:v2-location.1": _V2LocationProposalStore(),
    "proposal-contract:v2-resource.1": _V2ResourceProposalStore(),
    "proposal-contract:v2-attention.1": _V2AttentionProposalStore(),
}

_TYPED_PROPOSAL_REGISTRY = TypedProposalRegistry(
    tuple(
        TypedProposalRegistration(
            contract_ref=family.contract_ref,
            selector=family.selector,
            mutation_event_types=family.mutation_event_types,
            codec=family.codec,
            store=_TYPED_PROPOSAL_STORES[family.contract_ref],
        )
        for family in INSTALLED_TYPED_PROPOSAL_FAMILIES
    )
)


def _audit_only(state: ReducerState, _event: WorldEvent) -> ReducerState:
    return state


def _actor_authority_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    if state.logical_time is not None and event.logical_time != state.logical_time:
        raise ValueError("actor authority transition must be pinned to current logical time")
    logical_time = event.logical_time
    payload = ActorAuthorityMutationPayload.model_validate_json(event.payload_json)
    authorities, history, nonces = reduce_actor_authority(
        state.actor_authorities,
        state.actor_authority_transitions,
        state.consumed_actor_root_nonces,
        payload,
        event=event,
        logical_time=logical_time,
        accepted_world_revision=len(state.committed_world_event_refs) + 1,
    )
    return state.model_copy(
        update={
            "actor_authorities": authorities,
            "actor_authority_transitions": history,
            "consumed_actor_root_nonces": nonces,
        }
    )


def _authorization_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    if state.logical_time is not None and event.logical_time != state.logical_time:
        raise ValueError("authorization transition must be pinned to current logical time")
    model = AUTHORIZATION_PAYLOAD_MODELS[event.event_type]
    payload = model.model_validate_json(event.payload_json)
    domain = authorization_domain(event.event_type)
    if domain == "capability":
        projections, history = state.capability_grants, state.capability_transitions
        projection_field, history_field = "capability_grants", "capability_transitions"
    elif domain == "consent":
        projections, history = state.consent_grants, state.consent_transitions
        projection_field, history_field = "consent_grants", "consent_transitions"
    else:
        projections, history = state.privacy_policies, state.privacy_transitions
        projection_field, history_field = "privacy_policies", "privacy_transitions"
    updated, transitions, nonces, challenges, sources = reduce_authorization(
        projections,
        history,
        state.consumed_authorization_root_nonces,
        state.consumed_authorization_challenge_ids,
        state.consumed_authorization_source_ids,
        state.actor_authorities,
        payload,
        event=event,
        logical_time=event.logical_time,
    )
    return state.model_copy(
        update={
            projection_field: updated,
            history_field: transitions,
            "consumed_authorization_root_nonces": nonces,
            "consumed_authorization_challenge_ids": challenges,
            "consumed_authorization_source_ids": sources,
        }
    )


def _model_result_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = ModelResultRecordedPayload.model_validate(event.payload())
    if payload.evaluated_world_revision > len(state.committed_world_event_refs):
        raise ValueError("model result cannot evaluate a future world revision")
    if any(
        value.model_result_ref == payload.model_result_ref
        or value.model_call_id == payload.model_call_id
        for value in state.model_result_audits
    ):
        raise ValueError("model result identity is already registered")
    prior = tuple(
        value
        for value in state.model_result_audits
        if value.deliberation_result_id == payload.deliberation_result_id
    )
    if len(prior) != payload.attempt_index:
        raise ValueError("model attempt audit is not contiguous")
    if prior and any(
        value.attempt_id != payload.attempt_id
        or value.capsule_id != payload.capsule_id
        or value.trigger_ref != payload.trigger_ref
        or value.evaluated_world_revision != payload.evaluated_world_revision
        or value.attempt_count != payload.attempt_count
        or value.proposal_hash != payload.proposal_hash
        for value in prior
    ):
        raise ValueError("model attempt audit lineage changed")
    if payload.attempt_index == payload.attempt_count - 1:
        audits = tuple(
            RecordedModelResultAudit.model_validate_json(value.audit_json)
            for value in (*prior, payload)
        )
        validate_recorded_attempt_lineage(
            audits,
            capsule_id=payload.capsule_id,
            proposal_hash=payload.proposal_hash,
            deliberation_result_id=payload.deliberation_result_id,
        )
    projection = ModelResultAuditProjection(
        **payload.model_dump(mode="json"),
        event_ref=event.event_id,
        event_payload_hash=event.payload_hash,
    )
    return state.model_copy(
        update={"model_result_audits": (*state.model_result_audits, projection)}
    )


def _proposal_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    raw = event.payload()
    if raw.get("audit_contract") == "proposal-envelope-audit.1":
        payload = ProposalRecordedV2Payload.model_validate(raw)
        if payload.proposal_id in state.proposal_ids:
            raise ValueError("proposal identity is already registered")
        matching = next(
            (
                value
                for value in state.model_result_audits
                if value.model_result_ref == payload.model_result_ref
            ),
            None,
        )
        if matching is None or (
            matching.model_call_id != payload.model_call_id
            or matching.attempt_id != payload.attempt_id
            or matching.capsule_id != payload.capsule_id
            or matching.trigger_ref != payload.trigger_ref
            or matching.evaluated_world_revision != payload.evaluated_world_revision
            or matching.deliberation_result_id != payload.deliberation_result_id
            or matching.attempt_index != matching.attempt_count - 1
            or matching.proposal_hash != payload.proposal_hash
        ):
            raise ValueError("proposal does not bind the final recorded model result")
        if payload.evaluated_world_revision > len(state.committed_world_event_refs):
            raise ValueError("proposal cannot evaluate a future world revision")
        projection = ProposalAuditProjection(
            **payload.model_dump(mode="json"),
            event_ref=event.event_id,
            event_payload_hash=event.payload_hash,
        )
        return state.model_copy(
            update={
                "proposal_ids": (*state.proposal_ids, payload.proposal_id),
                "proposal_revisions": (
                    *state.proposal_revisions,
                    ProposalRevisionRef(
                        proposal_id=payload.proposal_id,
                        evaluated_world_revision=payload.evaluated_world_revision,
                    ),
                ),
                "proposal_audits": (*state.proposal_audits, projection),
            }
        )
    proposal_id = raw.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise ValueError("ProposalRecorded requires proposal_id")
    if proposal_id in state.proposal_ids:
        raise ValueError("proposal identity is already registered")
    registration = _TYPED_PROPOSAL_REGISTRY.registration_for_record(event.event_type, raw)
    if registration is not None:
        proposal = registration.codec.decode_record(event_type=event.event_type, payload=raw)
        return registration.store.validate_and_store(state, event, proposal)
    if raw.get("proposal_kind") not in {
        "appraisal_transition",
        "affect_transition",
    }:
        evaluated = raw.get("evaluated_world_revision")
        if isinstance(evaluated, int) and evaluated != len(state.committed_world_event_refs):
            raise ValueError("proposal must evaluate the current world revision")
        return state.model_copy(
            update={
                "proposal_ids": (*state.proposal_ids, proposal_id),
                "proposal_revisions": (
                    (
                        *state.proposal_revisions,
                        ProposalRevisionRef(
                            proposal_id=proposal_id,
                            evaluated_world_revision=evaluated,
                        ),
                    )
                    if isinstance(evaluated, int)
                    else state.proposal_revisions
                ),
            }
        )
    if raw.get("proposal_kind") == "affect_transition":
        return _affect_proposal_recorded(state, event)
    proposal = AppraisalProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("appraisal proposal must evaluate the current world revision")
    if proposal.proposal_id in state.appraisal_proposal_ids:
        raise ValueError("appraisal proposal identity is already registered")
    if proposal.policy_refs != INSTALLED_APPRAISAL_POLICY_REFS:
        raise ValueError("appraisal proposal references an uninstalled policy")
    proposed_model = {
        "AppraisalAccepted": AppraisalAcceptedPayload,
        "AppraisalContradicted": AppraisalContradictedPayload,
        "AppraisalSuperseded": AppraisalSupersededPayload,
    }[proposal.proposed_mutation.event_type]
    proposed_payload = proposed_model.model_validate_json(proposal.proposed_mutation.payload_json)
    if (
        proposed_payload.proposal_id != proposal.proposal_id
        or proposed_payload.change_id != proposal.change_id
        or proposed_payload.trigger_id != proposal.trigger_id
        or proposed_payload.evaluated_world_revision != proposal.evaluated_world_revision
        or proposed_payload.expected_entity_revision != proposal.expected_entity_revision
        or proposed_payload.accepted_change_hash != proposal.proposed_change_hash
        or proposed_payload.evidence_refs != proposal.evidence_refs
        or proposed_payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted appraisal proposal body does not match its index")
    trigger = next(
        (item for item in state.trigger_processes if item.trigger_id == proposal.trigger_id),
        None,
    )
    if (
        trigger is None
        or trigger.process_kind not in {"npc_world_appraisal", "interaction_appraisal"}
        or trigger.state != "claimed"
        or trigger.trigger_ref != proposal.trigger_ref
        or trigger.source_evidence_ref != proposal.source_evidence_ref
    ):
        raise ValueError("appraisal proposal requires its claimed source-bound trigger")
    source_evidence = next(
        (ref for ref in proposal.evidence_refs if ref.ref_id == proposal.source_evidence_ref),
        None,
    )
    expected_source_kind = (
        "settled_world_event"
        if trigger.process_kind == "npc_world_appraisal"
        else "observed_message"
    )
    if source_evidence is None or source_evidence.evidence_type != expected_source_kind:
        raise ValueError("appraisal proposal source evidence has the wrong authority kind")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    return state.model_copy(
        update={
            "appraisal_proposals": (*state.appraisal_proposals, proposal),
            "appraisal_proposal_ids": (
                *state.appraisal_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _relationship_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: RelationshipProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or RelationshipProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("relationship proposal must evaluate the current world revision")
    if proposal.proposal_id in state.relationship_proposal_ids:
        raise ValueError("relationship proposal identity is already registered")
    proposed_model = RELATIONSHIP_PAYLOAD_MODELS[proposal.proposed_mutation.event_type]
    proposed_payload = proposed_model.model_validate_json(proposal.proposed_mutation.payload_json)
    if not isinstance(proposed_payload, RelationshipAuthorizedMutationPayload):
        raise ValueError("relationship proposal does not contain an authorized mutation")
    if (
        proposed_payload.proposal_id != proposal.proposal_id
        or proposed_payload.change_id != proposal.change_id
        or proposed_payload.transition_id != proposal.transition_id
        or proposed_payload.evaluated_world_revision != proposal.evaluated_world_revision
        or proposed_payload.expected_entity_revision != proposal.expected_entity_revision
        or proposed_payload.accepted_change_hash != proposal.proposed_change_hash
        or proposed_payload.evidence_refs != proposal.evidence_refs
        or proposed_payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted relationship proposal body does not match its index")
    installed_policy = {
        "signal": INSTALLED_RELATIONSHIP_SIGNAL_POLICY_REFS,
        "adjust": INSTALLED_RELATIONSHIP_POLICY_REFS,
        "compensate": INSTALLED_RELATIONSHIP_POLICY_REFS,
        "boundary_open": INSTALLED_BOUNDARY_POLICY_REFS,
        "boundary_revise": INSTALLED_BOUNDARY_POLICY_REFS,
        "boundary_close": INSTALLED_BOUNDARY_POLICY_REFS,
    }[proposal.transition_kind]
    if proposal.policy_refs != installed_policy:
        raise ValueError("relationship proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    logical_time = _require_life_time(state, event)
    if isinstance(proposed_payload, RelationshipSignalAcceptedPayload):
        accept_relationship_signal(
            state.relationship_signals, proposed_payload, logical_time=logical_time
        )
    elif isinstance(proposed_payload, RelationshipSlowVariableAdjustedPayload):
        adjust_relationship_slow_variables(
            state.relationship_states,
            state.relationship_adjustments,
            state.relationship_signals,
            proposed_payload,
            logical_time=logical_time,
        )
    elif isinstance(proposed_payload, BoundaryChangedPayload):
        change_boundary(state.boundaries, proposed_payload, logical_time=logical_time)
    return state.model_copy(
        update={
            "relationship_proposals": (*state.relationship_proposals, proposal),
            "relationship_proposal_ids": (*state.relationship_proposal_ids, proposal.proposal_id),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _thread_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: ThreadProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or ThreadProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("thread proposal must evaluate the current world revision")
    if proposal.proposal_id in state.thread_proposal_ids:
        raise ValueError("thread proposal identity is already registered")
    proposed_model = THREAD_PAYLOAD_MODELS[proposal.proposed_mutation.event_type]
    proposed_payload = proposed_model.model_validate_json(proposal.proposed_mutation.payload_json)
    if not isinstance(proposed_payload, ThreadAuthorizedMutationPayload):
        raise ValueError("thread proposal does not contain an authorized mutation")
    if (
        proposed_payload.proposal_id != proposal.proposal_id
        or proposed_payload.change_id != proposal.change_id
        or proposed_payload.transition_id != proposal.transition_id
        or proposed_payload.evaluated_world_revision != proposal.evaluated_world_revision
        or proposed_payload.expected_entity_revision != proposal.expected_entity_revision
        or proposed_payload.accepted_change_hash != proposal.proposed_change_hash
        or proposed_payload.evidence_refs != proposal.evidence_refs
        or proposed_payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted thread proposal body does not match its index")
    if proposal.policy_refs != INSTALLED_THREAD_POLICY_REFS:
        raise ValueError("thread proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    logical_time = _require_life_time(state, event)
    if isinstance(proposed_payload, ThreadChangedPayload):
        reduce_thread(
            state.threads,
            state.thread_transitions,
            proposed_payload,
            event_type=proposal.proposed_mutation.event_type,
            logical_time=logical_time,
        )
    return state.model_copy(
        update={
            "thread_proposals": (*state.thread_proposals, proposal),
            "thread_proposal_ids": (*state.thread_proposal_ids, proposal.proposal_id),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _private_impression_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: PrivateImpressionProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or PrivateImpressionProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("private impression proposal must evaluate the current world revision")
    if proposal.proposal_id in state.private_impression_proposal_ids:
        raise ValueError("private impression proposal identity is already registered")
    payload = PrivateImpressionAcceptedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    if (
        payload.proposal_id != proposal.proposal_id
        or payload.change_id != proposal.change_id
        or payload.transition_id != proposal.transition_id
        or payload.evaluated_world_revision != proposal.evaluated_world_revision
        or payload.expected_entity_revision != proposal.expected_entity_revision
        or payload.accepted_change_hash != proposal.proposed_change_hash
        or payload.evidence_refs != proposal.evidence_refs
        or payload.appraisal_refs != proposal.appraisal_refs
        or payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted private impression proposal body does not match its index")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    _validate_private_impression_source_events(state, payload)
    # Dry-run the pure reducer at the pinned revision.  This prevents an LLM
    # from persisting an arbitrary internal judgement merely because its JSON
    # is well shaped; every readable meaning must resolve through appraisal.
    accept_private_impression(
        state.private_impressions,
        payload,
        logical_time=_require_life_time(state, event),
        appraisals=state.appraisals,
    )
    return state.model_copy(
        update={
            "private_impression_proposals": (*state.private_impression_proposals, proposal),
            "private_impression_proposal_ids": (
                *state.private_impression_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _commitment_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: CommitmentProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or CommitmentProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("commitment proposal must evaluate the current world revision")
    if proposal.proposal_id in state.commitment_proposal_ids:
        raise ValueError("commitment proposal identity is already registered")
    proposed_model = COMMITMENT_ACCEPTED_PAYLOAD_MODELS.get(
        proposal.proposed_mutation.event_type, CommitmentChangedPayload
    )
    proposed_payload = proposed_model.model_validate_json(proposal.proposed_mutation.payload_json)
    if not isinstance(proposed_payload, CommitmentAuthorizedMutationPayload):
        raise ValueError("commitment proposal does not contain accepted authority")
    if (
        proposed_payload.proposal_id != proposal.proposal_id
        or proposed_payload.change_id != proposal.change_id
        or proposed_payload.transition_id != proposal.transition_id
        or proposed_payload.evaluated_world_revision != proposal.evaluated_world_revision
        or proposed_payload.expected_entity_revision != proposal.expected_entity_revision
        or proposed_payload.accepted_change_hash != proposal.proposed_change_hash
        or proposed_payload.evidence_refs != proposal.evidence_refs
        or proposed_payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted commitment proposal body does not match its index")
    if proposal.policy_refs != INSTALLED_COMMITMENT_POLICY_REFS:
        raise ValueError("commitment proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    logical_time = _require_life_time(state, event)
    reduce_commitment(
        state.commitments,
        state.commitment_transitions,
        proposed_payload,
        event_type=proposal.proposed_mutation.event_type,
        logical_time=logical_time,
        committed_events=state.committed_world_event_refs,
        execution_receipts=state.execution_receipts,
        actions=state.actions,
        threads=state.threads,
        thread_history=state.thread_transitions,
        message_observations=state.message_observations,
    )
    return state.model_copy(
        update={
            "commitment_proposals": (*state.commitment_proposals, proposal),
            "commitment_proposal_ids": (
                *state.commitment_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _fact_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: FactProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or FactProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("fact proposal must evaluate the current world revision")
    if proposal.proposal_id in state.fact_proposal_ids:
        raise ValueError("fact proposal identity is already registered")
    proposed_payload = FactChangedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    if (
        proposed_payload.proposal_id != proposal.proposal_id
        or proposed_payload.change_id != proposal.change_id
        or proposed_payload.transition_id != proposal.transition_id
        or proposed_payload.evaluated_world_revision != proposal.evaluated_world_revision
        or proposed_payload.expected_entity_revision != proposal.expected_entity_revision
        or proposed_payload.accepted_change_hash != proposal.proposed_change_hash
        or proposed_payload.evidence_refs != proposal.evidence_refs
        or proposed_payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted fact proposal body does not match its index")
    if proposal.policy_refs != INSTALLED_FACT_POLICY_REFS:
        raise ValueError("fact proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    reduce_fact(
        state.facts,
        state.fact_transitions,
        proposed_payload,
        event_type=proposal.proposed_mutation.event_type,
        logical_time=_require_life_time(state, event),
        message_observations=state.message_observations,
        operator_observations=state.operator_observations,
    )
    return state.model_copy(
        update={
            "fact_proposals": (*state.fact_proposals, proposal),
            "fact_proposal_ids": (*state.fact_proposal_ids, proposal.proposal_id),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _experience_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: ExperienceProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or ExperienceProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("experience proposal must evaluate the current world revision")
    if proposal.proposal_id in state.experience_proposal_ids:
        raise ValueError("experience proposal identity is already registered")
    payload = ExperienceCommittedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    if (
        payload.proposal_id != proposal.proposal_id
        or payload.change_id != proposal.change_id
        or payload.transition_id != proposal.transition_id
        or payload.evaluated_world_revision != proposal.evaluated_world_revision
        or payload.expected_entity_revision != proposal.expected_entity_revision
        or payload.accepted_change_hash != proposal.proposed_change_hash
        or payload.evidence_refs != proposal.evidence_refs
        or payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted experience proposal body does not match its index")
    if proposal.policy_refs != INSTALLED_EXPERIENCE_POLICY_REFS:
        raise ValueError("experience proposal references an uninstalled policy")
    # Proposal evidence is present-tense rationale. Future settlement bindings
    # remain only inside the accepted canonical body until mutation time.
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    return state.model_copy(
        update={
            "experience_proposals": (*state.experience_proposals, proposal),
            "experience_proposal_ids": (
                *state.experience_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _memory_candidate_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: MemoryCandidateProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or MemoryCandidateProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("memory proposal must evaluate the current world revision")
    if proposal.proposal_id in state.memory_candidate_proposal_ids:
        raise ValueError("memory proposal identity is already registered")
    payload = MemoryCandidateChangedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    if (
        payload.proposal_id != proposal.proposal_id
        or payload.change_id != proposal.change_id
        or payload.transition_id != proposal.transition_id
        or payload.evaluated_world_revision != proposal.evaluated_world_revision
        or payload.expected_entity_revision != proposal.expected_entity_revision
        or payload.accepted_change_hash != proposal.proposed_change_hash
        or payload.evidence_refs != proposal.evidence_refs
        or payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted memory proposal body does not match its index")
    if proposal.policy_refs != MEMORY_POLICY_REFS:
        raise ValueError("memory proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    if isinstance(payload.forget_authority, MemoryEvidenceForgetAuthority):
        _validate_memory_forget_decision_evidence(state, payload)
    reduce_memory_candidate(
        state.memory_candidates,
        state.memory_candidate_transitions,
        payload,
        event_type=proposal.proposed_mutation.event_type,
        event_id=payload.candidate_after.origin.accepted_event_ref,
        logical_time=_require_life_time(state, event),
        facts=state.facts,
        fact_history=state.fact_transitions,
        experiences=state.experiences,
        experience_history=state.experience_transitions,
        threads=state.threads,
        thread_history=state.thread_transitions,
        committed_events=state.committed_world_event_refs,
    )
    return state.model_copy(
        update={
            "memory_candidate_proposals": (
                *state.memory_candidate_proposals,
                proposal,
            ),
            "memory_candidate_proposal_ids": (
                *state.memory_candidate_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _character_core_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: CharacterCoreProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or CharacterCoreProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("character core proposal must evaluate current world revision")
    if proposal.proposal_id in state.character_core_proposal_ids:
        raise ValueError("character core proposal identity is already registered")
    payload = CharacterCoreChangedPayload.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
    if (
        payload.proposal_id != proposal.proposal_id
        or payload.change_id != proposal.change_id
        or payload.transition_id != proposal.transition_id
        or payload.evaluated_world_revision != proposal.evaluated_world_revision
        or payload.expected_entity_revision != proposal.expected_entity_revision
        or payload.accepted_change_hash != proposal.proposed_change_hash
        or payload.evidence_refs != proposal.evidence_refs
        or payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted character core proposal body does not match index")
    if proposal.policy_refs != CHARACTER_CORE_POLICY_REFS:
        raise ValueError("character core proposal references uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    reduce_character_core(
        state.character_core,
        state.character_core_transitions,
        payload,
        event_type=proposal.proposed_mutation.event_type,
        event_id=payload.core_after.origin.accepted_event_ref,
        logical_time=_require_life_time(state, event),
        actor_authorities=state.actor_authorities,
        facts=state.facts,
        fact_history=state.fact_transitions,
        experiences=state.experiences,
        experience_history=state.experience_transitions,
        world_occurrences=state.world_occurrences,
        committed_events=state.committed_world_event_refs,
    )
    return state.model_copy(
        update={
            "character_core_proposals": (*state.character_core_proposals, proposal),
            "character_core_proposal_ids": (
                *state.character_core_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _v2_goal_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: V2GoalProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or V2GoalProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("Goal proposal must evaluate the current world revision")
    if proposal.proposal_id in state.goal_proposal_ids:
        raise ValueError("Goal proposal identity is already registered")
    payload = V2GoalChangedPayload.model_validate_json(proposal.proposed_mutation.payload_json)
    if (
        payload.proposal_id != proposal.proposal_id
        or payload.change_id != proposal.change_id
        or payload.transition_id != proposal.transition_id
        or payload.evaluated_world_revision != proposal.evaluated_world_revision
        or payload.expected_entity_revision != proposal.expected_entity_revision
        or payload.accepted_change_hash != proposal.proposed_change_hash
        or payload.evidence_refs != proposal.evidence_refs
        or payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted Goal proposal body does not match index")
    if proposal.policy_refs != V2_GOAL_POLICY_REFS:
        raise ValueError("Goal proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    logical_time = _require_life_time(state, event)
    reduce_v2_goal(
        state.goals,
        state.goal_transitions,
        payload,
        event_type=proposal.proposed_mutation.event_type,
        event_id=payload.goal_after.origin.accepted_event_ref,
        logical_time=logical_time,
        actor_authorities=state.actor_authorities,
        committed_events=state.committed_world_event_refs,
        random_draws=(),
        world_occurrences=state.world_occurrences,
        facts=state.facts,
        experiences=state.experiences,
        clock_transition_history=state.clock_transition_history,
    )
    return state.model_copy(
        update={
            "goal_proposals": (*state.goal_proposals, proposal),
            "goal_proposal_ids": (*state.goal_proposal_ids, proposal.proposal_id),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _v2_location_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: V2LocationProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or V2LocationProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("Location proposal must evaluate the current world revision")
    if proposal.proposal_id in state.location_proposal_ids:
        raise ValueError("Location proposal identity is already registered")
    payload = V2LocationChangedPayload.model_validate_json(proposal.proposed_mutation.payload_json)
    if (
        payload.proposal_id != proposal.proposal_id
        or payload.change_id != proposal.change_id
        or payload.transition_id != proposal.transition_id
        or payload.evaluated_world_revision != proposal.evaluated_world_revision
        or payload.expected_entity_revision != proposal.expected_entity_revision
        or payload.accepted_change_hash != proposal.proposed_change_hash
        or payload.evidence_refs != proposal.evidence_refs
        or payload.policy_refs != proposal.policy_refs
        or payload.operation != proposal.transition_kind
        or payload.model_dump(mode="json") != json.loads(proposal.proposed_mutation.payload_json)
    ):
        raise ValueError("persisted Location proposal body does not match its index")
    if proposal.policy_refs != V2_LOCATION_POLICY_REFS:
        raise ValueError("Location proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    reduce_v2_location(
        state.locations,
        state.location_transitions,
        payload,
        event_type=proposal.proposed_mutation.event_type,
        event_id=payload.location_after.origin.accepted_event_ref,
        logical_time=_require_life_time(state, event),
        actor_authorities=state.actor_authorities,
        committed_events=state.committed_world_event_refs,
    )
    return state.model_copy(
        update={
            "location_proposals": (*state.location_proposals, proposal),
            "location_proposal_ids": (
                *state.location_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _v2_resource_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: V2ResourceProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or V2ResourceProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("Resource proposal must evaluate the current world revision")
    if proposal.proposal_id in state.resource_proposal_ids:
        raise ValueError("Resource proposal identity is already registered")
    payload = V2ResourceChangedPayload.model_validate_json(proposal.proposed_mutation.payload_json)
    if (
        payload.proposal_id != proposal.proposal_id
        or payload.change_id != proposal.change_id
        or payload.transition_id != proposal.transition_id
        or payload.evaluated_world_revision != proposal.evaluated_world_revision
        or payload.expected_entity_revision != proposal.expected_entity_revision
        or payload.accepted_change_hash != proposal.proposed_change_hash
        or payload.evidence_refs != proposal.evidence_refs
        or payload.policy_refs != proposal.policy_refs
        or payload.operation != proposal.transition_kind
        or payload.model_dump(mode="json") != json.loads(proposal.proposed_mutation.payload_json)
    ):
        raise ValueError("persisted Resource proposal body does not match its index")
    if proposal.policy_refs != V2_RESOURCE_POLICY_REFS:
        raise ValueError("Resource proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    reduce_v2_resource(
        state.resources,
        state.resource_transitions,
        payload,
        event_type=proposal.proposed_mutation.event_type,
        event_id=payload.resource_after.origin.accepted_event_ref,
        logical_time=_require_life_time(state, event),
        actor_authorities=state.actor_authorities,
        committed_events=state.committed_world_event_refs,
    )
    return state.model_copy(
        update={
            "resource_proposals": (*state.resource_proposals, proposal),
            "resource_proposal_ids": (*state.resource_proposal_ids, proposal.proposal_id),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _v2_attention_proposal_recorded(
    state: ReducerState,
    event: WorldEvent,
    *,
    proposal: V2AttentionProposalProjection | None = None,
) -> ReducerState:
    proposal = proposal or V2AttentionProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("Attention proposal must evaluate the current world revision")
    if proposal.proposal_id in state.attention_proposal_ids:
        raise ValueError("Attention proposal identity is already registered")
    payload = V2AttentionChangedPayload.model_validate_json(proposal.proposed_mutation.payload_json)
    if (
        payload.proposal_id != proposal.proposal_id
        or payload.change_id != proposal.change_id
        or payload.transition_id != proposal.transition_id
        or payload.attention_after.actor_ref != proposal.actor_ref
        or payload.evaluated_world_revision != proposal.evaluated_world_revision
        or payload.expected_entity_revision != proposal.expected_entity_revision
        or payload.accepted_change_hash != proposal.proposed_change_hash
        or payload.evidence_refs != proposal.evidence_refs
        or payload.policy_refs != proposal.policy_refs
        or payload.operation != proposal.transition_kind
        or payload.model_dump(mode="json") != json.loads(proposal.proposed_mutation.payload_json)
    ):
        raise ValueError("persisted Attention proposal body does not match its index")
    if proposal.policy_refs != V2_ATTENTION_POLICY_REFS:
        raise ValueError("Attention proposal references an uninstalled policy")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    reduce_v2_attention(
        state.attentions,
        state.attention_transitions,
        payload,
        event_type=proposal.proposed_mutation.event_type,
        event_id=payload.attention_after.origin.accepted_event_ref,
        logical_time=_require_life_time(state, event),
        actor_authorities=state.actor_authorities,
        committed_events=state.committed_world_event_refs,
        plans=state.plans,
        world_occurrences=state.world_occurrences,
        triggers=state.trigger_processes,
    )
    return state.model_copy(
        update={
            "attention_proposals": (*state.attention_proposals, proposal),
            "attention_proposal_ids": (
                *state.attention_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _affect_proposal_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    proposal = AffectProposalProjection.model_validate_json(event.payload_json)
    if proposal.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("affect proposal must evaluate the current world revision")
    if proposal.proposal_id in state.affect_proposal_ids:
        raise ValueError("affect proposal identity is already registered")
    installed_policy = (
        INSTALLED_AFFECT_BASELINE_POLICY_REFS
        if proposal.transition_kind == "baseline_adjust"
        else INSTALLED_AFFECT_POLICY_REFS
    )
    if proposal.policy_refs != installed_policy:
        raise ValueError("affect proposal references an uninstalled policy")
    if proposal.source_audit is not None:
        _validate_compiled_affect_proposal_source(state, proposal)
    proposed_model = AFFECT_PAYLOAD_MODELS[proposal.proposed_mutation.event_type]
    proposed_payload = proposed_model.model_validate_json(proposal.proposed_mutation.payload_json)
    if not isinstance(proposed_payload, AffectAuthorizedMutationPayload):
        raise ValueError("mechanical affect decay cannot be proposed by deliberation")
    if (
        proposed_payload.proposal_id != proposal.proposal_id
        or proposed_payload.change_id != proposal.change_id
        or proposed_payload.transition_id != proposal.transition_id
        or proposed_payload.evaluated_world_revision != proposal.evaluated_world_revision
        or proposed_payload.expected_entity_revision != proposal.expected_entity_revision
        or proposed_payload.accepted_change_hash != proposal.proposed_change_hash
        or proposed_payload.evidence_refs != proposal.evidence_refs
        or proposed_payload.appraisal_refs != proposal.appraisal_refs
        or proposed_payload.policy_refs != proposal.policy_refs
    ):
        raise ValueError("persisted affect proposal body does not match its index")
    _validate_evidence_authority(state, proposal.evidence_refs, require_all=True)
    _validate_appraisal_meaning_refs(state.appraisals, proposal.appraisal_refs)
    if isinstance(proposed_payload, AffectBaselineAdjustedPayload):
        if state.logical_time is None:
            raise ValueError("baseline proposal requires authoritative logical time")
        adjust_affect_baseline(
            state.affect_baselines,
            state.affect_episodes,
            proposed_payload,
            logical_time=state.logical_time,
        )
    proposal = proposal.model_copy(
        update={
            "recorded_event_ref": event.event_id,
            "recorded_event_payload_hash": event.payload_hash,
        }
    )
    return state.model_copy(
        update={
            "affect_proposals": (*state.affect_proposals, proposal),
            "affect_proposal_ids": (
                *state.affect_proposal_ids,
                proposal.proposal_id,
            ),
            "proposal_ids": (*state.proposal_ids, proposal.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=proposal.proposal_id,
                    evaluated_world_revision=proposal.evaluated_world_revision,
                ),
            ),
        }
    )


def _validate_compiled_affect_proposal_source(
    state: ReducerState, proposal: AffectProposalProjection
) -> None:
    """Reprove that a production Affect candidate came from one generic decision change."""

    source = proposal.source_audit
    assert source is not None
    audit = next(
        (item for item in state.proposal_audits if item.event_ref == source.proposal_event_ref),
        None,
    )
    if audit is None or (
        audit.event_payload_hash != source.proposal_event_payload_hash
        or audit.model_result_ref != source.model_result_ref
        or audit.capsule_id != source.capsule_id
        or audit.evaluated_world_revision != proposal.evaluated_world_revision
    ):
        raise ValueError("compiled affect proposal source audit does not resolve")
    try:
        generic = validate_proposal_envelope(json.loads(audit.proposal_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("compiled affect proposal source proposal is invalid") from exc
    if not isinstance(generic, DecisionProposal) or generic.affect_decision != "propose":
        raise ValueError("compiled affect proposal source is not an affect decision")
    changes = tuple(
        item
        for item in generic.proposed_changes
        if item.kind == "affect_transition" and item.change_id == source.change_id
    )
    if len(changes) != 1 or changes[0].payload.payload_hash != source.change_payload_hash:
        raise ValueError("compiled affect proposal source change does not resolve")


def _acceptance_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    raw = event.payload()
    if "manifest_version" in raw and raw.get("manifest_version") not in {
        "acceptance-manifest.2",
        "acceptance-manifest.3",
        MINIMAL_REPLY_MANIFEST_VERSION,
        EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION,
        APPRAISAL_ACCEPTANCE_MANIFEST_VERSION,
        AFFECT_ACCEPTANCE_MANIFEST_VERSION,
        OUTCOME_ACCEPTANCE_MANIFEST_VERSION,
        ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
        *MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS,
        INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION,
        MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
    }:
        raise ValueError("acceptance_manifest.unsupported_manifest_version")
    if raw.get("manifest_version") == "acceptance-manifest.2":
        return _acceptance_manifest_v2_recorded(state, event)
    if raw.get("manifest_version") == "acceptance-manifest.3":
        return _acceptance_manifest_v3_recorded(state, event)
    if raw.get("manifest_version") == MINIMAL_REPLY_MANIFEST_VERSION:
        return _minimal_reply_manifest_recorded(state, event)
    if raw.get("manifest_version") == EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION:
        return _expression_plan_manifest_recorded(state, event)
    if raw.get("manifest_version") == APPRAISAL_ACCEPTANCE_MANIFEST_VERSION:
        return _appraisal_acceptance_manifest_recorded(state, event)
    if raw.get("manifest_version") == AFFECT_ACCEPTANCE_MANIFEST_VERSION:
        return _affect_acceptance_manifest_recorded(state, event)
    if raw.get("manifest_version") == OUTCOME_ACCEPTANCE_MANIFEST_VERSION:
        return _outcome_acceptance_manifest_recorded(state, event)
    if raw.get("manifest_version") == ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION:
        return _activity_lifecycle_acceptance_manifest_recorded(state, event)
    if raw.get("manifest_version") in MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS:
        return _media_selection_acceptance_manifest_recorded(state, event)
    if raw.get("manifest_version") == INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION:
        return _interaction_bid_acceptance_manifest_recorded(state, event)
    if raw.get("manifest_version") == MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION:
        return _media_thread_acceptance_manifest_recorded(state, event)
    proposal_id = raw.get("proposal_id")
    evaluated_world_revision = raw.get("evaluated_world_revision")
    if not isinstance(proposal_id, str) or not isinstance(evaluated_world_revision, int):
        raise ValueError("AcceptanceRecorded requires proposal and evaluated revision")
    if any(audit.proposal_id == proposal_id for audit in state.proposal_audits):
        raise ValueError("acceptance_manifest.v2_proposal_requires_manifest")
    if proposal_id not in state.proposal_ids:
        raise ValueError("AcceptanceRecorded references an unknown proposal")
    if any(item.proposal_id == proposal_id for item in state.acceptance_decisions):
        raise ValueError("proposal already has an acceptance decision")
    proposal_revision = next(
        (
            item.evaluated_world_revision
            for item in state.proposal_revisions
            if item.proposal_id == proposal_id
        ),
        None,
    )
    if proposal_revision is None or evaluated_world_revision != proposal_revision:
        raise ValueError("acceptance decision does not match proposal revision")
    acceptance_id = raw.get("acceptance_id")
    if acceptance_id is not None and (
        not isinstance(acceptance_id, str)
        or not acceptance_id
        or any(item.acceptance_id == acceptance_id for item in state.acceptance_decisions)
    ):
        raise ValueError("acceptance identity is already registered or invalid")
    status = raw.get("status")
    if status not in {"accepted", "rejected", "stale"}:
        raise ValueError("AcceptanceRecorded has an invalid status")
    current_world_revision = len(state.committed_world_event_refs)
    experience_proposal = next(
        (item for item in state.experience_proposals if item.proposal_id == proposal_id),
        None,
    )
    settlement_bridge = False
    if (
        status == "accepted"
        and experience_proposal is not None
        and current_world_revision == evaluated_world_revision + 2
        and len(state.committed_world_event_refs) >= 2
    ):
        proposed = ExperienceCommittedPayload.model_validate_json(
            experience_proposal.proposed_mutation.payload_json
        )
        latest = state.committed_world_event_refs[-1]
        settlement_bridge = (
            state.committed_world_event_refs[-2].event_type == "AcceptanceRecorded"
            and latest.event_type == "WorldOccurrenceSettled"
            and any(
                isinstance(binding, ExperienceOccurrenceSettlementBinding)
                and binding.authority_event_ref == latest.event_id
                and binding.authority_world_revision == latest.world_revision
                and binding.authority_payload_hash == latest.payload_hash
                for binding in proposed.experience.values.source_bindings
            )
        )
    if status in {"accepted", "rejected"} and (
        evaluated_world_revision != current_world_revision and not settlement_bridge
    ):
        raise ValueError("accepted or rejected decision must evaluate the current world")
    if status == "stale" and evaluated_world_revision >= current_world_revision:
        raise ValueError("stale decision must evaluate an older world revision")
    typed_authority = _TYPED_PROPOSAL_REGISTRY.authority_for(state, proposal_id)
    if status == "accepted":
        if typed_authority is None:
            raise ValueError("accepted decision requires a typed proposal")
        authority = typed_authority[1]
        if (
            raw.get("accepted_change_id") != authority.change_id
            or raw.get("accepted_change_hash") != authority.proposed_change_hash
            or evaluated_world_revision != authority.evaluated_world_revision
        ):
            raise ValueError("accepted decision does not match proposal authority")
    decision = AcceptanceDecisionRef(
        proposal_id=proposal_id,
        evaluated_world_revision=evaluated_world_revision,
        acceptance_id=acceptance_id,
        status=status,
        accepted_change_id=raw.get("accepted_change_id"),
        accepted_change_hash=raw.get("accepted_change_hash"),
    )
    decided_state = state.model_copy(
        update={
            "acceptance_decisions": (*state.acceptance_decisions, decision),
        }
    )
    if status in {"rejected", "stale"}:
        discarded = _TYPED_PROPOSAL_REGISTRY.discard_decided(decided_state, proposal_id)
        if not isinstance(discarded, ReducerState):
            raise TypeError("typed proposal registry returned an incompatible state")
        return discarded
    return decided_state


def _acceptance_manifest_v2_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    manifest = parse_acceptance_manifest_v2(event.payload())
    current_world_revision = len(state.committed_world_event_refs)
    if manifest.status == "rejected":
        if manifest.evaluated_world_revision != current_world_revision:
            raise ValueError("rejected manifest must evaluate the current world")
    elif manifest.status == "stale":
        if manifest.evaluated_world_revision >= current_world_revision:
            raise ValueError("stale manifest must evaluate an older world revision")
    else:  # parse_acceptance_manifest_v2 currently fails closed before this branch.
        raise ValueError("acceptance_manifest.accepted_not_enabled")
    if any(
        item.acceptance_id == manifest.acceptance_id for item in state.acceptance_manifests_v2
    ) or any(item.acceptance_id == manifest.acceptance_id for item in state.acceptance_decisions):
        raise ValueError("acceptance identity is already registered")

    audit_by_id = {item.proposal_id: item for item in state.proposal_audits}
    for binding in manifest.proposals:
        audit = audit_by_id.get(binding.proposal_id)
        if audit is None:
            raise ValueError("acceptance manifest references an unknown ProposalAudit")
        if any(
            decision.proposal_id == binding.proposal_id for decision in state.acceptance_decisions
        ):
            raise ValueError("proposal already has an acceptance decision")
        expected_binding = derive_acceptance_manifest_proposal_v2(
            proposal_json=audit.proposal_json,
            proposal_event_ref=audit.event_ref,
            proposal_event_payload_hash=audit.event_payload_hash,
        )
        if binding != expected_binding or (
            binding.evaluated_world_revision != manifest.evaluated_world_revision
        ):
            raise ValueError("acceptance manifest does not exactly bind ProposalAudit")

    decisions = tuple(
        AcceptanceDecisionRef(
            proposal_id=binding.proposal_id,
            evaluated_world_revision=binding.evaluated_world_revision,
            acceptance_id=manifest.acceptance_id,
            status=manifest.status,
            manifest_version=manifest.manifest_version,
            manifest_hash=manifest.manifest_hash,
            acceptance_event_ref=event.event_id,
            acceptance_event_payload_hash=event.payload_hash,
        )
        for binding in manifest.proposals
    )
    updated = state.model_copy(
        update={
            "acceptance_decisions": (*state.acceptance_decisions, *decisions),
            "acceptance_manifests_v2": (
                *state.acceptance_manifests_v2,
                AcceptanceManifestRefV2.from_manifest(
                    manifest,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                    recorded_at_world_revision=current_world_revision + 1,
                ),
            ),
        }
    )
    for binding in manifest.proposals:
        discarded = _TYPED_PROPOSAL_REGISTRY.discard_decided(updated, binding.proposal_id)
        if not isinstance(discarded, ReducerState):
            raise TypeError("typed proposal registry returned an incompatible state")
        updated = discarded
    return updated


def _fact_commit_proposal_audit_v2_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Index the closed Fact-v2 audit without treating it as a legacy proposal.

    ``FactCommitProposalRecorded`` is a deliberation event.  Its pinned world
    revision therefore has to match the state immediately before this event;
    the later manifest-v3 reducer rebinds the complete event hash rather than
    relying on an ambient proposal cache.
    """

    audit = FactCommitProposalAuditRefV2.from_event(event)
    if audit.proposal_world_id != event.world_id:
        raise ValueError("Fact v2 proposal audit world identity does not match its event")
    if audit.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("Fact v2 proposal audit must evaluate the current world revision")
    if any(item.proposal_id == audit.proposal_id for item in state.fact_commit_proposal_audits_v2):
        raise ValueError("Fact v2 proposal audit identity is already registered")
    return state.model_copy(
        update={
            "fact_commit_proposal_audits_v2": (
                *state.fact_commit_proposal_audits_v2,
                audit,
            )
        }
    )


def _acceptance_manifest_v3_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Accept the first closed v3 authority lane: one Fact-v2 commit plan."""

    manifest = rehydrate_acceptance_manifest_v3(event.payload())
    current_world_revision = len(state.committed_world_event_refs)
    if manifest.status != "accepted":
        raise ValueError("Fact v2 manifest must be accepted")
    if manifest.evaluated_world_revision != current_world_revision:
        raise ValueError("accepted Fact v2 manifest must evaluate the current world")
    if len(manifest.proposals) != 1 or len(manifest.authorized_effects) != 1:
        raise ValueError("Fact v2 manifest must contain exactly one proposal and effect")
    if (
        any(
            item.manifest.acceptance_id == manifest.acceptance_id
            for item in state.acceptance_manifests_v3
        )
        or any(
            item.acceptance_id == manifest.acceptance_id for item in state.acceptance_manifests_v2
        )
        or any(item.acceptance_id == manifest.acceptance_id for item in state.acceptance_decisions)
    ):
        raise ValueError("acceptance identity is already registered")

    summary = manifest.proposals[0]
    if summary.audit_contract != "fact-commit-proposal-audit.2":
        raise ValueError("Fact v2 manifest uses an unsupported proposal audit")
    audit = next(
        (
            item
            for item in state.fact_commit_proposal_audits_v2
            if item.proposal_id == summary.proposal_id
        ),
        None,
    )
    if audit is None or any(
        item.proposal_id == summary.proposal_id for item in state.acceptance_decisions
    ):
        raise ValueError("Fact v2 manifest references unavailable or decided proposal authority")
    if (
        summary.proposal_schema_registry != audit.proposal_schema_registry
        or summary.proposal_event_ref != audit.event_ref
        or summary.proposal_event_payload_hash != audit.event_payload_hash
        or summary.proposal_hash != audit.proposal_hash
        or summary.evaluated_world_revision != audit.evaluated_world_revision
        or event.causation_id != audit.event_ref
    ):
        raise ValueError("Fact v2 manifest does not exactly bind its proposal audit")
    proposal = validate_fact_commit_proposal_v2(
        json.loads(audit.proposal_json), world_id=audit.proposal_world_id
    )
    if (
        summary.proposal_kind != proposal.proposal_kind
        or summary.proposal_schema_registry != proposal.schema_registry_version
        or summary.action_intents
        or len(summary.changes) != len(proposal.proposed_changes)
    ):
        raise ValueError("Fact v2 manifest proposal summary is not exact")
    expected_changes = {
        change.change_id: (
            change.kind,
            change.target_id,
            change.transition,
            change.expected_entity_revision,
            change.evidence_refs,
            change.preconditions,
            change.policy_refs,
            change.payload.payload_schema,
            change.payload.payload_version,
            change.payload.payload_hash,
            canonical_full_change_authority_hash_v2(change),
        )
        for change in proposal.proposed_changes
    }
    for change in summary.changes:
        if expected_changes.get(change.change_id) != (
            change.kind,
            change.target_id,
            change.transition,
            change.expected_entity_revision,
            change.evidence_refs,
            change.preconditions,
            change.policy_refs,
            change.payload_schema,
            change.payload_version,
            change.payload_hash,
            change.full_change_authority_hash,
        ):
            raise ValueError("Fact v2 manifest change authority is not exact")

    effect = manifest.authorized_effects[0]
    if (
        effect.ordinal != 0
        or effect.role != "domain_mutation"
        or effect.event_type != "FactCommittedV2"
        or len(effect.authority_refs) != 1
    ):
        raise ValueError("Fact v2 manifest effect is not the installed mutation")
    authority = effect.authority_refs[0]
    change = next(
        (item for item in summary.changes if item.change_id == authority.authority_id), None
    )
    if (
        authority.proposal_id != summary.proposal_id
        or authority.authority_kind != "change"
        or change is None
        or authority.authority_hash != change.full_change_authority_hash
    ):
        raise ValueError("Fact v2 manifest effect authority is not exact")

    decision = AcceptanceDecisionRef(
        proposal_id=summary.proposal_id,
        evaluated_world_revision=summary.evaluated_world_revision,
        acceptance_id=manifest.acceptance_id,
        status="accepted",
        accepted_change_id=change.change_id,
        accepted_change_hash=change.full_change_authority_hash,
        manifest_version=manifest.manifest_version,
        manifest_hash=manifest.manifest_hash,
        acceptance_event_ref=event.event_id,
        acceptance_event_payload_hash=event.payload_hash,
    )
    return state.model_copy(
        update={
            "acceptance_decisions": (*state.acceptance_decisions, decision),
            "acceptance_manifests_v3": (
                *state.acceptance_manifests_v3,
                AcceptanceManifestRefV3.from_manifest(
                    manifest,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                    recorded_at_world_revision=current_world_revision + 1,
                ),
            ),
        }
    )


def _minimal_reply_manifest_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Index one reply-only acceptance without granting Fact-v3 semantics."""

    manifest = MinimalReplyManifest.model_validate_json(event.payload_json)
    current_world_revision = len(state.committed_world_event_refs)
    if manifest.evaluated_world_revision != current_world_revision:
        raise ValueError("minimal reply manifest must evaluate the current world")
    if event.causation_id != manifest.proposal_event_ref:
        raise ValueError("minimal reply manifest causation does not bind its audit")
    if any(item.acceptance_id == manifest.acceptance_id for item in state.minimal_reply_manifests):
        raise ValueError("minimal reply acceptance identity is already registered")
    if any(item.proposal_id == manifest.proposal_id for item in state.minimal_reply_manifests):
        raise ValueError("minimal reply proposal already has an acceptance")
    audit = next(
        (item for item in state.proposal_audits if item.proposal_id == manifest.proposal_id), None
    )
    if audit is None or audit.proposal_kind != "minimal":
        raise ValueError("minimal reply manifest references unavailable proposal authority")
    if (
        audit.event_ref != manifest.proposal_event_ref
        or audit.event_payload_hash != manifest.proposal_event_payload_hash
        or audit.proposal_hash != manifest.proposal_hash
        or audit.evaluated_world_revision != manifest.evaluated_world_revision
    ):
        raise ValueError("minimal reply manifest does not exactly bind its proposal audit")
    try:
        proposal = validate_proposal_envelope(
            MinimalProposal.model_validate_json(audit.proposal_json, strict=True)
        )
    except Exception as exc:
        raise ValueError("minimal reply proposal audit is invalid") from exc
    if not isinstance(proposal, MinimalProposal) or (
        proposal.proposal_id != manifest.proposal_id
        or proposal.proposal_hash != manifest.proposal_hash
        or len(proposal.proposed_changes) != 1
        or len(proposal.action_intents) != 1
    ):
        raise ValueError("minimal reply proposal authority is not exact")
    change = proposal.proposed_changes[0]
    intent = proposal.action_intents[0]
    payload = change.payload.value()
    drafts = payload.get("beat_drafts")
    if not isinstance(drafts, list) or len(drafts) != 1 or not isinstance(drafts[0], dict):
        raise ValueError("minimal reply proposal has no single expression beat")
    draft = drafts[0]
    intent_hash = hashlib.sha256(
        json.dumps(
            intent.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        change.kind != "expression_plan_transition"
        or change.transition != "accept"
        or change.change_id != manifest.expression_change_id
        or change.payload.payload_hash == ""
        or intent.intent_id != manifest.intent_id
        or intent_hash != manifest.intent_hash
        or intent.causal_change_id != change.change_id
        or payload.get("plan_id") != manifest.plan_id
        or draft.get("beat_id") != manifest.beat_id
        or draft.get("materialized_payload_ref") != manifest.message_payload_ref
        or draft.get("payload_hash") != manifest.message_payload_hash
        or intent.payload_ref != manifest.message_payload_ref
        or intent.payload_hash != manifest.message_payload_hash
    ):
        raise ValueError("minimal reply manifest does not exactly bind proposal content")
    ref = MinimalReplyManifestRef(
        **manifest.model_dump(mode="python", exclude={"manifest_version"}),
        expression_change_hash=change.payload.payload_hash,
        acceptance_event_ref=event.event_id,
        acceptance_event_payload_hash=event.payload_hash,
        recorded_at_world_revision=current_world_revision + 1,
    )
    return state.model_copy(
        update={"minimal_reply_manifests": (*state.minimal_reply_manifests, ref)}
    )


def _expression_plan_manifest_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Index a normal multi-beat ExpressionPlan before any effects exist."""

    manifest = ExpressionPlanAcceptanceManifest.model_validate_json(event.payload_json)
    current_world_revision = len(state.committed_world_event_refs)
    if manifest.evaluated_world_revision != current_world_revision:
        raise ValueError("expression plan manifest must evaluate the current world")
    if event.causation_id != manifest.proposal_event_ref:
        raise ValueError("expression plan manifest causation does not bind its audit")
    if any(
        item.acceptance_id == manifest.acceptance_id or item.proposal_id == manifest.proposal_id
        for item in state.expression_plan_manifests
    ) or any(item.proposal_id == manifest.proposal_id for item in state.minimal_reply_manifests):
        raise ValueError("expression plan proposal or acceptance is already decided")
    audit = next(
        (item for item in state.proposal_audits if item.proposal_id == manifest.proposal_id), None
    )
    if audit is None or (
        audit.event_ref != manifest.proposal_event_ref
        or audit.event_payload_hash != manifest.proposal_event_payload_hash
        or audit.proposal_hash != manifest.proposal_hash
        or audit.evaluated_world_revision != manifest.evaluated_world_revision
    ):
        raise ValueError("expression plan manifest does not exactly bind proposal audit")
    try:
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("expression plan proposal audit is invalid") from exc
    changes = tuple(
        item
        for item in proposal.proposed_changes
        if item.change_id == manifest.expression_change_id
        and item.kind == "expression_plan_transition"
        and item.transition == "accept"
    )
    if len(changes) != 1 or len(proposal.proposed_changes) != 1:
        raise ValueError("expression plan manifest proposal is not exact")
    change = changes[0]
    payload = change.payload.value()
    drafts = payload.get("beat_drafts")
    if (
        change.payload.payload_hash != manifest.expression_change_hash
        or payload.get("plan_id") != manifest.plan_id
        or payload.get("ordering_policy") != manifest.ordering_policy
        or payload.get("terminal_policy") != manifest.terminal_policy
        or not isinstance(drafts, list)
        or len(drafts) != len(manifest.beats)
    ):
        raise ValueError("expression plan manifest does not exactly bind proposal content")
    intents = {item.beat_ref: item for item in proposal.action_intents}
    if len(intents) != len(proposal.action_intents):
        raise ValueError("expression plan proposal has duplicate beat intent")
    refs: list[ExpressionPlanManifestBeatRef] = []
    for draft, item in zip(drafts, manifest.beats, strict=True):
        if not isinstance(draft, dict):
            raise ValueError("expression plan draft is invalid")
        intent = intents.get(item.beat.beat_id)
        inline_text = draft.get("inline_text")
        has_sidecar = (
            isinstance(draft.get("payload_ref"), str) or "inline_encrypted_payload" in draft
        )
        if intent is None or (
            draft.get("beat_id") != item.beat.beat_id
            or (
                draft.get("payload_ref")
                if isinstance(draft.get("payload_ref"), str)
                else draft.get("materialized_payload_ref")
            )
            != item.beat.payload.payload_ref
            or draft.get("payload_hash") != item.beat.payload.payload_hash
            or inline_text != item.beat.payload.text
            or (item.beat.payload.storage_kind == "inline_text") != isinstance(inline_text, str)
            or (item.beat.payload.storage_kind == "sidecar") != has_sidecar
            or (
                item.beat.payload.storage_kind == "sidecar"
                and item.beat.payload.sidecar_kind
                != (
                    "referenced"
                    if isinstance(draft.get("payload_ref"), str)
                    else "inline_encrypted"
                )
            )
            or tuple(draft.get("dependency_beat_ids", ())) != item.beat.dependency_beat_ids
            or intent.intent_id != item.intent_id
            or canonical_expression_plan_value_hash(intent.model_dump(mode="json"))
            != item.intent_hash
            or intent.causal_change_id != manifest.expression_change_id
            or intent.payload_ref != item.beat.payload.payload_ref
            or intent.payload_hash != item.beat.payload.payload_hash
            or item.action.intent_ref != f"{manifest.proposal_id}:{item.intent_id}"
        ):
            raise ValueError("expression plan manifest beat does not bind proposal")
        refs.append(
            ExpressionPlanManifestBeatRef(
                beat_id=item.beat.beat_id,
                payload_ref=item.beat.payload.payload_ref,
                payload_hash=item.beat.payload.payload_hash,
                text=item.beat.payload.text,
                content_type=item.beat.payload.content_type,
                storage_kind=item.beat.payload.storage_kind,
                sidecar_kind=item.beat.payload.sidecar_kind,
                privacy_class=item.beat.payload.privacy_class,
                dependency_beat_ids=item.beat.dependency_beat_ids,
                not_before=item.beat.not_before,
                expires_at=item.beat.expires_at,
                cancel_policy=item.beat.cancel_policy,
                reconsider_policy=item.beat.reconsider_policy,
                merge_policy=item.beat.merge_policy,
                intent_id=item.intent_id,
                intent_hash=item.intent_hash,
                message_hash=item.message_hash,
                beat_hash=item.beat_hash,
                reservation=item.reservation,
                reservation_hash=item.reservation_hash,
                action=item.action,
                action_hash=item.action_hash,
            )
        )
    return state.model_copy(
        update={
            "expression_plan_manifests": (
                *state.expression_plan_manifests,
                ExpressionPlanManifestRef(
                    acceptance_id=manifest.acceptance_id,
                    proposal_id=manifest.proposal_id,
                    proposal_event_ref=manifest.proposal_event_ref,
                    proposal_event_payload_hash=manifest.proposal_event_payload_hash,
                    proposal_hash=manifest.proposal_hash,
                    evaluated_world_revision=manifest.evaluated_world_revision,
                    policy_digest=manifest.policy_digest,
                    expression_change_id=manifest.expression_change_id,
                    expression_change_hash=manifest.expression_change_hash,
                    plan_id=manifest.plan_id,
                    ordering_policy=manifest.ordering_policy,
                    terminal_policy=manifest.terminal_policy,
                    beats=tuple(refs),
                    manifest_hash=manifest.manifest_hash,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                    recorded_at_world_revision=current_world_revision + 1,
                ),
            )
        }
    )


def _appraisal_acceptance_manifest_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Record the decision half of the isolated Appraisal accepted batch."""

    manifest = AppraisalAcceptanceManifest.model_validate_json(event.payload_json)
    current_world_revision = len(state.committed_world_event_refs)
    if manifest.evaluated_world_revision != current_world_revision:
        raise ValueError("appraisal acceptance manifest must evaluate the current world")
    if event.causation_id != manifest.proposal_event_ref:
        raise ValueError("appraisal acceptance manifest causation does not bind proposal")
    if any(
        item.acceptance_id == manifest.acceptance_id or item.proposal_id == manifest.proposal_id
        for item in state.acceptance_decisions
    ):
        raise ValueError("appraisal proposal or acceptance is already decided")
    proposal = next(
        (item for item in state.appraisal_proposals if item.proposal_id == manifest.proposal_id),
        None,
    )
    if proposal is None or (
        proposal.change_id != manifest.accepted_change_id
        or proposal.evaluated_world_revision != manifest.evaluated_world_revision
        or proposal.proposed_change_hash != manifest.accepted_change_hash
        or proposal.trigger_id != manifest.trigger_id
        or proposal.proposed_mutation.event_type != manifest.mutation_event_type
    ):
        raise ValueError("appraisal acceptance manifest does not bind persisted proposal")
    proposed = json.loads(proposal.proposed_mutation.payload_json)
    if canonical_appraisal_acceptance_value_hash(proposed) != manifest.mutation_payload_hash:
        raise ValueError("appraisal acceptance manifest mutation hash is invalid")
    return state.model_copy(
        update={
            "acceptance_decisions": (
                *state.acceptance_decisions,
                AcceptanceDecisionRef(
                    proposal_id=manifest.proposal_id,
                    evaluated_world_revision=manifest.evaluated_world_revision,
                    acceptance_id=manifest.acceptance_id,
                    status="accepted",
                    accepted_change_id=manifest.accepted_change_id,
                    accepted_change_hash=manifest.accepted_change_hash,
                    manifest_version=manifest.manifest_version,
                    manifest_hash=manifest.manifest_hash,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _affect_acceptance_manifest_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Record the decision half of the isolated Affect accepted batch."""

    manifest = AffectAcceptanceManifest.model_validate_json(event.payload_json)
    current_world_revision = len(state.committed_world_event_refs)
    if manifest.evaluated_world_revision != current_world_revision:
        raise ValueError("affect acceptance manifest must evaluate the current world")
    if event.causation_id != manifest.proposal_event_ref:
        raise ValueError("affect acceptance manifest causation does not bind proposal")
    if any(
        item.acceptance_id == manifest.acceptance_id or item.proposal_id == manifest.proposal_id
        for item in state.acceptance_decisions
    ):
        raise ValueError("affect proposal or acceptance is already decided")
    proposal = next(
        (item for item in state.affect_proposals if item.proposal_id == manifest.proposal_id),
        None,
    )
    if proposal is None or (
        proposal.change_id != manifest.accepted_change_id
        or proposal.evaluated_world_revision != manifest.evaluated_world_revision
        or proposal.proposed_change_hash != manifest.accepted_change_hash
        or proposal.proposed_mutation.event_type != manifest.mutation_event_type
        or proposal.recorded_event_ref != manifest.proposal_event_ref
        or proposal.recorded_event_payload_hash != manifest.proposal_event_payload_hash
    ):
        raise ValueError("affect acceptance manifest does not bind persisted proposal")
    proposed = json.loads(proposal.proposed_mutation.payload_json)
    if canonical_affect_acceptance_value_hash(proposed) != manifest.mutation_payload_hash:
        raise ValueError("affect acceptance manifest mutation hash is invalid")
    return state.model_copy(
        update={
            "acceptance_decisions": (
                *state.acceptance_decisions,
                AcceptanceDecisionRef(
                    proposal_id=manifest.proposal_id,
                    evaluated_world_revision=manifest.evaluated_world_revision,
                    acceptance_id=manifest.acceptance_id,
                    status="accepted",
                    accepted_change_id=manifest.accepted_change_id,
                    accepted_change_hash=manifest.accepted_change_hash,
                    manifest_version=manifest.manifest_version,
                    manifest_hash=manifest.manifest_hash,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _outcome_acceptance_manifest_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Record only a compiler-bound Outcome acceptance decision.

    The batch invariant separately verifies the settlement and continuation;
    this reducer makes direct or legacy Outcome acceptance fail closed.
    """

    manifest = OutcomeAcceptanceManifest.model_validate_json(event.payload_json)
    current_world_revision = len(state.committed_world_event_refs)
    if manifest.evaluated_world_revision != current_world_revision:
        raise ValueError("outcome acceptance manifest must evaluate the current world")
    if event.causation_id != manifest.proposal_event_ref:
        raise ValueError("outcome acceptance manifest causation does not bind proposal")
    if any(
        item.acceptance_id == manifest.acceptance_id or item.proposal_id == manifest.proposal_id
        for item in state.acceptance_decisions
    ):
        raise ValueError("outcome proposal or acceptance is already decided")
    proposal = next(
        (
            item
            for item in state.outcome_proposals
            if item.outcome_proposal_id == manifest.proposal_id
        ),
        None,
    )
    audit = next(
        (
            item
            for item in state.proposal_audits
            if item.proposal_id == proposal.decision_proposal_id
        )
        if proposal is not None
        else (),
        None,
    )
    expected_proposal_ref = None
    if audit is not None and proposal is not None:
        encoded = json.dumps(
            {
                "contract": "outcome-proposal-compiler.1",
                "source_proposal_event": audit.event_ref,
                "source_change": proposal.change_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_proposal_ref = (
            "event:outcome-proposal-compiled:" + hashlib.sha256(encoded).hexdigest()
        )
    if (
        proposal is None
        or audit is None
        or (
            manifest.proposal_event_ref != expected_proposal_ref
            or proposal.change_id != manifest.accepted_change_id
            or proposal.evaluated_world_revision != manifest.evaluated_world_revision
            or proposal.proposed_change_hash != manifest.accepted_change_hash
            or proposal.deliberation_trigger_id != manifest.deliberation_trigger_id
            or proposal.deliberation_trigger_id is None
            or proposal.source_observation_id is None
        )
    ):
        raise ValueError("outcome acceptance manifest does not bind persisted proposal")
    trigger = next(
        (
            item
            for item in state.trigger_processes
            if item.trigger_id == manifest.deliberation_trigger_id
        ),
        None,
    )
    source_event_id = f"event:outcome-observation:{proposal.source_observation_id}"
    if (
        trigger is None
        or trigger.process_kind != "outcome_deliberation"
        or trigger.state != "claimed"
        or trigger.claim_lease is None
        or trigger.source_evidence_ref != source_event_id
        or not any(
            item.event_id == source_event_id and item.event_type == "OutcomeObservationRecorded"
            for item in state.committed_world_event_refs
        )
    ):
        raise ValueError("outcome acceptance manifest source trigger is not claimed")
    return state.model_copy(
        update={
            "acceptance_decisions": (
                *state.acceptance_decisions,
                AcceptanceDecisionRef(
                    proposal_id=manifest.proposal_id,
                    evaluated_world_revision=manifest.evaluated_world_revision,
                    acceptance_id=manifest.acceptance_id,
                    status="accepted",
                    accepted_change_id=manifest.accepted_change_id,
                    accepted_change_hash=manifest.accepted_change_hash,
                    manifest_version=manifest.manifest_version,
                    manifest_hash=manifest.manifest_hash,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _activity_lifecycle_acceptance_manifest_recorded(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    """Record the acceptance half of an activity scheduler transition."""

    manifest = ActivityLifecycleAcceptanceManifest.model_validate_json(event.payload_json)
    current_world_revision = len(state.committed_world_event_refs)
    revision = next(
        (item for item in state.proposal_revisions if item.proposal_id == manifest.proposal_id),
        None,
    )
    if (
            manifest.evaluated_world_revision != current_world_revision
            or event.causation_id != manifest.proposal_event_ref
            or revision is None
            or revision.evaluated_world_revision != manifest.evaluated_world_revision
            or revision.proposal_event_ref != manifest.proposal_event_ref
            or revision.proposal_event_payload_hash != manifest.proposal_event_payload_hash
        or any(
            item.acceptance_id == manifest.acceptance_id or item.proposal_id == manifest.proposal_id
            for item in state.acceptance_decisions
        )
    ):
        raise ValueError("activity lifecycle acceptance manifest does not bind persisted proposal")
    return state.model_copy(
        update={
            "acceptance_decisions": (
                *state.acceptance_decisions,
                AcceptanceDecisionRef(
                    proposal_id=manifest.proposal_id,
                    evaluated_world_revision=manifest.evaluated_world_revision,
                    acceptance_id=manifest.acceptance_id,
                    status="accepted",
                    accepted_change_id=manifest.accepted_change_id,
                    accepted_change_hash=manifest.accepted_change_hash,
                    manifest_version=manifest.manifest_version,
                    manifest_hash=manifest.manifest_hash,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _media_selection_acceptance_manifest_recorded(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    """Accept a P1 choice only while its source-bound candidate remains current."""

    manifest = parse_media_selection_acceptance_manifest(event.payload())
    current_world_revision = len(state.committed_world_event_refs)
    revision = next(
        (item for item in state.proposal_revisions if item.proposal_id == manifest.proposal_id),
        None,
    )
    candidate = next(
        (item for item in state.photo_candidates if item.candidate_id == manifest.candidate_id),
        None,
    )
    if (
        manifest.evaluated_world_revision != current_world_revision
        or event.causation_id != manifest.proposal_event_ref
        or revision is None
        or revision.evaluated_world_revision != manifest.evaluated_world_revision
        or revision.proposal_event_ref != manifest.proposal_event_ref
        or revision.proposal_event_payload_hash != manifest.proposal_event_payload_hash
        or revision.proposed_change_hash != manifest.accepted_change_hash
        or revision.selection_hash != manifest.selection_hash
        or candidate is None
        or candidate.status != "available"
        or candidate.entity_revision != manifest.expected_candidate_revision
        or candidate.expires_at is None
        or event.logical_time >= candidate.expires_at
        or media_candidate_authority_hash(candidate) != manifest.candidate_authority_hash
        or any(
            item.acceptance_id == manifest.acceptance_id or item.proposal_id == manifest.proposal_id
            for item in state.acceptance_decisions
        )
    ):
        raise ValueError("media selection acceptance manifest does not bind a current proposal")
    return state.model_copy(
        update={
            "acceptance_decisions": (
                *state.acceptance_decisions,
                AcceptanceDecisionRef(
                    proposal_id=manifest.proposal_id,
                    evaluated_world_revision=manifest.evaluated_world_revision,
                    acceptance_id=manifest.acceptance_id,
                    status="accepted",
                    accepted_change_id=manifest.accepted_change_id,
                    accepted_change_hash=manifest.accepted_change_hash,
                    selection_hash=manifest.selection_hash,
                    manifest_version=manifest.manifest_version,
                    manifest_hash=manifest.manifest_hash,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _interaction_bid_acceptance_manifest_recorded(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    manifest = InteractionBidAcceptanceManifest.model_validate_json(event.payload_json)
    current_world_revision = len(state.committed_world_event_refs)
    proposal = next(
        (
            item
            for item in state.interaction_bid_proposals
            if item.interaction_bid_proposal_id == manifest.proposal_id
        ),
        None,
    )
    trigger = next(
        (
            item
            for item in state.trigger_processes
            if item.trigger_id == manifest.deliberation_trigger_id
        ),
        None,
    )
    source = next(
        (
            item
            for item in state.committed_world_event_refs
            if item.event_id == manifest.delivery_event_ref
        ),
        None,
    )
    if (
        manifest.evaluated_world_revision != current_world_revision
        or event.causation_id != manifest.proposal_event_ref
        or proposal is None
        or proposal.change_id != manifest.accepted_change_id
        or proposal.proposed_change_hash != manifest.accepted_change_hash
        or proposal.evaluated_world_revision != manifest.evaluated_world_revision
        or proposal.delivery_id != manifest.delivery_id
        or proposal.delivery_event_ref != manifest.delivery_event_ref
        or proposal.delivery_event_payload_hash != manifest.delivery_event_payload_hash
        or proposal.deliberation_trigger_id != manifest.deliberation_trigger_id
        or source is None
        or source.event_type != "MediaDeliveryShared"
        or source.payload_hash != manifest.delivery_event_payload_hash
        or trigger is None
        or trigger.process_kind != "media_delivery_interaction"
        or trigger.state != "claimed"
        or trigger.claim_lease is None
        or trigger.source_evidence_ref != manifest.delivery_event_ref
        or any(
            item.acceptance_id == manifest.acceptance_id or item.proposal_id == manifest.proposal_id
            for item in state.acceptance_decisions
        )
    ):
        raise ValueError(
            "interaction bid acceptance manifest does not bind a claimed delivery proposal"
        )
    return state.model_copy(
        update={
            "acceptance_decisions": (
                *state.acceptance_decisions,
                AcceptanceDecisionRef(
                    proposal_id=manifest.proposal_id,
                    evaluated_world_revision=manifest.evaluated_world_revision,
                    acceptance_id=manifest.acceptance_id,
                    status="accepted",
                    accepted_change_id=manifest.accepted_change_id,
                    accepted_change_hash=manifest.accepted_change_hash,
                    manifest_version=manifest.manifest_version,
                    manifest_hash=manifest.manifest_hash,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _media_thread_acceptance_manifest_recorded(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    """Accept only a compiled delivered-media thread proposal, never generic Thread."""
    manifest = MediaDeliveryThreadAcceptanceManifest.model_validate_json(event.payload_json)
    proposal = next(
        (
            item
            for item in state.media_thread_proposals
            if item.media_thread_proposal_id == manifest.proposal_id
        ),
        None,
    )
    trigger = next(
        (
            item
            for item in state.trigger_processes
            if item.trigger_id == manifest.deliberation_trigger_id
        ),
        None,
    )
    source = next(
        (
            item
            for item in state.committed_world_event_refs
            if item.event_id == manifest.delivery_event_ref
        ),
        None,
    )
    if (
        proposal is None
        or manifest.evaluated_world_revision != len(state.committed_world_event_refs)
        or event.causation_id != manifest.proposal_event_ref
        or proposal.change_id != manifest.accepted_change_id
        or proposal.proposed_change_hash != manifest.accepted_change_hash
        or proposal.evaluated_world_revision != manifest.evaluated_world_revision
        or proposal.delivery_id != manifest.delivery_id
        or proposal.delivery_event_ref != manifest.delivery_event_ref
        or proposal.delivery_event_payload_hash != manifest.delivery_event_payload_hash
        or proposal.deliberation_trigger_id != manifest.deliberation_trigger_id
        or source is None
        or source.event_type != "MediaDeliveryShared"
        or source.payload_hash != manifest.delivery_event_payload_hash
        or trigger is None
        or trigger.process_kind != "media_delivery_interaction"
        or trigger.state != "claimed"
        or trigger.claim_lease is None
        or trigger.source_evidence_ref != manifest.delivery_event_ref
        or any(
            item.acceptance_id == manifest.acceptance_id or item.proposal_id == manifest.proposal_id
            for item in state.acceptance_decisions
        )
    ):
        raise ValueError(
            "media delivery thread acceptance manifest does not bind a claimed delivery proposal"
        )
    return state.model_copy(
        update={
            "acceptance_decisions": (
                *state.acceptance_decisions,
                AcceptanceDecisionRef(
                    proposal_id=manifest.proposal_id,
                    evaluated_world_revision=manifest.evaluated_world_revision,
                    acceptance_id=manifest.acceptance_id,
                    status="accepted",
                    accepted_change_id=manifest.accepted_change_id,
                    accepted_change_hash=manifest.accepted_change_hash,
                    manifest_version=manifest.manifest_version,
                    manifest_hash=manifest.manifest_hash,
                    acceptance_event_ref=event.event_id,
                    acceptance_event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _message_payload_stored(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MessagePayloadStoredPayload.model_validate_json(event.payload_json)
    generic = _expression_plan_manifest(state, payload.acceptance_id)
    if generic is not None:
        if any(
            item.acceptance_id == payload.acceptance_id for item in state.stored_message_payloads
        ) or any(
            item.acceptance_id == payload.acceptance_id
            for item in state.expression_payload_descriptors
        ):
            if not state.committed_world_event_refs or state.committed_world_event_refs[
                -1
            ].event_type not in {"MessagePayloadStored", "ExpressionPayloadDescriptorRecorded"}:
                raise ValueError("expression plan payload storage must be contiguous")
        else:
            _require_previous_event(state, "AcceptanceRecorded", generic.acceptance_event_ref)
        beat = next(
            (item for item in generic.beats if item.payload_ref == payload.message.payload_ref),
            None,
        )
        if (
            beat is None
            or payload.proposal_id != generic.proposal_id
            or payload.message.payload_hash != beat.payload_hash
            or payload.message.text != beat.text
            or payload.message.content_type != beat.content_type
            or payload.message.storage_kind != "inline_text"
            or canonical_expression_plan_value_hash(payload.message.model_dump(mode="json"))
            != beat.message_hash
            or any(
                item.payload_ref == payload.message.payload_ref
                for item in state.stored_message_payloads
            )
        ):
            raise ValueError("expression plan message payload is not authorized")
        return state.model_copy(
            update={
                "stored_message_payloads": (
                    *state.stored_message_payloads,
                    StoredMessagePayloadProjection(
                        acceptance_id=payload.acceptance_id,
                        proposal_id=payload.proposal_id,
                        payload_ref=payload.message.payload_ref,
                        payload_hash=payload.message.payload_hash,
                        text=payload.message.text,
                        content_type=payload.message.content_type,
                        event_ref=event.event_id,
                        event_payload_hash=event.payload_hash,
                    ),
                )
            }
        )
    manifest = _minimal_reply_manifest(state, payload.acceptance_id)
    _require_previous_event(state, "AcceptanceRecorded", manifest.acceptance_event_ref)
    if (
        payload.proposal_id != manifest.proposal_id
        or payload.message.payload_ref != manifest.message_payload_ref
        or payload.message.payload_hash != manifest.message_payload_hash
        or any(
            item.payload_ref == payload.message.payload_ref
            for item in state.stored_message_payloads
        )
    ):
        raise ValueError("minimal reply message payload is not authorized")
    return state.model_copy(
        update={
            "stored_message_payloads": (
                *state.stored_message_payloads,
                StoredMessagePayloadProjection(
                    acceptance_id=payload.acceptance_id,
                    proposal_id=payload.proposal_id,
                    payload_ref=payload.message.payload_ref,
                    payload_hash=payload.message.payload_hash,
                    text=payload.message.text,
                    content_type=payload.message.content_type,
                    event_ref=event.event_id,
                    event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _expression_payload_descriptor_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = ExpressionPayloadDescriptorRecordedPayload.model_validate_json(event.payload_json)
    generic = _expression_plan_manifest(state, payload.acceptance_id)
    if generic is None:
        raise ValueError("expression payload descriptor requires generic expression manifest")
    if any(
        item.acceptance_id == payload.acceptance_id for item in state.stored_message_payloads
    ) or any(
        item.acceptance_id == payload.acceptance_id for item in state.expression_payload_descriptors
    ):
        if not state.committed_world_event_refs or state.committed_world_event_refs[
            -1
        ].event_type not in {"MessagePayloadStored", "ExpressionPayloadDescriptorRecorded"}:
            raise ValueError("expression plan payload storage must be contiguous")
    else:
        _require_previous_event(state, "AcceptanceRecorded", generic.acceptance_event_ref)
    beat = next((item for item in generic.beats if item.payload_ref == payload.payload_ref), None)
    if (
        beat is None
        or payload.proposal_id != generic.proposal_id
        or beat.storage_kind != "sidecar"
        or beat.sidecar_kind != payload.payload_kind
        or beat.payload_hash != payload.payload_hash
        or beat.content_type != payload.content_type
        or beat.privacy_class != payload.privacy_class
        or any(
            item.payload_ref == payload.payload_ref for item in state.expression_payload_descriptors
        )
        or any(item.payload_ref == payload.payload_ref for item in state.stored_message_payloads)
    ):
        raise ValueError("expression payload descriptor is not authorized")
    return state.model_copy(
        update={
            "expression_payload_descriptors": (
                *state.expression_payload_descriptors,
                ExpressionPayloadDescriptorProjection(
                    acceptance_id=payload.acceptance_id,
                    proposal_id=payload.proposal_id,
                    payload_ref=payload.payload_ref,
                    payload_hash=payload.payload_hash,
                    content_type=payload.content_type,
                    privacy_class=payload.privacy_class,
                    payload_kind=payload.payload_kind,
                    event_ref=event.event_id,
                    event_payload_hash=event.payload_hash,
                ),
            )
        }
    )


def _expression_plan_accepted(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = ExpressionPlanAcceptedPayload.model_validate_json(event.payload_json)
    generic = _expression_plan_manifest(state, payload.acceptance_id)
    if generic is not None:
        if (
            payload.proposal_id != generic.proposal_id
            or payload.expression_change_id != generic.expression_change_id
            or payload.plan_id != generic.plan_id
            or any(item.plan_id == payload.plan_id for item in state.expression_plans)
            or (
                {
                    item.payload_ref
                    for item in state.stored_message_payloads
                    if item.acceptance_id == payload.acceptance_id
                }
                | {
                    item.payload_ref
                    for item in state.expression_payload_descriptors
                    if item.acceptance_id == payload.acceptance_id
                }
            )
            != {item.payload_ref for item in generic.beats}
        ):
            raise ValueError("expression plan is not authorized")
        return state.model_copy(
            update={
                "expression_plans": (
                    *state.expression_plans,
                    ExpressionPlanProjection(
                        acceptance_id=payload.acceptance_id,
                        proposal_id=payload.proposal_id,
                        expression_change_id=payload.expression_change_id,
                        plan_id=payload.plan_id,
                        event_ref=event.event_id,
                        event_payload_hash=event.payload_hash,
                        history=(
                            ExpressionPlanLifecycleEntry(
                                state="authorized",
                                event_ref=event.event_id,
                                event_payload_hash=event.payload_hash,
                            ),
                        ),
                    ),
                )
            }
        )
    manifest = _minimal_reply_manifest(state, payload.acceptance_id)
    _require_previous_event(state, "MessagePayloadStored")
    if (
        payload.proposal_id != manifest.proposal_id
        or payload.expression_change_id != manifest.expression_change_id
        or payload.plan_id != manifest.plan_id
        or any(item.plan_id == payload.plan_id for item in state.expression_plans)
    ):
        raise ValueError("minimal reply expression plan is not authorized")
    return state.model_copy(
        update={
            "expression_plans": (
                *state.expression_plans,
                ExpressionPlanProjection(
                    acceptance_id=payload.acceptance_id,
                    proposal_id=payload.proposal_id,
                    expression_change_id=payload.expression_change_id,
                    plan_id=payload.plan_id,
                    event_ref=event.event_id,
                    event_payload_hash=event.payload_hash,
                    history=(
                        ExpressionPlanLifecycleEntry(
                            state="authorized",
                            event_ref=event.event_id,
                            event_payload_hash=event.payload_hash,
                        ),
                    ),
                ),
            )
        }
    )


def _expression_beat_authorized(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = ExpressionBeatAuthorizedPayload.model_validate_json(event.payload_json)
    generic = _expression_plan_manifest(state, payload.acceptance_id)
    if generic is not None:
        beat_ref = next(
            (item for item in generic.beats if item.beat_id == payload.beat.beat_id), None
        )
        if (
            beat_ref is None
            or payload.proposal_id != generic.proposal_id
            or payload.expression_change_id != generic.expression_change_id
            or payload.beat.plan_id != generic.plan_id
            or payload.beat.payload.payload_ref != beat_ref.payload_ref
            or payload.beat.payload.payload_hash != beat_ref.payload_hash
            or canonical_expression_plan_value_hash(payload.beat.model_dump(mode="json"))
            != beat_ref.beat_hash
            or not any(item.plan_id == generic.plan_id for item in state.expression_plans)
            or not any(
                item.payload_ref == beat_ref.payload_ref
                and item.payload_hash == beat_ref.payload_hash
                for item in (*state.stored_message_payloads, *state.expression_payload_descriptors)
            )
            or any(item.beat_id == payload.beat.beat_id for item in state.expression_beats)
        ):
            raise ValueError("expression beat is not authorized")
        return state.model_copy(
            update={
                "expression_beats": (
                    *state.expression_beats,
                    ExpressionBeatProjection(
                        acceptance_id=payload.acceptance_id,
                        proposal_id=payload.proposal_id,
                        expression_change_id=payload.expression_change_id,
                        plan_id=payload.beat.plan_id,
                        beat_id=payload.beat.beat_id,
                        payload_ref=payload.beat.payload.payload_ref,
                        payload_hash=payload.beat.payload.payload_hash,
                        action_id=beat_ref.action.action_id,
                        dependency_beat_ids=payload.beat.dependency_beat_ids,
                        not_before=payload.beat.not_before,
                        expires_at=payload.beat.expires_at,
                        cancel_policy=payload.beat.cancel_policy,
                        reconsider_policy=payload.beat.reconsider_policy,
                        merge_policy=payload.beat.merge_policy,
                        event_ref=event.event_id,
                        event_payload_hash=event.payload_hash,
                        history=(
                            ExpressionBeatLifecycleEntry(
                                state="authorized",
                                event_ref=event.event_id,
                                event_payload_hash=event.payload_hash,
                            ),
                        ),
                    ),
                )
            }
        )
    manifest = _minimal_reply_manifest(state, payload.acceptance_id)
    _require_previous_event(state, "ExpressionPlanAccepted")
    if (
        payload.proposal_id != manifest.proposal_id
        or payload.expression_change_id != manifest.expression_change_id
        or payload.beat.plan_id != manifest.plan_id
        or payload.beat.beat_id != manifest.beat_id
        or payload.beat.payload.payload_ref != manifest.message_payload_ref
        or payload.beat.payload.payload_hash != manifest.message_payload_hash
        or canonical_minimal_reply_value_hash(payload.beat.model_dump(mode="json"))
        != manifest.beat_hash
        or not any(
            item.payload_ref == manifest.message_payload_ref
            and item.payload_hash == manifest.message_payload_hash
            for item in state.stored_message_payloads
        )
        or any(item.beat_id == payload.beat.beat_id for item in state.expression_beats)
    ):
        raise ValueError("minimal reply expression beat is not authorized")
    return state.model_copy(
        update={
            "expression_beats": (
                *state.expression_beats,
                ExpressionBeatProjection(
                    acceptance_id=payload.acceptance_id,
                    proposal_id=payload.proposal_id,
                    expression_change_id=payload.expression_change_id,
                    plan_id=payload.beat.plan_id,
                    beat_id=payload.beat.beat_id,
                    payload_ref=payload.beat.payload.payload_ref,
                    payload_hash=payload.beat.payload.payload_hash,
                    action_id=manifest.action_id,
                    dependency_beat_ids=payload.beat.dependency_beat_ids,
                    not_before=payload.beat.not_before,
                    expires_at=payload.beat.expires_at,
                    cancel_policy=payload.beat.cancel_policy,
                    reconsider_policy=payload.beat.reconsider_policy,
                    merge_policy=payload.beat.merge_policy,
                    event_ref=event.event_id,
                    event_payload_hash=event.payload_hash,
                    history=(
                        ExpressionBeatLifecycleEntry(
                            state="authorized",
                            event_ref=event.event_id,
                            event_payload_hash=event.payload_hash,
                        ),
                    ),
                ),
            )
        }
    )


def _expression_beat_settled(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Advance the beat head only from its adjacent terminal receipt authority."""

    payload = ExpressionBeatSettledPayload.model_validate_json(event.payload_json)
    _require_previous_event(state, "ExecutionReceiptRecorded", payload.receipt_event_ref)
    generic = _expression_plan_manifest(state, payload.acceptance_id)
    manifest = _minimal_reply_manifest(state, payload.acceptance_id) if generic is None else None
    receipt = next(
        (item for item in state.execution_receipts if item.receipt_id == payload.receipt_id), None
    )
    action = next((item for item in state.actions if item.action_id == payload.action_id), None)
    beat_index = next(
        (
            index
            for index, item in enumerate(state.expression_beats)
            if item.beat_id == payload.beat_id
        ),
        None,
    )
    if (
        receipt is None
        or action is None
        or beat_index is None
        or payload.proposal_id
        != (generic.proposal_id if generic is not None else manifest.proposal_id)
        or payload.plan_id != (generic.plan_id if generic is not None else manifest.plan_id)
        or (generic is None and payload.beat_id != manifest.beat_id)
        or (generic is None and payload.action_id != manifest.action_id)
        or receipt.action_id != payload.action_id
        or not receipt.is_terminal
        or receipt.observed_state != payload.terminal_action_state
        or action.state != payload.terminal_action_state
        or action.expression_plan_id != payload.plan_id
        or action.expression_beat_id != payload.beat_id
        or state.committed_world_event_refs[-1].payload_hash != payload.receipt_event_payload_hash
    ):
        raise ValueError("expression beat settlement is not bound to terminal receipt authority")
    beat = state.expression_beats[beat_index]
    generic_beat = (
        next((item for item in generic.beats if item.beat_id == payload.beat_id), None)
        if generic is not None
        else None
    )
    if (
        beat.state != "authorized"
        or beat.acceptance_id != payload.acceptance_id
        or beat.proposal_id != payload.proposal_id
        or beat.plan_id != payload.plan_id
        or beat.action_id != payload.action_id
        or (
            generic is not None
            and (generic_beat is None or generic_beat.action.action_id != payload.action_id)
        )
    ):
        raise ValueError("expression beat is not currently authorized for settlement")
    settled = beat.model_copy(
        update={
            "state": "settled",
            "history": (
                *beat.history,
                ExpressionBeatLifecycleEntry(
                    state="settled",
                    event_ref=event.event_id,
                    event_payload_hash=event.payload_hash,
                    receipt_id=receipt.receipt_id,
                    terminal_action_state=receipt.observed_state,
                ),
            ),
        }
    )
    return state.model_copy(
        update={
            "expression_beats": (
                *state.expression_beats[:beat_index],
                settled,
                *state.expression_beats[beat_index + 1 :],
            )
        }
    )


def _expression_plan_completed(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Close a plan only after every one of its durable beats has settled."""

    payload = ExpressionPlanCompletedPayload.model_validate_json(event.payload_json)
    _require_previous_event(state, "ExpressionBeatSettled")
    generic = _expression_plan_manifest(state, payload.acceptance_id)
    manifest = _minimal_reply_manifest(state, payload.acceptance_id) if generic is None else None
    plan_index = next(
        (
            index
            for index, item in enumerate(state.expression_plans)
            if item.plan_id == payload.plan_id
        ),
        None,
    )
    receipt = next(
        (item for item in state.execution_receipts if item.receipt_id == payload.receipt_id), None
    )
    terminal_beat = next(
        (item for item in state.expression_beats if item.beat_id == payload.terminal_beat_id), None
    )
    if (
        plan_index is None
        or receipt is None
        or terminal_beat is None
        or payload.proposal_id
        != (generic.proposal_id if generic is not None else manifest.proposal_id)
        or payload.plan_id != (generic.plan_id if generic is not None else manifest.plan_id)
        or (generic is None and payload.terminal_beat_id != manifest.beat_id)
        or terminal_beat.plan_id != payload.plan_id
        or terminal_beat.state != "settled"
        or receipt.action_id != terminal_beat.action_id
        or receipt.observed_state != payload.terminal_action_state
        or payload.terminal_action_state != "delivered"
        or state.committed_world_event_refs[-2].event_id != payload.receipt_event_ref
        or state.committed_world_event_refs[-2].payload_hash != payload.receipt_event_payload_hash
    ):
        raise ValueError("expression plan completion is not bound to settled beat authority")
    plan = state.expression_plans[plan_index]
    if (
        plan.state != "authorized"
        or plan.acceptance_id != payload.acceptance_id
        or plan.proposal_id != payload.proposal_id
        or any(
            beat.state != "settled"
            for beat in state.expression_beats
            if beat.plan_id == plan.plan_id
        )
        or any(
            not beat.history or beat.history[-1].terminal_action_state != "delivered"
            for beat in state.expression_beats
            if beat.plan_id == plan.plan_id
        )
    ):
        raise ValueError("expression plan still has un-settled beats")
    completed = plan.model_copy(
        update={
            "state": "completed",
            "history": (
                *plan.history,
                ExpressionPlanLifecycleEntry(
                    state="completed",
                    event_ref=event.event_id,
                    event_payload_hash=event.payload_hash,
                    receipt_id=receipt.receipt_id,
                    terminal_action_state=receipt.observed_state,
                ),
            ),
        }
    )
    return state.model_copy(
        update={
            "expression_plans": (
                *state.expression_plans[:plan_index],
                completed,
                *state.expression_plans[plan_index + 1 :],
            )
        }
    )


def _minimal_reply_manifest(state: ReducerState, acceptance_id: str) -> MinimalReplyManifestRef:
    manifest = next(
        (item for item in state.minimal_reply_manifests if item.acceptance_id == acceptance_id),
        None,
    )
    if manifest is None:
        raise ValueError("minimal reply effect has no accepted manifest")
    return manifest


def _expression_plan_manifest(
    state: ReducerState, acceptance_id: str
) -> ExpressionPlanManifestRef | None:
    return next(
        (item for item in state.expression_plan_manifests if item.acceptance_id == acceptance_id),
        None,
    )


def _require_previous_event(
    state: ReducerState, event_type: str, event_id: str | None = None
) -> None:
    if not state.committed_world_event_refs:
        raise ValueError("minimal reply effect has no predecessor")
    previous = state.committed_world_event_refs[-1]
    if previous.event_type != event_type or (
        event_id is not None and previous.event_id != event_id
    ):
        raise ValueError("minimal reply effect predecessor is not exact")


def _world_started(state: ReducerState, _event: WorldEvent) -> ReducerState:
    return state


def _observation_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    observation_id = event.payload().get("observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise ValueError("ObservationRecorded requires observation_id")
    if observation_id in state.observation_refs:
        raise ValueError("observation identity is already registered")
    payload = event.payload()
    if payload.get("observation_kind") == "message":
        observation = Observation.model_validate_json(event.payload_json)
        envelope_pairs = (
            (observation.world_id, event.world_id),
            (observation.logical_time, event.logical_time),
            (observation.created_at, event.created_at),
            (observation.actor, event.actor),
            (observation.source, event.source),
            (observation.trace_id, event.trace_id),
            (observation.causation_id, event.causation_id),
            (observation.correlation_id, event.correlation_id),
        )
        if any(payload_value != envelope_value for payload_value, envelope_value in envelope_pairs):
            raise ValueError("message observation payload conflicts with event envelope")
    else:
        if any(
            field in payload
            for field in (
                "source",
                "source_event_id",
                "channel",
                "payload_ref",
                "payload_hash",
                "received_at",
            )
        ):
            raise ValueError("message-shaped observation requires observation_kind")
        observation = None
    is_message = (
        observation is not None
        and observation.world_id == event.world_id
        and observation.observation_id == observation_id
    )
    return state.model_copy(
        update={
            "observation_refs": (*state.observation_refs, observation_id),
            "message_observations": (
                (
                    *state.message_observations,
                    MessageObservationRef(
                        observation_id=observation_id,
                        source=observation.source,
                        source_event_id=observation.source_event_id,
                        content_payload_hash=observation.payload_hash,
                        event_payload_hash=event.payload_hash,
                        world_revision=len(state.committed_world_event_refs) + 1,
                        actor=observation.actor,
                        channel=observation.channel,
                        payload_ref=observation.payload_ref,
                    ),
                )
                if is_message
                else state.message_observations
            ),
            "logical_time": max(state.logical_time, event.logical_time)
            if state.logical_time is not None
            else event.logical_time,
        }
    )


def _clock_advanced(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time_to = event.payload().get("logical_time_to")
    logical_time_from = event.payload().get("logical_time_from")
    if not isinstance(logical_time_from, str):
        raise ValueError("ClockAdvanced requires logical_time_from")
    if not isinstance(logical_time_to, str):
        raise ValueError("ClockAdvanced requires logical_time_to")
    origin = datetime.fromisoformat(logical_time_from)
    target = datetime.fromisoformat(logical_time_to)
    if (
        origin.tzinfo is None
        or origin.utcoffset() is None
        or target.tzinfo is None
        or target.utcoffset() is None
    ):
        raise ValueError("ClockAdvanced timestamps must be timezone-aware")
    if target <= origin:
        raise ValueError("ClockAdvanced logical_time_to must follow logical_time_from")
    if state.logical_time is not None and origin != state.logical_time:
        raise ValueError("ClockAdvanced logical_time_from does not match current logical time")
    if state.logical_time is not None and target <= state.logical_time:
        raise ValueError("logical time cannot move backwards or remain unchanged")
    history = append_clock_transition(
        state.clock_transition_history,
        event=event,
        current_logical_time=state.logical_time,
        computed_world_revision=len(state.committed_world_event_refs) + 1,
    )
    return state.model_copy(update={"logical_time": target, "clock_transition_history": history})


def _operator_observation_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = event.payload()
    observation_id = payload.get("observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise ValueError("OperatorObservationRecorded requires observation_id")
    if any(item.observation_id == observation_id for item in state.operator_observations):
        raise ValueError("operator observation identity is already registered")
    observation_hash = payload.get("observation_hash")
    if not isinstance(observation_hash, str):
        raise ValueError("OperatorObservationRecorded requires observation_hash")
    return state.model_copy(
        update={
            "operator_observations": (
                *state.operator_observations,
                OperatorObservationRef(
                    observation_id=observation_id,
                    observation_hash=observation_hash,
                ),
            )
        }
    )


def _action_authorized(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_payload = event.payload().get("action")
    action = Action.model_validate_json(
        json.dumps(action_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if action.world_id != event.world_id:
        raise ValueError("ActionAuthorized action belongs to another world")
    if action.state != "authorized":
        raise ValueError("ActionAuthorized requires authorized state")
    if any(existing.action_id == action.action_id for existing in state.actions):
        raise ValueError(f"action {action.action_id!r} is already registered")
    if any(existing.idempotency_key == action.idempotency_key for existing in state.actions):
        raise ValueError(f"action idempotency_key {action.idempotency_key!r} already exists")
    reservation = next(
        (
            item
            for item in state.budget_reservations
            if item.reservation_id == action.budget_reservation_id
        ),
        None,
    )
    if reservation is None or reservation.action_id != action.action_id:
        raise ValueError("ActionAuthorized requires its matching budget reservation")
    if reservation.state != "reserved":
        raise ValueError("ActionAuthorized budget reservation is not active")
    if is_provider_media_action(action):
        # A provider-side media action is executable only through the
        # enforcement grant vertical.  Existing message/media_send Actions do
        # not enter this branch and retain their legacy compatibility.
        require_provider_media_grant(
            action=action,
            projection=state,
            logical_time=event.logical_time,
        )
    if action.kind == "media_planning":
        opportunity = next(
            (
                item
                for item in state.media_opportunities
                if item.opportunity_id == action.intent_ref
            ),
            None,
        )
        if opportunity is None:
            raise ValueError("media planning Action must bind a frozen opportunity")
        if any(item.opportunity_id == opportunity.opportunity_id for item in state.media_plans):
            raise ValueError("media planning Action cannot replan an already planned opportunity")
        if opportunity.opportunity_id in state.media_unrenderable_opportunity_ids:
            raise ValueError("media planning Action cannot replan an unrenderable opportunity")
        if (
            action.layer != "media_action"
            or action.payload_ref != opportunity.event_snapshot_ref
            or action.payload_hash != opportunity.event_snapshot_hash
            or action.idempotency_key != planning_request_id(opportunity.opportunity_id)
            or action.target != "provider:media-planner"
        ):
            raise ValueError("media planning Action is not exactly bound to its frozen opportunity")
    if action.kind == "media_repair":
        plan = next((item for item in state.media_plans if item.plan_id == action.intent_ref), None)
        repair_event_id = "event:media-v2:MediaRepairAuthorized:" + media_digest(
            {"role": "MediaRepairAuthorized", "stable": _media_repair_stable_from_action(action)}
        )
        # Repair authorization is an accepted, prior event in the same UoW;
        # the action never constitutes acceptance by itself.
        if plan is None or not any(
            item.event_id == repair_event_id for item in state.committed_world_event_refs
        ):
            raise ValueError("media repair Action requires its accepted repair authorization")
        inspection = next(
            (
                item
                for item in state.media_inspections
                if item.plan_id == plan.plan_id
                and not item.passed
                and item.repairable
                and action.idempotency_key
                == media_repair_attempt_id(
                    plan_id=plan.plan_id,
                    failed_artifact_hash=next(
                        (
                            artifact.artifact_hash
                            for artifact in state.media_artifacts
                            if artifact.artifact_id == item.artifact_id
                        ),
                        "",
                    ),
                )
            ),
            None,
        )
        if (
            inspection is None
            or action.layer != "media_action"
            or action.action_id
            != media_repair_action_id(
                world_id=event.world_id, repair_attempt_id=action.idempotency_key
            )
            or action.budget_reservation_id
            != media_repair_reservation_id(
                world_id=event.world_id, repair_attempt_id=action.idempotency_key
            )
            or action.payload_ref != inspection.inspection_payload_ref
            or action.payload_hash != inspection.inspection_payload_hash
            or action.target != "provider:media-renderer"
            or any(
                existing.kind == "media_repair" and existing.intent_ref == plan.plan_id
                for existing in state.actions
            )
        ):
            raise ValueError(
                "media repair Action is not exactly bound to its accepted failed inspection"
            )
    if action.kind == "media_delivery":
        binding = action.media_delivery_approval
        approval = next(
            (
                item
                for item in state.media_delivery_approvals
                if binding is not None
                and item.approval_id == binding.approval_id
                and item.entity_revision == binding.approval_revision
            ),
            None,
        )
        inspection = next(
            (
                item
                for item in state.media_inspections
                if approval is not None and item.inspection_id == approval.inspection_id
            ),
            None,
        )
        artifact = next(
            (
                item
                for item in state.media_artifacts
                if approval is not None and item.artifact_id == approval.artifact_id
            ),
            None,
        )
        if (
            binding is None
            or approval is None
            or inspection is None
            or artifact is None
            or approval.expires_at <= event.logical_time
            or not inspection.passed
            or action.layer != "external_action"
            or action.intent_ref != inspection.inspection_id
            or action.payload_ref != artifact.artifact_ref
            or action.payload_hash != artifact.artifact_hash
            or action.action_id
            != media_delivery_action_id(
                world_id=event.world_id,
                approval_id=approval.approval_id,
                approval_revision=approval.entity_revision,
            )
            or action.budget_reservation_id
            != media_delivery_reservation_id(
                world_id=event.world_id,
                approval_id=approval.approval_id,
                approval_revision=approval.entity_revision,
            )
            or action.idempotency_key
            != "media-delivery:" + approval.approval_id + ":" + str(approval.entity_revision)
        ):
            raise ValueError(
                "media delivery Action is not exactly bound to one active operator approval"
            )
    generic_manifest = next(
        (
            manifest
            for manifest in state.expression_plan_manifests
            if any(item.action.action_id == action.action_id for item in manifest.beats)
        ),
        None,
    )
    if generic_manifest is not None:
        beat = next(
            item for item in generic_manifest.beats if item.action.action_id == action.action_id
        )
        _require_previous_event(state, "BudgetReserved")
        if (
            action != beat.action
            or action.budget_reservation_id != beat.reservation.reservation_id
            or reservation != beat.reservation
            or action.expression_plan_id != generic_manifest.plan_id
            or action.expression_beat_id != beat.beat_id
            or not any(
                item.beat_id == beat.beat_id and item.action_id == action.action_id
                for item in state.expression_beats
            )
        ):
            raise ValueError("expression plan ActionAuthorized is not bound to its expression")
    minimal = next(
        (item for item in state.minimal_reply_manifests if item.action_id == action.action_id), None
    )
    if minimal is not None:
        _require_previous_event(state, "BudgetReserved")
        if (
            action.budget_reservation_id != minimal.reservation_id
            or action.payload_ref != minimal.message_payload_ref
            or action.payload_hash != minimal.message_payload_hash
            or action.intent_ref != f"{minimal.proposal_id}:{minimal.intent_id}"
            or action.expression_plan_id != minimal.plan_id
            or action.expression_beat_id != minimal.beat_id
            or canonical_minimal_reply_value_hash(action.model_dump(mode="json"))
            != minimal.action_hash
            or not any(
                beat.acceptance_id == minimal.acceptance_id
                and beat.beat_id == minimal.beat_id
                and beat.payload_ref == minimal.message_payload_ref
                for beat in state.expression_beats
            )
        ):
            raise ValueError("minimal reply ActionAuthorized is not bound to its expression")
    return state.model_copy(
        update={
            "actions": (*state.actions, action),
            "pending_actions": (*state.pending_actions, action),
        }
    )


def _photo_candidate_opened(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = PhotoCandidateOpenedPayload.model_validate_json(event.payload_json)
    candidate = payload.candidate
    if any(item.candidate_id == candidate.candidate_id for item in state.photo_candidates):
        raise ValueError("photo candidate identity is already registered")
    committed = {item.event_id: item.payload_hash for item in state.committed_world_event_refs}
    if not set(candidate.source_event_refs) <= set(committed):
        raise ValueError("photo candidate must be bound only to prior committed world events")
    if candidate.source_events and any(
        committed.get(source.event_ref) != source.payload_hash
        for source in candidate.source_events
    ):
        raise ValueError("P1 photo candidate source hashes do not bind committed world events")
    if candidate.opened_at is not None and candidate.opened_at != event.logical_time:
        raise ValueError("P1 photo candidate opening time must be authoritative")
    if candidate.opened_at is not None:
        candidate = candidate.model_copy(
            update={"opened_event_ref": event.event_id, "opened_event_payload_hash": event.payload_hash}
        )
    return state.model_copy(update={"photo_candidates": (*state.photo_candidates, candidate)})


def _photo_candidate_unrenderable(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Record a compiler refusal against the same available P1 candidate."""

    payload = PhotoCandidateUnrenderablePayload.model_validate_json(event.payload_json)
    candidate = next(
        (item for item in state.photo_candidates if item.candidate_id == payload.candidate_id), None
    )
    if (
        state.logical_time is None
        or event.logical_time != state.logical_time
        or candidate is None
        or candidate.status != "available"
        or candidate.entity_revision != payload.expected_entity_revision
        or candidate.opened_at is None
        or candidate.expires_at is None
        or candidate.opened_event_ref is None
        or candidate.opened_event_payload_hash is None
        or not any(
            item.proposal_event_ref == event.causation_id
            and item.candidate_id == candidate.candidate_id
            and item.expected_candidate_revision == candidate.entity_revision
            for item in state.proposal_revisions
        )
    ):
        raise ValueError("photo candidate unrenderable result is not current")
    return state.model_copy(
        update={
            "photo_candidates": _advance_media_candidate(
                state.photo_candidates,
                candidate_id=candidate.candidate_id,
                expected_status="available",
                next_status="unrenderable",
            )
        }
    )


def _photo_candidate_expired(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Close an available candidate after its immutable selection window."""

    payload = PhotoCandidateExpiredPayload.model_validate_json(event.payload_json)
    candidate = next(
        (item for item in state.photo_candidates if item.candidate_id == payload.candidate_id), None
    )
    if (
        state.logical_time is None
        or event.logical_time != state.logical_time
        or candidate is None
        or candidate.status != "available"
        or candidate.entity_revision != payload.expected_entity_revision
        or candidate.expires_at is None
        or candidate.expires_at > event.logical_time
        or candidate.opened_event_ref is None
        or candidate.opened_event_payload_hash is None
    ):
        raise ValueError("photo candidate expiry is not current")
    return state.model_copy(
        update={
            "photo_candidates": _advance_media_candidate(
                state.photo_candidates,
                candidate_id=candidate.candidate_id,
                expected_status="available",
                next_status="expired",
            )
        }
    )


def _image_evidence_declared(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Validate source lineage; declaration bytes remain in the immutable ledger."""

    payload = ImageEvidenceDeclaredPayload.model_validate_json(event.payload_json)
    source = next(
        (item for item in state.committed_world_event_refs if item.event_id == payload.source_event_ref),
        None,
    )
    if (
        state.logical_time is None
        or event.logical_time != state.logical_time
        or payload.declared_at != event.logical_time
        or source is None
        or source.event_type != payload.source_event_type
        or source.payload_hash != payload.source_event_payload_hash
        or source.event_type not in DECLARABLE_SOURCE_EVENT_TYPES
    ):
        raise ValueError("image evidence declaration source is not current")
    return state


def _recipient_scoped_image_evidence_declared(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    """Validate P3 private evidence without widening the P0/P2 declaration."""

    payload = RecipientScopedImageEvidenceDeclaredPayload.model_validate_json(event.payload_json)
    source = next(
        (item for item in state.committed_world_event_refs if item.event_id == payload.source_event_ref),
        None,
    )
    if (
        state.logical_time is None
        or event.logical_time != state.logical_time
        or payload.declared_at != event.logical_time
        or event.causation_id != payload.source_event_ref
        or source is None
        or source.event_type != payload.source_event_type
        or source.payload_hash != payload.source_event_payload_hash
        or source.event_type not in DECLARABLE_SOURCE_EVENT_TYPES
    ):
        raise ValueError("recipient-scoped image evidence declaration source is not current")
    return state


def _appearance_state_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Project a sparse visible state only from a prior committed source."""

    payload = AppearanceStateRecordedPayload.model_validate_json(event.payload_json)
    appearance = payload.state
    source = next(
        (item for item in state.committed_world_event_refs if item.event_id == appearance.source_event_ref),
        None,
    )
    history = tuple(item for item in state.appearance_states if item.subject_ref == appearance.subject_ref)
    if (
        state.logical_time is None
        or event.logical_time != state.logical_time
        or appearance.valid_from != event.logical_time
        or event.causation_id != appearance.source_event_ref
        or source is None
        or source.event_type != appearance.source_event_type
        or source.payload_hash != appearance.source_event_payload_hash
        or source.event_type not in APPEARANCE_SOURCE_EVENT_TYPES
        or source.logical_time > appearance.valid_from
        or appearance.entity_revision != len(history) + 1
        or (history and appearance.appearance_state_id != history[0].appearance_state_id)
        or (history and appearance.valid_from <= history[-1].valid_from)
    ):
        raise ValueError("appearance state source is not current")
    updated_history = history
    if history and (history[-1].valid_until is None or history[-1].valid_until > appearance.valid_from):
        updated_history = (*history[:-1], history[-1].model_copy(update={"valid_until": appearance.valid_from}))
    updated_by_subject = {item.entity_revision: item for item in updated_history}
    next_states = tuple(
        updated_by_subject.get(item.entity_revision, item)
        if item.subject_ref == appearance.subject_ref
        else item
        for item in state.appearance_states
    )
    return state.model_copy(update={"appearance_states": (*next_states, appearance)})


def _visible_physical_state_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Project only current-clock, source-bound short-lived physical evidence."""

    payload = VisiblePhysicalStateRecordedPayload.model_validate_json(event.payload_json)
    physical = payload.state
    source = next(
        (item for item in state.committed_world_event_refs if item.event_id == physical.source_event_ref),
        None,
    )
    history = tuple(
        item for item in state.visible_physical_states if item.subject_ref == physical.subject_ref
    )
    if (
        state.logical_time is None
        or event.logical_time != state.logical_time
        or physical.valid_from != event.logical_time
        or event.causation_id != physical.source_event_ref
        or source is None
        or source.event_type != physical.source_event_type
        or source.payload_hash != physical.source_event_payload_hash
        or source.event_type not in APPEARANCE_SOURCE_EVENT_TYPES
        or source.logical_time > physical.valid_from
        or physical.entity_revision != len(history) + 1
        or (history and physical.physical_state_id != history[0].physical_state_id)
        or (history and physical.valid_from <= history[-1].valid_from)
    ):
        raise ValueError("visible physical state source is not current")
    updated_history = history
    if history and history[-1].valid_until > physical.valid_from:
        updated_history = (*history[:-1], history[-1].model_copy(update={"valid_until": physical.valid_from}))
    updated_by_subject = {item.entity_revision: item for item in updated_history}
    next_states = tuple(
        updated_by_subject.get(item.entity_revision, item)
        if item.subject_ref == physical.subject_ref
        else item
        for item in state.visible_physical_states
    )
    return state.model_copy(update={"visible_physical_states": (*next_states, physical)})


def _random_draw_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    RandomDrawRecordedPayload.model_validate_json(event.payload_json)
    if state.logical_time is None or event.logical_time != state.logical_time:
        raise ValueError("random draw requires authoritative logical time")
    return state


def _media_selection_proposal_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Persist a model's P1 candidate choice without granting media effects."""

    if state.logical_time is None or event.logical_time != state.logical_time:
        raise ValueError("media selection proposal requires authoritative logical time")
    payload = MediaSelectionProposalRecordedPayload.model_validate_json(event.payload_json)
    if payload.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("media selection proposal must evaluate the current world revision")
    if payload.proposal_id in state.proposal_ids:
        raise ValueError("media selection proposal identity is already registered")
    candidate = next(
        (item for item in state.photo_candidates if item.candidate_id == payload.candidate_id), None
    )
    if (
        candidate is None
        or candidate.status != "available"
        or candidate.entity_revision != payload.expected_candidate_revision
        or candidate.opened_at is None
        or candidate.expires_at is None
        or candidate.expires_at <= event.logical_time
        or candidate.opened_event_ref is None
        or candidate.opened_event_payload_hash is None
        or media_candidate_authority_hash(candidate) != payload.candidate_authority_hash
    ):
        raise ValueError("media selection proposal candidate is not current")
    committed_hashes = {
        item.event_id: item.payload_hash for item in state.committed_world_event_refs
    }
    if any(
        committed_hashes.get(source.event_ref) != source.payload_hash
        for source in candidate.source_events
    ):
        raise ValueError("media selection proposal candidate source evidence is unavailable")
    return state.model_copy(
        update={
            "proposal_ids": (*state.proposal_ids, payload.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=payload.proposal_id,
                    evaluated_world_revision=payload.evaluated_world_revision,
                    proposal_event_ref=event.event_id,
                    proposal_event_payload_hash=event.payload_hash,
                    proposed_change_hash=payload.proposed_change_hash,
                    selection_hash=payload.selection_hash,
                    candidate_id=payload.candidate_id,
                    expected_candidate_revision=payload.expected_candidate_revision,
                ),
            ),
        }
    )


def _advance_media_candidate(
    candidates: tuple[PhotoCandidate, ...], *, candidate_id: str, expected_status: str, next_status: str
) -> tuple[PhotoCandidate, ...]:
    """Advance one candidate aggregate without inferring an alternate picture.

    The caller has already established the specific ledger event (opportunity,
    plan, artifact, or delivery) that justifies this transition.  A terminal
    candidate is never reopened by a retry in another media lane.
    """

    index = next((i for i, item in enumerate(candidates) if item.candidate_id == candidate_id), None)
    if index is None:
        # Pre-P1 snapshots persisted opportunities without a lifecycle
        # aggregate.  They remain readable for migration/replay, but no live
        # path can reach this branch because opportunity freezing requires the
        # candidate above.  Do not manufacture a state record during replay.
        return candidates
    candidate = candidates[index]
    if candidate.status != expected_status:
        raise ValueError("media candidate lifecycle transition is not current")
    updated = candidate.model_copy(
        update={"entity_revision": candidate.entity_revision + 1, "status": next_status}
    )
    return (*candidates[:index], updated, *candidates[index + 1 :])


def _media_opportunity_frozen(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MediaOpportunityFrozenPayload.model_validate_json(event.payload_json)
    opportunity = payload.opportunity
    candidate = next(
        (item for item in state.photo_candidates if item.candidate_id == opportunity.candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError("media opportunity requires an existing photo candidate")
    if any(item.opportunity_id == opportunity.opportunity_id for item in state.media_opportunities):
        raise ValueError("media opportunity identity is already registered")
    if opportunity.selection_proposal_id is not None:
        if not state.committed_world_event_refs:
            raise ValueError("P1 media opportunity requires adjacent acceptance")
        acceptance = state.committed_world_event_refs[-1]
        decision = next(
            (
                item
                for item in state.acceptance_decisions
                if item.proposal_id == opportunity.selection_proposal_id
                and item.acceptance_event_ref == acceptance.event_id
            ),
            None,
        )
        if (
            acceptance.event_type != "AcceptanceRecorded"
            or event.causation_id != acceptance.event_id
            or decision is None
            or decision.manifest_version
            not in {
                MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION,
                MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION,
            }
            or opportunity.selection_hash is None
            or decision.selection_hash != opportunity.selection_hash
            or opportunity.selected_candidate_revision is None
            or candidate.entity_revision != opportunity.selected_candidate_revision
            or candidate.status != "available"
        ):
            raise ValueError("P1 media opportunity does not bind accepted candidate selection")
    if (
        candidate.family != opportunity.family
        or candidate.privacy_ceiling != opportunity.privacy_ceiling
        or candidate.source_event_refs != (
            opportunity.candidate_source_event_refs or opportunity.source_event_refs
        )
        or candidate.status != "available"
        or (candidate.expires_at is not None and event.logical_time >= candidate.expires_at)
        or (
            candidate.ecology_category is not None
            and opportunity.ecology_category != candidate.ecology_category
        )
        or event.logical_time >= opportunity.expires_at
    ):
        raise ValueError("media opportunity does not exactly freeze its candidate")
    if opportunity.snapshot_source_events:
        committed_hashes = {
            item.event_id: item.payload_hash for item in state.committed_world_event_refs
        }
        if (
            tuple(item.event_ref for item in opportunity.snapshot_source_events)
            != opportunity.source_event_refs
            or any(
                committed_hashes.get(item.event_ref) != item.payload_hash
                for item in opportunity.snapshot_source_events
            )
        ):
            raise ValueError("media opportunity snapshot lineage is not committed")
    return state.model_copy(
        update={
            "photo_candidates": _advance_media_candidate(
                state.photo_candidates,
                candidate_id=candidate.candidate_id,
                expected_status="available",
                next_status="selected",
            ),
            "media_opportunities": (*state.media_opportunities, opportunity),
        }
    )


def _media_plan_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MediaPlanRecordedPayload.model_validate_json(event.payload_json)
    plan = payload.plan
    action = next((item for item in state.actions if item.action_id == payload.action_id), None)
    opportunity = next(
        (item for item in state.media_opportunities if item.opportunity_id == plan.opportunity_id),
        None,
    )
    receipt = next(
        (item for item in state.execution_receipts if item.receipt_id == payload.receipt_id), None
    )
    if (
        action is None
        or action.kind != "media_planning"
        or action.state != "delivered"
        or receipt is None
        or not receipt.is_terminal
        or receipt.observed_state != "delivered"
        or opportunity is None
        or plan.planning_request_id != action.idempotency_key
        or plan.event_snapshot_hash != opportunity.event_snapshot_hash
        or plan.family != opportunity.family
        or plan.media_machine_version != opportunity.media_machine_version
        or plan.inspection_contract_version != opportunity.inspection_contract_version
        or plan.media_lane != opportunity.media_lane
        or any(
            item.plan_id == plan.plan_id or item.opportunity_id == plan.opportunity_id
            for item in state.media_plans
        )
    ):
        raise ValueError("MediaPlanRecorded is not bound to one delivered planning Action")
    return state.model_copy(
        update={
            "photo_candidates": _advance_media_candidate(
                state.photo_candidates,
                candidate_id=opportunity.candidate_id,
                expected_status="selected",
                next_status="planned",
            ),
            "media_plans": (*state.media_plans, plan),
        }
    )


def _media_not_renderable_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MediaNotRenderableRecordedPayload.model_validate_json(event.payload_json)
    action = next((item for item in state.actions if item.action_id == payload.action_id), None)
    opportunity = next(
        (
            item
            for item in state.media_opportunities
            if item.opportunity_id == payload.result.opportunity_id
        ),
        None,
    )
    receipt = next(
        (item for item in state.execution_receipts if item.receipt_id == payload.receipt_id), None
    )
    if (
        action is None
        or action.kind != "media_planning"
        or action.state != "delivered"
        or receipt is None
        or receipt.observed_state != "delivered"
        or not receipt.is_terminal
        or opportunity is None
        or payload.result.planning_request_id != action.idempotency_key
        or payload.result.event_snapshot_hash != opportunity.event_snapshot_hash
        or any(item.opportunity_id == opportunity.opportunity_id for item in state.media_plans)
        or opportunity.opportunity_id in state.media_unrenderable_opportunity_ids
    ):
        raise ValueError("MediaNotRenderableRecorded is not bound to one delivered planning Action")
    return state.model_copy(
        update={
            "photo_candidates": _advance_media_candidate(
                state.photo_candidates,
                candidate_id=opportunity.candidate_id,
                expected_status="selected",
                next_status="unrenderable",
            ),
            "media_unrenderable_opportunity_ids": (
                *state.media_unrenderable_opportunity_ids,
                opportunity.opportunity_id,
            )
        }
    )


def _media_render_artifact_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MediaRenderArtifactRecordedPayload.model_validate_json(event.payload_json)
    artifact = payload.artifact
    action = next((item for item in state.actions if item.action_id == payload.action_id), None)
    receipt = next(
        (item for item in state.execution_receipts if item.receipt_id == payload.receipt_id), None
    )
    plan = next((item for item in state.media_plans if item.plan_id == artifact.plan_id), None)
    if (
        action is None
        or action.kind not in {"media_render", "media_repair"}
        or action.intent_ref != artifact.plan_id
        or action.state != "delivered"
        or receipt is None
        or receipt.action_id != action.action_id
        or receipt.observed_state != "delivered"
        or not receipt.is_terminal
        or plan is None
        or any(item.artifact_id == artifact.artifact_id for item in state.media_artifacts)
        or artifact.attempts != (2 if action.kind == "media_repair" else 1)
    ):
        raise ValueError("MediaRenderArtifactRecorded is not bound to one delivered render Action")
    prior_artifacts = tuple(
        item for item in state.media_artifacts if item.plan_id == artifact.plan_id
    )
    if action.kind == "media_render" and prior_artifacts:
        raise ValueError("MediaPlan may have only one original render artifact")
    if action.kind == "media_repair":
        if len(prior_artifacts) != 1:
            raise ValueError("media repair may create exactly one second artifact")
        prior = prior_artifacts[0]
        failed = next(
            (item for item in state.media_inspections if item.artifact_id == prior.artifact_id),
            None,
        )
        if failed is None or failed.passed or not failed.repairable:
            raise ValueError(
                "media repair artifact requires the first repairable inspection failure"
            )
    opportunity = next(
        (item for item in state.media_opportunities if item.opportunity_id == plan.opportunity_id), None
    )
    if opportunity is None:
        raise ValueError("media render artifact plan lacks its frozen opportunity")
    return state.model_copy(
        update={
            "photo_candidates": _advance_media_candidate(
                state.photo_candidates,
                candidate_id=opportunity.candidate_id,
                expected_status="planned" if action.kind == "media_render" else "generated",
                next_status="generated",
            ),
            "media_artifacts": (*state.media_artifacts, artifact),
        }
    )


def _media_inspection_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MediaInspectionRecordedPayload.model_validate_json(event.payload_json)
    inspection = payload.inspection
    action = next((item for item in state.actions if item.action_id == payload.action_id), None)
    receipt = next(
        (item for item in state.execution_receipts if item.receipt_id == payload.receipt_id), None
    )
    artifact = next(
        (item for item in state.media_artifacts if item.artifact_id == inspection.artifact_id), None
    )
    if (
        action is None
        or action.kind != "media_inspection"
        or action.intent_ref != inspection.artifact_id
        or action.state != "delivered"
        or receipt is None
        or receipt.action_id != action.action_id
        or receipt.observed_state != "delivered"
        or not receipt.is_terminal
        or artifact is None
        or inspection.plan_id != artifact.plan_id
        or inspection.inspection_action_id != action.action_id
        or any(
            item.inspection_id == inspection.inspection_id
            or item.artifact_id == inspection.artifact_id
            for item in state.media_inspections
        )
    ):
        raise ValueError("MediaInspectionRecorded is not bound to one delivered inspection Action")
    return state.model_copy(update={"media_inspections": (*state.media_inspections, inspection)})


def _media_repair_stable_from_action(action: Action) -> str:
    """The authorizer event id has the repair id as its stable component."""
    return action.idempotency_key


def _media_repair_authorized(state: ReducerState, event: WorldEvent) -> ReducerState:
    repair = MediaRepairAuthorizedPayload.model_validate_json(event.payload_json).repair
    plan = next((item for item in state.media_plans if item.plan_id == repair.plan_id), None)
    artifact = next(
        (item for item in state.media_artifacts if item.artifact_id == repair.failed_artifact_id),
        None,
    )
    inspection = next(
        (item for item in state.media_inspections if item.inspection_id == repair.inspection_id),
        None,
    )
    trigger = next(
        (item for item in state.trigger_processes if item.trigger_id == repair.trigger_id), None
    )
    if (
        plan is None
        or artifact is None
        or inspection is None
        or trigger is None
        or trigger.process_kind != "media_repair"
        or trigger.state != "claimed"
        or plan.opportunity_id != repair.opportunity_id
        or plan.event_snapshot_hash != repair.event_snapshot_hash
        or artifact.plan_id != plan.plan_id
        or artifact.artifact_hash != repair.failed_artifact_hash
        or inspection.plan_id != plan.plan_id
        or inspection.artifact_id != artifact.artifact_id
        or inspection.passed
        or not inspection.repairable
        or inspection.inspection_payload_hash != repair.inspection_payload_hash
        or repair.defect_scope != inspection.repair_scope
        or repair.trigger_id
        != media_repair_trigger_id(world_id=event.world_id, inspection_id=inspection.inspection_id)
        or repair.repair_attempt_id
        != media_repair_attempt_id(
            plan_id=plan.plan_id, failed_artifact_hash=artifact.artifact_hash
        )
        or repair.action_id
        != media_repair_action_id(
            world_id=event.world_id, repair_attempt_id=repair.repair_attempt_id
        )
        or repair.reservation_id
        != media_repair_reservation_id(
            world_id=event.world_id, repair_attempt_id=repair.repair_attempt_id
        )
        or any(
            item.kind == "media_repair" and item.intent_ref == plan.plan_id
            for item in state.actions
        )
    ):
        raise ValueError(
            "MediaRepairAuthorized is not one bounded accepted repair of a failed inspection"
        )
    return state


def _media_preview_generated(state: ReducerState, event: WorldEvent) -> ReducerState:
    preview = MediaPreviewGeneratedPayload.model_validate_json(event.payload_json).preview
    inspection = next(
        (item for item in state.media_inspections if item.inspection_id == preview.inspection_id),
        None,
    )
    opportunity = next(
        (
            item
            for item in state.media_opportunities
            if item.opportunity_id
            == next(
                (
                    plan.opportunity_id
                    for plan in state.media_plans
                    if plan.plan_id == preview.plan_id
                ),
                None,
            )
        ),
        None,
    )
    if (
        inspection is None
        or not inspection.passed
        or inspection.plan_id != preview.plan_id
        or inspection.artifact_id != preview.artifact_id
        or opportunity is None
        or opportunity.delivery_mode != "preview"
        or preview.delivery_mode != "preview"
        or preview.recipient_ref != opportunity.recipient_ref
        or any(
            item.preview_id == preview.preview_id or item.inspection_id == preview.inspection_id
            for item in state.media_previews
        )
    ):
        raise ValueError(
            "MediaPreviewGenerated may only materialize an inspected preview; it is never delivery"
        )
    return state.model_copy(update={"media_previews": (*state.media_previews, preview)})


def _media_preview_failed(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MediaPreviewFailedPayload.model_validate_json(event.payload_json)
    if payload.plan_id in state.media_failed_plan_ids:
        raise ValueError("media preview failure is already recorded")
    if not any(item.plan_id == payload.plan_id for item in state.media_plans):
        raise ValueError("media preview failure requires frozen plan")
    if payload.inspection_id is not None:
        inspection = next(
            (
                item
                for item in state.media_inspections
                if item.inspection_id == payload.inspection_id
            ),
            None,
        )
        if inspection is None or inspection.plan_id != payload.plan_id or inspection.passed:
            raise ValueError("media preview failure does not bind a failed inspection")
    return state.model_copy(
        update={"media_failed_plan_ids": (*state.media_failed_plan_ids, payload.plan_id)}
    )


def _media_automatic_delivery_approved(state: ReducerState, event: WorldEvent) -> ReducerState:
    approval = MediaAutomaticDeliveryApprovedPayload.model_validate_json(
        event.payload_json
    ).approval
    plan = next((item for item in state.media_plans if item.plan_id == approval.plan_id), None)
    inspection = next(
        (item for item in state.media_inspections if item.inspection_id == approval.inspection_id),
        None,
    )
    artifact = next(
        (item for item in state.media_artifacts if item.artifact_id == approval.artifact_id), None
    )
    opportunity = next(
        (
            item
            for item in state.media_opportunities
            if item.opportunity_id == (plan.opportunity_id if plan else None)
        ),
        None,
    )
    prior = tuple(
        item for item in state.media_delivery_approvals if item.approval_id == approval.approval_id
    )
    if (
        event.logical_time != approval.approved_at
        or event.actor != approval.operator_ref
        or plan is None
        or inspection is None
        or artifact is None
        or opportunity is None
        or opportunity.delivery_mode != "automatic"
        or opportunity.family != approval.family
        or opportunity.recipient_ref != approval.recipient_ref
        or plan.media_machine_version != approval.media_machine_version
        or plan.inspection_contract_version != approval.inspection_contract_version
        or inspection.plan_id != plan.plan_id
        or not inspection.passed
        or inspection.artifact_id != artifact.artifact_id
        or artifact.plan_id != plan.plan_id
        or artifact.artifact_hash != approval.artifact_hash
        or (prior and approval.entity_revision != prior[-1].entity_revision + 1)
        or (not prior and approval.entity_revision != 1)
    ):
        raise ValueError(
            "MediaAutomaticDeliveryApproved must pin one passed automatic-media inspection"
        )
    return state.model_copy(
        update={"media_delivery_approvals": (*state.media_delivery_approvals, approval)}
    )


def _media_delivery_shared(state: ReducerState, event: WorldEvent) -> ReducerState:
    delivery = MediaDeliverySharedPayload.model_validate_json(event.payload_json).delivery
    action = next((item for item in state.actions if item.action_id == delivery.action_id), None)
    receipt = next(
        (item for item in state.execution_receipts if item.receipt_id == delivery.receipt_id), None
    )
    approval = next(
        (
            item
            for item in state.media_delivery_approvals
            if item.approval_id == delivery.approval_id
            and item.entity_revision == delivery.approval_revision
        ),
        None,
    )
    plan = next((item for item in state.media_plans if item.plan_id == delivery.plan_id), None)
    inspection = next(
        (item for item in state.media_inspections if item.inspection_id == delivery.inspection_id),
        None,
    )
    artifact = next(
        (item for item in state.media_artifacts if item.artifact_id == delivery.artifact_id), None
    )
    if (
        action is None
        or action.kind != "media_delivery"
        or action.state != "delivered"
        or action.media_delivery_approval is None
        or receipt is None
        or receipt.action_id != action.action_id
        or receipt.observed_state != "delivered"
        or not receipt.is_terminal
        or approval is None
        or plan is None
        or inspection is None
        or artifact is None
        or action.media_delivery_approval.approval_id != approval.approval_id
        or action.media_delivery_approval.approval_revision != approval.entity_revision
        or action.intent_ref != approval.inspection_id
        or action.payload_ref != artifact.artifact_ref
        or action.payload_hash != artifact.artifact_hash
        or approval.plan_id != plan.plan_id
        or approval.inspection_id != inspection.inspection_id
        or approval.artifact_id != artifact.artifact_id
        or approval.artifact_hash != artifact.artifact_hash
        or delivery.plan_id != plan.plan_id
        or delivery.inspection_id != inspection.inspection_id
        or delivery.artifact_id != artifact.artifact_id
        or delivery.artifact_hash != artifact.artifact_hash
        or delivery.recipient_ref != approval.recipient_ref
        or delivery.delivery_id
        != media_delivery_id(action_id=action.action_id, receipt_id=receipt.receipt_id)
        or any(
            item.delivery_id == delivery.delivery_id or item.action_id == action.action_id
            for item in state.media_deliveries
        )
    ):
        raise ValueError(
            "MediaDeliveryShared requires one delivered approved media-delivery Action"
        )
    opportunity = next(
        (item for item in state.media_opportunities if item.opportunity_id == plan.opportunity_id), None
    )
    if opportunity is None:
        raise ValueError("media delivery plan lacks its frozen opportunity")
    return state.model_copy(
        update={
            "photo_candidates": _advance_media_candidate(
                state.photo_candidates,
                candidate_id=opportunity.candidate_id,
                expected_status="generated",
                next_status="shared",
            ),
            "media_deliveries": (*state.media_deliveries, delivery),
        }
    )


def _interaction_bid_proposal_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = InteractionBidProposalRecordedPayload.model_validate_json(event.payload_json)
    if payload.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("interaction bid proposal must evaluate current world revision")
    if any(
        item.interaction_bid_proposal_id == payload.interaction_bid_proposal_id
        for item in state.interaction_bid_proposals
    ):
        raise ValueError("interaction bid proposal identity is already registered")
    delivery = next(
        (item for item in state.media_deliveries if item.delivery_id == payload.delivery_id), None
    )
    source = next(
        (
            item
            for item in state.committed_world_event_refs
            if item.event_id == payload.delivery_event_ref
        ),
        None,
    )
    trigger = next(
        (
            item
            for item in state.trigger_processes
            if item.trigger_id == payload.deliberation_trigger_id
        ),
        None,
    )
    if (
        delivery is None
        or source is None
        or source.event_type != "MediaDeliveryShared"
        or source.payload_hash != payload.delivery_event_payload_hash
        or trigger is None
        or trigger.process_kind != "media_delivery_interaction"
        or trigger.state != "claimed"
        or trigger.claim_lease is None
        or trigger.source_evidence_ref != payload.delivery_event_ref
        or not any(item.ref_id == payload.delivery_event_ref for item in payload.evidence_refs)
        or any(item.bid_id == payload.bid_id for item in state.interaction_bids)
    ):
        raise ValueError("interaction bid proposal is not bound to one claimed media delivery")
    return state.model_copy(
        update={
            "interaction_bid_proposals": (
                *state.interaction_bid_proposals,
                InteractionBidProposalProjection.model_validate(payload.model_dump()),
            ),
            "proposal_ids": (*state.proposal_ids, payload.interaction_bid_proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=payload.interaction_bid_proposal_id,
                    evaluated_world_revision=payload.evaluated_world_revision,
                ),
            ),
        }
    )


def _interaction_bid_opened(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = InteractionBidOpenedPayload.model_validate_json(event.payload_json)
    proposal = next(
        (
            item
            for item in state.interaction_bid_proposals
            if item.interaction_bid_proposal_id == payload.proposal_id
        ),
        None,
    )
    bid = payload.bid
    if (
        proposal is None
        or payload.evaluated_world_revision != len(state.committed_world_event_refs) - 1
        or proposal.change_id != payload.change_id
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or bid.bid_id != proposal.bid_id
        or bid.delivery_id != proposal.delivery_id
        or bid.delivery_event_ref != proposal.delivery_event_ref
        or bid.delivery_event_payload_hash != proposal.delivery_event_payload_hash
        or bid.deliberation_trigger_id != proposal.deliberation_trigger_id
        or bid.goal != proposal.goal
        or bid.hoped_response != proposal.hoped_response
        or bid.pressure_bp != proposal.pressure_bp
        or bid.audience_ref != proposal.audience_ref
        or bid.due_at != proposal.due_at
        or bid.evidence_refs != proposal.evidence_refs
        or bid.opened_at != event.logical_time
        or any(item.bid_id == bid.bid_id for item in state.interaction_bids)
    ):
        raise ValueError("InteractionBidOpened does not match accepted delivered-media proposal")
    return state.model_copy(update={"interaction_bids": (*state.interaction_bids, bid)})


def _media_thread_proposal_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MediaDeliveryThreadProposalRecordedPayload.model_validate_json(event.payload_json)
    source = next(
        (
            item
            for item in state.committed_world_event_refs
            if item.event_id == payload.delivery_event_ref
        ),
        None,
    )
    trigger = next(
        (
            item
            for item in state.trigger_processes
            if item.trigger_id == payload.deliberation_trigger_id
        ),
        None,
    )
    if (
        payload.evaluated_world_revision != len(state.committed_world_event_refs)
        or any(
            item.media_thread_proposal_id == payload.media_thread_proposal_id
            for item in state.media_thread_proposals
        )
        or source is None
        or source.event_type != "MediaDeliveryShared"
        or source.payload_hash != payload.delivery_event_payload_hash
        or trigger is None
        or trigger.process_kind != "media_delivery_interaction"
        or trigger.state != "claimed"
        or trigger.claim_lease is None
        or trigger.source_evidence_ref != payload.delivery_event_ref
        or any(item.thread_id == payload.thread_after.thread_id for item in state.threads)
        and payload.operation == "open"
    ):
        raise ValueError("media delivery thread proposal is not source-bound current authority")
    return state.model_copy(
        update={
            "media_thread_proposals": (
                *state.media_thread_proposals,
                MediaDeliveryThreadProposalProjection.model_validate(payload.model_dump()),
            )
        }
    )


def _media_thread_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = MediaDeliveryThreadChangedPayload.model_validate_json(event.payload_json)
    proposal = next(
        (
            item
            for item in state.media_thread_proposals
            if item.media_thread_proposal_id == payload.proposal_id
        ),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if (
        proposal is None
        or decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
        or not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
        or proposal.operation != payload.operation
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.thread_before != payload.thread_before
        or proposal.thread_after != payload.thread_after
        or proposal.evidence_refs != payload.evidence_refs
        or proposal.policy_refs != payload.policy_refs
        or payload.thread_after.origin.accepted_event_ref != event.event_id
    ):
        raise ValueError(
            "media delivery thread transition does not match accepted dedicated proposal"
        )
    # Delivery itself is a committed world-time authority.  Normal production
    # heads have ``logical_time``; recovery fixtures may only retain the source
    # event, in which case the immutable event time is the sole fallback.
    logical_time = state.logical_time or event.logical_time
    threads, transitions = reduce_thread(
        state.threads,
        state.thread_transitions,
        payload,
        event_type=event.event_type,
        logical_time=logical_time,
    )
    return state.model_copy(
        update={
            "threads": threads,
            "thread_transitions": transitions,
            "media_thread_proposals": tuple(
                item for item in state.media_thread_proposals if item != proposal
            ),
        }
    )


def _provider_media_grant_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = ProviderMediaGrantRecordedPayload.model_validate_json(event.payload_json)
    grant = payload.grant
    if any(item.grant_id == grant.grant_id for item in state.provider_media_grants):
        raise ValueError("provider media grant identity is already registered")
    if event.logical_time != grant.issued_at:
        raise ValueError("provider media grant event time does not bind grant issue time")
    validate_provider_media_grant_record(
        grant=grant, projection=state, logical_time=event.logical_time
    )
    return state.model_copy(update={"provider_media_grants": (*state.provider_media_grants, grant)})


def _budget_reserved(state: ReducerState, event: WorldEvent) -> ReducerState:
    reservation = _model_from_payload(event, "reservation", BudgetReservation)
    if any(item.reservation_id == reservation.reservation_id for item in state.budget_reservations):
        raise ValueError(f"budget reservation {reservation.reservation_id!r} already exists")
    if reservation.state != "reserved":
        raise ValueError("BudgetReserved requires reserved state")
    generic_manifest = next(
        (
            manifest
            for manifest in state.expression_plan_manifests
            if any(
                item.reservation.reservation_id == reservation.reservation_id
                for item in manifest.beats
            )
        ),
        None,
    )
    if generic_manifest is not None:
        beat = next(
            item
            for item in generic_manifest.beats
            if item.reservation.reservation_id == reservation.reservation_id
        )
        _require_previous_event(state, "ExpressionBeatAuthorized")
        if (
            reservation != beat.reservation
            or reservation.category != "chat"
            or not any(
                item.beat_id == beat.beat_id and item.action_id == beat.action.action_id
                for item in state.expression_beats
            )
        ):
            raise ValueError("expression plan reservation is not bound to its manifest")
    minimal = next(
        (
            item
            for item in state.minimal_reply_manifests
            if item.reservation_id == reservation.reservation_id
        ),
        None,
    )
    if minimal is not None:
        _require_previous_event(state, "ExpressionBeatAuthorized")
        if (
            reservation.action_id != minimal.action_id
            or reservation.category != "chat"
            or canonical_minimal_reply_value_hash(reservation.model_dump(mode="json"))
            != minimal.reservation_hash
        ):
            raise ValueError("minimal reply reservation is not bound to its manifest")
    account_index = next(
        (
            index
            for index, account in enumerate(state.budget_accounts)
            if account.account_id == reservation.account_id
        ),
        None,
    )
    if account_index is None:
        raise ValueError("BudgetReserved requires an active budget account")
    account = state.budget_accounts[account_index]
    if account.category != reservation.category:
        raise ValueError("budget reservation category does not match its account")
    if account.spent + account.reserved + reservation.amount_limit > account.limit:
        raise ValueError("budget account has insufficient available capacity")
    updated_account = account.model_copy(
        update={"reserved": account.reserved + reservation.amount_limit}
    )
    return state.model_copy(
        update={
            "budget_accounts": (
                *state.budget_accounts[:account_index],
                updated_account,
                *state.budget_accounts[account_index + 1 :],
            ),
            "budget_reservations": (*state.budget_reservations, reservation),
        }
    )


def _budget_account_configured(state: ReducerState, event: WorldEvent) -> ReducerState:
    account = _model_from_payload(event, "account", BudgetAccount)
    if any(item.account_id == account.account_id for item in state.budget_accounts):
        raise ValueError(f"budget account {account.account_id!r} already exists")
    if account.reserved != 0 or account.spent != 0 or account.overrun != 0:
        raise ValueError("new budget account must start with zero balances")
    return state.model_copy(update={"budget_accounts": (*state.budget_accounts, account)})


def _action_transitioned(
    state: ReducerState, event: WorldEvent, *, target: ActionState
) -> ReducerState:
    action_id = event.payload().get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError(f"{event.event_type} requires action_id")
    for index, existing in enumerate(state.actions):
        if existing.action_id == action_id:
            transitioned = transition_action(existing, target)
            if target != "dispatch_started":
                transitioned = transitioned.model_copy(update={"dispatch_pending": None})
            return _replace_action(state, index=index, action=transitioned)
    raise ValueError(f"action {action_id!r} does not exist")


def _action_claimed(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_id = _required_action_id(event)
    if "claim_lease" not in event.payload():
        raise ValueError("ActionClaimed requires claim_lease")
    lease = _model_from_payload(event, "claim_lease", ClaimLease)
    if lease.acquired_at != event.created_at:
        raise ValueError("claim lease acquired_at must equal event created_at")
    for index, existing in enumerate(state.actions):
        if existing.action_id != action_id:
            continue
        transitioned = transition_action(existing, "claimed")
        transitioned = transitioned.model_copy(update={"claim_lease": lease})
        return _replace_action(state, index=index, action=transitioned)
    raise ValueError(f"action {action_id!r} does not exist")


def _action_reclaimed(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_id = _required_action_id(event)
    if "claim_lease" not in event.payload():
        raise ValueError("ActionReclaimed requires claim_lease")
    lease = _model_from_payload(event, "claim_lease", ClaimLease)
    if lease.acquired_at != event.created_at:
        raise ValueError("claim lease acquired_at must equal event created_at")
    for index, existing in enumerate(state.actions):
        if existing.action_id != action_id:
            continue
        if existing.state != "claimed" or existing.claim_lease is None:
            raise ValueError(f"action {action_id!r} has no reclaimable claim lease")
        if lease.attempt_id == existing.claim_lease.attempt_id:
            raise ValueError("reclaimed action requires a new attempt_id")
        if lease.acquired_at < existing.claim_lease.expires_at:
            raise ValueError(f"action {action_id!r} claim lease has not expired")
        return _replace_action(
            state,
            index=index,
            action=existing.model_copy(update={"claim_lease": lease}),
        )
    raise ValueError(f"action {action_id!r} does not exist")


def _action_dispatch_started(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_id = _required_action_id(event)
    payload = event.payload()
    proof = ActionDispatchClaim.model_validate_json(
        json.dumps(
            {
                "owner_id": payload.get("owner_id"),
                "attempt_id": payload.get("attempt_id"),
                "started_at": payload.get("started_at"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if proof.started_at != event.created_at:
        raise ValueError("dispatch started_at must equal event created_at")
    for index, existing in enumerate(state.actions):
        if existing.action_id != action_id:
            continue
        lease = existing.claim_lease
        if lease is None or (lease.owner_id, lease.attempt_id) != (
            proof.owner_id,
            proof.attempt_id,
        ):
            raise ValueError("ActionDispatchStarted requires the active claim lease")
        if proof.started_at < lease.acquired_at:
            raise ValueError("dispatch cannot start before the claim lease is acquired")
        if proof.started_at >= lease.expires_at:
            raise ValueError("dispatch cannot start after the claim lease expired")
        transitioned = transition_action(existing, "dispatch_started")
        return _replace_action(state, index=index, action=transitioned)
    raise ValueError(f"action {action_id!r} does not exist")


def _action_dispatch_pending(state: ReducerState, event: WorldEvent) -> ReducerState:
    pending = _model_from_payload(event, "pending", DispatchPending)
    for index, existing in enumerate(state.actions):
        if existing.action_id != pending.action_id:
            continue
        if existing.state != "dispatch_started":
            raise ValueError("ActionDispatchPending requires dispatch_started Action")
        if pending.idempotency_key != existing.idempotency_key:
            raise ValueError("ActionDispatchPending has another Action identity")
        return _replace_action(
            state,
            index=index,
            action=existing.model_copy(update={"dispatch_pending": pending}),
        )
    raise ValueError(f"action {pending.action_id!r} does not exist")


def _required_action_id(event: WorldEvent) -> str:
    action_id = event.payload().get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError(f"{event.event_type} requires action_id")
    return action_id


def _replace_action(state: ReducerState, *, index: int, action: Action) -> ReducerState:
    actions = (
        *state.actions[:index],
        action,
        *state.actions[index + 1 :],
    )
    pending = tuple(
        candidate for candidate in actions if candidate.state not in TERMINAL_ACTION_STATES
    )
    return state.model_copy(
        update={
            "actions": actions,
            "pending_actions": pending,
        }
    )


def _model_from_payload(event: WorldEvent, key: str, model_type: type[Any]) -> Any:
    value = event.payload().get(key)
    return model_type.model_validate_json(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _external_observation_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    result = _model_from_payload(event, "result", ExternalObservation)
    if any(item.result_id == result.result_id for item in state.pending_external_observations):
        raise ValueError(f"external result {result.result_id!r} is already pending")
    return state.model_copy(
        update={
            "pending_external_observations": (
                *state.pending_external_observations,
                result,
            )
        }
    )


def _external_observation_processed(state: ReducerState, event: WorldEvent) -> ReducerState:
    result_id = event.payload().get("result_id")
    if not isinstance(result_id, str) or not result_id:
        raise ValueError("ExternalObservationProcessed requires result_id")
    remaining = tuple(
        item for item in state.pending_external_observations if item.result_id != result_id
    )
    if len(remaining) == len(state.pending_external_observations):
        raise ValueError(f"external result {result_id!r} is not pending")
    return state.model_copy(update={"pending_external_observations": remaining})


def _execution_receipt_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    receipt = _model_from_payload(event, "receipt", ExecutionReceipt)
    if any(item.receipt_id == receipt.receipt_id for item in state.execution_receipts):
        raise ValueError(f"execution receipt {receipt.receipt_id!r} already exists")
    return state.model_copy(update={"execution_receipts": (*state.execution_receipts, receipt)})


def _tool_request_accepted(state: ReducerState, event: WorldEvent) -> ReducerState:
    request = ToolRequestAcceptedPayload.model_validate_json(event.payload_json).request
    source = next(
        (item for item in state.committed_world_event_refs if item.event_id == request.source_event_ref),
        None,
    )
    if (
        source is None
        or source.world_revision != request.source_world_revision
        or source.payload_hash != request.source_payload_hash
        or any(item.request_id == request.request_id for item in state.read_only_tool_requests)
        or any(item.action_id == request.action_id for item in state.read_only_tool_requests)
    ):
        raise ValueError("read-only tool request is not bound to committed source authority")
    return state.model_copy(
        update={"read_only_tool_requests": (*state.read_only_tool_requests, request)}
    )


def _tool_result_accepted(state: ReducerState, event: WorldEvent) -> ReducerState:
    result = ToolResultAcceptedPayload.model_validate_json(event.payload_json).result
    request = next(
        (item for item in state.read_only_tool_requests if item.request_id == result.request_id),
        None,
    )
    receipt_event = next(
        (item for item in state.committed_world_event_refs if item.event_id == result.receipt_event_ref),
        None,
    )
    action = next((item for item in state.actions if item.action_id == result.action_id), None)
    receipt = next((item for item in state.execution_receipts if item.result_id == result.external_result_id), None)
    if (
        request is None
        or request.action_id != result.action_id
        or action is None
        or action.kind != "read_only_tool"
        or action.layer != "read_only_tool"
        or action.state != "delivered"
        or receipt is None
        or receipt.action_id != result.action_id
        or receipt.result_ref != result.result_ref
        or receipt.result_hash != result.result_hash
        or receipt_event is None
        or receipt_event.event_type != "ExecutionReceiptRecorded"
        or receipt_event.payload_hash != result.receipt_event_payload_hash
        or result.accepted_event_ref != event.event_id
        or any(item.result_id == result.result_id for item in state.tool_results)
    ):
        raise ValueError("tool result is not bound to its delivered Action receipt")
    return state.model_copy(update={"tool_results": (*state.tool_results, result)})


def _perception_request_accepted(state: ReducerState, event: WorldEvent) -> ReducerState:
    request = PerceptionRequestAcceptedPayload.model_validate_json(event.payload_json).request
    source = next((item for item in state.committed_world_event_refs if item.event_id == request.source_event_ref), None)
    if (
        source is None or source.world_revision != request.source_world_revision
        or source.payload_hash != request.source_payload_hash
        or any(item.request_id == request.request_id for item in state.perception_requests)
        or any(item.action_id == request.action_id for item in state.perception_requests)
    ):
        raise ValueError("perception request is not bound to committed source authority")
    return state.model_copy(update={"perception_requests": (*state.perception_requests, request)})


def _perception_result_accepted(state: ReducerState, event: WorldEvent) -> ReducerState:
    result = PerceptionResultAcceptedPayload.model_validate_json(event.payload_json).result
    request = next((item for item in state.perception_requests if item.request_id == result.request_id), None)
    receipt_event = next((item for item in state.committed_world_event_refs if item.event_id == result.receipt_event_ref), None)
    action = next((item for item in state.actions if item.action_id == result.action_id), None)
    receipt = next((item for item in state.execution_receipts if item.result_id == result.external_result_id), None)
    if (
        request is None or request.action_id != result.action_id or request.analysis_kind != result.analysis_kind
        or request.content_privacy_class != result.content_privacy_class or action is None
        or action.kind != result.analysis_kind or action.layer != "perception_tool" or action.state != "delivered"
        or receipt is None or receipt.action_id != result.action_id or receipt.result_ref != result.result_ref
        or receipt.result_hash != result.result_hash or receipt_event is None
        or receipt_event.event_type != "ExecutionReceiptRecorded" or receipt_event.payload_hash != result.receipt_event_payload_hash
        or result.accepted_event_ref != event.event_id or any(item.result_id == result.result_id for item in state.perception_results)
    ):
        raise ValueError("perception result is not bound to its delivered Action receipt")
    return state.model_copy(update={"perception_results": (*state.perception_results, result)})


def _budget_settlement_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    settlement = _model_from_payload(event, "settlement", BudgetSettlement)
    if any(item.settlement_id == settlement.settlement_id for item in state.budget_settlements):
        raise ValueError(f"budget result {settlement.result_id!r} already exists")
    reservation_index = next(
        (
            index
            for index, item in enumerate(state.budget_reservations)
            if item.reservation_id == settlement.reservation_id
        ),
        None,
    )
    if reservation_index is None:
        raise ValueError("budget settlement requires an existing reservation")
    reservation = state.budget_reservations[reservation_index]
    if reservation.action_id != settlement.action_id:
        raise ValueError("budget reservation cannot be settled by this result")
    if settlement.previous_cost != reservation.settled_cost:
        raise ValueError("budget settlement previous_cost is stale")
    account_index = next(
        (
            index
            for index, account in enumerate(state.budget_accounts)
            if account.account_id == reservation.account_id
        ),
        None,
    )
    if account_index is None:
        raise ValueError("budget settlement account does not exist")
    account = state.budget_accounts[account_index]
    if settlement.settlement_kind == "reconciliation_adjustment":
        if reservation.state == "reserved":
            raise ValueError("budget adjustment requires an existing terminal settlement")
        reserved_after = account.reserved
    else:
        if reservation.state != "reserved":
            raise ValueError("budget reservation is already terminal")
        reserved_after = account.reserved - reservation.amount_limit
    spent_after = account.spent + settlement.cost_delta
    if reserved_after < 0 or spent_after < 0:
        raise ValueError("budget settlement would make account totals negative")
    updated_account = account.model_copy(
        update={
            "reserved": reserved_after,
            "spent": spent_after,
            "overrun": max(0, spent_after - account.limit),
        }
    )
    updated_reservation = reservation.model_copy(
        update={"state": settlement.state, "settled_cost": settlement.cost_actual}
    )
    return state.model_copy(
        update={
            "budget_accounts": (
                *state.budget_accounts[:account_index],
                updated_account,
                *state.budget_accounts[account_index + 1 :],
            ),
            "budget_reservations": (
                *state.budget_reservations[:reservation_index],
                updated_reservation,
                *state.budget_reservations[reservation_index + 1 :],
            ),
            "budget_settlements": (*state.budget_settlements, settlement),
        }
    )


def _reconciliation_required(state: ReducerState, event: WorldEvent) -> ReducerState:
    reconciliation = _model_from_payload(event, "reconciliation", ActionReconciliation)
    if any(
        item.reconciliation_id == reconciliation.reconciliation_id for item in state.reconciliations
    ):
        raise ValueError(f"reconciliation {reconciliation.result_id!r} already exists")
    return state.model_copy(update={"reconciliations": (*state.reconciliations, reconciliation)})


def _trigger_process_completed(state: ReducerState, event: WorldEvent) -> ReducerState:
    trigger_id = event.payload().get("trigger_id")
    if not isinstance(trigger_id, str) or not trigger_id:
        raise ValueError("TriggerProcessCompleted requires trigger_id")
    process_index = next(
        (
            index
            for index, process in enumerate(state.trigger_processes)
            if process.trigger_id == trigger_id
        ),
        None,
    )
    if process_index is None:
        raise ValueError(f"trigger {trigger_id!r} was not claimed")
    process = state.trigger_processes[process_index]
    if process.state != "claimed":
        raise ValueError(f"trigger {trigger_id!r} is already completed")
    owner_id = event.payload().get("owner_id")
    attempt_id = event.payload().get("attempt_id")
    completed_at_raw = event.payload().get("completed_at")
    if owner_id != process.claim_lease.owner_id or attempt_id != process.claim_lease.attempt_id:
        raise ValueError("trigger completion does not own the active claim lease")
    if not isinstance(completed_at_raw, str):
        raise ValueError("TriggerProcessCompleted requires completed_at")
    completed_at = datetime.fromisoformat(completed_at_raw)
    if not (process.claim_lease.acquired_at <= completed_at <= process.claim_lease.expires_at):
        raise ValueError("trigger completion occurred outside its claim lease")
    completed = process.model_copy(
        update={
            "state": "terminal",
            "runtime_outcome_ref": event.payload().get("runtime_outcome_ref"),
        }
    )
    return state.model_copy(
        update={
            "trigger_processes": (
                *state.trigger_processes[:process_index],
                completed,
                *state.trigger_processes[process_index + 1 :],
            ),
            "completed_trigger_ids": (*state.completed_trigger_ids, trigger_id),
        }
    )


def _trigger_process_reclaimed(state: ReducerState, event: WorldEvent) -> ReducerState:
    replacement = _model_from_payload(event, "process", TriggerProcess)
    process_index = next(
        (
            index
            for index, process in enumerate(state.trigger_processes)
            if process.trigger_id == replacement.trigger_id
        ),
        None,
    )
    if process_index is None:
        raise ValueError("cannot reclaim an unknown trigger")
    existing = state.trigger_processes[process_index]
    if existing.state != "claimed":
        raise ValueError("cannot reclaim a terminal trigger")
    if replacement.state != "claimed":
        raise ValueError("reclaimed trigger must remain claimed")
    if (
        replacement.trigger_ref != existing.trigger_ref
        or replacement.process_kind != existing.process_kind
        or replacement.source_evidence_ref != existing.source_evidence_ref
    ):
        raise ValueError("reclaim cannot change trigger identity")
    if replacement.claim_lease.acquired_at < existing.claim_lease.expires_at:
        raise ValueError("cannot reclaim before the active lease expires")
    if replacement.attempt_ids[:-1] != existing.attempt_ids:
        raise ValueError("reclaimed trigger must preserve attempt lineage")
    if len(replacement.attempt_ids) != len(existing.attempt_ids) + 1:
        raise ValueError("reclaim must append exactly one attempt")
    return state.model_copy(
        update={
            "trigger_processes": (
                *state.trigger_processes[:process_index],
                replacement,
                *state.trigger_processes[process_index + 1 :],
            )
        }
    )


def _trigger_process_claimed(state: ReducerState, event: WorldEvent) -> ReducerState:
    process = _model_from_payload(event, "process", TriggerProcess)
    if process.state != "claimed":
        raise ValueError("TriggerProcessClaimed requires claimed state")
    if process.process_kind in {
        "npc_world_appraisal",
        "interaction_appraisal",
        "interaction_fact",
        "affect_deliberation",
        "outcome_deliberation",
        "expression_reconsideration",
        "life_ecology",
    }:
        if (
            state.logical_time is None
            or event.logical_time != state.logical_time
            or process.claim_lease is None
            or process.claim_lease.acquired_at != state.logical_time
        ):
            raise ValueError("appraisal claim lease must start at logical time")
    existing_index = next(
        (
            index
            for index, item in enumerate(state.trigger_processes)
            if item.trigger_id == process.trigger_id
        ),
        None,
    )
    if existing_index is not None:
        existing = state.trigger_processes[existing_index]
        if existing.state != "open":
            raise ValueError(f"trigger {process.trigger_id!r} is not open")
        if (
            existing.trigger_ref != process.trigger_ref
            or existing.process_kind != process.process_kind
            or existing.source_evidence_ref != process.source_evidence_ref
        ):
            raise ValueError("claim cannot change opened trigger identity")
        return state.model_copy(
            update={
                "trigger_processes": (
                    *state.trigger_processes[:existing_index],
                    process,
                    *state.trigger_processes[existing_index + 1 :],
                )
            }
        )
    if process.process_kind in {
        "npc_world_appraisal",
        "interaction_appraisal",
        "interaction_fact",
        "affect_deliberation",
        "outcome_deliberation",
        "expression_reconsideration",
        "life_ecology",
    }:
        raise ValueError("appraisal trigger must be opened before it is claimed")
    return state.model_copy(update={"trigger_processes": (*state.trigger_processes, process)})


def _trigger_process_opened(state: ReducerState, event: WorldEvent) -> ReducerState:
    process = _model_from_payload(event, "process", TriggerProcess)
    if process.state != "open":
        raise ValueError("TriggerProcessOpened requires open state")
    if process.process_kind == "interaction_appraisal":
        if not any(
            item.observation_id == process.source_evidence_ref
            for item in state.message_observations
        ):
            raise ValueError("interaction appraisal trigger requires an observed message")
        if (
            process.trigger_id
            != interaction_appraisal_trigger_identity(event.world_id, process.source_evidence_ref)
            or process.trigger_ref != f"interaction:{process.source_evidence_ref}"
        ):
            raise ValueError("interaction appraisal trigger identity is not deterministic")
    if process.process_kind == "interaction_fact":
        if not any(
            item.observation_id == process.source_evidence_ref
            for item in state.message_observations
        ):
            raise ValueError("interaction fact trigger requires an observed message")
        if (
            process.trigger_id
            != interaction_fact_trigger_identity(event.world_id, process.source_evidence_ref)
            or process.trigger_ref != f"fact:{process.source_evidence_ref}"
        ):
            raise ValueError("interaction fact trigger identity is not deterministic")
    if process.process_kind == "external_result_deliberation":
        source = next(
            (
                item
                for item in state.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        if source is None or source.event_type != "ToolResultAccepted":
            raise ValueError("external result trigger requires an accepted tool result")
        result = next(
            (item for item in state.tool_results if item.accepted_event_ref == source.event_id),
            None,
        )
        if result is None:
            raise ValueError("external result trigger source projection is unavailable")
        if (
            process.trigger_id
            != external_result_trigger_id(world_id=event.world_id, result_id=result.result_id)
            or process.trigger_ref != f"external-result:{result.result_id}"
        ):
            raise ValueError("external result trigger identity is not deterministic")
    if process.process_kind == "npc_world_appraisal":
        source = next(
            (
                item
                for item in state.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        if (
            source is None
            or source.event_type != "WorldOccurrenceSettled"
            or source.continuation_refs != (process.trigger_id,)
            or process.trigger_ref != process.trigger_id
        ):
            raise ValueError("npc appraisal trigger requires a settled world event")
    if process.process_kind == "affect_deliberation":
        source = next(
            (
                item
                for item in state.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        if (
            source is None
            or source.event_type != "AppraisalAccepted"
            or process.trigger_ref != f"affect:{process.source_evidence_ref}"
        ):
            raise ValueError("affect trigger requires an accepted appraisal event")
    if process.process_kind == "outcome_deliberation":
        source = next(
            (
                item
                for item in state.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        if source is None or source.event_type != "OutcomeObservationRecorded":
            raise ValueError("outcome trigger requires a recorded outcome observation")
        observation = next(
            (
                item
                for item in state.outcome_observations
                if item.observation_id == source.event_id.removeprefix("event:outcome-observation:")
            ),
            None,
        )
        if observation is None:
            raise ValueError("outcome trigger source observation is unavailable")
        from .outcome_trigger import outcome_deliberation_trigger_id

        if (
            process.trigger_id
            != outcome_deliberation_trigger_id(
                world_id=event.world_id,
                occurrence_id=observation.occurrence_id,
                observation_id=observation.observation_id,
            )
            or process.trigger_ref
            != f"outcome:{observation.occurrence_id}:{observation.observation_id}"
        ):
            raise ValueError("outcome trigger identity is not deterministic")
    if process.process_kind == "media_continuation":
        source = next(
            (
                item
                for item in state.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        expected_ids = {continuation_trigger_id(item) for item in state.media_plans}
        if (
            source is None
            or source.event_type != "MediaPlanRecorded"
            or process.trigger_id not in expected_ids
            or process.trigger_ref != process.trigger_id
        ):
            raise ValueError("media continuation trigger is not bound to a frozen MediaPlan")
    if process.process_kind == "media_repair":
        if process.source_evidence_ref is None or not process.source_evidence_ref.startswith(
            "inspection:"
        ):
            raise ValueError("media repair trigger requires a failed inspection source")
        inspection_id = process.source_evidence_ref.removeprefix("inspection:")
        inspection = next(
            (item for item in state.media_inspections if item.inspection_id == inspection_id), None
        )
        if (
            inspection is None
            or inspection.passed
            or not inspection.repairable
            or process.trigger_id
            != media_repair_trigger_id(world_id=event.world_id, inspection_id=inspection_id)
            or process.trigger_ref != f"media-repair:{inspection_id}"
            or any(
                item.kind == "media_repair" and item.intent_ref == inspection.plan_id
                for item in state.actions
            )
        ):
            raise ValueError(
                "media repair trigger is not bound to the first repairable inspection failure"
            )
    if process.process_kind == "media_delivery_interaction":
        source = next(
            (
                item
                for item in state.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        if source is None or source.event_type != "MediaDeliveryShared":
            raise ValueError("media delivery interaction trigger requires a delivered media share")
        delivery = next(
            (
                item
                for item in state.media_deliveries
                if process.trigger_ref == f"media-delivery:{item.delivery_id}"
            ),
            None,
        )
        if delivery is None:
            raise ValueError("media delivery interaction trigger source delivery is unavailable")
        from .media_delivery_interaction import media_delivery_interaction_trigger_id

        if (
            process.trigger_id
            != media_delivery_interaction_trigger_id(
                world_id=event.world_id, delivery_id=delivery.delivery_id
            )
            or event.causation_id != source.event_id
        ):
            raise ValueError("media delivery interaction trigger identity is not deterministic")
    if process.process_kind == "expression_reconsideration":
        source = next(
            (
                item
                for item in state.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        if (
            source is None
            or source.event_type != "ObservationRecorded"
            or event.causation_id != source.event_id
        ):
            raise ValueError("expression reconsideration requires its recorded observation")
        prefix = "expression-reconsideration:"
        if not process.trigger_ref.startswith(prefix):
            raise ValueError("expression reconsideration trigger ref is invalid")
        try:
            lineage = json.loads(process.trigger_ref.removeprefix(prefix))
        except json.JSONDecodeError as exc:
            raise ValueError("expression reconsideration trigger ref is invalid") from exc
        if not isinstance(lineage, dict):
            raise ValueError("expression reconsideration trigger ref is invalid")
        plan_id = lineage.get("plan_id")
        beat_id = lineage.get("beat_id")
        observation_id = lineage.get("observation_id")
        if not all(
            isinstance(value, str) and value for value in (plan_id, beat_id, observation_id)
        ):
            raise ValueError("expression reconsideration trigger ref is invalid")
        from .expression_reconsideration import expression_reconsideration_trigger_id

        plan = next((item for item in state.expression_plans if item.plan_id == plan_id), None)
        beat = next((item for item in state.expression_beats if item.beat_id == beat_id), None)
        action = next(
            (
                item
                for item in state.actions
                if beat is not None and item.action_id == beat.action_id
            ),
            None,
        )
        observed = any(item.observation_id == observation_id for item in state.message_observations)
        if (
            not observed
            or plan is None
            or plan.state != "authorized"
            or beat is None
            or beat.plan_id != plan_id
            or beat.state != "authorized"
            or action is None
            or action.expression_plan_id != plan_id
            or action.expression_beat_id != beat_id
            or action.state not in {"authorized", "scheduled", "claimed"}
            or process.trigger_id
            != expression_reconsideration_trigger_id(
                world_id=event.world_id,
                plan_id=plan_id,
                beat_id=beat_id,
                observation_id=observation_id,
            )
        ):
            raise ValueError("expression reconsideration trigger is not eligible")
    if process.process_kind == "life_ecology":
        source = next(
            (
                item
                for item in state.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        parsed_ref = parse_life_ecology_trigger_ref(process.trigger_ref)
        if (
            source is None
            or source.event_type not in LIFE_ECOLOGY_WAKE_EVENT_TYPES
            or parsed_ref is None
            or parsed_ref[1] != process.source_evidence_ref
            or process.trigger_id
            != life_ecology_trigger_id(
                world_id=event.world_id,
                wake_event_ref=parsed_ref[1],
                catalog_version=parsed_ref[0],
            )
            or event.causation_id != source.event_id
        ):
            raise ValueError("life ecology trigger is not bound to its committed wake")
    if any(item.trigger_id == process.trigger_id for item in state.trigger_processes):
        raise ValueError(f"trigger {process.trigger_id!r} already exists")
    return state.model_copy(update={"trigger_processes": (*state.trigger_processes, process)})


def _life_payload(event: WorldEvent, model_type):
    return model_type.model_validate_json(event.payload_json)


def _validated_life_payload(state: ReducerState, event: WorldEvent, model_type):
    payload = _life_payload(event, model_type)
    _validate_evidence_authority(state, payload.evidence_refs, require_all=True)
    return payload


def _canonical_model_hash(value: FrozenModel) -> str:
    encoded = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_evidence_authority(
    state: ReducerState,
    evidence_refs: tuple[EvidenceRef, ...],
    *,
    require_all: bool = False,
) -> None:
    """Resolve evidence against authoritative reducer state; fail closed."""

    authority = {ref.event_id: ref for ref in state.committed_world_event_refs}
    for evidence in evidence_refs:
        kind = evidence.evidence_type
        if not require_all and kind not in {
            "committed_world_event",
            "settled_world_event",
        }:
            continue
        if kind in {"committed_world_event", "settled_world_event"}:
            committed = authority.get(evidence.ref_id)
            if (
                committed is None
                or evidence.source_world_revision != committed.world_revision
                or evidence.immutable_hash != committed.payload_hash
            ):
                raise ValueError("world-event evidence does not resolve to ledger authority")
            if kind == "settled_world_event" and committed.event_type != "WorldOccurrenceSettled":
                raise ValueError("settled-world evidence is not a settlement event")
            continue
        if kind == "committed_fact":
            committed = authority.get(evidence.ref_id)
            transition = next(
                (
                    item
                    for item in state.fact_transitions
                    if item.accepted_event_ref == evidence.ref_id
                ),
                None,
            )
            if (
                committed is None
                or committed.event_type not in {*FACT_PAYLOAD_MODELS, "FactCommittedV2"}
                or transition is None
                or evidence.source_world_revision != committed.world_revision
                or evidence.immutable_hash != _canonical_model_hash(transition.values_after)
            ):
                raise ValueError("committed-fact evidence does not resolve to transition authority")
            continue
        if kind == "observed_message":
            message = next(
                (
                    item
                    for item in state.message_observations
                    if item.observation_id == evidence.ref_id
                ),
                None,
            )
            if message is None:
                raise ValueError("observed-message evidence does not resolve to authority")
            if (
                evidence.source_world_revision != message.world_revision
                or evidence.immutable_hash != message.event_payload_hash
            ):
                raise ValueError("observed-message evidence provenance does not match authority")
            continue
        if kind == "committed_experience":
            candidate = next(
                (
                    item
                    for item in state.experiences
                    if isinstance(item, ExperienceProjection)
                    and item.origin.accepted_event_ref == evidence.ref_id
                ),
                None,
            )
            committed = authority.get(evidence.ref_id)
            transition = next(
                (
                    item
                    for item in state.experience_transitions
                    if item.accepted_event_ref == evidence.ref_id
                    and candidate is not None
                    and item.experience_id == candidate.experience_id
                ),
                None,
            )
            if (
                candidate is None
                or candidate.status != "committed"
                or committed is None
                or committed.event_type != "ExperienceCommitted"
                or transition is None
                or transition.values_after != candidate.values
            ):
                raise ValueError("experience evidence does not resolve to authority")
            if (
                evidence.source_world_revision != committed.world_revision
                or evidence.immutable_hash != _canonical_model_hash(transition.values_after)
            ):
                raise ValueError("experience evidence hash does not match authority")
            continue
        if kind == "active_plan":
            candidate = next(
                (item for item in state.plans if item.plan_id == evidence.ref_id), None
            )
            if candidate is None or candidate.status not in {"planned", "active", "paused"}:
                raise ValueError("active-plan evidence does not resolve to authority")
            if (
                evidence.source_world_revision is not None
                or evidence.immutable_hash != canonical_plan_evidence_hash(candidate)
            ):
                raise ValueError("active-plan evidence hash does not match authority")
            continue
        if kind == "settled_external_result":
            receipt = next(
                (
                    item
                    for item in state.execution_receipts
                    if item.is_terminal
                    and evidence.ref_id in {item.receipt_id, item.result_id, item.source_event_id}
                ),
                None,
            )
            if receipt is None:
                raise ValueError("external-result evidence does not resolve to authority")
            if (
                evidence.source_world_revision is not None
                or evidence.immutable_hash != _canonical_model_hash(receipt)
            ):
                raise ValueError("external-result evidence hash does not match authority")
            continue
        if kind == "clock_observation":
            if (
                state.logical_time is None
                or evidence.ref_id != f"clock:{state.logical_time.isoformat()}"
                or evidence.source_world_revision is not None
                or evidence.immutable_hash is not None
            ):
                raise ValueError("clock evidence requires authoritative logical time")
            continue
        if kind == "operator_observation":
            operator_ref = next(
                (
                    item
                    for item in state.operator_observations
                    if item.observation_id == evidence.ref_id
                ),
                None,
            )
            if operator_ref is None:
                raise ValueError("operator evidence does not resolve to authority")
            if (
                evidence.source_world_revision is not None
                or evidence.immutable_hash != operator_ref.observation_hash
            ):
                raise ValueError("operator evidence hash does not match authority")
            continue
        raise ValueError(f"{kind} evidence has no installed authority resolver")


def _require_life_time(state: ReducerState, event: WorldEvent) -> datetime:
    if state.logical_time is None:
        raise ValueError("lived-world mutation requires authoritative logical time")
    if event.logical_time != state.logical_time:
        raise ValueError("lived-world event must be pinned to current logical time")
    return state.logical_time


def _npc_registered(state: ReducerState, event: WorldEvent) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, NpcRegisteredPayload)
    return state.model_copy(update={"npcs": register_npc(state.npcs, payload)})


def _activity_planned(
    state: ReducerState,
    event: WorldEvent,
    *,
    allow_legacy_missing_owner: bool = False,
) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = _validated_life_payload(state, event, ActivityPlannedPayload)
    return state.model_copy(
        update={
            "plans": plan_activity(
                state.plans,
                state.npcs,
                payload,
                event_ref=event.event_id,
                event_payload_hash=event.payload_hash,
                accepted_world_revision=len(state.committed_world_event_refs) + 1,
                logical_time=logical_time,
                allow_legacy_missing_owner=allow_legacy_missing_owner,
            )
        }
    )


def _activity_transitioned(
    state: ReducerState,
    event: WorldEvent,
    *,
    target_status: str,
    allowed_statuses: frozenset[str],
    allow_legacy_unowned_transition: bool = False,
) -> ReducerState:
    payload = _validated_life_payload(state, event, ActivityTransitionPayload)
    _validate_activity_lifecycle_effect(state, event, payload)
    return state.model_copy(
        update={
            "plans": transition_activity(
                state.plans,
                payload,
                target_status=target_status,
                allowed_statuses=allowed_statuses,
                logical_time=_require_life_time(state, event),
                event_type=event.event_type,
                event_ref=event.event_id,
                event_payload_hash=event.payload_hash,
                accepted_world_revision=len(state.committed_world_event_refs) + 1,
                allow_legacy_unowned_transition=allow_legacy_unowned_transition,
            )
        }
    )


def _validate_activity_lifecycle_effect(
    state: ReducerState, event: WorldEvent, payload: ActivityTransitionPayload
) -> None:
    """Require an adjacent accepted manifest for scheduler-originated effects.

    The legacy Activity transition vocabulary remains readable for migration
    and explicit host operations.  An effect carrying lifecycle acceptance
    coordinates, however, cannot bypass the proposal → acceptance batch.
    """

    if payload.activity_lifecycle_proposal_id is None:
        return
    if not state.committed_world_event_refs:
        raise ValueError("activity lifecycle effect requires adjacent acceptance")
    acceptance_ref = state.committed_world_event_refs[-1]
    if acceptance_ref.event_type != "AcceptanceRecorded" or event.causation_id != acceptance_ref.event_id:
        raise ValueError("activity lifecycle effect requires adjacent acceptance")
    # The acceptance handler has already parsed, self-hash-verified, and
    # retained a generic decision reference.  Rehydrate the immutable event
    # bytes here so a different accepted-manifest family cannot authorize it.
    # Reducers receive no ledger read capability, hence the version and the
    # retained decision jointly provide the replay-local proof.
    decision = next(
        (
            item
            for item in state.acceptance_decisions
            if item.acceptance_id == payload.acceptance_id
            and item.proposal_id == payload.activity_lifecycle_proposal_id
        ),
        None,
    )
    proposal_revision = next(
        (
            item
            for item in state.proposal_revisions
            if item.proposal_id == payload.activity_lifecycle_proposal_id
        ),
        None,
    )
    if (
        decision is None
        or proposal_revision is None
        or decision.manifest_version != ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION
        or decision.accepted_change_hash != payload.accepted_change_hash
        or decision.accepted_change_id != payload.change_id
        or payload.reason_ref != proposal_revision.proposal_event_ref
    ):
        raise ValueError("activity lifecycle effect does not bind accepted proposal")


def _activity_lifecycle_proposal_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Validate scheduler proposal authority without changing World facts."""

    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, ActivityLifecycleProposalRecordedPayload)
    if payload.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("activity lifecycle proposal must evaluate the current world revision")
    if payload.proposal_id in state.proposal_ids:
        raise ValueError("proposal identity is already registered")
    if payload.policy_digest != ACTIVITY_LIFECYCLE_PROPOSAL_POLICY_DIGEST:
        raise ValueError("activity lifecycle proposal policy is not installed")
    wake = next(
        (item for item in state.committed_world_event_refs if item.event_id == payload.wake_event_ref),
        None,
    )
    trigger = next(
        (item for item in state.trigger_processes if item.trigger_id == payload.ecology_trigger_id),
        None,
    )
    parsed_trigger = parse_life_ecology_trigger_ref(trigger.trigger_ref) if trigger else None
    if (
        wake is None
        or wake.event_type != "ClockAdvanced"
        or wake.payload_hash != payload.wake_event_payload_hash
        or trigger is None
        or trigger.process_kind != "life_ecology"
        or trigger.state != "claimed"
        or trigger.claim_lease is None
        or trigger.source_evidence_ref != payload.wake_event_ref
        or parsed_trigger is None
        or trigger.trigger_id
        != life_ecology_trigger_id(
            world_id=event.world_id,
            wake_event_ref=payload.wake_event_ref,
            catalog_version=parsed_trigger[0],
        )
    ):
        raise ValueError("activity lifecycle proposal does not bind claimed ecology trigger")
    plan = next((item for item in state.plans if item.plan_id == payload.plan_id), None)
    if plan is None or plan.owner_actor_ref is None:
        raise ValueError("activity lifecycle proposal plan is unavailable")
    projection = make_projection(
        world_id=event.world_id,
        world_revision=len(state.committed_world_event_refs),
        deliberation_revision=payload.evaluated_deliberation_revision,
        ledger_sequence=payload.evaluated_ledger_sequence,
        state=state,
        reducer_bundle_version=REDUCER_BUNDLE_VERSION,
    )
    resolved = ActivityOpeningCatalog(
        owner_actor_ref=plan.owner_actor_ref,
        catalog_version=payload.catalog_version,
    ).resolve_opening(
        projection=projection,
        wake_event_ref=payload.wake_event_ref,
        opening_token=payload.opening_token,
    )
    if (
        resolved is None
        or resolved.catalog_hash != payload.catalog_hash
        or resolved.plan_id != payload.plan_id
        or resolved.plan_revision != payload.expected_plan_revision
        or resolved.operation != payload.operation
    ):
        raise ValueError("activity lifecycle proposal opening is not current")
    return state.model_copy(
        update={
            "proposal_ids": (*state.proposal_ids, payload.proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=payload.proposal_id,
                    evaluated_world_revision=payload.evaluated_world_revision,
                    proposal_event_ref=event.event_id,
                    proposal_event_payload_hash=event.payload_hash,
                ),
            ),
        }
    )


def _world_occurrence_committed(state: ReducerState, event: WorldEvent) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, WorldOccurrenceCommittedPayload)
    return state.model_copy(
        update={
            "world_occurrences": commit_occurrence(
                state.world_occurrences,
                state.npcs,
                state.plans,
                payload,
            )
        }
    )


def _world_occurrence_activated(state: ReducerState, event: WorldEvent) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, WorldOccurrenceActivatedPayload)
    return state.model_copy(
        update={"world_occurrences": activate_occurrence(state.world_occurrences, payload)}
    )


def _outcome_observation_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, OutcomeObservationRecordedPayload)
    occurrences, observations = record_outcome_observation(
        state.world_occurrences,
        state.outcome_observations,
        state.committed_world_event_refs,
        payload,
        logical_time=_require_life_time(state, event),
    )
    return state.model_copy(
        update={
            "world_occurrences": occurrences,
            "outcome_observations": observations,
        }
    )


def _world_occurrence_settled(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, WorldOccurrenceSettledPayload)
    return state.model_copy(
        update={
            "world_occurrences": settle_occurrence(
                state.world_occurrences,
                state.outcome_observations,
                state.outcome_proposals,
                payload,
                logical_time=_require_life_time(state, event),
                settlement_event_ref=event.event_id,
                settlement_world_revision=len(state.committed_world_event_refs) + 1,
                settlement_payload_hash=event.payload_hash,
            )
        }
    )


def _outcome_proposal_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, OutcomeProposalRecordedPayload)
    if payload.evaluated_world_revision != len(state.committed_world_event_refs):
        raise ValueError("outcome proposal must evaluate the current world revision")
    if payload.outcome_proposal_id in state.proposal_ids:
        raise ValueError("proposal identity is already registered")
    if payload.deliberation_trigger_id is not None:
        trigger = next(
            (
                item
                for item in state.trigger_processes
                if item.trigger_id == payload.deliberation_trigger_id
            ),
            None,
        )
        source_event_id = f"event:outcome-observation:{payload.source_observation_id}"
        observation = next(
            (
                item
                for item in state.outcome_observations
                if item.observation_id == payload.source_observation_id
            ),
            None,
        )
        if (
            trigger is None
            or trigger.process_kind != "outcome_deliberation"
            or trigger.state != "claimed"
            or trigger.source_evidence_ref != source_event_id
            or observation is None
            or observation.occurrence_id != payload.occurrence_id
            or payload.source_observation_id not in payload.observation_refs
        ):
            raise ValueError("outcome proposal does not bind a claimed source trigger")
    return state.model_copy(
        update={
            "outcome_proposals": record_outcome_proposal(
                state.outcome_proposals,
                payload,
            ),
            "proposal_ids": (*state.proposal_ids, payload.outcome_proposal_id),
            "proposal_revisions": (
                *state.proposal_revisions,
                ProposalRevisionRef(
                    proposal_id=payload.outcome_proposal_id,
                    evaluated_world_revision=payload.evaluated_world_revision,
                ),
            ),
        }
    )


def _experience_committed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = ExperienceCommittedPayload.model_validate_json(event.payload_json)
    if payload.policy_refs != INSTALLED_EXPERIENCE_POLICY_REFS:
        raise ValueError("experience commit references an uninstalled policy")
    if payload.experience.origin.accepted_event_ref != event.event_id:
        raise ValueError("experience origin does not identify its accepted mutation event")
    if any(item.transition_id == payload.transition_id for item in state.experience_transitions):
        raise ValueError("experience transition identity is already registered")
    proposal = _require_authorized_experience(state, payload)
    return state.model_copy(
        update={
            "experiences": commit_experience(
                state.experiences,
                state.world_occurrences,
                state.plans,
                state.committed_world_event_refs,
                state.execution_receipts,
                state.actions,
                state.facts,
                payload,
                logical_time=logical_time,
            ),
            "experience_transitions": (
                *state.experience_transitions,
                ExperienceTransitionProjection(
                    transition_id=payload.transition_id,
                    experience_id=payload.experience.experience_id,
                    values_after=payload.experience.values,
                    semantic_fingerprint_after=payload.experience.semantic_fingerprint,
                    change_id=payload.change_id,
                    policy_refs=payload.policy_refs,
                    accepted_event_ref=event.event_id,
                    accepted_at=logical_time,
                ),
            ),
            "experience_proposals": tuple(
                item for item in state.experience_proposals if item != proposal
            ),
        }
    )


_LIFE_CONTENT_PRIVACY_RANK = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def _life_content_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Expose sidecar bytes only after proving their exact life authority."""

    _require_life_time(state, event)
    payload = LifeContentRecordedPayload.model_validate_json(event.payload_json)
    if any(
        item.content_id == payload.content_id or item.content_ref == payload.content_ref
        for item in state.life_content_descriptors
    ):
        raise ValueError("life content descriptor identity is already registered")
    source_event = next(
        (
            item
            for item in state.committed_world_event_refs
            if item.event_id == payload.source_event_ref
        ),
        None,
    )
    if source_event is None or (
        source_event.world_revision != payload.source_world_revision
        or source_event.payload_hash != payload.source_payload_hash
    ):
        raise ValueError("life content descriptor source authority is unavailable")

    source_privacy: str
    if payload.source_kind == "occurrence_settlement":
        if (
            payload.content_kind != "occurrence_result"
            or source_event.event_type != "WorldOccurrenceSettled"
        ):
            raise ValueError("occurrence content must bind an exact settlement")
        occurrence = next(
            (
                item
                for item in state.world_occurrences
                if item.occurrence_id == payload.source_entity_id
            ),
            None,
        )
        if occurrence is None or (
            occurrence.status != "settled"
            or occurrence.entity_revision != payload.source_entity_revision
            or occurrence.settlement_event_ref != payload.source_event_ref
            or occurrence.settlement_world_revision != payload.source_world_revision
            or occurrence.settlement_payload_hash != payload.source_payload_hash
            or occurrence.result_payload_ref != payload.content_ref
            or occurrence.result_payload_hash != payload.content_payload_hash
        ):
            raise ValueError("life content descriptor does not match settled occurrence")
        source_privacy = occurrence.visibility
    else:
        if (
            payload.content_kind != "experience_summary"
            or source_event.event_type != "ExperienceCommitted"
        ):
            raise ValueError("experience content must bind an exact experience commit")
        experience = next(
            (
                item
                for item in state.experiences
                if isinstance(item, ExperienceProjection)
                and item.experience_id == payload.source_entity_id
            ),
            None,
        )
        if experience is None or (
            experience.entity_revision != payload.source_entity_revision
            or experience.origin.accepted_event_ref != payload.source_event_ref
            or experience.values.summary_ref != payload.content_ref
            or experience.values.summary_payload_hash != payload.content_payload_hash
        ):
            raise ValueError("life content descriptor does not match committed experience")
        source_privacy = experience.values.privacy_class

    if (
        _LIFE_CONTENT_PRIVACY_RANK[payload.privacy_class]
        < _LIFE_CONTENT_PRIVACY_RANK[source_privacy]
    ):
        raise ValueError("life content descriptor cannot weaken source privacy")
    descriptor = LifeContentDescriptorProjection(
        **payload.model_dump(),
        descriptor_event_ref=event.event_id,
        descriptor_world_revision=len(state.committed_world_event_refs) + 1,
        descriptor_payload_hash=event.payload_hash,
    )
    return state.model_copy(
        update={"life_content_descriptors": (*state.life_content_descriptors, descriptor)}
    )


def _legacy_experience_committed(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = LegacyExperienceCommittedPayload.model_validate_json(event.payload_json)
    return state.model_copy(
        update={"experiences": commit_legacy_experience(state.experiences, payload)}
    )


def _memory_candidate_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = MemoryCandidateChangedPayload.model_validate_json(event.payload_json)
    proposal = _require_authorized_memory_candidate(state, payload)
    _validate_evidence_authority(state, payload.evidence_refs, require_all=True)
    if isinstance(payload.forget_authority, MemoryEvidenceForgetAuthority):
        _validate_memory_forget_decision_evidence(state, payload)
    candidates, history = reduce_memory_candidate(
        state.memory_candidates,
        state.memory_candidate_transitions,
        payload,
        event_type=event.event_type,
        event_id=event.event_id,
        logical_time=logical_time,
        facts=state.facts,
        fact_history=state.fact_transitions,
        experiences=state.experiences,
        experience_history=state.experience_transitions,
        threads=state.threads,
        thread_history=state.thread_transitions,
        committed_events=state.committed_world_event_refs,
    )
    return state.model_copy(
        update={
            "memory_candidates": candidates,
            "memory_candidate_transitions": history,
            "memory_candidate_proposals": tuple(
                item for item in state.memory_candidate_proposals if item != proposal
            ),
        }
    )


def _character_core_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = CharacterCoreChangedPayload.model_validate_json(event.payload_json)
    proposal = _require_authorized_character_core(state, payload)
    _validate_evidence_authority(state, payload.evidence_refs, require_all=True)
    core, history = reduce_character_core(
        state.character_core,
        state.character_core_transitions,
        payload,
        event_type=event.event_type,
        event_id=event.event_id,
        logical_time=logical_time,
        actor_authorities=state.actor_authorities,
        facts=state.facts,
        fact_history=state.fact_transitions,
        experiences=state.experiences,
        experience_history=state.experience_transitions,
        world_occurrences=state.world_occurrences,
        committed_events=state.committed_world_event_refs,
    )
    return state.model_copy(
        update={
            "character_core": core,
            "character_core_transitions": history,
            "character_core_proposals": tuple(
                item for item in state.character_core_proposals if item != proposal
            ),
        }
    )


def _require_authorized_character_core(
    state: ReducerState,
    payload: CharacterCoreChangedPayload,
) -> CharacterCoreProposalProjection:
    proposal = next(
        (
            item
            for item in state.character_core_proposals
            if item.proposal_id == payload.proposal_id
        ),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("character core transition requires persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
        or not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
    ):
        raise ValueError("character core transition requires adjacent accepted authority")
    if (
        proposal.transition_kind != payload.operation
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted character core transition does not match proposal")
    return proposal


def _require_authorized_memory_candidate(
    state: ReducerState,
    payload: MemoryCandidateAuthorizedMutationPayload,
) -> MemoryCandidateProposalProjection:
    proposal = next(
        (
            item
            for item in state.memory_candidate_proposals
            if item.proposal_id == payload.proposal_id
        ),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("memory transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
        or not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
    ):
        raise ValueError("memory transition requires adjacent accepted authority")
    if (
        proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted memory transition does not match its proposal")
    return proposal


def _validate_memory_forget_decision_evidence(
    state: ReducerState,
    payload: MemoryCandidateChangedPayload,
) -> None:
    authority = payload.forget_authority
    if not isinstance(authority, MemoryEvidenceForgetAuthority):
        raise TypeError("memory forget decision is not evidence-authorized")
    _validate_evidence_authority(
        state,
        (authority.decision_evidence_ref,),
        require_all=True,
    )
    before = payload.candidate_before
    if before is None or authority.target_candidate_id != before.candidate_id:
        raise ValueError("memory forget decision scope targets another candidate")
    if authority.reason == "privacy_request":
        message = next(
            (
                item
                for item in state.message_observations
                if item.observation_id == authority.decision_evidence_ref.ref_id
            ),
            None,
        )
        if (
            message is None
            or message.actor != authority.decision_subject_ref
            or message.content_payload_hash != authority.decision_content_hash
        ):
            raise ValueError("memory privacy request lacks exact principal message scope")
        return
    observation = next(
        (
            item
            for item in state.operator_observations
            if item.observation_id == authority.decision_evidence_ref.ref_id
        ),
        None,
    )
    if (
        observation is None
        or authority.decision_subject_ref != observation.observation_id
        or observation.observation_hash != authority.decision_content_hash
    ):
        raise ValueError("memory suppression lacks exact operator decision scope")


def _require_authorized_experience(
    state: ReducerState,
    payload: ExperienceAuthorizedMutationPayload,
) -> ExperienceProposalProjection:
    proposal = next(
        (item for item in state.experience_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("experience commit requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("experience commit requires its accepted decision")
    if not state.acceptance_decisions or state.acceptance_decisions[-1] != decision:
        raise ValueError("experience commit requires adjacent AcceptanceRecorded authority")
    if (
        proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted experience commit does not match its proposal")
    return proposal


def _world_occurrence_terminated(
    state: ReducerState, event: WorldEvent, *, target_status: str
) -> ReducerState:
    payload = _validated_life_payload(state, event, WorldOccurrenceTerminalPayload)
    return state.model_copy(
        update={
            "world_occurrences": terminate_occurrence(
                state.world_occurrences,
                payload,
                target_status=target_status,
                logical_time=_require_life_time(state, event),
            )
        }
    )


def _appraisal_accepted(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AppraisalAcceptedPayload)
    _validate_evidence_authority(state, payload.evidence_refs, require_all=True)
    if payload.appraisal.origin.accepted_event_ref != event.event_id:
        raise ValueError("appraisal origin must reference its accepted event")
    _require_installed_appraisal_origin(payload.appraisal)
    proposal = _require_authorized_appraisal(state, payload, transition_kind="accept")
    return state.model_copy(
        update={
            "appraisals": accept_appraisal(
                state.appraisals, payload, logical_time=_require_life_time(state, event)
            ),
            "appraisal_proposals": tuple(
                item for item in state.appraisal_proposals if item != proposal
            ),
        }
    )


def _appraisal_contradicted(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AppraisalContradictedPayload)
    _validate_evidence_authority(state, payload.evidence_refs, require_all=True)
    proposal = _require_authorized_appraisal(state, payload, transition_kind="contradict")
    return state.model_copy(
        update={
            "appraisals": contradict_appraisal(
                state.appraisals, payload, logical_time=_require_life_time(state, event)
            ),
            "appraisal_proposals": tuple(
                item for item in state.appraisal_proposals if item != proposal
            ),
        }
    )


def _appraisal_expired(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AppraisalExpiredPayload)
    _validate_evidence_authority(state, payload.evidence_refs, require_all=True)
    return state.model_copy(
        update={
            "appraisals": expire_appraisal(
                state.appraisals, payload, logical_time=_require_life_time(state, event)
            )
        }
    )


def _appraisal_superseded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AppraisalSupersededPayload)
    _validate_evidence_authority(state, payload.evidence_refs, require_all=True)
    if payload.successor.origin.accepted_event_ref != event.event_id:
        raise ValueError("successor appraisal origin must reference its accepted event")
    _require_installed_appraisal_origin(payload.successor)
    proposal = _require_authorized_appraisal(state, payload, transition_kind="supersede")
    return state.model_copy(
        update={
            "appraisals": supersede_appraisal(
                state.appraisals, payload, logical_time=_require_life_time(state, event)
            ),
            "appraisal_proposals": tuple(
                item for item in state.appraisal_proposals if item != proposal
            ),
        }
    )


def _affect_episode_opened(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AffectEpisodeOpenedPayload)
    if payload.episode.origin.accepted_event_ref != event.event_id:
        raise ValueError("affect origin must reference its accepted event")
    _require_installed_affect_origin(payload.episode)
    proposal = _require_authorized_affect(state, payload, transition_kind="open")
    return state.model_copy(
        update={
            "affect_episodes": open_affect_episode(
                state.affect_episodes,
                payload,
                appraisals=state.appraisals,
                logical_time=_require_life_time(state, event),
                merge_window_seconds=INSTALLED_AFFECT_MERGE_WINDOW_SECONDS,
                baselines=state.affect_baselines,
            ),
            "affect_proposals": tuple(item for item in state.affect_proposals if item != proposal),
        }
    )


def _affect_episode_updated(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AffectEpisodeUpdatedPayload)
    proposal = _require_authorized_affect(state, payload, transition_kind="update")
    return state.model_copy(
        update={
            "affect_episodes": update_affect_episode(
                state.affect_episodes,
                payload,
                appraisals=state.appraisals,
                logical_time=_require_life_time(state, event),
                merge_window_seconds=INSTALLED_AFFECT_MERGE_WINDOW_SECONDS,
                baselines=state.affect_baselines,
            ),
            "affect_proposals": tuple(item for item in state.affect_proposals if item != proposal),
        }
    )


def _affect_episode_decayed(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AffectEpisodeDecayedPayload)
    return state.model_copy(
        update={
            "affect_episodes": decay_affect_episode(
                state.affect_episodes,
                payload,
                logical_time=_require_life_time(state, event),
                baselines=state.affect_baselines,
            )
        }
    )


def _affect_episode_resolved(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AffectEpisodeResolvedPayload)
    proposal = _require_authorized_affect(state, payload, transition_kind="resolve")
    return state.model_copy(
        update={
            "affect_episodes": resolve_affect_episode(
                state.affect_episodes,
                payload,
                logical_time=_require_life_time(state, event),
            ),
            "affect_proposals": tuple(item for item in state.affect_proposals if item != proposal),
        }
    )


def _affect_episode_superseded(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AffectEpisodeSupersededPayload)
    if payload.successor.origin.accepted_event_ref != event.event_id:
        raise ValueError("successor affect origin must reference its accepted event")
    _require_installed_affect_origin(payload.successor)
    proposal = _require_authorized_affect(state, payload, transition_kind="supersede")
    return state.model_copy(
        update={
            "affect_episodes": supersede_affect_episode(
                state.affect_episodes,
                payload,
                appraisals=state.appraisals,
                logical_time=_require_life_time(state, event),
                merge_window_seconds=INSTALLED_AFFECT_MERGE_WINDOW_SECONDS,
                baselines=state.affect_baselines,
            ),
            "affect_proposals": tuple(item for item in state.affect_proposals if item != proposal),
        }
    )


def _affect_baseline_adjusted(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, AffectBaselineAdjustedPayload)
    _require_life_time(state, event)
    if payload.policy_refs != INSTALLED_AFFECT_BASELINE_POLICY_REFS:
        raise ValueError("baseline adjustment references an uninstalled policy")
    proposal = _require_authorized_affect(state, payload, transition_kind="baseline_adjust")
    return state.model_copy(
        update={
            "affect_baselines": adjust_affect_baseline(
                state.affect_baselines,
                state.affect_episodes,
                payload,
                logical_time=state.logical_time,
            ),
            "affect_proposals": tuple(item for item in state.affect_proposals if item != proposal),
        }
    )


def _relationship_signal_accepted(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = RelationshipSignalAcceptedPayload.model_validate_json(event.payload_json)
    if payload.policy_refs != INSTALLED_RELATIONSHIP_SIGNAL_POLICY_REFS:
        raise ValueError("relationship signal references an uninstalled policy")
    if payload.signal.origin.accepted_event_ref != event.event_id:
        raise ValueError("relationship signal origin does not identify its mutation event")
    proposal = _require_authorized_relationship(state, payload, transition_kind="signal")
    return state.model_copy(
        update={
            "relationship_signals": accept_relationship_signal(
                state.relationship_signals, payload, logical_time=logical_time
            ),
            "relationship_proposals": tuple(
                item for item in state.relationship_proposals if item != proposal
            ),
        }
    )


def _private_impression_accepted(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = PrivateImpressionAcceptedPayload.model_validate_json(event.payload_json)
    if payload.impression.origin is None or payload.impression.origin.accepted_event_ref != event.event_id:
        raise ValueError("private impression origin does not identify its mutation event")
    _validate_evidence_authority(state, payload.evidence_refs, require_all=True)
    _validate_private_impression_source_events(state, payload)
    proposal = _require_authorized_private_impression(state, payload)
    return state.model_copy(
        update={
            "private_impressions": accept_private_impression(
                state.private_impressions,
                payload,
                logical_time=logical_time,
                appraisals=state.appraisals,
            ),
            "private_impression_proposals": tuple(
                item for item in state.private_impression_proposals if item != proposal
            ),
        }
    )


def _relationship_slow_variable_adjusted(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = RelationshipSlowVariableAdjustedPayload.model_validate_json(event.payload_json)
    if payload.policy_refs != INSTALLED_RELATIONSHIP_POLICY_REFS:
        raise ValueError("relationship adjustment references an uninstalled policy")
    transition_kind = "compensate" if payload.operation == "compensate" else "adjust"
    proposal = _require_authorized_relationship(state, payload, transition_kind=transition_kind)
    states, history = adjust_relationship_slow_variables(
        state.relationship_states,
        state.relationship_adjustments,
        state.relationship_signals,
        payload,
        logical_time=logical_time,
        accepted_event_ref=event.event_id,
    )
    return state.model_copy(
        update={
            "relationship_states": states,
            "relationship_adjustments": history,
            "relationship_proposals": tuple(
                item for item in state.relationship_proposals if item != proposal
            ),
        }
    )


def _boundary_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = BoundaryChangedPayload.model_validate_json(event.payload_json)
    if payload.policy_refs != INSTALLED_BOUNDARY_POLICY_REFS:
        raise ValueError("boundary transition references an uninstalled policy")
    if payload.boundary.origin.accepted_event_ref != event.event_id:
        raise ValueError("boundary origin does not identify its mutation event")
    transition_kind = f"boundary_{payload.operation}"
    proposal = _require_authorized_relationship(state, payload, transition_kind=transition_kind)
    return state.model_copy(
        update={
            "boundaries": change_boundary(state.boundaries, payload, logical_time=logical_time),
            "relationship_proposals": tuple(
                item for item in state.relationship_proposals if item != proposal
            ),
        }
    )


def _thread_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = ThreadChangedPayload.model_validate_json(event.payload_json)
    if payload.policy_refs != INSTALLED_THREAD_POLICY_REFS:
        raise ValueError("thread transition references an uninstalled policy")
    if payload.thread_after.origin.accepted_event_ref != event.event_id:
        raise ValueError("thread origin does not identify its mutation event")
    proposal = _require_authorized_thread(state, payload)
    threads, transitions = reduce_thread(
        state.threads,
        state.thread_transitions,
        payload,
        event_type=event.event_type,
        logical_time=logical_time,
    )
    return state.model_copy(
        update={
            "threads": threads,
            "thread_transitions": transitions,
            "thread_proposals": tuple(item for item in state.thread_proposals if item != proposal),
        }
    )


def _thread_expired(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = ThreadExpiredPayload.model_validate_json(event.payload_json)
    _validate_evidence_authority(state, (payload.clock_evidence_ref,), require_all=True)
    clock_authority = next(
        (
            item
            for item in state.committed_world_event_refs
            if item.event_id == payload.clock_event_ref
        ),
        None,
    )
    if (
        clock_authority is None
        or clock_authority.event_type != "ClockAdvanced"
        or clock_authority.logical_time != logical_time
        or clock_authority.payload_hash != payload.clock_event_payload_hash
    ):
        raise ValueError("thread expiry requires its committed ClockAdvanced authority")
    if payload.thread_after.origin.accepted_event_ref != event.event_id:
        raise ValueError("thread expiry origin does not identify its mutation event")
    threads, transitions = expire_thread(
        state.threads,
        state.thread_transitions,
        payload,
        logical_time=logical_time,
    )
    return state.model_copy(update={"threads": threads, "thread_transitions": transitions})


def _commitment_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = CommitmentChangedPayload.model_validate_json(event.payload_json)
    if payload.policy_refs != INSTALLED_COMMITMENT_POLICY_REFS:
        raise ValueError("commitment transition references an uninstalled policy")
    if payload.commitment_after.origin.accepted_event_ref != event.event_id:
        raise ValueError("commitment origin does not identify its mutation event")
    proposal = _require_authorized_commitment(state, payload)
    commitments, transitions = reduce_commitment(
        state.commitments,
        state.commitment_transitions,
        payload,
        event_type=event.event_type,
        logical_time=logical_time,
        committed_events=state.committed_world_event_refs,
        execution_receipts=state.execution_receipts,
        actions=state.actions,
        threads=state.threads,
        thread_history=state.thread_transitions,
        message_observations=state.message_observations,
    )
    return state.model_copy(
        update={
            "commitments": commitments,
            "commitment_transitions": transitions,
            "commitment_proposals": tuple(
                item for item in state.commitment_proposals if item != proposal
            ),
        }
    )


def _commitment_clock_changed(
    state: ReducerState,
    event: WorldEvent,
    *,
    payload: CommitmentClockTransitionPayload | None = None,
) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = payload or CommitmentClockTransitionPayload.model_validate_json(event.payload_json)
    _validate_evidence_authority(state, (payload.clock_evidence_ref,), require_all=True)
    clock = next(
        (
            item
            for item in state.committed_world_event_refs
            if item.event_id == payload.clock_event_ref
        ),
        None,
    )
    if (
        clock is None
        or clock.event_type != "ClockAdvanced"
        or clock.logical_time != logical_time
        or clock.payload_hash != payload.clock_event_payload_hash
    ):
        raise ValueError("commitment clock transition requires its committed ClockAdvanced")
    if payload.commitment_after.origin.accepted_event_ref != event.event_id:
        raise ValueError("commitment clock origin does not identify its mutation event")
    commitments, transitions = reduce_commitment_clock(
        state.commitments,
        state.commitment_transitions,
        payload,
        logical_time=logical_time,
    )
    return state.model_copy(
        update={"commitments": commitments, "commitment_transitions": transitions}
    )


def _fact_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = FactChangedPayload.model_validate_json(event.payload_json)
    if payload.policy_refs != INSTALLED_FACT_POLICY_REFS:
        raise ValueError("fact transition references an uninstalled policy")
    if payload.fact_after.origin.accepted_event_ref != event.event_id:
        raise ValueError("fact origin does not identify its mutation event")
    proposal = _require_authorized_fact(state, payload)
    facts, transitions = reduce_fact(
        state.facts,
        state.fact_transitions,
        payload,
        event_type=event.event_type,
        logical_time=logical_time,
        message_observations=state.message_observations,
        operator_observations=state.operator_observations,
    )
    return state.model_copy(
        update={
            "facts": facts,
            "fact_transitions": transitions,
            "fact_proposals": tuple(item for item in state.fact_proposals if item != proposal),
        }
    )


def _fact_v2_committed(state: ReducerState, event: WorldEvent) -> ReducerState:
    """Replay the sealed Fact-v2 effect only through its adjacent v3 manifest."""

    if (
        not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
    ):
        raise ValueError("Fact v2 commit requires adjacent AcceptanceRecorded authority")
    acceptance_ref = state.committed_world_event_refs[-1]
    retained = next(
        (
            item
            for item in state.acceptance_manifests_v3
            if item.acceptance_event_ref == acceptance_ref.event_id
            and item.acceptance_event_payload_hash == acceptance_ref.payload_hash
        ),
        None,
    )
    if retained is None:
        raise ValueError("Fact v2 commit requires a retained manifest v3 authority")
    manifest = retained.manifest
    if (
        manifest.status != "accepted"
        or len(manifest.proposals) != 1
        or len(manifest.authorized_effects) != 1
        or event.causation_id != acceptance_ref.event_id
    ):
        raise ValueError("Fact v2 commit does not bind its adjacent manifest authority")
    effect = manifest.authorized_effects[0]
    if (
        effect.ordinal != 0
        or effect.event_id != event.event_id
        or effect.event_type != event.event_type
        or effect.payload_hash != event.payload_hash
        or len(effect.authority_refs) != 1
    ):
        raise ValueError("Fact v2 commit does not match its authorized effect")
    payload = rehydrate_fact_commit_materialized_v2_json(event.payload_json)
    summary = manifest.proposals[0]
    authority = effect.authority_refs[0]
    change = next((item for item in summary.changes if item.change_id == payload.change_id), None)
    if (
        payload.policy_refs != ("policy:fact-commit.2",)
        or payload.acceptance_id != manifest.acceptance_id
        or payload.proposal_id != summary.proposal_id
        or payload.evaluated_world_revision != manifest.evaluated_world_revision
        or change is None
        or authority.proposal_id != summary.proposal_id
        or authority.authority_kind != "change"
        or authority.authority_id != payload.change_id
        or authority.authority_hash != payload.full_change_authority_hash
        or change.full_change_authority_hash != payload.full_change_authority_hash
        or change.target_id != payload.fact_id
        or change.transition != "commit"
        or change.expected_entity_revision != payload.expected_entity_revision
        or change.evidence_refs != tuple(item.ref_id for item in payload.evidence_refs)
        or change.policy_refs != payload.policy_refs
    ):
        raise ValueError("Fact v2 commit payload does not match its manifest change authority")
    projection_change = materialized_fact_v2_as_projection_change(
        payload=payload,
        event_id=event.event_id,
        logical_time=_require_life_time(state, event),
    )
    facts, transitions = reduce_fact(
        state.facts,
        state.fact_transitions,
        projection_change,
        event_type="FactCommittedV2",
        logical_time=_require_life_time(state, event),
        message_observations=state.message_observations,
        operator_observations=state.operator_observations,
    )
    return state.model_copy(update={"facts": facts, "fact_transitions": transitions})


def _v2_goal_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = V2GoalChangedPayload.model_validate_json(event.payload_json)
    proposal = _require_authorized_v2_goal(state, payload)
    goals, transitions = reduce_v2_goal(
        state.goals,
        state.goal_transitions,
        payload,
        event_type=event.event_type,
        event_id=event.event_id,
        logical_time=logical_time,
        actor_authorities=state.actor_authorities,
        committed_events=state.committed_world_event_refs,
        random_draws=(),
        world_occurrences=state.world_occurrences,
        facts=state.facts,
        experiences=state.experiences,
        clock_transition_history=state.clock_transition_history,
    )
    remaining = tuple(item for item in state.goal_proposals if item != proposal)
    return state.model_copy(
        update={
            "goals": goals,
            "goal_transitions": transitions,
            "goal_proposals": remaining,
            "goal_proposal_ids": tuple(item.proposal_id for item in remaining),
        }
    )


def _v2_goal_expired(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = V2GoalExpiredPayload.model_validate_json(event.payload_json)
    if payload.world_id != event.world_id:
        raise ValueError("goal expiry payload belongs to another world")
    goals, transitions = reduce_v2_goal_expiry(
        state.goals,
        state.goal_transitions,
        payload,
        event_type=event.event_type,
        event_id=event.event_id,
        logical_time=logical_time,
        clock_transition_history=state.clock_transition_history,
    )
    return state.model_copy(update={"goals": goals, "goal_transitions": transitions})


def _v2_location_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = V2LocationChangedPayload.model_validate_json(event.payload_json)
    proposal = _require_authorized_v2_location(state, payload)
    locations, transitions = reduce_v2_location(
        state.locations,
        state.location_transitions,
        payload,
        event_type=event.event_type,
        event_id=event.event_id,
        logical_time=logical_time,
        actor_authorities=state.actor_authorities,
        committed_events=state.committed_world_event_refs,
    )
    remaining = tuple(item for item in state.location_proposals if item != proposal)
    return state.model_copy(
        update={
            "locations": locations,
            "location_transitions": transitions,
            "location_proposals": remaining,
            "location_proposal_ids": tuple(item.proposal_id for item in remaining),
        }
    )


def _require_authorized_v2_location(
    state: ReducerState,
    payload: V2LocationChangedPayload,
) -> V2LocationProposalProjection:
    proposal = next(
        (item for item in state.location_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("Location transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("Location transition requires its accepted decision")
    if (
        not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
    ):
        raise ValueError("Location transition requires adjacent AcceptanceRecorded authority")
    if (
        proposal.transition_kind != payload.operation
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or proposal.policy_refs != payload.policy_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted Location transition does not match its proposal")
    return proposal


def _v2_resource_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = V2ResourceChangedPayload.model_validate_json(event.payload_json)
    proposal = _require_authorized_v2_resource(state, payload)
    resources, transitions = reduce_v2_resource(
        state.resources,
        state.resource_transitions,
        payload,
        event_type=event.event_type,
        event_id=event.event_id,
        logical_time=logical_time,
        actor_authorities=state.actor_authorities,
        committed_events=state.committed_world_event_refs,
    )
    remaining = tuple(item for item in state.resource_proposals if item != proposal)
    return state.model_copy(
        update={
            "resources": resources,
            "resource_transitions": transitions,
            "resource_proposals": remaining,
            "resource_proposal_ids": tuple(item.proposal_id for item in remaining),
        }
    )


def _v2_resource_clock_adjusted(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = V2ResourceClockAdjustedPayload.model_validate_json(event.payload_json)
    reduce_v2_resource_clock_adjustment(state.resources, payload)
    raise AssertionError("unreachable Resource recovery capability")


def _require_authorized_v2_resource(
    state: ReducerState,
    payload: V2ResourceChangedPayload,
) -> V2ResourceProposalProjection:
    proposal = next(
        (item for item in state.resource_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("Resource transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("Resource transition requires its accepted decision")
    if (
        not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
    ):
        raise ValueError("Resource transition requires adjacent AcceptanceRecorded authority")
    if (
        proposal.transition_kind != payload.operation
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or proposal.policy_refs != payload.policy_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted Resource transition does not match its proposal")
    return proposal


def _v2_attention_changed(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = V2AttentionChangedPayload.model_validate_json(event.payload_json)
    proposal = _require_authorized_v2_attention(state, payload)
    attentions, transitions = reduce_v2_attention(
        state.attentions,
        state.attention_transitions,
        payload,
        event_type=event.event_type,
        event_id=event.event_id,
        logical_time=logical_time,
        actor_authorities=state.actor_authorities,
        committed_events=state.committed_world_event_refs,
        plans=state.plans,
        world_occurrences=state.world_occurrences,
        triggers=state.trigger_processes,
    )
    remaining = tuple(item for item in state.attention_proposals if item != proposal)
    return state.model_copy(
        update={
            "attentions": attentions,
            "attention_transitions": transitions,
            "attention_proposals": remaining,
            "attention_proposal_ids": tuple(item.proposal_id for item in remaining),
        }
    )


def _require_authorized_v2_attention(
    state: ReducerState,
    payload: V2AttentionChangedPayload,
) -> V2AttentionProposalProjection:
    proposal = next(
        (item for item in state.attention_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("Attention transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("Attention transition requires its accepted decision")
    if (
        not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
    ):
        raise ValueError("Attention transition requires adjacent AcceptanceRecorded authority")
    if (
        proposal.transition_kind != payload.operation
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.actor_ref != payload.attention_after.actor_ref
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or proposal.policy_refs != payload.policy_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted Attention transition does not match its proposal")
    return proposal


def _require_authorized_v2_goal(
    state: ReducerState,
    payload: V2GoalChangedPayload,
) -> V2GoalProposalProjection:
    proposal = next(
        (item for item in state.goal_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("Goal transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("Goal transition requires its accepted decision")
    if (
        not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
    ):
        raise ValueError("Goal transition requires adjacent AcceptanceRecorded authority")
    if (
        proposal.transition_kind != payload.operation
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted Goal transition does not match its proposal")
    return proposal


def _require_authorized_fact(
    state: ReducerState,
    payload: FactAuthorizedMutationPayload,
) -> FactProposalProjection:
    proposal = next(
        (item for item in state.fact_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("fact transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("fact transition requires its accepted decision")
    if (
        not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
    ):
        raise ValueError("fact transition requires adjacent AcceptanceRecorded authority")
    if (
        proposal.transition_kind != getattr(payload, "operation", None)
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted fact transition does not match its proposal")
    return proposal


def _require_authorized_commitment(
    state: ReducerState,
    payload: CommitmentAuthorizedMutationPayload,
) -> CommitmentProposalProjection:
    proposal = next(
        (item for item in state.commitment_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("commitment transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("commitment transition requires its accepted decision")
    if (
        not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
    ):
        raise ValueError("commitment transition requires adjacent AcceptanceRecorded authority")
    if (
        proposal.transition_kind != getattr(payload, "operation", None)
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted commitment transition does not match its proposal")
    return proposal


def _require_authorized_thread(
    state: ReducerState,
    payload: ThreadAuthorizedMutationPayload,
) -> ThreadProposalProjection:
    proposal = next(
        (item for item in state.thread_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("thread transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("thread transition requires its accepted decision")
    if (
        not state.committed_world_event_refs
        or state.committed_world_event_refs[-1].event_type != "AcceptanceRecorded"
        or not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
    ):
        raise ValueError("thread transition requires adjacent AcceptanceRecorded authority")
    if (
        proposal.transition_kind != getattr(payload, "operation", None)
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted thread transition does not match its proposal")
    return proposal


def _require_authorized_relationship(
    state: ReducerState,
    payload: RelationshipAuthorizedMutationPayload,
    *,
    transition_kind: str,
) -> RelationshipProposalProjection:
    proposal = next(
        (item for item in state.relationship_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("relationship transition requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("relationship transition requires its accepted decision")
    if (
        proposal.transition_kind != transition_kind
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted relationship transition does not match its proposal")
    return proposal


def _require_authorized_affect(
    state: ReducerState,
    payload: AffectAuthorizedMutationPayload,
    *,
    transition_kind: str,
) -> AffectProposalProjection:
    proposal = next(
        (item for item in state.affect_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("affect transition requires a persisted proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("affect transition requires its accepted decision")
    if (
        proposal.transition_kind != transition_kind
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or proposal.appraisal_refs != payload.appraisal_refs
        or proposal.policy_refs != payload.policy_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted affect transition does not match its proposal")
    return proposal


def _validate_appraisal_meaning_refs(
    appraisals: tuple[AppraisalProjection, ...],
    refs: tuple[AppraisalMeaningRef, ...],
) -> None:
    for ref in refs:
        appraisal = next(
            (item for item in appraisals if item.appraisal_id == ref.appraisal_id),
            None,
        )
        if (
            appraisal is None
            or appraisal.status != "active"
            or appraisal.source_cluster_ref != ref.source_cluster_ref
            or appraisal.origin.change_id != ref.accepted_change_id
            or appraisal.origin.transition_id != ref.accepted_transition_id
            or not any(item.hypothesis_id == ref.hypothesis_id for item in appraisal.hypotheses)
        ):
            raise ValueError("affect appraisal meaning does not resolve to authority")


def _require_installed_affect_origin(episode: AffectEpisodeProjection) -> None:
    if (
        episode.origin.matrix_catalog_version != INSTALLED_AFFECT_MATRIX_VERSION
        or episode.origin.policy_refs != INSTALLED_AFFECT_POLICY_REFS
    ):
        raise ValueError("affect origin references an uninstalled matrix policy")


def _require_authorized_appraisal(
    state: ReducerState,
    payload: (AppraisalAcceptedPayload | AppraisalContradictedPayload | AppraisalSupersededPayload),
    *,
    transition_kind: str,
) -> AppraisalProposalProjection:
    trigger = next(
        (item for item in state.trigger_processes if item.trigger_id == payload.trigger_id),
        None,
    )
    proposal = next(
        (item for item in state.appraisal_proposals if item.proposal_id == payload.proposal_id),
        None,
    )
    if (
        trigger is None
        or trigger.process_kind not in {"npc_world_appraisal", "interaction_appraisal"}
        or trigger.state != "claimed"
    ):
        raise ValueError("appraisal transition requires a claimed appraisal trigger")
    if proposal is None:
        raise ValueError("appraisal transition requires a persisted proposal")
    if (
        proposal.transition_kind != transition_kind
        or proposal.change_id != payload.change_id
        or proposal.trigger_id != payload.trigger_id
        or proposal.trigger_ref != trigger.trigger_ref
        or proposal.source_evidence_ref != trigger.source_evidence_ref
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or proposal.policy_refs != payload.policy_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted appraisal transition does not match its proposal")
    return proposal


def _require_authorized_private_impression(
    state: ReducerState,
    payload: PrivateImpressionAuthorizedPayload,
) -> PrivateImpressionProposalProjection:
    proposal = next(
        (
            item
            for item in state.private_impression_proposals
            if item.proposal_id == payload.proposal_id
        ),
        None,
    )
    decision = next(
        (item for item in state.acceptance_decisions if item.proposal_id == payload.proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("private impression requires a persisted typed proposal")
    if (
        decision is None
        or decision.status != "accepted"
        or decision.acceptance_id != payload.acceptance_id
        or decision.accepted_change_id != payload.change_id
        or decision.accepted_change_hash != payload.accepted_change_hash
    ):
        raise ValueError("private impression requires its accepted decision")
    if (
        proposal.transition_kind != "open"
        or proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or proposal.appraisal_refs != payload.appraisal_refs
        or proposal.policy_refs != payload.policy_refs
        or json.loads(proposal.proposed_mutation.payload_json) != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted private impression does not match its proposal")
    return proposal


def _validate_private_impression_source_events(
    state: ReducerState, payload: PrivateImpressionAcceptedPayload
) -> None:
    """Bind each stored source reference to the exact committed evidence bytes."""
    for source_ref, evidence in zip(
        payload.impression.source_refs, payload.evidence_refs, strict=True
    ):
        committed = next(
            (item for item in state.committed_world_event_refs if item.event_id == source_ref),
            None,
        )
        if (
            committed is None
            or committed.world_revision != evidence.source_world_revision
            or committed.payload_hash != evidence.immutable_hash
        ):
            raise ValueError("private impression source refs do not bind committed evidence")


def _require_installed_appraisal_origin(appraisal: AppraisalProjection) -> None:
    if (
        appraisal.origin.matrix_catalog_version != INSTALLED_APPRAISAL_MATRIX_VERSION
        or appraisal.origin.clustering_policy_version != INSTALLED_SOURCE_CLUSTERING_VERSION
    ):
        raise ValueError("appraisal origin references an uninstalled matrix policy")


_EVENTS = {
    definition.event_type: definition
    for definition in (
        EventDefinition("WorldStarted", RevisionClass.WORLD, _world_started),
        EventDefinition(
            "ActorAuthorityBootstrapped", RevisionClass.WORLD, _actor_authority_changed
        ),
        EventDefinition("ActorAuthorityRotated", RevisionClass.WORLD, _actor_authority_changed),
        EventDefinition("ActorAuthorityRevoked", RevisionClass.WORLD, _actor_authority_changed),
        EventDefinition("ActorAuthorityCompensated", RevisionClass.WORLD, _actor_authority_changed),
        EventDefinition("CapabilityGranted", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("CapabilityRevised", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("CapabilityRevoked", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("CapabilityCompensated", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("ConsentGranted", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("ConsentRevised", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("ConsentRevoked", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("ConsentCompensated", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("PrivacyPolicyRevised", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("PrivacyPolicyRevoked", RevisionClass.WORLD, _authorization_changed),
        EventDefinition("PrivacyPolicyCompensated", RevisionClass.WORLD, _authorization_changed),
        EventDefinition(
            "ProviderMediaGrantRecorded", RevisionClass.WORLD, _provider_media_grant_recorded
        ),
        EventDefinition("PhotoCandidateOpened", RevisionClass.WORLD, _photo_candidate_opened),
        EventDefinition(
            "PhotoCandidateUnrenderable", RevisionClass.WORLD, _photo_candidate_unrenderable
        ),
        EventDefinition("PhotoCandidateExpired", RevisionClass.WORLD, _photo_candidate_expired),
        EventDefinition("ImageEvidenceDeclared", RevisionClass.WORLD, _image_evidence_declared),
        EventDefinition(
            "RecipientScopedImageEvidenceDeclared",
            RevisionClass.WORLD,
            _recipient_scoped_image_evidence_declared,
        ),
        EventDefinition("AppearanceStateRecorded", RevisionClass.WORLD, _appearance_state_recorded),
        EventDefinition(
            "VisiblePhysicalStateRecorded", RevisionClass.WORLD, _visible_physical_state_recorded
        ),
        EventDefinition("RandomDrawRecorded", RevisionClass.WORLD, _random_draw_recorded),
        EventDefinition(
            "MediaSelectionProposalRecorded",
            RevisionClass.DELIBERATION,
            _media_selection_proposal_recorded,
        ),
        EventDefinition("MediaOpportunityFrozen", RevisionClass.WORLD, _media_opportunity_frozen),
        EventDefinition("MediaPlanRecorded", RevisionClass.WORLD, _media_plan_recorded),
        EventDefinition(
            "MediaNotRenderableRecorded", RevisionClass.WORLD, _media_not_renderable_recorded
        ),
        EventDefinition(
            "MediaRenderArtifactRecorded", RevisionClass.WORLD, _media_render_artifact_recorded
        ),
        EventDefinition("MediaInspectionRecorded", RevisionClass.WORLD, _media_inspection_recorded),
        EventDefinition("MediaRepairAuthorized", RevisionClass.WORLD, _media_repair_authorized),
        EventDefinition("MediaPreviewGenerated", RevisionClass.WORLD, _media_preview_generated),
        EventDefinition("MediaPreviewFailed", RevisionClass.WORLD, _media_preview_failed),
        EventDefinition(
            "MediaAutomaticDeliveryApproved",
            RevisionClass.WORLD,
            _media_automatic_delivery_approved,
        ),
        EventDefinition("MediaDeliveryShared", RevisionClass.WORLD, _media_delivery_shared),
        EventDefinition(
            "InteractionBidProposalRecorded",
            RevisionClass.DELIBERATION,
            _interaction_bid_proposal_recorded,
        ),
        EventDefinition("InteractionBidOpened", RevisionClass.WORLD, _interaction_bid_opened),
        EventDefinition(
            "MediaDeliveryThreadProposalRecorded",
            RevisionClass.DELIBERATION,
            _media_thread_proposal_recorded,
        ),
        EventDefinition("MediaDeliveryThreadOpened", RevisionClass.WORLD, _media_thread_changed),
        EventDefinition("MediaDeliveryThreadUpdated", RevisionClass.WORLD, _media_thread_changed),
        EventDefinition("ObservationRecorded", RevisionClass.WORLD, _observation_recorded),
        EventDefinition(
            "OperatorObservationRecorded",
            RevisionClass.DELIBERATION,
            _operator_observation_recorded,
        ),
        EventDefinition("ClockAdvanced", RevisionClass.WORLD, _clock_advanced),
        EventDefinition(
            "ExternalObservationRecorded",
            RevisionClass.DELIBERATION,
            _external_observation_recorded,
        ),
        EventDefinition(
            "ExternalObservationProcessed",
            RevisionClass.DELIBERATION,
            _external_observation_processed,
        ),
        EventDefinition(
            "TriggerProcessOpened",
            RevisionClass.DELIBERATION,
            _trigger_process_opened,
        ),
        EventDefinition(
            "TriggerProcessClaimed",
            RevisionClass.DELIBERATION,
            _trigger_process_claimed,
        ),
        EventDefinition(
            "TriggerProcessReclaimed",
            RevisionClass.DELIBERATION,
            _trigger_process_reclaimed,
        ),
        EventDefinition("BudgetAccountConfigured", RevisionClass.WORLD, _budget_account_configured),
        EventDefinition("MessagePayloadStored", RevisionClass.WORLD, _message_payload_stored),
        EventDefinition(
            "ExpressionPayloadDescriptorRecorded",
            RevisionClass.WORLD,
            _expression_payload_descriptor_recorded,
        ),
        EventDefinition("ExpressionPlanAccepted", RevisionClass.WORLD, _expression_plan_accepted),
        EventDefinition(
            "ExpressionBeatAuthorized", RevisionClass.WORLD, _expression_beat_authorized
        ),
        EventDefinition("ExpressionBeatSettled", RevisionClass.WORLD, _expression_beat_settled),
        EventDefinition("ExpressionPlanCompleted", RevisionClass.WORLD, _expression_plan_completed),
        EventDefinition("BudgetReserved", RevisionClass.WORLD, _budget_reserved),
        EventDefinition(
            "ExecutionReceiptRecorded",
            RevisionClass.WORLD,
            _execution_receipt_recorded,
        ),
        EventDefinition("ToolRequestAccepted", RevisionClass.WORLD, _tool_request_accepted),
        EventDefinition("ToolResultAccepted", RevisionClass.WORLD, _tool_result_accepted),
        EventDefinition("PerceptionRequestAccepted", RevisionClass.WORLD, _perception_request_accepted),
        EventDefinition("PerceptionResultAccepted", RevisionClass.WORLD, _perception_result_accepted),
        EventDefinition("BudgetSettled", RevisionClass.WORLD, _budget_settlement_recorded),
        EventDefinition("BudgetReleased", RevisionClass.WORLD, _budget_settlement_recorded),
        EventDefinition("BudgetAdjusted", RevisionClass.WORLD, _budget_settlement_recorded),
        EventDefinition(
            "ActionReconciliationRequired",
            RevisionClass.WORLD,
            _reconciliation_required,
        ),
        EventDefinition(
            "TriggerProcessCompleted",
            RevisionClass.DELIBERATION,
            _trigger_process_completed,
        ),
        EventDefinition("ActionAuthorized", RevisionClass.WORLD, _action_authorized),
        EventDefinition(
            "ActionScheduled",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="scheduled"),
        ),
        EventDefinition(
            "ActionClaimed",
            RevisionClass.WORLD,
            _action_claimed,
        ),
        EventDefinition(
            "ActionReclaimed",
            RevisionClass.WORLD,
            _action_reclaimed,
        ),
        EventDefinition(
            "ActionDispatchStarted",
            RevisionClass.WORLD,
            _action_dispatch_started,
        ),
        EventDefinition("ActionDispatchPending", RevisionClass.WORLD, _action_dispatch_pending),
        EventDefinition(
            "ActionProviderAccepted",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="provider_accepted"),
        ),
        EventDefinition(
            "ActionDelivered",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="delivered"),
        ),
        EventDefinition(
            "ActionFailed",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="failed"),
        ),
        EventDefinition(
            "ActionUnknown",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="unknown"),
        ),
        EventDefinition(
            "ActionCancelled",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="cancelled"),
        ),
        EventDefinition(
            "ActionExpired",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="expired"),
        ),
        EventDefinition("ModelResultRecorded", RevisionClass.DELIBERATION, _model_result_recorded),
        EventDefinition("ProposalRecorded", RevisionClass.DELIBERATION, _proposal_recorded),
        EventDefinition(
            "FactCommitProposalRecorded",
            RevisionClass.DELIBERATION,
            _fact_commit_proposal_audit_v2_recorded,
        ),
        EventDefinition("AcceptanceRecorded", RevisionClass.WORLD, _acceptance_recorded),
        EventDefinition("LegacyAcceptanceAuditRecorded", RevisionClass.WORLD, _audit_only),
        EventDefinition("NpcRegistered", RevisionClass.WORLD, _npc_registered),
        EventDefinition("ActivityPlanned", RevisionClass.WORLD, _activity_planned),
        EventDefinition(
            "ActivityStarted",
            RevisionClass.WORLD,
            partial(
                _activity_transitioned,
                target_status="active",
                allowed_statuses=frozenset({"planned"}),
            ),
        ),
        EventDefinition(
            "ActivityPaused",
            RevisionClass.WORLD,
            partial(
                _activity_transitioned,
                target_status="paused",
                allowed_statuses=frozenset({"active"}),
            ),
        ),
        EventDefinition(
            "ActivityResumed",
            RevisionClass.WORLD,
            partial(
                _activity_transitioned,
                target_status="active",
                allowed_statuses=frozenset({"paused"}),
            ),
        ),
        EventDefinition(
            "ActivityCompleted",
            RevisionClass.WORLD,
            partial(
                _activity_transitioned,
                target_status="completed",
                allowed_statuses=frozenset({"active"}),
            ),
        ),
        EventDefinition(
            "ActivityAbandoned",
            RevisionClass.WORLD,
            partial(
                _activity_transitioned,
                target_status="abandoned",
                allowed_statuses=frozenset({"planned", "active", "paused"}),
            ),
        ),
        EventDefinition(
            "ActivityLifecycleProposalRecorded",
            RevisionClass.DELIBERATION,
            _activity_lifecycle_proposal_recorded,
        ),
        EventDefinition(
            "WorldOccurrenceCommitted",
            RevisionClass.WORLD,
            _world_occurrence_committed,
        ),
        EventDefinition(
            "WorldOccurrenceActivated",
            RevisionClass.WORLD,
            _world_occurrence_activated,
        ),
        EventDefinition(
            "OutcomeObservationRecorded",
            RevisionClass.WORLD,
            _outcome_observation_recorded,
        ),
        EventDefinition(
            "OutcomeProposalRecorded",
            RevisionClass.DELIBERATION,
            _outcome_proposal_recorded,
        ),
        EventDefinition(
            "WorldOccurrenceSettled",
            RevisionClass.WORLD,
            _world_occurrence_settled,
        ),
        EventDefinition("ExperienceCommitted", RevisionClass.WORLD, _experience_committed),
        EventDefinition("LifeContentRecorded", RevisionClass.WORLD, _life_content_recorded),
        EventDefinition(
            "LegacyExperienceCommitted",
            RevisionClass.WORLD,
            _legacy_experience_committed,
        ),
        EventDefinition(
            "WorldOccurrenceCancelled",
            RevisionClass.WORLD,
            partial(_world_occurrence_terminated, target_status="cancelled"),
        ),
        EventDefinition(
            "WorldOccurrenceExpired",
            RevisionClass.WORLD,
            partial(_world_occurrence_terminated, target_status="expired"),
        ),
        EventDefinition("AppraisalAccepted", RevisionClass.WORLD, _appraisal_accepted),
        EventDefinition("AppraisalContradicted", RevisionClass.WORLD, _appraisal_contradicted),
        EventDefinition("AppraisalExpired", RevisionClass.WORLD, _appraisal_expired),
        EventDefinition("AppraisalSuperseded", RevisionClass.WORLD, _appraisal_superseded),
        EventDefinition(
            "PrivateImpressionAccepted", RevisionClass.WORLD, _private_impression_accepted
        ),
        EventDefinition("AffectEpisodeOpened", RevisionClass.WORLD, _affect_episode_opened),
        EventDefinition("AffectEpisodeUpdated", RevisionClass.WORLD, _affect_episode_updated),
        EventDefinition("AffectEpisodeDecayed", RevisionClass.WORLD, _affect_episode_decayed),
        EventDefinition("AffectEpisodeResolved", RevisionClass.WORLD, _affect_episode_resolved),
        EventDefinition(
            "AffectEpisodeSuperseded",
            RevisionClass.WORLD,
            _affect_episode_superseded,
        ),
        EventDefinition("AffectBaselineAdjusted", RevisionClass.WORLD, _affect_baseline_adjusted),
        EventDefinition(
            "RelationshipSignalAccepted",
            RevisionClass.WORLD,
            _relationship_signal_accepted,
        ),
        EventDefinition(
            "RelationshipSlowVariableAdjusted",
            RevisionClass.WORLD,
            _relationship_slow_variable_adjusted,
        ),
        EventDefinition("BoundaryChanged", RevisionClass.WORLD, _boundary_changed),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _thread_changed)
            for event_type in THREAD_PAYLOAD_MODELS
        ),
        EventDefinition("ThreadExpired", RevisionClass.WORLD, _thread_expired),
        EventDefinition("PrivateCommitmentOpened", RevisionClass.WORLD, _commitment_changed),
        EventDefinition("PrivateCommitmentDue", RevisionClass.WORLD, _commitment_clock_changed),
        EventDefinition("PrivateCommitmentFulfilled", RevisionClass.WORLD, _commitment_changed),
        EventDefinition("PrivateCommitmentBroken", RevisionClass.WORLD, _commitment_changed),
        EventDefinition(
            "PrivateCommitmentDeadlineBroken", RevisionClass.WORLD, _commitment_clock_changed
        ),
        EventDefinition("PrivateCommitmentReleased", RevisionClass.WORLD, _commitment_changed),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _v2_goal_changed)
            for event_type in V2_GOAL_PAYLOAD_MODELS
        ),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _v2_goal_expired)
            for event_type in V2_GOAL_MECHANICAL_PAYLOAD_MODELS
        ),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _v2_location_changed)
            for event_type in V2_LOCATION_MUTATION_EVENT_TYPES
        ),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _v2_resource_changed)
            for event_type in V2_RESOURCE_EVENT_TYPES
        ),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _v2_attention_changed)
            for event_type in V2_ATTENTION_MUTATION_EVENT_TYPES
        ),
        EventDefinition(
            "V2ResourceClockAdjusted",
            RevisionClass.WORLD,
            _v2_resource_clock_adjusted,
        ),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _fact_changed)
            for event_type in FACT_PAYLOAD_MODELS
        ),
        EventDefinition("FactCommittedV2", RevisionClass.WORLD, _fact_v2_committed),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _memory_candidate_changed)
            for event_type in MEMORY_CANDIDATE_PAYLOAD_MODELS
        ),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _character_core_changed)
            for event_type in CHARACTER_CORE_PAYLOAD_MODELS
        ),
    )
}


def event_definition(event_type: str) -> EventDefinition:
    try:
        return _EVENTS[event_type]
    except KeyError as exc:
        raise UnknownEventType(f"event type {event_type!r} is not registered") from exc


def event_types() -> frozenset[str]:
    """Return reducer event types for machine contract coverage checks."""

    return frozenset(_EVENTS)


def reduce_event(
    state: ReducerState,
    event: WorldEvent,
    *,
    allow_legacy_plan_owner: bool = False,
) -> ReducerState:
    event_contract(event.event_type).validate_payload(event.payload())
    definition = event_definition(event.event_type)
    if event.event_type == "ActivityPlanned" and allow_legacy_plan_owner:
        reduced = _activity_planned(
            state, event, allow_legacy_missing_owner=allow_legacy_plan_owner
        )
    elif (
        event.event_type
        in {
            "ActivityStarted",
            "ActivityPaused",
            "ActivityResumed",
            "ActivityCompleted",
            "ActivityAbandoned",
        }
        and allow_legacy_plan_owner
    ):
        reduced = definition.reducer(state, event, allow_legacy_unowned_transition=True)
    else:
        reduced = definition.reducer(state, event)
    if definition.revision_class is RevisionClass.WORLD:
        return reduced.model_copy(
            update={
                "committed_world_event_refs": (
                    *reduced.committed_world_event_refs,
                    CommittedWorldEventRef(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        world_revision=len(reduced.committed_world_event_refs) + 1,
                        payload_hash=event.payload_hash,
                        logical_time=event.logical_time,
                        continuation_refs=(
                            (str(event.payload()["appraisal_trigger_ref"]),)
                            if event.event_type == "WorldOccurrenceSettled"
                            else ()
                        ),
                    ),
                )
            }
        )
    return reduced


def require_reducer_bundle(version: str) -> None:
    """Select an installed immutable reducer artifact or fail closed."""

    if version != REDUCER_BUNDLE_VERSION:
        raise ValueError(f"reducer bundle {version!r} is not installed")


def semantic_hash(
    *,
    world_id: str,
    world_revision: int,
    state: ReducerState,
    reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
) -> str:
    require_reducer_bundle(reducer_bundle_version)
    semantic_projection = state.semantic_payload(
        world_id=world_id,
        world_revision=world_revision,
        reducer_bundle_version=reducer_bundle_version,
    )
    encoded = json.dumps(
        semantic_projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_projection(
    *,
    world_id: str,
    world_revision: int,
    deliberation_revision: int,
    ledger_sequence: int,
    state: ReducerState,
    reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
) -> LedgerProjection:
    return LedgerProjection(
        reducer_bundle_version=reducer_bundle_version,
        world_id=world_id,
        world_revision=world_revision,
        deliberation_revision=deliberation_revision,
        ledger_sequence=ledger_sequence,
        logical_time=state.logical_time,
        actor_authorities=state.actor_authorities,
        actor_authority_transitions=state.actor_authority_transitions,
        consumed_actor_root_nonces=state.consumed_actor_root_nonces,
        capability_grants=state.capability_grants,
        capability_transitions=state.capability_transitions,
        consent_grants=state.consent_grants,
        consent_transitions=state.consent_transitions,
        privacy_policies=state.privacy_policies,
        privacy_transitions=state.privacy_transitions,
        provider_media_grants=state.provider_media_grants,
        consumed_authorization_root_nonces=state.consumed_authorization_root_nonces,
        consumed_authorization_challenge_ids=state.consumed_authorization_challenge_ids,
        consumed_authorization_source_ids=state.consumed_authorization_source_ids,
        observation_refs=state.observation_refs,
        message_observations=state.message_observations,
        operator_observations=state.operator_observations,
        committed_world_event_refs=state.committed_world_event_refs,
        clock_transition_history=state.clock_transition_history,
        goals=state.goals,
        goal_transitions=state.goal_transitions,
        goal_proposals=state.goal_proposals,
        goal_proposal_ids=state.goal_proposal_ids,
        locations=state.locations,
        location_transitions=state.location_transitions,
        location_proposals=state.location_proposals,
        location_proposal_ids=state.location_proposal_ids,
        resources=state.resources,
        resource_transitions=state.resource_transitions,
        resource_proposals=state.resource_proposals,
        resource_proposal_ids=state.resource_proposal_ids,
        attentions=state.attentions,
        attention_transitions=state.attention_transitions,
        attention_proposals=state.attention_proposals,
        attention_proposal_ids=state.attention_proposal_ids,
        actions=state.actions,
        pending_actions=state.pending_actions,
        read_only_tool_requests=state.read_only_tool_requests,
        tool_results=state.tool_results,
        perception_requests=state.perception_requests,
        perception_results=state.perception_results,
        appearance_states=state.appearance_states,
        visible_physical_states=state.visible_physical_states,
        photo_candidates=state.photo_candidates,
        media_opportunities=state.media_opportunities,
        media_plans=state.media_plans,
        media_unrenderable_opportunity_ids=state.media_unrenderable_opportunity_ids,
        media_artifacts=state.media_artifacts,
        media_inspections=state.media_inspections,
        media_previews=state.media_previews,
        media_failed_plan_ids=state.media_failed_plan_ids,
        media_delivery_approvals=state.media_delivery_approvals,
        media_deliveries=state.media_deliveries,
        interaction_bids=state.interaction_bids,
        interaction_bid_proposals=state.interaction_bid_proposals,
        media_thread_proposals=state.media_thread_proposals,
        budget_accounts=state.budget_accounts,
        budget_reservations=state.budget_reservations,
        trigger_processes=state.trigger_processes,
        pending_external_observations=state.pending_external_observations,
        execution_receipts=state.execution_receipts,
        budget_settlements=state.budget_settlements,
        reconciliations=state.reconciliations,
        completed_trigger_ids=state.completed_trigger_ids,
        npcs=state.npcs,
        plans=state.plans,
        world_occurrences=state.world_occurrences,
        outcome_observations=state.outcome_observations,
        experiences=state.experiences,
        experience_transitions=state.experience_transitions,
        experience_proposals=state.experience_proposals,
        experience_proposal_ids=state.experience_proposal_ids,
        memory_candidates=state.memory_candidates,
        memory_candidate_transitions=state.memory_candidate_transitions,
        memory_candidate_proposals=state.memory_candidate_proposals,
        memory_candidate_proposal_ids=state.memory_candidate_proposal_ids,
        character_core=state.character_core,
        character_core_transitions=state.character_core_transitions,
        character_core_proposals=state.character_core_proposals,
        character_core_proposal_ids=state.character_core_proposal_ids,
        appraisals=state.appraisals,
        affect_baselines=state.affect_baselines,
        affect_episodes=state.affect_episodes,
        appraisal_proposals=state.appraisal_proposals,
        appraisal_proposal_ids=state.appraisal_proposal_ids,
        affect_proposals=state.affect_proposals,
        affect_proposal_ids=state.affect_proposal_ids,
        relationship_signals=state.relationship_signals,
        relationship_adjustments=state.relationship_adjustments,
        relationship_states=state.relationship_states,
        boundaries=state.boundaries,
        relationship_proposals=state.relationship_proposals,
        relationship_proposal_ids=state.relationship_proposal_ids,
        private_impressions=state.private_impressions,
        private_impression_proposals=state.private_impression_proposals,
        private_impression_proposal_ids=state.private_impression_proposal_ids,
        threads=state.threads,
        thread_transitions=state.thread_transitions,
        thread_proposals=state.thread_proposals,
        thread_proposal_ids=state.thread_proposal_ids,
        commitments=state.commitments,
        commitment_transitions=state.commitment_transitions,
        commitment_proposals=state.commitment_proposals,
        commitment_proposal_ids=state.commitment_proposal_ids,
        facts=state.facts,
        fact_transitions=state.fact_transitions,
        fact_proposals=state.fact_proposals,
        fact_proposal_ids=state.fact_proposal_ids,
        proposal_ids=state.proposal_ids,
        proposal_revisions=state.proposal_revisions,
        model_result_audits=state.model_result_audits,
        proposal_audits=state.proposal_audits,
        acceptance_manifests_v2=state.acceptance_manifests_v2,
        fact_commit_proposal_audits_v2=state.fact_commit_proposal_audits_v2,
        acceptance_manifests_v3=state.acceptance_manifests_v3,
        minimal_reply_manifests=state.minimal_reply_manifests,
        expression_plan_manifests=state.expression_plan_manifests,
        stored_message_payloads=state.stored_message_payloads,
        expression_payload_descriptors=state.expression_payload_descriptors,
        life_content_descriptors=state.life_content_descriptors,
        expression_plans=state.expression_plans,
        expression_beats=state.expression_beats,
        acceptance_decisions=state.acceptance_decisions,
        outcome_proposals=state.outcome_proposals,
        semantic_hash=semantic_hash(
            world_id=world_id,
            world_revision=world_revision,
            state=state,
            reducer_bundle_version=reducer_bundle_version,
        ),
    )
