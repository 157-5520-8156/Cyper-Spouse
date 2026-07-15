"""CLI for the file-backed Phase-8 formal evaluation artifact pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .formal_evaluation_pipeline import (
    FORMAL_PIPELINE_VERSION,
    FormalEvaluationPipelineError,
    build_synthetic_formal_fixture,
    finalize_formal_evaluation_report,
    prepare_formal_evaluation_packet,
)
from .human_likeness_evaluator import RUBRIC_DIMENSIONS


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _capture_list(path: Path) -> list[object]:
    data = _read(path)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("captures"), list):
        return data["captures"]
    raise FormalEvaluationPipelineError("capture input must be a JSON array or {captures: [...]} object")


def _review_list(path: Path) -> list[object]:
    data = _read(path)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("reviews"), list):
        return data["reviews"]
    raise FormalEvaluationPipelineError("review input must be a JSON array or {reviews: [...]} object")


def _synthetic_reviews(packet_dir: Path) -> list[dict[str, object]]:
    blind = _read(packet_dir / "blind-reviewer-input.json")
    reviews: list[dict[str, object]] = []
    for presentation in blind["presentations"]:
        for reviewer_id in ("fixture-reviewer.a", "fixture-reviewer.b"):
            reviews.append(
                {
                    "review_id": f"fixture:{presentation['blind_output_id']}:{reviewer_id}",
                    "reviewer_id": reviewer_id,
                    "blind_output_id": presentation["blind_output_id"],
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
    return reviews


def _prepare(args: argparse.Namespace) -> int:
    result = prepare_formal_evaluation_packet(
        captures=_capture_list(args.captures),
        output_dir=args.output_dir,
        blinding_secret=args.blinding_secret_file.read_text(encoding="utf-8").strip(),
        judge_model_id=args.judge_model_id,
        judge_prompt_version=args.judge_prompt_version,
        artifact_source=args.artifact_source,
    )
    print(json.dumps({"schema_version": FORMAL_PIPELINE_VERSION, "paths": result["paths"]}, sort_keys=True))
    return 0


def _finalize(args: argparse.Namespace) -> int:
    report = finalize_formal_evaluation_report(
        packet_dir=args.packet_dir,
        review_records=_review_list(args.reviews),
        mechanical_trace=_read(args.mechanical_trace),
        output_path=args.output,
    )
    print(json.dumps({"report_status": report["report_status"], "blockers": report["blockers"]}, sort_keys=True))
    return 0


def _fixture(args: argparse.Namespace) -> int:
    captures, _review_template, trace = build_synthetic_formal_fixture()
    result = prepare_formal_evaluation_packet(
        captures=captures,
        output_dir=args.output_dir,
        blinding_secret="ci-structural-fixture-secret",
        judge_model_id="fixture-judge-not-real",
        judge_prompt_version="fixture-rubric.1",
        artifact_source="synthetic_fixture",
    )
    report = finalize_formal_evaluation_report(
        packet_dir=args.output_dir,
        review_records=_synthetic_reviews(args.output_dir),
        mechanical_trace=trace,
        output_path=args.output_dir / "synthetic-report.json",
    )
    if "synthetic_fixture_not_external_evidence" not in report["blockers"]:
        raise FormalEvaluationPipelineError("synthetic fixture unexpectedly became evaluation evidence")
    print(json.dumps({"packet": result["paths"]["package"], "report_status": report["report_status"], "synthetic": True}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="validate external captures and produce blinded reviewer artifacts")
    prepare.add_argument("--captures", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--blinding-secret-file", type=Path, required=True)
    prepare.add_argument("--judge-model-id", required=True)
    prepare.add_argument("--judge-prompt-version", required=True)
    prepare.add_argument("--artifact-source", choices=("external_real", "synthetic_fixture"), default="external_real")
    prepare.set_defaults(handler=_prepare)
    finalize = commands.add_parser("finalize", help="verify blinded reviews, mechanical trace, and write report")
    finalize.add_argument("--packet-dir", type=Path, required=True)
    finalize.add_argument("--reviews", type=Path, required=True)
    finalize.add_argument("--mechanical-trace", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.set_defaults(handler=_finalize)
    fixture = commands.add_parser("verify-fixture", help="CI-only structural artifact-pipeline regression")
    fixture.add_argument("--output-dir", type=Path, required=True)
    fixture.set_defaults(handler=_fixture)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (FormalEvaluationPipelineError, OSError, json.JSONDecodeError) as exc:
        print(f"formal evaluation artifact pipeline error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
