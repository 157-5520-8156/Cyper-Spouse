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
    viewer_kind: Literal[
        "platform_adapter", "dashboard_operator", "room_renderer", "evaluator"
    ]
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
        if (self.capability_issued_at is None) != (
            self.capability_expires_at is None
        ):
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
    PlatformProjectionView
    | OperatorProjectionView
    | RoomProjectionView
    | EvaluatorProjectionView
)


class WorldProjection(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    world_id: str
    world_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    viewer_kind: Literal[
        "platform_adapter", "dashboard_operator", "room_renderer", "evaluator"
    ]
    redaction_policy: str = Field(min_length=1)
    projection_policy_version: str = "world-v2-projection-policy.1"
    reducer_bundle_version: str = Field(min_length=1)
    projection_hash: str = Field(min_length=64, max_length=64)
    semantic_hash: str | None = Field(default=None, min_length=64, max_length=64)
    logical_time: datetime | None = None
    view: ViewerProjection = Field(discriminator="view_kind")


PrivacyClass = Literal["public", "shareable", "personal", "private", "withhold"]


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
    summary_ref: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    occurred_at: datetime
    participant_refs: tuple[str, ...] = ()
    privacy_class: PrivacyClass
    status: Literal["committed", "superseded"] = "committed"


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
    goal_ref: str | None = None
    source_refs: tuple[str, ...] = Field(min_length=1)
    status: Literal["planned", "active", "paused", "completed", "cancelled"]
    importance_bp: int = Field(ge=0, le=10_000)
    due_window: tuple[datetime, datetime] | None = None
    privacy_class: PrivacyClass = "private"


class CommitmentStateProjection(FrozenModel):
    commitment_id: str = Field(min_length=1)
    content_ref: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    importance_bp: int = Field(ge=0, le=10_000)
    persistence_level: Literal["ephemeral", "turn", "session", "durable"]
    due_window: tuple[datetime, datetime] | None = None
    status: Literal["open", "fulfilled", "broken", "released"]
    privacy_class: PrivacyClass = "private"


class AffectDecayProfileProjection(FrozenModel):
    kind: str = Field(min_length=1)
    half_life_seconds: int = Field(gt=0)
    floor_bp: int = Field(ge=0, le=10_000)
    delay_seconds: int = Field(default=0, ge=0)
    config_version: str = Field(min_length=1)


class AffectComponentProjection(FrozenModel):
    dimension: Literal[
        "hurt", "anger", "sadness", "loneliness", "anxiety", "resentment", "warmth", "joy"
    ]
    intensity_bp: int = Field(ge=0, le=10_000)
    baseline_bp: int = Field(ge=0, le=10_000)
    source_cluster: str = Field(min_length=1)
    opened_at: datetime
    last_updated_at: datetime
    decay_profile: AffectDecayProfileProjection
    residue_bp: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def component_time_moves_forward(self) -> AffectComponentProjection:
        if self.last_updated_at < self.opened_at:
            raise ValueError("affect component update precedes opening")
        return self


class AffectEpisodeProjection(FrozenModel):
    episode_id: str = Field(min_length=1)
    components: tuple[AffectComponentProjection, ...] = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    appraisal_refs: tuple[str, ...] = ()
    opened_at: datetime
    updated_at: datetime
    status: Literal["active", "resolved", "superseded"]
    privacy_class: PrivacyClass = "private"
    expression_history_refs: tuple[str, ...] = ()


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


class RelationshipStateProjection(FrozenModel):
    subject_ref: str = Field(min_length=1)
    stage: Literal[
        "stranger", "acquaintance", "friend", "close_friend", "ambiguous", "lover"
    ] = "stranger"
    variables: RelationshipVariablesProjection = Field(
        default_factory=RelationshipVariablesProjection
    )
    temperature: str = "ordinary"
    boundary_refs: tuple[str, ...] = ()
    policy_revision: int = Field(default=0, ge=0)
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


class CapabilityStateProjection(FrozenModel):
    grant_id: str = Field(min_length=1)
    capability_kind: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    target_scope_refs: tuple[str, ...] = Field(min_length=1)
    constraint_refs: tuple[str, ...] = ()
    valid_from: datetime
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    state: Literal["active", "revoked", "expired"]
    policy_revision: int = Field(ge=0)
    expires_at: datetime | None = None
    revoked_by_ref: str | None = None


class ConsentStateProjection(FrozenModel):
    consent_id: str = Field(min_length=1)
    grantor_ref: str = Field(min_length=1)
    grantee_ref: str = Field(min_length=1)
    action_scope_refs: tuple[str, ...] = Field(min_length=1)
    data_scope_refs: tuple[str, ...] = ()
    channel_scope_refs: tuple[str, ...] = ()
    valid_from: datetime
    expires_at: datetime | None = None
    revocable: bool
    status: Literal["active", "revoked", "expired"]
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class NamedPolicyRef(FrozenModel):
    name: str = Field(min_length=1)
    value_ref: str = Field(min_length=1)


class PrivacyPolicyProjection(FrozenModel):
    policy_revision: int = Field(ge=0)
    subject_ref: str = Field(min_length=1)
    data_classes: tuple[NamedPolicyRef, ...] = ()
    viewer_rules: tuple[NamedPolicyRef, ...] = ()
    media_rules: tuple[NamedPolicyRef, ...] = ()
    retention_rules: tuple[NamedPolicyRef, ...] = ()
    effective_at: datetime
    evidence_refs: tuple[str, ...] = Field(min_length=1)


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
    current_situation: SituationStateProjection | None = None
    plans: tuple[PlanStateProjection, ...] = ()
    commitments: tuple[CommitmentStateProjection, ...] = ()
    affect_episodes: tuple[AffectEpisodeProjection, ...] = ()
    private_impressions: tuple[PrivateImpressionProjection, ...] = ()
    relationship_state: RelationshipStateProjection | None = None
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
    reducer_bundle_version: str = "world-v2-reducers.2"
    world_id: str
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    logical_time: datetime | None = None
    observation_refs: tuple[str, ...] = ()
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
    semantic_hash: str

    @model_validator(mode="after")
    def pending_index_matches_actions(self) -> LedgerProjection:
        terminal = {"delivered", "failed", "unknown", "cancelled", "expired"}
        expected = tuple(
            action for action in self.actions if action.state not in terminal
        )
        if self.pending_actions != expected:
            raise ValueError("pending_actions must equal the non-terminal action index")
        return self
