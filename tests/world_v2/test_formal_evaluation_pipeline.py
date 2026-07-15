from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.formal_evaluation_pipeline import (
    FormalEvaluationPipelineError,
    build_synthetic_formal_fixture,
    finalize_formal_evaluation_report,
    prepare_formal_evaluation_packet,
)
from companion_daemon.world_v2.human_likeness_evaluator import RUBRIC_DIMENSIONS


def _reviews(packet_dir) -> list[dict[str, object]]:
    blind = json.loads((packet_dir / "blind-reviewer-input.json").read_text(encoding="utf-8"))
    result: list[dict[str, object]] = []
    for row in blind["presentations"]:
        for reviewer in ("reviewer.one", "reviewer.two"):
            result.append(
                {
                    "review_id": f"review:{row['blind_output_id']}:{reviewer}",
                    "reviewer_id": reviewer,
                    "blind_output_id": row["blind_output_id"],
                    "rubric_scores": {dimension: 4 for dimension in RUBRIC_DIMENSIONS},
                    "response_tags": [],
                    "question_ending": False,
                    "non_necessary_question_ending": False,
                    "reply_eligible": True,
                    "fallback_template_hit": False,
                    "fallback_smell_confirmed": False,
                    "model_failed": False,
                    "asserted_alternative_as_fact": False,
                    "awareness_evidence": [],
                    "used_fact_refs": [],
                    "action_refs": [],
                    "affect_episode_refs": [],
                }
            )
    return result


def _prepared_fixture(tmp_path):
    captures, _template, trace = build_synthetic_formal_fixture()
    prepare_formal_evaluation_packet(
        captures=captures,
        output_dir=tmp_path,
        blinding_secret="test-secret",
        judge_model_id="fixture-judge",
        judge_prompt_version="fixture-rubric.1",
        artifact_source="synthetic_fixture",
    )
    return captures, trace


def test_formal_packet_requires_full_bare_archive_v2_three_seed_matrix_and_hides_variants(tmp_path) -> None:
    captures, _template, _trace = build_synthetic_formal_fixture()

    with pytest.raises(FormalEvaluationPipelineError, match="capture matrix"):
        prepare_formal_evaluation_packet(
            captures=captures[:-1],
            output_dir=tmp_path / "incomplete",
            blinding_secret="test-secret",
            judge_model_id="fixture-judge",
            judge_prompt_version="fixture-rubric.1",
            artifact_source="synthetic_fixture",
        )

    _prepared_fixture(tmp_path / "packet")
    reviewer_input = json.loads((tmp_path / "packet" / "blind-reviewer-input.json").read_text(encoding="utf-8"))
    serialized = json.dumps(reviewer_input, ensure_ascii=False)
    assert "variant_id" not in serialized
    assert len(reviewer_input["presentations"]) == 120 * 3 * 3


def test_finalization_requires_two_independent_reviews_per_blinded_output(tmp_path) -> None:
    _captures, trace = _prepared_fixture(tmp_path)
    reviews = _reviews(tmp_path)

    with pytest.raises(FormalEvaluationPipelineError, match="two independent reviews"):
        finalize_formal_evaluation_report(
            packet_dir=tmp_path,
            review_records=reviews[:-1],
            mechanical_trace=trace,
            output_path=tmp_path / "report.json",
        )


def test_synthetic_pipeline_exercises_bundle_bootstrap_report_but_can_never_claim_real_evidence(tmp_path) -> None:
    _captures, trace = _prepared_fixture(tmp_path)

    report = finalize_formal_evaluation_report(
        packet_dir=tmp_path,
        review_records=_reviews(tmp_path),
        mechanical_trace=trace,
        output_path=tmp_path / "report.json",
    )

    assert report["artifact_bundle"]["capture_count"] == 120 * 3 * 3
    assert report["artifact_bundle"]["independent_review_count"] == 120 * 3 * 3 * 2
    assert "human_likeness" in report["comparisons"]
    assert "synthetic_fixture_not_external_evidence" in report["blockers"]
    assert report["report_status"] == "blocked"
