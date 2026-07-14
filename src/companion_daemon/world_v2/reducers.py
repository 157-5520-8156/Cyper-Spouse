from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
import hashlib
import json
from typing import Any

from pydantic import model_validator

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
from .appraisal_reducers import (
    accept_appraisal,
    contradict_appraisal,
    expire_appraisal,
    supersede_appraisal,
)
from .actor_authority_events import ActorAuthorityMutationPayload
from .actor_authority_reducers import reduce_actor_authority
from .authorization_events import AUTHORIZATION_PAYLOAD_MODELS, authorization_domain
from .authorization_reducers import reduce_authorization
from .batch_invariants import interaction_appraisal_trigger_identity
from .commitment_events import (
    COMMITMENT_ACCEPTED_PAYLOAD_MODELS,
    CommitmentAuthorizedMutationPayload,
    CommitmentChangedPayload,
    CommitmentClockTransitionPayload,
)
from .commitment_reducers import reduce_commitment, reduce_commitment_clock
from .fact_events import FACT_PAYLOAD_MODELS, FactAuthorizedMutationPayload, FactChangedPayload
from .fact_reducers import reduce_fact
from .experience_events import (
    ExperienceAuthorizedMutationPayload,
    ExperienceCommittedPayload,
    LegacyExperienceCommittedPayload,
)
from .errors import UnknownEventType
from .event_catalog import event_contract
from .life_events import (
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
from .memory_events import (
    MEMORY_CANDIDATE_PAYLOAD_MODELS,
    MemoryCandidateAuthorizedMutationPayload,
    MemoryCandidateChangedPayload,
    MemoryEvidenceForgetAuthority,
)
from .memory_reducers import MEMORY_POLICY_REFS, reduce_memory_candidate
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
from .thread_events import (
    THREAD_PAYLOAD_MODELS,
    ThreadAuthorizedMutationPayload,
    ThreadChangedPayload,
    ThreadExpiredPayload,
)
from .thread_reducers import expire_thread, reduce_thread
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
    FactProjection,
    FactProposalProjection,
    FactTransitionProjection,
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
    OutcomeProposalProjection,
    OperatorObservationRef,
    PlanStateProjection,
    ProposalRevisionRef,
    RelationshipAdjustmentProjection,
    RelationshipProposalProjection,
    RelationshipSignalProjection,
    RelationshipStateProjection,
    ThreadProjection,
    ThreadProposalProjection,
    ThreadTransitionProjection,
    TriggerProcess,
    WorldOccurrenceProjection,
    WorldEvent,
)


REDUCER_BUNDLE_VERSION = "world-v2-reducers.14"
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
    if (
        reducer_bundle_version
        not in {"world-v2-reducers.13", REDUCER_BUNDLE_VERSION}
        and isinstance(experience, LegacyExperienceProjection)
    ):
        dumped.pop("authority_contract_version", None)
        dumped["status"] = "committed"
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
    consumed_authorization_root_nonces: tuple[str, ...] = ()
    consumed_authorization_challenge_ids: tuple[str, ...] = ()
    consumed_authorization_source_ids: tuple[str, ...] = ()
    observation_refs: tuple[str, ...] = ()
    message_observations: tuple[MessageObservationRef, ...] = ()
    operator_observations: tuple[OperatorObservationRef, ...] = ()
    committed_world_event_refs: tuple[CommittedWorldEventRef, ...] = ()
    logical_time: datetime | None = None
    actions: tuple[Action, ...] = ()
    pending_actions: tuple[Action, ...] = ()
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
    proposal_ids: tuple[str, ...] = ()
    proposal_revisions: tuple[ProposalRevisionRef, ...] = ()
    acceptance_decisions: tuple[AcceptanceDecisionRef, ...] = ()

    @model_validator(mode="after")
    def pending_index_matches_actions(self) -> ReducerState:
        expected = tuple(
            action for action in self.actions if action.state not in TERMINAL_ACTION_STATES
        )
        if self.pending_actions != expected:
            raise ValueError("pending_actions must equal the non-terminal action index")
        dimensions = tuple(item.dimension for item in self.affect_baselines)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("affect baseline dimensions must be unique")
        if len(self.relationship_states) > 1:
            raise ValueError("world v2.1 permits one primary relationship state")
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
        transition_ids = tuple(
            item.transition_id for item in self.actor_authority_transitions
        )
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("actor authority transition ids must be unique")
        if len(self.consumed_actor_root_nonces) != len(
            set(self.consumed_actor_root_nonces)
        ):
            raise ValueError("consumed actor root nonces must be unique")
        if len(self.consumed_actor_root_nonces) != len(
            self.actor_authority_transitions
        ):
            raise ValueError("actor authority transitions must consume one root nonce")
        projected_ids = set(authority_ids)
        if any(
            item.authority_id not in projected_ids
            for item in self.actor_authority_transitions
        ):
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
        if len(self.consumed_authorization_root_nonces) != len(
            authorization_transitions
        ) or len(self.consumed_authorization_challenge_ids) != len(
            authorization_transitions
        ) or len(self.consumed_authorization_source_ids) != len(
            authorization_transitions
        ):
            raise ValueError(
                "authorization transitions require one root nonce and evidence identity"
            )
        if len(self.consumed_authorization_root_nonces) != len(
            set(self.consumed_authorization_root_nonces)
        ) or len(self.consumed_authorization_challenge_ids) != len(
            set(self.consumed_authorization_challenge_ids)
        ) or len(self.consumed_authorization_source_ids) != len(
            set(self.consumed_authorization_source_ids)
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
                    item
                    for item in transitions
                    if getattr(item, id_field) == entity_id
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
            item.proposal_id not in set(self.thread_proposal_ids)
            for item in self.thread_proposals
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
            if tuple(item.entity_revision for item in lineage) != tuple(
                range(1, len(lineage) + 1)
            ):
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
            item.commitment_id not in set(commitment_ids)
            for item in self.commitment_transitions
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
            if tuple(item.entity_revision for item in lineage) != tuple(
                range(1, len(lineage) + 1)
            ):
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
        if any(
            item.proposal_id not in set(self.fact_proposal_ids)
            for item in self.fact_proposals
        ):
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
            lineage = tuple(
                item for item in self.fact_transitions if item.fact_id == fact.fact_id
            )
            if not lineage or lineage[0].operation != "commit":
                raise ValueError("fact lineage must begin with commit")
            if tuple(item.entity_revision for item in lineage) != tuple(
                range(1, len(lineage) + 1)
            ):
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
        if any(
            item.experience_id not in hardened_ids
            for item in self.experience_transitions
        ):
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
                or transition.semantic_fingerprint_after
                != experience.semantic_fingerprint
                or transition.accepted_event_ref
                != experience.origin.accepted_event_ref
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
            if tuple(item.entity_revision for item in lineage) != tuple(
                range(1, len(lineage) + 1)
            ):
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
        if len(self.memory_candidate_proposal_ids) != len(
            set(self.memory_candidate_proposal_ids)
        ):
            raise ValueError("memory candidate proposal ids must be unique")
        if any(
            item.proposal_id not in set(self.memory_candidate_proposal_ids)
            for item in self.memory_candidate_proposals
        ):
            raise ValueError("pending memory proposal is absent from durable index")
        return self

    def semantic_payload(
        self,
        *,
        world_id: str,
        world_revision: int,
        reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
    ) -> dict[str, Any]:
        payload = {
            "reducer_bundle_version": reducer_bundle_version,
            "schema_version": "world-v2.1",
            "world_id": world_id,
            "world_revision": world_revision,
            "actor_authorities": tuple(
                item.model_dump(mode="json") for item in self.actor_authorities
            ),
            "actor_authority_transitions": tuple(
                item.model_dump(mode="json") for item in self.actor_authority_transitions
            ),
            "consumed_actor_root_nonces": self.consumed_actor_root_nonces,
            "capability_grants": tuple(
                item.model_dump(mode="json") for item in self.capability_grants
            ),
            "capability_transitions": tuple(
                item.model_dump(mode="json") for item in self.capability_transitions
            ),
            "consent_grants": tuple(
                item.model_dump(mode="json") for item in self.consent_grants
            ),
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
                        REDUCER_BUNDLE_VERSION,
                    }
                    else item.model_dump(
                        mode="json", exclude={"actor", "channel", "payload_ref"}
                    )
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
            "actions": tuple(action.model_dump(mode="json") for action in self.actions),
            "pending_actions": tuple(
                action.model_dump(mode="json") for action in self.pending_actions
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
            "plans": tuple(plan.model_dump(mode="json") for plan in self.plans),
            "world_occurrences": tuple(
                (
                    occurrence.model_dump(mode="json")
                    if reducer_bundle_version
                    in {"world-v2-reducers.13", REDUCER_BUNDLE_VERSION}
                    else occurrence.model_dump(
                        mode="json",
                        exclude={
                            "settlement_event_ref",
                            "settlement_world_revision",
                            "settlement_payload_hash",
                        },
                    )
                )
                for occurrence in self.world_occurrences
            ),
            "outcome_observations": tuple(
                observation.model_dump(mode="json") for observation in self.outcome_observations
            ),
            "experiences": tuple(
                _experience_semantic_dump(
                    experience, reducer_bundle_version=reducer_bundle_version
                )
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
        if reducer_bundle_version in {
            "world-v2-reducers.10",
            "world-v2-reducers.11",
            "world-v2-reducers.12",
            "world-v2-reducers.13",
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
            REDUCER_BUNDLE_VERSION,
        }:
            payload["facts"] = tuple(item.model_dump(mode="json") for item in self.facts)
            payload["fact_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.fact_transitions
            )
        if reducer_bundle_version in {
            "world-v2-reducers.13",
            REDUCER_BUNDLE_VERSION,
        }:
            payload["experience_transitions"] = tuple(
                item.model_dump(mode="json") for item in self.experience_transitions
            )
        if reducer_bundle_version == REDUCER_BUNDLE_VERSION:
            payload["memory_candidates"] = tuple(
                item.model_dump(mode="json") for item in self.memory_candidates
            )
            payload["memory_candidate_transitions"] = tuple(
                item.model_dump(mode="json")
                for item in self.memory_candidate_transitions
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
            (
                item
                for item in state.outcome_proposals
                if item.outcome_proposal_id == proposal_id
            ),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        # Outcome proposals are a durable deliberation audit used to explain a
        # later settlement or rejection; deciding one does not erase that audit.
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


class _ThreadProposalStore:
    def validate_and_store(
        self, state: ReducerState, event: object, proposal: object
    ) -> ReducerState:
        if not isinstance(event, WorldEvent) or not isinstance(
            proposal, ThreadProposalProjection
        ):
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
                    item
                    for item in state.commitment_proposals
                    if item.proposal_id != proposal_id
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

    def find(
        self, state: ReducerState, proposal_id: str
    ) -> ExperienceProposalProjection | None:
        return next(
            (
                item
                for item in state.experience_proposals
                if item.proposal_id == proposal_id
            ),
            None,
        )

    def discard(self, state: ReducerState, proposal_id: str) -> ReducerState:
        return state.model_copy(
            update={
                "experience_proposals": tuple(
                    item
                    for item in state.experience_proposals
                    if item.proposal_id != proposal_id
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
            (
                item
                for item in state.memory_candidate_proposals
                if item.proposal_id == proposal_id
            ),
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

_TYPED_PROPOSAL_STORES = {
    "proposal-contract:appraisal-legacy.1": _LegacyAppraisalProposalStore(),
    "proposal-contract:affect-legacy.1": _LegacyAffectProposalStore(),
    "proposal-contract:outcome-legacy.1": _LegacyOutcomeProposalStore(),
    "proposal-contract:relationship.1": _RelationshipProposalStore(),
    "proposal-contract:thread.1": _ThreadProposalStore(),
    "proposal-contract:commitment.1": _CommitmentProposalStore(),
    "proposal-contract:fact.1": _FactProposalStore(),
    "proposal-contract:experience.1": _ExperienceProposalStore(),
    "proposal-contract:memory-candidate.1": _MemoryCandidateProposalStore(),
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
        raise ValueError(
            "actor authority transition must be pinned to current logical time"
        )
    logical_time = event.logical_time
    payload = ActorAuthorityMutationPayload.model_validate_json(event.payload_json)
    authorities, history, nonces = reduce_actor_authority(
        state.actor_authorities,
        state.actor_authority_transitions,
        state.consumed_actor_root_nonces,
        payload,
        event=event,
        logical_time=logical_time,
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


def _proposal_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    raw = event.payload()
    proposal_id = raw.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise ValueError("ProposalRecorded requires proposal_id")
    if proposal_id in state.proposal_ids:
        raise ValueError("proposal identity is already registered")
    registration = _TYPED_PROPOSAL_REGISTRY.registration_for_record(
        event.event_type, raw
    )
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
        accept_relationship_signal(state.relationship_signals, proposed_payload, logical_time=logical_time)
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
    proposed_payload = proposed_model.model_validate_json(
        proposal.proposed_mutation.payload_json
    )
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
    proposal = proposal or MemoryCandidateProposalProjection.model_validate_json(
        event.payload_json
    )
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


def _acceptance_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    raw = event.payload()
    proposal_id = raw.get("proposal_id")
    evaluated_world_revision = raw.get("evaluated_world_revision")
    if not isinstance(proposal_id, str) or not isinstance(evaluated_world_revision, int):
        raise ValueError("AcceptanceRecorded requires proposal and evaluated revision")
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
        (
            item
            for item in state.experience_proposals
            if item.proposal_id == proposal_id
        ),
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
    if target <= origin:
        raise ValueError("ClockAdvanced logical_time_to must follow logical_time_from")
    if state.logical_time is not None and origin != state.logical_time:
        raise ValueError("ClockAdvanced logical_time_from does not match current logical time")
    if state.logical_time is not None and target <= state.logical_time:
        raise ValueError("logical time cannot move backwards or remain unchanged")
    return state.model_copy(update={"logical_time": target})


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
    return state.model_copy(
        update={
            "actions": (*state.actions, action),
            "pending_actions": (*state.pending_actions, action),
        }
    )


def _budget_reserved(state: ReducerState, event: WorldEvent) -> ReducerState:
    reservation = _model_from_payload(event, "reservation", BudgetReservation)
    if any(item.reservation_id == reservation.reservation_id for item in state.budget_reservations):
        raise ValueError(f"budget reservation {reservation.reservation_id!r} already exists")
    if reservation.state != "reserved":
        raise ValueError("BudgetReserved requires reserved state")
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
    if process.process_kind in {"npc_world_appraisal", "interaction_appraisal"}:
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
    if process.process_kind in {"npc_world_appraisal", "interaction_appraisal"}:
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
                or committed.event_type not in FACT_PAYLOAD_MODELS
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
                or evidence.immutable_hash != _canonical_model_hash(
                    transition.values_after
                )
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
                or evidence.immutable_hash != _canonical_model_hash(candidate)
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


def _activity_planned(state: ReducerState, event: WorldEvent) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, ActivityPlannedPayload)
    return state.model_copy(update={"plans": plan_activity(state.plans, state.npcs, payload)})


def _activity_transitioned(
    state: ReducerState,
    event: WorldEvent,
    *,
    target_status: str,
    allowed_statuses: frozenset[str],
) -> ReducerState:
    payload = _validated_life_payload(state, event, ActivityTransitionPayload)
    return state.model_copy(
        update={
            "plans": transition_activity(
                state.plans,
                payload,
                target_status=target_status,
                allowed_statuses=allowed_statuses,
                logical_time=_require_life_time(state, event),
            )
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
    if any(
        item.transition_id == payload.transition_id
        for item in state.experience_transitions
    ):
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
        (
            item
            for item in state.acceptance_decisions
            if item.proposal_id == payload.proposal_id
        ),
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
        or json.loads(proposal.proposed_mutation.payload_json)
        != payload.model_dump(mode="json")
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
        (
            item
            for item in state.experience_proposals
            if item.proposal_id == payload.proposal_id
        ),
        None,
    )
    decision = next(
        (
            item
            for item in state.acceptance_decisions
            if item.proposal_id == payload.proposal_id
        ),
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
    if (
        not state.acceptance_decisions
        or state.acceptance_decisions[-1] != decision
    ):
        raise ValueError("experience commit requires adjacent AcceptanceRecorded authority")
    if (
        proposal.change_id != payload.change_id
        or proposal.transition_id != payload.transition_id
        or proposal.evaluated_world_revision != payload.evaluated_world_revision
        or proposal.expected_entity_revision != payload.expected_entity_revision
        or proposal.proposed_change_hash != payload.accepted_change_hash
        or proposal.evidence_refs != payload.evidence_refs
        or json.loads(proposal.proposed_mutation.payload_json)
        != payload.model_dump(mode="json")
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


def _relationship_signal_accepted(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
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


def _relationship_slow_variable_adjusted(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    logical_time = _require_life_time(state, event)
    payload = RelationshipSlowVariableAdjustedPayload.model_validate_json(event.payload_json)
    if payload.policy_refs != INSTALLED_RELATIONSHIP_POLICY_REFS:
        raise ValueError("relationship adjustment references an uninstalled policy")
    transition_kind = "compensate" if payload.operation == "compensate" else "adjust"
    proposal = _require_authorized_relationship(
        state, payload, transition_kind=transition_kind
    )
    states, history = adjust_relationship_slow_variables(
        state.relationship_states,
        state.relationship_adjustments,
        state.relationship_signals,
        payload,
        logical_time=logical_time,
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
    proposal = _require_authorized_relationship(
        state, payload, transition_kind=transition_kind
    )
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
            "thread_proposals": tuple(
                item for item in state.thread_proposals if item != proposal
            ),
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
            "fact_proposals": tuple(
                item for item in state.fact_proposals if item != proposal
            ),
        }
    )


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
        or json.loads(proposal.proposed_mutation.payload_json)
        != payload.model_dump(mode="json")
    ):
        raise ValueError("accepted fact transition does not match its proposal")
    return proposal


def _require_authorized_commitment(
    state: ReducerState,
    payload: CommitmentAuthorizedMutationPayload,
) -> CommitmentProposalProjection:
    proposal = next(
        (
            item
            for item in state.commitment_proposals
            if item.proposal_id == payload.proposal_id
        ),
        None,
    )
    decision = next(
        (
            item
            for item in state.acceptance_decisions
            if item.proposal_id == payload.proposal_id
        ),
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
        or json.loads(proposal.proposed_mutation.payload_json)
        != payload.model_dump(mode="json")
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
        or json.loads(proposal.proposed_mutation.payload_json)
        != payload.model_dump(mode="json")
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
        EventDefinition(
            "ActorAuthorityCompensated", RevisionClass.WORLD, _actor_authority_changed
        ),
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
        EventDefinition("BudgetReserved", RevisionClass.WORLD, _budget_reserved),
        EventDefinition(
            "ExecutionReceiptRecorded",
            RevisionClass.WORLD,
            _execution_receipt_recorded,
        ),
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
        EventDefinition("ProposalRecorded", RevisionClass.DELIBERATION, _proposal_recorded),
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
            EventDefinition(event_type, RevisionClass.WORLD, _fact_changed)
            for event_type in FACT_PAYLOAD_MODELS
        ),
        *(
            EventDefinition(event_type, RevisionClass.WORLD, _memory_candidate_changed)
            for event_type in MEMORY_CANDIDATE_PAYLOAD_MODELS
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


def reduce_event(state: ReducerState, event: WorldEvent) -> ReducerState:
    event_contract(event.event_type).validate_payload(event.payload())
    definition = event_definition(event.event_type)
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
        consumed_authorization_root_nonces=state.consumed_authorization_root_nonces,
        consumed_authorization_challenge_ids=state.consumed_authorization_challenge_ids,
        consumed_authorization_source_ids=state.consumed_authorization_source_ids,
        observation_refs=state.observation_refs,
        message_observations=state.message_observations,
        operator_observations=state.operator_observations,
        committed_world_event_refs=state.committed_world_event_refs,
        actions=state.actions,
        pending_actions=state.pending_actions,
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
        acceptance_decisions=state.acceptance_decisions,
        outcome_proposals=state.outcome_proposals,
        semantic_hash=semantic_hash(
            world_id=world_id,
            world_revision=world_revision,
            state=state,
            reducer_bundle_version=reducer_bundle_version,
        ),
    )
