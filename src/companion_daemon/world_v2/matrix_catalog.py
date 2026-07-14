"""Versioned vocabulary for World v2 situation coordinates.

The catalog deliberately stops at naming and validating coordinates.  It does not rank a
visible response, select a stance, or render text: classifiers provide distributions and the
main deliberation may accept, combine, or reject them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


FixedPoint = int
CatalogOwner = Literal[
    "observation",
    "logical_time",
    "projection",
    "character_core",
    "advisory",
    "deliberation",
    "acceptance",
]
Persistence = Literal[
    "observation",
    "projection",
    "candidate",
    "proposal",
    "accepted_event",
    "external_result",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MatrixSchemaError(ValueError):
    """A coordinate or distribution does not belong to the selected catalog version."""


class MatrixSelection(_FrozenModel):
    field_id: str = Field(min_length=1)
    value: str = Field(min_length=1)


class MatrixField(_FrozenModel):
    """One catalog coordinate and its provenance/lifecycle contract."""

    field_id: str = Field(min_length=1)
    value_set: tuple[str, ...] = Field(min_length=1)
    owner: CatalogOwner
    candidate_producers: tuple[str, ...] = Field(min_length=1)
    consumers: tuple[str, ...] = Field(min_length=1)
    persistence: Persistence
    confidence_required: bool
    expiry_or_decay: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    compatible_values: tuple[MatrixSelection, ...] = ()
    cardinality: Literal["one", "many"] = "one"
    # A catalog coordinate never has authority to prescribe visible behaviour.  Keeping this
    # as a literal makes an accidental rules-engine extension fail schema validation.
    behavior_authority: Literal["none"] = "none"
    hard_invariant_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def values_are_unique(self) -> Self:
        if len(set(self.value_set)) != len(self.value_set):
            raise ValueError(f"{self.field_id} contains duplicate values")
        if self.hard_invariant_refs and self.owner in {"advisory", "deliberation"}:
            raise ValueError("hard invariants belong to authority fields, not aesthetic guidance")
        return self


class CombinationConstraint(_FrozenModel):
    """A schema-level contradiction, never a preferred or mandatory behaviour."""

    constraint_id: str = Field(min_length=1)
    when: tuple[MatrixSelection, ...] = Field(min_length=1)
    incompatible_with: tuple[MatrixSelection, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    constraint_type: Literal["compatibility"] = "compatibility"


class FrequencyBudget(_FrozenModel):
    """Recorded variation pressure supplied to deliberation, not a dispatch quota."""

    state: Literal["normal", "recently_varied", "cooldown_required"]
    window: str = Field(min_length=1)
    used: int = Field(ge=0)
    limit: int = Field(gt=0)
    source_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def usage_is_bounded(self) -> Self:
        if self.used > self.limit:
            raise ValueError("frequency budget used cannot exceed its limit")
        return self


class ClassificationCandidate(_FrozenModel):
    """One fallible interpretation produced outside the authoritative ledger."""

    value: str = Field(min_length=1)
    weight: FixedPoint = Field(ge=0, le=10_000)
    confidence: FixedPoint = Field(ge=0, le=10_000)
    source_refs: tuple[str, ...] = Field(min_length=1)
    producer: str = Field(min_length=1)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def expiry_is_timezone_aware(self) -> Self:
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must be timezone-aware")
        return self

    def is_active(self, *, at: datetime) -> bool:
        return self.expires_at is None or self.expires_at > at


class CandidateDistribution(_FrozenModel):
    """Alternative labels for one field; preserving uncertainty is intentional."""

    catalog_version: str = Field(min_length=1)
    field_id: str = Field(min_length=1)
    candidates: tuple[ClassificationCandidate, ...] = Field(min_length=1)
    frequency_budget: FrequencyBudget | None = None
    produced_at: datetime

    @model_validator(mode="after")
    def produced_at_is_timezone_aware(self) -> Self:
        if self.produced_at.tzinfo is None or self.produced_at.utcoffset() is None:
            raise ValueError("produced_at must be timezone-aware")
        return self

    def active_candidates(self, *, at: datetime) -> tuple[ClassificationCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.is_active(at=at))


class MatrixCatalog(_FrozenModel):
    """Immutable, versioned matrix vocabulary used by compilers and deliberation."""

    catalog_version: str = Field(min_length=1)
    fields: tuple[MatrixField, ...] = Field(min_length=1)
    constraints: tuple[CombinationConstraint, ...] = ()

    @model_validator(mode="after")
    def catalog_is_internally_consistent(self) -> Self:
        field_ids = [field.field_id for field in self.fields]
        if len(set(field_ids)) != len(field_ids):
            raise ValueError("catalog field IDs must be unique")
        for field in self.fields:
            if field.catalog_version != self.catalog_version:
                raise ValueError(f"{field.field_id} belongs to another catalog version")
        known = {field.field_id: set(field.value_set) for field in self.fields}
        for constraint in self.constraints:
            for selection in (*constraint.when, *constraint.incompatible_with):
                if selection.field_id not in known:
                    raise ValueError(
                        f"constraint {constraint.constraint_id} references unknown field "
                        f"{selection.field_id}"
                    )
                if selection.value not in known[selection.field_id]:
                    raise ValueError(
                        f"constraint {constraint.constraint_id} references unknown value "
                        f"{selection.field_id}={selection.value}"
                    )
        return self

    def lookup(self, field_id: str) -> MatrixField:
        for field in self.fields:
            if field.field_id == field_id:
                return field
        raise MatrixSchemaError(f"unknown field: {field_id}")

    def validate_schema(
        self, selections: tuple[MatrixSelection, ...]
    ) -> tuple[MatrixSelection, ...]:
        selected = set(selections)
        selected_by_field: dict[str, set[str]] = {}
        for selection in selections:
            field = self.lookup(selection.field_id)
            if selection.value not in field.value_set:
                raise MatrixSchemaError(
                    f"unknown value for {selection.field_id}: {selection.value}"
                )
            selected_by_field.setdefault(selection.field_id, set()).add(selection.value)
            if field.cardinality == "one" and len(selected_by_field[selection.field_id]) > 1:
                raise MatrixSchemaError(f"multiple values selected for {selection.field_id}")

        for constraint in self.constraints:
            if set(constraint.when).issubset(selected) and any(
                forbidden in selected for forbidden in constraint.incompatible_with
            ):
                raise MatrixSchemaError(f"incompatible coordinates: {constraint.constraint_id}")
        return selections

    def validate_candidates(
        self, distribution: CandidateDistribution, *, at: datetime
    ) -> CandidateDistribution:
        if distribution.catalog_version != self.catalog_version:
            raise MatrixSchemaError(
                f"catalog version mismatch: {distribution.catalog_version} != "
                f"{self.catalog_version}"
            )
        field = self.lookup(distribution.field_id)
        for candidate in distribution.candidates:
            if candidate.value not in field.value_set:
                raise MatrixSchemaError(
                    f"unknown value for {distribution.field_id}: {candidate.value}"
                )
            if field.confidence_required and candidate.confidence == 0:
                raise MatrixSchemaError(
                    f"confidence required for {distribution.field_id}: {candidate.value}"
                )
        # Expired alternatives remain in the record for audit/replay.  Callers explicitly ask
        # ``active_candidates`` when constructing a current capsule.
        distribution.active_candidates(at=at)
        return distribution


def _field(
    field_id: str,
    values: tuple[str, ...],
    *,
    owner: CatalogOwner = "advisory",
    producers: tuple[str, ...] = ("projection", "classifier", "main_model"),
    consumers: tuple[str, ...] = ("situation_compiler", "deliberation", "evaluator"),
    persistence: Persistence = "candidate",
    confidence_required: bool = True,
    expiry: str = "trigger_or_expiry",
    cardinality: Literal["one", "many"] = "one",
    hard_invariant_refs: tuple[str, ...] = (),
) -> MatrixField:
    return MatrixField(
        field_id=field_id,
        value_set=values,
        owner=owner,
        candidate_producers=producers,
        consumers=consumers,
        persistence=persistence,
        confidence_required=confidence_required,
        expiry_or_decay=expiry,
        catalog_version="world-v2-matrix-1",
        cardinality=cardinality,
        hard_invariant_refs=hard_invariant_refs,
    )


def default_matrix_catalog() -> MatrixCatalog:
    """Return the frozen World v2 matrix vocabulary from specification section 5."""

    fields = (
        # 5.1 Observation and evidence
        _field("observation.trigger_source", ("user_message", "clock_tick", "scheduled_plan", "npc_event", "external_result", "operator_command", "recovery"), owner="observation", persistence="observation", confidence_required=False),
        _field("observation.kind", ("text", "attachment", "receipt", "tool_result", "time_elapsed", "world_seed"), owner="observation", persistence="observation", confidence_required=False),
        _field("evidence.status", ("committed_fact", "committed_experience", "committed_or_settled_world_event", "observed_message", "active_plan", "proposal", "settled_external_result", "private_impression", "hypothesis", "unknown"), owner="acceptance", persistence="projection", confidence_required=False, hard_invariant_refs=("fact-truth-v1",)),
        _field("evidence.strength", ("direct", "corroborated", "plausible", "uncertain"), owner="deliberation", persistence="proposal"),
        _field("evidence.temporal_relation", ("current", "recent", "historical", "future_plan", "expired"), owner="projection", persistence="projection", confidence_required=False),
        _field("evidence.causal_role", ("cause", "constraint", "context", "consequence", "reference_only"), owner="deliberation", persistence="proposal"),
        # 5.2 Life situation
        _field("life.time_period", ("deep_night", "morning", "midday", "afternoon", "evening", "late_evening"), owner="logical_time", persistence="projection", confidence_required=False),
        _field("life.location", ("location_ref",), owner="projection", persistence="projection", confidence_required=False),
        _field("life.location_visibility", ("private", "shareable", "public"), owner="projection", persistence="projection", confidence_required=False, hard_invariant_refs=("privacy-v1",)),
        _field("life.state", ("resting", "routine", "focused_work", "study", "social", "travel", "creative", "errand", "recovering", "unstructured"), owner="projection", persistence="projection", confidence_required=False),
        _field("life.activity_phase", ("not_started", "starting", "engaged", "interrupted", "paused", "wrapping_up", "completed", "abandoned"), owner="projection", persistence="projection", confidence_required=False),
        _field("life.attention", ("available", "glancing", "occupied", "deep_focus", "do_not_disturb", "recovering_attention"), owner="projection", persistence="projection", confidence_required=False),
        _field("life.energy", ("restored", "steady", "strained", "depleted"), owner="projection", persistence="projection", confidence_required=False),
        _field("life.resource_pressure", ("none", "mild", "competing", "urgent"), owner="projection", persistence="projection", confidence_required=False),
        _field("life.plan_relation", ("on_plan", "delayed", "substituted", "self_revised", "interrupted_by_event", "cancelled"), owner="projection", persistence="projection", confidence_required=False),
        _field("life.social_environment", ("alone", "with_known_npc", "group_context", "public_ambient", "family_context"), owner="projection", persistence="projection", confidence_required=False),
        _field("life.scene_visibility", ("private", "shareable_life", "shareable_character_media", "not_shareable"), owner="projection", persistence="projection", confidence_required=False, hard_invariant_refs=("privacy-v1",)),
        _field("life.current_goal", ("goal_ref",), owner="projection", persistence="projection", confidence_required=False),
        _field("life.open_commitment", ("commitment_ref",), owner="projection", persistence="projection", confidence_required=False, cardinality="many"),
        # 5.3 Affect, needs, personality and drives
        _field("affect.dimension", ("hurt", "anger", "sadness", "loneliness", "anxiety", "resentment", "warmth", "joy"), owner="projection", persistence="projection", expiry="versioned_decay", cardinality="many"),
        _field("need.kind", ("energy", "attention", "security", "boundary", "connection", "competence", "novelty", "rest"), owner="projection", persistence="projection", cardinality="many"),
        _field("personality.tendency", ("care", "autonomy", "curiosity", "directness", "playfulness", "privacy", "slow_warmth", "persistence"), owner="character_core", persistence="projection", confidence_required=False, expiry="character_core_revision", cardinality="many"),
        _field("drive.temporary", ("care_for_user", "self_protection", "repair", "connection", "competence", "curiosity", "expression", "restoration", "avoidance"), owner="deliberation", persistence="proposal", cardinality="many"),
        _field("conflict.internal", ("care_vs_self_protection", "connection_vs_space", "plan_vs_rest", "honesty_vs_tact", "autonomy_vs_request", "repair_vs_withdrawal", "novelty_vs_stability"), owner="deliberation", persistence="proposal", cardinality="many"),
        # 5.4 Appraisal
        _field("appraisal.base", ("ordinary", "care", "support", "shared_joy", "goal_progress", "uncertainty", "misunderstanding"), cardinality="many"),
        _field("appraisal.negative", ("disappointment", "dismissal", "boundary_violation", "dehumanization", "coercion", "control_pressure", "betrayal", "loss"), cardinality="many"),
        _field("appraisal.relationship", ("user_withdrawing", "user_confused", "repair_attempt", "reliability_confirmed", "reliability_broken"), cardinality="many"),
        _field("appraisal.life", ("restorative_solitude", "creative_satisfaction", "social_warmth", "goal_strain", "npc_conflict", "family_connection"), cardinality="many"),
        _field("appraisal.attribution", ("user", "companion", "npc", "situation", "third_party", "unknown")),
        _field("appraisal.controllability", ("controllable", "partly_controllable", "uncontrollable")),
        _field("appraisal.severity", ("low", "moderate", "high", "acute")),
        _field("appraisal.confidence_level", ("low", "medium", "high")),
        _field("appraisal.lifecycle", ("candidate", "active", "contradicted", "expired", "superseded"), owner="projection", persistence="projection", confidence_required=False),
        # 5.5 Relationship and continuity
        _field("relationship.stage", ("stranger", "acquaintance", "friend", "close_friend", "ambiguous", "lover"), owner="projection", persistence="projection", confidence_required=False),
        _field("relationship.slow_variable", ("trust", "closeness", "respect", "reliability", "mutuality", "repair_confidence"), owner="projection", persistence="projection", confidence_required=False, cardinality="many"),
        _field("relationship.temperature", ("guarded", "cautious", "ordinary", "warm", "playful", "strained", "repairing"), owner="projection", persistence="projection", confidence_required=False),
        _field("relationship.action", ("approach", "maintain", "hold_space", "clarify", "repair", "set_boundary", "withdraw", "reconnect"), owner="deliberation", persistence="proposal"),
        _field("conversation.thread_kind", ("question", "comfort", "promise", "contradiction", "life_share", "reply_reconsider", "pulse", "media_bid"), owner="projection", persistence="projection", confidence_required=False, cardinality="many"),
        _field("conversation.thread_state", ("open", "answered", "skipped", "superseded", "cancelled", "expired"), owner="projection", persistence="projection", confidence_required=False),
        _field("relationship.waiting_stage", ("not_due", "anticipating", "holding_back", "confused", "mildly_hurt", "letting_go", "revisit_later"), owner="projection", persistence="projection", confidence_required=False),
        # Explicit candidate space for "noticed, but chose not to intervene".  These are options
        # seen by deliberation, not a mapping from relationship stage or appraisal.
        _field("social_response_option", ("intervene", "no_intervention", "defer_intervention", "leave_open"), owner="deliberation", persistence="proposal"),
        # 5.6 Stance and expression
        _field("expression.stance", ("comply", "comply_then_revisit", "disagree_gently", "refuse_to_affirm", "set_boundary", "seek_repair", "care_despite_hurt", "care_override", "defer", "remain_silent", "initiate"), owner="deliberation", persistence="proposal"),
        _field("expression.display_strategy", ("direct", "brief_boundary", "acknowledge_then_reply", "listen_before_advice", "gentle_objection", "playful_deflection", "partial_disclosure", "withhold_for_now", "dry_humor", "warm_repair", "quiet_presence"), owner="deliberation", persistence="proposal"),
        _field("expression.language_intensity", ("restrained", "ordinary", "expressive", "emotionally_exposed"), owner="deliberation", persistence="proposal"),
        _field("expression.rhythm", ("immediate", "short_pause", "defer", "multi_beat", "no_reply_now"), owner="deliberation", persistence="proposal"),
        _field("expression.visible_action", ("reply", "question", "reaction", "typing", "read_later", "life_share", "media_share", "tool_result"), owner="deliberation", persistence="proposal", cardinality="many"),
        _field("expression.privacy", ("public_safe", "personal", "intimate_non_explicit", "withhold"), owner="deliberation", persistence="proposal"),
        # 5.7 Variation
        _field("variation.deviation_kind", ("none", "rhythm_deviation", "plan_deviation", "preference_shift", "affect_leakage", "relationship_tension"), owner="deliberation", persistence="proposal"),
        _field("variation.intensity", ("subtle", "noticeable", "strong", "rupture_risk"), owner="deliberation", persistence="proposal"),
        _field("variation.behavior_form", ("linger", "procrastinate", "switch_activity", "decline", "go_quiet", "speak_bluntly", "seek_contact", "pull_away", "repair_spontaneously"), owner="deliberation", persistence="proposal"),
        _field("variation.recovery_posture", ("self_correct", "explain_later", "repair", "hold_boundary", "let_consequence_stand"), owner="deliberation", persistence="proposal"),
        _field("variation.sampling_mode", ("baseline", "weighted_variation", "novelty_seeking", "pressure_amplified"), owner="deliberation", persistence="proposal"),
        _field("variation.frequency_budget", ("normal", "recently_varied", "cooldown_required"), owner="projection", persistence="projection", confidence_required=False),
        # 5.8 Action/capability/budget
        _field("action.family", ("dialogue", "reaction", "media", "multimodal_understanding", "tool", "blocked_initial_auto"), owner="acceptance", persistence="accepted_event", confidence_required=False, hard_invariant_refs=("capability-v1",)),
        _field("action.kind", ("reply", "proactive_message", "followup", "reaction", "sticker", "typing", "media_planning", "media_render", "media_inspection", "media_delivery", "vision", "transcription", "read_only_tool", "file_write", "delete", "shell", "account", "payment", "third_party_commitment"), owner="acceptance", persistence="accepted_event", confidence_required=False, cardinality="many", hard_invariant_refs=("capability-v1", "budget-v1")),
        _field("action.initial_permission", ("budget_auto", "preview_auto", "approval_required", "blocked"), owner="acceptance", persistence="projection", confidence_required=False, hard_invariant_refs=("capability-v1", "privacy-v1", "consent-v1")),
        _field("action.settlement", ("platform_receipt", "platform_receipt_or_timeout", "external_result", "media_result_and_platform_receipt", "not_executable"), owner="acceptance", persistence="external_result", confidence_required=False),
        _field("action.state", ("authorized", "scheduled", "claimed", "dispatch_started", "provider_accepted", "delivered", "failed", "unknown", "cancelled", "expired"), owner="acceptance", persistence="projection", confidence_required=False, hard_invariant_refs=("action-terminal-state-v1",)),
        # 5.9-5.11
        _field("behavior.tendency", ("maintain", "advance", "explore", "procrastinate", "avoid", "rest", "share", "repair", "set_boundary", "disagree"), owner="deliberation", persistence="proposal"),
        _field("change.phase", ("baseline", "preference_deviation", "stress_response", "relationship_tension", "recovery"), owner="projection", persistence="projection", confidence_required=False, expiry="versioned_phase_lifecycle"),
        _field("action.layer", ("internal_state_transition", "world_event", "external_action", "media_action", "read_only_tool"), owner="acceptance", persistence="accepted_event", confidence_required=False, hard_invariant_refs=("action-authority-v1",)),
        # 5.12 lifecycle (shared with appraisal above)
        _field("impression.lifecycle", ("candidate", "active", "contradicted", "expired", "superseded"), owner="projection", persistence="projection", confidence_required=False),
        # 5.13 persistence
        _field("continuity.persistence_kind", ("transient_notice", "open_thread", "private_commitment", "durable_memory_candidate", "committed_fact_or_experience"), owner="deliberation", persistence="proposal"),
        _field("continuity.settlement", ("turn_end", "thread_closed", "answered", "skipped", "expired", "fulfilled", "broken", "released", "accepted", "rejected", "corrected", "superseded"), owner="projection", persistence="projection", confidence_required=False),
        # 5.14 rhythm, interruption and beats
        _field("conversation.input_state", ("user_typing", "coalescing", "complete_thought", "long_narration", "new_interjection"), owner="observation", persistence="observation", confidence_required=False),
        _field("conversation.attention", ("available", "glancing", "occupied", "deep_focus", "recovering_attention"), owner="projection", persistence="projection", confidence_required=False),
        _field("expression.form", ("single_beat", "multi_beat", "reaction_then_text", "defer_with_intent", "silence"), owner="deliberation", persistence="proposal"),
        _field("interruption.motive", ("high_interest", "strong_disagreement", "urgent_correction", "care_impulse", "boundary_pressure", "playful_overlap"), owner="advisory", persistence="candidate", cardinality="many"),
        _field("interruption.cost", ("low", "moderate", "high"), owner="advisory", persistence="candidate"),
        _field("expression.beat_after_interjection", ("continue", "reconsider", "cancel", "merge", "defer"), owner="deliberation", persistence="proposal"),
    )
    return MatrixCatalog(catalog_version="world-v2-matrix-1", fields=fields)


__all__ = [
    "CandidateDistribution",
    "ClassificationCandidate",
    "CombinationConstraint",
    "FrequencyBudget",
    "MatrixCatalog",
    "MatrixField",
    "MatrixSchemaError",
    "MatrixSelection",
    "default_matrix_catalog",
]
