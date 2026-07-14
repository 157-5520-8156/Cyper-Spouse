"""Strict payload contracts for the World v2 lived-world vertical slice."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from pydantic import Field, model_validator

from .schemas import (
    EvidenceRef,
    ExperienceProjection,
    FrozenModel,
    NpcProjection,
    OutcomeObservationProjection,
    PlanStateProjection,
    WorldOccurrenceProjection,
)


class DomainMutationPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = ()


class NpcRegisteredPayload(DomainMutationPayload):
    npc: NpcProjection

    @model_validator(mode="after")
    def creates_revision_one_npc(self) -> NpcRegisteredPayload:
        if self.expected_entity_revision != 0 or self.npc.entity_revision != 1:
            raise ValueError("NpcRegistered must create entity revision one")
        if self.npc.status != "active":
            raise ValueError("NpcRegistered requires an active NPC")
        return self


class ActivityPlannedPayload(DomainMutationPayload):
    plan: PlanStateProjection

    @model_validator(mode="after")
    def creates_planned_activity(self) -> ActivityPlannedPayload:
        if self.expected_entity_revision != 0 or self.plan.entity_revision != 1:
            raise ValueError("ActivityPlanned must create entity revision one")
        if self.plan.status != "planned":
            raise ValueError("ActivityPlanned requires planned state")
        return self


class ActivityTransitionPayload(DomainMutationPayload):
    plan_id: str = Field(min_length=1)
    transitioned_at: datetime
    reason_ref: str = Field(min_length=1)


class WorldOccurrenceTerminalPayload(DomainMutationPayload):
    occurrence_id: str = Field(min_length=1)
    effective_at: datetime
    reason_ref: str = Field(min_length=1)


class WorldOccurrenceCommittedPayload(DomainMutationPayload):
    occurrence: WorldOccurrenceProjection

    @model_validator(mode="after")
    def creates_committed_occurrence(self) -> WorldOccurrenceCommittedPayload:
        if self.expected_entity_revision != 0 or self.occurrence.entity_revision != 1:
            raise ValueError("WorldOccurrenceCommitted must create revision one")
        if self.occurrence.status != "committed":
            raise ValueError("WorldOccurrenceCommitted requires committed state")
        if any(
            value is not None
            for value in (
                self.occurrence.activated_at,
                self.occurrence.result_id,
                self.occurrence.result_payload_ref,
                self.occurrence.result_payload_hash,
                self.occurrence.settled_at,
            )
        ):
            raise ValueError("new occurrence cannot contain lifecycle results")
        return self


class WorldOccurrenceActivatedPayload(DomainMutationPayload):
    occurrence_id: str = Field(min_length=1)
    activated_at: datetime
    satisfied_precondition_refs: tuple[str, ...] = ()


class OutcomeObservationRecordedPayload(DomainMutationPayload):
    observation: OutcomeObservationProjection


class OutcomeProposalRecordedPayload(FrozenModel):
    outcome_proposal_id: str = Field(min_length=1)
    decision_proposal_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    occurrence_id: str = Field(min_length=1)
    evaluated_entity_revision: int = Field(ge=1)
    evaluated_world_revision: int = Field(ge=0)
    trigger_ref: str = Field(min_length=1)
    candidate_result_ref: str = Field(min_length=1)
    proposed_result_id: str = Field(min_length=1)
    proposed_result_payload_ref: str = Field(min_length=1)
    proposed_result_payload_hash: str = Field(min_length=1)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    observation_refs: tuple[str, ...] = Field(min_length=1)
    precondition_refs: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=0, le=10_000)
    expires_at: datetime

    @model_validator(mode="after")
    def proposed_change_hash_matches_fields(self) -> OutcomeProposalRecordedPayload:
        expected = outcome_mutation_hash(
            change_id=self.change_id,
            occurrence_id=self.occurrence_id,
            evaluated_entity_revision=self.evaluated_entity_revision,
            evaluated_world_revision=self.evaluated_world_revision,
            candidate_result_ref=self.candidate_result_ref,
            result_id=self.proposed_result_id,
            result_payload_ref=self.proposed_result_payload_ref,
            result_payload_hash=self.proposed_result_payload_hash,
            observation_refs=self.observation_refs,
        )
        if self.proposed_change_hash != expected:
            raise ValueError("outcome proposal change hash does not match proposed mutation")
        return self


class WorldOccurrenceSettledPayload(DomainMutationPayload):
    acceptance_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)
    occurrence_id: str = Field(min_length=1)
    outcome_proposal_id: str = Field(min_length=1)
    candidate_result_ref: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    observation_refs: tuple[str, ...] = Field(min_length=1)
    result_payload_ref: str = Field(min_length=1)
    result_payload_hash: str = Field(min_length=1)
    settled_at: datetime
    appraisal_trigger_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def accepted_change_hash_matches_fields(self) -> WorldOccurrenceSettledPayload:
        expected = outcome_mutation_hash(
            change_id=self.change_id,
            occurrence_id=self.occurrence_id,
            evaluated_entity_revision=self.expected_entity_revision,
            evaluated_world_revision=self.evaluated_world_revision,
            candidate_result_ref=self.candidate_result_ref,
            result_id=self.result_id,
            result_payload_ref=self.result_payload_ref,
            result_payload_hash=self.result_payload_hash,
            observation_refs=self.observation_refs,
        )
        if self.accepted_change_hash != expected:
            raise ValueError("settlement change hash does not match accepted mutation")
        return self


def outcome_mutation_hash(
    *,
    change_id: str,
    occurrence_id: str,
    evaluated_entity_revision: int,
    evaluated_world_revision: int,
    candidate_result_ref: str,
    result_id: str,
    result_payload_ref: str,
    result_payload_hash: str,
    observation_refs: tuple[str, ...] | list[str],
) -> str:
    encoded = json.dumps(
        {
            "candidate_result_ref": candidate_result_ref,
            "change_id": change_id,
            "evaluated_entity_revision": evaluated_entity_revision,
            "evaluated_world_revision": evaluated_world_revision,
            "observation_refs": sorted(observation_refs),
            "occurrence_id": occurrence_id,
            "result_id": result_id,
            "result_payload_hash": result_payload_hash,
            "result_payload_ref": result_payload_ref,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ExperienceCommittedPayload(DomainMutationPayload):
    experience: ExperienceProjection

    @model_validator(mode="after")
    def creates_committed_experience(self) -> ExperienceCommittedPayload:
        if self.expected_entity_revision != 0 or self.experience.entity_revision != 1:
            raise ValueError("ExperienceCommitted must create entity revision one")
        if self.experience.status != "committed":
            raise ValueError("ExperienceCommitted requires committed state")
        return self


LIFE_PAYLOAD_MODELS = {
    "NpcRegistered": NpcRegisteredPayload,
    "ActivityPlanned": ActivityPlannedPayload,
    "ActivityStarted": ActivityTransitionPayload,
    "ActivityPaused": ActivityTransitionPayload,
    "ActivityResumed": ActivityTransitionPayload,
    "ActivityCompleted": ActivityTransitionPayload,
    "ActivityAbandoned": ActivityTransitionPayload,
    "WorldOccurrenceCommitted": WorldOccurrenceCommittedPayload,
    "WorldOccurrenceActivated": WorldOccurrenceActivatedPayload,
    "OutcomeObservationRecorded": OutcomeObservationRecordedPayload,
    "OutcomeProposalRecorded": OutcomeProposalRecordedPayload,
    "WorldOccurrenceSettled": WorldOccurrenceSettledPayload,
    "ExperienceCommitted": ExperienceCommittedPayload,
    "WorldOccurrenceCancelled": WorldOccurrenceTerminalPayload,
    "WorldOccurrenceExpired": WorldOccurrenceTerminalPayload,
}
