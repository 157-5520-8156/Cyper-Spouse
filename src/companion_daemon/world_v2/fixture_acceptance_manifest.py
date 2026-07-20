"""Machine-readable acceptance contract for the 70 frozen ``W2-*`` fixtures.

The Phase-8 scenario corpus is useful regression evidence, but its 120 inputs do
not prove each fixture in section 11.2 of the refactor plan.  This manifest is
the one-to-one bridge from every frozen requirement to mechanisms, production
reachability anchors, durable evidence and executable pytest nodes.

Production anchors are deliberately static claims.  The verifier requires the
file and every marker to exist; a runtime path cannot be claimed merely because
a similarly named test helper exists.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


AcceptanceKind = Literal["internal", "hybrid"]
Reachability = Literal["production", "module_only", "ci_gate"]


@dataclass(frozen=True, slots=True)
class ProductionAnchor:
    path: str
    markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FixtureAcceptance:
    fixture_id: str
    requirement: str
    mechanisms: tuple[str, ...]
    production_anchors: tuple[ProductionAnchor, ...]
    authority_events: tuple[str, ...]
    authority_projections: tuple[str, ...]
    test_nodes: tuple[str, ...]
    coverage_tags: tuple[str, ...] = ()
    production_reachability: Reachability = "production"
    reachability_note: str | None = None
    acceptance_kind: AcceptanceKind = "internal"
    external_gate: str | None = None


def _anchor(path: str, *markers: str) -> ProductionAnchor:
    return ProductionAnchor(path=path, markers=tuple(markers))


def _fixture(
    fixture_id: str,
    requirement: str,
    mechanisms: tuple[str, ...],
    anchors: tuple[ProductionAnchor, ...],
    events: tuple[str, ...],
    projections: tuple[str, ...],
    tests: tuple[str, ...],
    *,
    coverage_tags: tuple[str, ...] = (),
    production_reachability: Reachability = "production",
    reachability_note: str | None = None,
    acceptance_kind: AcceptanceKind = "internal",
    external_gate: str | None = None,
) -> FixtureAcceptance:
    return FixtureAcceptance(
        fixture_id=fixture_id,
        requirement=requirement,
        mechanisms=mechanisms,
        production_anchors=anchors,
        authority_events=events,
        authority_projections=projections,
        test_nodes=tests,
        coverage_tags=coverage_tags,
        production_reachability=production_reachability,
        reachability_note=reachability_note,
        acceptance_kind=acceptance_kind,
        external_gate=external_gate,
    )


_LEDGER = _anchor("src/companion_daemon/world_v2/ledger.py", "class WorldLedger", "def commit")
_FACT = _anchor("src/companion_daemon/world_v2/fact_reducers.py", "FactCommitted", "FactWithdrawn")
_EXP = _anchor("src/companion_daemon/world_v2/experience_events.py", "ExperienceCommitted")
_CORE = _anchor("src/companion_daemon/world_v2/character_core_reducers.py", "CharacterCoreRevised")
_APPRAISAL = _anchor("src/companion_daemon/world_v2/appraisal_reducers.py", "AppraisalAccepted")
_AFFECT = _anchor("src/companion_daemon/world_v2/affect_reducers.py", "AffectEpisodeOpened")
_LIFE = _anchor("src/companion_daemon/world_v2/life_reducers.py", "WorldOccurrenceSettled")
_ACTIVITY = _anchor(
    "src/companion_daemon/world_v2/activity_lifecycle_runtime.py", "ActivityLifecycleRuntime"
)
_SITUATION = _anchor(
    "src/companion_daemon/world_v2/situation_compiler.py", "class SituationCompiler"
)
_MEMORY = _anchor("src/companion_daemon/world_v2/memory_reducers.py", "MemoryCandidateOpened")
_SOCIAL = _anchor(
    "src/companion_daemon/world_v2/social_action_worker.py", "class SocialActionWorker"
)
_PROACTIVE = _anchor(
    "src/companion_daemon/world_v2/proactive_action.py",
    "class ProactiveActionRuntime",
    "class ProactiveDeliberationTurn",
)
_EXPRESSION = _anchor(
    "src/companion_daemon/world_v2/expression_plan_acceptance.py", "ExpressionPlanAcceptance"
)
_RECONSIDER = _anchor(
    "src/companion_daemon/world_v2/expression_reconsideration_runtime.py",
    "ExpressionReconsiderationRuntime",
)
_ACTION = _anchor(
    "src/companion_daemon/world_v2/action_lifecycle.py",
    "def transition_action",
    "def settlement_event_type",
)
_PUMP = _anchor("src/companion_daemon/world_v2/action_pump.py", "class ActionPump")
_MEDIA = _anchor(
    "src/companion_daemon/world_v2/media_execution_runtime.py", "class MediaExecutionRuntime"
)
_MEDIA_CONT = _anchor(
    "src/companion_daemon/world_v2/media_continuation_runtime.py", "class MediaContinuationRuntime"
)
_PERCEPTION = _anchor(
    "src/companion_daemon/world_v2/perception_executor.py", "class PerceptionActionExecutor"
)
_TOOL = _anchor(
    "src/companion_daemon/world_v2/read_only_tool_executor.py", "class ReadOnlyToolActionExecutor"
)
_PROJECTION = _anchor("src/companion_daemon/world_v2/projection.py", "class ProjectionCompiler")
_ECONOMY = _anchor(
    "src/companion_daemon/world_v2/test_economy.py",
    "class CostProfileGate",
    "class LatencyMetricsExporter",
)
_REPLAY = _anchor("src/companion_daemon/world_v2/replay_evaluator.py", "class ReplayEvaluator")


FIXTURE_ACCEPTANCE_MANIFEST_VERSION = "world-v2-fixture-acceptance.1"


FIXTURE_ACCEPTANCE_MANIFEST: tuple[FixtureAcceptance, ...] = (
    _fixture(
        "W2-OBS-001",
        "duplicate QQ/HTTP ingress records one observation",
        ("idempotent ingress", "observation identity"),
        (
            _LEDGER,
            _anchor(
                "src/companion_daemon/world_v2/production_turn_application.py",
                "class WorldV2TurnApplication",
            ),
        ),
        ("ObservationRecorded",),
        ("observation history",),
        (
            "tests/world_v2/test_production_turn_application.py::test_production_application_bootstraps_sqlite_once_and_exposes_only_turn_operations",
        ),
    ),
    _fixture(
        "W2-FACT-001",
        "current user fact retains the complete message envelope and typed slot",
        ("typed Fact authority", "source envelope binding"),
        (_FACT,),
        ("FactCommitted",),
        ("Fact head",),
        (
            "tests/world_v2/test_fact_authority.py::test_fact_message_authority_binds_the_whole_retained_envelope",
        ),
    ),
    _fixture(
        "W2-FACT-002",
        "single-valued Fact conflicts require correction",
        ("cardinality catalog", "Fact CAS"),
        (_FACT,),
        ("FactCommitted",),
        ("single active Fact head",),
        (
            "tests/world_v2/test_fact_authority.py::test_fact_rejects_duplicate_active_semantics_and_single_value_conflict",
        ),
    ),
    _fixture(
        "W2-FACT-003",
        "Fact compensation restores exact latest correction lineage",
        ("typed correction", "compensation lineage"),
        (_FACT,),
        ("FactCorrected", "FactCorrectionCompensated"),
        ("Fact transition history",),
        (
            "tests/world_v2/test_fact_authority.py::test_fact_correction_compensation_restores_exact_latest_before_image",
        ),
    ),
    _fixture(
        "W2-FACT-004",
        "withdrawn Fact is absent from current retrieval but history remains",
        ("withdrawal event", "source-bound retrieval"),
        (
            _FACT,
            _anchor("src/companion_daemon/world_v2/memory_retrieval.py", "class MemoryRetrieval"),
        ),
        ("FactWithdrawn",),
        ("inactive Fact head", "retrieval index"),
        (
            "tests/world_v2/test_fact_authority.py::test_fact_withdrawal_freezes_claim_and_closes_authority",
        ),
    ),
    _fixture(
        "W2-EXP-001",
        "an unfinished plan cannot be committed as lived experience",
        ("experience evidence resolver", "settlement authority"),
        (_EXP,),
        ("ProposalRecorded",),
        ("no confirmed Experience",),
        (
            "tests/world_v2/test_experience_authority.py::test_experience_proposal_rejects_future_settlement_as_current_evidence",
        ),
    ),
    _fixture(
        "W2-EXP-002",
        "settled occurrence and Experience bind exact prospective settlement in one UoW",
        ("occurrence settlement", "Experience A2 authority"),
        (_EXP, _LIFE),
        ("WorldOccurrenceSettled", "ExperienceCommitted"),
        ("Experience head", "occurrence head"),
        (
            "tests/world_v2/test_life_projection.py::test_lived_world_settlement_creates_experience_and_appraisal_atomically",
            "tests/world_v2/test_experience_authority.py::test_experience_rejects_cross_confused_occurrence_settlement",
        ),
    ),
    _fixture(
        "W2-EXP-003",
        "execution Experience binds terminal receipt bytes rather than provider acknowledgement",
        ("receipt authority", "Experience A2 authority"),
        (_EXP, _PUMP),
        ("ActionReceiptRecorded", "ExperienceCommitted"),
        ("Experience head", "Action terminal result"),
        (
            "tests/world_v2/test_experience_authority.py::test_experience_rejects_cross_confused_receipt_hash",
        ),
    ),
    _fixture(
        "W2-EXP-004",
        "legacy Experience cannot be live-appended and migrates as unverified",
        ("legacy quarantine", "SQLite migration"),
        (_EXP, _anchor("src/companion_daemon/world_v2/sqlite_ledger.py", "legacy-unverified")),
        ("ExperienceCommitted",),
        ("legacy-unverified Experience",),
        (
            "tests/world_v2/test_experience_authority.py::test_sqlite_migrates_nonempty_v12_legacy_experience_without_fabricated_lineage",
        ),
        coverage_tags=("migration",),
    ),
    _fixture(
        "W2-CORE-001",
        "one intense argument cannot rewrite slow character traits",
        ("CharacterCore slow lane", "cross-scene evidence window"),
        (_CORE,),
        ("ProposalRecorded",),
        ("unchanged CharacterCore",),
        (
            "tests/world_v2/test_character_core_authority.py::test_long_duration_single_event_cannot_fake_longitudinal_separation",
        ),
    ),
    _fixture(
        "W2-CORE-002",
        "cross-scene evidence can revise an allowed preference with CAS",
        ("longitudinal evidence", "typed Core revision"),
        (_CORE,),
        ("CharacterCoreRevised",),
        ("CharacterCore head",),
        (
            "tests/world_v2/test_character_core_authority.py::test_longitudinal_revision_requires_exact_cross_scene_cross_time_experiences",
        ),
    ),
    _fixture(
        "W2-CORE-003",
        "ordinary model authority cannot change operator-governed boundaries",
        ("operator lane", "actor capability"),
        (_CORE,),
        ("ProposalRecorded",),
        ("unchanged operator Core fields",),
        (
            "tests/world_v2/test_character_core_authority.py::test_operator_lane_rejects_actor_authority_without_character_scope",
        ),
    ),
    _fixture(
        "W2-CORE-004",
        "Core compensation targets exact latest before/after lineage",
        ("Core compensation", "privacy floor"),
        (_CORE,),
        ("CharacterCoreRevisionCompensated",),
        ("CharacterCore transition history",),
        (
            "tests/world_v2/test_character_core_authority.py::test_compensation_restores_semantics_but_never_loosens_privacy_floor",
        ),
    ),
    _fixture(
        "W2-AFF-001",
        "repeated user disappointment is noticed and remains optional advisory material on the next turn",
        (
            "same-turn semantic advisory",
            "appraisal-to-affect",
            "next-turn Context",
            "relationship signal and repair option",
        ),
        (
            _anchor(
                "src/companion_daemon/world_v2/semantic_chat_composition.py",
                "SemanticChatComposition",
            ),
            _APPRAISAL,
            _AFFECT,
        ),
        ("AppraisalAccepted", "AffectEpisodeOpened", "RelationshipSignalAccepted"),
        ("advisory slice", "Affect projection", "Thread projection", "Relationship projection"),
        (
            "tests/world_v2/test_production_same_turn_advisory.py::test_current_disappointment_and_thread_advice_reach_reply_model_without_forcing_comfort",
            "tests/world_v2/test_affect_acceptance_runtime.py::test_accepted_affect_is_source_bound_into_the_next_context_capsule",
            "tests/world_v2/test_production_turn_application.py::test_significant_interaction_state_is_consumed_by_the_next_visible_turn",
            "tests/world_v2/test_appraisal_authority.py::test_accepted_appraisal_opens_a_replayable_relationship_trigger",
        ),
        coverage_tags=("emotion", "relationship", "repair"),
    ),
    _fixture(
        "W2-AFF-002",
        "surface wording such as 'fine' does not resolve hurt or anger authority",
        ("surface/internal separation", "Affect lifecycle", "emotion inertia"),
        (_APPRAISAL, _AFFECT),
        ("AppraisalAccepted", "AffectEpisodeOpened", "AffectEpisodeDecayed"),
        ("open Affect episode",),
        (
            "tests/world_v2/test_appraisal_authority.py::test_accepted_appraisal_can_open_affect_through_an_independent_authority_path",
            "tests/world_v2/test_affect_acceptance_runtime.py::test_accepted_affect_is_source_bound_into_the_next_context_capsule",
        ),
        coverage_tags=("emotion", "resistance"),
    ),
    _fixture(
        "W2-IMP-001",
        "sarcasm uncertainty remains multiple expiring interpretations instead of a user fact",
        ("multi-hypothesis appraisal", "semantic advisory", "expiry"),
        (
            _APPRAISAL,
            _anchor(
                "src/companion_daemon/world_v2/semantic_advisory_adapter.py",
                "class SemanticAdvisoryAdapter",
            ),
        ),
        ("AppraisalAccepted",),
        ("Appraisal hypotheses", "advisory slice"),
        (
            "tests/world_v2/test_life_projection.py::test_claimed_world_trigger_can_commit_multi_hypothesis_appraisal",
            "tests/world_v2/test_semantic_advisory_adapter.py::test_semantic_adapter_returns_only_source_bound_catalog_alternatives",
        ),
    ),
    _fixture(
        "W2-LIFE-001",
        "settled NPC conflict has exactly one psychological consumer before later chat",
        (
            "occurrence lifecycle",
            "NPC appraisal continuation",
            "affect continuation",
            "Context consumption",
        ),
        (
            _LIFE,
            _anchor(
                "src/companion_daemon/world_v2/npc_world_appraisal_trigger_runtime.py",
                "class NpcWorldAppraisalTriggerRuntime",
            ),
            _AFFECT,
        ),
        ("WorldOccurrenceSettled", "AppraisalAccepted", "AffectEpisodeOpened"),
        ("occurrence head", "Experience head", "Affect projection"),
        (
            "tests/world_v2/test_npc_world_appraisal_trigger_runtime.py::test_settled_npc_event_can_create_a_source_bound_companion_appraisal",
            "tests/world_v2/test_scenario_runner.py::test_seeded_multiturn_mechanism_cases_use_the_public_app_and_assert_predicates",
        ),
        coverage_tags=("npc", "emotion"),
    ),
    _fixture(
        "W2-LIFE-002",
        "temporary plan substitution does not fabricate completed experience",
        ("activity plan lifecycle", "zero cascade"),
        (_ACTIVITY, _EXP),
        ("ActivityPlanned", "ActivityReplaced"),
        ("Activity head", "no completed Experience"),
        (
            "tests/world_v2/test_deferred_reply_runtime.py::test_activity_replacement_abandons_predecessor_without_completed_experience",
        ),
    ),
    _fixture(
        "W2-LIFE-003",
        "depleted occupied character may defer or remain silent with a terminal audit",
        ("Situation material", "model social choice", "commitment and followup"),
        (_SITUATION, _SOCIAL),
        ("PrivateCommitmentOpened", "ActionScheduled", "ProposalRecorded"),
        ("Resource slice", "Activity slice", "Commitment head"),
        (
            "tests/world_v2/test_production_turn_application.py::test_production_shared_reply_audit_reaches_defer_without_second_model_call_and_restarts",
            "tests/world_v2/test_production_turn_application.py::test_production_now_and_silent_are_final_without_a_social_background_unit",
        ),
    ),
    _fixture(
        "W2-LIFE-004",
        "paused activity resumes from explicit recovery or clock evidence",
        ("activity lifecycle", "typed resume evidence"),
        (_ACTIVITY,),
        ("ActivityPaused", "ActivityResumed"),
        ("Activity head",),
        (
            "tests/world_v2/test_activity_lifecycle_runtime.py::test_worker_turns_one_claimed_wake_into_one_accepted_transition",
        ),
    ),
    _fixture(
        "W2-LIFE-005",
        "Activity completion changes no unrelated domain head",
        ("zero-cascade reducer", "activity lifecycle"),
        (_ACTIVITY,),
        ("ActivityCompleted",),
        ("Activity head",),
        (
            "tests/world_v2/test_life_projection.py::test_activity_lifecycle_is_revisioned_and_terminal",
        ),
    ),
    _fixture(
        "W2-LIFE-006",
        "goal progress at 10000 remains nonterminal until explicit completion",
        ("Goal lifecycle", "explicit completion contract"),
        (
            _anchor(
                "src/companion_daemon/world_v2/goal_authority_reducers.py",
                "GoalProgressed",
                "GoalCompleted",
            ),
        ),
        ("GoalProgressed",),
        ("active Goal head",),
        (
            "tests/world_v2/test_goal_authority_v16.py::test_goal_open_at_full_progress_remains_active",
        ),
    ),
    _fixture(
        "W2-LIFE-007",
        "one logical clock jump produces each due/expiry event once from the same authority",
        ("logical clock", "mechanical expiry", "deterministic identity"),
        (
            _anchor("src/companion_daemon/world_v2/clock_authority.py", "ClockAdvanced"),
            _anchor(
                "src/companion_daemon/world_v2/goal_expiry_runtime.py",
                "def build_due_goal_expiry_events",
            ),
        ),
        ("ClockAdvanced", "GoalExpired", "AttentionExpired"),
        ("Clock head", "Goal head", "Attention head"),
        (
            "tests/world_v2/test_commitment_authority.py::test_multiple_commitments_can_share_one_clock_without_identity_collision",
            "tests/world_v2/test_goal_authority_v16.py::test_typed_goal_roundtrip_and_two_expiries_share_one_clock_authority",
        ),
    ),
    _fixture(
        "W2-LIFE-008",
        "user assertion about character location cannot directly move the character",
        ("actor authority", "typed Location lane"),
        (
            _anchor(
                "src/companion_daemon/world_v2/location_authority_reducers.py", "LocationChanged"
            ),
        ),
        ("ObservationRecorded",),
        ("unchanged Location head",),
        (
            "tests/world_v2/test_location_authority_v16.py::test_random_and_non_operator_movement_lanes_fail_closed",
        ),
    ),
    _fixture(
        "W2-LIFE-009",
        "out-of-range or nonconserving Resource delta rejects without clamping",
        ("Resource before/after", "closed band policy"),
        (
            _anchor(
                "src/companion_daemon/world_v2/resource_authority_reducers.py",
                "def reduce_v2_resource",
            ),
        ),
        ("ProposalRecorded",),
        ("unchanged Resource head",),
        (
            "tests/world_v2/test_resource_authority_v16.py::test_adjustment_rejects_zero_nonconserving_and_out_of_range_values",
        ),
    ),
    _fixture(
        "W2-LIFE-010",
        "deep focus is Situation material, not an Action gate",
        ("Attention projection", "Situation compiler", "model autonomy"),
        (
            _SITUATION,
            _anchor(
                "src/companion_daemon/world_v2/production_turn_application.py",
                "WorldV2TurnApplication",
            ),
        ),
        ("ObservationRecorded", "ActionAuthorized"),
        ("Attention slice", "Action projection"),
        (
            "tests/world_v2/test_situation_compiler_v16.py::test_compile_is_order_invariant_source_bound_and_explicit_about_missing_heads",
            "tests/world_v2/test_production_turn_application.py::test_production_application_materializes_a_chat_draft_and_settles_one_platform_reply",
        ),
    ),
    _fixture(
        "W2-SIT-001",
        "same pinned authority snapshot compiles to identical Situation hash",
        ("pinned cursor", "canonical compiler"),
        (_SITUATION,),
        (),
        ("Situation semantic hash",),
        (
            "tests/world_v2/test_situation_compiler_v16.py::test_output_is_canonical_json_byte_stable",
        ),
    ),
    _fixture(
        "W2-SIT-002",
        "missing Goal or Location support is explicit unavailable rather than empty truth",
        ("availability semantics", "typed slices"),
        (_SITUATION,),
        (),
        ("unknown/unavailable Situation slices",),
        (
            "tests/world_v2/test_situation_compiler_v16.py::test_empty_ledger_projection_compiles_to_explicit_unavailable_constituents",
        ),
    ),
    _fixture(
        "W2-SIT-003",
        "occurrence and Location truth remain independent when inconsistent",
        ("domain separation", "zero cascade"),
        (_SITUATION, _LIFE),
        ("WorldOccurrenceCommitted",),
        ("occurrence head", "Location head"),
        (
            "tests/world_v2/test_situation_compiler_v16.py::test_compile_is_order_invariant_source_bound_and_explicit_about_missing_heads",
            "tests/world_v2/test_location_authority_v16.py::test_one_head_per_actor_and_unrelated_actor_state_are_preserved",
        ),
    ),
    _fixture(
        "W2-SIT-004",
        "platform Situation projection minimally redacts private location and resources",
        ("viewer capability", "privacy projection"),
        (_SITUATION, _PROJECTION),
        (),
        ("redacted Situation projection",),
        (
            "tests/world_v2/test_situation_compiler_v16.py::test_viewer_projection_redacts_private_domains_without_changing_internal_identity",
        ),
    ),
    _fixture(
        "W2-MEM-001",
        "resolved trivia does not become durable memory",
        ("retention rationale", "candidate rejection"),
        (_MEMORY,),
        ("MemoryCandidateRejected",),
        ("no active MemoryCandidate",),
        (
            "tests/world_v2/test_memory_candidate_authority.py::test_pending_and_stale_candidates_are_suppressed_without_state_write",
        ),
    ),
    _fixture(
        "W2-MEM-002",
        "repeated boundary evidence creates source-bound retrievable memory",
        ("MemoryCandidate lifecycle", "source-bound retrieval", "privacy ceiling"),
        (
            _MEMORY,
            _anchor("src/companion_daemon/world_v2/memory_retrieval.py", "class MemoryRetrieval"),
        ),
        ("MemoryCandidateOpened", "MemoryCandidateAccepted"),
        ("active MemoryCandidate", "retrieval index"),
        (
            "tests/world_v2/test_memory_candidate_authority.py::test_memory_open_is_source_bound_and_zero_cascade",
            "tests/world_v2/test_memory_retrieval.py::test_fact_backed_memory_retrieval_uses_only_the_exact_persisted_assertion_text",
        ),
        coverage_tags=("memory",),
    ),
    _fixture(
        "W2-MEM-003",
        "reading memory never mutates strength or count",
        ("read-only selector", "zero-cascade retrieval"),
        (_anchor("src/companion_daemon/world_v2/memory_retrieval.py", "class MemoryRetrieval"),),
        (),
        ("unchanged MemoryCandidate head",),
        (
            "tests/world_v2/test_memory_candidate_authority.py::test_retrieval_accepts_only_exact_hardened_experience_authority",
        ),
    ),
    _fixture(
        "W2-MEM-004",
        "new evidence reinforcement is explicit and exact",
        ("reinforcement transition", "before/after policy"),
        (_MEMORY,),
        ("MemoryCandidateReinforced",),
        ("MemoryCandidate head",),
        (
            "tests/world_v2/test_memory_candidate_authority.py::test_reinforcement_strength_cannot_be_arbitrarily_reported",
        ),
    ),
    _fixture(
        "W2-MEM-005",
        "forget review emits an event while retaining history and sources",
        ("deliberative forget", "immutable history"),
        (_MEMORY,),
        ("MemoryCandidateForgotten",),
        ("forgotten MemoryCandidate history",),
        (
            "tests/world_v2/test_memory_candidate_authority.py::test_clock_forget_requires_latest_exact_clock_at_or_after_frozen_due",
        ),
    ),
    _fixture(
        "W2-MEM-006",
        "Fact withdrawal opens review rather than cascading memory deletion",
        ("cross-domain trigger", "zero cascade"),
        (
            _FACT,
            _MEMORY,
            _anchor(
                "src/companion_daemon/world_v2/memory_withdrawal_review.py",
                "class MemoryWithdrawalReviewRuntime",
                "MemorySourceInvalidationForgetAuthority",
                "TriggerProcessOpened",
            ),
            _anchor(
                "src/companion_daemon/world_v2/production_turn_application.py",
                "MemoryWithdrawalReviewRuntime(",
                "memory_model is not None",
            ),
        ),
        ("FactWithdrawn", "TriggerProcessOpened", "MemoryCandidateForgotten"),
        ("MemoryCandidate head", "terminal memory_candidate_review TriggerProcess"),
        (
            "tests/world_v2/test_fact_authority.py::test_fact_withdrawal_freezes_claim_and_closes_authority",
            "tests/world_v2/test_memory_candidate_authority.py::test_correction_cannot_drop_current_source_but_may_drop_stale_source",
            "tests/world_v2/test_memory_withdrawal_review_runtime.py::test_fact_reducer_does_not_cascade_but_review_forgets_exactly_once",
            "tests/world_v2/test_memory_withdrawal_review_runtime.py::test_production_builder_drains_withdrawal_review_when_memory_model_is_injected",
        ),
    ),
    _fixture(
        "W2-RHY-001",
        "defer matures through commitment and scheduled Action to a terminal receipt",
        ("shared main ReplyDraft", "atomic social defer", "logical clock", "Action pump"),
        (
            _SOCIAL,
            _anchor(
                "src/companion_daemon/world_v2/runtime.py",
                "self._social_action_worker.run_observation",
            ),
            _PUMP,
        ),
        ("PrivateCommitmentOpened", "ActionAuthorized", "ActionReceiptRecorded"),
        ("Commitment head", "terminal Action"),
        (
            "tests/world_v2/test_production_turn_application.py::test_production_shared_reply_audit_reaches_defer_without_second_model_call_and_restarts",
            "tests/world_v2/test_http_v2_host_migration.py::test_http_shared_reply_audit_reaches_deferred_followup_with_one_main_call",
            "tests/world_v2/test_qq_c2c_host_migration.py::test_qq_shared_reply_audit_reaches_deferred_followup_with_one_main_call",
            "tests/world_v2/test_social_action_vertical.py::test_delivered_followup_receipt_fulfills_exact_social_commitment",
        ),
    ),
    _fixture(
        "W2-RHY-002",
        "new user interjection reconsiders or cancels stale deferred output",
        ("interjection gate", "expression reconsideration", "budget release"),
        (
            _SOCIAL,
            _anchor(
                "src/companion_daemon/world_v2/production_turn_application.py",
                "expression_reconsideration_reviewer",
                "social_action_worker=",
            ),
            _RECONSIDER,
        ),
        ("ExpressionReconsiderationOpened", "ActionCancelled"),
        ("ExpressionPlan head", "Action head"),
        (
            "tests/world_v2/test_production_turn_application.py::test_production_user_interjection_cancels_shared_deferred_followup",
            "tests/world_v2/test_social_action_vertical.py::test_new_user_interjection_gates_and_can_cancel_deferred_followup",
        ),
    ),
    _fixture(
        "W2-RHY-003",
        "nonstreaming warm and cold chat trace ingress-to-visible-receipt segments",
        ("segmented latency trace", "incremental Context"),
        (
            _ECONOMY,
            _anchor(
                "src/companion_daemon/world_v2/production_latency_trace.py",
                "class ProductionLatencyRecorder",
            ),
            _anchor(
                "src/companion_daemon/world_v2/production_turn_application.py",
                "WorldV2TurnApplication",
            ),
        ),
        ("ObservationRecorded", "ActionReceiptRecorded"),
        ("latency trace",),
        (
            "tests/world_v2/test_production_latency_trace.py::test_ingress_startup_classification_is_atomic_and_duplicates_join",
            "tests/world_v2/test_platform_action_executor.py::test_platform_executor_records_real_dispatch_receipt_and_visible_boundaries",
            "tests/world_v2/test_production_performance_evidence.py::test_twenty_production_warm_turns_are_incremental_metered_and_under_offline_p95",
            "tests/world_v2/test_test_economy.py::test_latency_exporter_reports_percentiles_and_warm_speed_without_fake_network_slo",
        ),
        acceptance_kind="hybrid",
        external_gate="real provider warm/cold P95 requires a separately captured complete transport trace",
    ),
    _fixture(
        "W2-BEAT-001",
        "interjection reopens deliberation for all remaining beats",
        ("ExpressionPlan", "interjection gate", "reconsideration"),
        (_EXPRESSION, _RECONSIDER),
        ("ExpressionReconsiderationOpened",),
        ("gated remaining beats",),
        (
            "tests/world_v2/test_expression_reconsideration.py::test_interjection_gates_every_remaining_beat_of_a_multi_beat_plan_in_stable_order",
        ),
    ),
    _fixture(
        "W2-BEAT-002",
        "ordinary reply atomically binds one beat, payload and Action",
        ("ExpressionPlan acceptance", "payload sidecar", "Action authority"),
        (_EXPRESSION, _ACTION),
        ("ExpressionPlanOpened", "ExpressionBeatPlanned", "ActionAuthorized"),
        ("ExpressionPlan head", "Action projection"),
        (
            "tests/world_v2/test_expression_plan_acceptance.py::test_accepted_expression_plan_materializes_all_beats_actions_dependencies_and_delay",
        ),
    ),
    _fixture(
        "W2-BEAT-003",
        "provider acceptance of beat one does not complete a multibeat plan",
        ("per-beat receipt", "plan aggregation"),
        (
            _anchor(
                "src/companion_daemon/world_v2/expression_lifecycle_runtime.py",
                "class ExpressionReceiptLifecycle",
            ),
            _PUMP,
        ),
        ("ExpressionBeatSettled",),
        ("active ExpressionPlan",),
        (
            "tests/world_v2/test_expression_lifecycle_runtime.py::test_multibeat_plan_completes_only_after_the_last_independent_receipt",
        ),
    ),
    _fixture(
        "W2-BEAT-004",
        "concurrent scheduler and user interjection produce one CAS-bound reconsideration",
        ("deterministic trigger", "CAS gate", "immutable payload"),
        (_RECONSIDER,),
        ("ExpressionReconsiderationOpened",),
        ("unique reconsideration process",),
        (
            "tests/world_v2/test_expression_reconsideration.py::test_user_interjection_opens_one_deterministic_reconsideration_trigger_for_undispatched_beat",
            "tests/world_v2/test_expression_reconsideration.py::test_ingress_atomically_opens_reconsideration_gate_before_any_new_turn_work",
        ),
    ),
    _fixture(
        "W2-BEAT-005",
        "delayed beat clock wake still requires Action claim before dispatch",
        ("logical clock", "scheduler", "Action lease"),
        (_RECONSIDER, _PUMP),
        ("ClockAdvanced", "ActionClaimed"),
        ("scheduled Action",),
        (
            "tests/world_v2/test_action_lifecycle.py::test_action_claim_requires_a_finite_lease_atomically",
        ),
    ),
    _fixture(
        "W2-INT-001",
        "high-interest interruption advice may be accepted or rejected by the main model",
        ("semantic interrupt advisory", "model autonomy", "no keyword action"),
        (
            _anchor("src/companion_daemon/world_v2/semantic_advisory_adapter.py", "interrupt"),
            _anchor(
                "src/companion_daemon/world_v2/chat_model_deliberation_adapter.py",
                "RoutedChatModelDeliberationAdapter",
            ),
        ),
        ("ProposalRecorded",),
        ("advisory slice", "DecisionProposal audit"),
        (
            "tests/world_v2/test_semantic_advisory_adapter.py::test_semantic_adapter_returns_only_source_bound_catalog_alternatives",
            "tests/world_v2/test_production_same_turn_advisory.py::test_current_disappointment_and_thread_advice_reach_reply_model_without_forcing_comfort",
        ),
        coverage_tags=("resistance",),
    ),
    _fixture(
        "W2-INT-002",
        "interest plus high interruption cost may preserve a thread without interrupting",
        ("relationship-aware advisory", "Thread authority", "model choice"),
        (
            _anchor("src/companion_daemon/world_v2/semantic_advisory_adapter.py", "interrupt"),
            _anchor("src/companion_daemon/world_v2/thread_reducers.py", "ThreadOpened"),
        ),
        ("ThreadOpened", "ProposalRecorded"),
        ("Thread head", "no forced Action"),
        (
            "tests/world_v2/test_thread_authority.py::test_thread_open_is_typed_persistent_and_behavior_neutral",
            "tests/world_v2/test_semantic_advisory_adapter.py::test_semantic_adapter_returns_only_source_bound_catalog_alternatives",
        ),
    ),
    _fixture(
        "W2-PRO-001",
        "world-event proactive share uses proposal, proactive budget and terminal Action",
        (
            "settled-world-event opportunity",
            "model-owned now/later/silent choice",
            "proactive ExpressionPlan budget",
            "Action pump receipt",
        ),
        (_LIFE, _PROACTIVE, _EXPRESSION, _ACTION, _PUMP),
        ("ProposalRecorded", "BudgetReserved", "ActionReceiptRecorded"),
        ("proactive trigger process", "terminal proactive Action", "settled reservation"),
        (
            "tests/world_v2/test_life_projection.py::test_settled_world_occurrence_reaches_model_owned_proactive_action",
            "tests/world_v2/test_proactive_action_production.py::test_authorized_proactive_action_reaches_a_durable_delivery_receipt",
        ),
    ),
    _fixture(
        "W2-PRO-002",
        "exhausted proactive budget terminalizes intent without partial effects",
        ("proactive budget account", "atomic ExpressionPlan acceptance", "durable terminal trigger"),
        (_PROACTIVE, _EXPRESSION, _ACTION),
        ("ProposalRecorded", "TriggerProcessCompleted"),
        ("no partial Action", "terminal proactive trigger process"),
        (
            "tests/world_v2/test_proactive_action_production.py::test_exhausted_proactive_budget_abandons_with_a_durable_terminal_outcome",
            "tests/world_v2/test_proactive_action_production.py::test_sqlite_restart_resumes_open_proactive_process_once",
        ),
    ),
    _fixture(
        "W2-PULSE-001",
        "unfinished thought follows thread and commitment authority to a receipt",
        ("Thread continuity", "PrivateCommitment", "scheduled followup"),
        (_SOCIAL, _anchor("src/companion_daemon/world_v2/thread_reducers.py", "ThreadOpened")),
        ("ThreadOpened", "PrivateCommitmentOpened", "ActionReceiptRecorded"),
        ("Thread head", "Commitment head", "terminal Action"),
        (
            "tests/world_v2/test_social_action_vertical.py::test_delivered_followup_receipt_fulfills_exact_social_commitment",
            "tests/world_v2/test_http_v2_host_migration.py::test_http_shared_reply_audit_reaches_deferred_followup_with_one_main_call",
        ),
    ),
    _fixture(
        "W2-REA-001",
        "reaction suitability never bypasses a main-model no-action decision",
        ("single main-model ExpressionDraft", "deployment capability profile", "Action authority"),
        (
            _anchor(
                "src/companion_daemon/world_v2/expression_draft.py",
                "class ExpressionDraft",
                "timing_choice == \"silent\"",
                "QQ_NAPCAT_EXPRESSION_CAPABILITIES",
            ),
            _anchor(
                "src/companion_daemon/world_v2/qq_c2c_transport.py",
                "request.kind == \"reaction\"",
                "request.kind == \"sticker\"",
                "request.kind == \"typing\"",
            ),
            _ACTION,
        ),
        ("ProposalRecorded", "ActionProviderAccepted"),
        ("no Action after silent", "source-bound expression Action", "provider receipt"),
        (
            "tests/world_v2/test_qq_c2c_host_migration.py::test_napcat_main_model_can_refuse_every_available_expression_without_action",
            "tests/world_v2/test_qq_c2c_host_migration.py::test_napcat_expression_is_selected_by_the_single_main_model_and_reaches_delivery",
            "tests/world_v2/test_http_v2_host_migration.py::test_http_production_profile_fails_closed_when_model_selects_unavailable_reaction",
            "tests/world_v2/test_expression_nontext_acceptance.py::test_nontext_acceptance_rejects_model_envelope_redirecting_reaction_target",
        ),
    ),
    _fixture(
        "W2-ACT-001",
        "crash after provider acceptance reconciles without duplicate delivery",
        ("stable idempotency key", "provider lookup", "Action recovery"),
        (_PUMP, _ACTION),
        ("ActionDispatchStarted", "ActionReceiptRecorded"),
        ("terminal Action", "reconciliation inbox"),
        (
            "tests/world_v2/test_action_pump.py::test_started_idempotent_action_recovers_from_provider_lookup_without_redispatch",
        ),
    ),
    _fixture(
        "W2-ACT-002",
        "unknown receipt is terminal and never automatically retried",
        ("unknown terminal state", "reconciliation"),
        (_ACTION, _PUMP),
        ("ActionReceiptRecorded",),
        ("unknown Action",),
        (
            "tests/world_v2/test_action_lifecycle.py::test_unknown_is_terminal_and_a_later_delivery_cannot_reopen_the_action",
        ),
    ),
    _fixture(
        "W2-ACT-003",
        "duplicate and out-of-order receipts are effect-once or reconciled",
        ("receipt identity", "legal transition", "budget effect-once"),
        (_ACTION,),
        ("ActionReceiptRecorded", "BudgetSettled"),
        ("terminal Action", "reconciliation inbox"),
        (
            "tests/world_v2/test_action_lifecycle.py::test_same_provider_receipt_triple_under_new_source_event_has_no_second_effect",
            "tests/world_v2/test_action_lifecycle.py::test_provider_ref_reuse_under_a_new_source_event_enters_reconciliation",
        ),
    ),
    _fixture(
        "W2-ACT-004",
        "quick or parse fallback still becomes an accepted reply/defer Action",
        ("MinimalProposal", "Acceptance", "Action authority"),
        (
            _anchor(
                "src/companion_daemon/world_v2/minimal_reply_acceptance.py",
                "MinimalReplyAcceptance",
            ),
            _ACTION,
        ),
        ("AcceptanceRecorded", "ActionAuthorized"),
        ("Action projection",),
        (
            "tests/world_v2/test_minimal_reply_acceptance.py::test_recorder_materializes_a_closed_reply_batch_that_reduces_to_dispatchable_state",
        ),
    ),
    _fixture(
        "W2-MED-001",
        "media planning resumes the same frozen plan after crash",
        ("frozen MediaPlan", "stable planning attempt", "SQLite join"),
        (_MEDIA, _MEDIA_CONT),
        ("MediaPlanRecorded",),
        ("MediaPlan head",),
        (
            "tests/world_v2/test_media_continuation_runtime.py::test_sqlite_restart_joins_proposal_then_accepts_once",
            "tests/world_v2/test_media_v2_planning.py::test_media_freeze_replays_across_sqlite_restart_and_sidecar_ref_cannot_rebind",
        ),
    ),
    _fixture(
        "W2-MED-002",
        "inspection permits at most one same-plan repair then terminal failure",
        ("inspection authority", "one repair budget", "fail closed"),
        (_MEDIA,),
        ("MediaInspectionRecorded", "MediaRepairAuthorized", "MediaRenderFailed"),
        ("terminal media attempt",),
        (
            "tests/world_v2/test_media_v2_planning.py::test_repairable_inspection_has_one_source_bound_repair_then_second_failure_is_terminal",
        ),
    ),
    _fixture(
        "W2-MED-003",
        "failed delivery does not claim sharing or open an interaction bid",
        ("delivery receipt authority", "media share settlement"),
        (
            _MEDIA,
            _anchor(
                "src/companion_daemon/world_v2/media_delivery_runtime.py", "MediaDeliveryRuntime"
            ),
        ),
        ("ActionReceiptRecorded",),
        ("unshared media", "settled budget"),
        (
            "tests/world_v2/test_media_delivery_approval.py::test_settlement_uow_only_derives_media_share_from_delivered_receipt",
        ),
    ),
    _fixture(
        "W2-MED-004",
        "unknown media delivery enters reconciliation without resend",
        ("unknown terminal Action", "stable delivery key"),
        (_MEDIA, _PUMP),
        ("ActionReceiptRecorded",),
        ("generated media", "unknown Action"),
        (
            "tests/world_v2/test_action_pump.py::test_non_idempotent_started_action_becomes_unknown_without_redispatch",
        ),
    ),
    _fixture(
        "W2-MED-005",
        "crash after provider acceptance looks up the original stable key",
        ("provider lookup", "receipt-bound artifact"),
        (_MEDIA, _PUMP),
        ("ActionDispatchStarted", "MediaRenderArtifactRecorded"),
        ("media artifact", "terminal Action"),
        (
            "tests/world_v2/test_media_provider_results.py::test_worker_only_materializes_provider_bytes_bound_to_terminal_receipt",
            "tests/world_v2/test_action_pump.py::test_started_idempotent_action_recovers_from_provider_lookup_without_redispatch",
        ),
    ),
    _fixture(
        "W2-MED-006",
        "planner or inspector major upgrade invalidates automatic delivery approval",
        ("approval version binding", "preview fallback"),
        (_anchor("src/companion_daemon/world_v2/media_v2.py", "MediaAutomaticDeliveryApproval"),),
        ("MediaPreviewGenerated",),
        ("preview-only media",),
        (
            "tests/world_v2/test_media_delivery_approval.py::test_operator_approval_is_revisioned_and_invalidates_not_dispatched_action",
        ),
    ),
    _fixture(
        "W2-MED-007",
        "plan-render-inspect-delivery uses one accepted continuation per stage",
        ("continuation state machine", "per-stage Acceptance", "per-stage budget", "crash join"),
        (_MEDIA_CONT, _MEDIA),
        (
            "MediaPlanRecorded",
            "MediaRenderArtifactRecorded",
            "MediaInspectionRecorded",
            "ActionAuthorized",
        ),
        ("continuation processes", "media artifact", "Action projection"),
        (
            "tests/world_v2/test_media_continuation_runtime.py::test_render_artifact_opens_and_accepts_inspection_after_sqlite_restart",
            "tests/world_v2/test_media_continuation_runtime.py::test_competing_acceptance_has_one_action_and_loser_joins",
        ),
    ),
    _fixture(
        "W2-CMEDIA-001",
        "creative image request stays in creative pipeline without a lived Experience",
        ("creative media lane", "world/media separation"),
        (_MEDIA,),
        ("MediaPlanRecorded",),
        ("creative artifact", "no Experience"),
        (
            "tests/world_v2/test_media_v2_planning.py::test_candidate_and_opportunity_are_bound_only_to_prior_committed_events",
        ),
    ),
    _fixture(
        "W2-VIS-001",
        "uncertain visual result is bounded and opens one external-result consumer",
        ("perception authorization", "result trigger", "visible evidence bound"),
        (
            _PERCEPTION,
            _anchor(
                "src/companion_daemon/world_v2/perception_result_trigger_runtime.py",
                "PerceptionResultTriggerRuntime",
            ),
        ),
        ("VisionResultAccepted", "TriggerProcessOpened"),
        ("perception result", "external-result process"),
        (
            "tests/world_v2/test_perception_vertical.py::test_injected_perception_provider_is_source_bound_private_and_result_triggered_once",
        ),
    ),
    _fixture(
        "W2-TOOL-001",
        "read-only tool receipt replay opens one result decision and settles once",
        ("tool capability", "receipt-bound result", "external-result trigger"),
        (
            _TOOL,
            _anchor(
                "src/companion_daemon/world_v2/external_result_trigger_runtime.py",
                "ExternalResultTriggerRuntime",
            ),
        ),
        ("ToolResultAccepted", "TriggerProcessOpened", "BudgetSettled"),
        ("tool result", "external-result process"),
        (
            "tests/world_v2/test_read_only_tool_vertical.py::test_source_bound_tool_request_settles_result_and_opens_one_result_trigger",
        ),
    ),
    _fixture(
        "W2-PROJ-001",
        "viewer projection is capability-redacted and read-only",
        ("viewer grants", "privacy ceiling", "pure projection"),
        (_PROJECTION,),
        (),
        ("viewer-specific projection",),
        (
            "tests/world_v2/test_projection_policy.py::test_projection_is_bounded_and_has_no_ledger_side_effects",
            "tests/world_v2/test_projection_policy.py::test_projection_grants_reject_cross_viewer_permissions",
        ),
    ),
    _fixture(
        "W2-COST-001",
        "chat route model calls and tokens obey the declared profile",
        ("semantic route accounting", "cost profile gate"),
        (
            _ECONOMY,
            _anchor(
                "src/companion_daemon/world_v2/production_performance_evidence.py",
                "class ProductionPerformanceEvidenceReader",
            ),
            _anchor(
                "src/companion_daemon/world_v2/semantic_compute_router.py",
                "class SemanticComputeRouter",
            ),
        ),
        ("ProposalRecorded",),
        ("model call trace",),
        (
            "tests/world_v2/test_production_performance_evidence.py::test_twenty_production_warm_turns_are_incremental_metered_and_under_offline_p95",
            "tests/world_v2/test_production_performance_evidence.py::test_production_trace_restart_and_duplicate_do_not_repeat_model_or_rebind_trace",
            "tests/world_v2/test_test_economy.py::test_fixed_profile_accepts_one_metered_flash_chat_call",
            "tests/world_v2/test_test_economy.py::test_profile_rejects_extra_chat_call_and_missing_thinking_accounting",
        ),
        acceptance_kind="hybrid",
        external_gate="provider invoice and production-token reconciliation require real provider artifacts",
    ),
    _fixture(
        "W2-COST-002",
        "failed reserved Action settles or releases budget exactly once",
        ("budget reservation", "terminal settlement", "effect-once"),
        (_ACTION, _PUMP),
        ("BudgetReserved", "BudgetReleased"),
        ("budget account", "failed Action"),
        (
            "tests/world_v2/test_action_pump.py::test_expired_action_releases_its_budget_without_dispatch",
            "tests/world_v2/test_action_lifecycle.py::test_terminal_settlement_atomically_records_receipt_budget_and_completion",
        ),
    ),
    _fixture(
        "W2-REP-001",
        "full-ledger replay performs no external call and reproduces hashes and draws",
        ("deterministic reducer", "recorded model result", "recorded random draw"),
        (
            _REPLAY,
            _anchor("src/companion_daemon/world_v2/random_authority.py", "RandomDrawRecorded"),
        ),
        ("RandomDrawRecorded",),
        ("replay projection hash",),
        (
            "tests/world_v2/test_replay_evaluator.py::test_replay_evaluator_accepts_identical_deterministic_rebuild",
            "tests/world_v2/test_random_authority.py::test_draw_is_canonical_replayable_and_persisted_once",
        ),
        coverage_tags=("controlled_random",),
    ),
    _fixture(
        "W2-PERF-001",
        "twenty warm ordinary turns use incremental Context and meet P95 target",
        ("incremental projection", "segmented latency", "performance gate"),
        (
            _ECONOMY,
            _anchor(
                "src/companion_daemon/world_v2/production_performance_evidence.py",
                "class WarmChatPerformanceGate",
            ),
            _anchor(
                "src/companion_daemon/world_v2/sqlite_ledger.py",
                "class SQLiteProjectionPerformanceCounters",
            ),
        ),
        ("ObservationRecorded", "ActionReceiptRecorded"),
        ("latency percentile report",),
        (
            "tests/world_v2/test_production_performance_evidence.py::test_twenty_production_warm_turns_are_incremental_metered_and_under_offline_p95",
            "tests/world_v2/test_sqlite_ledger.py::test_verified_lookup_tracks_same_process_head_without_replay",
            "tests/world_v2/test_sqlite_ledger.py::test_verified_lookup_revalidates_cross_connection_change_fail_closed",
            "tests/world_v2/test_test_economy.py::test_latency_exporter_reports_percentiles_and_warm_speed_without_fake_network_slo",
            "tests/world_v2/test_test_economy.py::test_real_transport_trace_is_neither_mixed_with_offline_nor_called_measured_when_incomplete",
        ),
        acceptance_kind="hybrid",
        external_gate="P95 <= 5s needs 20 complete real-transport warm samples in the target deployment",
    ),
    _fixture(
        "W2-ARCH-001",
        "import graph contains only allowed architecture edges and no legacy authority",
        ("static import graph", "v2/archive isolation"),
        (
            _anchor(
                "src/companion_daemon/world_v2/platform_architecture_guard.py",
                "def assert_v2_platform_architecture",
            ),
        ),
        (),
        ("architecture report",),
        (
            "tests/world_v2/test_architecture_contract.py::test_world_v2_does_not_import_legacy_or_platform_authorities",
            "tests/world_v2/test_platform_reverse_architecture_guard.py::test_selected_v2_platform_paths_do_not_reach_legacy_runtime_authority",
        ),
        production_reachability="ci_gate",
        reachability_note="architecture acceptance is an executable static CI gate, not a runtime production route",
    ),
)


def export_fixture_acceptance_manifest() -> dict[str, object]:
    """Return a JSON-serializable, fully expanded manifest."""

    return {
        "version": FIXTURE_ACCEPTANCE_MANIFEST_VERSION,
        "fixtures": [asdict(item) for item in FIXTURE_ACCEPTANCE_MANIFEST],
    }


__all__ = [
    "FIXTURE_ACCEPTANCE_MANIFEST",
    "FIXTURE_ACCEPTANCE_MANIFEST_VERSION",
    "FixtureAcceptance",
    "ProductionAnchor",
    "export_fixture_acceptance_manifest",
]
