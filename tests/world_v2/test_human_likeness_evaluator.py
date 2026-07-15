import pytest

from companion_daemon.world_v2.evaluation_artifacts import (
    BlindPresentation,
    CapturedScenarioOutput,
    EvaluationArtifactBundle,
    MechanicalTraceEvidence,
    ProtocolIdentity,
    ScenarioCorpusEntry,
)
from companion_daemon.world_v2.human_likeness_evaluator import (
    EvaluationProtocol,
    ExperienceEvaluator,
    EvidenceArtifact,
    MechanicalEvaluation,
    ExperienceEvaluationError,
    RUBRIC_DIMENSIONS,
    ReviewedRun,
    ScenarioTurn,
    AwarenessEvidence,
)


def test_incomplete_external_evidence_is_a_reported_gate_failure_not_a_human_likeness_claim() -> None:
    protocol = EvaluationProtocol(
        protocol_version="human-likeness-eval-v1",
        scenario_set_version="scenarios.1",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("bare", "archived", "v2"),
        required_repetitions=3,
        minimum_scenario_turns=120,
        minimum_emotion_gold_turns=40,
    )
    corpus = (
        ScenarioTurn(
            scenario_turn_id="disappointment.1",
            scenario_id="mild_disappointment",
            emotional_gold=True,
            acceptable_response_tags=("notice_disappointment", "give_space"),
        ),
    )
    runs = (
        ReviewedRun(
            variant_id="v2",
            scenario_turn_id="disappointment.1",
            seed="seed.1",
            output_hash="a" * 64,
            judge_id="judge.1",
            judge_prompt_version="judge-prompt.1",
            rubric_scores={
                "current_input_fit": 5,
                "subtext_awareness": 5,
                "subjectivity": 4,
                "continuity": 4,
                "non_scriptedness": 4,
                "fact_safety": 5,
                "world_synchronicity": 4,
            },
            response_tags=("notice_disappointment",),
            question_ending=False,
            fallback_template_hit=False,
            fallback_smell_confirmed=False,
            model_failed=False,
            asserted_alternative_as_fact=False,
            awareness_evidence=(
                AwarenessEvidence(
                    source="output",
                    reference_id="output:disappointment.1",
                    response_tags=("notice_disappointment",),
                    output_hash="a" * 64,
                ),
            ),
        ),
    )

    report = ExperienceEvaluator().evaluate(
        protocol=protocol,
        corpus=corpus,
        reviewed_runs=runs,
        evidence_artifacts=(
            EvidenceArtifact(
                source="output",
                reference_id="output:disappointment.1",
                output_hash="a" * 64,
                artifact_hash="1" * 64,
            ),
        ),
    )

    assert report.passed is False
    assert "insufficient_scenario_turns" in report.blockers
    assert "insufficient_emotion_gold_turns" in report.blockers
    assert "missing_variant_runs:archived" in report.blockers
    assert report.variant_metrics["v2"].emotional_awareness_recall == 1.0


def test_emotional_awareness_requires_a_bound_output_proposal_or_affect_episode_evidence() -> None:
    protocol = EvaluationProtocol(
        protocol_version="experiment.1",
        scenario_set_version="scenarios.test",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=1,
        minimum_emotion_gold_turns=1,
    )
    corpus = (
        ScenarioTurn(
            scenario_turn_id="offence.1",
            scenario_id="explicit_offence",
            emotional_gold=True,
            acceptable_response_tags=("set_boundary",),
        ),
    )
    run = ReviewedRun(
        variant_id="v2",
        scenario_turn_id="offence.1",
        seed="seed.1",
        output_hash="d" * 64,
        judge_id="judge.1",
        judge_prompt_version="judge-prompt.1",
        rubric_scores={dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        response_tags=("set_boundary",),
        question_ending=False,
        fallback_template_hit=False,
        fallback_smell_confirmed=False,
        model_failed=False,
        asserted_alternative_as_fact=False,
        used_fact_refs=("fact:user:workload",),
        action_refs=("action:reply:offence.1",),
        affect_episode_refs=("affect:boundary.1",),
    )

    report = ExperienceEvaluator().evaluate(protocol=protocol, corpus=corpus, reviewed_runs=(run,))

    assert report.variant_metrics["v2"].emotional_awareness_recall == 0.0
    assert report.issues[0].code == "missing_emotional_awareness_evidence"
    assert report.evidence_by_variant["v2"].used_fact_refs == ("fact:user:workload",)
    assert report.evidence_by_variant["v2"].action_refs == ("action:reply:offence.1",)
    assert report.evidence_by_variant["v2"].affect_episode_refs == ("affect:boundary.1",)


def test_independent_reviews_cannot_silently_score_different_outputs_as_one_unit() -> None:
    protocol = EvaluationProtocol(
        protocol_version="experiment.1",
        scenario_set_version="scenarios.test",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=1,
        minimum_emotion_gold_turns=0,
    )
    corpus = (
        ScenarioTurn("share.1", "ordinary_share", False, ()),
    )
    base = dict(
        variant_id="v2",
        scenario_turn_id="share.1",
        seed="seed.1",
        judge_prompt_version="judge-prompt.1",
        rubric_scores={dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        response_tags=(),
        question_ending=False,
        fallback_template_hit=False,
        fallback_smell_confirmed=False,
        model_failed=False,
        asserted_alternative_as_fact=False,
    )
    first = ReviewedRun(**base, output_hash="e" * 64, judge_id="judge.1")
    second = ReviewedRun(**base, output_hash="f" * 64, judge_id="judge.2")

    with pytest.raises(ExperienceEvaluationError, match="one output hash"):
        ExperienceEvaluator().evaluate(
            protocol=protocol, corpus=corpus, reviewed_runs=(first, second)
        )


def test_emotion_recall_is_weighted_by_scenario_turn_units_not_reviewer_count() -> None:
    protocol = EvaluationProtocol(
        protocol_version="experiment.1",
        scenario_set_version="scenarios.test",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=2,
        minimum_emotion_gold_turns=2,
    )
    corpus = (
        ScenarioTurn("emotion.1", "mild_disappointment", True, ("notice",)),
        ScenarioTurn("emotion.2", "explicit_offence", True, ("set_boundary",)),
    )

    def review(turn_id: str, judge: int, *, noticed: bool) -> ReviewedRun:
        output_hash = ("a" if turn_id == "emotion.1" else "b") * 64
        tag = "notice" if turn_id == "emotion.1" else "set_boundary"
        evidence = ()
        if noticed:
            evidence = (
                AwarenessEvidence("output", f"output:{turn_id}", (tag,), output_hash),
            )
        return ReviewedRun(
            variant_id="v2",
            scenario_turn_id=turn_id,
            seed="seed.1",
            output_hash=output_hash,
            judge_id=f"judge.{judge}",
            judge_prompt_version="judge-prompt.1",
            rubric_scores={dimension: 4 for dimension in RUBRIC_DIMENSIONS},
            response_tags=(tag,) if noticed else (),
            question_ending=False,
            fallback_template_hit=False,
            fallback_smell_confirmed=False,
            model_failed=False,
            asserted_alternative_as_fact=False,
            awareness_evidence=evidence,
        )

    runs = (
        *(review("emotion.1", judge, noticed=False) for judge in range(2)),
        *(review("emotion.2", judge, noticed=True) for judge in range(6)),
    )
    artifacts = (
        EvidenceArtifact("output", "output:emotion.2", "b" * 64, "2" * 64),
    )

    report = ExperienceEvaluator().evaluate(
        protocol=protocol, corpus=corpus, reviewed_runs=runs, evidence_artifacts=artifacts
    )

    assert report.variant_metrics["v2"].emotional_awareness_recall == 0.5


def test_review_cannot_be_paired_to_a_scenario_turn_with_different_input_hash() -> None:
    protocol = EvaluationProtocol(
        protocol_version="experiment.1",
        scenario_set_version="scenarios.test",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=1,
        minimum_emotion_gold_turns=0,
    )
    corpus = (
        ScenarioTurn(
            "share.1",
            "ordinary_share",
            False,
            (),
            input_hash="1" * 64,
            fact_set_hash="2" * 64,
        ),
    )
    run = ReviewedRun(
        variant_id="v2",
        scenario_turn_id="share.1",
        seed="seed.1",
        output_hash="a" * 64,
        judge_id="judge.1",
        judge_prompt_version="judge-prompt.1",
        rubric_scores={dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        response_tags=(),
        question_ending=False,
        fallback_template_hit=False,
        fallback_smell_confirmed=False,
        model_failed=False,
        asserted_alternative_as_fact=False,
        scenario_input_hash="3" * 64,
        scenario_fact_set_hash="2" * 64,
    )

    with pytest.raises(ExperienceEvaluationError, match="input hash does not match"):
        ExperienceEvaluator().evaluate(protocol=protocol, corpus=corpus, reviewed_runs=(run,))


def test_tied_independent_emotion_reviews_are_not_counted_as_awareness() -> None:
    protocol = EvaluationProtocol(
        protocol_version="experiment.1",
        scenario_set_version="scenarios.test",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=1,
        minimum_emotion_gold_turns=1,
    )
    corpus = (ScenarioTurn("emotion.1", "mild_disappointment", True, ("notice",)),)
    base = dict(
        variant_id="v2",
        scenario_turn_id="emotion.1",
        seed="seed.1",
        output_hash="a" * 64,
        judge_prompt_version="judge-prompt.1",
        rubric_scores={dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        response_tags=(),
        question_ending=False,
        fallback_template_hit=False,
        fallback_smell_confirmed=False,
        model_failed=False,
        asserted_alternative_as_fact=False,
    )
    noticed = ReviewedRun(
        **base,
        judge_id="judge.1",
        awareness_evidence=(AwarenessEvidence("output", "output:emotion.1", ("notice",), "a" * 64),),
    )
    not_noticed = ReviewedRun(**base, judge_id="judge.2")

    report = ExperienceEvaluator().evaluate(
        protocol=protocol,
        corpus=corpus,
        reviewed_runs=(noticed, not_noticed),
        evidence_artifacts=(EvidenceArtifact("output", "output:emotion.1", "a" * 64, "2" * 64),),
    )

    assert report.variant_metrics["v2"].emotional_awareness_recall == 0.0


def test_a_single_reviewer_cannot_establish_a_blind_experience_baseline() -> None:
    protocol = EvaluationProtocol(
        protocol_version="human-likeness-eval-v1",
        scenario_set_version="scenarios.test",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=1,
        minimum_emotion_gold_turns=0,
    )
    corpus = (
        ScenarioTurn(
            scenario_turn_id="share.1",
            scenario_id="ordinary_share",
            emotional_gold=False,
            acceptable_response_tags=(),
        ),
    )
    run = ReviewedRun(
        variant_id="v2",
        scenario_turn_id="share.1",
        seed="seed.1",
        output_hash="b" * 64,
        judge_id="judge.1",
        judge_prompt_version="judge-prompt.1",
        rubric_scores={dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        response_tags=(),
        question_ending=False,
        fallback_template_hit=False,
        fallback_smell_confirmed=False,
        model_failed=False,
        asserted_alternative_as_fact=False,
    )

    report = ExperienceEvaluator().evaluate(protocol=protocol, corpus=corpus, reviewed_runs=(run,))

    assert report.passed is False
    assert "insufficient_independent_reviews:v2:share.1:seed.1" in report.blockers


def test_official_gate_reports_when_v2_is_paired_worse_than_bare() -> None:
    protocol = EvaluationProtocol(
        protocol_version="human-likeness-eval-v1",
        scenario_set_version="scenarios.1",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("bare", "archived", "v2"),
        required_repetitions=3,
        minimum_scenario_turns=120,
        minimum_emotion_gold_turns=40,
        judge_model_id="judge-model.1",
        judge_prompt_version="judge-prompt.1",
        judge_temperature=0,
    )
    corpus = (
        ScenarioTurn(
            scenario_turn_id="disappointment.1",
            scenario_id="mild_disappointment",
            scenario_family="mild_disappointment",
            emotional_gold=True,
            acceptable_response_tags=("notice_disappointment",),
        ),
    )
    scores = {"bare": 5, "archived": 3, "v2": 1}
    runs = tuple(
        ReviewedRun(
            variant_id=variant,
            scenario_turn_id="disappointment.1",
            seed=seed,
            output_hash=("a" if variant == "bare" else "b" if variant == "archived" else "c") * 64,
            judge_id=f"review:{judge}",
            judge_model_id="judge-model.1",
            judge_prompt_version="judge-prompt.1",
            rubric_scores={dimension: scores[variant] for dimension in RUBRIC_DIMENSIONS},
            response_tags=("notice_disappointment",),
            question_ending=False,
            fallback_template_hit=False,
            fallback_smell_confirmed=False,
            model_failed=False,
            asserted_alternative_as_fact=False,
        )
        for variant in ("bare", "archived", "v2")
        for seed in ("seed.1", "seed.2", "seed.3")
        for judge in ("a", "b")
    )

    report = ExperienceEvaluator().evaluate(protocol=protocol, corpus=corpus, reviewed_runs=runs)

    comparison = report.comparisons["human_likeness"]
    assert comparison.difference == -1.0
    assert comparison.ci_lower == -1.0
    assert comparison.ci_upper == -1.0
    assert "v2_human_likeness_below_bare" in report.blockers


def test_official_gate_includes_mechanical_world_and_latency_evidence() -> None:
    protocol = EvaluationProtocol(
        protocol_version="human-likeness-eval-v1",
        scenario_set_version="scenarios.1",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=1,
        minimum_emotion_gold_turns=0,
    )

    report = ExperienceEvaluator().evaluate(
        protocol=protocol,
        corpus=(),
        reviewed_runs=(),
        mechanical_evaluation=MechanicalEvaluation(
            hard_invariant_violations=1,
            nonterminal_action_leaks=0,
            replay_hash_mismatches=0,
            affect_episode_invalid_clears=0,
            random_draw_replay_consistency=1.0,
            hot_visible_action_p95_ms=5_001,
        ),
    )

    assert "hard_invariant_violation" in report.blockers
    assert "hot_visible_action_p95_exceeds_5s" in report.blockers
    assert "missing_verified_evaluation_artifact_bundle" in report.blockers


def test_official_gate_distinguishes_absent_random_authority_from_missing_required_draw_evidence() -> None:
    protocol = EvaluationProtocol(
        protocol_version="human-likeness-eval-v1",
        scenario_set_version="scenarios.1",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=1,
        minimum_emotion_gold_turns=0,
    )
    base = dict(
        hard_invariant_violations=0,
        nonterminal_action_leaks=0,
        replay_hash_mismatches=0,
        affect_episode_invalid_clears=0,
        random_draw_replay_consistency=1.0,
        hot_visible_action_p95_ms=1.0,
    )

    unavailable = ExperienceEvaluator().evaluate(
        protocol=protocol,
        corpus=(),
        reviewed_runs=(),
        mechanical_evaluation=MechanicalEvaluation(**base, random_draw_status="not_applicable"),
    )
    missing = ExperienceEvaluator().evaluate(
        protocol=protocol,
        corpus=(),
        reviewed_runs=(),
        mechanical_evaluation=MechanicalEvaluation(**base, random_draw_status="missing_required"),
    )

    assert "random_draw_evidence_missing" not in unavailable.blockers
    assert "random_draw_evidence_missing" in missing.blockers


def test_official_review_requires_the_exact_verified_protocol_corpus_output_and_blinding() -> None:
    scenario = ScenarioTurn(
        scenario_turn_id="share.1",
        scenario_id="ordinary_share",
        scenario_family="ordinary_share",
        emotional_gold=False,
        acceptable_response_tags=(),
        input_hash="1" * 64,
        fact_set_hash="2" * 64,
    )
    captured = CapturedScenarioOutput.from_text(
        variant_id="v2",
        scenario_turn_id=scenario.scenario_turn_id,
        seed="seed.1",
        scenario_input_hash=scenario.input_hash,
        scenario_fact_set_hash=scenario.fact_set_hash,
        text="我在。",
    )
    presentation = BlindPresentation(
        *captured.unit_key, "blind.1", captured.output_hash, 0
    )
    probe = EvaluationArtifactBundle(
        protocol=ProtocolIdentity("probe", "probe", "probe", "probe", "0" * 64),
        corpus=(
            ScenarioCorpusEntry(
                scenario_turn_id=scenario.scenario_turn_id,
                scenario_id=scenario.scenario_id,
                scenario_family=scenario.scenario_family,
                emotional_gold=scenario.emotional_gold,
                acceptable_response_tags=scenario.acceptable_response_tags,
                input_hash=scenario.input_hash,
                fact_set_hash=scenario.fact_set_hash,
            ),
        ),
        outputs=(captured,),
        presentations=(presentation,),
        mechanical_trace=MechanicalTraceEvidence(
            fixture_manifest_hash="3" * 64,
            replay_evidence_hash="4" * 64,
            action_receipt_evidence_hash="5" * 64,
            affect_evidence_hash="6" * 64,
            random_draw_evidence_hash="7" * 64,
            performance_trace_hash="8" * 64,
            hard_invariant_violations=0,
            nonterminal_action_leaks=0,
            replay_hash_mismatches=0,
            affect_episode_invalid_clears=0,
            random_draw_replay_consistency=1.0,
            hot_visible_action_p95_ms=500.0,
        ),
    )
    protocol = EvaluationProtocol(
        protocol_version="human-likeness-eval-v1",
        scenario_set_version="scenarios.1",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        required_variants=("v2",),
        required_repetitions=1,
        minimum_scenario_turns=1,
        minimum_emotion_gold_turns=0,
        blinded_order_hash=probe.blinded_order_hash,
        unblinding_map_hash=probe.unblinding_map_hash,
        blinding_scheme_version="test.1",
    )
    artifact_bundle = EvaluationArtifactBundle(
        protocol=protocol.artifact_identity,
        corpus=probe.corpus,
        outputs=probe.outputs,
        presentations=probe.presentations,
        mechanical_trace=probe.mechanical_trace,
    )
    review = ReviewedRun(
        variant_id="v2",
        scenario_turn_id="share.1",
        seed="seed.1",
        output_hash=captured.output_hash,
        judge_id="judge.1",
        judge_prompt_version="judge-prompt.1",
        rubric_scores={dimension: 4 for dimension in RUBRIC_DIMENSIONS},
        response_tags=(),
        question_ending=False,
        fallback_template_hit=False,
        fallback_smell_confirmed=False,
        model_failed=False,
        asserted_alternative_as_fact=False,
        scenario_input_hash=scenario.input_hash,
        scenario_fact_set_hash=scenario.fact_set_hash,
        blind_output_id="blind.1",
        non_necessary_question_ending=False,
    )

    report = ExperienceEvaluator().evaluate(
        protocol=protocol,
        corpus=(scenario,),
        reviewed_runs=(review,),
        artifact_bundle=artifact_bundle,
    )

    assert "missing_verified_evaluation_artifact_bundle" not in report.blockers
    assert "verified_evaluation_artifact_bundle_mismatch" not in report.blockers
    assert "verified_evaluation_artifact_blinding_mismatch" not in report.blockers
    assert "reviewed_run_not_bound_to_verified_output" not in report.blockers
    assert "reviewed_run_blind_identity_mismatch" not in report.blockers

    mismatched_evidence_report = ExperienceEvaluator().evaluate(
        protocol=protocol,
        corpus=(scenario,),
        reviewed_runs=(review,),
        evidence_artifacts=(
            EvidenceArtifact(
                source="output",
                reference_id="output.share.1",
                variant_id="v2",
                scenario_turn_id="share.1",
                seed="seed.1",
                output_hash=captured.output_hash,
                artifact_hash="9" * 64,
            ),
        ),
        artifact_bundle=artifact_bundle,
    )

    assert "review_evidence_not_bound_to_verified_artifact_bundle" in mismatched_evidence_report.blockers
