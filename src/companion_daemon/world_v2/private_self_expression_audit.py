"""Read-only evidence for the private-self-to-expression causal chain.

This evaluator reports what an immutable World v2 replay contains.  It does
not score whether the character should have asked a question, shared
something, replied, or stayed silent.  Surface counts are descriptive audit
material only and never feed Acceptance or production prompts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .proposal_audit_schemas import (
    ModelResultAuditProjection,
    ProposalAuditProjection,
    RecordedModelResultAudit,
    RecordedModelUsage,
)
from .proposal_envelope import DecisionProposal, MinimalProposal, validate_proposal_envelope
from .replay_evidence import ReplayEventEvidence, ReplayEvidence
from .schema_core import FrozenModel
from .schemas import Observation


class PrivateSelfExpressionScenarioFragment(FrozenModel):
    """One sender bubble inside a single coalesced user volley."""

    fragment_id: str = Field(min_length=1, max_length=64)
    offset_ms: int = Field(ge=0, le=30_000)
    text: str = Field(min_length=1, max_length=12_000)


class PrivateSelfExpressionScenarioTurn(FrozenModel):
    turn_id: str = Field(min_length=1, max_length=64)
    at_minutes: int = Field(ge=0, le=86_400)
    text: str = Field(min_length=1, max_length=12_000)
    fragments: tuple[PrivateSelfExpressionScenarioFragment, ...] = Field(
        default=(),
        max_length=32,
    )
    overlap_group: str | None = Field(default=None, min_length=1, max_length=64)
    launch_offset_ms: int = Field(default=0, ge=0, le=30_000)

    @model_validator(mode="after")
    def interaction_schedule_is_unambiguous(
        self,
    ) -> PrivateSelfExpressionScenarioTurn:
        if self.fragments:
            if self.fragments[0].offset_ms != 0:
                raise ValueError("a burst must start with a zero-offset fragment")
            offsets = tuple(fragment.offset_ms for fragment in self.fragments)
            if offsets != tuple(sorted(offsets)):
                raise ValueError("burst fragment offsets must be ordered")
            if len({fragment.fragment_id for fragment in self.fragments}) != len(
                self.fragments
            ):
                raise ValueError("burst fragment ids must be unique")
            if self.text != "\n".join(fragment.text for fragment in self.fragments):
                raise ValueError("burst turn text must equal its coalesced fragments")
        if self.overlap_group is None and self.launch_offset_ms != 0:
            raise ValueError("launch offset requires an overlap group")
        return self


class PrivateSelfExpressionScenario(FrozenModel):
    scenario_id: str = Field(min_length=1, max_length=256)
    turns: tuple[PrivateSelfExpressionScenarioTurn, ...] = Field(min_length=1, max_length=128)
    source_event_prefix: str = "private-self-expression-audit"

    def source_event_id(self, turn: PrivateSelfExpressionScenarioTurn) -> str:
        return f"{self.source_event_prefix}:{self.scenario_id}:{turn.turn_id}"

    def source_event_id_for_fragment(
        self,
        turn: PrivateSelfExpressionScenarioTurn,
        index: int,
    ) -> str:
        if not turn.fragments:
            if index != 0:
                raise IndexError("single-message turn has only one source event")
            return self.source_event_id(turn)
        if not 0 <= index < len(turn.fragments):
            raise IndexError("burst fragment index is out of bounds")
        if index == 0:
            # The stable turn identity remains an Observation alias, so the
            # immutable evaluator still emits one report row for the volley.
            return self.source_event_id(turn)
        return (
            self.source_event_id(turn)
            + ":fragment:"
            + turn.fragments[index].fragment_id
        )


class PreconversationLifeEcologyUnitAudit(FrozenModel):
    """One clock opportunity and the distinct Life result it actually committed."""

    ordinal: int = Field(ge=1)
    logical_time_from: datetime
    logical_time_to: datetime
    clock_status: str = Field(min_length=1)
    ecology_status: Literal[
        "accepted",
        "cooldown",
        "no_op",
        "not_observed",
        "technical_failure",
        "unknown",
    ]
    ecology_reason_code: str = Field(min_length=1)
    ecology_runtime_outcome_ref: str | None = Field(default=None, min_length=1)
    ecology_trigger_id: str | None = Field(default=None, min_length=1)
    ecology_completion_event_ref: str | None = Field(default=None, min_length=1)
    life_model_attempt_counts_by_role: dict[str, int] = Field(default_factory=dict)
    world_author_decision: Literal["no_op", "propose"] | None = None
    cadence_draw_event_ref: str | None = Field(default=None, min_length=1)
    cadence_delay_seconds: int | None = Field(default=None, ge=0)
    ledger_sequence_before: int = Field(ge=0)
    ledger_sequence_after: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_unit_window(self) -> PreconversationLifeEcologyUnitAudit:
        if self.logical_time_to <= self.logical_time_from:
            raise ValueError("ecology audit unit must advance logical time")
        if self.ledger_sequence_after < self.ledger_sequence_before:
            raise ValueError("ecology audit unit cannot move the ledger backwards")
        if any(
            not isinstance(role, str)
            or not role
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for role, count in self.life_model_attempt_counts_by_role.items()
        ):
            raise ValueError("ecology audit model attempt counts are invalid")
        if (self.cadence_draw_event_ref is None) != (
            self.cadence_delay_seconds is None
        ):
            raise ValueError("ecology audit cadence binding is incomplete")
        return self


class PreconversationLifeEcologyStatusCounts(FrozenModel):
    accepted: int = Field(ge=0)
    cooldown: int = Field(ge=0)
    no_op: int = Field(ge=0)
    not_observed: int = Field(ge=0)
    technical_failure: int = Field(ge=0)
    unknown: int = Field(ge=0)


class PreconversationLifeEcologyAudit(FrozenModel):
    """Descriptive runner evidence; clock acceptance is not a Life outcome."""

    contract: Literal["private-self-expression-preconversation-life-ecology.2"] = (
        "private-self-expression-preconversation-life-ecology.2"
    )
    requested_units: int = Field(ge=0)
    unit_seconds: int = Field(gt=0)
    world_started_at: datetime
    conversation_started_at: datetime
    tick_statuses: tuple[str, ...] = ()
    tick_statuses_deprecated: Literal[True] = True
    tick_statuses_semantics: Literal["legacy_clock_status_only"] = "legacy_clock_status_only"
    units: tuple[PreconversationLifeEcologyUnitAudit, ...] = ()
    ecology_status_counts: PreconversationLifeEcologyStatusCounts
    ecology_reason_code_counts: dict[str, int]
    recorded_cadence_cooldown_ordinals: tuple[int, ...] = ()
    next_recorded_consideration_at: datetime | None = None
    life_model_attempt_counts_by_role: dict[str, int] = Field(default_factory=dict)
    world_author_consideration_ordinals: tuple[int, ...] = ()
    world_author_decision_counts: dict[str, int] = Field(default_factory=dict)
    ledger_sequence_before: int = Field(ge=0)
    ledger_sequence_after: int = Field(ge=0)
    new_event_type_counts: dict[str, int]
    experience_count_before: int = Field(ge=0)
    experience_count_after: int = Field(ge=0)
    plan_count_before: int = Field(ge=0)
    plan_count_after: int = Field(ge=0)
    memory_candidate_count_before: int = Field(ge=0)
    memory_candidate_count_after: int = Field(ge=0)

    @model_validator(mode="after")
    def counts_match_units(self) -> PreconversationLifeEcologyAudit:
        if (
            len(self.units) != self.requested_units
            or len(self.tick_statuses) != self.requested_units
        ):
            raise ValueError("ecology audit unit count does not match the request")
        actual_status_counts = {
            key: sum(unit.ecology_status == key for unit in self.units)
            for key in (
                "accepted",
                "cooldown",
                "no_op",
                "not_observed",
                "technical_failure",
                "unknown",
            )
        }
        if self.ecology_status_counts.model_dump() != actual_status_counts:
            raise ValueError("ecology status counts do not match unit evidence")
        actual_reason_counts: dict[str, int] = {}
        for unit in self.units:
            actual_reason_counts[unit.ecology_reason_code] = (
                actual_reason_counts.get(unit.ecology_reason_code, 0) + 1
            )
        if self.ecology_reason_code_counts != actual_reason_counts:
            raise ValueError("ecology reason counts do not match unit evidence")
        expected_cooldown_ordinals = tuple(
            unit.ordinal for unit in self.units if unit.ecology_status == "cooldown"
        )
        if self.recorded_cadence_cooldown_ordinals != expected_cooldown_ordinals:
            raise ValueError("recorded cadence cooldown ordinals do not match unit evidence")
        expected_attempt_counts: dict[str, int] = {}
        for unit in self.units:
            for role, count in unit.life_model_attempt_counts_by_role.items():
                expected_attempt_counts[role] = expected_attempt_counts.get(role, 0) + count
        if self.life_model_attempt_counts_by_role != dict(
            sorted(expected_attempt_counts.items())
        ):
            raise ValueError("life model attempt counts do not match unit evidence")
        expected_consideration_ordinals = tuple(
            unit.ordinal
            for unit in self.units
            if unit.life_model_attempt_counts_by_role.get("world_author", 0) > 0
        )
        if self.world_author_consideration_ordinals != (
            expected_consideration_ordinals
        ):
            raise ValueError(
                "World Author consideration ordinals do not match unit evidence"
            )
        expected_decision_counts: dict[str, int] = {}
        for unit in self.units:
            if unit.world_author_decision is not None:
                expected_decision_counts[unit.world_author_decision] = (
                    expected_decision_counts.get(unit.world_author_decision, 0) + 1
                )
        if self.world_author_decision_counts != dict(
            sorted(expected_decision_counts.items())
        ):
            raise ValueError("World Author decision counts do not match unit evidence")
        if self.ledger_sequence_after < self.ledger_sequence_before:
            raise ValueError("ecology audit cannot move the ledger backwards")
        for expected_ordinal, unit in enumerate(self.units, start=1):
            if unit.ordinal != expected_ordinal:
                raise ValueError("ecology audit unit ordinals must be contiguous")
            if unit.clock_status != self.tick_statuses[expected_ordinal - 1]:
                raise ValueError("legacy tick status must match the unit clock status")
            expected_from = self.world_started_at + (
                timedelta(seconds=self.unit_seconds) * (expected_ordinal - 1)
            )
            expected_to = expected_from + timedelta(seconds=self.unit_seconds)
            if unit.logical_time_from != expected_from or unit.logical_time_to != expected_to:
                raise ValueError("ecology audit unit logical windows are inconsistent")
            if expected_ordinal > 1 and (
                unit.ledger_sequence_before
                != self.units[expected_ordinal - 2].ledger_sequence_after
            ):
                raise ValueError("ecology audit unit ledger windows are not contiguous")
        expected_conversation_start = self.world_started_at + (
            timedelta(seconds=self.unit_seconds) * self.requested_units
        )
        if self.conversation_started_at != expected_conversation_start:
            raise ValueError("ecology audit duration does not reach conversation start")
        if self.units and (
            self.units[0].ledger_sequence_before != self.ledger_sequence_before
            or self.units[-1].ledger_sequence_after != self.ledger_sequence_after
        ):
            raise ValueError("ecology audit unit ledger range does not match summary")
        if not self.units and self.ledger_sequence_after != self.ledger_sequence_before:
            raise ValueError("zero-unit ecology audit cannot advance the ledger")
        return self


class PrivateTurnStateAudit(FrozenModel):
    inner_state_summary: str
    attended_source_refs: tuple[str, ...] = ()


class ImmutableLedgerEventAudit(FrozenModel):
    event_ref: str
    event_type: str
    event_payload_hash: str
    event_envelope_hash: str
    commit_id: str
    ledger_sequence: int = Field(ge=1)


class ModelResultAttemptAudit(FrozenModel):
    """Non-content diagnostics for one immutable model attempt.

    Request/response bodies and model prose are deliberately absent.  This is
    enough to distinguish authored silence from a technical failure and to
    correlate the latter with provider metering without copying private chat
    material out of the ledger. ``attempt_lane`` comes only from the durable
    process attempt identity; it never infers purpose from model content.
    """

    model_result_ref: str
    parent_model_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    route_reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    selected_proposal_author: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
    )
    attempt_lane: Literal["expression", "background", "unknown"]
    status: str
    failure_code: str | None = None
    request_hash: str
    response_hash: str | None = None
    attempt_index: int = Field(ge=0)
    attempt_count: int = Field(ge=1)
    failure_stage: (
        Literal[
            "role_author",
            "source_closure_review",
            "role_reselection",
            "recovery_role_author",
            "unknown",
        ]
        | None
    ) = None
    failure_class: (
        Literal[
            "timeout",
            "invalid",
            "exception",
            "cancelled",
            "lost",
            "budget_exhausted",
            "unknown",
        ]
        | None
    ) = None
    slot: Literal["primary", "backup", "corrective"] | None = None
    outcome: (
        Literal[
            "winner",
            "returned",
            "invalid",
            "timeout",
            "exception",
            "hedge_cancelled",
            "hedge_lost",
            "budget_exhausted",
        ]
        | None
    ) = None
    usage: RecordedModelUsage | None = None
    ledger_event: ImmutableLedgerEventAudit | None = None


class RecallTraceAudit(FrozenModel):
    mode: Literal["prefetch", "character_pull"]
    query_text: str
    hit_source_refs: tuple[str, ...] = ()
    hit_source_slices: tuple[str, ...] = ()
    hit_count: int = Field(ge=0)
    embedding_status: Literal["unknown", "used", "degraded"]


class WorldClaimAudit(FrozenModel):
    claim_text: str
    scope: str
    source_refs: tuple[str, ...] = ()


class ExpressionBeatAudit(FrozenModel):
    beat_id: str
    text: str | None = None
    content_type: str
    semantic_role: str | None = None
    dependency_beat_ids: tuple[str, ...] = ()
    not_before: datetime | None = None
    expires_at: datetime | None = None


class ExpressionActionAudit(FrozenModel):
    action_id: str
    beat_id: str | None = None
    kind: str
    state: str
    dependencies: tuple[str, ...] = ()
    ledger_event: ImmutableLedgerEventAudit | None = None


class ExpressionReceiptAudit(FrozenModel):
    receipt_id: str
    action_id: str
    observed_state: str
    is_terminal: bool
    provider_ref: str
    ledger_event: ImmutableLedgerEventAudit | None = None


class PrivateSelfCausalChainAudit(FrozenModel):
    private_state_recorded: bool
    # A recorded prefetch trace with one or more hits means its bounded source
    # material was placed in a role-model Context (initial author call or, if
    # the first-pass join lost the race, the character-chosen follow-up). It
    # is attention input, never proof that the character used it. An empty
    # trace leaves the role-model Context unchanged and therefore remains
    # false here.
    prefetch_presented: bool = False
    # Explicit name for the role model's optional second, query-authored pull.
    # The legacy character_recall_selected field remains byte-compatible
    # below, but it must not be read as "any remembered material was visible".
    character_pull_selected: bool = False
    character_recall_selected: bool
    final_private_state_recorded_after_character_recall: bool
    source_bound_claim_recorded: bool
    visible_action_authorized: bool
    terminal_receipt_recorded: bool

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_character_recall_name(
        cls,
        value: object,
    ) -> object:
        """Keep the old field readable while giving its choice a precise name."""

        if not isinstance(value, dict):
            return value
        legacy = value.get("character_recall_selected")
        explicit = value.get("character_pull_selected")
        if explicit is None and isinstance(legacy, bool):
            return {**value, "character_pull_selected": legacy}
        if isinstance(legacy, bool) and isinstance(explicit, bool) and legacy != explicit:
            raise ValueError(
                "legacy character recall and explicit character pull flags disagree"
            )
        return value


class PrivateSelfExpressionTurnAudit(FrozenModel):
    turn_id: str
    source_event_id: str
    observation_id: str | None = None
    observation_event_ref: str | None = None
    proposal_id: str | None = None
    proposal_event: ImmutableLedgerEventAudit | None = None
    model_result_event: ImmutableLedgerEventAudit | None = None
    model_result_attempts: tuple[ModelResultAttemptAudit, ...] = ()
    proposal_selection: Literal[
        "effect_accepted",
        "model_silent",
        "recorded_unaccepted",
        "rejected",
        "stale",
        "missing",
    ]
    private_turn_state: PrivateTurnStateAudit | None = None
    recall_traces: tuple[RecallTraceAudit, ...] = ()
    timing_choice: Literal["now", "later", "silent"] | None = None
    world_claims: tuple[WorldClaimAudit, ...] = ()
    beats: tuple[ExpressionBeatAudit, ...] = ()
    actions: tuple[ExpressionActionAudit, ...] = ()
    receipts: tuple[ExpressionReceiptAudit, ...] = ()
    surface_question_mark_count: int = Field(ge=0)
    causal_chain: PrivateSelfCausalChainAudit
    observations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def selected_author_is_unique(self) -> "PrivateSelfExpressionTurnAudit":
        if (
            sum(
                attempt.selected_proposal_author
                for attempt in self.model_result_attempts
            )
            > 1
        ):
            raise ValueError("a turn cannot select more than one proposal author")
        return self


class PrivateSelfExpressionAuditSummary(FrozenModel):
    turn_count: int = Field(ge=0)
    effect_accepted_turn_count: int = Field(ge=0)
    model_silent_turn_count: int = Field(ge=0)
    prefetch_presented_turn_count: int = Field(default=0, ge=0)
    character_pull_selected_turn_count: int = Field(default=0, ge=0)
    character_recall_turn_count: int = Field(ge=0)
    turns_with_surface_question_marks: int = Field(ge=0)
    surface_question_mark_count: int = Field(ge=0)
    terminal_delivery_turn_count: int = Field(ge=0)
    technical_failure_turn_count: int = Field(ge=0)
    source_review_technical_failure_turn_count: int = Field(default=0, ge=0)
    candidate_validation_exhausted_turn_count: int = Field(default=0, ge=0)
    other_expression_failure_turn_count: int = Field(default=0, ge=0)
    unclassified_attempt_turn_count: int = Field(ge=0)
    reporting_policy: Literal["descriptive_only_not_an_acceptance_rule"] = (
        "descriptive_only_not_an_acceptance_rule"
    )
    surface_question_counting_policy: Literal[
        "surface_question_marks_only_descriptive"
    ] = "surface_question_marks_only_descriptive"

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_character_recall_count(
        cls,
        value: object,
    ) -> object:
        """Populate the explicit pull count when reading a version-2 legacy report."""

        if not isinstance(value, dict):
            return value
        legacy = value.get("character_recall_turn_count")
        explicit = value.get("character_pull_selected_turn_count")
        if explicit is None and isinstance(legacy, int) and not isinstance(legacy, bool):
            return {**value, "character_pull_selected_turn_count": legacy}
        if (
            isinstance(legacy, int)
            and not isinstance(legacy, bool)
            and isinstance(explicit, int)
            and not isinstance(explicit, bool)
            and legacy != explicit
        ):
            raise ValueError(
                "legacy character recall and explicit character pull counts disagree"
            )
        return value


class PrivateSelfExpressionAuditReport(FrozenModel):
    contract: Literal["private-self-expression-audit.2"] = "private-self-expression-audit.2"
    scenario_id: str
    world_id: str
    reducer_bundle_version: str
    ledger_sequence: int = Field(ge=0)
    evidence_source: Literal["immutable_cold_replay"] = "immutable_cold_replay"
    turns: tuple[PrivateSelfExpressionTurnAudit, ...]
    summary: PrivateSelfExpressionAuditSummary


class NaturalnessSourceBoundSelfMaterialAudit(FrozenModel):
    """Cold-replay evidence that personal material could be retrieved.

    Counts deliberately exclude legacy-unverified Experiences and inactive
    MemoryCandidates.  They describe available authority; they do not infer
    that the character noticed or mentioned any item.
    """

    status: Literal["available", "unavailable", "unknown"]
    committed_experience_count: int = Field(ge=0)
    active_memory_candidate_count: int = Field(ge=0)
    pending_memory_candidate_count: int = Field(ge=0)
    unclassified_experience_count: int = Field(ge=0)
    unclassified_memory_candidate_count: int = Field(ge=0)
    unknown_source_family_count: int = Field(ge=0)
    evidence_basis: Literal["immutable_cold_replay_projection"] = (
        "immutable_cold_replay_projection"
    )

    @model_validator(mode="after")
    def status_matches_counts(self) -> NaturalnessSourceBoundSelfMaterialAudit:
        available = self.committed_experience_count + self.active_memory_candidate_count
        unclassified = (
            self.unclassified_experience_count
            + self.unclassified_memory_candidate_count
            + self.unknown_source_family_count
        )
        expected = (
            "available"
            if available
            else ("unknown" if unclassified else "unavailable")
        )
        if self.status != expected:
            raise ValueError("self-material status does not match replay counts")
        return self


class NaturalnessCurrentSelfStateAudit(FrozenModel):
    """Whether replay exposes inputs from which inner_life_snapshot is built.

    The runner cannot observe the provider request body.  This field therefore
    says only whether immutable projection inputs were available to the normal
    Capsule compiler, and explicitly does not claim provider presentation.
    """

    status: Literal["available", "unavailable", "unknown"]
    evidence_basis: Literal["immutable_projection_inputs_not_provider_delivery"] = (
        "immutable_projection_inputs_not_provider_delivery"
    )
    source_input_counts: dict[str, int]
    unknown_input_families: tuple[str, ...] = ()
    provider_presentation: Literal["not_observed"] = "not_observed"

    @model_validator(mode="after")
    def status_matches_inputs(self) -> NaturalnessCurrentSelfStateAudit:
        if any(
            not isinstance(name, str)
            or not name
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for name, count in self.source_input_counts.items()
        ):
            raise ValueError("current-self-state input counts are invalid")
        expected = (
            "available"
            if any(self.source_input_counts.values())
            else ("unknown" if self.unknown_input_families else "unavailable")
        )
        if self.status != expected:
            raise ValueError("current-self-state status does not match replay inputs")
        return self


class NaturalnessPriorInteractionAppraisalAudit(FrozenModel):
    """Convergence of scenario Observations preceding the final turn."""

    status: Literal["settled", "pending", "not_applicable", "unknown"]
    eligible_observation_count: int = Field(ge=0)
    terminal_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    missing_trigger_count: int = Field(ge=0)
    missing_observation_binding_count: int = Field(ge=0)
    unknown_evidence_count: int = Field(ge=0)
    evidence_basis: Literal["immutable_cold_replay_trigger_processes"] = (
        "immutable_cold_replay_trigger_processes"
    )

    @model_validator(mode="after")
    def counts_match_status(self) -> NaturalnessPriorInteractionAppraisalAudit:
        classified = self.terminal_count + self.pending_count + self.missing_trigger_count
        if classified > self.eligible_observation_count:
            raise ValueError("interaction-appraisal counts exceed eligible observations")
        if self.status == "not_applicable" and (
            self.eligible_observation_count
            or classified
            or self.missing_observation_binding_count
            or self.unknown_evidence_count
        ):
            raise ValueError("not-applicable appraisal evidence must be empty")
        if self.status == "settled" and (
            self.eligible_observation_count == 0
            or self.terminal_count != self.eligible_observation_count
            or self.pending_count
            or self.missing_trigger_count
            or self.missing_observation_binding_count
            or self.unknown_evidence_count
        ):
            raise ValueError("settled appraisal evidence is incomplete")
        if self.status == "pending" and not (
            self.pending_count or self.missing_trigger_count
        ):
            raise ValueError("pending appraisal status requires unfinished evidence")
        if self.status == "unknown" and not (
            self.missing_observation_binding_count or self.unknown_evidence_count
        ):
            raise ValueError("unknown appraisal status requires missing audit bindings")
        return self


class NaturalnessReadinessAudit(FrozenModel):
    """Purely descriptive preconditions for interpreting a naturalness run."""

    contract: Literal["private-self-expression-naturalness-readiness.1"] = (
        "private-self-expression-naturalness-readiness.1"
    )
    assessment: Literal[
        "reliability_only",
        "ready_for_naturalness_observation",
        "not_ready_for_naturalness_observation",
        "indeterminate",
    ]
    reporting_policy: Literal["descriptive_only_not_an_acceptance_rule"] = (
        "descriptive_only_not_an_acceptance_rule"
    )
    production_behavior_gate: Literal[False] = False
    requested_preconversation_life_ecology_units: int = Field(ge=0)
    zero_preheat_semantics: Literal["reliability_only"] = "reliability_only"
    source_bound_self_material: NaturalnessSourceBoundSelfMaterialAudit
    inner_life_snapshot: NaturalnessCurrentSelfStateAudit
    prior_interaction_appraisal: NaturalnessPriorInteractionAppraisalAudit
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def zero_preheat_is_never_naturalness_ready(self) -> NaturalnessReadinessAudit:
        if (
            self.requested_preconversation_life_ecology_units == 0
            and self.assessment != "reliability_only"
        ):
            raise ValueError("zero-preheat audit must remain reliability-only")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("naturalness readiness reason codes must be unique")
        return self


_CURRENT_SELF_STATE_SEQUENCE_INPUTS = (
    "appraisals",
    "affect_episodes",
    "relationship_states",
    "private_impressions",
    "world_occurrences",
    "life_arcs",
    "aspirations",
    "goals",
    "locations",
    "resources",
    "attentions",
    "plans",
    "commitments",
    "threads",
)


def _projection_sequence(projection: object, name: str) -> tuple[object, ...] | None:
    value = getattr(projection, name, None)
    if value is None:
        return None
    if isinstance(value, tuple):
        return value
    try:
        return tuple(value)
    except TypeError:
        return None


def _source_bound_self_material(
    projection: object,
) -> NaturalnessSourceBoundSelfMaterialAudit:
    experiences = _projection_sequence(projection, "experiences")
    memories = _projection_sequence(projection, "memory_candidates")
    committed_experience_count = 0
    unclassified_experience_count = 0
    for experience in experiences or ():
        values = getattr(experience, "values", None)
        bindings = getattr(values, "source_bindings", None)
        if (
            getattr(experience, "authority_contract_version", None) == "experience.1"
            and getattr(experience, "status", None) == "committed"
            and bindings
        ):
            committed_experience_count += 1
        else:
            unclassified_experience_count += 1

    active_memory_candidate_count = 0
    pending_memory_candidate_count = 0
    unclassified_memory_candidate_count = 0
    for candidate in memories or ():
        values = getattr(candidate, "values", None)
        status = getattr(values, "status", None)
        bindings = getattr(values, "source_bindings", None)
        if status == "active" and bindings:
            active_memory_candidate_count += 1
        elif status == "pending" and bindings:
            pending_memory_candidate_count += 1
        elif status not in {"rejected", "forgotten"}:
            unclassified_memory_candidate_count += 1

    unavailable_families = sum(item is None for item in (experiences, memories))
    status = (
        "available"
        if committed_experience_count or active_memory_candidate_count
        else (
            "unknown"
            if (
                unclassified_experience_count
                or unclassified_memory_candidate_count
                or unavailable_families
            )
            else "unavailable"
        )
    )
    return NaturalnessSourceBoundSelfMaterialAudit(
        status=status,
        committed_experience_count=committed_experience_count,
        active_memory_candidate_count=active_memory_candidate_count,
        pending_memory_candidate_count=pending_memory_candidate_count,
        unclassified_experience_count=unclassified_experience_count,
        unclassified_memory_candidate_count=unclassified_memory_candidate_count,
        unknown_source_family_count=unavailable_families,
    )


def _inner_life_snapshot_readiness(
    projection: object,
    *,
    self_material: NaturalnessSourceBoundSelfMaterialAudit,
) -> NaturalnessCurrentSelfStateAudit:
    counts: dict[str, int] = {
        "character_core": int(getattr(projection, "character_core", None) is not None),
        "committed_experiences": self_material.committed_experience_count,
        "active_memory_candidates": self_material.active_memory_candidate_count,
    }
    unknown: list[str] = []
    if not hasattr(projection, "character_core"):
        unknown.append("character_core")
    for name in _CURRENT_SELF_STATE_SEQUENCE_INPUTS:
        values = _projection_sequence(projection, name)
        if values is None:
            unknown.append(name)
            counts[name] = 0
        else:
            counts[name] = len(values)
    status = (
        "available"
        if any(counts.values())
        else ("unknown" if unknown else "unavailable")
    )
    return NaturalnessCurrentSelfStateAudit(
        status=status,
        source_input_counts=dict(sorted(counts.items())),
        unknown_input_families=tuple(sorted(unknown)),
    )


def _prior_interaction_appraisal_readiness(
    projection: object,
    *,
    immutable_replay_audit: dict[str, Any],
) -> NaturalnessPriorInteractionAppraisalAudit:
    raw_turns = immutable_replay_audit.get("turns")
    if not isinstance(raw_turns, list):
        return NaturalnessPriorInteractionAppraisalAudit(
            status="unknown",
            eligible_observation_count=0,
            terminal_count=0,
            pending_count=0,
            missing_trigger_count=0,
            missing_observation_binding_count=1,
            unknown_evidence_count=0,
        )
    prior_turns = raw_turns[:-1]
    if not prior_turns:
        return NaturalnessPriorInteractionAppraisalAudit(
            status="not_applicable",
            eligible_observation_count=0,
            terminal_count=0,
            pending_count=0,
            missing_trigger_count=0,
            missing_observation_binding_count=0,
            unknown_evidence_count=0,
        )
    observation_refs: list[str] = []
    missing_bindings = 0
    for turn in prior_turns:
        observation_ref = turn.get("observation_id") if isinstance(turn, dict) else None
        if isinstance(observation_ref, str) and observation_ref:
            observation_refs.append(observation_ref)
        else:
            missing_bindings += 1
    if missing_bindings:
        return NaturalnessPriorInteractionAppraisalAudit(
            status="unknown",
            eligible_observation_count=len(observation_refs),
            terminal_count=0,
            pending_count=0,
            missing_trigger_count=0,
            missing_observation_binding_count=missing_bindings,
            unknown_evidence_count=0,
        )

    processes = _projection_sequence(projection, "trigger_processes")
    if processes is None:
        return NaturalnessPriorInteractionAppraisalAudit(
            status="unknown",
            eligible_observation_count=len(observation_refs),
            terminal_count=0,
            pending_count=0,
            missing_trigger_count=0,
            missing_observation_binding_count=0,
            unknown_evidence_count=len(observation_refs),
        )
    by_source = {
        getattr(process, "source_evidence_ref", None): process
        for process in processes
        if getattr(process, "process_kind", None) == "interaction_appraisal"
        and isinstance(getattr(process, "source_evidence_ref", None), str)
    }
    terminal_count = 0
    pending_count = 0
    missing_trigger_count = 0
    for observation_ref in observation_refs:
        process = by_source.get(observation_ref)
        if process is None:
            missing_trigger_count += 1
        elif getattr(process, "state", None) == "terminal":
            terminal_count += 1
        else:
            pending_count += 1
    return NaturalnessPriorInteractionAppraisalAudit(
        status=(
            "settled"
            if terminal_count == len(observation_refs)
            else "pending"
        ),
        eligible_observation_count=len(observation_refs),
        terminal_count=terminal_count,
        pending_count=pending_count,
        missing_trigger_count=missing_trigger_count,
        missing_observation_binding_count=0,
        unknown_evidence_count=0,
    )


def assess_naturalness_readiness(
    *,
    projection: object,
    immutable_replay_audit: dict[str, Any],
    requested_preconversation_units: int,
) -> NaturalnessReadinessAudit:
    """Describe whether a retained run contains interpretable naturalness evidence.

    This result is audit metadata only.  It never feeds prompts, Acceptance,
    scheduling, retries, or any other production behavior.
    """

    if requested_preconversation_units < 0:
        raise ValueError("preconversation unit count cannot be negative")
    self_material = _source_bound_self_material(projection)
    inner_life_snapshot = _inner_life_snapshot_readiness(
        projection,
        self_material=self_material,
    )
    prior_appraisal = _prior_interaction_appraisal_readiness(
        projection,
        immutable_replay_audit=immutable_replay_audit,
    )
    reason_codes: list[str] = []
    if requested_preconversation_units == 0:
        reason_codes.append("zero_preconversation_life_ecology")
    if self_material.status == "unavailable":
        reason_codes.append("no_source_bound_self_experience_or_memory")
    elif self_material.status == "unknown":
        reason_codes.append("self_material_evidence_unknown")
    if inner_life_snapshot.status == "unavailable":
        reason_codes.append("inner_life_snapshot_inputs_unavailable")
    elif inner_life_snapshot.status == "unknown":
        reason_codes.append("inner_life_snapshot_evidence_unknown")
    if prior_appraisal.status == "pending":
        reason_codes.append("prior_interaction_appraisal_pending")
    elif prior_appraisal.status == "unknown":
        reason_codes.append("prior_interaction_appraisal_evidence_unknown")

    if requested_preconversation_units == 0:
        assessment = "reliability_only"
    elif (
        self_material.status == "unavailable"
        or inner_life_snapshot.status == "unavailable"
        or prior_appraisal.status == "pending"
    ):
        assessment = "not_ready_for_naturalness_observation"
    elif "unknown" in {
        self_material.status,
        inner_life_snapshot.status,
        prior_appraisal.status,
    }:
        assessment = "indeterminate"
    else:
        assessment = "ready_for_naturalness_observation"
    return NaturalnessReadinessAudit(
        assessment=assessment,
        requested_preconversation_life_ecology_units=requested_preconversation_units,
        source_bound_self_material=self_material,
        inner_life_snapshot=inner_life_snapshot,
        prior_interaction_appraisal=prior_appraisal,
        reason_codes=tuple(reason_codes),
    )


def load_private_self_expression_scenario(path: Path) -> PrivateSelfExpressionScenario:
    """Load the bounded real-model conversation fixture used by audit runners."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("private-self expression scenario must be a JSON object")
    scenario_id = value.get("scenario_id")
    raw_turns = value.get("turns")
    if not isinstance(scenario_id, str) or not isinstance(raw_turns, list):
        raise ValueError("private-self expression scenario identity or turns are missing")
    turns = tuple(
        PrivateSelfExpressionScenarioTurn(
            turn_id=str(item["id"]),
            at_minutes=int(item["at_minutes"]),
            text=str(item["text"]),
            fragments=tuple(
                PrivateSelfExpressionScenarioFragment(
                    fragment_id=str(fragment["id"]),
                    offset_ms=int(fragment["offset_ms"]),
                    text=str(fragment["text"]),
                )
                for fragment in item.get("fragments", ())
                if isinstance(fragment, dict)
            ),
            overlap_group=(
                str(item["overlap_group"])
                if item.get("overlap_group") is not None
                else None
            ),
            launch_offset_ms=int(item.get("launch_offset_ms", 0)),
        )
        for item in raw_turns
        if isinstance(item, dict)
    )
    if len(turns) != len(raw_turns):
        raise ValueError("private-self expression scenario contains an invalid turn")
    if len({turn.turn_id for turn in turns}) != len(turns):
        raise ValueError("private-self expression scenario turn ids must be unique")
    return PrivateSelfExpressionScenario(scenario_id=scenario_id, turns=turns)


def _expression_payload(
    proposal: DecisionProposal | MinimalProposal,
) -> tuple[str | None, dict[str, object] | None]:
    for change in proposal.proposed_changes:
        if change.kind == "expression_plan_transition":
            return change.target_id, change.payload.value()
    return None, None


def _timing_choice(
    proposal: DecisionProposal | MinimalProposal,
) -> Literal["now", "later", "silent"]:
    if isinstance(proposal, DecisionProposal):
        return proposal.timing_choice
    if not proposal.proposed_changes and not proposal.action_intents:
        return "silent"
    if len(proposal.action_intents) == 1 and proposal.action_intents[0].kind == "followup":
        return "later"
    return "now"


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expression audit found a timezone-naive due time")
    return parsed


def _beat_audits(payload: dict[str, object] | None) -> tuple[ExpressionBeatAudit, ...]:
    if payload is None:
        return ()
    raw_beats = payload.get("beat_drafts")
    if not isinstance(raw_beats, list):
        return ()
    beats: list[ExpressionBeatAudit] = []
    for raw in raw_beats:
        if not isinstance(raw, dict):
            continue
        delay = raw.get("delay_window")
        if not isinstance(delay, dict):
            delay = {}
        dependencies = raw.get("dependency_beat_ids")
        beats.append(
            ExpressionBeatAudit(
                beat_id=str(raw["beat_id"]),
                text=(str(raw["inline_text"]) if raw.get("inline_text") is not None else None),
                content_type=str(raw["content_type"]),
                semantic_role=(
                    str(raw["semantic_role"]) if raw.get("semantic_role") is not None else None
                ),
                dependency_beat_ids=(
                    tuple(str(item) for item in dependencies)
                    if isinstance(dependencies, list)
                    else ()
                ),
                not_before=_time(delay.get("not_before")),
                expires_at=_time(delay.get("expires_at")),
            )
        )
    return tuple(beats)


def _world_claim_audits(payload: dict[str, object] | None) -> tuple[WorldClaimAudit, ...]:
    if payload is None:
        return ()
    raw_claims = payload.get("world_claims")
    if not isinstance(raw_claims, list):
        return ()
    claims: list[WorldClaimAudit] = []
    for raw in raw_claims:
        if not isinstance(raw, dict):
            continue
        refs = raw.get("source_refs")
        claims.append(
            WorldClaimAudit(
                claim_text=str(raw["claim_text"]),
                scope=str(raw["scope"]),
                source_refs=(tuple(str(item) for item in refs) if isinstance(refs, list) else ()),
            )
        )
    return tuple(claims)


def _recall_trace_audits(
    model_audit: RecordedModelResultAudit | None,
) -> tuple[RecallTraceAudit, ...]:
    if model_audit is None:
        return ()
    # Put the explicit role-owned pull before automatic prefetch in the report.
    if model_audit.presented_prefetch_traces:
        prefetch_traces = tuple(
            presentation.trace
            for presentation in model_audit.presented_prefetch_traces
        )
    else:
        # ``model-result-audit.4`` stored the one presented prefetch directly.
        # Keep that replay shape readable after audit.5 made presentations
        # ordered and model-call-bound.
        prefetch_traces = (model_audit.prefetch_trace,)
    traces = (model_audit.recall_trace, *prefetch_traces)
    output: list[RecallTraceAudit] = []
    for trace in traces:
        if trace is None:
            continue
        output.append(
            RecallTraceAudit(
                mode=trace.mode,
                query_text=trace.request.query_text,
                hit_source_refs=tuple(
                    sorted(
                        {
                            source_ref
                            for hit in trace.hits
                            for source_ref in hit.document.source_refs
                        }
                    )
                ),
                hit_source_slices=tuple(sorted({hit.document.source_slice for hit in trace.hits})),
                hit_count=len(trace.hits),
                embedding_status=trace.embedding_status,
            )
        )
    return tuple(output)


def _ledger_event(item: ReplayEventEvidence) -> ImmutableLedgerEventAudit:
    return ImmutableLedgerEventAudit(
        event_ref=item.event.event_id,
        event_type=item.event.event_type,
        event_payload_hash=item.event.payload_hash,
        event_envelope_hash=item.event_envelope_hash,
        commit_id=item.commit_id,
        ledger_sequence=item.cursor.ledger_sequence,
    )


def _entity_event_indexes(
    evidence: ReplayEvidence,
) -> tuple[
    dict[str, ImmutableLedgerEventAudit],
    dict[str, ImmutableLedgerEventAudit],
    dict[str, ImmutableLedgerEventAudit],
]:
    events_by_ref: dict[str, ImmutableLedgerEventAudit] = {}
    action_events: dict[str, ImmutableLedgerEventAudit] = {}
    receipt_events: dict[str, ImmutableLedgerEventAudit] = {}
    for item in evidence.events:
        pointer = _ledger_event(item)
        events_by_ref[item.event.event_id] = pointer
        if item.event.event_type not in {"ActionAuthorized", "ExecutionReceiptRecorded"}:
            continue
        try:
            payload = json.loads(item.event.payload_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        field = "action" if item.event.event_type == "ActionAuthorized" else "receipt"
        entity = payload.get(field)
        if not isinstance(entity, dict):
            continue
        identity_field = "action_id" if field == "action" else "receipt_id"
        identity = entity.get(identity_field)
        if not isinstance(identity, str):
            continue
        (action_events if field == "action" else receipt_events)[identity] = pointer
    return events_by_ref, action_events, receipt_events


def _model_result_attempt_audits(
    *,
    projections: tuple[ModelResultAuditProjection, ...],
    recorded: dict[str, RecordedModelResultAudit],
    trigger_ref: str,
    events_by_ref: dict[str, ImmutableLedgerEventAudit],
) -> tuple[ModelResultAttemptAudit, ...]:
    """Return Observation-bound attempt metadata without provider/user text."""

    matching = tuple(
        (item, audit)
        for item in projections
        if item.trigger_ref == trigger_ref
        and (audit := recorded.get(item.model_result_ref)) is not None
    )
    expression_authority_present = any(
        audit.attempt_id.startswith("attempt:expression-episode:") for _, audit in matching
    )
    return tuple(
        ModelResultAttemptAudit(
            model_result_ref=item.model_result_ref,
            parent_model_call_id=audit.parent_model_call_id,
            route_reason_code=audit.route.reason_code,
            attempt_lane=classify_model_attempt_lane(
                attempt_id=audit.attempt_id,
                expression_authority_present=expression_authority_present,
            ),
            status=audit.status,
            failure_code=audit.failure_code,
            request_hash=audit.request_hash,
            response_hash=audit.response_hash,
            attempt_index=item.attempt_index,
            attempt_count=item.attempt_count,
            failure_stage=_failure_stage(
                audit.failure_code,
                route_reason_code=audit.route.reason_code,
            ),
            failure_class=_failure_class(audit.failure_code, outcome=audit.outcome),
            slot=audit.slot,
            outcome=audit.outcome,
            usage=audit.usage,
            ledger_event=events_by_ref.get(item.event_ref),
        )
        for item, audit in matching
    )


def classify_model_attempt_lane(
    *,
    attempt_id: str,
    expression_authority_present: bool,
) -> Literal["expression", "background", "unknown"]:
    """Classify an Observation-bound call from its durable process identity.

    Background appraisal deliberately shares the source Observation with the
    foreground expression episode.  Its content-free attempt identity is the
    stable authority boundary; model status or prose must not decide which
    failure can make the user-facing turn silent.  The historical
    ``attempt:pinned-turn:`` identity predates the expression lifecycle and
    could therefore be either foreground or background when replayed alone.
    It is called background only when the same Observation also proves the
    current expression-episode authority. Unknown and future prefixes remain
    explicitly visible instead of being silently excluded as background.
    """

    if attempt_id.startswith("attempt:expression-episode:"):
        return "expression"
    if expression_authority_present and attempt_id.startswith("attempt:pinned-turn:"):
        return "background"
    return "unknown"


def _expression_attempts(
    attempts: tuple[ModelResultAttemptAudit, ...],
) -> tuple[ModelResultAttemptAudit, ...]:
    return tuple(attempt for attempt in attempts if attempt.attempt_lane == "expression")


def _top_level_expression_attempts(
    attempts: tuple[ModelResultAttemptAudit, ...],
) -> tuple[ModelResultAttemptAudit, ...]:
    """Return role-author results, excluding their nested validation calls."""

    return tuple(
        attempt
        for attempt in _expression_attempts(attempts)
        if attempt.parent_model_call_id is None
    )


def _expression_technically_failed(
    attempts: tuple[ModelResultAttemptAudit, ...],
) -> bool:
    author_attempts = _top_level_expression_attempts(attempts)
    return bool(author_attempts) and all(
        attempt.status != "proposal_validated" for attempt in author_attempts
    )


def _expression_failure_bucket(
    attempts: tuple[ModelResultAttemptAudit, ...],
) -> Literal[
    "source_review_technical_failure",
    "candidate_validation_exhausted",
    "other_expression_failure",
] | None:
    author_attempts = _top_level_expression_attempts(attempts)
    if not author_attempts or any(
        attempt.status == "proposal_validated" for attempt in author_attempts
    ):
        return None
    if any(
        (attempt.failure_code or "").startswith("source_review_")
        for attempt in author_attempts
    ):
        return "source_review_technical_failure"
    if all(attempt.failure_class == "invalid" for attempt in author_attempts):
        return "candidate_validation_exhausted"
    return "other_expression_failure"


def _failure_stage(
    failure_code: str | None,
    *,
    route_reason_code: str | None,
) -> (
    Literal[
        "role_author",
        "source_closure_review",
        "role_reselection",
        "recovery_role_author",
        "unknown",
    ]
    | None
):
    if route_reason_code == "validation.validation_reselection":
        return "role_reselection"
    if route_reason_code == "validation.source_review":
        return "source_closure_review"
    if failure_code is None:
        return None
    if failure_code.startswith("source_review_"):
        return "source_closure_review"
    if failure_code.startswith("corrective_"):
        return "role_reselection"
    if failure_code.startswith(("backup_", "quick_")):
        return "recovery_role_author"
    if failure_code.startswith(("main_", "primary_")):
        return "role_author"
    return "unknown"


def _failure_class(
    failure_code: str | None,
    *,
    outcome: str | None,
) -> (
    Literal[
        "timeout",
        "invalid",
        "exception",
        "cancelled",
        "lost",
        "budget_exhausted",
        "unknown",
    ]
    | None
):
    if failure_code is None:
        return None
    normalized = f"{failure_code}:{outcome or ''}"
    for candidate in (
        "budget_exhausted",
        "timeout",
        "invalid",
        "exception",
        "cancelled",
        "lost",
    ):
        if candidate in normalized:
            return candidate
    return "unknown"


class PrivateSelfExpressionAuditEvaluator:
    """Summarize one scenario solely from a cursor-consistent cold replay."""

    def evaluate(
        self,
        *,
        evidence: ReplayEvidence,
        scenario: PrivateSelfExpressionScenario,
    ) -> PrivateSelfExpressionAuditReport:
        projection = evidence.replay
        observation_events: dict[str, tuple[Observation, str]] = {}
        for item in evidence.events:
            if item.event.event_type != "ObservationRecorded":
                continue
            observation = Observation.model_validate_json(item.event.payload_json)
            source_event_ids = observation.coalescing_metadata.get("source_event_ids")
            aliases = (
                tuple(str(value) for value in source_event_ids)
                if isinstance(source_event_ids, list)
                else (observation.source_event_id,)
            )
            for source_event_id in aliases:
                observation_events[source_event_id] = (
                    observation,
                    item.event.event_id,
                )

        decisions = {item.proposal_id: item.status for item in projection.acceptance_decisions}
        effect_accepted_ids = {item.proposal_id for item in projection.minimal_reply_manifests} | {
            item.proposal_id for item in projection.expression_plan_manifests
        }
        effect_accepted_ids.update(
            proposal_id for proposal_id, status in decisions.items() if status == "accepted"
        )
        model_audits = {
            item.model_result_ref: RecordedModelResultAudit.model_validate_json(item.audit_json)
            for item in projection.model_result_audits
        }
        model_audit_projections = {
            item.model_result_ref: item for item in projection.model_result_audits
        }
        events_by_ref, action_events, receipt_events = _entity_event_indexes(evidence)

        turns: list[PrivateSelfExpressionTurnAudit] = []
        for scenario_turn in scenario.turns:
            source_event_id = scenario.source_event_id(scenario_turn)
            located = observation_events.get(source_event_id)
            if located is None:
                turns.append(self._missing_turn(scenario_turn.turn_id, source_event_id))
                continue
            observation, observation_event_ref = located
            model_result_attempts = _model_result_attempt_audits(
                projections=projection.model_result_audits,
                recorded=model_audits,
                trigger_ref=observation_event_ref,
                events_by_ref=events_by_ref,
            )
            candidates: list[
                tuple[ProposalAuditProjection, DecisionProposal | MinimalProposal]
            ] = []
            for audit in projection.proposal_audits:
                if audit.trigger_ref != observation_event_ref:
                    continue
                try:
                    proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(proposal, (DecisionProposal, MinimalProposal)):
                    continue
                if proposal.private_turn_state is None:
                    continue
                candidates.append((audit, proposal))
            if not candidates:
                turns.append(
                    self._missing_turn(
                        scenario_turn.turn_id,
                        source_event_id,
                        observation=observation,
                        observation_event_ref=observation_event_ref,
                        model_result_attempts=model_result_attempts,
                    )
                )
                continue
            selected_audit, proposal = next(
                (
                    candidate
                    for candidate in reversed(candidates)
                    if decisions.get(candidate[1].proposal_id) not in {"rejected", "stale"}
                ),
                candidates[-1],
            )
            model_result_attempts = tuple(
                attempt.model_copy(
                    update={
                        "selected_proposal_author": (
                            attempt.model_result_ref
                            == selected_audit.model_result_ref
                        )
                    }
                )
                for attempt in model_result_attempts
            )
            selection = decisions.get(proposal.proposal_id)
            timing = _timing_choice(proposal)
            proposal_selection: Literal[
                "effect_accepted",
                "model_silent",
                "recorded_unaccepted",
                "rejected",
                "stale",
                "missing",
            ] = (
                "effect_accepted"
                if proposal.proposal_id in effect_accepted_ids
                else "model_silent"
                if timing == "silent" and selection not in {"rejected", "stale"}
                else selection
                if selection in {"rejected", "stale"}
                else "recorded_unaccepted"
            )
            plan_id, payload = _expression_payload(proposal)
            beats = _beat_audits(payload)
            claims = _world_claim_audits(payload)
            actions = tuple(
                ExpressionActionAudit(
                    action_id=action.action_id,
                    beat_id=action.expression_beat_id,
                    kind=action.kind,
                    state=action.state,
                    dependencies=action.dependencies,
                    ledger_event=action_events.get(action.action_id),
                )
                for action in projection.actions
                if plan_id is not None and action.expression_plan_id == plan_id
            )
            action_ids = {action.action_id for action in actions}
            receipts = tuple(
                ExpressionReceiptAudit(
                    receipt_id=receipt.receipt_id,
                    action_id=receipt.action_id,
                    observed_state=receipt.observed_state,
                    is_terminal=receipt.is_terminal,
                    provider_ref=receipt.provider_ref,
                    ledger_event=receipt_events.get(receipt.receipt_id),
                )
                for receipt in projection.execution_receipts
                if receipt.action_id in action_ids
            )
            terminal_action_ids = {receipt.action_id for receipt in receipts if receipt.is_terminal}
            model_audit = model_audits.get(selected_audit.model_result_ref)
            model_audit_projection = model_audit_projections.get(selected_audit.model_result_ref)
            recall_traces = _recall_trace_audits(model_audit)
            prefetch_presented = any(
                trace.mode == "prefetch" and trace.hit_count > 0 for trace in recall_traces
            )
            character_pull_selected = any(
                trace.mode == "character_pull" for trace in recall_traces
            )
            private_state = proposal.private_turn_state
            question_marks = sum(
                (beat.text or "").count("?") + (beat.text or "").count("？") for beat in beats
            )
            observations: list[str] = []
            if proposal_selection == "recorded_unaccepted":
                observations.append("proposal_recorded_without_effect_acceptance")
            if any(not claim.source_refs for claim in claims):
                observations.append("world_claim_without_source_refs")
            if actions and any(
                action.state not in {"delivered", "failed", "unknown"} for action in actions
            ):
                observations.append("expression_action_not_terminal")
            if any(attempt.attempt_lane == "unknown" for attempt in model_result_attempts):
                observations.append("model_result_attempt_lane_unknown")
            turns.append(
                PrivateSelfExpressionTurnAudit(
                    turn_id=scenario_turn.turn_id,
                    source_event_id=source_event_id,
                    observation_id=observation.observation_id,
                    observation_event_ref=observation_event_ref,
                    proposal_id=proposal.proposal_id,
                    proposal_event=events_by_ref.get(selected_audit.event_ref),
                    model_result_event=(
                        events_by_ref.get(model_audit_projection.event_ref)
                        if model_audit_projection is not None
                        else None
                    ),
                    model_result_attempts=model_result_attempts,
                    proposal_selection=proposal_selection,
                    private_turn_state=PrivateTurnStateAudit(
                        inner_state_summary=private_state.inner_state_summary,
                        attended_source_refs=private_state.attended_source_refs,
                    ),
                    recall_traces=recall_traces,
                    timing_choice=timing,
                    world_claims=claims,
                    beats=beats,
                    actions=actions,
                    receipts=receipts,
                    surface_question_mark_count=question_marks,
                    causal_chain=PrivateSelfCausalChainAudit(
                        private_state_recorded=True,
                        prefetch_presented=prefetch_presented,
                        character_pull_selected=character_pull_selected,
                        character_recall_selected=character_pull_selected,
                        # The required-Pts provider contract accepts a character pull
                        # only after an initial PTS and accepts the resulting Proposal
                        # only with a freshly parsed final PTS.  Both are bound to this
                        # model-result/proposal pair in immutable audit records.
                        final_private_state_recorded_after_character_recall=(
                            character_pull_selected
                        ),
                        source_bound_claim_recorded=bool(claims)
                        and all(claim.source_refs for claim in claims),
                        visible_action_authorized=bool(actions),
                        terminal_receipt_recorded=bool(actions)
                        and action_ids.issubset(terminal_action_ids),
                    ),
                    observations=tuple(observations),
                )
            )

        turn_tuple = tuple(turns)
        return PrivateSelfExpressionAuditReport(
            scenario_id=scenario.scenario_id,
            world_id=evidence.world_id,
            reducer_bundle_version=evidence.reducer_bundle_version,
            ledger_sequence=evidence.cursor.ledger_sequence,
            turns=turn_tuple,
            summary=PrivateSelfExpressionAuditSummary(
                turn_count=len(turn_tuple),
                effect_accepted_turn_count=sum(
                    turn.proposal_selection == "effect_accepted" for turn in turn_tuple
                ),
                model_silent_turn_count=sum(
                    turn.proposal_selection == "model_silent" for turn in turn_tuple
                ),
                prefetch_presented_turn_count=sum(
                    turn.causal_chain.prefetch_presented for turn in turn_tuple
                ),
                character_pull_selected_turn_count=sum(
                    turn.causal_chain.character_pull_selected for turn in turn_tuple
                ),
                character_recall_turn_count=sum(
                    turn.causal_chain.character_recall_selected for turn in turn_tuple
                ),
                turns_with_surface_question_marks=sum(
                    turn.surface_question_mark_count > 0 for turn in turn_tuple
                ),
                surface_question_mark_count=sum(
                    turn.surface_question_mark_count for turn in turn_tuple
                ),
                terminal_delivery_turn_count=sum(
                    bool(turn.actions)
                    and all(
                        any(
                            receipt.action_id == action.action_id
                            and receipt.is_terminal
                            and receipt.observed_state == "delivered"
                            for receipt in turn.receipts
                        )
                        for action in turn.actions
                    )
                    for turn in turn_tuple
                ),
                technical_failure_turn_count=sum(
                    turn.proposal_selection == "missing"
                    and _expression_technically_failed(turn.model_result_attempts)
                    for turn in turn_tuple
                ),
                source_review_technical_failure_turn_count=sum(
                    turn.proposal_selection == "missing"
                    and _expression_failure_bucket(turn.model_result_attempts)
                    == "source_review_technical_failure"
                    for turn in turn_tuple
                ),
                candidate_validation_exhausted_turn_count=sum(
                    turn.proposal_selection == "missing"
                    and _expression_failure_bucket(turn.model_result_attempts)
                    == "candidate_validation_exhausted"
                    for turn in turn_tuple
                ),
                other_expression_failure_turn_count=sum(
                    turn.proposal_selection == "missing"
                    and _expression_failure_bucket(turn.model_result_attempts)
                    == "other_expression_failure"
                    for turn in turn_tuple
                ),
                unclassified_attempt_turn_count=sum(
                    any(attempt.attempt_lane == "unknown" for attempt in turn.model_result_attempts)
                    for turn in turn_tuple
                ),
            ),
        )

    @staticmethod
    def _missing_turn(
        turn_id: str,
        source_event_id: str,
        *,
        observation: Observation | None = None,
        observation_event_ref: str | None = None,
        model_result_attempts: tuple[ModelResultAttemptAudit, ...] = (),
    ) -> PrivateSelfExpressionTurnAudit:
        return PrivateSelfExpressionTurnAudit(
            turn_id=turn_id,
            source_event_id=source_event_id,
            observation_id=observation.observation_id if observation is not None else None,
            observation_event_ref=observation_event_ref,
            model_result_attempts=model_result_attempts,
            proposal_selection="missing",
            surface_question_mark_count=0,
            causal_chain=PrivateSelfCausalChainAudit(
                private_state_recorded=False,
                prefetch_presented=False,
                character_pull_selected=False,
                character_recall_selected=False,
                final_private_state_recorded_after_character_recall=False,
                source_bound_claim_recorded=False,
                visible_action_authorized=False,
                terminal_receipt_recorded=False,
            ),
            observations=PrivateSelfExpressionAuditEvaluator._missing_turn_observations(
                observation=observation,
                model_result_attempts=model_result_attempts,
            ),
        )

    @staticmethod
    def _missing_turn_observations(
        *,
        observation: Observation | None,
        model_result_attempts: tuple[ModelResultAttemptAudit, ...],
    ) -> tuple[str, ...]:
        if observation is None:
            return ("observation_missing",)
        observations = [
            (
                "model_result_failure_recorded"
                if _expression_technically_failed(model_result_attempts)
                else "accepted_expression_proposal_missing"
            )
        ]
        if any(attempt.attempt_lane == "unknown" for attempt in model_result_attempts):
            observations.append("model_result_attempt_lane_unknown")
        return tuple(observations)


__all__ = [
    "ExpressionActionAudit",
    "ExpressionBeatAudit",
    "ExpressionReceiptAudit",
    "ImmutableLedgerEventAudit",
    "ModelResultAttemptAudit",
    "NaturalnessCurrentSelfStateAudit",
    "NaturalnessPriorInteractionAppraisalAudit",
    "NaturalnessReadinessAudit",
    "NaturalnessSourceBoundSelfMaterialAudit",
    "PrivateSelfCausalChainAudit",
    "PrivateSelfExpressionAuditEvaluator",
    "PrivateSelfExpressionAuditReport",
    "PrivateSelfExpressionAuditSummary",
    "PrivateSelfExpressionScenario",
    "PrivateSelfExpressionScenarioFragment",
    "PrivateSelfExpressionScenarioTurn",
    "PrivateSelfExpressionTurnAudit",
    "PrivateTurnStateAudit",
    "RecallTraceAudit",
    "WorldClaimAudit",
    "assess_naturalness_readiness",
    "classify_model_attempt_lane",
    "load_private_self_expression_scenario",
]
