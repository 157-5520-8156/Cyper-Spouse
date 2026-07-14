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
    reducer_bundle: str = "world-v2-reducers.1"
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
            "AcceptanceRecordedPayload", {"status": _ID}, allow_audit_extensions=True
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
        "ActionAuthorized": _payload_model(
            "ActionAuthorizedPayload", {"action": (Action, ...)}
        ),
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
                "ActionProviderAccepted", "ActionDelivered", "ActionFailed",
                "ActionUnknown", "ActionCancelled", "ActionExpired",
            )
        },
        "ExecutionReceiptRecorded": _payload_model(
            "ExecutionReceiptRecordedPayload", {"receipt": (ExecutionReceipt, ...)}
        ),
        "ActionReconciliationRequired": _payload_model(
            "ActionReconciliationPayload", {"reconciliation": (ActionReconciliation, ...)}
        ),
    }
)

_IDEMPOTENCY_IDENTITIES: Mapping[str, str] = MappingProxyType(
    {
        "WorldStarted": "world_id+seed_version",
        "ObservationRecorded": "source+source_event_id",
        "ClockAdvanced": "world_id+tick_id",
        "ExternalObservationRecorded": "source+source_event_id",
        "ExternalObservationProcessed": "source+source_event_id+processed",
        "TriggerProcessClaimed": "world_id+trigger_id+attempt_id+claimed",
        "TriggerProcessReclaimed": "world_id+trigger_id+attempt_id+reclaimed",
        "TriggerProcessCompleted": "world_id+trigger_id+attempt_id+completed",
        "ProposalRecorded": "world_id+trigger_id+proposal_id",
        "AcceptanceRecorded": "world_id+proposal_id+evaluated_world_revision",
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
    }
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
                "ObservationRecorded",
                "world_runtime",
                "world",
                "ObservationRecordedPayload",
                evidence_types=("observed_message",),
                successors=("TriggerProcessClaimed",),
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
                successors=("BudgetReserved", "ActionAuthorized"),
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
