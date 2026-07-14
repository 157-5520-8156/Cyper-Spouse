"""Shared `.16` authority value objects and GoalAuthority projections.

The public seams are immutable typed values plus ``reduce_v2_goal``.  Cause
resolution, lifecycle policy, compensation lineage, and randomness stay behind
the reducer interface.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .schema_core import EvidenceRef, FrozenModel, PrivacyClass


V16AuthorityLane = Literal[
    "deliberative",
    "operator",
    "settlement",
    "clock_runtime",
    "compensation",
]

V2_GOAL_EVIDENCE_PARSER_BY_KIND = {
    "settled_occurrence_outcome": "goal-completion:occurrence.1",
    "active_fact_predicate": "goal-completion:active-fact.1",
}
V2_GOAL_CONTRACT_SCHEMA_BY_KIND = {
    "settled_occurrence_outcome": "goal-contract-schema:occurrence.1",
    "active_fact_predicate": "goal-contract-schema:active-fact.1",
}
V2_GOAL_EVIDENCE_SCHEMA_BY_KIND = {
    "settled_occurrence_outcome": "world-occurrence-settlement.1",
    "active_fact_predicate": "fact-authority.1",
}


class V16AuthorizedMutationEnvelope(FrozenModel):
    """Common immutable proposal authority fields; domain bodies stay concrete."""

    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    # Compatibility/audit projection only.  Deliberative authorization comes
    # from the typed basis carried by ``cause_authority``; callers cannot add
    # free-form evidence here.
    evidence_refs: tuple[EvidenceRef, ...] = ()
    policy_refs: tuple[str, ...] = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)


class CommittedEvidenceSource(FrozenModel):
    source_kind: Literal[
        "settled_world_event",
        "fact",
        "experience",
        "world_started",
        "clock_advanced",
        "character_core",
    ]
    event_ref: str = Field(min_length=1)
    world_revision: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)
    source_entity_ref: str | None = Field(default=None, min_length=1)
    source_entity_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def entity_binding_matches_source_kind(self) -> CommittedEvidenceSource:
        needs_entity = self.source_kind in {
            "settled_world_event",
            "fact",
            "experience",
            "character_core",
        }
        supplied = self.source_entity_ref is not None or self.source_entity_revision is not None
        if needs_entity and (
            self.source_entity_ref is None or self.source_entity_revision is None
        ):
            raise ValueError("typed committed basis requires an exact entity binding")
        if not needs_entity and supplied:
            raise ValueError("event-only committed basis cannot claim an entity binding")
        return self


class CommittedEvidenceBasis(FrozenModel):
    basis_kind: Literal["committed_evidence"] = "committed_evidence"
    sources: tuple[CommittedEvidenceSource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def sources_are_canonical(self) -> CommittedEvidenceBasis:
        identities = tuple(
            (
                item.source_kind,
                item.event_ref,
                item.source_entity_ref or "",
                item.source_entity_revision or 0,
            )
            for item in self.sources
        )
        if identities != tuple(sorted(set(identities))):
            raise ValueError("committed deliberative sources must be sorted and unique")
        return self


class V2GoalRationale(FrozenModel):
    text: str = Field(min_length=1, max_length=512)
    privacy_class: PrivacyClass

    @field_validator("text")
    @classmethod
    def text_is_canonical(cls, value: str) -> str:
        if (
            value != value.strip()
            or value != unicodedata.normalize("NFC", value)
            or any(unicodedata.category(character) == "Cc" for character in value)
        ):
            raise ValueError("goal rationale must be trimmed NFC text")
        return value


class InternalIntentionBasis(FrozenModel):
    basis_kind: Literal["internal_intention"] = "internal_intention"
    actor_ref: str = Field(min_length=1)
    trigger_ref: str = Field(min_length=1)
    decision_slot: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    logical_time: datetime
    intention_kind: Literal[
        "goal_choice",
        "goal_governance",
        "attention_choice",
        "resource_self_regulation",
    ]
    intention_class: Literal[
        "self_direction",
        "priority_reassessment",
        "constraint_response",
        "value_alignment",
        "uncertainty_management",
    ]
    rationale: V2GoalRationale
    intention_material_hash: str = Field(min_length=64, max_length=64)
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    privacy_class: Literal["private"] = "private"

    @field_validator("logical_time")
    @classmethod
    def logical_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("internal intention logical time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def material_hash_is_derived(self) -> InternalIntentionBasis:
        material = self.model_dump(mode="json")
        material.pop("intention_material_hash", None)
        expected = hashlib.sha256(
            json.dumps(
                _canonicalize(material), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if self.intention_material_hash != expected:
            raise ValueError("internal intention material hash is invalid")
        return self


DeliberativeBasisBinding = Annotated[
    CommittedEvidenceBasis | InternalIntentionBasis,
    Field(discriminator="basis_kind"),
]


class DeliberativeCauseAuthority(FrozenModel):
    kind: Literal["accepted_deliberation"] = "accepted_deliberation"
    basis: DeliberativeBasisBinding


class DomainOperatorAuthorityBinding(FrozenModel):
    kind: Literal["deployment_actor_authority"] = "deployment_actor_authority"
    authority_id: str = Field(min_length=1)
    authority_revision: int = Field(ge=1)
    principal_ref: str = Field(min_length=1)
    authority_event_ref: str = Field(min_length=1)
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(min_length=64, max_length=64)
    authority_values_hash: str = Field(min_length=64, max_length=64)
    authority_policy_digest: str = Field(min_length=64, max_length=64)
    authorization_contract: Literal["deployment-actor-authority:v16-domain.1"]
    required_operation: Literal[
        "v2_goal_governance",
        "v2_location_governance",
        "v2_resource_governance",
        "v2_attention_governance",
    ]
    audit_observation_ref: str | None = None


class SettledEventCauseAuthority(FrozenModel):
    kind: Literal["settled_event"] = "settled_event"
    event_ref: str = Field(min_length=1)
    event_type: Literal[
        "ActivityCompleted",
        "WorldOccurrenceSettled",
        "ActionDelivered",
        "ActionFailed",
        "ActionCancelled",
        "ActionExpired",
        "ExperienceCommitted",
        "ExecutionReceiptRecorded",
        "FactCommitted",
        "FactCorrected",
    ]
    world_revision: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)


class V2GoalBlockerAuthorityBinding(FrozenModel):
    blocker_ref: str = Field(min_length=1)
    authority_event_ref: str = Field(min_length=1)
    authority_event_type: Literal[
        "FactCommitted",
        "FactCorrected",
        "ActivityCompleted",
        "WorldOccurrenceSettled",
        "ActionDelivered",
    ]
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(min_length=64, max_length=64)


class ClockCauseAuthority(FrozenModel):
    kind: Literal["clock"] = "clock"
    clock_event_ref: str = Field(min_length=1)
    clock_world_revision: int = Field(ge=1)
    clock_payload_hash: str = Field(min_length=64, max_length=64)
    logical_time_from: datetime
    logical_time_to: datetime
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)

    @field_validator("logical_time_from", "logical_time_to")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock authority time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def interval_is_forward(self) -> ClockCauseAuthority:
        if self.logical_time_to <= self.logical_time_from:
            raise ValueError("clock authority interval must advance")
        return self


class GoalExpiryCorrectionBasis(FrozenModel):
    basis_kind: Literal["goal_expiry_correction"] = "goal_expiry_correction"
    target_expiry_transition_id: str = Field(min_length=1)
    target_expiry_event_ref: str = Field(min_length=1)
    target_expiry_world_revision: int = Field(ge=1)
    target_expiry_payload_hash: str = Field(min_length=64, max_length=64)
    original_clock: ClockCauseAuthority
    operator_authority: DomainOperatorAuthorityBinding
    correction_class: Literal[
        "due_window",
        "clock_transition",
        "policy_application",
        "operator_import_error",
    ]
    sources: CommittedEvidenceBasis
    rationale: V2GoalRationale
    privacy_class: PrivacyClass
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)


GoalCorrectionBasisBinding = Annotated[
    CommittedEvidenceBasis | InternalIntentionBasis | GoalExpiryCorrectionBasis,
    Field(discriminator="basis_kind"),
]


class CompensationCauseAuthority(FrozenModel):
    kind: Literal["compensation"] = "compensation"
    target_transition_id: str = Field(min_length=1)
    target_entity_revision: int = Field(ge=1)
    target_accepted_event_ref: str = Field(min_length=1)
    target_accepted_world_revision: int = Field(ge=1)
    target_accepted_payload_hash: str = Field(min_length=64, max_length=64)
    target_authority_lane: V16AuthorityLane
    correction_basis: GoalCorrectionBasisBinding
    correction_rationale: V2GoalRationale
    operator_authority: DomainOperatorAuthorityBinding | None = None


V16CauseAuthority = Annotated[
    DeliberativeCauseAuthority
    | DomainOperatorAuthorityBinding
    | SettledEventCauseAuthority
    | ClockCauseAuthority
    | CompensationCauseAuthority,
    Field(discriminator="kind"),
]


class RandomDrawBinding(FrozenModel):
    draw_event_ref: str = Field(min_length=1)
    draw_world_revision: int = Field(ge=1)
    draw_payload_hash: str = Field(min_length=64, max_length=64)
    attempt_id: str = Field(min_length=1)
    candidate_set_hash: str = Field(min_length=64, max_length=64)
    selected_candidate_ref: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    sampler_version: str = Field(min_length=1)
    supersedes_draw_ref: str | None = None


class RandomDrawProjection(FrozenModel):
    draw_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    candidate_refs: tuple[str, ...] = Field(min_length=1)
    candidate_set_hash: str = Field(min_length=64, max_length=64)
    selected_candidate_ref: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    sampler_version: str = Field(min_length=1)
    supersedes_draw_ref: str | None = None
    origin_event_ref: str = Field(min_length=1)
    origin_world_revision: int = Field(ge=1)
    origin_payload_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def selection_is_canonical(self) -> RandomDrawProjection:
        if self.candidate_refs != tuple(sorted(set(self.candidate_refs))):
            raise ValueError("random draw candidates must be sorted and unique")
        expected = hashlib.sha256(
            json.dumps(self.candidate_refs, separators=(",", ":")).encode()
        ).hexdigest()
        if self.candidate_set_hash != expected:
            raise ValueError("random draw candidate set hash is invalid")
        if self.selected_candidate_ref not in self.candidate_refs:
            raise ValueError("random draw selected candidate is outside candidate set")
        return self


class V2GoalDueWindow(FrozenModel):
    starts_at: datetime
    ends_at: datetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("goal due time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def window_is_forward(self) -> V2GoalDueWindow:
        if self.ends_at <= self.starts_at:
            raise ValueError("goal due window must move forward")
        return self


class V2GoalCompletionContract(FrozenModel):
    contract_id: str = Field(min_length=1)
    contract_version: Literal["v2-goal-completion-contract.1"] = (
        "v2-goal-completion-contract.1"
    )
    completion_kind: Literal[
        "settled_occurrence_outcome", "active_fact_predicate"
    ]
    outcome_ref: str = Field(min_length=1)
    expected_actor_ref: str = Field(min_length=1)
    allowed_settled_event_types: tuple[
        Literal[
            "WorldOccurrenceSettled",
            "FactCommitted",
            "FactCorrected",
        ],
        ...,
    ] = Field(min_length=1)
    contract_schema_ref: str = Field(min_length=1)
    completion_parser_ref: str = Field(min_length=1)
    evidence_schema_ref: str = Field(min_length=1)
    required_fact_predicate: str | None = Field(default=None, min_length=1)
    required_fact_value_hash: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    evidence_cutoff_world_revision: int = Field(ge=0)
    policy_version: Literal["v2-goal-completion-contract.1"] = (
        "v2-goal-completion-contract.1"
    )
    policy_digest: str = Field(min_length=64, max_length=64)
    contract_digest: str = Field(min_length=64, max_length=64)
    privacy_class: PrivacyClass

    @model_validator(mode="after")
    def contract_is_canonical(self) -> V2GoalCompletionContract:
        if self.allowed_settled_event_types != tuple(
            sorted(set(self.allowed_settled_event_types))
        ):
            raise ValueError("completion event types must be sorted and unique")
        fact_fields = (
            self.required_fact_predicate,
            self.required_fact_value_hash,
        )
        if self.completion_kind == "active_fact_predicate":
            if any(value is None for value in fact_fields):
                raise ValueError("fact completion contract requires exact fact predicate/value")
            if not set(self.allowed_settled_event_types).issubset(
                {"FactCommitted", "FactCorrected"}
            ):
                raise ValueError("fact completion contract has non-fact event type")
        elif any(value is not None for value in fact_fields):
            raise ValueError("non-fact completion contract cannot claim fact predicates")
        if self.completion_kind == "settled_occurrence_outcome" and (
            self.allowed_settled_event_types != ("WorldOccurrenceSettled",)
        ):
            raise ValueError("occurrence completion contract requires settlement event")
        if self.completion_parser_ref != V2_GOAL_EVIDENCE_PARSER_BY_KIND[
            self.completion_kind
        ]:
            raise ValueError("completion evidence parser is not installed")
        if self.contract_schema_ref != V2_GOAL_CONTRACT_SCHEMA_BY_KIND[
            self.completion_kind
        ]:
            raise ValueError("completion contract schema is not installed")
        if self.evidence_schema_ref != V2_GOAL_EVIDENCE_SCHEMA_BY_KIND[
            self.completion_kind
        ]:
            raise ValueError("completion evidence schema is not installed")
        if self.contract_digest != v2_goal_completion_contract_digest(self):
            raise ValueError("completion contract digest is invalid")
        return self


def v2_goal_completion_contract_digest(
    contract: V2GoalCompletionContract | dict[str, object],
) -> str:
    material = (
        contract.model_dump(mode="json")
        if isinstance(contract, V2GoalCompletionContract)
        else dict(contract)
    )
    material.pop("contract_digest", None)
    return hashlib.sha256(
        json.dumps(
            _canonicalize(material), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class V2GoalOccurrenceCompletionEvidence(FrozenModel):
    evidence_kind: Literal["occurrence_settlement"] = "occurrence_settlement"
    evidence_ref: str = Field(min_length=1)
    evidence_world_revision: int = Field(ge=1)
    evidence_payload_hash: str = Field(min_length=64, max_length=64)
    evidence_schema_ref: Literal["world-occurrence-settlement.1"]
    occurrence_id: str = Field(min_length=1)
    occurrence_entity_revision: int = Field(ge=1)
    resolved_actor_ref: str = Field(min_length=1)
    resolved_outcome_ref: str = Field(min_length=1)
    privacy_class: PrivacyClass


class V2GoalFactCompletionEvidence(FrozenModel):
    evidence_kind: Literal["fact_state"] = "fact_state"
    evidence_ref: str = Field(min_length=1)
    evidence_world_revision: int = Field(ge=1)
    evidence_payload_hash: str = Field(min_length=64, max_length=64)
    evidence_schema_ref: Literal["fact-authority.1"]
    fact_id: str = Field(min_length=1)
    fact_entity_revision: int = Field(ge=1)
    resolved_actor_ref: str = Field(min_length=1)
    resolved_outcome_ref: str = Field(min_length=1)
    resolved_fact_predicate: str = Field(min_length=1)
    resolved_fact_value_hash: str = Field(min_length=64, max_length=64)
    privacy_class: PrivacyClass


V2GoalCompletionEvidence = Annotated[
    V2GoalOccurrenceCompletionEvidence
    | V2GoalFactCompletionEvidence,
    Field(discriminator="evidence_kind"),
]


class V2GoalSupersedesAuthority(FrozenModel):
    goal_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_world_revision: int = Field(ge=1)
    accepted_payload_hash: str = Field(min_length=64, max_length=64)
    target_head_semantic_hash: str = Field(min_length=64, max_length=64)
    privacy_class: PrivacyClass


class V2GoalProgressAssessment(FrozenModel):
    contribution_class: Literal[
        "direct_contribution",
        "indirect_support",
        "milestone_reached",
        "reappraisal",
    ]
    basis: CommittedEvidenceBasis
    rationale: V2GoalRationale


class V2GoalLifecycleReason(FrozenModel):
    reason_kind: Literal[
        "priority_shift",
        "resource_constraint",
        "uncertainty",
        "relationship_consideration",
        "priority_restored",
        "constraint_resolved",
        "renewed_intent",
        "no_longer_desired",
        "superseded",
        "infeasible",
        "values_changed",
        "context_changed",
        "completion_verified",
        "due_window_elapsed",
    ]
    rationale: V2GoalRationale
    basis: DeliberativeBasisBinding
    privacy_class: PrivacyClass


class V2GoalBlocker(FrozenModel):
    blocker_id: str = Field(min_length=1)
    blocker_class: Literal[
        "external_dependency",
        "resource_constraint",
        "uncertainty",
        "priority_conflict",
        "relationship_constraint",
        "environmental_constraint",
    ]
    basis: DeliberativeBasisBinding
    rationale: V2GoalRationale
    privacy_class: PrivacyClass
    blocker_semantic_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def semantic_hash_is_derived(self) -> V2GoalBlocker:
        material = self.model_dump(mode="json")
        material.pop("blocker_semantic_hash", None)
        expected = hashlib.sha256(
            json.dumps(
                _canonicalize(material), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if self.blocker_semantic_hash != expected:
            raise ValueError("goal blocker semantic hash is invalid")
        return self


class V2GoalBlockerResolution(FrozenModel):
    blocker_id: str = Field(min_length=1)
    blocker_semantic_hash: str = Field(min_length=64, max_length=64)
    resolution_class: Literal[
        "externally_resolved",
        "no_longer_relevant",
        "accepted_tradeoff",
        "superseded_assessment",
    ]
    rationale: V2GoalRationale
    basis: DeliberativeBasisBinding

    @model_validator(mode="after")
    def resolution_basis_has_capability(self) -> V2GoalBlockerResolution:
        if self.resolution_class == "externally_resolved" and not isinstance(
            self.basis, CommittedEvidenceBasis
        ):
            raise ValueError("external blocker resolution requires committed evidence")
        return self


class V2GoalAbandonedTerminalReason(FrozenModel):
    terminal_kind: Literal["abandoned"] = "abandoned"
    reason: V2GoalLifecycleReason


class V2GoalCompletedTerminalReason(FrozenModel):
    terminal_kind: Literal["completed"] = "completed"
    contract_id: str = Field(min_length=1)
    contract_digest: str = Field(min_length=64, max_length=64)
    completion_evidence_ref: str = Field(min_length=1)
    privacy_class: PrivacyClass


class V2GoalExpiredTerminalReason(FrozenModel):
    terminal_kind: Literal["expired"] = "expired"
    due_window: V2GoalDueWindow
    clock_projection_ref: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    privacy_class: PrivacyClass


V2GoalTerminalReason = Annotated[
    V2GoalAbandonedTerminalReason
    | V2GoalCompletedTerminalReason
    | V2GoalExpiredTerminalReason,
    Field(discriminator="terminal_kind"),
]


class V2GoalValues(FrozenModel):
    outcome_ref: str = Field(min_length=1)
    importance_bp: int = Field(ge=0, le=10_000)
    progress_bp: int = Field(ge=0, le=10_000)
    due_window: V2GoalDueWindow | None = None
    blockers: tuple[V2GoalBlocker, ...] = ()
    privacy_class: PrivacyClass
    completion_contract: V2GoalCompletionContract | None = None
    status: Literal[
        "active", "paused", "blocked", "completed", "abandoned", "expired"
    ]
    terminal_reason: V2GoalTerminalReason | None = None
    supersedes_goal_id: str | None = None
    supersedes_goal_authority: V2GoalSupersedesAuthority | None = None

    @model_validator(mode="after")
    def values_are_canonical(self) -> V2GoalValues:
        blocker_ids = tuple(item.blocker_id for item in self.blockers)
        if blocker_ids != tuple(sorted(set(blocker_ids))):
            raise ValueError("goal blockers must be sorted and unique")
        terminal = self.status in {"completed", "abandoned", "expired"}
        if terminal != (self.terminal_reason is not None):
            raise ValueError("goal terminal reason must match terminal status")
        if terminal and self.terminal_reason is not None and (
            self.terminal_reason.terminal_kind != self.status
        ):
            raise ValueError("goal terminal reason kind does not match status")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked goal requires blockers")
        if self.status != "blocked" and self.blockers:
            raise ValueError("only blocked goals may retain blockers")
        if self.completion_contract is not None and (
            self.completion_contract.outcome_ref != self.outcome_ref
        ):
            raise ValueError("completion contract outcome must match goal outcome")
        if (self.supersedes_goal_id is None) != (
            self.supersedes_goal_authority is None
        ):
            raise ValueError("goal supersession requires an exact authority binding")
        if self.supersedes_goal_authority is not None and (
            self.supersedes_goal_authority.goal_id != self.supersedes_goal_id
        ):
            raise ValueError("goal supersession authority identifies another goal")
        return self


class V2GoalOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


def v2_goal_semantic_fingerprint(
    *,
    goal_id: str,
    actor_ref: str,
    values: V2GoalValues,
    policy_refs: tuple[str, ...],
) -> str:
    material = {
        "goal_id": goal_id,
        "actor_ref": actor_ref,
        "values": values.model_dump(mode="json"),
        "policy_refs": policy_refs,
    }
    return hashlib.sha256(
        json.dumps(
            _canonicalize(material), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _canonicalize(value: object) -> object:
    if isinstance(value, dict):
        return {key: _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and (value.endswith("Z") or "+" in value[-6:]):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value


class V2GoalProjection(FrozenModel):
    goal_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: V2GoalValues
    origin: V2GoalOrigin
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None

    @field_validator("opened_at", "updated_at", "closed_at")
    @classmethod
    def times_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("goal lifecycle time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def projection_is_canonical(self) -> V2GoalProjection:
        expected = v2_goal_semantic_fingerprint(
            goal_id=self.goal_id,
            actor_ref=self.actor_ref,
            values=self.values,
            policy_refs=self.origin.policy_refs,
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("goal semantic fingerprint is invalid")
        if self.opened_at > self.updated_at:
            raise ValueError("goal opened time cannot follow updated time")
        terminal = self.values.status in {"completed", "abandoned", "expired"}
        if terminal != (self.closed_at is not None):
            raise ValueError("goal closed time must match terminal status")
        if self.closed_at is not None and self.closed_at != self.updated_at:
            raise ValueError("goal terminal close time must equal update time")
        return self


class V2GoalTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal[
        "open",
        "revise",
        "progress",
        "pause",
        "resume",
        "block",
        "unblock",
        "complete",
        "abandon",
        "expire",
        "compensate",
    ]
    authority_lane: V16AuthorityLane
    selection_mode: Literal["direct", "random_draw"]
    values_before: V2GoalValues | None = None
    values_after: V2GoalValues
    semantic_fingerprint_after: str = Field(min_length=64, max_length=64)
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime
    cause_authority: V16CauseAuthority
    revise_kind: Literal["reprioritize", "reschedule", "recontract"] | None = None
    progress_assessment: V2GoalProgressAssessment | None = None
    lifecycle_reason: V2GoalLifecycleReason | None = None
    completion_evidence: V2GoalCompletionEvidence | None = None
    blocker_resolutions: tuple[V2GoalBlockerResolution, ...] = ()
    terminal_reason: V2GoalTerminalReason | None = None
    removed_blocker_fingerprints: tuple[str, ...] = ()
    random_draw_binding: RandomDrawBinding | None = None
    compensates_transition_id: str | None = None

    @field_validator("accepted_at")
    @classmethod
    def accepted_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("goal accepted time must be timezone-aware")
        return value


class V2GoalProposedMutation(FrozenModel):
    event_type: Literal[
        "V2GoalOpened",
        "V2GoalRevised",
        "V2GoalProgressed",
        "V2GoalPaused",
        "V2GoalResumed",
        "V2GoalBlocked",
        "V2GoalUnblocked",
        "V2GoalCompleted",
        "V2GoalAbandoned",
        "V2GoalTransitionCompensated",
    ]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical_json(self) -> V2GoalProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("goal proposed mutation must be a JSON object")
        canonical = json.dumps(
            decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if self.payload_json != canonical:
            raise ValueError("goal proposed mutation JSON must be canonical")
        return self


class V2GoalProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["v2_goal_transition"] = "v2_goal_transition"
    proposal_encoding: Literal["typed-authority-v1"] = "typed-authority-v1"
    authority_contract_ref: Literal["proposal-contract:v2-goal.1"] = (
        "proposal-contract:v2-goal.1"
    )
    transition_kind: Literal[
        "open",
        "revise",
        "progress",
        "pause",
        "resume",
        "block",
        "unblock",
        "complete",
        "abandon",
        "compensate",
    ]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: V2GoalProposedMutation

    @model_validator(mode="after")
    def transition_matches_event_and_payload(self) -> V2GoalProposalProjection:
        expected = {
            "open": "V2GoalOpened",
            "revise": "V2GoalRevised",
            "progress": "V2GoalProgressed",
            "pause": "V2GoalPaused",
            "resume": "V2GoalResumed",
            "block": "V2GoalBlocked",
            "unblock": "V2GoalUnblocked",
            "complete": "V2GoalCompleted",
            "abandon": "V2GoalAbandoned",
            "compensate": "V2GoalTransitionCompensated",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected:
            raise ValueError("goal proposal transition does not match event")
        decoded = json.loads(self.proposed_mutation.payload_json)
        expected_fields = {
            "proposal_id": self.proposal_id,
            "change_id": self.change_id,
            "transition_id": self.transition_id,
            "evaluated_world_revision": self.evaluated_world_revision,
            "expected_entity_revision": self.expected_entity_revision,
            "accepted_change_hash": self.proposed_change_hash,
        }
        if any(decoded.get(key) != value for key, value in expected_fields.items()):
            raise ValueError("goal proposal envelope does not match proposed mutation")
        return self


def validate_v2_goal_authority_state(
    goals: tuple[V2GoalProjection, ...],
    history: tuple[V2GoalTransitionProjection, ...],
    proposals: tuple[V2GoalProposalProjection, ...],
    proposal_ids: tuple[str, ...],
    *,
    global_proposal_ids: tuple[str, ...] = (),
) -> None:
    """Validate the persisted Goal head/history/proposal indexes as one authority."""

    goal_ids = tuple(item.goal_id for item in goals)
    if len(goal_ids) != len(set(goal_ids)):
        raise ValueError("goal ids must be unique")
    transition_ids = tuple(item.transition_id for item in history)
    change_ids = tuple(item.change_id for item in history)
    if len(transition_ids) != len(set(transition_ids)):
        raise ValueError("goal transition ids must be globally unique")
    if len(change_ids) != len(set(change_ids)):
        raise ValueError("goal change ids must be globally unique")

    by_goal: dict[str, list[V2GoalTransitionProjection]] = {
        goal_id: [] for goal_id in goal_ids
    }
    for transition in history:
        if transition.goal_id not in by_goal:
            raise ValueError("goal transition has no projected head")
        by_goal[transition.goal_id].append(transition)
    for goal in goals:
        lineage = by_goal[goal.goal_id]
        if not lineage or lineage[0].operation != "open":
            raise ValueError("goal head requires an opening transition")
        if lineage[0].entity_revision != 1 or lineage[0].values_before is not None:
            raise ValueError("goal opening transition must create revision one")
        for before, after in zip(lineage, lineage[1:], strict=False):
            if (
                after.entity_revision != before.entity_revision + 1
                or after.values_before != before.values_after
                or after.accepted_at <= before.accepted_at
            ):
                raise ValueError("goal transition lineage is discontinuous")
        latest = lineage[-1]
        if (
            goal.entity_revision != latest.entity_revision
            or goal.values != latest.values_after
            or goal.semantic_fingerprint != latest.semantic_fingerprint_after
            or goal.origin.transition_id != latest.transition_id
            or goal.origin.change_id != latest.change_id
            or goal.origin.policy_refs != latest.policy_refs
            or goal.origin.accepted_event_ref != latest.accepted_event_ref
            or goal.updated_at != latest.accepted_at
        ):
            raise ValueError("goal head does not equal its latest transition")

    indexed = tuple(item.proposal_id for item in proposals)
    if proposal_ids != indexed:
        raise ValueError("goal proposal ids must exactly index goal proposals")
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ValueError("goal proposal ids must be globally unique")
    proposal_transition_ids = tuple(item.transition_id for item in proposals)
    proposal_change_ids = tuple(item.change_id for item in proposals)
    if len(proposal_transition_ids) != len(set(proposal_transition_ids)) or set(
        proposal_transition_ids
    ).intersection(transition_ids):
        raise ValueError("goal proposal transition ids must be globally unique")
    if len(proposal_change_ids) != len(set(proposal_change_ids)) or set(
        proposal_change_ids
    ).intersection(change_ids):
        raise ValueError("goal proposal change ids must be globally unique")
    if global_proposal_ids and any(
        proposal_id not in global_proposal_ids for proposal_id in proposal_ids
    ):
        raise ValueError("goal proposal index is absent from the global proposal index")
