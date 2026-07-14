"""Versioned contracts for events accepted by the World v2 reducer bundle.

The catalog is descriptive authority: it records who may produce an event, what
revision axis it advances, and the evidence/lifecycle lineage expected around it.
It deliberately does not decide behavior or reduce state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from types import MappingProxyType
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, create_model

from .errors import UnknownEventType
from .appraisal_events import APPRAISAL_PAYLOAD_MODELS
from .affect_events import AFFECT_PAYLOAD_MODELS
from .actor_authority_events import ACTOR_AUTHORITY_PAYLOAD_MODELS
from .authorization_events import AUTHORIZATION_PAYLOAD_MODELS
from .commitment_events import COMMITMENT_PAYLOAD_MODELS
from .fact_events import FACT_PAYLOAD_MODELS
from .experience_events import (
    EXPERIENCE_PAYLOAD_MODELS,
    LegacyExperienceCommittedPayload,
)
from .life_events import LIFE_PAYLOAD_MODELS
from .relationship_events import RELATIONSHIP_PAYLOAD_MODELS
from .thread_events import THREAD_MECHANICAL_PAYLOAD_MODELS, THREAD_PAYLOAD_MODELS
from .schemas import (
    Action,
    ActionReconciliation,
    BudgetAccount,
    BudgetReservation,
    BudgetSettlement,
    ClaimLease,
    ClockObservation,
    ExecutionReceipt,
    ExternalObservation,
    Observation,
    TriggerProcess,
)


RevisionClassName = Literal["world", "deliberation"]


@dataclass(frozen=True, slots=True)
class EventContract:
    event_type: str
    producer: str
    revision_class: RevisionClassName
    payload_model: type[BaseModel]
    idempotency_identity: str
    schema_version: str = "world-v2.1"
    allowed_predecessors: tuple[str, ...] = ()
    evidence_types: tuple[str, ...] = ()
    successors: tuple[str, ...] = ()
    compensations: tuple[str, ...] = ()
    reducer_bundle: str = "world-v2-reducers.13"
    upcaster: str = "world-v2-upcasters.1"

    @property
    def payload_contract(self) -> str:
        return self.payload_model.__name__

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(
            name for name, field in self.payload_model.model_fields.items() if field.is_required()
        )

    def json_schema(self) -> dict[str, object]:
        """Return payload JSON Schema with lifecycle metadata for CI tooling."""

        schema = self.payload_model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["x-world-event"] = {
            "event_type": self.event_type,
            "producer": self.producer,
            "revision_class": self.revision_class,
            "allowed_predecessors": list(self.allowed_predecessors),
            "evidence_types": list(self.evidence_types),
            "successors": list(self.successors),
            "compensations": list(self.compensations),
            "idempotency_identity": self.idempotency_identity,
            "reducer_bundle": self.reducer_bundle,
            "upcaster": self.upcaster,
        }
        return schema

    def validate_payload(self, payload: Mapping[str, object]) -> None:
        self.payload_model.model_validate_json(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )


_FORBID = ConfigDict(extra="forbid", strict=True)
_ALLOW_AUDIT = ConfigDict(extra="allow", strict=True)
_Required = tuple[Any, Any]


def _payload_model(
    name: str,
    fields: Mapping[str, _Required] | None = None,
    *,
    allow_audit_extensions: bool = False,
) -> type[BaseModel]:
    return create_model(
        name,
        __config__=_ALLOW_AUDIT if allow_audit_extensions else _FORBID,
        **dict(fields or {}),
    )


def _optional_model_projection(
    name: str, base: type[BaseModel], *, required: frozenset[str]
) -> type[BaseModel]:
    fields: dict[str, _Required] = {}
    for field_name, field in base.model_fields.items():
        fields[field_name] = (
            (field.annotation, ...) if field_name in required else (field.annotation | None, None)
        )
    return _payload_model(name, fields)


def _action_settlement_payload(name: str) -> type[BaseModel]:
    fields: dict[str, _Required] = {"action_id": _ID}
    for field_name, field in ExternalObservation.model_fields.items():
        if field_name == "action_id":
            continue
        fields[field_name] = (field.annotation | None, None)
    return _payload_model(name, fields)


_ID = (str, Field(min_length=1))
_PAYLOAD_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        "WorldStarted": _payload_model("WorldStartedPayload"),
        "ObservationRecorded": _optional_model_projection(
            "ObservationRecordedPayload",
            Observation,
            required=frozenset({"observation_id"}),
        ),
        "OperatorObservationRecorded": _payload_model(
            "OperatorObservationRecordedPayload",
            {"observation_id": _ID, "observation_hash": _ID},
        ),
        "ClockAdvanced": _optional_model_projection(
            "ClockAdvancedPayload",
            ClockObservation,
            required=frozenset({"logical_time_from", "logical_time_to"}),
        ),
        "ExternalObservationRecorded": _payload_model(
            "ExternalObservationRecordedPayload", {"result": (ExternalObservation, ...)}
        ),
        "ExternalObservationProcessed": _payload_model(
            "ExternalObservationProcessedPayload", {"result_id": _ID}
        ),
        "TriggerProcessClaimed": _payload_model(
            "TriggerProcessClaimedPayload", {"process": (TriggerProcess, ...)}
        ),
        "TriggerProcessOpened": _payload_model(
            "TriggerProcessOpenedPayload", {"process": (TriggerProcess, ...)}
        ),
        "TriggerProcessReclaimed": _payload_model(
            "TriggerProcessReclaimedPayload", {"process": (TriggerProcess, ...)}
        ),
        "TriggerProcessCompleted": _payload_model(
            "TriggerProcessCompletedPayload",
            {
                "trigger_id": _ID,
                "owner_id": _ID,
                "attempt_id": _ID,
                "completed_at": (datetime, ...),
                "runtime_outcome_ref": _ID,
            },
        ),
        "ProposalRecorded": _payload_model(
            "ProposalRecordedPayload", {"proposal_id": _ID}, allow_audit_extensions=True
        ),
        "AcceptanceRecorded": _payload_model(
            "AcceptanceRecordedPayload",
            {
                "status": _ID,
                "proposal_id": _ID,
                "evaluated_world_revision": (int, Field(ge=0)),
            },
            allow_audit_extensions=True,
        ),
        "LegacyAcceptanceAuditRecorded": _payload_model(
            "LegacyAcceptanceAuditRecordedPayload",
            {"status": _ID},
            allow_audit_extensions=True,
        ),
        "BudgetAccountConfigured": _payload_model(
            "BudgetAccountConfiguredPayload", {"account": (BudgetAccount, ...)}
        ),
        "BudgetReserved": _payload_model(
            "BudgetReservedPayload", {"reservation": (BudgetReservation, ...)}
        ),
        "BudgetSettled": _payload_model(
            "BudgetSettlementPayload", {"settlement": (BudgetSettlement, ...)}
        ),
        "BudgetReleased": _payload_model(
            "BudgetReleasedPayload", {"settlement": (BudgetSettlement, ...)}
        ),
        "BudgetAdjusted": _payload_model(
            "BudgetAdjustedPayload", {"settlement": (BudgetSettlement, ...)}
        ),
        "ActionAuthorized": _payload_model("ActionAuthorizedPayload", {"action": (Action, ...)}),
        "ActionScheduled": _payload_model("ActionScheduledPayload", {"action_id": _ID}),
        "ActionClaimed": _payload_model(
            "ActionClaimedPayload", {"action_id": _ID, "claim_lease": (ClaimLease, ...)}
        ),
        "ActionReclaimed": _payload_model(
            "ActionReclaimedPayload", {"action_id": _ID, "claim_lease": (ClaimLease, ...)}
        ),
        "ActionDispatchStarted": _payload_model(
            "ActionDispatchStartedPayload",
            {"action_id": _ID, "owner_id": _ID, "attempt_id": _ID, "started_at": (datetime, ...)},
        ),
        **{
            event_type: _action_settlement_payload(f"{event_type}Payload")
            for event_type in (
                "ActionProviderAccepted",
                "ActionDelivered",
                "ActionFailed",
                "ActionUnknown",
                "ActionCancelled",
                "ActionExpired",
            )
        },
        "ExecutionReceiptRecorded": _payload_model(
            "ExecutionReceiptRecordedPayload", {"receipt": (ExecutionReceipt, ...)}
        ),
        "ActionReconciliationRequired": _payload_model(
            "ActionReconciliationPayload", {"reconciliation": (ActionReconciliation, ...)}
        ),
        **LIFE_PAYLOAD_MODELS,
        **APPRAISAL_PAYLOAD_MODELS,
        **AFFECT_PAYLOAD_MODELS,
        **RELATIONSHIP_PAYLOAD_MODELS,
        **THREAD_PAYLOAD_MODELS,
        **COMMITMENT_PAYLOAD_MODELS,
        **FACT_PAYLOAD_MODELS,
        **EXPERIENCE_PAYLOAD_MODELS,
        "LegacyExperienceCommitted": LegacyExperienceCommittedPayload,
        **THREAD_MECHANICAL_PAYLOAD_MODELS,
        **ACTOR_AUTHORITY_PAYLOAD_MODELS,
        **AUTHORIZATION_PAYLOAD_MODELS,
    }
)

_IDEMPOTENCY_IDENTITIES: Mapping[str, str] = MappingProxyType(
    {
        "WorldStarted": "world_id+seed_version",
        "ObservationRecorded": "source+source_event_id",
        "OperatorObservationRecorded": "world_id+observation_id",
        "ClockAdvanced": "world_id+tick_id",
        "ExternalObservationRecorded": "source+source_event_id",
        "ExternalObservationProcessed": "source+source_event_id+processed",
        "TriggerProcessClaimed": "world_id+trigger_id+attempt_id+claimed",
        "TriggerProcessOpened": "world_id+trigger_id+opened",
        "TriggerProcessReclaimed": "world_id+trigger_id+attempt_id+reclaimed",
        "TriggerProcessCompleted": "world_id+trigger_id+attempt_id+completed",
        "ProposalRecorded": "world_id+trigger_id+proposal_id",
        "AcceptanceRecorded": "world_id+proposal_id+evaluated_world_revision",
        "LegacyAcceptanceAuditRecorded": "migration-only:original-event-id",
        "AffectEpisodeOpened": "world_id+episode_id+transition_id",
        "AffectEpisodeUpdated": "episode_id+transition_id",
        "AffectEpisodeDecayed": "episode_id+expected_revision+to_logical_time+config",
        "AffectEpisodeResolved": "episode_id+transition_id",
        "AffectEpisodeSuperseded": "episode_id+successor_episode_id+transition_id",
        "AffectBaselineAdjusted": "world_id+dimension+calibration_revision+transition_id",
        "BudgetAccountConfigured": "account_id+window_id",
        "BudgetReserved": "reservation_id",
        "BudgetSettled": "reservation_id+result_id+terminal",
        "BudgetReleased": "reservation_id+result_id+terminal",
        "BudgetAdjusted": "reservation_id+result_id+adjustment_index",
        "ActionAuthorized": "world_id+intent_id+action_kind",
        "ActionScheduled": "action_id+scheduled",
        "ActionClaimed": "action_id+attempt_id+claimed",
        "ActionReclaimed": "action_id+attempt_id+reclaimed",
        "ActionDispatchStarted": "action_id+attempt_id+dispatch_started",
        "ActionProviderAccepted": "provider+source_event_id+provider_accepted",
        "ActionDelivered": "provider+source_event_id+delivered",
        "ActionFailed": "provider+source_event_id+failed",
        "ActionUnknown": "provider+source_event_id+unknown",
        "ActionCancelled": "action_id+cancellation_id",
        "ActionExpired": "action_id+expiry_boundary",
        "ExecutionReceiptRecorded": "provider+source_event_id+raw_payload_hash",
        "ActionReconciliationRequired": "result_id+reason+observed_state",
        "NpcRegistered": "world_id+npc_id",
        "ActivityPlanned": "plan_id+transition_id",
        "ActivityStarted": "plan_id+transition_id",
        "ActivityPaused": "plan_id+transition_id",
        "ActivityResumed": "plan_id+transition_id",
        "ActivityCompleted": "plan_id+transition_id",
        "ActivityAbandoned": "plan_id+transition_id",
        "WorldOccurrenceCommitted": "occurrence_id+transition_id",
        "WorldOccurrenceActivated": "occurrence_id+transition_id",
        "OutcomeObservationRecorded": "world_id+outcome_observation_id",
        "OutcomeProposalRecorded": "world_id+outcome_proposal_id",
        "WorldOccurrenceSettled": "occurrence_id+result_id+expected_entity_revision",
        "ExperienceCommitted": "world_id+experience_id",
        "LegacyExperienceCommitted": "migration-only:original-event-id",
        "WorldOccurrenceCancelled": "occurrence_id+transition_id",
        "WorldOccurrenceExpired": "occurrence_id+transition_id",
        "AppraisalAccepted": "world_id+appraisal_id+transition_id",
        "AppraisalContradicted": "appraisal_id+transition_id",
        "AppraisalExpired": "appraisal_id+transition_id",
        "AppraisalSuperseded": "appraisal_id+transition_id",
        "RelationshipSignalAccepted": "world_id+signal_semantic_fingerprint",
        "RelationshipSlowVariableAdjusted": "relationship_id+expected_entity_revision+adjustment_id",
        "BoundaryChanged": "boundary_id+expected_entity_revision+transition_id",
        **{
            event_type: "world_id+thread_id+expected_entity_revision+transition_id"
            for event_type in THREAD_PAYLOAD_MODELS
        },
        "ThreadExpired": "world_id+thread_id+expected_entity_revision+transition_id",
        **{
            event_type: "world_id+commitment_id+expected_entity_revision+transition_id"
            for event_type in COMMITMENT_PAYLOAD_MODELS
        },
        **{
            event_type: "world_id+fact_id+expected_entity_revision+transition_id"
            for event_type in FACT_PAYLOAD_MODELS
        },
        "ActorAuthorityBootstrapped": "world_id+authority_id+transition_id",
        "ActorAuthorityRotated": "world_id+authority_id+expected_entity_revision+transition_id",
        "ActorAuthorityRevoked": "world_id+authority_id+expected_entity_revision+transition_id",
        "ActorAuthorityCompensated": "world_id+authority_id+expected_entity_revision+transition_id",
        **{
            event_type: "world_id+entity_id+expected_entity_revision+transition_id"
            for event_type in AUTHORIZATION_PAYLOAD_MODELS
        },
    }
)

_RELATIONSHIP_EVIDENCE_TYPES = (
    "observed_message",
    "committed_world_event",
    "committed_experience",
    "settled_world_event",
    "settled_external_result",
    "active_plan",
    "operator_observation",
)


def _contract(
    event_type: str,
    producer: str,
    revision_class: RevisionClassName,
    payload_contract: str,
    *,
    allowed_predecessors: tuple[str, ...] = (),
    evidence_types: tuple[str, ...] = (),
    successors: tuple[str, ...] = (),
    compensations: tuple[str, ...] = (),
) -> EventContract:
    return EventContract(
        event_type=event_type,
        producer=producer,
        revision_class=revision_class,
        payload_model=_PAYLOAD_MODELS[event_type],
        idempotency_identity=_IDEMPOTENCY_IDENTITIES[event_type],
        allowed_predecessors=allowed_predecessors,
        evidence_types=evidence_types,
        successors=successors,
        compensations=compensations,
    )


_CONTRACTS: Mapping[str, EventContract] = MappingProxyType(
    {
        contract.event_type: contract
        for contract in (
            _contract("WorldStarted", "world_bootstrap", "world", "WorldStartedPayload"),
            _contract(
                "ActorAuthorityBootstrapped",
                "deployment_root",
                "world",
                "ActorAuthorityMutationPayload",
                evidence_types=("deployment_root_signature",),
                successors=("ActorAuthorityRotated", "ActorAuthorityRevoked"),
            ),
            _contract(
                "ActorAuthorityRotated",
                "deployment_root",
                "world",
                "ActorAuthorityMutationPayload",
                allowed_predecessors=("ActorAuthorityBootstrapped", "ActorAuthorityRotated"),
                evidence_types=("deployment_root_signature",),
                successors=("ActorAuthorityRotated", "ActorAuthorityRevoked", "ActorAuthorityCompensated"),
                compensations=("ActorAuthorityCompensated",),
            ),
            _contract(
                "ActorAuthorityRevoked",
                "deployment_root",
                "world",
                "ActorAuthorityMutationPayload",
                allowed_predecessors=("ActorAuthorityBootstrapped", "ActorAuthorityRotated"),
                evidence_types=("deployment_root_signature",),
            ),
            _contract(
                "ActorAuthorityCompensated",
                "deployment_root",
                "world",
                "ActorAuthorityMutationPayload",
                allowed_predecessors=("ActorAuthorityRotated",),
                evidence_types=("deployment_root_signature",),
            ),
            *(
                _contract(
                    event_type,
                    "deployment_root_shadow_attestor",
                    "world",
                    payload_model.__name__,
                    evidence_types=(
                        "deployment_root_signature",
                        "external_principal_action_evidence",
                    ),
                    compensations=(
                        (event_type.removesuffix("Revised") + "Compensated",)
                        if event_type.endswith("Revised")
                        else ()
                    ),
                )
                for event_type, payload_model in AUTHORIZATION_PAYLOAD_MODELS.items()
            ),
            _contract(
                "ThreadExpired",
                "logical_clock",
                "world",
                "ThreadExpiredPayload",
                allowed_predecessors=("ClockAdvanced", "ThreadExpired"),
                evidence_types=("clock_observation",),
            ),
            _contract(
                "ObservationRecorded",
                "world_runtime",
                "world",
                "ObservationRecordedPayload",
                evidence_types=("observed_message",),
                successors=("TriggerProcessClaimed",),
            ),
            _contract(
                "LegacyAcceptanceAuditRecorded",
                "bundle_migration",
                "world",
                "LegacyAcceptanceAuditRecordedPayload",
            ),
            _contract(
                "OperatorObservationRecorded",
                "operator_ingress",
                "deliberation",
                "OperatorObservationRecordedPayload",
                evidence_types=("operator_observation",),
            ),
            _contract(
                "ClockAdvanced",
                "world_runtime",
                "world",
                "ClockAdvancedPayload",
                evidence_types=("clock_observation",),
                successors=("TriggerProcessClaimed",),
            ),
            _contract(
                "ExternalObservationRecorded",
                "settlement_inbox",
                "deliberation",
                "ExternalObservationRecordedPayload",
                evidence_types=("external_observation",),
                successors=("TriggerProcessClaimed", "ExternalObservationProcessed"),
            ),
            _contract(
                "ExternalObservationProcessed",
                "settlement_planner",
                "deliberation",
                "ExternalObservationProcessedPayload",
                allowed_predecessors=("ExternalObservationRecorded",),
                evidence_types=("external_observation",),
                successors=("TriggerProcessCompleted",),
            ),
            _contract(
                "TriggerProcessOpened",
                "world_runtime",
                "deliberation",
                "TriggerProcessOpenedPayload",
                evidence_types=("settled_world_event",),
                successors=("TriggerProcessClaimed",),
            ),
            _contract(
                "TriggerProcessClaimed",
                "world_runtime",
                "deliberation",
                "TriggerProcessClaimedPayload",
                evidence_types=("observation", "clock_observation", "external_observation"),
                successors=("ProposalRecorded", "TriggerProcessCompleted"),
            ),
            _contract(
                "TriggerProcessReclaimed",
                "world_runtime",
                "deliberation",
                "TriggerProcessReclaimedPayload",
                allowed_predecessors=("TriggerProcessClaimed", "TriggerProcessReclaimed"),
                evidence_types=("expired_claim_lease",),
                successors=("ProposalRecorded", "TriggerProcessCompleted"),
            ),
            _contract(
                "TriggerProcessCompleted",
                "world_runtime",
                "deliberation",
                "TriggerProcessCompletedPayload",
                allowed_predecessors=(
                    "TriggerProcessClaimed",
                    "TriggerProcessReclaimed",
                    "ExternalObservationProcessed",
                ),
                evidence_types=("runtime_outcome",),
            ),
            _contract(
                "ProposalRecorded",
                "deliberation",
                "deliberation",
                "ProposalRecordedPayload",
                allowed_predecessors=("TriggerProcessClaimed", "TriggerProcessReclaimed"),
                evidence_types=("model_result", "context_capsule"),
                successors=("AcceptanceRecorded",),
            ),
            _contract(
                "AcceptanceRecorded",
                "proposal_acceptance",
                "world",
                "AcceptanceRecordedPayload",
                allowed_predecessors=("ProposalRecorded",),
                evidence_types=("decision_proposal", "evaluated_world_revision"),
                successors=(
                    "BudgetReserved",
                    "ActionAuthorized",
                    "WorldOccurrenceSettled",
                ),
            ),
            _contract(
                "BudgetAccountConfigured",
                "operator",
                "world",
                "BudgetAccountConfiguredPayload",
                evidence_types=("budget_policy",),
                successors=("BudgetReserved",),
            ),
            _contract(
                "BudgetReserved",
                "proposal_acceptance",
                "world",
                "BudgetReservedPayload",
                allowed_predecessors=("AcceptanceRecorded", "BudgetAccountConfigured"),
                evidence_types=("accepted_action_intent", "budget_account"),
                successors=("ActionAuthorized", "BudgetSettled", "BudgetReleased"),
            ),
            _contract(
                "BudgetSettled",
                "settlement_planner",
                "world",
                "BudgetSettlementPayload",
                allowed_predecessors=("BudgetReserved", "ExecutionReceiptRecorded"),
                evidence_types=("execution_receipt",),
                successors=("BudgetAdjusted",),
                compensations=("BudgetAdjusted",),
            ),
            _contract(
                "BudgetReleased",
                "settlement_planner",
                "world",
                "BudgetSettlementPayload",
                allowed_predecessors=("BudgetReserved", "ExecutionReceiptRecorded"),
                evidence_types=("execution_receipt",),
                successors=("BudgetAdjusted",),
                compensations=("BudgetAdjusted",),
            ),
            _contract(
                "BudgetAdjusted",
                "reconciliation_planner",
                "world",
                "BudgetSettlementPayload",
                allowed_predecessors=("BudgetSettled", "BudgetReleased"),
                evidence_types=("reconciliation_result",),
            ),
            _contract(
                "ActionAuthorized",
                "proposal_acceptance",
                "world",
                "ActionAuthorizedPayload",
                allowed_predecessors=("AcceptanceRecorded", "BudgetReserved"),
                evidence_types=("accepted_action_intent", "budget_reservation"),
                successors=("ActionScheduled", "ActionCancelled", "ActionExpired"),
            ),
            _contract(
                "ActionScheduled",
                "action_scheduler",
                "world",
                "ActionIdentityPayload",
                allowed_predecessors=("ActionAuthorized",),
                evidence_types=("authorized_action",),
                successors=("ActionClaimed", "ActionCancelled", "ActionExpired"),
            ),
            _contract(
                "ActionClaimed",
                "action_pump",
                "world",
                "ActionClaimedPayload",
                allowed_predecessors=("ActionScheduled",),
                evidence_types=("active_claim_lease",),
                successors=("ActionDispatchStarted", "ActionCancelled", "ActionExpired"),
            ),
            _contract(
                "ActionReclaimed",
                "action_pump",
                "world",
                "ActionClaimedPayload",
                allowed_predecessors=("ActionClaimed", "ActionReclaimed"),
                evidence_types=("expired_claim_lease",),
                successors=("ActionDispatchStarted", "ActionCancelled", "ActionExpired"),
            ),
            _contract(
                "ActionDispatchStarted",
                "action_pump",
                "world",
                "ActionDispatchStartedPayload",
                allowed_predecessors=("ActionClaimed", "ActionReclaimed"),
                evidence_types=("active_claim_lease",),
                successors=(
                    "ActionProviderAccepted",
                    "ActionDelivered",
                    "ActionFailed",
                    "ActionUnknown",
                ),
            ),
            _contract(
                "ActionProviderAccepted",
                "settlement_planner",
                "world",
                "ActionIdentityPayload",
                allowed_predecessors=("ActionDispatchStarted",),
                evidence_types=("provider_receipt", "execution_receipt"),
                successors=("ActionDelivered", "ActionFailed", "ActionUnknown"),
            ),
            _contract(
                "ActionDelivered",
                "settlement_planner",
                "world",
                "ActionIdentityPayload",
                allowed_predecessors=("ActionDispatchStarted", "ActionProviderAccepted"),
                evidence_types=("provider_receipt", "execution_receipt"),
                successors=("BudgetSettled", "TriggerProcessCompleted"),
                compensations=("ActionReconciliationRequired",),
            ),
            _contract(
                "ActionFailed",
                "settlement_planner",
                "world",
                "ActionIdentityPayload",
                allowed_predecessors=("ActionDispatchStarted", "ActionProviderAccepted"),
                evidence_types=("provider_receipt", "execution_receipt"),
                successors=("BudgetReleased", "TriggerProcessCompleted"),
                compensations=("ActionReconciliationRequired",),
            ),
            _contract(
                "ActionUnknown",
                "settlement_planner",
                "world",
                "ActionIdentityPayload",
                allowed_predecessors=("ActionDispatchStarted", "ActionProviderAccepted"),
                evidence_types=("provider_receipt", "execution_receipt", "timeout"),
                successors=("ActionReconciliationRequired", "TriggerProcessCompleted"),
                compensations=("ActionReconciliationRequired",),
            ),
            _contract(
                "ActionCancelled",
                "action_scheduler",
                "world",
                "ActionIdentityPayload",
                allowed_predecessors=(
                    "ActionAuthorized",
                    "ActionScheduled",
                    "ActionClaimed",
                    "ActionReclaimed",
                ),
                evidence_types=("cancellation_reason",),
                successors=("BudgetReleased", "TriggerProcessCompleted"),
            ),
            _contract(
                "ActionExpired",
                "action_scheduler",
                "world",
                "ActionIdentityPayload",
                allowed_predecessors=(
                    "ActionAuthorized",
                    "ActionScheduled",
                    "ActionClaimed",
                    "ActionReclaimed",
                ),
                evidence_types=("logical_time",),
                successors=("BudgetReleased", "TriggerProcessCompleted"),
            ),
            _contract(
                "ExecutionReceiptRecorded",
                "settlement_planner",
                "world",
                "ExecutionReceiptRecordedPayload",
                allowed_predecessors=("ExternalObservationRecorded",),
                evidence_types=("provider_receipt", "external_observation"),
                successors=(
                    "ActionProviderAccepted",
                    "ActionDelivered",
                    "ActionFailed",
                    "ActionUnknown",
                    "BudgetSettled",
                    "BudgetReleased",
                ),
            ),
            _contract(
                "ActionReconciliationRequired",
                "settlement_planner",
                "world",
                "ActionReconciliationPayload",
                allowed_predecessors=(
                    "ActionDelivered",
                    "ActionFailed",
                    "ActionUnknown",
                    "ExecutionReceiptRecorded",
                ),
                evidence_types=("conflicting_receipt", "unknown_outcome"),
                successors=("BudgetAdjusted",),
            ),
            _contract(
                "NpcRegistered",
                "proposal_acceptance",
                "world",
                "NpcRegisteredPayload",
                evidence_types=("committed_world_event", "operator_observation"),
                successors=("WorldOccurrenceCommitted",),
            ),
            _contract(
                "ActivityPlanned",
                "proposal_acceptance",
                "world",
                "ActivityPlannedPayload",
                evidence_types=("observed_message", "active_plan"),
                successors=("ActivityStarted", "ActivityAbandoned", "WorldOccurrenceCommitted"),
            ),
            *(
                _contract(
                    event_type,
                    "proposal_acceptance",
                    "world",
                    "ActivityTransitionPayload",
                    allowed_predecessors=predecessors,
                    evidence_types=("active_plan", "committed_world_event"),
                    successors=successors,
                )
                for event_type, predecessors, successors in (
                    (
                        "ActivityStarted",
                        ("ActivityPlanned", "ActivityResumed"),
                        ("ActivityPaused", "ActivityCompleted", "ActivityAbandoned"),
                    ),
                    (
                        "ActivityPaused",
                        ("ActivityStarted", "ActivityResumed"),
                        ("ActivityResumed", "ActivityAbandoned"),
                    ),
                    (
                        "ActivityResumed",
                        ("ActivityPaused",),
                        ("ActivityPaused", "ActivityCompleted", "ActivityAbandoned"),
                    ),
                    ("ActivityCompleted", ("ActivityStarted", "ActivityResumed"), ()),
                    (
                        "ActivityAbandoned",
                        ("ActivityPlanned", "ActivityStarted", "ActivityPaused", "ActivityResumed"),
                        ("ActivityPlanned",),
                    ),
                )
            ),
            _contract(
                "WorldOccurrenceCommitted",
                "proposal_acceptance",
                "world",
                "WorldOccurrenceCommittedPayload",
                evidence_types=("active_plan", "committed_world_event"),
                successors=(
                    "WorldOccurrenceActivated",
                    "WorldOccurrenceCancelled",
                    "WorldOccurrenceExpired",
                ),
            ),
            _contract(
                "WorldOccurrenceCancelled",
                "proposal_acceptance",
                "world",
                "WorldOccurrenceTerminalPayload",
                allowed_predecessors=("WorldOccurrenceCommitted",),
                evidence_types=("committed_world_event", "operator_observation"),
            ),
            _contract(
                "WorldOccurrenceExpired",
                "world_runtime",
                "world",
                "WorldOccurrenceTerminalPayload",
                allowed_predecessors=("WorldOccurrenceCommitted",),
                evidence_types=("committed_world_event", "operator_observation"),
            ),
            _contract(
                "WorldOccurrenceActivated",
                "proposal_acceptance",
                "world",
                "WorldOccurrenceActivatedPayload",
                allowed_predecessors=("WorldOccurrenceCommitted",),
                evidence_types=("active_plan", "committed_world_event"),
                successors=("OutcomeObservationRecorded",),
            ),
            _contract(
                "OutcomeObservationRecorded",
                "world_runtime",
                "world",
                "OutcomeObservationRecordedPayload",
                allowed_predecessors=("WorldOccurrenceActivated",),
                evidence_types=(
                    "settled_external_result",
                    "operator_observation",
                    "committed_world_event",
                ),
                successors=("OutcomeProposalRecorded",),
            ),
            _contract(
                "OutcomeProposalRecorded",
                "deliberation",
                "deliberation",
                "OutcomeProposalRecordedPayload",
                allowed_predecessors=("OutcomeObservationRecorded",),
                evidence_types=("committed_world_event",),
                successors=("AcceptanceRecorded",),
            ),
            _contract(
                "WorldOccurrenceSettled",
                "proposal_acceptance",
                "world",
                "WorldOccurrenceSettledPayload",
                allowed_predecessors=(
                    "WorldOccurrenceActivated",
                    "OutcomeObservationRecorded",
                    "OutcomeProposalRecorded",
                ),
                evidence_types=("settled_world_event", "operator_observation"),
                successors=("ExperienceCommitted", "TriggerProcessOpened"),
            ),
            _contract(
                "ExperienceCommitted",
                "proposal_acceptance",
                "world",
                "ExperienceCommittedPayload",
                allowed_predecessors=("AcceptanceRecorded",),
                evidence_types=("settled_world_event", "settled_external_result"),
            ),
            _contract(
                "LegacyExperienceCommitted",
                "bundle_migration",
                "world",
                "LegacyExperienceCommittedPayload",
            ),
            _contract(
                "AppraisalAccepted",
                "proposal_acceptance",
                "world",
                "AppraisalAcceptedPayload",
                allowed_predecessors=("AcceptanceRecorded", "TriggerProcessClaimed"),
                evidence_types=("settled_world_event", "observed_message"),
                successors=(
                    "AppraisalContradicted",
                    "AppraisalExpired",
                    "AppraisalSuperseded",
                    "AffectEpisodeOpened",
                ),
            ),
            _contract(
                "AppraisalContradicted",
                "proposal_acceptance",
                "world",
                "AppraisalContradictedPayload",
                allowed_predecessors=("AppraisalAccepted",),
                evidence_types=("observed_message", "committed_world_event"),
            ),
            _contract(
                "AppraisalExpired",
                "world_runtime",
                "world",
                "AppraisalExpiredPayload",
                allowed_predecessors=("AppraisalAccepted",),
                evidence_types=("clock_observation",),
            ),
            _contract(
                "AppraisalSuperseded",
                "proposal_acceptance",
                "world",
                "AppraisalSupersededPayload",
                allowed_predecessors=("AppraisalAccepted", "AppraisalContradicted"),
                evidence_types=("observed_message", "committed_world_event"),
            ),
            _contract(
                "AffectEpisodeOpened",
                "proposal_acceptance",
                "world",
                "AffectEpisodeOpenedPayload",
                allowed_predecessors=("AcceptanceRecorded", "AppraisalAccepted"),
                evidence_types=("observed_message", "committed_world_event"),
                successors=(
                    "AffectEpisodeUpdated",
                    "AffectEpisodeDecayed",
                    "AffectEpisodeResolved",
                    "AffectEpisodeSuperseded",
                ),
            ),
            _contract(
                "AffectEpisodeUpdated",
                "proposal_acceptance",
                "world",
                "AffectEpisodeUpdatedPayload",
                allowed_predecessors=("AcceptanceRecorded",),
                evidence_types=("observed_message", "committed_world_event"),
            ),
            _contract(
                "AffectEpisodeDecayed",
                "world_runtime",
                "world",
                "AffectEpisodeDecayedPayload",
                allowed_predecessors=(
                    "ClockAdvanced",
                    "AffectEpisodeDecayed",
                ),
                evidence_types=("clock_observation",),
            ),
            _contract(
                "AffectEpisodeResolved",
                "proposal_acceptance",
                "world",
                "AffectEpisodeResolvedPayload",
                allowed_predecessors=("AcceptanceRecorded",),
                evidence_types=("observed_message", "committed_world_event"),
            ),
            _contract(
                "AffectEpisodeSuperseded",
                "proposal_acceptance",
                "world",
                "AffectEpisodeSupersededPayload",
                allowed_predecessors=("AcceptanceRecorded",),
                evidence_types=("observed_message", "committed_world_event"),
            ),
            _contract(
                "AffectBaselineAdjusted",
                "proposal_acceptance",
                "world",
                "AffectBaselineAdjustedPayload",
                allowed_predecessors=("AcceptanceRecorded",),
                evidence_types=(
                    "observed_message",
                    "committed_world_event",
                    "committed_experience",
                    "settled_world_event",
                    "settled_external_result",
                    "active_plan",
                    "operator_observation",
                    "clock_observation",
                ),
            ),
            _contract(
                "RelationshipSignalAccepted",
                "proposal_acceptance",
                "world",
                "RelationshipSignalAcceptedPayload",
                allowed_predecessors=("AcceptanceRecorded",),
                evidence_types=_RELATIONSHIP_EVIDENCE_TYPES,
            ),
            _contract(
                "RelationshipSlowVariableAdjusted",
                "proposal_acceptance",
                "world",
                "RelationshipSlowVariableAdjustedPayload",
                allowed_predecessors=("AcceptanceRecorded",),
                evidence_types=_RELATIONSHIP_EVIDENCE_TYPES,
                compensations=("RelationshipSlowVariableAdjusted",),
            ),
            _contract(
                "BoundaryChanged",
                "proposal_acceptance",
                "world",
                "BoundaryChangedPayload",
                allowed_predecessors=("AcceptanceRecorded",),
                evidence_types=_RELATIONSHIP_EVIDENCE_TYPES,
                compensations=("BoundaryChanged",),
            ),
            *(
                _contract(
                    event_type,
                    "proposal_acceptance",
                    "world",
                    payload_model.__name__,
                    allowed_predecessors=("AcceptanceRecorded",),
                    evidence_types=_RELATIONSHIP_EVIDENCE_TYPES,
                    compensations=(
                        ("ThreadCompensated",) if event_type == "ThreadUpdated" else ()
                    ),
                )
                for event_type, payload_model in THREAD_PAYLOAD_MODELS.items()
            ),
            *(
                _contract(
                    event_type,
                    (
                        "logical_clock"
                        if event_type in {
                            "PrivateCommitmentDue",
                            "PrivateCommitmentDeadlineBroken",
                        }
                        else "proposal_acceptance"
                    ),
                    "world",
                    payload_model.__name__,
                    allowed_predecessors=(
                        ("ClockAdvanced", "PrivateCommitmentDue")
                        if event_type == "PrivateCommitmentDue"
                        else (
                            "ClockAdvanced",
                            "PrivateCommitmentDue",
                            "PrivateCommitmentDeadlineBroken",
                        )
                        if event_type == "PrivateCommitmentDeadlineBroken"
                        else ("AcceptanceRecorded",)
                    ),
                    evidence_types=_RELATIONSHIP_EVIDENCE_TYPES,
                )
                for event_type, payload_model in COMMITMENT_PAYLOAD_MODELS.items()
            ),
            *(
                _contract(
                    event_type,
                    "proposal_acceptance",
                    "world",
                    payload_model.__name__,
                    allowed_predecessors=("AcceptanceRecorded",),
                    evidence_types=(
                        "observed_message",
                        "operator_observation",
                        "committed_fact",
                    ),
                    compensations=(
                        ("FactCorrectionCompensated",)
                        if event_type == "FactCorrected"
                        else ()
                    ),
                )
                for event_type, payload_model in FACT_PAYLOAD_MODELS.items()
            ),
        )
    }
)


def event_contract(event_type: str) -> EventContract:
    """Return immutable metadata for one accepted event type."""

    try:
        return _CONTRACTS[event_type]
    except KeyError as exc:
        raise UnknownEventType(f"event type {event_type!r} is not catalogued") from exc


def event_contracts() -> Mapping[str, EventContract]:
    """Return the immutable event catalog keyed by event type."""

    return _CONTRACTS
