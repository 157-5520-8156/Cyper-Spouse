"""Strict payload contracts for the World v2 lived-world vertical slice."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .activity_lifecycle_contract import ActivityLifecycleProposalRecordedPayload
from .schemas import (
    BiographicalCoordinateReplacement,
    EvidenceRef,
    FrozenModel,
    NpcProjection,
    OutcomeObservationProjection,
    PlanStateProjection,
    ProjectionCursor,
    RecordedWorldDrawBinding,
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


class NpcStatusChangedPayload(DomainMutationPayload):
    npc_before: NpcProjection
    npc_after: NpcProjection

    @model_validator(mode="after")
    def changes_only_lifecycle_status(self) -> "NpcStatusChangedPayload":
        before = self.npc_before
        after = self.npc_after
        if (
            self.expected_entity_revision != before.entity_revision
            or after.entity_revision != before.entity_revision + 1
            or after.npc_id != before.npc_id
            or after.status == before.status
        ):
            raise ValueError("NPC status transition revision is inconsistent")
        immutable = (
            "stable_identity_ref",
            "known_trait_refs",
            "privacy_class",
            "current_location_ref",
            "source_event_ref",
            "effect_descriptor_hash",
            "accepted_event_ref",
            "registration_event_ref",
            "promotion_edge",
            "subjective_state",
        )
        if any(getattr(after, field) != getattr(before, field) for field in immutable):
            raise ValueError("NPC status transition changed reviewed identity")
        return self


class NpcStateChangedPayload(DomainMutationPayload):
    """Advance one NPC-owned state without rewriting stable identity."""

    npc_before: NpcProjection
    npc_after: NpcProjection

    @model_validator(mode="after")
    def changes_only_mutable_social_state(self) -> "NpcStateChangedPayload":
        before = self.npc_before
        after = self.npc_after
        if (
            self.expected_entity_revision != before.entity_revision
            or after.entity_revision != before.entity_revision + 1
            or after.npc_id != before.npc_id
            or after.subjective_state is None
            or after.subjective_state == before.subjective_state
        ):
            raise ValueError("NPC subjective transition revision is inconsistent")
        immutable = (
            "stable_identity_ref",
            "known_trait_refs",
            "privacy_class",
            "current_location_ref",
            "status",
            "source_event_ref",
            "effect_descriptor_hash",
            "accepted_event_ref",
            "registration_event_ref",
            "promotion_edge",
        )
        if any(getattr(after, field) != getattr(before, field) for field in immutable):
            raise ValueError("NPC subjective transition changed stable identity")
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
    acceptance_id: str | None = Field(default=None, min_length=1)
    activity_lifecycle_proposal_id: str | None = Field(default=None, min_length=1)
    accepted_change_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def scheduler_acceptance_binding_is_complete(self) -> "ActivityTransitionPayload":
        values = (
            self.acceptance_id,
            self.activity_lifecycle_proposal_id,
            self.accepted_change_hash,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("activity lifecycle acceptance binding is incomplete")
        return self


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
    # Legacy records predate the outcome worker.  New production records must
    # bind both fields and are validated by the reducer before acceptance.
    deliberation_trigger_id: str | None = Field(default=None, min_length=1)
    source_observation_id: str | None = Field(default=None, min_length=1)
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
    # Legacy and ordinary proposals predate character-owned consequential
    # outcome selection. A long-lived biographical choice writes this complete
    # group so the selected candidate is auditable against exactly one pinned
    # Context Capsule and model result.
    decision_authority: (
        Literal[
            "character_model",
            "recorded_world_draw",
            "external_observation",
        ]
        | None
    ) = None
    recorded_world_draw: RecordedWorldDrawBinding | None = None
    decision_model: str | None = Field(default=None, min_length=1, max_length=256)
    decision_raw_output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_model_result_ref: str | None = Field(default=None, min_length=1)
    decision_model_result_event_ref: str | None = Field(default=None, min_length=1)
    decision_audit_proposal_event_ref: str | None = Field(default=None, min_length=1)
    decision_audit_proposal_event_payload_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    decision_candidate_matrix_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    adopt_proposed_life_direction: bool | None = None
    character_life_direction: BiographicalCoordinateReplacement | None = None
    context_identity_version: (
        Literal[
            "life-aftermath-context.1",
            "life-aftermath-context.2",
            "life-aftermath-context.3",
        ]
        | None
    ) = None
    context_capsule_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_model_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_snapshot_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_cursor: ProjectionCursor | None = None

    @model_validator(mode="after")
    def proposed_change_hash_matches_fields(self) -> OutcomeProposalRecordedPayload:
        if (self.deliberation_trigger_id is None) != (self.source_observation_id is None):
            raise ValueError("outcome proposal trigger binding is incomplete")
        context_identity = (
            self.decision_model,
            self.decision_raw_output_hash,
            self.context_identity_version,
            self.context_capsule_id,
            self.context_model_content_hash,
            self.context_snapshot_hash,
            self.context_cursor,
        )
        durable_audit_identity = (
            self.decision_model_result_ref,
            self.decision_model_result_event_ref,
            self.decision_audit_proposal_event_ref,
            self.decision_audit_proposal_event_payload_hash,
            self.decision_candidate_matrix_hash,
        )
        if any(item is not None for item in durable_audit_identity) and any(
            item is None for item in durable_audit_identity
        ):
            raise ValueError("outcome proposal durable model audit is incomplete")
        if any(item is not None for item in context_identity) and any(
            item is None for item in context_identity
        ):
            raise ValueError("outcome proposal Context identity must be complete")
        if self.decision_authority == "character_model":
            if any(item is None for item in context_identity):
                raise ValueError("character outcome proposal requires complete Context identity")
            if self.recorded_world_draw is not None:
                raise ValueError("character outcome cannot carry a world draw")
            if self.context_identity_version == "life-aftermath-context.2" and (
                any(item is None for item in durable_audit_identity)
                or self.adopt_proposed_life_direction is None
                or self.character_life_direction is not None
            ):
                raise ValueError(
                    "Context v2 character outcome requires a durable model audit "
                    "and an explicit direction-adoption decision"
                )
            if self.context_identity_version == "life-aftermath-context.3" and (
                any(item is None for item in durable_audit_identity)
                or self.adopt_proposed_life_direction is not None
            ):
                raise ValueError(
                    "Context v3 character outcome requires a durable model audit "
                    "and forbids World-authored direction adoption"
                )
            if self.context_identity_version == "life-aftermath-context.1" and (
                any(item is not None for item in durable_audit_identity)
                or self.character_life_direction is not None
            ):
                raise ValueError("legacy Context v1 cannot claim a durable model audit")
        elif self.decision_authority == "recorded_world_draw":
            if self.recorded_world_draw is None:
                raise ValueError("world contingency requires a recorded world draw")
            if (
                any(item is not None for item in context_identity)
                or any(item is not None for item in durable_audit_identity)
                or self.adopt_proposed_life_direction is not None
                or self.character_life_direction is not None
            ):
                raise ValueError("world draw cannot carry character-model identity")
        elif self.decision_authority == "external_observation":
            if (
                self.recorded_world_draw is not None
                or any(item is not None for item in context_identity)
                or any(item is not None for item in durable_audit_identity)
                or self.adopt_proposed_life_direction is not None
                or self.character_life_direction is not None
            ):
                raise ValueError("external outcome cannot carry model or random authority")
        elif (
            self.recorded_world_draw is not None
            or any(item is not None for item in context_identity)
            or any(item is not None for item in durable_audit_identity)
            or self.adopt_proposed_life_direction is not None
            or self.character_life_direction is not None
        ):
            raise ValueError("legacy outcome proposal cannot carry resolution authority")
        if (
            self.context_cursor is not None
            and self.context_cursor.world_revision != self.evaluated_world_revision
        ):
            raise ValueError(
                "outcome proposal must be recorded against its exact pinned World prefix"
            )
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
            adopt_proposed_life_direction=self.adopt_proposed_life_direction,
            character_life_direction=self.character_life_direction,
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
    adopt_proposed_life_direction: bool | None = None
    character_life_direction: BiographicalCoordinateReplacement | None = None

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
            adopt_proposed_life_direction=self.adopt_proposed_life_direction,
            character_life_direction=self.character_life_direction,
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
    adopt_proposed_life_direction: bool | None = None,
    character_life_direction: BiographicalCoordinateReplacement | None = None,
) -> str:
    material: dict[str, object] = {
        "candidate_result_ref": candidate_result_ref,
        "change_id": change_id,
        "evaluated_entity_revision": evaluated_entity_revision,
        "evaluated_world_revision": evaluated_world_revision,
        "observation_refs": sorted(observation_refs),
        "occurrence_id": occurrence_id,
        "result_id": result_id,
        "result_payload_hash": result_payload_hash,
        "result_payload_ref": result_payload_ref,
    }
    # Historical V2 hashes predate this independent character choice.  Omit
    # the field only for those legacy records so cold replay stays byte exact.
    if adopt_proposed_life_direction is not None:
        material["adopt_proposed_life_direction"] = adopt_proposed_life_direction
    if character_life_direction is not None:
        material["character_life_direction"] = character_life_direction.model_dump(mode="json")
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


LIFE_PAYLOAD_MODELS = {
    "NpcRegistered": NpcRegisteredPayload,
    "NpcStatusChanged": NpcStatusChangedPayload,
    "NpcStateChanged": NpcStateChangedPayload,
    "ActivityPlanned": ActivityPlannedPayload,
    "ActivityStarted": ActivityTransitionPayload,
    "ActivityPaused": ActivityTransitionPayload,
    "ActivityResumed": ActivityTransitionPayload,
    "ActivityCompleted": ActivityTransitionPayload,
    "ActivityAbandoned": ActivityTransitionPayload,
    "ActivityLifecycleProposalRecorded": ActivityLifecycleProposalRecordedPayload,
    "WorldOccurrenceCommitted": WorldOccurrenceCommittedPayload,
    "WorldOccurrenceActivated": WorldOccurrenceActivatedPayload,
    "OutcomeObservationRecorded": OutcomeObservationRecordedPayload,
    "OutcomeProposalRecorded": OutcomeProposalRecordedPayload,
    "WorldOccurrenceSettled": WorldOccurrenceSettledPayload,
    "WorldOccurrenceCancelled": WorldOccurrenceTerminalPayload,
    "WorldOccurrenceExpired": WorldOccurrenceTerminalPayload,
}
