"""Auditable, read-only evaluation for World v2 companion experience.

The evaluator consumes externally captured and reviewed outputs.  It neither
calls a judge model nor infers that prose is human-like from surface heuristics;
missing corpus, annotations, or baseline runs is a visible gate failure.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from random import Random
from statistics import fmean
from typing import Literal, Mapping


RUBRIC_DIMENSIONS = (
    "current_input_fit",
    "subtext_awareness",
    "subjectivity",
    "continuity",
    "non_scriptedness",
    "fact_safety",
    "world_synchronicity",
)

OFFICIAL_PROTOCOL_VERSION = "human-likeness-eval-v1"
OFFICIAL_VARIANTS = ("bare", "archived", "v2")
OFFICIAL_SCENARIO_FAMILIES = frozenset(
    {
        "ordinary_share",
        "question_loop",
        "mild_disappointment",
        "explicit_offence",
        "subtext_sarcasm",
        "hurt_residue",
        "distant_relationship",
        "repair",
        "npc_world_impact",
        "plan_change",
        "procrastination",
        "reply_later",
        "interruption",
        "multi_segment",
        "media_opportunity",
        "provider_timeout",
        "projection_gap",
    }
)


class ExperienceEvaluationError(ValueError):
    """Evaluation evidence cannot support a reproducible comparison."""


@dataclass(frozen=True, slots=True)
class EvaluationProtocol:
    """Versions and coverage requirements for one comparable evaluation run."""

    protocol_version: str
    scenario_set_version: str
    rubric_version: str
    statistics_version: str
    required_variants: tuple[str, ...]
    required_repetitions: int
    minimum_scenario_turns: int
    minimum_emotion_gold_turns: int
    minimum_independent_reviews: int = 2
    judge_model_id: str | None = None
    judge_prompt_version: str | None = None
    judge_temperature: float | None = None
    blinding_scheme_version: str | None = None
    blinded_order_hash: str | None = None
    unblinding_map_hash: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.protocol_version,
                self.scenario_set_version,
                self.rubric_version,
                self.statistics_version,
            )
        ):
            raise ExperienceEvaluationError("protocol versions are required")
        if not self.required_variants or any(not item.strip() for item in self.required_variants):
            raise ExperienceEvaluationError("at least one non-empty variant is required")
        if len(set(self.required_variants)) != len(self.required_variants):
            raise ExperienceEvaluationError("required variants must be unique")
        if self.required_repetitions < 1:
            raise ExperienceEvaluationError("required repetitions must be positive")
        if self.minimum_scenario_turns < 1 or self.minimum_emotion_gold_turns < 0:
            raise ExperienceEvaluationError("scenario coverage requirements are invalid")
        if self.minimum_emotion_gold_turns > self.minimum_scenario_turns:
            raise ExperienceEvaluationError("emotion gold requirement exceeds scenario requirement")
        if self.minimum_independent_reviews < 2:
            raise ExperienceEvaluationError("at least two independent reviews are required")
        for name, value in (
            ("blinded order hash", self.blinded_order_hash),
            ("unblinding map hash", self.unblinding_map_hash),
        ):
            if value is not None and (
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ExperienceEvaluationError(f"{name} must be a lowercase sha256 hex digest")


@dataclass(frozen=True, slots=True)
class ScenarioTurn:
    """One fixed input turn and its allowed, non-prescriptive response traces."""

    scenario_turn_id: str
    scenario_id: str
    emotional_gold: bool
    acceptable_response_tags: tuple[str, ...]
    scenario_family: str = ""
    input_hash: str = ""
    fact_set_hash: str = ""

    def __post_init__(self) -> None:
        if not self.scenario_turn_id.strip() or not self.scenario_id.strip():
            raise ExperienceEvaluationError("scenario and scenario-turn ids are required")
        if self.emotional_gold and not self.acceptable_response_tags:
            raise ExperienceEvaluationError("emotion gold turn requires acceptable response tags")
        if any(not item.strip() for item in self.acceptable_response_tags):
            raise ExperienceEvaluationError("response tags must be non-empty")
        if len(set(self.acceptable_response_tags)) != len(self.acceptable_response_tags):
            raise ExperienceEvaluationError("acceptable response tags must be unique")
        for name, value in (("input hash", self.input_hash), ("fact-set hash", self.fact_set_hash)):
            if value and (
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ExperienceEvaluationError(f"{name} must be a lowercase sha256 hex digest")


@dataclass(frozen=True, slots=True)
class AwarenessEvidence:
    """Reference proving where a reviewer observed an emotion response trace."""

    source: Literal["output", "proposal", "affect_episode"]
    reference_id: str
    response_tags: tuple[str, ...]
    output_hash: str

    def __post_init__(self) -> None:
        if not self.reference_id.strip():
            raise ExperienceEvaluationError("awareness evidence reference is required")
        if not self.response_tags or any(not tag.strip() for tag in self.response_tags):
            raise ExperienceEvaluationError("awareness evidence needs non-empty response tags")
        if len(self.output_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.output_hash
        ):
            raise ExperienceEvaluationError("awareness evidence must bind a lowercase sha256 output hash")


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    """Hash-addressed exported artifact from output, Proposal, or AffectEpisode."""

    source: Literal["output", "proposal", "affect_episode"]
    reference_id: str
    output_hash: str
    artifact_hash: str
    variant_id: str = ""
    scenario_turn_id: str = ""
    seed: str = ""

    def __post_init__(self) -> None:
        if not self.reference_id.strip():
            raise ExperienceEvaluationError("evidence artifact reference is required")
        for name, value in (("output hash", self.output_hash), ("artifact hash", self.artifact_hash)):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ExperienceEvaluationError(f"evidence artifact {name} must be a lowercase sha256 hex digest")
        identity = (self.variant_id, self.scenario_turn_id, self.seed)
        if any(identity) and not all(item.strip() for item in identity):
            raise ExperienceEvaluationError("evidence artifact binding must include variant, turn, and seed")


@dataclass(frozen=True, slots=True)
class ReviewedRun:
    """One blinded reviewer record for one variant output.

    ``response_tags`` are reviewer observations, not directives imposed on the
    character.  In particular, an emotion-gold turn may be noticed by giving
    space, disagreeing, repairing, or deliberately not intervening.
    """

    variant_id: str
    scenario_turn_id: str
    seed: str
    output_hash: str
    judge_id: str
    judge_prompt_version: str
    rubric_scores: Mapping[str, int]
    response_tags: tuple[str, ...]
    question_ending: bool
    fallback_template_hit: bool
    fallback_smell_confirmed: bool
    model_failed: bool
    asserted_alternative_as_fact: bool
    judge_model_id: str | None = None
    awareness_evidence: tuple[AwarenessEvidence, ...] = ()
    scenario_input_hash: str = ""
    scenario_fact_set_hash: str = ""
    blind_output_id: str = ""
    reply_eligible: bool = True
    non_necessary_question_ending: bool | None = None
    used_fact_refs: tuple[str, ...] = ()
    action_refs: tuple[str, ...] = ()
    affect_episode_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.variant_id,
                self.scenario_turn_id,
                self.seed,
                self.judge_id,
                self.judge_prompt_version,
            )
        ):
            raise ExperienceEvaluationError("reviewed run identity is required")
        if len(self.output_hash) != 64 or any(ch not in "0123456789abcdef" for ch in self.output_hash):
            raise ExperienceEvaluationError("output hash must be a lowercase sha256 hex digest")
        if tuple(sorted(self.rubric_scores)) != tuple(sorted(RUBRIC_DIMENSIONS)):
            raise ExperienceEvaluationError("review must score every rubric dimension exactly once")
        if any(
            not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5
            for score in self.rubric_scores.values()
        ):
            raise ExperienceEvaluationError("rubric scores must be integers from 1 to 5")
        if any(not item.strip() for item in self.response_tags):
            raise ExperienceEvaluationError("response tags must be non-empty")
        if self.fallback_smell_confirmed and not self.fallback_template_hit:
            raise ExperienceEvaluationError("confirmed fallback smell requires a template hit")
        if self.judge_model_id is not None and not self.judge_model_id.strip():
            raise ExperienceEvaluationError("judge model id must be non-empty when provided")
        if any(item.output_hash != self.output_hash for item in self.awareness_evidence):
            raise ExperienceEvaluationError("awareness evidence must bind this reviewed output hash")
        for name, value in (
            ("scenario input hash", self.scenario_input_hash),
            ("scenario fact-set hash", self.scenario_fact_set_hash),
        ):
            if value and (
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ExperienceEvaluationError(f"{name} must be a lowercase sha256 hex digest")
        if any(
            not reference.strip()
            for references in (self.used_fact_refs, self.action_refs, self.affect_episode_refs)
            for reference in references
        ):
            raise ExperienceEvaluationError("experience evidence references must be non-empty")


@dataclass(frozen=True, slots=True)
class VariantMetrics:
    reviewed_turns: int
    human_likeness: float
    emotional_awareness_recall: float | None
    question_loop_rate: float | None
    fallback_smell_rate: float | None


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """Paired candidate-minus-baseline difference with deterministic bootstrap CI."""

    metric: str
    paired_units: int
    baseline_mean: float
    candidate_mean: float
    difference: float
    ci_lower: float
    ci_upper: float


@dataclass(frozen=True, slots=True)
class MechanicalEvaluation:
    """Scenario-suite evidence supplied by replay and performance adapters."""

    hard_invariant_violations: int
    nonterminal_action_leaks: int
    replay_hash_mismatches: int
    affect_episode_invalid_clears: int
    random_draw_replay_consistency: float
    hot_visible_action_p95_ms: float
    fixture_manifest_hash: str = ""
    replay_evidence_hash: str = ""
    action_receipt_evidence_hash: str = ""
    affect_evidence_hash: str = ""
    random_draw_evidence_hash: str = ""
    performance_trace_hash: str = ""

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                self.hard_invariant_violations,
                self.nonterminal_action_leaks,
                self.replay_hash_mismatches,
                self.affect_episode_invalid_clears,
            )
        ):
            raise ExperienceEvaluationError("mechanical failure counts must be non-negative integers")
        if not 0 <= self.random_draw_replay_consistency <= 1:
            raise ExperienceEvaluationError("random draw replay consistency must be between zero and one")
        if self.hot_visible_action_p95_ms < 0:
            raise ExperienceEvaluationError("hot visible action p95 must be non-negative")
        for name, value in (
            ("fixture manifest hash", self.fixture_manifest_hash),
            ("replay evidence hash", self.replay_evidence_hash),
            ("action receipt evidence hash", self.action_receipt_evidence_hash),
            ("affect evidence hash", self.affect_evidence_hash),
            ("random draw evidence hash", self.random_draw_evidence_hash),
            ("performance trace hash", self.performance_trace_hash),
        ):
            if value and (
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ExperienceEvaluationError(f"{name} must be a lowercase sha256 hex digest")


@dataclass(frozen=True, slots=True)
class EvaluationIssue:
    """One scenario-turn diagnostic with only auditable references, never prose."""

    variant_id: str
    scenario_turn_id: str
    seed: str
    code: str
    severity: Literal["medium", "high"]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VariantEvidence:
    used_fact_refs: tuple[str, ...]
    action_refs: tuple[str, ...]
    affect_episode_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExperienceEvaluationReport:
    evaluator_version: str
    protocol_version: str
    scenario_set_version: str
    rubric_version: str
    statistics_version: str
    variant_metrics: Mapping[str, VariantMetrics]
    comparisons: Mapping[str, MetricComparison]
    mechanical_evaluation: MechanicalEvaluation | None
    issues: tuple[EvaluationIssue, ...]
    evidence_by_variant: Mapping[str, VariantEvidence]
    blockers: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.blockers


class ExperienceEvaluator:
    """Deterministically turn reviewed runs into an explicitly limited report."""

    version = "world-v2-experience-evaluator.1"

    def evaluate(
        self,
        *,
        protocol: EvaluationProtocol,
        corpus: tuple[ScenarioTurn, ...],
        reviewed_runs: tuple[ReviewedRun, ...],
        mechanical_evaluation: MechanicalEvaluation | None = None,
        evidence_artifacts: tuple[EvidenceArtifact, ...] = (),
    ) -> ExperienceEvaluationReport:
        scenarios = self._index_corpus(corpus)
        grouped = self._index_runs(scenarios, reviewed_runs)
        artifact_keys = self._index_artifacts(evidence_artifacts)
        blockers = self._coverage_blockers(protocol, corpus, grouped)
        blockers.extend(
            self._official_protocol_blockers(
                protocol, corpus, reviewed_runs, grouped, evidence_artifacts
            )
        )
        comparisons = self._compare_variants(protocol, grouped)
        blockers.extend(
            self._official_gate_blockers(
                protocol,
                scenarios,
                grouped,
                comparisons,
                mechanical_evaluation,
                artifact_keys,
            )
        )
        metrics = {
            variant: self._metrics_for_runs(grouped.get(variant, ()), scenarios, artifact_keys)
            for variant in protocol.required_variants
            if grouped.get(variant)
        }
        issues = self._issues_for_runs(grouped, scenarios, artifact_keys)
        evidence_by_variant = self._variant_evidence(grouped)
        return ExperienceEvaluationReport(
            evaluator_version=self.version,
            protocol_version=protocol.protocol_version,
            scenario_set_version=protocol.scenario_set_version,
            rubric_version=protocol.rubric_version,
            statistics_version=protocol.statistics_version,
            variant_metrics=metrics,
            comparisons=comparisons,
            mechanical_evaluation=mechanical_evaluation,
            issues=issues,
            evidence_by_variant=evidence_by_variant,
            blockers=tuple(blockers),
        )

    @staticmethod
    def _official_protocol_blockers(
        protocol: EvaluationProtocol,
        corpus: tuple[ScenarioTurn, ...],
        reviewed_runs: tuple[ReviewedRun, ...],
        grouped: Mapping[str, tuple[ReviewedRun, ...]],
        evidence_artifacts: tuple[EvidenceArtifact, ...],
    ) -> list[str]:
        """Keep lightweight experiments possible without weakening the named protocol."""

        if protocol.protocol_version != OFFICIAL_PROTOCOL_VERSION:
            return []
        blockers: list[str] = []
        if protocol.required_variants != OFFICIAL_VARIANTS:
            blockers.append("official_protocol_requires_bare_archived_v2")
        if protocol.required_repetitions < 3:
            blockers.append("official_protocol_requires_three_seeds")
        if protocol.minimum_scenario_turns < 120:
            blockers.append("official_protocol_requires_120_scenario_turns")
        if protocol.minimum_emotion_gold_turns < 40:
            blockers.append("official_protocol_requires_40_emotion_gold_turns")
        if not protocol.judge_model_id or not protocol.judge_prompt_version:
            blockers.append("official_protocol_requires_fixed_judge_identity")
        if protocol.judge_temperature != 0:
            blockers.append("official_protocol_requires_zero_temperature_judge")
        if not protocol.blinding_scheme_version or not protocol.blinded_order_hash or not protocol.unblinding_map_hash:
            blockers.append("official_protocol_requires_blinding_manifest")
        families = {item.scenario_family for item in corpus}
        missing_families = sorted(OFFICIAL_SCENARIO_FAMILIES - families)
        if missing_families:
            blockers.append("missing_required_scenario_families")
        for run in reviewed_runs:
            if protocol.judge_model_id and run.judge_model_id != protocol.judge_model_id:
                blockers.append("judge_identity_mismatch")
                break
            if protocol.judge_prompt_version and run.judge_prompt_version != protocol.judge_prompt_version:
                blockers.append("judge_prompt_version_mismatch")
                break
        if any(not item.input_hash or not item.fact_set_hash for item in corpus):
            blockers.append("missing_scenario_input_or_fact_hash")
        if any(
            not run.scenario_input_hash or not run.scenario_fact_set_hash
            for run in reviewed_runs
        ):
            blockers.append("missing_run_input_or_fact_hash")
        if any(not run.blind_output_id for run in reviewed_runs):
            blockers.append("missing_blind_output_identity")
        if any(run.non_necessary_question_ending is None for run in reviewed_runs):
            blockers.append("missing_question_necessity_annotation")
        output_artifact_bindings = {
            (item.variant_id, item.scenario_turn_id, item.seed, item.output_hash)
            for item in evidence_artifacts
            if item.source == "output"
        }
        required_output_bindings = {
            (run.variant_id, run.scenario_turn_id, run.seed, run.output_hash)
            for run in reviewed_runs
        }
        if not required_output_bindings.issubset(output_artifact_bindings):
            blockers.append("missing_output_artifact_manifest")
        unit_sets = {
            variant: {(item.scenario_turn_id, item.seed) for item in grouped.get(variant, ())}
            for variant in OFFICIAL_VARIANTS
        }
        if any(unit_sets[variant] != unit_sets["bare"] for variant in OFFICIAL_VARIANTS[1:]):
            blockers.append("variant_sample_matrix_mismatch")
        reviewer_sets = {
            variant: {
                key: frozenset(
                    item.judge_id
                    for item in grouped.get(variant, ())
                    if (item.scenario_turn_id, item.seed) == key
                )
                for key in unit_sets[variant]
            }
            for variant in OFFICIAL_VARIANTS
        }
        if any(reviewer_sets[variant] != reviewer_sets["bare"] for variant in OFFICIAL_VARIANTS[1:]):
            blockers.append("variant_reviewer_panel_mismatch")
        return blockers

    @staticmethod
    def _index_corpus(corpus: tuple[ScenarioTurn, ...]) -> dict[str, ScenarioTurn]:
        scenarios = {item.scenario_turn_id: item for item in corpus}
        if len(scenarios) != len(corpus):
            raise ExperienceEvaluationError("scenario-turn ids must be unique")
        return scenarios

    @staticmethod
    def _index_runs(
        scenarios: Mapping[str, ScenarioTurn], reviewed_runs: tuple[ReviewedRun, ...]
    ) -> dict[str, tuple[ReviewedRun, ...]]:
        grouped: dict[str, list[ReviewedRun]] = {}
        seen: set[tuple[str, str, str, str]] = set()
        for run in reviewed_runs:
            scenario = scenarios.get(run.scenario_turn_id)
            if scenario is None:
                raise ExperienceEvaluationError("review references a scenario-turn outside the corpus")
            if run.scenario_input_hash and run.scenario_input_hash != scenario.input_hash:
                raise ExperienceEvaluationError("review input hash does not match its scenario-turn")
            if run.scenario_fact_set_hash and run.scenario_fact_set_hash != scenario.fact_set_hash:
                raise ExperienceEvaluationError("review fact-set hash does not match its scenario-turn")
            identity = (run.variant_id, run.scenario_turn_id, run.seed, run.judge_id)
            if identity in seen:
                raise ExperienceEvaluationError("duplicate reviewer record for variant, turn, seed, and judge")
            seen.add(identity)
            grouped.setdefault(run.variant_id, []).append(run)
        for variant, reviews in grouped.items():
            output_hashes: dict[tuple[str, str], set[str]] = {}
            eligibility: dict[tuple[str, str], set[bool]] = {}
            for review in reviews:
                output_hashes.setdefault(
                    (review.scenario_turn_id, review.seed), set()
                ).add(review.output_hash)
                eligibility.setdefault(
                    (review.scenario_turn_id, review.seed), set()
                ).add(review.reply_eligible)
            if any(len(hashes) != 1 for hashes in output_hashes.values()):
                raise ExperienceEvaluationError(
                    f"independent reviewers must reference one output hash per unit ({variant})"
                )
            if any(len(values) != 1 for values in eligibility.values()):
                raise ExperienceEvaluationError(
                    f"independent reviewers must agree on reply eligibility ({variant})"
                )
        return {variant: tuple(items) for variant, items in grouped.items()}

    @staticmethod
    def _index_artifacts(
        evidence_artifacts: tuple[EvidenceArtifact, ...]
    ) -> frozenset[tuple[str, str, str]]:
        keys = {
            (item.source, item.reference_id, item.output_hash)
            for item in evidence_artifacts
        }
        if len(keys) != len(evidence_artifacts):
            raise ExperienceEvaluationError("evidence artifacts must have unique source/reference/output bindings")
        return frozenset(keys)

    @staticmethod
    def _coverage_blockers(
        protocol: EvaluationProtocol,
        corpus: tuple[ScenarioTurn, ...],
        grouped: Mapping[str, tuple[ReviewedRun, ...]],
    ) -> list[str]:
        blockers: list[str] = []
        if len(corpus) < protocol.minimum_scenario_turns:
            blockers.append("insufficient_scenario_turns")
        if sum(item.emotional_gold for item in corpus) < protocol.minimum_emotion_gold_turns:
            blockers.append("insufficient_emotion_gold_turns")
        for variant in protocol.required_variants:
            runs = grouped.get(variant, ())
            if not runs:
                blockers.append(f"missing_variant_runs:{variant}")
                continue
            present = {(item.scenario_turn_id, item.seed) for item in runs}
            for scenario in corpus:
                seeds = sorted(
                    seed for turn_id, seed in present if turn_id == scenario.scenario_turn_id
                )
                if len(seeds) < protocol.required_repetitions:
                    blockers.append(f"insufficient_repetitions:{variant}:{scenario.scenario_turn_id}")
                for seed in seeds:
                    reviewers = {
                        item.judge_id
                        for item in runs
                        if item.scenario_turn_id == scenario.scenario_turn_id and item.seed == seed
                    }
                    if len(reviewers) < protocol.minimum_independent_reviews:
                        blockers.append(
                            f"insufficient_independent_reviews:{variant}:{scenario.scenario_turn_id}:{seed}"
                        )
        return blockers

    @staticmethod
    def _metrics_for_runs(
        runs: tuple[ReviewedRun, ...],
        scenarios: Mapping[str, ScenarioTurn],
        artifact_keys: frozenset[tuple[str, str, str]],
    ) -> VariantMetrics:
        units: dict[tuple[str, str], list[ReviewedRun]] = {}
        for run in runs:
            units.setdefault((run.scenario_turn_id, run.seed), []).append(run)
        unit_scores = [
            fmean(score for review in reviews for score in review.rubric_scores.values())
            for reviews in units.values()
        ]
        emotional_units = [
            (key, reviews)
            for key, reviews in units.items()
            if scenarios[key[0]].emotional_gold
        ]
        noticed_units = [
            key
            for key, reviews in emotional_units
            if sum(
                not review.asserted_alternative_as_fact
                and any(
                    set(evidence.response_tags)
                    & set(scenarios[key[0]].acceptable_response_tags)
                    for evidence in review.awareness_evidence
                    if (evidence.source, evidence.reference_id, evidence.output_hash) in artifact_keys
                )
                for review in reviews
            )
            > len(reviews) / 2
        ]
        question_units = [
            ExperienceEvaluator._question_loop_unit(reviews)
            for reviews in units.values()
        ]
        failed_units = [
            reviews for reviews in units.values() if any(review.model_failed for review in reviews)
        ]
        return VariantMetrics(
            reviewed_turns=len(units),
            human_likeness=round((fmean(unit_scores) - 1) / 4, 4) if unit_scores else 0.0,
            emotional_awareness_recall=(
                round(len(noticed_units) / len(emotional_units), 4) if emotional_units else None
            ),
            question_loop_rate=(
                round(fmean(value for value in question_units if value is not None), 4)
                if any(value is not None for value in question_units)
                else None
            ),
            fallback_smell_rate=(
                round(
                    fmean(
                        fmean(
                            review.fallback_template_hit and review.fallback_smell_confirmed
                            for review in reviews
                        )
                        for reviews in failed_units
                    ),
                    4,
                )
                if failed_units
                else None
            ),
        )

    @staticmethod
    def _question_loop_unit(reviews: list[ReviewedRun]) -> bool | None:
        if not reviews[0].reply_eligible:
            return None
        labels = [
            review.question_ending
            if review.non_necessary_question_ending is None
            else review.non_necessary_question_ending
            for review in reviews
        ]
        return sum(labels) > len(labels) / 2

    @staticmethod
    def _issues_for_runs(
        grouped: Mapping[str, tuple[ReviewedRun, ...]],
        scenarios: Mapping[str, ScenarioTurn],
        artifact_keys: frozenset[tuple[str, str, str]],
    ) -> tuple[EvaluationIssue, ...]:
        issues: dict[tuple[str, str, str, str], EvaluationIssue] = {}
        for variant, runs in grouped.items():
            for run in runs:
                scenario = scenarios[run.scenario_turn_id]
                evidence_refs = tuple(item.reference_id for item in run.awareness_evidence)
                if scenario.emotional_gold and not any(
                    set(evidence.response_tags) & set(scenario.acceptable_response_tags)
                    for evidence in run.awareness_evidence
                    if (evidence.source, evidence.reference_id, evidence.output_hash) in artifact_keys
                ):
                    key = (variant, run.scenario_turn_id, run.seed, "missing_emotional_awareness_evidence")
                    issues[key] = EvaluationIssue(
                        variant,
                        run.scenario_turn_id,
                        run.seed,
                        "missing_emotional_awareness_evidence",
                        "high",
                        evidence_refs,
                    )
                if run.asserted_alternative_as_fact:
                    key = (variant, run.scenario_turn_id, run.seed, "alternative_interpretation_asserted_as_fact")
                    issues[key] = EvaluationIssue(
                        variant,
                        run.scenario_turn_id,
                        run.seed,
                        "alternative_interpretation_asserted_as_fact",
                        "high",
                        evidence_refs,
                    )
                if run.model_failed and run.fallback_template_hit and run.fallback_smell_confirmed:
                    key = (variant, run.scenario_turn_id, run.seed, "confirmed_fallback_smell")
                    issues[key] = EvaluationIssue(
                        variant,
                        run.scenario_turn_id,
                        run.seed,
                        "confirmed_fallback_smell",
                        "medium",
                        evidence_refs,
                    )
        return tuple(issues[key] for key in sorted(issues))

    @staticmethod
    def _variant_evidence(
        grouped: Mapping[str, tuple[ReviewedRun, ...]]
    ) -> dict[str, VariantEvidence]:
        return {
            variant: VariantEvidence(
                used_fact_refs=tuple(sorted({ref for run in runs for ref in run.used_fact_refs})),
                action_refs=tuple(sorted({ref for run in runs for ref in run.action_refs})),
                affect_episode_refs=tuple(
                    sorted({ref for run in runs for ref in run.affect_episode_refs})
                ),
            )
            for variant, runs in grouped.items()
        }

    def _compare_variants(
        self,
        protocol: EvaluationProtocol,
        grouped: Mapping[str, tuple[ReviewedRun, ...]],
    ) -> dict[str, MetricComparison]:
        if "bare" not in grouped or "v2" not in grouped:
            return {}
        bare = self._aggregate_units(grouped["bare"])
        candidate = self._aggregate_units(grouped["v2"])
        shared = tuple(sorted(set(bare) & set(candidate)))
        comparisons: dict[str, MetricComparison] = {}
        for metric in (
            "human_likeness",
            "continuity",
            "fact_safety",
            "world_synchronicity",
            "question_loop_rate",
            "fallback_smell_rate",
        ):
            pairs = [
                (bare[key].get(metric), candidate[key].get(metric))
                for key in shared
            ]
            values = [(baseline, current) for baseline, current in pairs if baseline is not None and current is not None]
            if not values:
                continue
            baseline_values = tuple(float(item[0]) for item in values)
            candidate_values = tuple(float(item[1]) for item in values)
            differences = tuple(current - baseline for baseline, current in values)
            lower, upper = self._bootstrap_interval(
                differences,
                seed_material=(
                    protocol.protocol_version,
                    protocol.scenario_set_version,
                    protocol.statistics_version,
                    metric,
                ),
            )
            comparisons[metric] = MetricComparison(
                metric=metric,
                paired_units=len(differences),
                baseline_mean=round(fmean(baseline_values), 4),
                candidate_mean=round(fmean(candidate_values), 4),
                difference=round(fmean(differences), 4),
                ci_lower=lower,
                ci_upper=upper,
            )
        return comparisons

    @staticmethod
    def _aggregate_units(
        runs: tuple[ReviewedRun, ...]
    ) -> dict[tuple[str, str], dict[str, float | None]]:
        units: dict[tuple[str, str], list[ReviewedRun]] = {}
        for run in runs:
            units.setdefault((run.scenario_turn_id, run.seed), []).append(run)
        result: dict[tuple[str, str], dict[str, float | None]] = {}
        for key, reviews in units.items():
            result[key] = {
                "human_likeness": fmean(
                    (fmean(item.rubric_scores.values()) - 1) / 4 for item in reviews
                ),
                "continuity": fmean(
                    (item.rubric_scores["continuity"] - 1) / 4 for item in reviews
                ),
                "fact_safety": fmean(
                    (item.rubric_scores["fact_safety"] - 1) / 4 for item in reviews
                ),
                "world_synchronicity": fmean(
                    (item.rubric_scores["world_synchronicity"] - 1) / 4 for item in reviews
                ),
                "question_loop_rate": ExperienceEvaluator._question_loop_unit(reviews),
                "fallback_smell_rate": (
                    fmean(
                        item.fallback_template_hit and item.fallback_smell_confirmed
                        for item in reviews
                        if item.model_failed
                    )
                    if any(item.model_failed for item in reviews)
                    else None
                ),
            }
        return result

    @staticmethod
    def _bootstrap_interval(
        differences: tuple[float, ...], *, seed_material: tuple[str, ...]
    ) -> tuple[float, float]:
        if not differences:
            raise ExperienceEvaluationError("bootstrap requires paired observations")
        digest = hashlib.sha256("\x1f".join(seed_material).encode("utf-8")).digest()
        random = Random(int.from_bytes(digest[:8], "big"))
        samples = sorted(
            fmean(differences[random.randrange(len(differences))] for _ in differences)
            for _ in range(2_000)
        )
        lower = samples[int((len(samples) - 1) * 0.025)]
        upper = samples[int((len(samples) - 1) * 0.975)]
        return round(lower, 4), round(upper, 4)

    @staticmethod
    def _official_gate_blockers(
        protocol: EvaluationProtocol,
        scenarios: Mapping[str, ScenarioTurn],
        grouped: Mapping[str, tuple[ReviewedRun, ...]],
        comparisons: Mapping[str, MetricComparison],
        mechanical_evaluation: MechanicalEvaluation | None,
        artifact_keys: frozenset[tuple[str, str, str]],
    ) -> list[str]:
        if protocol.protocol_version != OFFICIAL_PROTOCOL_VERSION:
            return []
        blockers: list[str] = []
        if mechanical_evaluation is None:
            blockers.append("missing_mechanical_evaluation")
        else:
            if not all(
                (
                    mechanical_evaluation.fixture_manifest_hash,
                    mechanical_evaluation.replay_evidence_hash,
                    mechanical_evaluation.action_receipt_evidence_hash,
                    mechanical_evaluation.affect_evidence_hash,
                    mechanical_evaluation.random_draw_evidence_hash,
                    mechanical_evaluation.performance_trace_hash,
                )
            ):
                blockers.append("missing_mechanical_evidence_manifest")
            if mechanical_evaluation.hard_invariant_violations:
                blockers.append("hard_invariant_violation")
            if mechanical_evaluation.nonterminal_action_leaks:
                blockers.append("nonterminal_action_leak")
            if mechanical_evaluation.replay_hash_mismatches:
                blockers.append("replay_hash_mismatch")
            if mechanical_evaluation.affect_episode_invalid_clears:
                blockers.append("affect_episode_invalid_clear")
            if mechanical_evaluation.random_draw_replay_consistency < 1:
                blockers.append("random_draw_replay_inconsistent")
            if mechanical_evaluation.hot_visible_action_p95_ms > 5_000:
                blockers.append("hot_visible_action_p95_exceeds_5s")
        v2_runs = grouped.get("v2", ())
        if not v2_runs:
            return blockers
        emotional_recall = ExperienceEvaluator._metrics_for_runs(
            v2_runs, scenarios, artifact_keys
        ).emotional_awareness_recall
        if emotional_recall is None or emotional_recall < 0.9:
            blockers.append("v2_emotional_awareness_recall_below_90_percent")
        if not comparisons:
            blockers.append("missing_paired_v2_bare_comparison")
            return blockers
        likeness = comparisons.get("human_likeness")
        if likeness is None or likeness.ci_lower < -0.03:
            blockers.append("v2_human_likeness_below_bare")
        for metric, blocker in (
            ("question_loop_rate", "v2_question_loop_above_bare"),
            ("fallback_smell_rate", "v2_fallback_smell_above_bare"),
        ):
            comparison = comparisons.get(metric)
            if comparison is None or comparison.ci_upper > 0:
                blockers.append(blocker)
        improvements = sum(
            comparisons.get(metric) is not None and comparisons[metric].ci_lower > 0
            for metric in ("continuity", "fact_safety", "world_synchronicity")
        )
        if improvements < 2:
            blockers.append("v2_requires_two_material_world_improvements")
        return blockers


__all__ = [
    "AwarenessEvidence",
    "EvaluationProtocol",
    "EvaluationIssue",
    "VariantEvidence",
    "ExperienceEvaluationError",
    "ExperienceEvaluationReport",
    "ExperienceEvaluator",
    "OFFICIAL_PROTOCOL_VERSION",
    "OFFICIAL_SCENARIO_FAMILIES",
    "OFFICIAL_VARIANTS",
    "RUBRIC_DIMENSIONS",
    "MetricComparison",
    "MechanicalEvaluation",
    "ReviewedRun",
    "ScenarioTurn",
    "VariantMetrics",
]
