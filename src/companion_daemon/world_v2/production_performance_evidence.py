"""Mechanical evidence for the World-v2 production chat hot path.

This reader joins two existing authorities without becoming one: immutable
``ModelResultRecorded`` audits from replay evidence and process-local monotonic
latency samples.  SQLite projection counters prove access shape directly; a
fast test is not allowed to stand in for "no historical replay".
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from .production_latency_trace import ProductionLatencySample, ProductionLatencyRecorder
from .sqlite_ledger import SQLiteProjectionPerformanceCounters, SQLiteWorldLedger
from .test_economy import ModelCallTrace, model_traces_from_projection


@dataclass(frozen=True, slots=True)
class ProductionPerformanceEvidence:
    model_calls: tuple[ModelCallTrace, ...]
    latency_samples: tuple[ProductionLatencySample, ...]
    projection_counters: SQLiteProjectionPerformanceCounters


class ProductionPerformanceEvidenceReader:
    """Read bounded production evidence without exposing a ledger writer."""

    def __init__(
        self, *, ledger: SQLiteWorldLedger, latency_recorder: ProductionLatencyRecorder
    ) -> None:
        self._ledger = ledger
        self._latency = latency_recorder

    def capture(self) -> ProductionPerformanceEvidence:
        projection = self._ledger.project()
        return ProductionPerformanceEvidence(
            model_calls=model_traces_from_projection(projection=projection),
            latency_samples=self._latency.samples(),
            projection_counters=self._ledger.performance_counters(),
        )


@dataclass(frozen=True, slots=True)
class WarmChatPerformanceGateResult:
    expected_turns: int
    observed_hot_turns: int
    p95_ingress_to_visible_ms: float | None
    violations: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.violations


class WarmChatPerformanceGate:
    """Offline W2-PERF-001 gate; it makes no real-provider SLO claim."""

    def evaluate(
        self,
        *,
        before: ProductionPerformanceEvidence,
        after: ProductionPerformanceEvidence,
        expected_trace_ids: Iterable[str],
        expected_turn_ids: Iterable[str],
        p95_limit_ms: float = 5_000,
    ) -> WarmChatPerformanceGateResult:
        expected = tuple(expected_trace_ids)
        turns = tuple(expected_turn_ids)
        if len(expected) != 20 or len(set(expected)) != 20:
            raise ValueError("warm chat gate requires exactly twenty unique trace ids")
        if len(turns) != 20 or len(set(turns)) != 20:
            raise ValueError("warm chat gate requires exactly twenty unique turn ids")
        if not math.isfinite(p95_limit_ms) or p95_limit_ms <= 0:
            raise ValueError("warm chat P95 limit must be positive and finite")
        expected_set = set(expected)
        visible = {
            sample.trace_id: sample.duration_ms
            for sample in after.latency_samples
            if sample.trace_id in expected_set
            and sample.startup == "hot"
            and sample.environment == "offline_in_process"
            and sample.segment == "ingress_to_visible"
        }
        violations: list[str] = []
        if set(visible) != expected_set:
            violations.append("twenty_hot_visible_samples_required")
        p95 = _nearest_rank(tuple(visible.values()), 0.95) if visible else None
        if p95 is not None and p95 > p95_limit_ms:
            violations.append("offline_hot_p95_exceeded")
        turn_set = set(turns)
        calls = tuple(call for call in after.model_calls if call.turn_id in turn_set)
        by_turn = {turn_id: [] for turn_id in turns}
        for call in calls:
            by_turn[call.turn_id].append(call)
        if any(
            len(turn_calls) != 1
            or turn_calls[0].route_class != "chat"
            or turn_calls[0].model_tier != "flash"
            or (turn_calls[0].thinking_tokens or 0) != 0
            for turn_calls in by_turn.values()
        ):
            violations.append("ordinary_turn_requires_one_metered_flash_call")
        if (
            after.projection_counters.historical_replay_calls
            != before.projection_counters.historical_replay_calls
            or after.projection_counters.total_replay_calls
            != before.projection_counters.total_replay_calls
        ):
            violations.append("hot_path_historical_replay_detected")
        return WarmChatPerformanceGateResult(
            expected_turns=20,
            observed_hot_turns=len(visible),
            p95_ingress_to_visible_ms=p95,
            violations=tuple(violations),
        )


def _nearest_rank(values: tuple[float, ...], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


__all__ = [
    "ProductionPerformanceEvidence",
    "ProductionPerformanceEvidenceReader",
    "WarmChatPerformanceGate",
    "WarmChatPerformanceGateResult",
]
