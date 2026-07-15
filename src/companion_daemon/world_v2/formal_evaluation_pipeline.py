"""Formal, file-backed Phase-8 artifact pipeline.

This is deliberately an *artifact* boundary.  It does not call a chat model,
provider, or judge.  Production runners capture real bare/archive/v2 output
and their trace payloads outside this module, then this module verifies the
complete 120 x 3-seed matrix, creates a blinded reviewer packet, and turns two
or more independent review records back into the existing read-only
``ExperienceEvaluator`` records.

The synthetic fixture mode exists solely for CI schema regression.  Its report
is permanently marked as non-evidence, so a green structural test can never
be mistaken for a completed human-likeness evaluation.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from random import Random
from typing import Any, Iterable, Literal, Mapping

from .evaluation_artifacts import (
    BlindPresentation,
    CapturedScenarioOutput,
    EvaluationArtifactBundle,
    EvidenceArtifactCapture,
    MechanicalTraceEvidence,
    ScenarioCorpusEntry,
)
from .human_likeness_evaluator import (
    AwarenessEvidence,
    EvidenceArtifact,
    EvaluationProtocol,
    ExperienceEvaluator,
    MechanicalEvaluation,
    ReviewedRun,
    RUBRIC_DIMENSIONS,
    ScenarioTurn,
)
from .scenario_corpus import SCENARIO_CASES, SCENARIO_CORPUS_VERSION, verify_frozen_scenario_corpus


FORMAL_PIPELINE_VERSION = "world-v2-formal-evaluation-pipeline.1"
FORMAL_VARIANTS = ("bare", "archived", "v2")
FORMAL_SEEDS = ("seed.1", "seed.2", "seed.3")
ArtifactSource = Literal["external_real", "synthetic_fixture"]


class FormalEvaluationPipelineError(ValueError):
    """A capture, review packet, or external artifact is not protocol-bound."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FormalEvaluationPipelineError(f"{name} must be an object")
    return value


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FormalEvaluationPipelineError(f"{name} must be non-empty text")
    return value


def _require_digest(value: object, name: str) -> str:
    value = _require_text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise FormalEvaluationPipelineError(f"{name} must be a lowercase sha256 digest")
    return value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalEvaluationPipelineError(f"cannot read JSON artifact {path}: {exc}") from exc


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _corpus_by_turn() -> dict[str, tuple[ScenarioCorpusEntry, str]]:
    verify_frozen_scenario_corpus()
    result = {case.entry.scenario_turn_id: (case.entry, case.user_text) for case in SCENARIO_CASES}
    if len(result) != len(SCENARIO_CASES):
        raise RuntimeError("frozen corpus has duplicate turn ids")
    return result


def _protocol_contract(*, judge_model_id: str, judge_prompt_version: str) -> dict[str, object]:
    return {
        "required_variants": FORMAL_VARIANTS,
        "required_repetitions": len(FORMAL_SEEDS),
        "minimum_scenario_turns": len(SCENARIO_CASES),
        "minimum_emotion_gold_turns": sum(case.entry.emotional_gold for case in SCENARIO_CASES),
        "minimum_independent_reviews": 2,
        "judge_model_id": judge_model_id,
        "judge_prompt_version": judge_prompt_version,
        "judge_temperature": 0,
        "blinding_scheme_version": "world-v2-blind-packet.1",
    }


def _protocol_from_packet(packet: Mapping[str, Any], *, blinded_order_hash: str, unblinding_map_hash: str) -> EvaluationProtocol:
    review = _require_mapping(packet.get("review_protocol"), "review_protocol")
    return EvaluationProtocol(
        protocol_version=_require_text(packet.get("protocol_version"), "protocol_version"),
        scenario_set_version=_require_text(packet.get("scenario_set_version"), "scenario_set_version"),
        rubric_version=_require_text(review.get("rubric_version"), "rubric_version"),
        statistics_version=_require_text(review.get("statistics_version"), "statistics_version"),
        required_variants=tuple(review.get("required_variants", ())),
        required_repetitions=int(review.get("required_repetitions", 0)),
        minimum_scenario_turns=int(review.get("minimum_scenario_turns", 0)),
        minimum_emotion_gold_turns=int(review.get("minimum_emotion_gold_turns", 0)),
        minimum_independent_reviews=int(review.get("minimum_independent_reviews", 0)),
        judge_model_id=_require_text(review.get("judge_model_id"), "judge_model_id"),
        judge_prompt_version=_require_text(review.get("judge_prompt_version"), "judge_prompt_version"),
        judge_temperature=review.get("judge_temperature"),
        blinding_scheme_version=_require_text(review.get("blinding_scheme_version"), "blinding_scheme_version"),
        blinded_order_hash=blinded_order_hash,
        unblinding_map_hash=unblinding_map_hash,
    )


def _packet_paths(directory: Path) -> dict[str, Path]:
    return {
        "capture_archive": directory / "capture-archive.json",
        "blind_reviewer_input": directory / "blind-reviewer-input.json",
        "unblinding_map": directory / "unblinding-map.json",
        "package": directory / "evaluation-package.json",
    }


def _capture_record(raw: object, *, corpus: Mapping[str, tuple[ScenarioCorpusEntry, str]]) -> dict[str, Any]:
    item = _require_mapping(raw, "capture record")
    variant_id = _require_text(item.get("variant_id"), "capture variant_id")
    if variant_id not in FORMAL_VARIANTS:
        raise FormalEvaluationPipelineError("capture variant_id is not in formal variants")
    turn_id = _require_text(item.get("scenario_turn_id"), "capture scenario_turn_id")
    entry_and_input = corpus.get(turn_id)
    if entry_and_input is None:
        raise FormalEvaluationPipelineError("capture references a scenario outside frozen corpus")
    entry, input_text = entry_and_input
    seed = _require_text(item.get("seed"), "capture seed")
    if seed not in FORMAL_SEEDS:
        raise FormalEvaluationPipelineError("capture seed is not in formal seeds")
    output_text = _require_text(item.get("output_text"), "capture output_text")
    trace = dict(_require_mapping(item.get("trace"), "capture trace"))
    if not trace:
        raise FormalEvaluationPipelineError("capture trace must not be empty")
    # Capture producers may include the request text, but the persisted packet
    # always takes its authority from the frozen corpus rather than trusting it.
    supplied_input_hash = item.get("scenario_input_hash", entry.input_hash)
    supplied_fact_hash = item.get("scenario_fact_set_hash", entry.fact_set_hash)
    if supplied_input_hash != entry.input_hash or supplied_fact_hash != entry.fact_set_hash:
        raise FormalEvaluationPipelineError("capture input/fact hashes do not match frozen corpus")
    evidence_artifacts: list[dict[str, str]] = []
    raw_evidence = item.get("evidence_artifacts", ())
    if not isinstance(raw_evidence, (list, tuple)):
        raise FormalEvaluationPipelineError("capture evidence_artifacts must be an array")
    seen_evidence: set[tuple[str, str]] = set()
    for raw_artifact in raw_evidence:
        artifact = _require_mapping(raw_artifact, "capture evidence artifact")
        source = artifact.get("source")
        if source not in {"proposal", "affect_episode"}:
            raise FormalEvaluationPipelineError("capture evidence source must be proposal or affect_episode")
        reference_id = _require_text(artifact.get("reference_id"), "capture evidence reference_id")
        identity = (source, reference_id)
        if identity in seen_evidence:
            raise FormalEvaluationPipelineError("capture evidence identities must be unique per output")
        seen_evidence.add(identity)
        payload = _require_mapping(artifact.get("payload"), "capture evidence payload")
        if not payload:
            raise FormalEvaluationPipelineError("capture evidence payload must not be empty")
        evidence_artifacts.append(
            {"source": source, "reference_id": reference_id, "artifact_hash": _digest(payload)}
        )
    return {
        "variant_id": variant_id,
        "scenario_turn_id": turn_id,
        "scenario_id": entry.scenario_id,
        "scenario_family": entry.scenario_family,
        "seed": seed,
        "input_text": input_text,
        "scenario_input_hash": entry.input_hash,
        "scenario_fact_set_hash": entry.fact_set_hash,
        "output_text": output_text,
        "output_hash": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        "trace": trace,
        "trace_hash": _digest(trace),
        "evidence_artifacts": evidence_artifacts,
    }


def _expected_units(corpus: Mapping[str, tuple[ScenarioCorpusEntry, str]]) -> set[tuple[str, str, str]]:
    return {(variant, turn_id, seed) for variant in FORMAL_VARIANTS for turn_id in corpus for seed in FORMAL_SEEDS}


def _validate_complete_capture_matrix(records: Iterable[dict[str, Any]], *, corpus: Mapping[str, tuple[ScenarioCorpusEntry, str]]) -> tuple[dict[str, Any], ...]:
    records = tuple(records)
    by_unit = {(item["variant_id"], item["scenario_turn_id"], item["seed"]): item for item in records}
    if len(by_unit) != len(records):
        raise FormalEvaluationPipelineError("capture matrix has duplicate variant/turn/seed units")
    expected = _expected_units(corpus)
    if set(by_unit) != expected:
        missing = len(expected - set(by_unit))
        unexpected = len(set(by_unit) - expected)
        raise FormalEvaluationPipelineError(
            f"capture matrix must be bare/archive/v2 x frozen corpus x three seeds (missing={missing}, unexpected={unexpected})"
        )
    return tuple(by_unit[key] for key in sorted(by_unit))


def _blind_id(*, secret: str, unit: tuple[str, str, str], output_hash: str) -> str:
    return "blind:" + hashlib.sha256((secret + "\x1f" + "\x1f".join(unit) + "\x1f" + output_hash).encode("utf-8")).hexdigest()[:24]


def prepare_formal_evaluation_packet(
    *,
    captures: Iterable[object],
    output_dir: Path,
    blinding_secret: str,
    judge_model_id: str,
    judge_prompt_version: str,
    artifact_source: ArtifactSource,
) -> dict[str, Any]:
    """Validate external captures and write archive, blinded input, and map.

    ``blinding_secret`` must be held outside the reviewer packet.  It is used
    only to create stable opaque ids and a deterministic shuffled order.
    """

    if not blinding_secret.strip():
        raise FormalEvaluationPipelineError("blinding_secret must be non-empty")
    if artifact_source not in {"external_real", "synthetic_fixture"}:
        raise FormalEvaluationPipelineError("artifact_source is invalid")
    corpus = _corpus_by_turn()
    records = _validate_complete_capture_matrix(
        (_capture_record(item, corpus=corpus) for item in captures), corpus=corpus
    )
    order = list(records)
    random = Random(int.from_bytes(hashlib.sha256(blinding_secret.encode("utf-8")).digest()[:8], "big"))
    random.shuffle(order)
    blind_rows: list[dict[str, Any]] = []
    map_rows: list[dict[str, Any]] = []
    for position, record in enumerate(order):
        unit = (record["variant_id"], record["scenario_turn_id"], record["seed"])
        blind_id = _blind_id(secret=blinding_secret, unit=unit, output_hash=record["output_hash"])
        blind_rows.append(
            {
                "blind_output_id": blind_id,
                "presentation_position": position,
                "scenario_turn_id": record["scenario_turn_id"],
                "scenario_id": record["scenario_id"],
                "scenario_family": record["scenario_family"],
                "input_text": record["input_text"],
                "scenario_input_hash": record["scenario_input_hash"],
                "scenario_fact_set_hash": record["scenario_fact_set_hash"],
                "output_text": record["output_text"],
                "output_hash": record["output_hash"],
                "available_evidence": [
                    {"source": "output", "reference_id": f"output:{record['scenario_turn_id']}:{record['seed']}", "artifact_hash": record["trace_hash"]},
                    *record["evidence_artifacts"],
                ],
            }
        )
        map_rows.append(
            {
                "blind_output_id": blind_id,
                "variant_id": record["variant_id"],
                "scenario_turn_id": record["scenario_turn_id"],
                "seed": record["seed"],
                "output_hash": record["output_hash"],
            }
        )
    contract = _protocol_contract(judge_model_id=judge_model_id, judge_prompt_version=judge_prompt_version)
    reviewer_schema = {
        "reviewer_id": "non-empty independent reviewer identity",
        "review_id": "unique immutable review submission identity",
        "blind_output_id": "from blind-reviewer-input.json",
        "rubric_scores": {dimension: "integer 1..5" for dimension in RUBRIC_DIMENSIONS},
        "response_tags": "array of observed tags (may be empty)",
        "question_ending": "boolean",
        "non_necessary_question_ending": "boolean or null",
        "reply_eligible": "boolean",
        "fallback_template_hit": "boolean",
        "fallback_smell_confirmed": "boolean",
        "model_failed": "boolean",
        "asserted_alternative_as_fact": "boolean",
        "awareness_evidence": "array of {source, reference_id, response_tags}; source is output/proposal/affect_episode",
        "used_fact_refs": "array of source-bound fact refs",
        "action_refs": "array of source-bound action refs",
        "affect_episode_refs": "array of source-bound affect refs",
    }
    paths = _packet_paths(output_dir)
    archive = {
        "schema_version": FORMAL_PIPELINE_VERSION,
        "artifact_source": artifact_source,
        "scenario_set_version": SCENARIO_CORPUS_VERSION,
        "captures": records,
    }
    blind_input = {
        "schema_version": FORMAL_PIPELINE_VERSION,
        "review_protocol": contract,
        "review_schema": reviewer_schema,
        "presentations": blind_rows,
        "warning": "This packet is blinded. Do not add variant guesses or unblinding data to reviews.",
    }
    unblinding = {"schema_version": FORMAL_PIPELINE_VERSION, "presentations": map_rows}
    _write_json(paths["capture_archive"], archive)
    _write_json(paths["blind_reviewer_input"], blind_input)
    _write_json(paths["unblinding_map"], unblinding)
    packet = {
        "schema_version": FORMAL_PIPELINE_VERSION,
        "protocol_version": "human-likeness-eval-v1",
        "scenario_set_version": SCENARIO_CORPUS_VERSION,
        "artifact_source": artifact_source,
        "review_protocol": {**contract, "rubric_version": "world-v2-rubric.1", "statistics_version": "world-v2-bootstrap.1"},
        "files": {name: {"path": path.name, "sha256": _digest(_read_json(path))} for name, path in paths.items() if name != "package"},
        "external_required_artifacts": (
            "real bare/archive/v2 capture archive",
            "two independent blinded reviewer submissions",
            "recomputed mechanical trace evidence",
        ),
    }
    _write_json(paths["package"], packet)
    return {"packet": packet, "paths": {name: str(path) for name, path in paths.items()}}


def _load_packet(directory: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    paths = _packet_paths(directory)
    packet = dict(_require_mapping(_read_json(paths["package"]), "evaluation package"))
    archive = dict(_require_mapping(_read_json(paths["capture_archive"]), "capture archive"))
    blind = dict(_require_mapping(_read_json(paths["blind_reviewer_input"]), "blind reviewer input"))
    unblinding = dict(_require_mapping(_read_json(paths["unblinding_map"]), "unblinding map"))
    for name, value in packet.get("files", {}).items():
        if name not in paths or name == "package":
            raise FormalEvaluationPipelineError("evaluation package references an invalid file")
        actual = _digest({"capture_archive": archive, "blind_reviewer_input": blind, "unblinding_map": unblinding}[name])
        if actual != value.get("sha256"):
            raise FormalEvaluationPipelineError(f"evaluation package hash mismatch: {name}")
    corpus = _corpus_by_turn()
    raw_captures = archive.get("captures")
    if not isinstance(raw_captures, list):
        raise FormalEvaluationPipelineError("capture archive captures must be an array")
    canonical = _validate_complete_capture_matrix(
        (_capture_record(item, corpus=corpus) for item in raw_captures), corpus=corpus
    )
    if list(canonical) != raw_captures:
        raise FormalEvaluationPipelineError("capture archive does not contain canonical output/trace hashes")
    maps = unblinding.get("presentations")
    blind_rows = blind.get("presentations")
    if not isinstance(maps, list) or not isinstance(blind_rows, list) or len(maps) != len(canonical) or len(blind_rows) != len(canonical):
        raise FormalEvaluationPipelineError("blind and unblinding presentations must cover every captured output")
    captures_by_unit = {(item["variant_id"], item["scenario_turn_id"], item["seed"]): item for item in canonical}
    maps_by_id = {item.get("blind_output_id"): item for item in maps if isinstance(item, Mapping)}
    if len(maps_by_id) != len(maps):
        raise FormalEvaluationPipelineError("unblinding map blind ids must be unique")
    for row in blind_rows:
        if not isinstance(row, Mapping) or "variant_id" in row:
            raise FormalEvaluationPipelineError("blind reviewer input must not contain a variant id")
        mapped = maps_by_id.get(row.get("blind_output_id"))
        if mapped is None:
            raise FormalEvaluationPipelineError("blind reviewer input references an unknown unblinding id")
        unit = (mapped.get("variant_id"), mapped.get("scenario_turn_id"), mapped.get("seed"))
        capture = captures_by_unit.get(unit)
        if capture is None or any(
            row.get(key) != capture[key]
            for key in ("scenario_turn_id", "scenario_id", "scenario_family", "input_text", "scenario_input_hash", "scenario_fact_set_hash", "output_text", "output_hash")
        ):
            raise FormalEvaluationPipelineError("blind reviewer input does not bind the captured output")
    return packet, archive, blind, unblinding


def _mechanical_trace(raw: object) -> MechanicalTraceEvidence:
    item = _require_mapping(raw, "mechanical trace")
    keys = (
        "fixture_manifest_hash", "replay_evidence_hash", "action_receipt_evidence_hash", "affect_evidence_hash",
        "random_draw_evidence_hash", "performance_trace_hash", "hard_invariant_violations",
        "nonterminal_action_leaks", "replay_hash_mismatches", "affect_episode_invalid_clears",
        "random_draw_replay_consistency", "hot_visible_action_p95_ms",
    )
    if set(item) - set(keys) - {"random_draw_status"}:
        raise FormalEvaluationPipelineError("mechanical trace has unknown fields")
    try:
        return MechanicalTraceEvidence(**{key: item[key] for key in keys}, random_draw_status=item.get("random_draw_status", "not_applicable"))
    except (KeyError, TypeError, ValueError) as exc:
        raise FormalEvaluationPipelineError(f"invalid mechanical trace: {exc}") from exc


def _reviewed_runs(
    *, archive: Mapping[str, Any], blind: Mapping[str, Any], unblinding: Mapping[str, Any], review_records: Iterable[object]
) -> tuple[tuple[ReviewedRun, ...], tuple[EvidenceArtifactCapture, ...]]:
    captures = _require_mapping({"captures": archive.get("captures")}, "capture archive")
    records = tuple(captures["captures"] if isinstance(captures["captures"], list) else ())
    if not records:
        raise FormalEvaluationPipelineError("capture archive has no captures")
    by_unit = {(row["variant_id"], row["scenario_turn_id"], row["seed"]): row for row in records}
    maps = unblinding.get("presentations")
    if not isinstance(maps, list):
        raise FormalEvaluationPipelineError("unblinding map presentations must be an array")
    by_blind = {row.get("blind_output_id"): row for row in maps if isinstance(row, Mapping)}
    if len(by_blind) != len(maps):
        raise FormalEvaluationPipelineError("unblinding map blind identities must be unique")
    seen_reviews: set[tuple[str, str]] = set()
    review_ids: set[str] = set()
    reviewer_ids_by_blind: dict[str, set[str]] = {}
    converted: list[ReviewedRun] = []
    for raw in review_records:
        item = _require_mapping(raw, "review record")
        if any(key in item for key in ("variant_id", "scenario_turn_id", "seed", "output_hash")):
            raise FormalEvaluationPipelineError("review records must not contain unblinded output identity")
        review_id = _require_text(item.get("review_id"), "review_id")
        reviewer_id = _require_text(item.get("reviewer_id"), "reviewer_id")
        blind_id = _require_text(item.get("blind_output_id"), "review blind_output_id")
        if review_id in review_ids:
            raise FormalEvaluationPipelineError("review_id must be globally unique")
        if (blind_id, reviewer_id) in seen_reviews:
            raise FormalEvaluationPipelineError("reviewer may submit only one review per blinded output")
        seen_reviews.add((blind_id, reviewer_id))
        review_ids.add(review_id)
        mapping = by_blind.get(blind_id)
        if mapping is None:
            raise FormalEvaluationPipelineError("review references an unknown blind output")
        unit = (mapping["variant_id"], mapping["scenario_turn_id"], mapping["seed"])
        capture = by_unit.get(unit)
        if capture is None or capture["output_hash"] != mapping["output_hash"]:
            raise FormalEvaluationPipelineError("unblinding map does not bind a captured output")
        awareness: list[AwarenessEvidence] = []
        raw_awareness = item.get("awareness_evidence", [])
        if not isinstance(raw_awareness, list):
            raise FormalEvaluationPipelineError("awareness_evidence must be an array")
        for evidence in raw_awareness:
            value = _require_mapping(evidence, "awareness evidence")
            awareness.append(AwarenessEvidence(
                source=value.get("source"), reference_id=_require_text(value.get("reference_id"), "awareness reference_id"),
                response_tags=tuple(value.get("response_tags", ())), output_hash=capture["output_hash"],
            ))
        converted.append(ReviewedRun(
            variant_id=unit[0], scenario_turn_id=unit[1], seed=unit[2], output_hash=capture["output_hash"],
            judge_id=reviewer_id, judge_prompt_version=_require_text(blind["review_protocol"].get("judge_prompt_version"), "judge_prompt_version"),
            judge_model_id=_require_text(blind["review_protocol"].get("judge_model_id"), "judge_model_id"),
            rubric_scores=_require_mapping(item.get("rubric_scores"), "rubric_scores"), response_tags=tuple(item.get("response_tags", ())),
            question_ending=item.get("question_ending"), fallback_template_hit=item.get("fallback_template_hit"),
            fallback_smell_confirmed=item.get("fallback_smell_confirmed"), model_failed=item.get("model_failed"),
            asserted_alternative_as_fact=item.get("asserted_alternative_as_fact"), awareness_evidence=tuple(awareness),
            scenario_input_hash=capture["scenario_input_hash"], scenario_fact_set_hash=capture["scenario_fact_set_hash"],
            blind_output_id=blind_id, reply_eligible=item.get("reply_eligible", True),
            non_necessary_question_ending=item.get("non_necessary_question_ending"),
            used_fact_refs=tuple(item.get("used_fact_refs", ())), action_refs=tuple(item.get("action_refs", ())),
            affect_episode_refs=tuple(item.get("affect_episode_refs", ())),
        ))
        reviewer_ids_by_blind.setdefault(blind_id, set()).add(reviewer_id)
    expected_blind_ids = set(by_blind)
    missing = [blind_id for blind_id in expected_blind_ids if len(reviewer_ids_by_blind.get(blind_id, set())) < 2]
    if missing:
        raise FormalEvaluationPipelineError(f"every blinded output needs two independent reviews (missing={len(missing)})")
    evidence: list[EvidenceArtifactCapture] = []
    for capture in records:
        evidence.append(EvidenceArtifactCapture(
            source="output", reference_id=f"output:{capture['scenario_turn_id']}:{capture['seed']}",
            variant_id=capture["variant_id"], scenario_turn_id=capture["scenario_turn_id"], seed=capture["seed"],
            output_hash=capture["output_hash"], artifact_hash=capture["trace_hash"],
        ))
        for artifact in capture.get("evidence_artifacts", ()):
            evidence.append(EvidenceArtifactCapture(
                source=artifact["source"], reference_id=artifact["reference_id"],
                variant_id=capture["variant_id"], scenario_turn_id=capture["scenario_turn_id"], seed=capture["seed"],
                output_hash=capture["output_hash"], artifact_hash=artifact["artifact_hash"],
            ))
    return tuple(converted), tuple(evidence)


def finalize_formal_evaluation_report(
    *, packet_dir: Path, review_records: Iterable[object], mechanical_trace: object, output_path: Path
) -> dict[str, Any]:
    """Unblind verified reviews and write a deterministic formal report.

    This is the only stage that sees both reviewer records and the unblinding
    map.  Reviewer input remains variant-free; the generated report contains
    hashes and metrics, never captured output text.
    """

    packet, archive, blind, unblinding = _load_packet(packet_dir)
    reviewed, captures_evidence = _reviewed_runs(
        archive=archive, blind=blind, unblinding=unblinding, review_records=review_records
    )
    trace = _mechanical_trace(mechanical_trace)
    captures = archive["captures"]
    output_records = tuple(CapturedScenarioOutput(
        variant_id=item["variant_id"], scenario_turn_id=item["scenario_turn_id"], seed=item["seed"],
        scenario_input_hash=item["scenario_input_hash"], scenario_fact_set_hash=item["scenario_fact_set_hash"], output_hash=item["output_hash"],
    ) for item in captures)
    presentations = tuple(BlindPresentation(
        variant_id=item["variant_id"], scenario_turn_id=item["scenario_turn_id"], seed=item["seed"],
        blind_output_id=item["blind_output_id"], output_hash=item["output_hash"], presentation_position=next(
            index for index, row in enumerate(blind["presentations"]) if row["blind_output_id"] == item["blind_output_id"]
        ),
    ) for item in unblinding["presentations"])
    blinded_order_hash = _digest([(row["blind_output_id"], row["output_hash"], row["presentation_position"]) for row in blind["presentations"]])
    unblinding_map_hash = _digest(sorted(
        [(row["blind_output_id"], row["variant_id"], row["scenario_turn_id"], row["seed"], row["output_hash"]) for row in unblinding["presentations"]]
    ))
    protocol = _protocol_from_packet(packet, blinded_order_hash=blinded_order_hash, unblinding_map_hash=unblinding_map_hash)
    corpus = tuple(ScenarioTurn(
        scenario_turn_id=case.entry.scenario_turn_id, scenario_id=case.entry.scenario_id,
        emotional_gold=case.entry.emotional_gold, acceptable_response_tags=case.entry.acceptable_response_tags,
        scenario_family=case.entry.scenario_family, input_hash=case.entry.input_hash, fact_set_hash=case.entry.fact_set_hash,
    ) for case in SCENARIO_CASES)
    artifact_bundle = EvaluationArtifactBundle(
        protocol=protocol.artifact_identity,
        corpus=tuple(case.entry for case in SCENARIO_CASES), outputs=output_records, presentations=presentations,
        evidence_artifacts=captures_evidence, mechanical_trace=trace,
    )
    mechanical = MechanicalEvaluation(
        hard_invariant_violations=trace.hard_invariant_violations, nonterminal_action_leaks=trace.nonterminal_action_leaks,
        replay_hash_mismatches=trace.replay_hash_mismatches, affect_episode_invalid_clears=trace.affect_episode_invalid_clears,
        random_draw_replay_consistency=trace.random_draw_replay_consistency, hot_visible_action_p95_ms=trace.hot_visible_action_p95_ms,
        fixture_manifest_hash=trace.fixture_manifest_hash, replay_evidence_hash=trace.replay_evidence_hash,
        action_receipt_evidence_hash=trace.action_receipt_evidence_hash, affect_evidence_hash=trace.affect_evidence_hash,
        random_draw_evidence_hash=trace.random_draw_evidence_hash, performance_trace_hash=trace.performance_trace_hash,
        random_draw_status=trace.random_draw_status,
    )
    report = ExperienceEvaluator().evaluate(
        protocol=protocol, corpus=corpus, reviewed_runs=reviewed, mechanical_evaluation=mechanical,
        evidence_artifacts=tuple(
            EvidenceArtifact(
                source=item.source, reference_id=item.reference_id, output_hash=item.output_hash, artifact_hash=item.artifact_hash,
                variant_id=item.variant_id, scenario_turn_id=item.scenario_turn_id, seed=item.seed,
            ) for item in captures_evidence
        ), artifact_bundle=artifact_bundle,
    )
    blockers = list(report.blockers)
    if packet["artifact_source"] != "external_real":
        blockers.append("synthetic_fixture_not_external_evidence")
    payload = {
        "schema_version": FORMAL_PIPELINE_VERSION,
        "artifact_source": packet["artifact_source"],
        "report_status": "passed" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "protocol": asdict(protocol),
        "artifact_bundle": {
            "bundle_digest": artifact_bundle.verify().bundle_digest,
            "blinded_order_hash": blinded_order_hash,
            "unblinding_map_hash": unblinding_map_hash,
            "capture_count": len(captures),
            "independent_review_count": len(reviewed),
        },
        "metrics": {key: asdict(value) for key, value in report.variant_metrics.items()},
        "comparisons": {key: asdict(value) for key, value in report.comparisons.items()},
        "mechanical_evaluation": asdict(mechanical),
        "issues": [asdict(value) for value in report.issues],
        "limitations": (
            "A report is evidence only when artifact_source is external_real and captures/reviews are independently obtained.",
            "Synthetic fixtures exercise schema and CI paths; they are never human-likeness evidence.",
        ),
    }
    _write_json(output_path, payload)
    return payload


def build_synthetic_formal_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Generate complete fake data for CI structural verification only."""

    corpus = _corpus_by_turn()
    captures: list[dict[str, Any]] = []
    for variant in FORMAL_VARIANTS:
        for turn_id, (entry, _text) in corpus.items():
            for seed in FORMAL_SEEDS:
                captures.append({
                    "variant_id": variant, "scenario_turn_id": turn_id, "seed": seed,
                    "scenario_input_hash": entry.input_hash, "scenario_fact_set_hash": entry.fact_set_hash,
                    "output_text": f"synthetic {variant} {turn_id} {seed}",
                    "trace": {"fixture": True, "unit": f"{variant}/{turn_id}/{seed}"},
                })
    review_template = {dimension: 4 for dimension in RUBRIC_DIMENSIONS}
    trace = {
        "fixture_manifest_hash": "1" * 64, "replay_evidence_hash": "2" * 64,
        "action_receipt_evidence_hash": "3" * 64, "affect_evidence_hash": "4" * 64,
        "random_draw_evidence_hash": "5" * 64, "performance_trace_hash": "6" * 64,
        "hard_invariant_violations": 0, "nonterminal_action_leaks": 0, "replay_hash_mismatches": 0,
        "affect_episode_invalid_clears": 0, "random_draw_replay_consistency": 1.0,
        "hot_visible_action_p95_ms": 500.0, "random_draw_status": "not_applicable",
    }
    # Reviews are finalized after packet creation because blind ids must never
    # be guessed from variant identities.
    return captures, [{"rubric_scores": review_template}], trace


__all__ = [
    "ArtifactSource", "FORMAL_PIPELINE_VERSION", "FORMAL_SEEDS", "FORMAL_VARIANTS",
    "FormalEvaluationPipelineError", "build_synthetic_formal_fixture", "finalize_formal_evaluation_report",
    "prepare_formal_evaluation_packet",
]
