"""Pure scoped compiler from replay exports to mechanical evaluation evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

from .evaluation_artifacts import MechanicalTraceEvidence
from .human_likeness_evaluator import MechanicalEvaluation
from .mechanical_evaluation_scope import MechanicalEvaluationScope
from .replay_evaluator import ReplayEvaluator
from .replay_evidence import ReplayEvidence


class MechanicalEvaluationExportError(ValueError):
    pass


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VisibleActionLatencySample:
    sample_id: str
    startup: str
    ingress_to_visible_ms: float

    def __post_init__(self) -> None:
        if not self.sample_id.strip() or self.startup not in {"hot", "cold"}:
            raise MechanicalEvaluationExportError("latency sample identity is invalid")
        if not math.isfinite(self.ingress_to_visible_ms) or self.ingress_to_visible_ms < 0:
            raise MechanicalEvaluationExportError("latency sample must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class MechanicalEvaluationExport:
    trace: MechanicalTraceEvidence
    evaluation: MechanicalEvaluation
    finding_codes: tuple[str, ...]


class MechanicalEvaluationExporter:
    """Recompute bounded evidence; no count or score enters from the caller."""

    _TERMINAL = frozenset({"delivered", "failed", "unknown", "cancelled", "expired"})

    def export(
        self,
        *,
        scope: MechanicalEvaluationScope,
        replay_evidence: ReplayEvidence,
        latency_samples: tuple[VisibleActionLatencySample, ...],
    ) -> MechanicalEvaluationExport:
        if replay_evidence.world_id != scope.world_id:
            raise MechanicalEvaluationExportError("replay evidence world does not match fixture scope")
        if replay_evidence.cursor.ledger_sequence != scope.end_ledger_sequence:
            raise MechanicalEvaluationExportError("replay evidence cursor does not match fixture end")
        expected_samples = {item.sample_id: item.startup for item in scope.performance_samples}
        received_samples = {item.sample_id: item.startup for item in latency_samples}
        if received_samples != expected_samples or len(received_samples) != len(latency_samples):
            raise MechanicalEvaluationExportError("latency samples do not match fixture scope")
        replay_result = ReplayEvaluator().evaluate(evidence=replay_evidence)
        scoped_events = tuple(
            item for item in replay_evidence.events
            if scope.start_ledger_sequence < item.cursor.ledger_sequence <= scope.end_ledger_sequence
        )
        actions = {item.action_id: item for item in replay_evidence.projection.actions}
        expected_actions = tuple(actions.get(action_id) for action_id in scope.action_ids_expected_to_settle)
        missing_actions = sum(item is None for item in expected_actions)
        action_leaks = sum(item is not None and item.state not in self._TERMINAL for item in expected_actions)
        episodes = {item.episode_id: item for item in replay_evidence.projection.affect_episodes}
        invalid_clears = sum(
            episode is None or episode.status != assertion.required_status
            for assertion in scope.affect_assertions
            for episode in (episodes.get(assertion.episode_id),)
        )
        draw_events = tuple(
            item.event for item in scoped_events if item.event.event_type == "RandomDrawRecorded"
        )
        expected_draws = scope.random_draw_expectation
        draw_ids = {str(item.payload().get("draw_id", "")) for item in draw_events}
        random_status = expected_draws.status
        random_consistency = 1.0
        if expected_draws.status == "installed" and set(expected_draws.draw_ids) != draw_ids:
            random_status, random_consistency = "missing_required", 0.0
        replay_mismatches = sum(item.code == "replay_hash_mismatch" for item in replay_result.findings)
        hard = missing_actions + sum(
            item.severity == "error" and item.code != "replay_hash_mismatch"
            for item in replay_result.findings
        )
        hot = sorted(item.ingress_to_visible_ms for item in latency_samples if item.startup == "hot")
        hot_p95 = hot[math.ceil(0.95 * len(hot)) - 1]
        trace = MechanicalTraceEvidence(
            fixture_manifest_hash=scope.fixture_manifest_hash,
            replay_evidence_hash=_digest([(item.event_envelope_hash, item.commit_id) for item in scoped_events]),
            action_receipt_evidence_hash=_digest([
                receipt.model_dump(mode="json") for receipt in replay_evidence.projection.execution_receipts
                if receipt.action_id in scope.action_ids_expected_to_settle
            ]),
            affect_evidence_hash=_digest([
                (assertion.episode_id, episodes.get(assertion.episode_id).model_dump(mode="json") if episodes.get(assertion.episode_id) else None)
                for assertion in scope.affect_assertions
            ]),
            random_draw_evidence_hash=_digest([(item.event_id, item.payload_hash) for item in draw_events]),
            performance_trace_hash=_digest([(item.sample_id, item.startup, item.ingress_to_visible_ms) for item in sorted(latency_samples, key=lambda item: item.sample_id)]),
            hard_invariant_violations=hard,
            nonterminal_action_leaks=action_leaks,
            replay_hash_mismatches=replay_mismatches,
            affect_episode_invalid_clears=invalid_clears,
            random_draw_replay_consistency=random_consistency,
            hot_visible_action_p95_ms=hot_p95,
            random_draw_status=random_status,
        )
        return MechanicalEvaluationExport(
            trace=trace,
            evaluation=MechanicalEvaluation(
                hard_invariant_violations=hard, nonterminal_action_leaks=action_leaks,
                replay_hash_mismatches=replay_mismatches, affect_episode_invalid_clears=invalid_clears,
                random_draw_replay_consistency=random_consistency, hot_visible_action_p95_ms=hot_p95,
                fixture_manifest_hash=trace.fixture_manifest_hash, replay_evidence_hash=trace.replay_evidence_hash,
                action_receipt_evidence_hash=trace.action_receipt_evidence_hash, affect_evidence_hash=trace.affect_evidence_hash,
                random_draw_evidence_hash=trace.random_draw_evidence_hash, performance_trace_hash=trace.performance_trace_hash,
                random_draw_status=random_status,
            ),
            finding_codes=tuple(item.code for item in replay_result.findings),
        )


__all__ = ["MechanicalEvaluationExport", "MechanicalEvaluationExportError", "MechanicalEvaluationExporter", "VisibleActionLatencySample"]
