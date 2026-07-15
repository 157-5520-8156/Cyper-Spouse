from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Any, Literal

from pydantic import (
    Field,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)

from .attention_authority_schemas import (
    V2AttentionProjection,
    V2AttentionProposalProjection,
    V2AttentionTransitionProjection,
    validate_v2_attention_authority_state,
)

from .goal_situation_schemas import (
    V2GoalProjection,
    V2GoalProposalProjection,
    V2GoalTransitionProjection,
    validate_v2_goal_authority_state,
)
from .location_authority_schemas import (
    V2LocationProjection,
    V2LocationProposalProjection,
    V2LocationTransitionProjection,
    validate_v2_location_authority_state,
)
from .resource_authority_schemas import (
    V2ResourceProjection,
    V2ResourceProposalProjection,
    V2ResourceTransitionProjection,
    validate_v2_resource_authority_state,
)
from .proposal_audit_schemas import ModelResultAuditProjection, ProposalAuditProjection
from .acceptance_manifest import AcceptanceManifestRefV2
from .schema_core import EvidenceRef, FrozenModel, PrivacyClass
from .media_v2 import (
    MediaArtifact,
    MediaAutomaticDeliveryApproval,
    MediaDeliveryShared,
    MediaInspectionRecord,
    MediaOpportunity,
    MediaPlan,
    MediaPreview,
    PhotoCandidate,
)


SchemaVersion = Literal["world-v2.1"]
_LEGACY_WITHOUT_SETTLED_OUTCOME = frozenset(
    f"world-v2-reducers.{version}" for version in (1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
)
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
    # ``payload_ref`` proves where the ingress payload lives, but cannot make
    # a local Advisory understand a user's disappointment, sarcasm or repair
    # attempt.  This bounded, auditable copy is optional for compatibility;
    # when supplied it is part of the immutable Observation event payload.
    text: str | None = Field(default=None, min_length=1, max_length=12_000)
    received_at: datetime
    reply_context: dict[str, Any] | None = None
    attachment_refs: tuple[str, ...] = ()
    coalescing_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def idempotency_identity(self) -> tuple[str, str]:
        return (self.provider, self.provider_ref or self.idempotency_key)


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
    policy_version: str | None = Field(default=None, min_length=1)
    policy_digest: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def time_moves_forward(self) -> ClockObservation:
        if self.logical_time_to <= self.logical_time_from:
            raise ValueError("logical time must move forwards")
        if (self.policy_version is None) != (self.policy_digest is None):
            raise ValueError("clock policy version and digest must be supplied together")
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


class ProviderReceipt(FrozenModel):
    """Raw provider result, before Runtime turns it into ledger input."""

    provider_receipt_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_ref: str = Field(min_length=1)
    status: Literal["provider_accepted", "delivered", "failed", "unknown"]
    artifact_refs: tuple[str, ...] = ()
    cost_actual: int = Field(ge=0)
    error_class: str | None = Field(default=None, min_length=1)
    received_at: datetime
    raw_payload_hash: str = Field(min_length=1)


class DispatchPending(FrozenModel):
    """Provider accepted the hand-off but has no terminal receipt yet."""

    action_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_ref: str | None = Field(default=None, min_length=1)
    lookup_after: datetime
    deadline: datetime
    dispatch_started_at: datetime
    idempotency_mode: Literal["effect_once", "result_lookup", "none"]

    @model_validator(mode="after")
    def deadline_is_not_before_lookup(self) -> DispatchPending:
        if self.deadline < self.lookup_after or self.lookup_after < self.dispatch_started_at:
            raise ValueError("dispatch pending time window is invalid")
        return self

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


class ProviderMediaGrantBinding(FrozenModel):
    """Exact, ledger-backed authority consumed by one provider media Action.

    This is deliberately a reference to a *revision*, rather than a mutable
    provider configuration name.  The Action remains immutable while the
    pump re-checks that the referenced grant and all of its source authority
    are still active immediately before a provider call.
    """

    grant_id: str = Field(min_length=1)
    grant_revision: int = Field(ge=1)


class MediaDeliveryApprovalBinding(FrozenModel):
    """Immutable Action reference to one operator delivery-approval revision."""

    approval_id: str = Field(min_length=1)
    approval_revision: int = Field(ge=1)


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
    # A reply/expression Action is not merely text-shaped: it is the sole
    # external effect for a particular durable beat.  Both fields stay
    # optional for pre-lifecycle records, but must travel as a pair for all
    # new expression work.
    expression_plan_id: str | None = Field(default=None, min_length=1)
    expression_beat_id: str | None = Field(default=None, min_length=1)
    provider_media_grant: ProviderMediaGrantBinding | None = None
    media_delivery_approval: MediaDeliveryApprovalBinding | None = None
    idempotency_key: str = Field(min_length=1)
    not_before: datetime | None = None
    expires_at: datetime | None = None
    dependencies: tuple[str, ...] = ()
    budget_reservation_id: str = Field(min_length=1)
    claim_lease: ClaimLease | None = None
    dispatch_pending: DispatchPending | None = None
    state: ActionState
    recovery_policy: str = Field(min_length=1)

    @model_validator(mode="after")
    def claimed_action_has_a_lease(self) -> Action:
        if (self.expression_plan_id is None) != (self.expression_beat_id is None):
            raise ValueError("expression Action must bind both plan and beat")
        provider_media_kinds = {
            "media_planning",
            "media_render",
            "media_inspection",
            "media_repair",
        }
        if self.kind in provider_media_kinds:
            if self.layer != "media_action" or self.provider_media_grant is None:
                raise ValueError("provider media Action requires an exact provider media grant")
        elif self.provider_media_grant is not None:
            raise ValueError("only provider media Actions may carry a provider media grant")
        if self.kind == "media_delivery":
            if self.layer != "external_action" or self.media_delivery_approval is None:
                raise ValueError("media delivery Action requires an exact operator approval")
        elif self.media_delivery_approval is not None:
            raise ValueError("only media delivery Actions may carry a delivery approval")
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
        if self.dispatch_pending is not None:
            if self.state != "dispatch_started":
                raise ValueError("only a dispatch-started action may retain pending provider state")
            if (
                self.dispatch_pending.action_id != self.action_id
                or self.dispatch_pending.idempotency_key != self.idempotency_key
                or self.dispatch_pending.idempotency_mode != self.recovery_policy
            ):
                raise ValueError("pending provider state does not bind its Action")
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
        "interaction_fact",
        "affect_deliberation",
        "outcome_deliberation",
        "expression_reconsideration",
        "media_continuation",
        "media_repair",
        "media_delivery_interaction",
    ]
    source_evidence_ref: str | None = None
    state: Literal["open", "claimed", "terminal"]
    claim_lease: ClaimLease | None = None
    attempt_ids: tuple[str, ...] = ()
    runtime_outcome_ref: str | None = None

    @model_validator(mode="after")
    def active_attempt_matches_lease(self) -> TriggerProcess:
        if (
            self.process_kind
            not in {
                "npc_world_appraisal",
                "interaction_appraisal",
                "interaction_fact",
                "affect_deliberation",
                "outcome_deliberation",
                "expression_reconsideration",
                "media_continuation",
                "media_repair",
                "media_delivery_interaction",
            }
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


class CommittedWorldEventRef(FrozenModel):
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    world_revision: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)
    logical_time: datetime
    continuation_refs: tuple[str, ...] = ()


class ClockTransitionProjection(FrozenModel):
    clock_event_ref: str = Field(min_length=1)
    computed_world_revision: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)
    logical_time_from: datetime
    logical_time_to: datetime
    installed_policy_version: str = Field(min_length=1)
    installed_policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def interval_is_forward(self) -> ClockTransitionProjection:
        if self.logical_time_to <= self.logical_time_from:
            raise ValueError("clock transition interval must move forward")
        return self


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
    # Optional only so persisted <= .11 reducer states remain decodable during
    # verified migration. New observations always retain the full envelope.
    actor: str | None = None
    channel: str | None = None
    payload_ref: str | None = None


class AcceptanceDecisionRef(FrozenModel):
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    acceptance_id: str | None = None
    status: Literal["accepted", "rejected", "stale"]
    accepted_change_id: str | None = None
    accepted_change_hash: str | None = Field(default=None, min_length=64, max_length=64)
    manifest_version: (
        Literal[
            "acceptance-manifest.2",
            "acceptance-manifest.3",
            "appraisal-acceptance.1",
            "affect-acceptance.1",
            "outcome-acceptance.1",
            "interaction-bid-acceptance.1",
            "media-delivery-thread-acceptance.1",
        ]
        | None
    ) = None
    manifest_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    acceptance_event_ref: str | None = None
    acceptance_event_payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

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
        manifest_fields = (
            self.manifest_hash,
            self.acceptance_event_ref,
            self.acceptance_event_payload_hash,
        )
        if self.manifest_version is None and any(value is not None for value in manifest_fields):
            raise ValueError("legacy decision cannot carry partial manifest audit")
        if self.manifest_version is not None and any(value is None for value in manifest_fields):
            raise ValueError("manifest-backed decision requires complete manifest audit")
        return self


class MinimalReplyManifestRef(FrozenModel):
    """Durable authority record for one isolated ordinary-reply acceptance."""

    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expression_change_id: str = Field(min_length=1)
    expression_change_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    intent_id: str = Field(min_length=1)
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    beat_id: str = Field(min_length=1)
    message_payload_ref: str = Field(min_length=1)
    message_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    beat_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation_id: str = Field(min_length=1)
    reservation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_id: str = Field(min_length=1)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_event_ref: str = Field(min_length=1)
    acceptance_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at_world_revision: int = Field(ge=1)


class ExpressionPlanManifestBeatRef(FrozenModel):
    """Projection-safe copy of one immutable accepted multi-beat triple."""

    beat_id: str = Field(min_length=1)
    payload_ref: str = Field(min_length=1)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    text: str | None = Field(default=None, min_length=1, max_length=4_096)
    content_type: str = Field(min_length=1, max_length=128)
    storage_kind: Literal["inline_text", "sidecar"] = "inline_text"
    sidecar_kind: Literal["referenced", "inline_encrypted"] | None = None
    privacy_class: PrivacyClass = "private"
    dependency_beat_ids: tuple[str, ...] = ()
    not_before: datetime | None = None
    expires_at: datetime | None = None
    cancel_policy: str = Field(min_length=1, max_length=128)
    reconsider_policy: str = Field(min_length=1, max_length=128)
    merge_policy: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1)
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    message_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    beat_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation: BudgetReservation
    reservation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Action
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExpressionPlanManifestRef(FrozenModel):
    """Durable authority for one generic accepted expression plan."""

    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expression_change_id: str = Field(min_length=1)
    expression_change_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    ordering_policy: str = Field(min_length=1, max_length=128)
    terminal_policy: str = Field(min_length=1, max_length=128)
    beats: tuple[ExpressionPlanManifestBeatRef, ...] = Field(min_length=1, max_length=32)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_event_ref: str = Field(min_length=1)
    acceptance_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at_world_revision: int = Field(ge=1)


class StoredMessagePayloadProjection(FrozenModel):
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    payload_ref: str = Field(min_length=1)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=4_096)
    content_type: str = Field(min_length=1, max_length=128)
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExpressionPayloadDescriptorProjection(FrozenModel):
    """Ledger authority permitting an opaque expression sidecar read."""

    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    payload_ref: str = Field(min_length=1)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=128)
    privacy_class: PrivacyClass
    payload_kind: Literal["referenced", "inline_encrypted"]
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class LifeContentDescriptorProjection(FrozenModel):
    """Ledger-visible authority that permits a sidecar text read.

    The descriptor is deliberately separate from both the text store and the
    lived-world source.  Its event cursor makes historical Context replay
    deterministic even when the append-only sidecar already contains bytes.
    """

    content_id: str = Field(min_length=1)
    content_kind: Literal["occurrence_result", "experience_summary"]
    content_ref: str = Field(min_length=1)
    content_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    privacy_class: PrivacyClass
    source_kind: Literal["occurrence_settlement", "experience"]
    source_event_ref: str = Field(min_length=1)
    source_world_revision: int = Field(ge=1)
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_entity_id: str = Field(min_length=1)
    source_entity_revision: int = Field(ge=1)
    descriptor_event_ref: str = Field(min_length=1)
    descriptor_world_revision: int = Field(ge=1)
    descriptor_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExpressionPlanProjection(FrozenModel):
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    expression_change_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["authorized", "completed"] = "authorized"
    history: tuple["ExpressionPlanLifecycleEntry", ...] = ()


class ExpressionPlanLifecycleEntry(FrozenModel):
    state: Literal["authorized", "completed"]
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_id: str | None = Field(default=None, min_length=1)
    terminal_action_state: (
        Literal["delivered", "failed", "unknown", "cancelled", "expired"] | None
    ) = None

    @model_validator(mode="after")
    def completed_entry_has_terminal_receipt(self) -> "ExpressionPlanLifecycleEntry":
        if self.state == "completed" and (
            self.receipt_id is None or self.terminal_action_state is None
        ):
            raise ValueError("completed expression plan history requires terminal receipt")
        if self.state == "authorized" and (
            self.receipt_id is not None or self.terminal_action_state is not None
        ):
            raise ValueError("authorized expression plan history cannot carry a receipt")
        return self


class ExpressionBeatProjection(FrozenModel):
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    expression_change_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    beat_id: str = Field(min_length=1)
    payload_ref: str = Field(min_length=1)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action_id: str | None = Field(default=None, min_length=1)
    dependency_beat_ids: tuple[str, ...] = ()
    not_before: datetime | None = None
    expires_at: datetime | None = None
    cancel_policy: str = Field(min_length=1, max_length=128)
    reconsider_policy: str = Field(min_length=1, max_length=128)
    merge_policy: str = Field(min_length=1, max_length=128)
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["authorized", "settled"] = "authorized"
    history: tuple["ExpressionBeatLifecycleEntry", ...] = ()


class ExpressionBeatLifecycleEntry(FrozenModel):
    state: Literal["authorized", "settled"]
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_id: str | None = Field(default=None, min_length=1)
    terminal_action_state: (
        Literal["delivered", "failed", "unknown", "cancelled", "expired"] | None
    ) = None

    @model_validator(mode="after")
    def settled_entry_has_terminal_receipt(self) -> "ExpressionBeatLifecycleEntry":
        if self.state == "settled" and (
            self.receipt_id is None or self.terminal_action_state is None
        ):
            raise ValueError("settled expression beat history requires terminal receipt")
        if self.state == "authorized" and (
            self.receipt_id is not None or self.terminal_action_state is not None
        ):
            raise ValueError("authorized expression beat history cannot carry a receipt")
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


class CharacterCoreImmutableIdentity(FrozenModel):
    canonical_identity_refs: tuple[str, ...] = Field(min_length=1)
    continuity_anchor_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def refs_are_unique(self) -> CharacterCoreImmutableIdentity:
        refs = (*self.canonical_identity_refs, *self.continuity_anchor_refs)
        if len(refs) != len(set(refs)):
            raise ValueError("character core identity refs must be unique")
        return self


class CharacterCoreOperatorGoverned(FrozenModel):
    role_refs: tuple[str, ...] = ()
    non_negotiable_value_refs: tuple[str, ...] = ()
    hard_boundary_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def refs_are_canonical(self) -> CharacterCoreOperatorGoverned:
        for refs in (
            self.role_refs,
            self.non_negotiable_value_refs,
            self.hard_boundary_refs,
        ):
            if tuple(sorted(refs)) != refs or len(refs) != len(set(refs)):
                raise ValueError("character core operator refs must be sorted and unique")
        return self


CHARACTER_CORE_COORDINATE_CATALOG_VERSION = "character-core-coordinate-catalog.1"
CHARACTER_CORE_TRAIT_AXES = (
    "agreeableness",
    "assertiveness",
    "autonomy",
    "conscientiousness",
    "curiosity",
    "emotional_stability",
    "extraversion",
    "openness",
    "warmth",
)
CHARACTER_CORE_VALUE_REFS = (
    "value:autonomy",
    "value:care",
    "value:growth",
    "value:honesty",
    "value:privacy",
    "value:reciprocity",
)
CHARACTER_CORE_PREFERENCE_REFS = (
    "preference:direct_communication",
    "preference:independent_time",
    "preference:playful_banter",
    "preference:quiet_reflection",
    "preference:shared_routines",
)
CHARACTER_CORE_COORDINATE_CATALOG_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "version": CHARACTER_CORE_COORDINATE_CATALOG_VERSION,
            "trait_axes": CHARACTER_CORE_TRAIT_AXES,
            "value_refs": CHARACTER_CORE_VALUE_REFS,
            "preference_refs": CHARACTER_CORE_PREFERENCE_REFS,
            "excluded_short_term": (
                "mood",
                "anxiety",
                "anger",
                "sadness",
                "current_energy",
                "relationship_stage",
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


class CharacterCoreAxis(FrozenModel):
    axis_code: Literal[
        "agreeableness",
        "assertiveness",
        "autonomy",
        "conscientiousness",
        "curiosity",
        "emotional_stability",
        "extraversion",
        "openness",
        "warmth",
    ]
    value_bp: int = Field(ge=0, le=10_000)


class CharacterCoreValuePriority(FrozenModel):
    value_ref: Literal[
        "value:autonomy",
        "value:care",
        "value:growth",
        "value:honesty",
        "value:privacy",
        "value:reciprocity",
    ]
    priority_bp: int = Field(ge=0, le=10_000)


class CharacterCoreSlowEvolving(FrozenModel):
    coordinate_catalog_version: Literal["character-core-coordinate-catalog.1"]
    coordinate_catalog_digest: str = Field(min_length=64, max_length=64)
    trait_axes: tuple[CharacterCoreAxis, ...] = ()
    value_priorities: tuple[CharacterCoreValuePriority, ...] = ()
    preference_refs: tuple[
        Literal[
            "preference:direct_communication",
            "preference:independent_time",
            "preference:playful_banter",
            "preference:quiet_reflection",
            "preference:shared_routines",
        ],
        ...,
    ] = ()
    autonomy_style: Literal["dependent", "collaborative", "self_directed"]
    attachment_tendency: Literal["guarded", "balanced", "connection_seeking"]
    conflict_style: Literal["avoidant", "deliberative", "direct"]
    privacy_tendency: Literal["open", "selective", "reserved"]

    @model_validator(mode="after")
    def coordinates_are_canonical(self) -> CharacterCoreSlowEvolving:
        if self.coordinate_catalog_digest != CHARACTER_CORE_COORDINATE_CATALOG_DIGEST:
            raise ValueError("character core coordinate catalog is not installed")
        axes = tuple(item.axis_code for item in self.trait_axes)
        priorities = tuple(item.value_ref for item in self.value_priorities)
        if axes != tuple(sorted(axes)) or len(axes) != len(set(axes)):
            raise ValueError("character trait axes must be sorted and unique")
        if priorities != tuple(sorted(priorities)) or len(priorities) != len(set(priorities)):
            raise ValueError("character value priorities must be sorted and unique")
        if self.preference_refs != tuple(sorted(self.preference_refs)) or len(
            self.preference_refs
        ) != len(set(self.preference_refs)):
            raise ValueError("character preferences must be sorted and unique")
        return self


CharacterCoreEvidenceSourceKind = Literal["fact", "experience"]
CharacterCoreEvidencePolarity = Literal["supporting", "contradicting"]


class CharacterCoreEvidenceBinding(FrozenModel):
    source_kind: CharacterCoreEvidenceSourceKind
    source_id: str = Field(min_length=1)
    source_entity_revision: int = Field(ge=1)
    authority_event_ref: str = Field(min_length=1)
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(min_length=64, max_length=64)
    source_values_hash: str = Field(min_length=64, max_length=64)
    polarity: CharacterCoreEvidencePolarity
    scene_ref: str = Field(min_length=1)
    trigger_kind: str = Field(min_length=1)
    observed_from: datetime
    observed_to: datetime

    @field_validator("observed_from", "observed_to")
    @classmethod
    def observation_times_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("character evidence observation time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def window_is_ordered(self) -> CharacterCoreEvidenceBinding:
        if self.observed_to < self.observed_from:
            raise ValueError("character evidence source window is reversed")
        return self


def character_core_evidence_authority_id(binding: CharacterCoreEvidenceBinding) -> str:
    material = {
        "source_kind": binding.source_kind,
        "source_id": binding.source_id,
        "source_entity_revision": binding.source_entity_revision,
        "authority_event_ref": binding.authority_event_ref,
        "authority_world_revision": binding.authority_world_revision,
        "authority_payload_hash": binding.authority_payload_hash,
        "source_values_hash": binding.source_values_hash,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CharacterCoreEvidenceWindow(FrozenModel):
    opens_at: datetime
    closes_at: datetime
    source_bindings: tuple[CharacterCoreEvidenceBinding, ...] = Field(min_length=1)
    distinct_scene_refs: tuple[str, ...] = Field(min_length=1)
    distinct_trigger_kinds: tuple[str, ...] = Field(min_length=1)
    supporting_count: int = Field(ge=0)
    contradicting_count: int = Field(ge=0)
    privacy_floor: PrivacyClass
    policy_version: str = Field(min_length=1)
    evidence_digest: str = Field(min_length=64, max_length=64)

    @field_validator("opens_at", "closes_at")
    @classmethod
    def window_times_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("character evidence window time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def summary_is_derived(self) -> CharacterCoreEvidenceWindow:
        if self.closes_at < self.opens_at:
            raise ValueError("character evidence window is reversed")
        authority_ids = tuple(
            character_core_evidence_authority_id(item) for item in self.source_bindings
        )
        if len(authority_ids) != len(set(authority_ids)):
            raise ValueError("character evidence authority cannot be reused in one window")
        if self.opens_at != min(item.observed_from for item in self.source_bindings) or (
            self.closes_at != max(item.observed_to for item in self.source_bindings)
        ):
            raise ValueError("character evidence window bounds are not source-derived")
        scenes = tuple(sorted({item.scene_ref for item in self.source_bindings}))
        triggers = tuple(sorted({item.trigger_kind for item in self.source_bindings}))
        if self.distinct_scene_refs != scenes or self.distinct_trigger_kinds != triggers:
            raise ValueError("character evidence diversity summary is not source-derived")
        if self.supporting_count != sum(
            item.polarity == "supporting" for item in self.source_bindings
        ) or self.contradicting_count != sum(
            item.polarity == "contradicting" for item in self.source_bindings
        ):
            raise ValueError("character evidence polarity counts are not source-derived")
        material = {
            "policy_version": self.policy_version,
            "source_bindings": [item.model_dump(mode="json") for item in self.source_bindings],
        }
        expected = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.evidence_digest != expected:
            raise ValueError("character evidence digest does not match exact sources")
        return self


class CharacterCoreValues(FrozenModel):
    immutable_identity: CharacterCoreImmutableIdentity
    operator_governed: CharacterCoreOperatorGoverned
    slow_evolving: CharacterCoreSlowEvolving
    privacy_class: PrivacyClass


class CharacterCoreOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


def character_core_semantic_fingerprint(
    *, core_id: str, actor_ref: str, values: CharacterCoreValues, policy_refs: tuple[str, ...]
) -> str:
    material = {
        "core_id": core_id,
        "actor_ref": actor_ref,
        "values": values.model_dump(mode="json"),
        "policy_refs": sorted(policy_refs),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CharacterCoreProjection(FrozenModel):
    core_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: CharacterCoreValues
    origin: CharacterCoreOrigin
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def chronology_times_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("character core chronology must be timezone-aware")
        return value

    @model_validator(mode="after")
    def fingerprint_is_exact(self) -> CharacterCoreProjection:
        if self.updated_at < self.created_at:
            raise ValueError("character core update precedes initialization")
        if self.semantic_fingerprint != character_core_semantic_fingerprint(
            core_id=self.core_id,
            actor_ref=self.actor_ref,
            values=self.values,
            policy_refs=self.origin.policy_refs,
        ):
            raise ValueError("character core semantic fingerprint is invalid")
        return self


class CharacterCoreOperatorAuthorityBinding(FrozenModel):
    authority_id: str = Field(min_length=1)
    authority_revision: int = Field(ge=1)
    principal_ref: str = Field(min_length=1)
    authority_event_ref: str = Field(min_length=1)
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(min_length=64, max_length=64)
    authority_values_hash: str = Field(min_length=64, max_length=64)
    authority_policy_digest: str = Field(min_length=64, max_length=64)
    authorization_contract: Literal["deployment-actor-authority:character-core.1"]


CharacterCoreFieldClass = Literal[
    "immutable_identity", "operator_governed", "privacy_class", "slow_evolving"
]
CharacterCoreAuthorityLane = Literal[
    "operator_initialize", "operator_revision", "longitudinal_evolution", "compensation"
]


class CharacterCoreTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    core_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal["initialize", "revise", "compensate"]
    authority_lane: CharacterCoreAuthorityLane
    changed_field_classes: tuple[CharacterCoreFieldClass, ...] = Field(min_length=1)
    values_before: CharacterCoreValues | None
    values_after: CharacterCoreValues
    evidence_window: CharacterCoreEvidenceWindow | None = None
    operator_authority: CharacterCoreOperatorAuthorityBinding | None = None
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime
    compensates_transition_id: str | None = None

    @field_validator("accepted_at")
    @classmethod
    def accepted_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("character core transition time must be timezone-aware")
        return value


class CharacterCoreProposedMutation(FrozenModel):
    event_type: Literal[
        "CharacterCoreInitialized",
        "CharacterCoreRevised",
        "CharacterCoreRevisionCompensated",
    ]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> CharacterCoreProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("character core mutation payload must be an object")
        if self.payload_json != json.dumps(
            decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ):
            raise ValueError("character core mutation payload must be canonical")
        return self


class CharacterCoreProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["character_core_revision"] = "character_core_revision"
    proposal_encoding: Literal["typed-authority-v1"]
    authority_contract_ref: Literal["proposal-contract:character-core.1"]
    transition_kind: Literal["initialize", "revise", "compensate"]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: CharacterCoreProposedMutation

    @model_validator(mode="after")
    def transition_matches_event(self) -> CharacterCoreProposalProjection:
        expected = {
            "initialize": "CharacterCoreInitialized",
            "revise": "CharacterCoreRevised",
            "compensate": "CharacterCoreRevisionCompensated",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected:
            raise ValueError("character core proposal transition does not match event")
        return self


FactCardinality = Literal["single", "set"]


class FactAssertionBinding(FrozenModel):
    source_kind: Literal["observed_message", "operator_observation"]
    source_ref: str = Field(min_length=1)
    asserted_subject_ref: str = Field(min_length=1)
    actor_ref: str | None = Field(default=None, min_length=1)
    channel: str | None = Field(default=None, min_length=1)
    payload_ref: str | None = Field(default=None, min_length=1)
    content_payload_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def source_shape_is_closed(self) -> FactAssertionBinding:
        retained = (self.actor_ref, self.channel, self.payload_ref)
        if self.source_kind == "observed_message" and any(item is None for item in retained):
            raise ValueError("message fact assertion requires the retained whole-message envelope")
        if self.source_kind == "operator_observation" and any(
            item is not None for item in retained
        ):
            raise ValueError("operator fact assertion cannot claim unretained envelope fields")
        return self


class FactValues(FrozenModel):
    subject_ref: str = Field(min_length=1)
    predicate_code: str = Field(min_length=1)
    cardinality: FactCardinality
    conflict_key: str = Field(min_length=1)
    value_ref: str = Field(min_length=1)
    value_hash: str = Field(min_length=64, max_length=64)
    assertion_binding: FactAssertionBinding
    anchor_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    source_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=1, le=10_000)
    privacy_class: PrivacyClass
    status: Literal["active", "withdrawn"] = "active"
    withdrawal_reason_code: (
        Literal["user_request", "source_retracted", "privacy_revoked", "invalid"] | None
    ) = None
    withdrawal_evidence_ref: str | None = None

    @model_validator(mode="after")
    def evidence_and_lifecycle_are_explicit(self) -> FactValues:
        if self.conflict_key != fact_conflict_key(
            subject_ref=self.subject_ref, predicate_code=self.predicate_code
        ):
            raise ValueError("fact conflict key must derive from its semantic slot")
        if len(self.source_evidence_refs) != len(
            {(item.evidence_type, item.ref_id) for item in self.source_evidence_refs}
        ):
            raise ValueError("fact source evidence refs must be unique")
        if not set(self.anchor_evidence_refs).issubset(set(self.source_evidence_refs)):
            raise ValueError("fact anchors must remain in source evidence")
        if self.status == "withdrawn" and (
            self.withdrawal_reason_code is None or not self.withdrawal_evidence_ref
        ):
            raise ValueError("withdrawn fact requires reason and evidence")
        if self.status == "active" and (
            self.withdrawal_reason_code is not None or self.withdrawal_evidence_ref is not None
        ):
            raise ValueError("active fact cannot carry withdrawal settlement")
        return self


class FactOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


def fact_conflict_key(*, subject_ref: str, predicate_code: str) -> str:
    material = json.dumps(
        {"subject_ref": subject_ref, "predicate_code": predicate_code},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"fact-slot:{hashlib.sha256(material.encode()).hexdigest()}"


def fact_semantic_fingerprint(
    *,
    subject_ref: str,
    predicate_code: str,
    cardinality: FactCardinality,
    conflict_key: str,
    value_hash: str,
    assertion_binding: FactAssertionBinding,
    anchor_evidence_refs: tuple[EvidenceRef, ...],
    policy_refs: tuple[str, ...],
) -> str:
    material = {
        "subject_ref": subject_ref,
        "predicate_code": predicate_code,
        "cardinality": cardinality,
        "conflict_key": conflict_key,
        "value_hash": value_hash,
        "assertion_binding": assertion_binding.model_dump(mode="json"),
        "anchors": sorted(
            (
                {
                    "ref_id": item.ref_id,
                    "evidence_type": item.evidence_type,
                    "source_world_revision": item.source_world_revision,
                    "immutable_hash": item.immutable_hash,
                }
                for item in anchor_evidence_refs
            ),
            key=lambda item: (str(item["evidence_type"]), str(item["ref_id"])),
        ),
        "policy_refs": sorted(policy_refs),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FactProjection(FrozenModel):
    fact_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: FactValues
    origin: FactOrigin
    committed_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def fingerprint_matches_authority(self) -> FactProjection:
        expected = fact_semantic_fingerprint(
            subject_ref=self.values.subject_ref,
            predicate_code=self.values.predicate_code,
            cardinality=self.values.cardinality,
            conflict_key=self.values.conflict_key,
            value_hash=self.values.value_hash,
            assertion_binding=self.values.assertion_binding,
            anchor_evidence_refs=self.values.anchor_evidence_refs,
            policy_refs=self.origin.policy_refs,
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("fact semantic fingerprint does not match authority")
        return self


class FactTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    fact_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal["commit", "correct", "withdraw", "compensate"]
    values_before: FactValues | None
    values_after: FactValues
    semantic_fingerprint_after: str = Field(min_length=64, max_length=64)
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime
    compensates_transition_id: str | None = None

    @model_validator(mode="after")
    def compensation_target_shape_is_explicit(self) -> FactTransitionProjection:
        if self.operation == "compensate" and not self.compensates_transition_id:
            raise ValueError("fact compensation history requires its target")
        if self.operation != "compensate" and self.compensates_transition_id is not None:
            raise ValueError("ordinary fact history cannot carry a compensation target")
        return self


class FactProposedMutation(FrozenModel):
    event_type: Literal[
        "FactCommitted", "FactCorrected", "FactWithdrawn", "FactCorrectionCompensated"
    ]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> FactProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("fact mutation payload must be an object")
        if (
            json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            != self.payload_json
        ):
            raise ValueError("fact mutation payload must use canonical JSON")
        return self


class FactProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["fact_transition"] = "fact_transition"
    proposal_encoding: Literal["typed-authority-v1"]
    authority_contract_ref: Literal["proposal-contract:fact.1"]
    transition_kind: Literal["commit", "correct", "withdraw", "compensate"]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: FactProposedMutation

    @model_validator(mode="after")
    def transition_matches_event(self) -> FactProposalProjection:
        expected = {
            "commit": "FactCommitted",
            "correct": "FactCorrected",
            "withdraw": "FactWithdrawn",
            "compensate": "FactCorrectionCompensated",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected:
            raise ValueError("fact proposal transition does not match event")
        return self


class LegacyExperienceEvidenceRef(FrozenModel):
    """Wide reader for pre-A2 evidence that was not revision/hash pinned."""

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


class LegacyExperienceProjection(FrozenModel):
    authority_contract_version: Literal["legacy-unverified"] = "legacy-unverified"
    experience_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    summary_ref: str = Field(min_length=1)
    evidence_refs: tuple[LegacyExperienceEvidenceRef, ...] = Field(min_length=1)
    occurred_from: datetime
    occurred_to: datetime
    participant_refs: tuple[str, ...] = Field(min_length=1)
    occurrence_refs: tuple[str, ...] = ()
    result_refs: tuple[str, ...] = ()
    privacy_class: PrivacyClass
    status: Literal["legacy-unverified"] = "legacy-unverified"

    @model_validator(mode="after")
    def experience_has_settled_origin(self) -> LegacyExperienceProjection:
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


class ExperienceOccurrenceSettlementBinding(FrozenModel):
    source_kind: Literal["occurrence_settlement"] = "occurrence_settlement"
    authority_event_ref: str = Field(min_length=1)
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(min_length=64, max_length=64)
    occurrence_id: str = Field(min_length=1)
    occurrence_entity_revision: int = Field(ge=1)
    result_id: str = Field(min_length=1)
    result_payload_ref: str = Field(min_length=1)
    result_payload_hash: str = Field(min_length=1)


class ExperienceExecutionReceiptBinding(FrozenModel):
    source_kind: Literal["execution_receipt"] = "execution_receipt"
    receipt_id: str = Field(min_length=1)
    receipt_hash: str = Field(min_length=64, max_length=64)
    action_id: str = Field(min_length=1)
    action_payload_hash: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    observed_state: Literal["delivered", "failed", "cancelled", "expired", "unknown"]
    raw_payload_hash: str = Field(min_length=1)


ExperienceSourceBinding = ExperienceOccurrenceSettlementBinding | ExperienceExecutionReceiptBinding


class ExperienceValues(FrozenModel):
    summary_ref: str = Field(min_length=1)
    summary_payload_hash: str = Field(min_length=64, max_length=64)
    occurred_from: datetime
    occurred_to: datetime
    participant_refs: tuple[str, ...] = Field(min_length=1)
    # A2 settlement accepts exactly one authoritative source.  Exposing a
    # multi-source shape would be misleading because occurrence settlements
    # are committed one at a time and the acceptance bridge cannot authorize
    # several future settlement revisions atomically yet.
    source_bindings: tuple[ExperienceSourceBinding, ...] = Field(min_length=1, max_length=1)
    privacy_class: PrivacyClass

    @model_validator(mode="after")
    def sources_and_window_are_closed(self) -> ExperienceValues:
        if self.occurred_to < self.occurred_from:
            raise ValueError("experience occurrence window is reversed")
        if len(self.participant_refs) != len(set(self.participant_refs)):
            raise ValueError("experience participant refs must be unique")
        identities = tuple(
            (item.source_kind, item.authority_event_ref)
            if isinstance(item, ExperienceOccurrenceSettlementBinding)
            else (item.source_kind, item.receipt_id)
            for item in self.source_bindings
        )
        if len(identities) != len(set(identities)):
            raise ValueError("experience source identities must be unique")
        return self


class ExperienceOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


def experience_semantic_fingerprint(
    *, values: ExperienceValues, policy_refs: tuple[str, ...]
) -> str:
    material = {
        "values": values.model_dump(mode="json"),
        "policy_refs": sorted(policy_refs),
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ExperienceProjection(FrozenModel):
    experience_id: str = Field(min_length=1)
    entity_revision: Literal[1] = 1
    authority_contract_version: Literal["experience.1"] = "experience.1"
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: ExperienceValues
    origin: ExperienceOrigin
    status: Literal["committed"] = "committed"

    @model_validator(mode="after")
    def fingerprint_matches_immutable_authority(self) -> ExperienceProjection:
        expected = experience_semantic_fingerprint(
            values=self.values, policy_refs=self.origin.policy_refs
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("experience semantic fingerprint does not match authority")
        return self


class ExperienceTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    experience_id: str = Field(min_length=1)
    entity_revision: Literal[1] = 1
    values_after: ExperienceValues
    semantic_fingerprint_after: str = Field(min_length=64, max_length=64)
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime


ExperienceAuthorityProjection = ExperienceProjection | LegacyExperienceProjection


class ExperienceProposedMutation(FrozenModel):
    event_type: Literal["ExperienceCommitted"] = "ExperienceCommitted"
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> ExperienceProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("experience mutation payload must be an object")
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise ValueError("experience mutation payload must use canonical JSON")
        return self


class ExperienceProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["experience_transition"] = "experience_transition"
    proposal_encoding: Literal["typed-authority-v1"]
    authority_contract_ref: Literal["proposal-contract:experience.1"]
    transition_kind: Literal["commit"] = "commit"
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: Literal[0] = 0
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: ExperienceProposedMutation


class SituationStateProjection(FrozenModel):
    location_ref: str | None = None
    activity: str | None = None
    activity_phase: str | None = None
    attention: str | None = None
    energy: str | None = None
    current_goal_ref: str | None = None
    participant_refs: tuple[str, ...] = ()
    visibility: PrivacyClass = "private"


class PlanAuthorityOrigin(FrozenModel):
    transition_id: str = Field(min_length=1)
    accepted_event_type: str = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_world_revision: int = Field(ge=1)
    accepted_payload_hash: str = Field(min_length=64, max_length=64)
    accepted_at: datetime
    authority_projection_hash: str = Field(min_length=64, max_length=64)
    binding_hash: str = Field(min_length=64, max_length=64)


def plan_authority_binding_hash(
    *,
    plan_id: str,
    owner_actor_ref: str,
    entity_revision: int,
    transition_id: str,
    event_type: str,
    accepted_event_ref: str,
    accepted_world_revision: int,
    accepted_payload_hash: str,
    accepted_at: datetime,
    projection_hash: str,
) -> str:
    material = {
        "accepted_at": accepted_at.isoformat(),
        "accepted_event_ref": accepted_event_ref,
        "accepted_payload_hash": accepted_payload_hash,
        "accepted_world_revision": accepted_world_revision,
        "entity_revision": entity_revision,
        "event_type": event_type,
        "owner_actor_ref": owner_actor_ref,
        "plan_id": plan_id,
        "projection_hash": projection_hash,
        "transition_id": transition_id,
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
    owner_actor_ref: str | None = Field(default=None, min_length=1)
    authority_origin: PlanAuthorityOrigin | None = None


def plan_authority_projection_hash(plan: PlanStateProjection) -> str:
    return hashlib.sha256(
        json.dumps(
            plan.model_dump(mode="json", exclude={"authority_origin"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def validate_plan_authority_state(
    plans: tuple[PlanStateProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    *,
    logical_time: datetime | None,
    allow_legacy_missing: bool = False,
) -> None:
    event_by_id = {item.event_id: item for item in committed_events}
    expected_type = {
        "planned": {"ActivityPlanned"},
        "active": {"ActivityStarted", "ActivityResumed"},
        "paused": {"ActivityPaused"},
        "completed": {"ActivityCompleted"},
        "abandoned": {"ActivityAbandoned"},
    }
    accepted_revisions: list[int] = []
    accepted_refs: list[str] = []
    transition_ids: list[str] = []
    for plan in plans:
        owner = plan.owner_actor_ref
        origin = plan.authority_origin
        if owner == "legacy:unknown-owner" and origin is None:
            continue
        if owner is None or origin is None:
            if allow_legacy_missing and owner is None and origin is None:
                continue
            raise ValueError("Plan requires explicit owner and exact authority origin")
        event = event_by_id.get(origin.accepted_event_ref)
        if (
            event is None
            or event.event_type != origin.accepted_event_type
            or event.event_type not in expected_type[plan.status]
            or event.world_revision != origin.accepted_world_revision
            or event.payload_hash != origin.accepted_payload_hash
            or event.logical_time != origin.accepted_at
        ):
            raise ValueError("Plan authority origin does not exactly bind its committed event")
        projection_hash = plan_authority_projection_hash(plan)
        if origin.authority_projection_hash != projection_hash:
            raise ValueError("Plan authority projection hash is invalid")
        expected_hash = plan_authority_binding_hash(
            plan_id=plan.plan_id,
            owner_actor_ref=owner,
            entity_revision=plan.entity_revision,
            transition_id=origin.transition_id,
            event_type=event.event_type,
            accepted_event_ref=event.event_id,
            accepted_world_revision=event.world_revision,
            accepted_payload_hash=event.payload_hash,
            accepted_at=event.logical_time,
            projection_hash=projection_hash,
        )
        if origin.binding_hash != expected_hash:
            raise ValueError("Plan authority binding hash is invalid")
        if plan.status == "planned":
            if plan.entity_revision != 1 or plan.last_transitioned_at is not None:
                raise ValueError("planned Plan authority lifecycle is inconsistent")
        elif plan.last_transitioned_at != origin.accepted_at:
            raise ValueError("Plan lifecycle time does not match accepted authority")
        if logical_time is None or origin.accepted_at > logical_time:
            raise ValueError("Plan authority is ahead of authoritative logical time")
        accepted_revisions.append(origin.accepted_world_revision)
        accepted_refs.append(origin.accepted_event_ref)
        transition_ids.append(origin.transition_id)
    if len(accepted_revisions) != len(set(accepted_revisions)):
        raise ValueError("Plan authority event revisions must be unique")
    if len(accepted_refs) != len(set(accepted_refs)):
        raise ValueError("Plan accepted event refs must be unique")
    if len(transition_ids) != len(set(transition_ids)):
        raise ValueError("Plan authority transition ids must be unique")


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


class OutcomeObservation(FrozenModel):
    """A host-supplied, source-bound observation about an active occurrence.

    This is an input command, not an assertion that an occurrence has settled.
    ``WorldRuntime`` resolves every source reference against its pinned ledger
    projection before it writes the corresponding lifecycle event.
    """

    schema_version: SchemaVersion
    observation_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    logical_time: datetime
    created_at: datetime
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
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

    @model_validator(mode="after")
    def observed_time_is_not_ahead_of_input_time(self) -> OutcomeObservation:
        if self.observed_at > self.logical_time:
            raise ValueError("outcome observation cannot be ahead of its input time")
        return self

    def as_projection(self) -> OutcomeObservationProjection:
        return OutcomeObservationProjection(
            observation_id=self.observation_id,
            occurrence_id=self.occurrence_id,
            source_kind=self.source_kind,
            source_refs=self.source_refs,
            observed_payload_ref=self.observed_payload_ref,
            observed_payload_hash=self.observed_payload_hash,
            observed_at=self.observed_at,
            confidence_bp=self.confidence_bp,
        )


class OutcomeProposalProjection(FrozenModel):
    outcome_proposal_id: str = Field(min_length=1)
    decision_proposal_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    occurrence_id: str = Field(min_length=1)
    evaluated_entity_revision: int = Field(ge=1)
    evaluated_world_revision: int = Field(ge=0)
    trigger_ref: str = Field(min_length=1)
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


class InteractionBidOrigin(FrozenModel):
    """The accepted authority behind one private bid opened after media delivery."""

    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    policy_refs: tuple[str, ...] = Field(min_length=1)


class InteractionBidProjection(FrozenModel):
    """A behavior-neutral, private invitation to continue an interaction."""

    bid_id: str = Field(min_length=1)
    entity_revision: Literal[1] = 1
    delivery_id: str = Field(min_length=1)
    delivery_event_ref: str = Field(min_length=1)
    delivery_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deliberation_trigger_id: str = Field(min_length=1)
    goal: str = Field(min_length=1, max_length=128)
    hoped_response: str = Field(min_length=1, max_length=128)
    pressure_bp: int = Field(ge=0, le=10_000)
    audience_ref: str = Field(min_length=1)
    due_at: datetime | None = None
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    status: Literal["open"] = "open"
    opened_at: datetime
    origin: InteractionBidOrigin

    @model_validator(mode="after")
    def source_is_delivery_authority(self) -> "InteractionBidProjection":
        if self.due_at is not None and self.due_at <= self.opened_at:
            raise ValueError("interaction bid due time must follow opening")
        if not any(item.ref_id == self.delivery_event_ref for item in self.evidence_refs):
            raise ValueError("interaction bid evidence must bind delivery event")
        return self


class InteractionBidProposalProjection(FrozenModel):
    interaction_bid_proposal_id: str = Field(min_length=1)
    decision_proposal_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    bid_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    delivery_id: str = Field(min_length=1)
    delivery_event_ref: str = Field(min_length=1)
    delivery_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deliberation_trigger_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    hoped_response: str = Field(min_length=1)
    pressure_bp: int = Field(ge=0, le=10_000)
    audience_ref: str = Field(min_length=1)
    due_at: datetime | None = None
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=0, le=10_000)
    proposed_change_hash: str = Field(min_length=64, max_length=64)


class MediaDeliveryThreadProposalProjection(FrozenModel):
    """A private Thread mutation whose only social source is a delivered image.

    This deliberately does not share ``ThreadProposalProjection``: the latter
    is the older generic typed-proposal authority lane.  Keeping the source
    binding first-class makes it impossible for a preview or a raw receipt to
    be substituted during acceptance/replay.
    """

    media_thread_proposal_id: str = Field(min_length=1)
    decision_proposal_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    operation: Literal["open", "update"]
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    delivery_id: str = Field(min_length=1)
    delivery_event_ref: str = Field(min_length=1)
    delivery_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deliberation_trigger_id: str = Field(min_length=1)
    thread_before: "ThreadProjection | None"
    thread_after: "ThreadProjection"
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=0, le=10_000)
    proposed_change_hash: str = Field(min_length=64, max_length=64)


class OutcomeCandidateDescriptor(FrozenModel):
    """A frozen, model-selectable result candidate for one occurrence.

    The candidate is an authority declaration made when the occurrence is
    committed.  Optional prose remains in the immutable sidecar; without its
    exact hash the candidate is still valid world authority but unavailable to
    semantic deliberation.
    """

    candidate_result_ref: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    result_payload_ref: str = Field(min_length=1)
    result_payload_hash: str = Field(min_length=1)
    privacy_class: PrivacyClass
    content_ref: str | None = Field(default=None, min_length=1)
    content_payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def content_binding_is_complete(self) -> OutcomeCandidateDescriptor:
        if (self.content_ref is None) != (self.content_payload_hash is None):
            raise ValueError("outcome candidate content binding is incomplete")
        return self


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
    candidate_outcomes: tuple[OutcomeCandidateDescriptor, ...] = ()
    # Exact chosen outcome is retained only after settlement.  Optional at the
    # field level permits non-settled heads; the state validator closes shape.
    settled_outcome_ref: str | None = Field(default=None, min_length=1)
    observation_refs: tuple[str, ...] = ()
    visibility: PrivacyClass
    status: Literal["committed", "active", "settled", "cancelled", "expired"]
    activated_at: datetime | None = None
    result_id: str | None = None
    result_payload_ref: str | None = None
    result_payload_hash: str | None = None
    settled_at: datetime | None = None
    settlement_event_ref: str | None = None
    settlement_world_revision: int | None = Field(default=None, ge=1)
    settlement_payload_hash: str | None = Field(default=None, min_length=64, max_length=64)
    terminal_reason_ref: str | None = None

    @model_validator(mode="after")
    def settled_outcome_matches_lifecycle(self, info: ValidationInfo) -> WorldOccurrenceProjection:
        if self.status == "settled":
            if self.settled_outcome_ref is None and (
                info.context is not None
                and info.context.get("source_reducer_bundle") in _LEGACY_WITHOUT_SETTLED_OUTCOME
            ):
                return self
            if (
                self.settled_outcome_ref is None
                or self.settled_outcome_ref not in self.candidate_outcome_refs
            ):
                raise ValueError("settled occurrence requires one candidate outcome")
        elif self.settled_outcome_ref is not None:
            raise ValueError("non-settled occurrence cannot retain a settled outcome")
        if self.candidate_outcomes:
            refs = tuple(item.candidate_result_ref for item in self.candidate_outcomes)
            if refs != self.candidate_outcome_refs or len(set(refs)) != len(refs):
                raise ValueError("outcome candidate descriptors must exactly match frozen refs")
            if len({item.result_id for item in self.candidate_outcomes}) != len(
                self.candidate_outcomes
            ):
                raise ValueError("outcome candidate result ids must be unique")
        return self


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


class CommitmentFulfillmentContract(FrozenModel):
    """Frozen, target-bound proof required to settle a private commitment."""

    contract_kind: Literal["thread_resolution", "execution_receipt"]
    evidence_type: Literal["committed_world_event", "settled_external_result"]
    expected_ref_id: str | None = None
    expected_immutable_hash: str | None = Field(default=None, min_length=64, max_length=64)
    expected_world_revision: int | None = Field(default=None, ge=1)
    expected_event_type: str | None = None
    expected_action_id: str | None = None
    expected_action_payload_hash: str | None = Field(default=None, min_length=64, max_length=64)
    expected_result_id: str | None = None
    expected_result_status: Literal["delivered"] | None = None
    expected_thread_id: str | None = None
    contract_version: Literal["commitment-fulfillment-contract.1"]

    @model_validator(mode="after")
    def contract_is_exact_and_supported(self) -> CommitmentFulfillmentContract:
        expected_evidence = {
            "thread_resolution": "committed_world_event",
            "execution_receipt": "settled_external_result",
        }[self.contract_kind]
        if self.evidence_type != expected_evidence:
            raise ValueError("commitment fulfillment contract evidence type is inconsistent")
        if self.contract_kind == "thread_resolution":
            if self.expected_event_type != "ThreadResolved" or not self.expected_thread_id:
                raise ValueError("thread fulfillment contract must pin ThreadResolved target")
            if any(
                item is not None
                for item in (
                    self.expected_ref_id,
                    self.expected_immutable_hash,
                    self.expected_world_revision,
                    self.expected_action_id,
                    self.expected_action_payload_hash,
                    self.expected_result_id,
                    self.expected_result_status,
                )
            ):
                raise ValueError("thread fulfillment contract has receipt constraints")
        elif (
            not self.expected_action_id
            or not self.expected_action_payload_hash
            or self.expected_result_status != "delivered"
            or self.expected_world_revision is not None
            or self.expected_event_type is not None
            or self.expected_ref_id is not None
            or self.expected_immutable_hash is not None
            or self.expected_thread_id is not None
        ):
            raise ValueError("receipt fulfillment contract must pin delivered action payload")
        return self


CommitmentStatus = Literal["open", "due", "fulfilled", "broken", "released"]


class CommitmentValues(FrozenModel):
    owner_ref: Literal["actor:companion"] = "actor:companion"
    subject_ref: str | None = None
    content_ref: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    anchor_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    source_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    importance_bp: int = Field(ge=0, le=10_000)
    due_window: DueWindow
    persistence_level: Literal["session", "durable"]
    fulfillment_contract: CommitmentFulfillmentContract
    privacy_class: PrivacyClass = "private"
    status: CommitmentStatus = "open"
    settlement_evidence_ref: str | None = None
    settlement_reason_code: (
        Literal[
            "evidence_satisfied",
            "deadline_elapsed",
            "authoritative_failure",
            "user_withdrew",
            "obsolete",
            "precondition_failed",
            "boundary_or_safety_conflict",
            "operator_correction",
        ]
        | None
    ) = None
    predecessor_commitment_ref: str | None = None
    lineage_kind: Literal["correction", "replacement", "renewal"] | None = None

    @model_validator(mode="after")
    def lifecycle_and_sources_are_complete(self) -> CommitmentValues:
        if self.due_window.opens_at.tzinfo is None or self.due_window.closes_at.tzinfo is None:
            raise ValueError("commitment due window must be timezone-aware")
        if len(self.source_evidence_refs) != len(
            {(item.evidence_type, item.ref_id) for item in self.source_evidence_refs}
        ):
            raise ValueError("commitment source evidence refs must be unique")
        if not set(self.anchor_evidence_refs).issubset(set(self.source_evidence_refs)):
            raise ValueError("commitment anchor evidence must remain in source evidence")
        terminal = self.status in {"fulfilled", "broken", "released"}
        if terminal != bool(self.settlement_evidence_ref and self.settlement_reason_code):
            raise ValueError("commitment terminal settlement evidence is incomplete")
        if self.status in {"open", "due"} and (
            self.settlement_evidence_ref is not None or self.settlement_reason_code is not None
        ):
            raise ValueError("active commitment cannot carry terminal settlement")
        if bool(self.predecessor_commitment_ref) != bool(self.lineage_kind):
            raise ValueError("commitment predecessor and lineage kind must appear together")
        return self


def commitment_semantic_fingerprint(
    *,
    owner_ref: str,
    subject_ref: str | None,
    content_ref: str,
    content_hash: str,
    anchor_evidence_refs: tuple[EvidenceRef, ...],
    fulfillment_contract: CommitmentFulfillmentContract,
    predecessor_commitment_ref: str | None = None,
    lineage_kind: str | None = None,
    policy_refs: tuple[str, ...],
) -> str:
    material = {
        "owner_ref": owner_ref,
        "subject_ref": subject_ref,
        "content_ref": content_ref,
        "content_hash": content_hash,
        "anchor_evidence": sorted(
            (
                {
                    "evidence_type": item.evidence_type,
                    "ref_id": item.ref_id,
                    "source_world_revision": item.source_world_revision,
                    "immutable_hash": item.immutable_hash,
                }
                for item in anchor_evidence_refs
            ),
            key=lambda item: (str(item["ref_id"]), json.dumps(item, sort_keys=True)),
        ),
        "fulfillment_contract": fulfillment_contract.model_dump(mode="json"),
        "predecessor_commitment_ref": predecessor_commitment_ref,
        "lineage_kind": lineage_kind,
        "policy_refs": sorted(policy_refs),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CommitmentOrigin(FrozenModel):
    authority_mode: Literal["accepted_proposal", "mechanical_clock"] = "accepted_proposal"
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


class CommitmentProjection(FrozenModel):
    commitment_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: CommitmentValues
    origin: CommitmentOrigin
    opened_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def fingerprint_matches_authority(self) -> CommitmentProjection:
        expected = commitment_semantic_fingerprint(
            owner_ref=self.values.owner_ref,
            subject_ref=self.values.subject_ref,
            content_ref=self.values.content_ref,
            content_hash=self.values.content_hash,
            anchor_evidence_refs=self.values.anchor_evidence_refs,
            fulfillment_contract=self.values.fulfillment_contract,
            predecessor_commitment_ref=self.values.predecessor_commitment_ref,
            lineage_kind=self.values.lineage_kind,
            policy_refs=self.origin.policy_refs,
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("commitment semantic fingerprint does not match authority")
        return self


class CommitmentTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    commitment_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal["open", "due", "fulfill", "break", "release"]
    values_before: CommitmentValues | None
    values_after: CommitmentValues
    change_id: str = Field(min_length=1)
    authority_mode: Literal["accepted_proposal", "mechanical_clock"]
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime


class CommitmentProposedMutation(FrozenModel):
    event_type: Literal[
        "PrivateCommitmentOpened",
        "PrivateCommitmentFulfilled",
        "PrivateCommitmentBroken",
        "PrivateCommitmentReleased",
    ]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> CommitmentProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("commitment mutation payload must be an object")
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise ValueError("commitment mutation payload must use canonical JSON")
        return self


class CommitmentProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["commitment_transition"] = "commitment_transition"
    proposal_encoding: Literal["typed-authority-v1"]
    authority_contract_ref: Literal["proposal-contract:commitment.1"]
    transition_kind: Literal["open", "fulfill", "break", "release"]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: CommitmentProposedMutation

    @model_validator(mode="after")
    def transition_matches_event(self) -> CommitmentProposalProjection:
        expected = {
            "open": "PrivateCommitmentOpened",
            "fulfill": "PrivateCommitmentFulfilled",
            "break": "PrivateCommitmentBroken",
            "release": "PrivateCommitmentReleased",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected:
            raise ValueError("commitment proposal transition does not match event")
        return self


# Temporary compatibility alias for callers that imported the old placeholder.
CommitmentStateProjection = CommitmentProjection


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


class AffectProposalAuditBinding(FrozenModel):
    """Exact generic deliberation authority consumed by the Affect compiler."""

    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_result_ref: str = Field(min_length=1)
    capsule_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_id: str = Field(min_length=1)
    change_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


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
    authority_contract_ref: Literal["affect-proposal-compiler.1"] | None = None
    source_audit: AffectProposalAuditBinding | None = None
    recorded_event_ref: str | None = Field(default=None, min_length=1)
    recorded_event_payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

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
        if (self.recorded_event_ref is None) != (self.recorded_event_payload_hash is None):
            raise ValueError("affect proposal event provenance must be complete or absent")
        if (self.authority_contract_ref is None) != (self.source_audit is None):
            raise ValueError("compiled affect proposal source authority must be complete or absent")
        return self


class PrivateImpressionOrigin(FrozenModel):
    """Accepted authority for an internal-only, revisable user impression."""

    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


class PrivateImpressionProjection(FrozenModel):
    impression_id: str = Field(min_length=1)
    entity_revision: int = Field(default=1, ge=1)
    subject_ref: str = Field(min_length=1)
    interpretation_refs: tuple[str, ...] = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    confidence_bp: int = Field(ge=0, le=10_000)
    first_seen: datetime
    last_supported: datetime
    expiry_condition: str = Field(min_length=1)
    contradiction_refs: tuple[str, ...] = ()
    status: Literal["active", "contradicted", "expired", "superseded"]
    origin: PrivateImpressionOrigin | None = None

    @model_validator(mode="after")
    def private_impression_is_temporally_consistent(self) -> PrivateImpressionProjection:
        if self.first_seen.tzinfo is None or self.first_seen.utcoffset() is None:
            raise ValueError("private impression first_seen must be timezone-aware")
        if self.last_supported.tzinfo is None or self.last_supported.utcoffset() is None:
            raise ValueError("private impression last_supported must be timezone-aware")
        if self.last_supported < self.first_seen:
            raise ValueError("private impression support precedes first_seen")
        if len(self.interpretation_refs) != len(set(self.interpretation_refs)):
            raise ValueError("private impression interpretation refs must be unique")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("private impression source refs must be unique")
        return self


class PrivateImpressionProposedMutation(FrozenModel):
    event_type: Literal["PrivateImpressionAccepted"]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> PrivateImpressionProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("private impression mutation payload must be an object")
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise ValueError("private impression mutation payload must use canonical JSON")
        return self


class PrivateImpressionProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["private_impression_transition"] = "private_impression_transition"
    proposal_encoding: Literal["typed-authority-v1"]
    authority_contract_ref: Literal["proposal-contract:private-impression.1"]
    transition_kind: Literal["open"]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    appraisal_refs: tuple[AppraisalMeaningRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: PrivateImpressionProposedMutation

    @model_validator(mode="after")
    def transition_matches_only_installed_open(self) -> PrivateImpressionProposalProjection:
        if self.proposed_mutation.event_type != "PrivateImpressionAccepted":
            raise ValueError("private impression proposal transition does not match event")
        return self


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
            self.candidate_since.tzinfo is None or self.candidate_since.utcoffset() is None
        ):
            raise ValueError("relationship hysteresis time must be timezone-aware")
        return self


class RelationshipStateOrigin(FrozenModel):
    """Exact accepted mutation that materialized the current relationship head.

    A relationship head is a reducer-derived aggregate, but its latest values
    are wholly asserted by one accepted ``RelationshipSlowVariableAdjusted``
    event.  Keeping that event identity on the head lets the Context resolver
    expose the state without treating a projection snapshot as authority.
    ``None`` is reserved for legacy decoded heads and therefore fails closed
    at the Context boundary.
    """

    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


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
    origin: RelationshipStateOrigin | None = None


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
    cancellation_reason_code: (
        Literal["user_withdrew", "obsolete", "invalid", "duplicate"] | None
    ) = None
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
        if self.status == "resolved" and (not self.resolution_ref or self.resolution_kind is None):
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
            self.cancellation_reason_code is not None or self.cancellation_evidence_ref is not None
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
    operation: Literal["open", "update", "resolve", "cancel", "supersede", "compensate", "expire"]
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


MemorySourceKind = Literal["fact", "experience", "terminal_thread"]
MemoryCandidateStatus = Literal["pending", "active", "rejected", "forgotten"]
MemoryRetentionRationale = Literal[
    "identity_relevance",
    "relationship_continuity",
    "boundary_relevance",
    "unfinished_business",
    "repeated_pattern",
    "future_utility",
    "emotional_salience",
    "world_continuity",
]
MemoryCueKind = Literal[
    "identity",
    "relationship",
    "boundary",
    "unfinished_business",
    "repeated_pattern",
    "future_utility",
    "emotional_residue",
    "world_continuity",
]
MEMORY_SALIENCE_MATRIX_VERSION = "memory-salience-matrix.1"
_MEMORY_SALIENCE_WEIGHTS = {
    "autobiographical_relevance_bp": 15,
    "relationship_relevance_bp": 15,
    "emotional_residue_bp": 10,
    "unfinished_business_bp": 15,
    "recurrence_bp": 15,
    "novelty_bp": 5,
    "future_utility_bp": 15,
    "world_continuity_bp": 10,
}
MEMORY_SALIENCE_MATRIX_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "version": MEMORY_SALIENCE_MATRIX_VERSION,
            "weights": _MEMORY_SALIENCE_WEIGHTS,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


class MemorySourceBinding(FrozenModel):
    source_kind: MemorySourceKind
    source_id: str = Field(min_length=1)
    source_entity_revision: int = Field(ge=1)
    authority_event_ref: str = Field(min_length=1)
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(min_length=64, max_length=64)
    source_values_hash: str = Field(min_length=64, max_length=64)

    @property
    def authority_identity(self) -> tuple[str, str, int]:
        return self.source_kind, self.source_id, self.source_entity_revision


def memory_source_authority_id(binding: MemorySourceBinding) -> str:
    encoded = json.dumps(
        binding.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class MemorySalienceVector(FrozenModel):
    autobiographical_relevance_bp: int = Field(ge=0, le=10_000)
    relationship_relevance_bp: int = Field(ge=0, le=10_000)
    emotional_residue_bp: int = Field(ge=0, le=10_000)
    unfinished_business_bp: int = Field(ge=0, le=10_000)
    recurrence_bp: int = Field(ge=0, le=10_000)
    novelty_bp: int = Field(ge=0, le=10_000)
    future_utility_bp: int = Field(ge=0, le=10_000)
    world_continuity_bp: int = Field(ge=0, le=10_000)
    matrix_version: Literal["memory-salience-matrix.1"] = MEMORY_SALIENCE_MATRIX_VERSION
    matrix_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def matrix_artifact_is_installed(self) -> MemorySalienceVector:
        if self.matrix_digest != MEMORY_SALIENCE_MATRIX_DIGEST:
            raise ValueError("memory salience matrix artifact is not installed")
        return self


def memory_retrieval_strength_bp(salience: MemorySalienceVector) -> int:
    weighted = sum(
        getattr(salience, field) * weight for field, weight in _MEMORY_SALIENCE_WEIGHTS.items()
    )
    return weighted // sum(_MEMORY_SALIENCE_WEIGHTS.values())


class MemoryCandidateValues(FrozenModel):
    summary_ref: str = Field(min_length=1)
    summary_payload_hash: str = Field(min_length=64, max_length=64)
    cue_kind: MemoryCueKind
    source_bindings: tuple[MemorySourceBinding, ...] = Field(min_length=1)
    consumed_source_authority_ids: tuple[str, ...] = Field(min_length=1)
    retention_rationales: tuple[MemoryRetentionRationale, ...] = Field(min_length=1)
    future_use_refs: tuple[str, ...] = ()
    privacy_ceiling: PrivacyClass
    salience: MemorySalienceVector
    review_due_at: datetime | None = None
    status: MemoryCandidateStatus
    retrieval_strength_bp: int = Field(ge=0, le=10_000)
    reinforcement_count: int = Field(ge=0)
    last_reinforced_at: datetime | None = None
    reviewed_at: datetime | None = None
    forgotten_at: datetime | None = None

    @model_validator(mode="after")
    def lifecycle_and_sources_are_explicit(self) -> MemoryCandidateValues:
        identities = tuple(item.authority_identity for item in self.source_bindings)
        if len(identities) != len(set(identities)):
            raise ValueError("memory source authority identities must be unique")
        event_refs = tuple(item.authority_event_ref for item in self.source_bindings)
        if len(event_refs) != len(set(event_refs)):
            raise ValueError("memory source authority events must not be aliased")
        if len(self.consumed_source_authority_ids) != len(set(self.consumed_source_authority_ids)):
            raise ValueError("memory consumed source authority ids must be unique")
        current_authorities = {memory_source_authority_id(item) for item in self.source_bindings}
        if not current_authorities.issubset(set(self.consumed_source_authority_ids)):
            raise ValueError("memory current sources must remain in consumed authority lineage")
        if len(self.retention_rationales) != len(set(self.retention_rationales)):
            raise ValueError("memory retention rationales must be unique")
        if len(self.future_use_refs) != len(set(self.future_use_refs)):
            raise ValueError("memory future-use refs must be unique")
        if self.reinforcement_count == 0 and self.last_reinforced_at is not None:
            raise ValueError("unreinforced memory cannot have a reinforcement time")
        if self.reinforcement_count > 0 and self.last_reinforced_at is None:
            raise ValueError("reinforced memory requires its last reinforcement time")
        if self.status == "pending" and (
            self.reviewed_at is not None or self.forgotten_at is not None
        ):
            raise ValueError("pending memory cannot be reviewed or forgotten")
        if self.status == "active" and (self.reviewed_at is None or self.forgotten_at is not None):
            raise ValueError("active memory requires review and cannot be forgotten")
        if self.status == "active" and self.retrieval_strength_bp == 0:
            raise ValueError("active memory requires nonzero retrieval strength")
        if self.status in {"pending", "active"} and self.retrieval_strength_bp != (
            memory_retrieval_strength_bp(self.salience)
        ):
            raise ValueError("memory retrieval strength must be policy-derived from salience")
        if self.status == "rejected" and (
            self.reviewed_at is None
            or self.forgotten_at is not None
            or self.retrieval_strength_bp != 0
        ):
            raise ValueError("rejected memory must be reviewed and inactive")
        if self.status == "forgotten" and (
            self.reviewed_at is None or self.forgotten_at is None or self.retrieval_strength_bp != 0
        ):
            raise ValueError("forgotten memory must retain explicit terminal timing")
        return self


def memory_candidate_semantic_fingerprint(
    *, values: MemoryCandidateValues, policy_refs: tuple[str, ...]
) -> str:
    material = {
        "summary_ref": values.summary_ref,
        "summary_payload_hash": values.summary_payload_hash,
        "cue_kind": values.cue_kind,
        "source_bindings": tuple(item.model_dump(mode="json") for item in values.source_bindings),
        "retention_rationales": sorted(values.retention_rationales),
        "future_use_refs": sorted(values.future_use_refs),
        "privacy_ceiling": values.privacy_ceiling,
        "salience": values.salience.model_dump(mode="json"),
        "review_due_at": (values.review_due_at.isoformat() if values.review_due_at else None),
        "policy_refs": sorted(policy_refs),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def memory_source_cluster_fingerprint(
    *, values: MemoryCandidateValues, policy_refs: tuple[str, ...]
) -> str:
    material = {
        "cue_kind": values.cue_kind,
        "stable_source_lineages": sorted(
            (item.source_kind, item.source_id) for item in values.source_bindings
        ),
        "policy_refs": sorted(policy_refs),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MemoryCandidateOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


class MemoryCandidateProjection(FrozenModel):
    candidate_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    source_cluster_fingerprint: str = Field(min_length=64, max_length=64)
    source_cluster_lineage: tuple[str, ...] = Field(min_length=1)
    values: MemoryCandidateValues
    origin: MemoryCandidateOrigin
    opened_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def semantic_identity_is_source_bound(self) -> MemoryCandidateProjection:
        expected = memory_candidate_semantic_fingerprint(
            values=self.values, policy_refs=self.origin.policy_refs
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("memory candidate fingerprint does not match sources")
        expected_cluster = memory_source_cluster_fingerprint(
            values=self.values, policy_refs=self.origin.policy_refs
        )
        if self.source_cluster_fingerprint != expected_cluster:
            raise ValueError("memory candidate source cluster fingerprint is invalid")
        if (
            len(self.source_cluster_lineage) != len(set(self.source_cluster_lineage))
            or self.source_cluster_lineage[-1] != self.source_cluster_fingerprint
        ):
            raise ValueError("memory candidate source cluster lineage is invalid")
        if self.opened_at > self.updated_at:
            raise ValueError("memory candidate update cannot precede opening")
        for instant in (
            self.values.last_reinforced_at,
            self.values.reviewed_at,
            self.values.forgotten_at,
        ):
            if instant is not None and not (self.opened_at <= instant <= self.updated_at):
                raise ValueError("memory lifecycle time is outside candidate chronology")
        if any(
            instant is not None and (instant.tzinfo is None or instant.utcoffset() is None)
            for instant in (
                self.opened_at,
                self.updated_at,
                self.values.last_reinforced_at,
                self.values.reviewed_at,
                self.values.forgotten_at,
                self.values.review_due_at,
            )
        ):
            raise ValueError("memory candidate times must be timezone-aware")
        return self


class MemoryCandidateTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal["open", "accept", "reject", "revise", "reinforce", "forget"]
    values_before: MemoryCandidateValues | None
    values_after: MemoryCandidateValues
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime
    revise_kind: Literal["pending_edit", "compress", "clarify", "correct"] | None = None
    reinforcement_reason: MemoryRetentionRationale | None = None
    rejection_reason: (
        Literal[
            "duplicate",
            "insufficient_future_utility",
            "operator_decision",
        ]
        | None
    ) = None
    forget_reason: (
        Literal[
            "scheduled_decay",
            "obsolete_review",
            "privacy_request",
            "source_invalidated",
            "compressed_into",
            "explicit_suppression",
            "low_future_utility",
        ]
        | None
    ) = None


class MemoryRetrievalDecision(FrozenModel):
    candidate_id: str = Field(min_length=1)
    eligible: bool
    source_ids: tuple[str, ...] = ()
    stale_source_ids: tuple[str, ...] = ()
    suppression_reasons: tuple[Literal["not_active", "stale_source", "privacy_ceiling"], ...] = ()
    review_required: bool = False


class MemoryCandidateProposedMutation(FrozenModel):
    event_type: Literal[
        "MemoryCandidateOpened",
        "MemoryCandidateAccepted",
        "MemoryCandidateRejected",
        "MemoryCandidateRevised",
        "MemoryCandidateReinforced",
        "MemoryCandidateForgotten",
    ]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> MemoryCandidateProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("memory candidate mutation payload must be an object")
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise ValueError("memory candidate mutation payload must use canonical JSON")
        return self


class MemoryCandidateProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["memory_candidate_transition"] = "memory_candidate_transition"
    proposal_encoding: Literal["typed-authority-v1"]
    authority_contract_ref: Literal["proposal-contract:memory-candidate.1"]
    transition_kind: Literal["open", "accept", "reject", "revise", "reinforce", "forget"]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: MemoryCandidateProposedMutation

    @model_validator(mode="after")
    def transition_matches_event(self) -> MemoryCandidateProposalProjection:
        expected = {
            "open": "MemoryCandidateOpened",
            "accept": "MemoryCandidateAccepted",
            "reject": "MemoryCandidateRejected",
            "revise": "MemoryCandidateRevised",
            "reinforce": "MemoryCandidateReinforced",
            "forget": "MemoryCandidateForgotten",
        }[self.transition_kind]
        if self.proposed_mutation.event_type != expected:
            raise ValueError("memory candidate proposal transition does not match event")
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
    attestation_environment: Literal["shadow", "enforcement"]
    root_attestation_verified: Literal[True] = True
    external_action_asserted: Literal[True] = True
    principal_possession_status: Literal["not_evaluated", "verified"] = "not_evaluated"
    enforcement_eligible: bool = False
    evidence_hash: str = Field(min_length=64, max_length=64)
    root_key_id: str = Field(min_length=1)
    root_keyset_digest: str = Field(min_length=64, max_length=64)
    root_nonce_hash: str = Field(min_length=64, max_length=64)
    root_proof_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def enforcement_origin_is_closed(self) -> AuthorizationOrigin:
        is_enforcement = self.attestation_environment == "enforcement"
        if is_enforcement != self.enforcement_eligible:
            raise ValueError(
                "authorization enforcement eligibility must match attestation environment"
            )
        if is_enforcement and self.principal_possession_status != "verified":
            raise ValueError("enforcement authorization requires verified principal possession")
        if not is_enforcement and self.principal_possession_status != "not_evaluated":
            raise ValueError("shadow authorization cannot claim principal possession verification")
        return self


class CapabilityGrantValues(FrozenModel):
    capability_kind: Literal[
        "message_send",
        "media_send",
        "reaction_send",
        "read_only_tool",
        "media_planning",
        "media_render",
        "media_inspection",
        "media_repair",
    ]
    actor_ref: str = Field(min_length=1)
    target_scope_refs: tuple[
        Literal[
            "channel:qq",
            "channel:wechat",
            "channel:http",
            "tool:weather",
            "tool:web_search",
            "tool:calendar_read",
            "provider:media",
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
            "character_core_governance",
            "v2_attention_governance",
            "v2_goal_governance",
            "v2_location_governance",
            "v2_resource_governance",
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
    accepted_event_ref: str | None = Field(default=None, min_length=1)
    accepted_world_revision: int | None = Field(default=None, ge=1)
    accepted_payload_hash: str | None = Field(default=None, min_length=64, max_length=64)
    changed_at: datetime
    compensates_transition_id: str | None = None

    @model_validator(mode="after")
    def accepted_event_binding_is_complete(
        self, info: ValidationInfo
    ) -> ActorAuthorityTransitionProjection:
        binding = (
            self.accepted_event_ref,
            self.accepted_world_revision,
            self.accepted_payload_hash,
        )
        if all(item is None for item in binding):
            source = (info.context or {}).get("source_reducer_bundle")
            if source in _LEGACY_WITHOUT_SETTLED_OUTCOME:
                return self
            raise ValueError("ActorAuthority transition requires exact accepted event binding")
        if any(item is None for item in binding):
            raise ValueError("ActorAuthority accepted event binding cannot be partial")
        return self


def validate_actor_authority_event_bindings(
    authorities: tuple[ActorAuthorityProjection, ...],
    transitions: tuple[ActorAuthorityTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    *,
    allow_legacy_missing: bool = False,
) -> None:
    event_by_operation = {
        "bootstrap": "ActorAuthorityBootstrapped",
        "rotate": "ActorAuthorityRotated",
        "revoke": "ActorAuthorityRevoked",
        "compensate": "ActorAuthorityCompensated",
    }
    revisions: list[int] = []
    accepted_refs: list[str] = []
    for transition in transitions:
        if transition.accepted_event_ref is None:
            if allow_legacy_missing:
                continue
            raise ValueError("ActorAuthority transition lacks accepted event binding")
        event = next(
            (item for item in committed_events if item.event_id == transition.accepted_event_ref),
            None,
        )
        if (
            event is None
            or event.event_type != event_by_operation[transition.operation]
            or event.world_revision != transition.accepted_world_revision
            or event.payload_hash != transition.accepted_payload_hash
            or event.logical_time != transition.changed_at
        ):
            raise ValueError("ActorAuthority transition accepted event binding is not exact")
        revisions.append(event.world_revision)
        accepted_refs.append(event.event_id)
    if revisions != sorted(revisions) or len(revisions) != len(set(revisions)):
        raise ValueError("ActorAuthority transition event revisions must be canonical")
    if len(accepted_refs) != len(set(accepted_refs)):
        raise ValueError("ActorAuthority accepted event refs must be unique")
    for authority in authorities:
        lineage = tuple(item for item in transitions if item.authority_id == authority.authority_id)
        if not lineage:
            continue
        latest = lineage[-1]
        if latest.accepted_event_ref is not None and (
            authority.origin.event_ref != latest.accepted_event_ref
            or authority.updated_at != latest.changed_at
        ):
            raise ValueError("ActorAuthority head origin does not match latest accepted event")


class ConsentGrantValues(FrozenModel):
    grantor_ref: str = Field(min_length=1)
    grantee_ref: str = Field(min_length=1)
    action_scope_refs: tuple[
        Literal[
            "message_send",
            "media_send",
            "reaction_send",
            "read_only_tool",
            "media_planning",
            "media_render",
            "media_inspection",
            "media_repair",
        ],
        ...,
    ] = Field(min_length=1)
    data_scope_refs: tuple[
        Literal["data:message_content", "data:user_profile", "data:attachment", "data:location"],
        ...,
    ] = ()
    channel_scope_refs: tuple[Literal["channel:qq", "channel:wechat", "channel:http"], ...] = ()
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
        Literal[
            "viewer:companion",
            "viewer:operator",
            "viewer:room_renderer",
            "viewer:platform_adapter",
            "viewer:media_provider",
        ],
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


class ProviderMediaGrant(FrozenModel):
    """Enforcement authority for an internal media-provider operation.

    The grant is a frozen, first-class ledger record.  It cannot be inferred
    from a generic capability: it pins the exact capability, consent and
    privacy revisions that were approved for a particular provider and media
    stage.  A later revocation/revision is observed by the ActionPump before
    it makes a side effect.
    """

    grant_id: str = Field(min_length=1)
    entity_revision: Literal[1] = 1
    provider_ref: str = Field(min_length=1)
    capability_kind: Literal["media_planning", "media_render", "media_inspection", "media_repair"]
    actor_ref: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    capability_grant_id: str = Field(min_length=1)
    capability_grant_revision: int = Field(ge=1)
    consent_id: str = Field(min_length=1)
    consent_revision: int = Field(ge=1)
    privacy_policy_id: str = Field(min_length=1)
    privacy_policy_revision: int = Field(ge=1)
    issued_at: datetime
    expires_at: datetime | None = None
    enforcement_contract_version: Literal["provider-media-grant.1"] = "provider-media-grant.1"

    @model_validator(mode="after")
    def expiry_follows_issue(self) -> ProviderMediaGrant:
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() is None:
            raise ValueError("provider media grant issue time must be timezone-aware")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise ValueError("provider media grant expiry must be timezone-aware")
            if self.expires_at <= self.issued_at:
                raise ValueError("provider media grant expiry must follow issue")
        return self


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
    experiences: tuple[ExperienceAuthorityProjection, ...] = ()
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
    photo_candidates: tuple[PhotoCandidate, ...] = ()
    media_opportunities: tuple[MediaOpportunity, ...] = ()
    media_plans: tuple[MediaPlan, ...] = ()
    media_unrenderable_opportunity_ids: tuple[str, ...] = ()
    media_artifacts: tuple[MediaArtifact, ...] = ()
    media_inspections: tuple[MediaInspectionRecord, ...] = ()
    media_previews: tuple[MediaPreview, ...] = ()
    media_failed_plan_ids: tuple[str, ...] = ()
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


# These contracts depend on ``WorldEvent`` / ``ProjectionCursor`` above.  Keep
# the imports at this seam so the Fact audit module can in turn use the core
# event and cursor models without creating a module-import cycle.
from .accepted_effect_contracts import AcceptanceManifestRefV3  # noqa: E402
from .fact_proposal_audit_v2 import FactCommitProposalAuditRefV2  # noqa: E402


class LedgerProjection(FrozenModel):
    schema_version: SchemaVersion = "world-v2.1"
    reducer_bundle_version: str = "world-v2-reducers.32"
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
    actions: tuple[Action, ...] = ()
    pending_actions: tuple[Action, ...] = ()
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
    outcome_proposals: tuple[OutcomeProposalProjection, ...] = ()
    semantic_hash: str

    @model_validator(mode="after")
    def pending_index_matches_actions(self) -> LedgerProjection:
        terminal = {"delivered", "failed", "unknown", "cancelled", "expired"}
        expected = tuple(action for action in self.actions if action.state not in terminal)
        if self.pending_actions != expected:
            raise ValueError("pending_actions must equal the non-terminal action index")
        validate_actor_authority_event_bindings(
            self.actor_authorities,
            self.actor_authority_transitions,
            self.committed_world_event_refs,
        )
        dimensions = tuple(item.dimension for item in self.affect_baselines)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("affect baseline dimensions must be unique")
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
        )
        return self
