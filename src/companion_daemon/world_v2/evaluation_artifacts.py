"""Immutable evidence bundle for one reproducible companion evaluation run.

This module is deliberately independent of ledger and model adapters.  Those
adapters export hashes and immutable captures; this deep module verifies that
they belong to one protocol/corpus/output/blinding experiment before a judge
or evaluator can use them.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


class EvaluationArtifactError(ValueError):
    """Artifact inputs cannot establish a reproducible experiment."""


def _hash(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_hash(name: str, value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise EvaluationArtifactError(f"{name} must be a lowercase sha256 digest")


def corpus_digest(entries: tuple[ScenarioCorpusEntry, ...]) -> str:
    return _hash(
        [
            (
                item.scenario_turn_id,
                item.scenario_id,
                item.scenario_family,
                item.emotional_gold,
                item.acceptable_response_tags,
                item.input_hash,
                item.fact_set_hash,
            )
            for item in sorted(entries, key=lambda item: item.scenario_turn_id)
        ]
    )


def evaluation_contract_digest(contract: object) -> str:
    """Hash every policy choice that changes what an evaluation means."""

    return _hash(contract)


@dataclass(frozen=True, slots=True)
class ProtocolIdentity:
    protocol_version: str
    scenario_set_version: str
    rubric_version: str
    statistics_version: str
    evaluation_contract_hash: str

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
            raise EvaluationArtifactError("protocol identity requires every version")
        _require_hash("evaluation contract hash", self.evaluation_contract_hash)

    @property
    def digest(self) -> str:
        return _hash(
            {
                "protocol_version": self.protocol_version,
                "scenario_set_version": self.scenario_set_version,
                "rubric_version": self.rubric_version,
                "statistics_version": self.statistics_version,
                "evaluation_contract_hash": self.evaluation_contract_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class ScenarioCorpusEntry:
    scenario_turn_id: str
    scenario_id: str
    scenario_family: str
    emotional_gold: bool
    acceptable_response_tags: tuple[str, ...]
    input_hash: str
    fact_set_hash: str

    def __post_init__(self) -> None:
        if not self.scenario_turn_id.strip() or not self.scenario_id.strip():
            raise EvaluationArtifactError("scenario and scenario-turn ids are required")
        if any(not item.strip() for item in self.acceptable_response_tags):
            raise EvaluationArtifactError("acceptable response tags must be non-empty")
        if len(set(self.acceptable_response_tags)) != len(self.acceptable_response_tags):
            raise EvaluationArtifactError("acceptable response tags must be unique")
        _require_hash("scenario input hash", self.input_hash)
        _require_hash("scenario fact-set hash", self.fact_set_hash)


@dataclass(frozen=True, slots=True)
class CapturedScenarioOutput:
    variant_id: str
    scenario_turn_id: str
    seed: str
    scenario_input_hash: str
    scenario_fact_set_hash: str
    output_hash: str

    @classmethod
    def from_text(
        cls,
        *,
        variant_id: str,
        scenario_turn_id: str,
        seed: str,
        scenario_input_hash: str,
        scenario_fact_set_hash: str,
        text: str,
    ) -> CapturedScenarioOutput:
        return cls(
            variant_id=variant_id,
            scenario_turn_id=scenario_turn_id,
            seed=seed,
            scenario_input_hash=scenario_input_hash,
            scenario_fact_set_hash=scenario_fact_set_hash,
            output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def __post_init__(self) -> None:
        if not self.variant_id.strip() or not self.scenario_turn_id.strip() or not self.seed.strip():
            raise EvaluationArtifactError("captured output identity is required")
        _require_hash("captured input hash", self.scenario_input_hash)
        _require_hash("captured fact-set hash", self.scenario_fact_set_hash)
        _require_hash("captured output hash", self.output_hash)

    @property
    def unit_key(self) -> tuple[str, str, str]:
        return (self.variant_id, self.scenario_turn_id, self.seed)


@dataclass(frozen=True, slots=True)
class BlindPresentation:
    variant_id: str
    scenario_turn_id: str
    seed: str
    blind_output_id: str
    output_hash: str
    presentation_position: int

    def __post_init__(self) -> None:
        if (
            not self.variant_id.strip()
            or not self.scenario_turn_id.strip()
            or not self.seed.strip()
            or not self.blind_output_id.strip()
            or self.presentation_position < 0
        ):
            raise EvaluationArtifactError("blind presentation identity is invalid")
        _require_hash("blind presentation output hash", self.output_hash)

    @property
    def unit_key(self) -> tuple[str, str, str]:
        return (self.variant_id, self.scenario_turn_id, self.seed)


@dataclass(frozen=True, slots=True)
class EvidenceArtifactCapture:
    """One hash-addressed output, proposal, or affect trace used in review."""

    source: str
    reference_id: str
    variant_id: str
    scenario_turn_id: str
    seed: str
    output_hash: str
    artifact_hash: str

    def __post_init__(self) -> None:
        if self.source not in {"output", "proposal", "affect_episode"}:
            raise EvaluationArtifactError("evidence artifact source is invalid")
        if (
            not self.reference_id.strip()
            or not self.variant_id.strip()
            or not self.scenario_turn_id.strip()
            or not self.seed.strip()
        ):
            raise EvaluationArtifactError("evidence artifact identity is required")
        _require_hash("evidence artifact output hash", self.output_hash)
        _require_hash("evidence artifact hash", self.artifact_hash)

    @property
    def unit_key(self) -> tuple[str, str, str]:
        return (self.variant_id, self.scenario_turn_id, self.seed)


@dataclass(frozen=True, slots=True)
class MechanicalTraceEvidence:
    fixture_manifest_hash: str
    replay_evidence_hash: str
    action_receipt_evidence_hash: str
    affect_evidence_hash: str
    random_draw_evidence_hash: str
    performance_trace_hash: str
    hard_invariant_violations: int
    nonterminal_action_leaks: int
    replay_hash_mismatches: int
    affect_episode_invalid_clears: int
    random_draw_replay_consistency: float
    hot_visible_action_p95_ms: float
    random_draw_status: str = "not_applicable"

    def __post_init__(self) -> None:
        for name, value in self._values.items():
            _require_hash(name.replace("_", " "), value)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                self.hard_invariant_violations,
                self.nonterminal_action_leaks,
                self.replay_hash_mismatches,
                self.affect_episode_invalid_clears,
            )
        ):
            raise EvaluationArtifactError("mechanical failure counts must be non-negative integers")
        if not 0 <= self.random_draw_replay_consistency <= 1:
            raise EvaluationArtifactError("random draw replay consistency must be between zero and one")
        if self.random_draw_status not in {"installed", "not_applicable", "missing_required"}:
            raise EvaluationArtifactError("random draw status is invalid")
        if self.hot_visible_action_p95_ms < 0:
            raise EvaluationArtifactError("hot visible action p95 must be non-negative")

    @property
    def digest(self) -> str:
        return _hash({"evidence": self._values, "measurements": self.measurements})

    @property
    def _values(self) -> dict[str, str]:
        return {
            "fixture_manifest_hash": self.fixture_manifest_hash,
            "replay_evidence_hash": self.replay_evidence_hash,
            "action_receipt_evidence_hash": self.action_receipt_evidence_hash,
            "affect_evidence_hash": self.affect_evidence_hash,
            "random_draw_evidence_hash": self.random_draw_evidence_hash,
            "performance_trace_hash": self.performance_trace_hash,
        }

    @property
    def measurements(self) -> dict[str, int | float]:
        return {
            "hard_invariant_violations": self.hard_invariant_violations,
            "nonterminal_action_leaks": self.nonterminal_action_leaks,
            "replay_hash_mismatches": self.replay_hash_mismatches,
            "affect_episode_invalid_clears": self.affect_episode_invalid_clears,
            "random_draw_replay_consistency": self.random_draw_replay_consistency,
            "random_draw_status": self.random_draw_status,
            "hot_visible_action_p95_ms": self.hot_visible_action_p95_ms,
        }


@dataclass(frozen=True, slots=True)
class VerifiedEvaluationArtifacts:
    protocol_digest: str
    corpus_hash: str
    blinding_hash: str
    blinded_order_hash: str
    unblinding_map_hash: str
    mechanical_trace_hash: str
    bundle_digest: str
    outputs: tuple[CapturedScenarioOutput, ...]
    presentations: tuple[BlindPresentation, ...]
    evidence_artifacts: tuple[EvidenceArtifactCapture, ...]
    mechanical_trace: MechanicalTraceEvidence

    def presentation_for(self, *, unit_key: tuple[str, str, str]) -> BlindPresentation:
        for item in self.presentations:
            if item.unit_key == unit_key:
                return item
        raise EvaluationArtifactError("captured output instance has no verified blind presentation")


@dataclass(frozen=True, slots=True)
class EvaluationArtifactBundle:
    protocol: ProtocolIdentity
    corpus: tuple[ScenarioCorpusEntry, ...]
    outputs: tuple[CapturedScenarioOutput, ...]
    presentations: tuple[BlindPresentation, ...]
    mechanical_trace: MechanicalTraceEvidence
    evidence_artifacts: tuple[EvidenceArtifactCapture, ...] = ()

    @property
    def corpus_hash(self) -> str:
        return corpus_digest(self.corpus)

    @property
    def blinding_hash(self) -> str:
        return _hash(
            [
                (
                    item.blind_output_id,
                    item.variant_id,
                    item.scenario_turn_id,
                    item.seed,
                    item.output_hash,
                    item.presentation_position,
                )
                for item in sorted(self.presentations, key=lambda item: item.presentation_position)
            ]
        )

    @property
    def blinded_order_hash(self) -> str:
        return _hash(
            [
                (item.blind_output_id, item.output_hash, item.presentation_position)
                for item in sorted(self.presentations, key=lambda item: item.presentation_position)
            ]
        )

    @property
    def unblinding_map_hash(self) -> str:
        return _hash(
            [
                (
                    item.blind_output_id,
                    item.variant_id,
                    item.scenario_turn_id,
                    item.seed,
                    item.output_hash,
                )
                for item in sorted(self.presentations, key=lambda item: item.blind_output_id)
            ]
        )

    def verify(self) -> VerifiedEvaluationArtifacts:
        corpus_by_turn = {item.scenario_turn_id: item for item in self.corpus}
        if not corpus_by_turn or len(corpus_by_turn) != len(self.corpus):
            raise EvaluationArtifactError("corpus requires unique scenario-turn entries")
        if not self.outputs:
            raise EvaluationArtifactError("evaluation bundle requires captured outputs")
        if len({item.unit_key for item in self.outputs}) != len(self.outputs):
            raise EvaluationArtifactError("captured output unit identities must be unique")
        for output in self.outputs:
            scenario = corpus_by_turn.get(output.scenario_turn_id)
            if scenario is None:
                raise EvaluationArtifactError("captured output references a missing corpus turn")
            if (
                output.scenario_input_hash != scenario.input_hash
                or output.scenario_fact_set_hash != scenario.fact_set_hash
            ):
                raise EvaluationArtifactError("captured output does not bind the corpus input and facts")
        outputs_by_unit = {item.unit_key: item for item in self.outputs}
        presentation_units = [item.unit_key for item in self.presentations]
        if (
            len(self.presentations) != len(self.outputs)
            or set(presentation_units) != set(outputs_by_unit)
            or len(set(presentation_units)) != len(presentation_units)
        ):
            raise EvaluationArtifactError(
                "blind presentation does not cover every captured output instance exactly once"
            )
        if any(
            outputs_by_unit[item.unit_key].output_hash != item.output_hash
            for item in self.presentations
        ):
            raise EvaluationArtifactError("blind presentation output hash does not match its captured output")
        evidence_keys = {
            (item.source, item.reference_id, item.unit_key) for item in self.evidence_artifacts
        }
        if len(evidence_keys) != len(self.evidence_artifacts):
            raise EvaluationArtifactError("evidence artifact identities must be unique")
        if any(
            item.unit_key not in outputs_by_unit
            or outputs_by_unit[item.unit_key].output_hash != item.output_hash
            for item in self.evidence_artifacts
        ):
            raise EvaluationArtifactError("evidence artifact does not bind a captured output instance")
        if (
            len({item.blind_output_id for item in self.presentations}) != len(self.presentations)
            or len({item.presentation_position for item in self.presentations}) != len(self.presentations)
        ):
            raise EvaluationArtifactError("blind presentation identities and positions must be unique")
        bundle_digest = _hash(
            {
                "protocol": self.protocol.digest,
                "corpus": self.corpus_hash,
                "outputs": sorted(
                    (
                        item.variant_id,
                        item.scenario_turn_id,
                        item.seed,
                        item.output_hash,
                    )
                    for item in self.outputs
                ),
                "blinding": self.blinding_hash,
                "evidence_artifacts": sorted(
                    (
                        item.source,
                        item.reference_id,
                        item.variant_id,
                        item.scenario_turn_id,
                        item.seed,
                        item.output_hash,
                        item.artifact_hash,
                    )
                    for item in self.evidence_artifacts
                ),
                "mechanical": self.mechanical_trace.digest,
            }
        )
        return VerifiedEvaluationArtifacts(
            protocol_digest=self.protocol.digest,
            corpus_hash=self.corpus_hash,
            blinding_hash=self.blinding_hash,
            blinded_order_hash=self.blinded_order_hash,
            unblinding_map_hash=self.unblinding_map_hash,
            mechanical_trace_hash=self.mechanical_trace.digest,
            bundle_digest=bundle_digest,
            outputs=tuple(sorted(self.outputs, key=lambda item: item.unit_key)),
            presentations=tuple(sorted(self.presentations, key=lambda item: item.presentation_position)),
            evidence_artifacts=tuple(
                sorted(
                    self.evidence_artifacts,
                    key=lambda item: (item.source, item.reference_id, item.unit_key),
                )
            ),
            mechanical_trace=self.mechanical_trace,
        )


__all__ = [
    "BlindPresentation",
    "CapturedScenarioOutput",
    "corpus_digest",
    "EvidenceArtifactCapture",
    "evaluation_contract_digest",
    "EvaluationArtifactBundle",
    "EvaluationArtifactError",
    "MechanicalTraceEvidence",
    "ProtocolIdentity",
    "ScenarioCorpusEntry",
    "VerifiedEvaluationArtifacts",
]
