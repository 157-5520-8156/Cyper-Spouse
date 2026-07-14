from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


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
    observation_kind: Literal["message"] = "message"
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


class ProjectionCursor(FrozenModel):
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)


class ProjectionRequest(FrozenModel):
    schema_version: SchemaVersion
    request_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    viewer_kind: Literal["platform_adapter", "dashboard_operator", "room_renderer", "evaluator"]
    viewer_id: str = Field(min_length=1)
    permissions: frozenset[
        Literal[
            "projection:actions:status",
            "projection:actions:diagnostic",
            "projection:diagnostics",
            "projection:debug_refs",
            "projection:internal_hash",
            "projection:evaluator:trace",
        ]
    ] = frozenset()
    at_world_revision: int | None = Field(default=None, ge=0)
    at_deliberation_revision: int | None = Field(default=None, ge=0)
    at_ledger_sequence: int | None = Field(default=None, ge=0)
    trace_id: str = Field(min_length=1)
    include_debug_refs: bool = False
    authority_token: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        repr=False,
        exclude=True,
    )
    capability_issued_at: datetime | None = None
    capability_expires_at: datetime | None = None
    redaction_policy: Literal[
        "platform-v1",
        "operator-default-v1",
        "room-public-v1",
        "evaluator-redacted-v1",
    ]

    @model_validator(mode="after")
    def historical_cursor_and_capability_are_complete(self) -> ProjectionRequest:
        historical = (
            self.at_world_revision,
            self.at_deliberation_revision,
            self.at_ledger_sequence,
        )
        if any(value is not None for value in historical) and not all(
            value is not None for value in historical
        ):
            raise ValueError("historical projection requires a complete cursor")
        if (self.capability_issued_at is None) != (self.capability_expires_at is None):
            raise ValueError("projection capability timestamps must be complete")
        if (
            self.capability_issued_at is not None
            and self.capability_expires_at is not None
            and self.capability_expires_at <= self.capability_issued_at
        ):
            raise ValueError("projection capability must expire after issuance")
        return self

    @property
    def at_cursor(self) -> ProjectionCursor | None:
        if self.at_world_revision is None:
            return None
        assert self.at_deliberation_revision is not None
        assert self.at_ledger_sequence is not None
        return ProjectionCursor(
            world_revision=self.at_world_revision,
            deliberation_revision=self.at_deliberation_revision,
            ledger_sequence=self.at_ledger_sequence,
        )


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
    process_kind: Literal[
        "observation",
        "clock",
        "settlement",
        "recovery",
        "npc_world_appraisal",
        "interaction_appraisal",
    ]
    source_evidence_ref: str | None = None
    state: Literal["open", "claimed", "terminal"]
    claim_lease: ClaimLease | None = None
    attempt_ids: tuple[str, ...] = ()
    runtime_outcome_ref: str | None = None

    @model_validator(mode="after")
    def active_attempt_matches_lease(self) -> TriggerProcess:
        if (
            self.process_kind not in {"npc_world_appraisal", "interaction_appraisal"}
            and self.source_evidence_ref is not None
        ):
            raise ValueError("only appraisal triggers may carry source evidence")
        if self.state == "open":
            if self.claim_lease is not None or self.attempt_ids:
                raise ValueError("open trigger cannot already own an attempt lease")
            return self
        if self.claim_lease is None or not self.attempt_ids:
            raise ValueError("claimed or terminal trigger requires an attempt lease")
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
    category: Literal["chat", "repair", "audit", "proactive", "vision", "audio", "image", "tool"]
    amount_limit: int = Field(ge=0)
    state: Literal["reserved", "settled", "released"] = "reserved"
    settled_cost: int = Field(default=0, ge=0)


class BudgetAccount(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    account_id: str = Field(min_length=1)
    category: Literal["chat", "repair", "audit", "proactive", "vision", "audio", "image", "tool"]
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


class PendingActionSummary(FrozenModel):
    action_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    layer: Literal["external_action", "media_action", "read_only_tool"]
    state: ActionState
    not_before: datetime | None = None
    expires_at: datetime | None = None
    dependencies: tuple[str, ...] = ()


class PlatformActionStatusProjection(PendingActionSummary):
    target: str = Field(min_length=1)


class DiagnosticActionProjection(PendingActionSummary):
    pass


class ProjectionSystemHealth(FrozenModel):
    status: Literal["ok", "degraded", "rebuilding"] = "ok"
    reducer_bundle_version: str | None = None
    deliberation_revision: int | None = Field(default=None, ge=0)
    action_count: int | None = Field(default=None, ge=0)
    pending_action_count: int | None = Field(default=None, ge=0)
    budget_account_count: int | None = Field(default=None, ge=0)
    reserved_budget: int | None = Field(default=None, ge=0)
    spent_budget: int | None = Field(default=None, ge=0)
    pending_external_result_count: int | None = Field(default=None, ge=0)
    reconciliation_count: int | None = Field(default=None, ge=0)
    unavailable_slices: tuple[str, ...] = ()


class ProjectionSliceWindow(FrozenModel):
    slice_name: str = Field(min_length=1)
    total_active: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    truncated: bool
    availability: Literal["available", "unavailable"] = "available"
    unavailable_reason: str | None = None
    authority_query_ref: str | None = None
    ordering_policy: str = Field(min_length=1)
    retention_policy_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def returned_items_do_not_exceed_total(self) -> ProjectionSliceWindow:
        if self.returned_count > self.total_active:
            raise ValueError("returned_count cannot exceed total_active")
        if self.truncated != (self.returned_count < self.total_active):
            raise ValueError("truncated must describe the returned slice")
        if self.availability == "unavailable" and not self.unavailable_reason:
            raise ValueError("unavailable slice requires a reason")
        if self.availability == "available" and self.unavailable_reason is not None:
            raise ValueError("available slice cannot have an unavailable reason")
        return self


class PlatformProjectionView(FrozenModel):
    view_kind: Literal["platform"] = "platform"
    action_statuses: tuple[PlatformActionStatusProjection, ...] = ()
    slice_windows: tuple[ProjectionSliceWindow, ...] = ()


class OperatorProjectionView(FrozenModel):
    view_kind: Literal["operator"] = "operator"
    pending_actions: tuple[DiagnosticActionProjection, ...] = ()
    system_health: ProjectionSystemHealth = Field(default_factory=ProjectionSystemHealth)
    debug_observation_refs: tuple[str, ...] = ()
    slice_windows: tuple[ProjectionSliceWindow, ...] = ()


class PublicSituationProjection(FrozenModel):
    location_ref: str | None = None
    activity: str | None = None
    activity_phase: str | None = None
    attention: str | None = None
    visible_status: str | None = None


class PublicAffectProjection(FrozenModel):
    display_state: str | None = None
    intensity_band: Literal["subtle", "noticeable", "strong"] | None = None


class RoomProjectionView(FrozenModel):
    view_kind: Literal["room"] = "room"
    situation: PublicSituationProjection = Field(default_factory=PublicSituationProjection)
    affect_display: PublicAffectProjection = Field(default_factory=PublicAffectProjection)
    approved_media_refs: tuple[str, ...] = ()


class NamedCount(FrozenModel):
    name: str = Field(min_length=1)
    count: int = Field(ge=0)


class EvaluatorProjectionView(FrozenModel):
    view_kind: Literal["evaluator"] = "evaluator"
    redacted_trace_refs: tuple[str, ...] = ()
    action_state_counts: tuple[NamedCount, ...] = ()


ViewerProjection = (
    PlatformProjectionView | OperatorProjectionView | RoomProjectionView | EvaluatorProjectionView
)


class WorldProjection(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    world_id: str
    world_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    viewer_kind: Literal["platform_adapter", "dashboard_operator", "room_renderer", "evaluator"]
    redaction_policy: str = Field(min_length=1)
    projection_policy_version: str = "world-v2-projection-policy.1"
    reducer_bundle_version: str = Field(min_length=1)
    projection_hash: str = Field(min_length=64, max_length=64)
    semantic_hash: str | None = Field(default=None, min_length=64, max_length=64)
    logical_time: datetime | None = None
    view: ViewerProjection = Field(discriminator="view_kind")


PrivacyClass = Literal["public", "shareable", "personal", "private", "withhold"]


class EvidenceRef(FrozenModel):
    ref_id: str = Field(min_length=1)
    evidence_type: Literal[
        "committed_fact",
        "committed_experience",
        "committed_world_event",
        "settled_world_event",
        "settled_external_result",
        "observed_message",
        "active_plan",
        "operator_observation",
        "clock_observation",
    ]
    claim_purpose: Literal[
        "current_fact",
        "past_experience",
        "future_plan",
        "private_hypothesis",
        "action_authorization",
        "conversation_continuity",
    ]
    source_world_revision: int | None = Field(default=None, ge=1)
    immutable_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def committed_world_evidence_is_revision_pinned(self) -> EvidenceRef:
        if self.evidence_type in {"committed_world_event", "settled_world_event"}:
            if self.source_world_revision is None or self.immutable_hash is None:
                raise ValueError("world-event evidence requires revision and immutable hash")
        return self


class CommittedWorldEventRef(FrozenModel):
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    world_revision: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)
    logical_time: datetime
    continuation_refs: tuple[str, ...] = ()


class OperatorObservationRef(FrozenModel):
    observation_id: str = Field(min_length=1)
    observation_hash: str = Field(min_length=64, max_length=64)


class MessageObservationRef(FrozenModel):
    observation_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    content_payload_hash: str = Field(min_length=1)
    event_payload_hash: str = Field(min_length=64, max_length=64)
    world_revision: int = Field(ge=1)


class AcceptanceDecisionRef(FrozenModel):
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    acceptance_id: str | None = None
    status: Literal["accepted", "rejected", "stale"]
    accepted_change_id: str | None = None
    accepted_change_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def accepted_decision_has_a_complete_change(self) -> AcceptanceDecisionRef:
        if self.status == "accepted" and (
            self.acceptance_id is None
            or self.accepted_change_id is None
            or self.accepted_change_hash is None
        ):
            raise ValueError("accepted decision requires complete change authority")
        if self.status != "accepted" and (
            self.accepted_change_id is not None or self.accepted_change_hash is not None
        ):
            raise ValueError("non-accepted decision cannot carry accepted change authority")
        return self


class ProposalRevisionRef(FrozenModel):
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)


class DueWindow(FrozenModel):
    opens_at: datetime
    closes_at: datetime

    @model_validator(mode="after")
    def closes_after_opening(self) -> DueWindow:
        if self.closes_at <= self.opens_at:
            raise ValueError("due window must close after it opens")
        return self


class CharacterCoreProjection(FrozenModel):
    core_revision: int = Field(default=0, ge=0)
    identity_refs: tuple[str, ...] = ()
    traits: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()


class FactProjection(FrozenModel):
    fact_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value_ref: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=0, le=10_000)
    status: Literal["active", "corrected", "superseded", "expired"]
    privacy_class: PrivacyClass
    updated_at: datetime


class ExperienceProjection(FrozenModel):
    experience_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    summary_ref: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    occurred_from: datetime
    occurred_to: datetime
    participant_refs: tuple[str, ...] = Field(min_length=1)
    occurrence_refs: tuple[str, ...] = ()
    result_refs: tuple[str, ...] = ()
    privacy_class: PrivacyClass
    status: Literal["committed", "superseded"] = "committed"

    @model_validator(mode="after")
    def experience_has_settled_origin(self) -> ExperienceProjection:
        if self.occurred_to < self.occurred_from:
            raise ValueError("experience occurrence window is reversed")
        if not self.occurrence_refs and not self.result_refs:
            raise ValueError("experience requires an occurrence or settled result")
        if not any(
            evidence.claim_purpose == "past_experience"
            and evidence.evidence_type
            in {
                "committed_experience",
                "committed_world_event",
                "settled_world_event",
                "settled_external_result",
                "operator_observation",
            }
            for evidence in self.evidence_refs
        ):
            raise ValueError("experience requires evidence of something that occurred")
        return self


class SituationStateProjection(FrozenModel):
    location_ref: str | None = None
    activity: str | None = None
    activity_phase: str | None = None
    attention: str | None = None
    energy: str | None = None
    current_goal_ref: str | None = None
    participant_refs: tuple[str, ...] = ()
    visibility: PrivacyClass = "private"


class PlanStateProjection(FrozenModel):
    plan_id: str = Field(min_length=1)
    activity_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    activity_kind: str = Field(min_length=1)
    goal_ref: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    status: Literal["planned", "active", "paused", "completed", "abandoned"]
    importance_bp: int = Field(ge=0, le=10_000)
    scheduled_window: DueWindow | None = None
    participant_refs: tuple[str, ...] = ()
    location_ref: str | None = None
    supersedes_plan_id: str | None = None
    last_transitioned_at: datetime | None = None
    terminal_reason_ref: str | None = None
    privacy_class: PrivacyClass = "private"


class NpcProjection(FrozenModel):
    npc_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    stable_identity_ref: str = Field(min_length=1)
    known_trait_refs: tuple[str, ...] = ()
    privacy_class: PrivacyClass
    current_location_ref: str | None = None
    status: Literal["active", "retired"] = "active"


class OutcomeObservationProjection(FrozenModel):
    observation_id: str = Field(min_length=1)
    occurrence_id: str = Field(min_length=1)
    source_kind: Literal[
        "settled_external_result",
        "clock_plan_precondition",
        "operator_observation",
        "committed_world_event",
    ]
    source_refs: tuple[str, ...] = Field(min_length=1)
    observed_payload_ref: str = Field(min_length=1)
    observed_payload_hash: str = Field(min_length=1)
    observed_at: datetime
    confidence_bp: int = Field(ge=0, le=10_000)


class OutcomeProposalProjection(FrozenModel):
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


class WorldOccurrenceProjection(FrozenModel):
    occurrence_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    trigger_ref: str = Field(min_length=1)
    participant_refs: tuple[str, ...] = Field(min_length=1)
    location_ref: str = Field(min_length=1)
    time_window: DueWindow
    precondition_refs: tuple[str, ...] = ()
    satisfied_precondition_refs: tuple[str, ...] = ()
    candidate_outcome_refs: tuple[str, ...] = Field(min_length=1)
    observation_refs: tuple[str, ...] = ()
    visibility: PrivacyClass
    status: Literal["committed", "active", "settled", "cancelled", "expired"]
    activated_at: datetime | None = None
    result_id: str | None = None
    result_payload_ref: str | None = None
    result_payload_hash: str | None = None
    settled_at: datetime | None = None
    terminal_reason_ref: str | None = None


class AppraisalHypothesis(FrozenModel):
    hypothesis_id: str = Field(min_length=1)
    meaning: Literal[
        "ordinary",
        "care",
        "support",
        "shared_joy",
        "goal_progress",
        "uncertainty",
        "misunderstanding",
        "disappointment",
        "dismissal",
        "boundary_violation",
        "dehumanization",
        "coercion",
        "control_pressure",
        "betrayal",
        "loss",
        "user_withdrawing",
        "user_confused",
        "repair_attempt",
        "reliability_confirmed",
        "reliability_broken",
        "restorative_solitude",
        "creative_satisfaction",
        "social_warmth",
        "goal_strain",
        "npc_conflict",
        "family_connection",
    ]
    attribution: Literal["user", "companion", "npc", "situation", "third_party", "unknown"]
    controllability: Literal["controllable", "partly_controllable", "uncontrollable"]
    severity: Literal["low", "moderate", "high", "acute"]
    weight_bp: int = Field(ge=1, le=10_000)


class AppraisalOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    matrix_catalog_version: str = Field(min_length=1)
    clustering_policy_version: str = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


class AppraisalProjection(FrozenModel):
    appraisal_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    subject_ref: str = Field(min_length=1)
    source_cluster_ref: str = Field(min_length=1)
    origin: AppraisalOrigin
    hypotheses: tuple[AppraisalHypothesis, ...] = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=1, le=10_000)
    accepted_at: datetime
    expires_at: datetime
    status: Literal["active", "contradicted", "expired", "superseded"] = "active"
    closed_at: datetime | None = None
    contradiction_refs: tuple[EvidenceRef, ...] = ()
    supersedes_appraisal_id: str | None = None
    superseded_by_appraisal_id: str | None = None

    @model_validator(mode="after")
    def normalized_and_lifecycle_consistent(self) -> AppraisalProjection:
        for name, value in (
            ("accepted_at", self.accepted_at),
            ("expires_at", self.expires_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.expires_at <= self.accepted_at:
            raise ValueError("appraisal expiry must follow acceptance")
        if sum(item.weight_bp for item in self.hypotheses) != 10_000:
            raise ValueError("appraisal hypothesis weights must total 10,000 bp")
        if len({item.hypothesis_id for item in self.hypotheses}) != len(self.hypotheses):
            raise ValueError("appraisal hypothesis IDs must be unique")
        semantic_keys = {
            (item.meaning, item.attribution, item.controllability, item.severity)
            for item in self.hypotheses
        }
        if len(semantic_keys) != len(self.hypotheses):
            raise ValueError("duplicate appraisal hypotheses are not alternatives")
        if self.status == "active" and (
            self.closed_at is not None
            or self.contradiction_refs
            or self.superseded_by_appraisal_id is not None
        ):
            raise ValueError("active appraisal cannot contain terminal state")
        if self.status != "active" and self.closed_at is None:
            raise ValueError("terminal appraisal requires closed_at")
        if self.closed_at is not None:
            if self.closed_at.tzinfo is None or self.closed_at.utcoffset() is None:
                raise ValueError("closed_at must be timezone-aware")
            if self.closed_at < self.accepted_at:
                raise ValueError("appraisal cannot close before acceptance")
        if self.status == "contradicted" and not self.contradiction_refs:
            raise ValueError("contradicted appraisal requires contradiction evidence")
        if self.status != "contradicted" and self.contradiction_refs:
            raise ValueError("only contradicted appraisal carries contradiction evidence")
        if self.status == "superseded" and not self.superseded_by_appraisal_id:
            raise ValueError("superseded appraisal requires its successor")
        if self.status != "superseded" and self.superseded_by_appraisal_id:
            raise ValueError("only superseded appraisal carries successor link")
        return self


class AppraisalMeaningRef(FrozenModel):
    appraisal_id: str = Field(min_length=1)
    accepted_entity_revision: Literal[1] = 1
    hypothesis_id: str = Field(min_length=1)
    source_cluster_ref: str = Field(min_length=1)
    accepted_change_id: str = Field(min_length=1)
    accepted_transition_id: str = Field(min_length=1)


class AppraisalProposalProjection(FrozenModel):
    """Persisted deliberation audit for one proposed appraisal transition."""

    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["appraisal_transition"] = "appraisal_transition"
    transition_kind: Literal["accept", "contradict", "supersede"]
    change_id: str = Field(min_length=1)
    trigger_id: str = Field(min_length=1)
    trigger_ref: str = Field(min_length=1)
    source_evidence_ref: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: AppraisalProposedMutation

    @model_validator(mode="after")
    def source_is_part_of_the_proposal_evidence(self) -> AppraisalProposalProjection:
        if self.source_evidence_ref not in {ref.ref_id for ref in self.evidence_refs}:
            raise ValueError("appraisal proposal must include its trigger source evidence")
        if len(self.policy_refs) != len(set(self.policy_refs)):
            raise ValueError("appraisal proposal policy refs must be unique")
        expected_event_type = {
            "accept": "AppraisalAccepted",
            "contradict": "AppraisalContradicted",
            "supersede": "AppraisalSuperseded",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected_event_type:
            raise ValueError("proposed mutation type does not match transition kind")
        return self


class AppraisalProposedMutation(FrozenModel):
    event_type: Literal["AppraisalAccepted", "AppraisalContradicted", "AppraisalSuperseded"]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_a_canonical_object(self) -> AppraisalProposedMutation:
        try:
            payload = json.loads(self.payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("proposed mutation payload is not JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("proposed mutation payload must be an object")
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if canonical != self.payload_json:
            raise ValueError("proposed mutation payload must use canonical JSON")
        return self


AppraisalProposalProjection.model_rebuild()


class CommitmentStateProjection(FrozenModel):
    commitment_id: str = Field(min_length=1)
    content_ref: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    importance_bp: int = Field(ge=0, le=10_000)
    persistence_level: Literal["ephemeral", "turn", "session", "durable"]
    due_window: tuple[datetime, datetime] | None = None
    status: Literal["open", "fulfilled", "broken", "released"]
    privacy_class: PrivacyClass = "private"


def affect_decay_config_digest(
    *,
    kind: str,
    half_life_seconds: int,
    floor_bp: int,
    delay_seconds: int,
    config_version: str,
    algorithm_version: str = "affect-decay-exp2-q48-binary-rhe-v1",
    table_digest: str = "6a3abe39937394f738dcd1563189086020127b7e1e7189868c84ae3f890ee49f",
    rounding_mode: str = "round-half-even",
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "algorithm_version": algorithm_version,
                "config_version": config_version,
                "delay_seconds": delay_seconds,
                "floor_bp": floor_bp,
                "half_life_seconds": half_life_seconds,
                "kind": kind,
                "rounding_mode": rounding_mode,
                "table_digest": table_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class AffectDecayProfileProjection(FrozenModel):
    kind: Literal["exponential_half_life"] = "exponential_half_life"
    half_life_seconds: int = Field(gt=0)
    floor_bp: int = Field(ge=0, le=10_000)
    delay_seconds: int = Field(default=0, ge=0)
    config_version: str = Field(min_length=1)
    algorithm_version: Literal["affect-decay-exp2-q48-binary-rhe-v1"] = (
        "affect-decay-exp2-q48-binary-rhe-v1"
    )
    table_digest: Literal["6a3abe39937394f738dcd1563189086020127b7e1e7189868c84ae3f890ee49f"] = (
        "6a3abe39937394f738dcd1563189086020127b7e1e7189868c84ae3f890ee49f"
    )
    rounding_mode: Literal["round-half-even"] = "round-half-even"
    config_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def config_digest_binds_every_decay_parameter(self) -> AffectDecayProfileProjection:
        expected = affect_decay_config_digest(
            kind=self.kind,
            half_life_seconds=self.half_life_seconds,
            floor_bp=self.floor_bp,
            delay_seconds=self.delay_seconds,
            config_version=self.config_version,
            algorithm_version=self.algorithm_version,
            table_digest=self.table_digest,
            rounding_mode=self.rounding_mode,
        )
        if self.config_digest != expected:
            raise ValueError("affect decay config digest does not match parameters")
        return self


class AffectBaselineProjection(FrozenModel):
    dimension: Literal[
        "hurt", "anger", "sadness", "loneliness", "anxiety", "resentment", "warmth", "joy"
    ]
    baseline_bp: int = Field(ge=0, le=10_000)
    calibration_revision: int = Field(ge=0)
    policy_version: str = Field(min_length=1)
    last_calibrated_at: datetime
    calibrated_through: datetime
    last_calibration_basis_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def calibration_times_are_ordered(self) -> AffectBaselineProjection:
        for value in (self.last_calibrated_at, self.calibrated_through):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("affect baseline times must be timezone-aware")
        if self.calibrated_through > self.last_calibrated_at:
            raise ValueError("baseline cannot be calibrated through the future")
        return self


class AffectCalibrationEpisodeRef(FrozenModel):
    episode_id: str = Field(min_length=1)
    terminal_entity_revision: int = Field(ge=2)
    component_id: str = Field(min_length=1)


class AffectAggregateProjection(FrozenModel):
    dimension: Literal[
        "hurt", "anger", "sadness", "loneliness", "anxiety", "resentment", "warmth", "joy"
    ]
    intensity_bp: int = Field(ge=0, le=10_000)
    active_component_count: int = Field(ge=0)


class AffectComponentProjection(FrozenModel):
    component_id: str = Field(min_length=1)
    dimension: Literal[
        "hurt", "anger", "sadness", "loneliness", "anxiety", "resentment", "warmth", "joy"
    ]
    source_cluster_ref: str = Field(min_length=1)
    appraisal_refs: tuple[AppraisalMeaningRef, ...] = Field(min_length=1)
    intensity_bp: int = Field(ge=0, le=10_000)
    decay_anchor_intensity_bp: int = Field(ge=0, le=10_000)
    opened_at: datetime
    decay_anchor_at: datetime
    decay_not_before: datetime
    last_stimulus_at: datetime
    last_updated_at: datetime
    decay_profile: AffectDecayProfileProjection
    residue_bp: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def component_time_moves_forward(self) -> AffectComponentProjection:
        times = (
            self.opened_at,
            self.decay_anchor_at,
            self.decay_not_before,
            self.last_stimulus_at,
            self.last_updated_at,
        )
        if any(item.tzinfo is None or item.utcoffset() is None for item in times):
            raise ValueError("affect component times must be timezone-aware")
        if any(item < self.opened_at for item in times[1:]):
            raise ValueError("affect component transition precedes opening")
        lower_bound = max(self.decay_profile.floor_bp, self.residue_bp)
        if self.intensity_bp < lower_bound or self.decay_anchor_intensity_bp < lower_bound:
            raise ValueError("affect intensity cannot fall below floor or residue")
        return self


class AffectOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    matrix_catalog_version: str = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


class AffectEpisodeProjection(FrozenModel):
    episode_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    origin: AffectOrigin
    components: tuple[AffectComponentProjection, ...] = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    opened_at: datetime
    updated_at: datetime
    status: Literal["active", "resolved", "superseded"]
    privacy_class: PrivacyClass = "private"
    expression_history_refs: tuple[str, ...] = ()
    closed_at: datetime | None = None
    resolution_refs: tuple[EvidenceRef, ...] = ()
    supersedes_episode_id: str | None = None
    superseded_by_episode_id: str | None = None

    @model_validator(mode="after")
    def affect_episode_is_structurally_consistent(self) -> AffectEpisodeProjection:
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("affect episode opened_at must be timezone-aware")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("affect episode updated_at must be timezone-aware")
        if self.updated_at < self.opened_at:
            raise ValueError("affect episode update precedes opening")
        component_ids = tuple(item.component_id for item in self.components)
        component_keys = tuple(
            (item.dimension, item.source_cluster_ref) for item in self.components
        )
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("affect component identities must be unique")
        if len(component_keys) != len(set(component_keys)):
            raise ValueError("affect component dimension/source keys must be unique")
        if any(item.opened_at < self.opened_at for item in self.components):
            raise ValueError("affect component cannot predate its episode")
        if self.status == "active" and (
            self.closed_at is not None
            or self.resolution_refs
            or self.superseded_by_episode_id is not None
        ):
            raise ValueError("active affect episode cannot contain terminal state")
        if self.status != "active" and self.closed_at is None:
            raise ValueError("terminal affect episode requires closed_at")
        if self.status == "resolved" and not self.resolution_refs:
            raise ValueError("resolved affect episode requires resolution evidence")
        if self.status != "resolved" and self.resolution_refs:
            raise ValueError("only resolved affect episode carries resolution evidence")
        if self.status == "superseded" and not self.superseded_by_episode_id:
            raise ValueError("superseded affect episode requires successor identity")
        if self.status != "superseded" and self.superseded_by_episode_id:
            raise ValueError("only superseded affect episode carries successor identity")
        return self


class AffectProposedMutation(FrozenModel):
    event_type: Literal[
        "AffectEpisodeOpened",
        "AffectEpisodeUpdated",
        "AffectEpisodeResolved",
        "AffectEpisodeSuperseded",
        "AffectBaselineAdjusted",
    ]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_a_canonical_object(self) -> AffectProposedMutation:
        try:
            payload = json.loads(self.payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("proposed affect mutation payload is not JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("proposed affect mutation payload must be an object")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise ValueError("proposed affect mutation payload must use canonical JSON")
        return self


class AffectProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["affect_transition"] = "affect_transition"
    transition_kind: Literal["open", "update", "resolve", "supersede", "baseline_adjust"]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    appraisal_refs: tuple[AppraisalMeaningRef, ...] = ()
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: AffectProposedMutation

    @model_validator(mode="after")
    def mutation_type_matches_transition(self) -> AffectProposalProjection:
        expected = {
            "open": "AffectEpisodeOpened",
            "update": "AffectEpisodeUpdated",
            "resolve": "AffectEpisodeResolved",
            "supersede": "AffectEpisodeSuperseded",
            "baseline_adjust": "AffectBaselineAdjusted",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected:
            raise ValueError("proposed affect mutation type does not match transition")
        if len(self.policy_refs) != len(set(self.policy_refs)):
            raise ValueError("affect proposal policy refs must be unique")
        return self


class PrivateImpressionProjection(FrozenModel):
    impression_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    interpretation_refs: tuple[str, ...] = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=0, le=10_000)
    first_seen: datetime
    last_supported: datetime
    expiry_condition: str = Field(min_length=1)
    contradiction_refs: tuple[str, ...] = ()
    status: Literal["active", "contradicted", "expired", "superseded"]


class RelationshipVariablesProjection(FrozenModel):
    trust_bp: int = Field(default=0, ge=0, le=10_000)
    closeness_bp: int = Field(default=0, ge=0, le=10_000)
    respect_bp: int = Field(default=0, ge=0, le=10_000)
    reliability_bp: int = Field(default=0, ge=0, le=10_000)
    mutuality_bp: int = Field(default=0, ge=0, le=10_000)
    repair_confidence_bp: int = Field(default=0, ge=0, le=10_000)


class RelationshipVariableDeltas(FrozenModel):
    trust_bp: int = Field(default=0, ge=-10_000, le=10_000)
    closeness_bp: int = Field(default=0, ge=-10_000, le=10_000)
    respect_bp: int = Field(default=0, ge=-10_000, le=10_000)
    reliability_bp: int = Field(default=0, ge=-10_000, le=10_000)
    mutuality_bp: int = Field(default=0, ge=-10_000, le=10_000)
    repair_confidence_bp: int = Field(default=0, ge=-10_000, le=10_000)


RelationshipStage = Literal[
    "stranger", "acquaintance", "friend", "close_friend", "ambiguous", "lover"
]


class RelationshipHysteresisProjection(FrozenModel):
    candidate_stage: Literal["stranger", "acquaintance", "friend", "close_friend"] | None = None
    direction: Literal["promote", "demote"] | None = None
    candidate_since: datetime | None = None
    confirming_adjustment_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def candidate_state_is_complete(self) -> RelationshipHysteresisProjection:
        values = (self.candidate_stage, self.direction, self.candidate_since)
        if any(item is None for item in values) != all(item is None for item in values):
            raise ValueError("relationship hysteresis candidate must be complete")
        if self.candidate_stage is None and self.confirming_adjustment_count != 0:
            raise ValueError("empty relationship hysteresis cannot have confirmations")
        if self.candidate_stage is not None and self.confirming_adjustment_count < 1:
            raise ValueError("relationship hysteresis candidate requires confirmation")
        if self.candidate_since is not None and (
            self.candidate_since.tzinfo is None
            or self.candidate_since.utcoffset() is None
        ):
            raise ValueError("relationship hysteresis time must be timezone-aware")
        return self


class RelationshipSignalOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


def relationship_signal_fingerprint(
    *,
    subject_ref: str,
    signal_code: str,
    evidence_refs: tuple[EvidenceRef, ...],
    policy_refs: tuple[str, ...],
) -> str:
    material = {
        "subject_ref": subject_ref,
        "signal_code": signal_code,
        "evidence_refs": sorted(
            (
                {
                    "evidence_type": item.evidence_type,
                    "ref_id": item.ref_id,
                    "source_world_revision": item.source_world_revision,
                    "immutable_hash": item.immutable_hash,
                }
                for item in evidence_refs
            ),
            key=lambda item: (str(item.get("ref_id")), json.dumps(item, sort_keys=True)),
        ),
        "policy_refs": sorted(policy_refs),
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RelationshipSignalProjection(FrozenModel):
    signal_id: str = Field(min_length=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    entity_revision: int = Field(ge=1)
    subject_ref: str = Field(min_length=1)
    signal_code: str = Field(min_length=1)
    confidence_bp: int = Field(ge=1, le=10_000)
    persistence: Literal["session", "durable"]
    contradiction_group_ref: str | None = None
    rationale_code: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    origin: RelationshipSignalOrigin
    accepted_at: datetime

    @model_validator(mode="after")
    def fingerprint_matches_semantic_evidence(self) -> RelationshipSignalProjection:
        expected = relationship_signal_fingerprint(
            subject_ref=self.subject_ref,
            signal_code=self.signal_code,
            evidence_refs=self.evidence_refs,
            policy_refs=self.origin.policy_refs,
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("relationship signal semantic fingerprint does not match authority")
        return self


class RelationshipProposedMutation(FrozenModel):
    event_type: Literal[
        "RelationshipSignalAccepted",
        "RelationshipSlowVariableAdjusted",
        "BoundaryChanged",
    ]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> RelationshipProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("relationship mutation payload must be an object")
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise ValueError("relationship mutation payload must use canonical JSON")
        return self


class RelationshipProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["relationship_transition"] = "relationship_transition"
    proposal_encoding: Literal["typed-authority-v1"]
    authority_contract_ref: Literal["proposal-contract:relationship.1"]
    transition_kind: Literal[
        "signal",
        "adjust",
        "compensate",
        "boundary_open",
        "boundary_revise",
        "boundary_close",
    ]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: RelationshipProposedMutation

    @model_validator(mode="after")
    def transition_matches_event(self) -> RelationshipProposalProjection:
        expected = {
            "signal": "RelationshipSignalAccepted",
            "adjust": "RelationshipSlowVariableAdjusted",
            "compensate": "RelationshipSlowVariableAdjusted",
            "boundary_open": "BoundaryChanged",
            "boundary_revise": "BoundaryChanged",
            "boundary_close": "BoundaryChanged",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected:
            raise ValueError("relationship proposal transition does not match event")
        return self


class RelationshipAdjustmentProjection(FrozenModel):
    adjustment_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    relationship_revision: int = Field(ge=1)
    operation: Literal["adjust", "compensate"]
    signal_refs: tuple[str, ...] = Field(min_length=1)
    proposed_deltas: RelationshipVariableDeltas
    accepted_deltas: RelationshipVariableDeltas
    variables_before: RelationshipVariablesProjection
    variables_after: RelationshipVariablesProjection
    stage_before: RelationshipStage
    stage_after: RelationshipStage
    hysteresis_before: RelationshipHysteresisProjection
    hysteresis_after: RelationshipHysteresisProjection
    commitment_refs: tuple[str, ...] = ()
    confidence_bp: int = Field(ge=1, le=10_000)
    persistence: Literal["session", "durable"]
    contradiction_group_ref: str | None = None
    rationale_code: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    adjusted_at: datetime
    compensates_adjustment_id: str | None = None


class RelationshipBoundaryOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


class BoundaryProjection(FrozenModel):
    boundary_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    subject_ref: str = Field(min_length=1)
    scope_ref: str = Field(min_length=1)
    strength_bp: int = Field(ge=0, le=10_000)
    status: Literal["active", "closed"]
    expires_at: datetime | None = None
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    origin: RelationshipBoundaryOrigin
    policy_version: str = Field(min_length=1)
    opened_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def boundary_time_is_ordered(self) -> BoundaryProjection:
        if self.updated_at < self.opened_at:
            raise ValueError("boundary update precedes opening")
        if self.expires_at is not None and self.expires_at <= self.opened_at:
            raise ValueError("boundary expiry must follow opening")
        return self


class RelationshipStateProjection(FrozenModel):
    relationship_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    entity_revision: int = Field(default=1, ge=1)
    stage: RelationshipStage = "stranger"
    variables: RelationshipVariablesProjection = Field(
        default_factory=RelationshipVariablesProjection
    )
    temperature: str = "ordinary"
    policy_version: str = "relationship-policy.1"
    policy_digest: str = Field(min_length=64, max_length=64)
    hysteresis: RelationshipHysteresisProjection = Field(
        default_factory=RelationshipHysteresisProjection
    )
    commitment_refs: tuple[str, ...] = ()
    last_adjusted_at: datetime | None = None


class ConversationThreadProjection(FrozenModel):
    thread_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    opened_by_ref: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    importance_bp: int = Field(ge=0, le=10_000)
    due_window: tuple[datetime, datetime] | None = None
    expected_response_ref: str | None = None
    status: Literal["open", "answered", "skipped", "superseded", "cancelled", "expired"]
    resolution_ref: str | None = None


ThreadKind = Literal[
    "question_pending",
    "topic_open",
    "repair_open",
    "external_result_pending",
    "coordination_pending",
    "reply_reconsideration",
]
ThreadStatus = Literal["open", "resolved", "superseded", "cancelled", "expired"]


def thread_semantic_fingerprint(
    *,
    kind: ThreadKind,
    subject_ref: str,
    conversation_ref: str,
    anchor_evidence_refs: tuple[EvidenceRef, ...],
    resolution_contract_ref: str,
    policy_refs: tuple[str, ...],
) -> str:
    """Stable active-thread identity; intentionally excludes IDs and tunable labels."""

    material = {
        "kind": kind,
        "subject_ref": subject_ref,
        "conversation_ref": conversation_ref,
        "anchor_evidence_identity": sorted(
            (
                {
                    "ref_id": item.ref_id,
                    "evidence_type": item.evidence_type,
                    "source_world_revision": item.source_world_revision,
                    "immutable_hash": item.immutable_hash,
                }
                for item in anchor_evidence_refs
            ),
            key=lambda item: (str(item["ref_id"]), json.dumps(item, sort_keys=True)),
        ),
        "resolution_contract_ref": resolution_contract_ref,
        "policy_refs": sorted(policy_refs),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ThreadValues(FrozenModel):
    kind: ThreadKind
    subject_ref: str = Field(min_length=1)
    conversation_ref: str = Field(min_length=1)
    anchor_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    source_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    importance_bp: int = Field(ge=0, le=10_000)
    due_window: DueWindow | None = None
    expires_at: datetime | None = None
    resolution_contract_ref: str = Field(min_length=1)
    privacy_class: PrivacyClass = "private"
    status: ThreadStatus = "open"
    resolution_kind: Literal["answered", "skipped"] | None = None
    resolution_ref: str | None = None
    cancellation_reason_code: Literal["user_withdrew", "obsolete", "invalid", "duplicate"] | None = None
    cancellation_evidence_ref: str | None = None
    superseded_by_thread_ref: str | None = None
    predecessor_thread_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def lifecycle_shape_is_explicit(self) -> ThreadValues:
        temporal_values = (
            *((self.due_window.opens_at, self.due_window.closes_at) if self.due_window else ()),
            *((self.expires_at,) if self.expires_at else ()),
        )
        if any(item.tzinfo is None or item.utcoffset() is None for item in temporal_values):
            raise ValueError("thread temporal bounds must be timezone-aware")
        if len(self.source_evidence_refs) != len(
            {item.ref_id for item in self.source_evidence_refs}
        ):
            raise ValueError("thread source evidence refs must be unique")
        if not set(self.anchor_evidence_refs).issubset(set(self.source_evidence_refs)):
            raise ValueError("thread anchor evidence must remain in source evidence")
        if self.expires_at is not None and self.due_window is not None:
            if self.expires_at < self.due_window.closes_at:
                raise ValueError("thread expiry cannot precede its due window close")
        if self.status == "open" and (
            self.resolution_ref is not None
            or self.resolution_kind is not None
            or self.cancellation_reason_code is not None
            or self.cancellation_evidence_ref is not None
            or self.superseded_by_thread_ref is not None
        ):
            raise ValueError("open thread cannot carry terminal resolution")
        if self.status == "resolved" and (
            not self.resolution_ref or self.resolution_kind is None
        ):
            raise ValueError("resolved thread requires a resolution ref")
        if self.status == "cancelled" and (
            self.cancellation_reason_code is None or not self.cancellation_evidence_ref
        ):
            raise ValueError("cancelled thread requires a reason ref")
        if self.status == "superseded" and not self.superseded_by_thread_ref:
            raise ValueError("superseded thread requires its successor ref")
        if self.status != "superseded" and self.superseded_by_thread_ref is not None:
            raise ValueError("only superseded thread may identify a successor")
        if self.status != "resolved" and (
            self.resolution_kind is not None or self.resolution_ref is not None
        ):
            raise ValueError("only resolved thread may carry a resolution")
        if self.status != "cancelled" and (
            self.cancellation_reason_code is not None
            or self.cancellation_evidence_ref is not None
        ):
            raise ValueError("only cancelled thread may carry a cancellation reason")
        return self


class ThreadOrigin(FrozenModel):
    authority_mode: Literal["accepted_proposal", "mechanical_clock"] = "accepted_proposal"
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


class ThreadProjection(FrozenModel):
    thread_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: ThreadValues
    origin: ThreadOrigin
    opened_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def semantic_identity_matches_sources(self) -> ThreadProjection:
        expected = thread_semantic_fingerprint(
            kind=self.values.kind,
            subject_ref=self.values.subject_ref,
            conversation_ref=self.values.conversation_ref,
            anchor_evidence_refs=self.values.anchor_evidence_refs,
            resolution_contract_ref=self.values.resolution_contract_ref,
            policy_refs=self.origin.policy_refs,
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("thread semantic fingerprint does not match its sources")
        return self


class ThreadTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal[
        "open", "update", "resolve", "cancel", "supersede", "compensate", "expire"
    ]
    values_before: ThreadValues | None
    values_after: ThreadValues
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime
    compensates_transition_id: str | None = None


class ThreadProposedMutation(FrozenModel):
    event_type: Literal[
        "ThreadOpened",
        "ThreadUpdated",
        "ThreadResolved",
        "ThreadCancelled",
        "ThreadSuperseded",
        "ThreadCompensated",
    ]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> ThreadProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("thread mutation payload must be an object")
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise ValueError("thread mutation payload must use canonical JSON")
        return self


class ThreadProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["thread_transition"] = "thread_transition"
    proposal_encoding: Literal["typed-authority-v1"]
    authority_contract_ref: Literal["proposal-contract:thread.1"]
    transition_kind: Literal["open", "update", "resolve", "cancel", "supersede", "compensate"]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: ThreadProposedMutation

    @model_validator(mode="after")
    def transition_matches_event(self) -> ThreadProposalProjection:
        expected = {
            "open": "ThreadOpened",
            "update": "ThreadUpdated",
            "resolve": "ThreadResolved",
            "cancel": "ThreadCancelled",
            "supersede": "ThreadSuperseded",
            "compensate": "ThreadCompensated",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected:
            raise ValueError("thread proposal transition does not match event")
        return self


class PrincipalActionEvidence(FrozenModel):
    source_event_ref: str = Field(min_length=1)
    payload_hash: str = Field(min_length=64, max_length=64)
    authenticated_principal_ref: str = Field(min_length=1)
    action_ref: str = Field(min_length=1)
    scope_hash: str = Field(min_length=64, max_length=64)
    intent_hash: str = Field(min_length=64, max_length=64)
    challenge_ref: str = Field(min_length=16)
    observed_at: datetime
    expires_at: datetime
    authentication_policy_version: str = Field(min_length=1)
    authentication_policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def evidence_window_is_bounded(self) -> PrincipalActionEvidence:
        if self.observed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("principal action evidence time must be timezone-aware")
        if self.expires_at <= self.observed_at:
            raise ValueError("principal action evidence expiry must follow observation")
        return self


class AuthorizationOrigin(FrozenModel):
    transition_id: str = Field(min_length=1)
    event_ref: str = Field(min_length=1)
    authority_id: str = Field(min_length=1)
    authority_revision: int = Field(ge=1)
    attested_principal_ref: str = Field(min_length=1)
    attestation_mode: Literal["root_attested_external_principal_action.1"]
    attestation_environment: Literal["shadow"]
    root_attestation_verified: Literal[True] = True
    external_action_asserted: Literal[True] = True
    principal_possession_status: Literal["not_evaluated"] = "not_evaluated"
    enforcement_eligible: Literal[False] = False
    evidence_hash: str = Field(min_length=64, max_length=64)
    root_key_id: str = Field(min_length=1)
    root_keyset_digest: str = Field(min_length=64, max_length=64)
    root_nonce_hash: str = Field(min_length=64, max_length=64)
    root_proof_hash: str = Field(min_length=64, max_length=64)


class CapabilityGrantValues(FrozenModel):
    capability_kind: Literal["message_send", "media_send", "reaction_send", "read_only_tool"]
    actor_ref: str = Field(min_length=1)
    target_scope_refs: tuple[
        Literal[
            "channel:qq",
            "channel:wechat",
            "channel:http",
            "tool:weather",
            "tool:web_search",
            "tool:calendar_read",
        ],
        ...,
    ] = Field(min_length=1)
    constraint_refs: tuple[
        Literal["constraint:text-only", "constraint:read-only", "constraint:no-third-party"],
        ...,
    ] = ()
    valid_from: datetime
    expires_at: datetime | None = None
    state: Literal["active", "revoked", "expired"]


class CapabilityStateProjection(FrozenModel):
    grant_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    values: CapabilityGrantValues
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    origin: AuthorizationOrigin
    updated_at: datetime


class CapabilityTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    grant_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal["grant", "revise", "revoke", "compensate"]
    values_before: CapabilityGrantValues | None = None
    values_after: CapabilityGrantValues
    origin: AuthorizationOrigin
    changed_at: datetime
    compensates_transition_id: str | None = None


class ActorAuthorityValues(FrozenModel):
    principal_ref: str = Field(min_length=1)
    principal_kind: Literal["deployment_operator", "user_consent_principal", "service_operator"]
    credential_ref: str = Field(min_length=1)
    allowed_operations: tuple[
        Literal[
            "capability_grant",
            "consent_grant",
            "privacy_policy",
            "actor_authority_rotation",
        ],
        ...,
    ] = Field(min_length=1)
    valid_from: datetime
    expires_at: datetime | None = None
    status: Literal["active", "revoked"]

    @model_validator(mode="after")
    def validity_and_operations_are_canonical(self) -> ActorAuthorityValues:
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() is None:
            raise ValueError("actor authority validity start must be timezone-aware")
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("actor authority expiry must be timezone-aware")
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("actor authority expiry must follow validity start")
        if tuple(sorted(self.allowed_operations)) != self.allowed_operations:
            raise ValueError("actor authority operations must be sorted")
        if len(self.allowed_operations) != len(set(self.allowed_operations)):
            raise ValueError("actor authority operations must be unique")
        return self


class ActorAuthorityOrigin(FrozenModel):
    transition_id: str = Field(min_length=1)
    event_ref: str = Field(min_length=1)
    root_key_id: str = Field(min_length=1)
    root_keyset_version: str = Field(min_length=1)
    root_keyset_digest: str = Field(min_length=64, max_length=64)
    root_nonce_hash: str = Field(min_length=64, max_length=64)
    root_proof_hash: str = Field(min_length=64, max_length=64)


class ActorAuthorityProjection(FrozenModel):
    authority_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    values: ActorAuthorityValues
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    origin: ActorAuthorityOrigin
    updated_at: datetime


class ActorAuthorityTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    authority_id: str = Field(min_length=1)
    authority_revision: int = Field(ge=1)
    operation: Literal["bootstrap", "rotate", "revoke", "compensate"]
    values_before: ActorAuthorityValues | None = None
    values_after: ActorAuthorityValues
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    root_key_id: str = Field(min_length=1)
    root_keyset_version: str = Field(min_length=1)
    root_keyset_digest: str = Field(min_length=64, max_length=64)
    root_nonce_hash: str = Field(min_length=64, max_length=64)
    root_proof_hash: str = Field(min_length=64, max_length=64)
    changed_at: datetime
    compensates_transition_id: str | None = None


class ConsentGrantValues(FrozenModel):
    grantor_ref: str = Field(min_length=1)
    grantee_ref: str = Field(min_length=1)
    action_scope_refs: tuple[
        Literal["message_send", "media_send", "reaction_send", "read_only_tool"], ...
    ] = Field(min_length=1)
    data_scope_refs: tuple[
        Literal["data:message_content", "data:user_profile", "data:attachment", "data:location"],
        ...,
    ] = ()
    channel_scope_refs: tuple[
        Literal["channel:qq", "channel:wechat", "channel:http"], ...
    ] = ()
    valid_from: datetime
    expires_at: datetime | None = None
    revocable: bool
    status: Literal["active", "revoked", "expired"]


class ConsentStateProjection(FrozenModel):
    consent_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    values: ConsentGrantValues
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    origin: AuthorizationOrigin
    updated_at: datetime


class ConsentTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    consent_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal["grant", "revise", "revoke", "compensate"]
    values_before: ConsentGrantValues | None = None
    values_after: ConsentGrantValues
    origin: AuthorizationOrigin
    changed_at: datetime
    compensates_transition_id: str | None = None


class NamedPolicyRef(FrozenModel):
    name: str = Field(min_length=1)
    value_ref: str = Field(min_length=1)


class PrivacyPolicyValues(FrozenModel):
    subject_ref: str = Field(min_length=1)
    data_class_refs: tuple[
        Literal["data:message_content", "data:user_profile", "data:attachment", "data:location"],
        ...,
    ] = ()
    viewer_rule_refs: tuple[
        Literal["viewer:companion", "viewer:operator", "viewer:room_renderer", "viewer:platform_adapter"],
        ...,
    ] = ()
    media_rule_refs: tuple[
        Literal["media:private_only", "media:share_allowed", "media:auto_delivery_allowed"],
        ...,
    ] = ()
    retention_rule_refs: tuple[
        Literal["retention:session", "retention:30d", "retention:persistent"], ...
    ] = ()
    effective_at: datetime
    expires_at: datetime | None = None
    status: Literal["active", "revoked", "expired"]


class PrivacyPolicyProjection(FrozenModel):
    policy_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    values: PrivacyPolicyValues
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    origin: AuthorizationOrigin
    updated_at: datetime


class PrivacyTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal["revise", "revoke", "compensate"]
    values_before: PrivacyPolicyValues | None = None
    values_after: PrivacyPolicyValues
    origin: AuthorizationOrigin
    changed_at: datetime
    compensates_transition_id: str | None = None


class MediaCandidateProjection(FrozenModel):
    candidate_id: str = Field(min_length=1)
    experience_ref: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    privacy_class: PrivacyClass
    status: Literal["candidate", "selected", "dismissed", "expired"]


class VersionRef(FrozenModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ActionAuthorityPage(FrozenModel):
    world_id: str = Field(min_length=1)
    cursor: ProjectionCursor
    actions: tuple[Action, ...]
    next_after_action_id: str | None = None
    complete: bool

    @model_validator(mode="after")
    def continuation_matches_completeness(self) -> ActionAuthorityPage:
        if self.complete and self.next_after_action_id is not None:
            raise ValueError("complete authority page cannot have a continuation")
        if not self.complete and self.next_after_action_id is None:
            raise ValueError("incomplete authority page requires a continuation")
        return self


class InternalWorldSnapshot(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    snapshot_id: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    world_id: str = Field(min_length=1)
    cursor: ProjectionCursor
    semantic_hash: str = Field(min_length=64, max_length=64)
    logical_time: datetime | None = None
    updated_at: datetime
    snapshot_purpose: Literal["situation_context"] = "situation_context"
    projection_policy_version: str = Field(min_length=1)
    character_core: CharacterCoreProjection | None = None
    facts: tuple[FactProjection, ...] = ()
    experiences: tuple[ExperienceProjection, ...] = ()
    appraisals: tuple[AppraisalProjection, ...] = ()
    npcs: tuple[NpcProjection, ...] = ()
    world_occurrences: tuple[WorldOccurrenceProjection, ...] = ()
    outcome_observations: tuple[OutcomeObservationProjection, ...] = ()
    current_situation: SituationStateProjection | None = None
    plans: tuple[PlanStateProjection, ...] = ()
    commitments: tuple[CommitmentStateProjection, ...] = ()
    affect_episodes: tuple[AffectEpisodeProjection, ...] = ()
    affect_baselines: tuple[AffectBaselineProjection, ...] = ()
    affect_aggregates: tuple[AffectAggregateProjection, ...] = ()
    private_impressions: tuple[PrivateImpressionProjection, ...] = ()
    relationship_state: RelationshipStateProjection | None = None
    relationship_boundaries: tuple[BoundaryProjection, ...] = ()
    conversation_threads: tuple[ConversationThreadProjection, ...] = ()
    capabilities: tuple[CapabilityStateProjection, ...] = ()
    consents: tuple[ConsentStateProjection, ...] = ()
    privacy_policy: PrivacyPolicyProjection | None = None
    pending_actions: tuple[Action, ...] = ()
    budget_accounts: tuple[BudgetAccount, ...] = ()
    budget_reservations: tuple[BudgetReservation, ...] = ()
    pending_external_observations: tuple[ExternalObservation, ...] = ()
    media_candidates: tuple[MediaCandidateProjection, ...] = ()
    reducer_versions: tuple[VersionRef, ...]
    slice_windows: tuple[ProjectionSliceWindow, ...] = ()
    system_health: ProjectionSystemHealth = Field(default_factory=ProjectionSystemHealth)

    @computed_field
    @property
    def world_revision(self) -> int:
        return self.cursor.world_revision

    @computed_field
    @property
    def deliberation_revision(self) -> int:
        return self.cursor.deliberation_revision

    @computed_field
    @property
    def ledger_sequence(self) -> int:
        return self.cursor.ledger_sequence


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
    reducer_bundle_version: str = "world-v2-reducers.10"
    world_id: str
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    logical_time: datetime | None = None
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
    experiences: tuple[ExperienceProjection, ...] = ()
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
    proposal_ids: tuple[str, ...] = ()
    proposal_revisions: tuple[ProposalRevisionRef, ...] = ()
    acceptance_decisions: tuple[AcceptanceDecisionRef, ...] = ()
    outcome_proposals: tuple[OutcomeProposalProjection, ...] = ()
    semantic_hash: str

    @model_validator(mode="after")
    def pending_index_matches_actions(self) -> LedgerProjection:
        terminal = {"delivered", "failed", "unknown", "cancelled", "expired"}
        expected = tuple(action for action in self.actions if action.state not in terminal)
        if self.pending_actions != expected:
            raise ValueError("pending_actions must equal the non-terminal action index")
        dimensions = tuple(item.dimension for item in self.affect_baselines)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("affect baseline dimensions must be unique")
        return self
