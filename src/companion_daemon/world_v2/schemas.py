from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SchemaVersion = Literal["world-v2.1"]
RuntimeStatus = Literal[
    "observed_only",
    "action_authorized",
    "action_scheduled",
    "action_executed",
    "deferred",
    "failed_safe",
]
ActionState = Literal[
    "authorized",
    "scheduled",
    "claimed",
    "dispatch_started",
    "provider_accepted",
    "delivered",
    "failed",
    "unknown",
    "cancelled",
    "expired",
]


def _contains_naive_datetime(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is None or value.utcoffset() is None
    if isinstance(value, dict):
        return any(_contains_naive_datetime(item) for item in value.values())
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_naive_datetime(item) for item in value)
    return False


class AcceptanceErrorCode(StrEnum):
    UNSUPPORTED_CLAIM = "unsupported_claim"
    STALE_REVISION = "stale_revision"
    SCHEMA_INVALID = "schema_invalid"
    CAPABILITY_DENIED = "capability_denied"
    PRIVACY_DENIED = "privacy_denied"
    CONSENT_MISSING = "consent_missing"
    BUDGET_UNAVAILABLE = "budget_unavailable"
    ACTION_DUPLICATE = "action_duplicate"
    DEPENDENCY_UNSATISFIED = "dependency_unsatisfied"
    EXPIRED_INTENT = "expired_intent"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="after")
    def datetimes_are_timezone_aware(self) -> FrozenModel:
        for name in type(self).model_fields:
            if _contains_naive_datetime(getattr(self, name)):
                raise ValueError(f"{name} must contain only timezone-aware datetimes")
        return self


class Observation(FrozenModel):
    schema_version: SchemaVersion
    observation_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    logical_time: datetime
    created_at: datetime
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    payload_ref: str = Field(min_length=1)
    payload_hash: str = Field(min_length=1)
    received_at: datetime
    reply_context: dict[str, Any] | None = None
    attachment_refs: tuple[str, ...] = ()
    coalescing_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def idempotency_identity(self) -> tuple[str, str]:
        return (self.source, self.source_event_id)


class ClockObservation(FrozenModel):
    schema_version: SchemaVersion
    tick_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    logical_time: datetime
    created_at: datetime
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    logical_time_from: datetime
    logical_time_to: datetime
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def time_moves_forward(self) -> ClockObservation:
        if self.logical_time_to <= self.logical_time_from:
            raise ValueError("logical time must move forwards")
        return self


class ExternalObservation(FrozenModel):
    schema_version: SchemaVersion
    result_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    logical_time: datetime
    created_at: datetime
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    kind: Literal[
        "provider_ack",
        "execution_receipt",
        "tool_result",
        "media_result",
        "reconciliation_result",
    ]
    source: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: Literal[
        "provider_accepted",
        "delivered",
        "failed",
        "cancelled",
        "expired",
        "unknown",
    ]
    provider_ref: str = Field(min_length=1)
    artifact_refs: tuple[str, ...] = ()
    cost_actual: int = Field(ge=0)
    observed_at: datetime
    error_class: str | None = None
    retryability: Literal["retryable", "not_retryable", "unknown"] | None = None
    raw_payload_hash: str = Field(min_length=1)

    @property
    def idempotency_identity(self) -> tuple[str, str]:
        return (self.source, self.source_event_id)


class ReplayMode(FrozenModel):
    schema_version: SchemaVersion
    request_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    from_revision: int = Field(ge=0)
    to_revision: int | None = Field(default=None, ge=0)
    revision_axis: Literal["ledger_sequence"] = "ledger_sequence"
    expected_hash: str | None = None
    trace_id: str = Field(min_length=1)
    model_result_policy: Literal["recorded_only"] = "recorded_only"
    random_policy: Literal["recorded_only"] = "recorded_only"
    side_effect_policy: Literal["forbidden"] = "forbidden"

    @model_validator(mode="after")
    def revision_range_moves_forward(self) -> ReplayMode:
        if self.to_revision is not None and self.to_revision < self.from_revision:
            raise ValueError("to_revision must be greater than or equal to from_revision")
        return self


class ProjectionRequest(FrozenModel):
    schema_version: SchemaVersion
    request_id: str = Field(min_length=1)
    viewer_kind: str = Field(min_length=1)
    viewer_id: str = Field(min_length=1)
    permissions: frozenset[str] = frozenset()
    at_world_revision: int | None = Field(default=None, ge=0)
    trace_id: str = Field(min_length=1)
    include_debug_refs: bool = False
    redaction_policy: str = Field(min_length=1)


class RuntimeOutcome(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    outcome_id: str
    trigger_id: str
    observation_ref: str | None = None
    committed_world_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    status: RuntimeStatus
    authorized_action_ids: tuple[str, ...] = ()
    scheduled_action_ids: tuple[str, ...] = ()
    deferred_refs: tuple[str, ...] = ()
    terminal_errors: tuple[str, ...] = ()
    projection_hint: str | None = None


class ActionIntent(FrozenModel):
    schema_version: SchemaVersion
    intent_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    layer: Literal[
        "internal_state_transition",
        "world_event",
        "external_action",
        "media_action",
        "read_only_tool",
    ]
    target: str = Field(min_length=1)
    payload_ref: str = Field(min_length=1)
    payload_hash: str = Field(min_length=1)
    causal_change_id: str | None = None
    beat_ref: str | None = None
    dependencies: tuple[str, ...] = ()
    due_window: tuple[datetime, datetime] | None = None


class ClaimLease(FrozenModel):
    owner_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    acquired_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def expires_after_acquisition(self) -> ClaimLease:
        if self.expires_at <= self.acquired_at:
            raise ValueError("claim lease must expire after acquisition")
        return self


class ActionDispatchClaim(FrozenModel):
    owner_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    started_at: datetime


class Action(FrozenModel):
    schema_version: SchemaVersion
    action_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    logical_time: datetime
    created_at: datetime
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    layer: Literal["external_action", "media_action", "read_only_tool"]
    intent_ref: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    target: str = Field(min_length=1)
    payload_ref: str = Field(min_length=1)
    payload_hash: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    not_before: datetime | None = None
    expires_at: datetime | None = None
    dependencies: tuple[str, ...] = ()
    budget_reservation_id: str = Field(min_length=1)
    claim_lease: ClaimLease | None = None
    state: ActionState
    recovery_policy: str = Field(min_length=1)

    @model_validator(mode="after")
    def claimed_action_has_a_lease(self) -> Action:
        lease_required_states: frozenset[ActionState] = frozenset(
            {
                "claimed",
                "dispatch_started",
                "provider_accepted",
                "delivered",
                "failed",
                "unknown",
            }
        )
        if self.state in lease_required_states and self.claim_lease is None:
            raise ValueError(f"action state {self.state!r} requires claim_lease")
        return self


class TriggerProcess(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    trigger_id: str = Field(min_length=1)
    trigger_ref: str = Field(min_length=1)
    process_kind: Literal["observation", "clock", "settlement", "recovery"]
    state: Literal["claimed", "terminal"]
    claim_lease: ClaimLease
    attempt_ids: tuple[str, ...] = Field(min_length=1)
    runtime_outcome_ref: str | None = None

    @model_validator(mode="after")
    def active_attempt_matches_lease(self) -> TriggerProcess:
        if len(set(self.attempt_ids)) != len(self.attempt_ids):
            raise ValueError("trigger attempt_ids must be unique")
        if self.attempt_ids[-1] != self.claim_lease.attempt_id:
            raise ValueError("active claim lease must reference the latest attempt")
        return self


class BudgetReservation(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    reservation_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    category: Literal[
        "chat", "repair", "audit", "proactive", "vision", "audio", "image", "tool"
    ]
    amount_limit: int = Field(ge=0)
    state: Literal["reserved", "settled", "released"] = "reserved"
    settled_cost: int = Field(default=0, ge=0)


class BudgetAccount(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    account_id: str = Field(min_length=1)
    category: Literal[
        "chat", "repair", "audit", "proactive", "vision", "audio", "image", "tool"
    ]
    window_id: str = Field(min_length=1)
    limit: int = Field(ge=0)
    reserved: int = Field(default=0, ge=0)
    spent: int = Field(default=0, ge=0)
    overrun: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def totals_are_consistent(self) -> BudgetAccount:
        if self.overrun != max(0, self.spent - self.limit):
            raise ValueError("budget account overrun does not match spent and limit")
        return self


class ExecutionReceipt(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    receipt_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_ref: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    receipt_kind: Literal["ack", "terminal"]
    observed_state: Literal[
        "provider_accepted", "delivered", "failed", "cancelled", "expired", "unknown"
    ]
    is_terminal: bool
    artifact_refs: tuple[str, ...] = ()
    cost_actual: int = Field(ge=0)
    error_class: str | None = None
    received_at: datetime
    raw_payload_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def receipt_kind_matches_observed_state(self) -> ExecutionReceipt:
        if self.receipt_kind == "ack" and (
            self.observed_state != "provider_accepted" or self.is_terminal
        ):
            raise ValueError("ack receipt must be non-terminal provider_accepted")
        if self.receipt_kind == "terminal" and (
            self.observed_state == "provider_accepted" or not self.is_terminal
        ):
            raise ValueError("terminal receipt must carry a terminal observed state")
        return self


class BudgetSettlement(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    settlement_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    state: Literal["settled", "released"]
    settlement_kind: Literal["terminal", "reconciliation_adjustment"] = "terminal"
    previous_cost: int = Field(default=0, ge=0)
    cost_actual: int = Field(ge=0)
    cost_delta: int

    @model_validator(mode="after")
    def delta_matches_cost_transition(self) -> BudgetSettlement:
        if self.cost_delta != self.cost_actual - self.previous_cost:
            raise ValueError("cost_delta must equal cost_actual minus previous_cost")
        return self


class ActionReconciliation(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    reconciliation_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    reason: Literal[
        "unknown_action",
        "identity_mismatch",
        "terminal_conflict",
        "invalid_transition",
    ]
    observed_state: ActionState
    existing_state: ActionState | None = None
    provider: str = Field(min_length=1)
    provider_ref: str = Field(min_length=1)
    raw_payload_hash: str = Field(min_length=1)


class WorldProjection(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    world_id: str
    world_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    semantic_hash: str = Field(min_length=64, max_length=64)
    logical_time: datetime | None = None
    character_public: dict[str, Any] = Field(default_factory=dict)
    current_situation: dict[str, Any] = Field(default_factory=dict)
    relationship_public: dict[str, Any] = Field(default_factory=dict)
    affect_summary: dict[str, Any] = Field(default_factory=dict)
    open_threads_summary: tuple[dict[str, Any], ...] = ()
    plans: tuple[dict[str, Any], ...] = ()
    recent_experiences: tuple[dict[str, Any], ...] = ()
    pending_actions: tuple[dict[str, Any], ...] = ()
    media_candidates: tuple[dict[str, Any], ...] = ()
    system_health: dict[str, Any] = Field(default_factory=lambda: {"status": "ok"})
    debug_observation_refs: tuple[str, ...] = ()


class WorldEvent(FrozenModel):
    schema_version: SchemaVersion
    event_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    logical_time: datetime
    created_at: datetime
    actor: str = Field(min_length=1)
    source: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    payload_json: str
    payload_hash: str

    @model_validator(mode="after")
    def payload_hash_matches_immutable_bytes(self) -> WorldEvent:
        actual = hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()
        if actual != self.payload_hash:
            raise ValueError("payload_hash does not match payload_json")
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("event payload must be a JSON object")
        return self

    @classmethod
    def from_payload(cls, *, payload: dict[str, Any], **envelope: Any) -> WorldEvent:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return cls(payload_json=payload_json, payload_hash=payload_hash, **envelope)

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise ValueError("event payload must decode to an object")
        return value


class CommitResult(FrozenModel):
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    event_ids: tuple[str, ...]


class LedgerProjection(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    reducer_bundle_version: str = "world-v2-reducers.1"
    world_id: str
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    logical_time: datetime | None = None
    observation_refs: tuple[str, ...] = ()
    actions: tuple[Action, ...] = ()
    budget_accounts: tuple[BudgetAccount, ...] = ()
    budget_reservations: tuple[BudgetReservation, ...] = ()
    trigger_processes: tuple[TriggerProcess, ...] = ()
    pending_external_observations: tuple[ExternalObservation, ...] = ()
    execution_receipts: tuple[ExecutionReceipt, ...] = ()
    budget_settlements: tuple[BudgetSettlement, ...] = ()
    reconciliations: tuple[ActionReconciliation, ...] = ()
    completed_trigger_ids: tuple[str, ...] = ()
    semantic_hash: str
