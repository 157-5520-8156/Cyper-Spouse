"""Executable, provenance-aware Phase-8 cost and latency gates.

The World v2 ledger already persists the model route and provider-reported
input/output token counts in ``ModelResultRecorded``.  This module turns that
authority into a small, versioned *mechanical* contract.  It deliberately
does not estimate a real provider bill or claim a network SLO from an offline
test run:

* a missing CostProfile blocks a paid action;
* token counts are accepted only with an explicit provenance;
* a thinking route without a separately reported thinking-token count fails;
* in-process latency samples remain diagnostic until a real transport trace
  declares itself as such.

It is intentionally a pure reader/gate.  It never writes the ledger, routes a
model, or dispatches an Action.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import json
import math
from typing import Callable, Iterable, Literal, Mapping

from .proposal_audit_schemas import ModelResultAuditProjection, RecordedModelResultAudit
from .replay_evidence import ReplayEvidence
from .schemas import LedgerProjection


RouteClass = Literal[
    "chat", "expressive", "world_action", "deep_deliberation", "quick_recovery", "unclassified"
]
TokenProvenance = Literal["provider_reported", "offline_estimated", "unknown"]
StartupClass = Literal["hot", "cold"]
TraceEnvironment = Literal["offline_in_process", "real_transport"]
LatencyEvidenceStatus = Literal["not_measured", "incomplete", "measured"]

_PROFILE_ROUTE_CLASSES = frozenset({"chat", "expressive", "world_action", "deep_deliberation", "quick_recovery"})
_TRACE_ROUTE_CLASSES = _PROFILE_ROUTE_CLASSES | {"unclassified"}
_TRACE_ENVIRONMENTS = frozenset({"offline_in_process", "real_transport"})
_REQUIRED_TRACE_SEGMENTS = frozenset(
    {
        "coalescing",
        "queue",
        "snapshot",
        "context",
        "ledger_commit",
        "advisor",
        "model_ttft",
        "model_completion",
        "acceptance",
        "dispatch",
        "receipt",
        "ingress_to_visible",
    }
)


class EconomyTraceError(ValueError):
    """The trace or profile cannot establish a trustworthy mechanical gate."""


@dataclass(frozen=True, slots=True)
class RouteCostLimit:
    """Bounded model usage for one semantic route in one user turn."""

    max_model_calls_per_turn: int
    max_input_tokens: int
    max_output_tokens: int
    max_thinking_tokens: int
    timeout_ms: int
    permitted_model_tiers: tuple[Literal["flash", "thinking"], ...]

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                self.max_model_calls_per_turn,
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_thinking_tokens,
                self.timeout_ms,
            )
        ):
            raise EconomyTraceError("route limits must be non-negative integers")
        if self.max_model_calls_per_turn < 1 or self.timeout_ms < 1:
            raise EconomyTraceError("route must permit at least one call and a timeout")
        if not self.permitted_model_tiers or len(set(self.permitted_model_tiers)) != len(
            self.permitted_model_tiers
        ):
            raise EconomyTraceError("route must contain unique permitted model tiers")
        if "thinking" not in self.permitted_model_tiers and self.max_thinking_tokens:
            raise EconomyTraceError("a non-thinking route cannot reserve thinking tokens")


@dataclass(frozen=True, slots=True)
class CostProfile:
    """A deployment-owned budget profile, never a domain default.

    Amounts are integers in the deployment's smallest currency unit.  The
    offline profile intentionally has no paid-action authority: testing a
    route must not accidentally create a paid provider Action.
    """

    profile_id: str
    currency: str
    effective_at: str
    per_route: Mapping[RouteClass, RouteCostLimit]
    daily_by_category: Mapping[str, int]
    per_action_caps: Mapping[str, int]
    proactive_daily_cap: int
    media_daily_cap: int
    warning_thresholds: tuple[int, ...]
    hard_stop_thresholds: tuple[int, ...]
    allows_paid_actions: bool
    accepted_token_provenance: tuple[TokenProvenance, ...]
    requires_cost_accounting: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.currency.strip() or not self.effective_at.strip():
            raise EconomyTraceError("cost profile identity is required")
        required = _PROFILE_ROUTE_CLASSES
        if set(self.per_route) != required:
            raise EconomyTraceError("cost profile must define every World v2 route")
        if not self.accepted_token_provenance:
            raise EconomyTraceError("cost profile must define token provenance")
        if len(set(self.accepted_token_provenance)) != len(self.accepted_token_provenance):
            raise EconomyTraceError("token provenance values must be unique")
        for category, amount in {**self.daily_by_category, **self.per_action_caps}.items():
            if not category or not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise EconomyTraceError("daily category cap is invalid")
        for value in (
            self.proactive_daily_cap,
            self.media_daily_cap,
            *self.warning_thresholds,
            *self.hard_stop_thresholds,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise EconomyTraceError("cost threshold is invalid")
        if tuple(sorted(set(self.warning_thresholds))) != self.warning_thresholds:
            raise EconomyTraceError("warning thresholds must be sorted and unique")
        if tuple(sorted(set(self.hard_stop_thresholds))) != self.hard_stop_thresholds:
            raise EconomyTraceError("hard stop thresholds must be sorted and unique")


# The fixed CI profile from the frozen design.  It admits only deterministic
# offline estimates, never paid Actions; production must inject its own
# provider-reported profile at composition time.
TEST_ECONOMY_V1 = CostProfile(
    profile_id="test-economy-v1",
    currency="TEST",
    effective_at="2026-07-16T00:00:00Z",
    per_route={
        "chat": RouteCostLimit(1, 12_000, 2_048, 0, 5_000, ("flash",)),
        "expressive": RouteCostLimit(2, 16_000, 3_072, 512, 7_000, ("flash", "thinking")),
        "world_action": RouteCostLimit(1, 12_000, 2_048, 0, 6_000, ("flash",)),
        "deep_deliberation": RouteCostLimit(1, 20_000, 4_096, 2_048, 12_000, ("thinking",)),
        "quick_recovery": RouteCostLimit(1, 6_000, 1_024, 0, 2_000, ("flash",)),
    },
    daily_by_category={
        "chat": 0,
        "repair": 0,
        "audit": 0,
        "proactive": 0,
        "vision": 0,
        "audio": 0,
        "image": 0,
        "tool": 0,
    },
    per_action_caps={},
    proactive_daily_cap=0,
    media_daily_cap=0,
    warning_thresholds=(),
    hard_stop_thresholds=(),
    allows_paid_actions=False,
    accepted_token_provenance=("offline_estimated",),
)


@dataclass(frozen=True, slots=True)
class ModelCallTrace:
    """One immutable model call as seen at the audit/replay boundary."""

    turn_id: str
    route_class: RouteClass
    model_call_id: str
    attempt_id: str
    attempt_index: int
    model_id: str | None
    model_version: str | None
    model_tier: Literal["flash", "thinking"]
    route_reason_code: str
    router_version: str
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    token_provenance: TokenProvenance | None
    status: str
    cost_category: str | None = None
    cost_units: int | None = None

    def __post_init__(self) -> None:
        if self.route_class not in _TRACE_ROUTE_CLASSES:
            raise EconomyTraceError("model trace route class is invalid")
        if self.model_tier not in {"flash", "thinking"}:
            raise EconomyTraceError("model trace tier is invalid")
        if self.token_provenance not in {None, "provider_reported", "offline_estimated", "unknown"}:
            raise EconomyTraceError("token provenance is invalid")
        if not isinstance(self.attempt_index, int) or isinstance(self.attempt_index, bool) or self.attempt_index < 0:
            raise EconomyTraceError("model trace attempt index is invalid")
        if (
            not self.turn_id
            or not self.model_call_id
            or not self.attempt_id
            or not self.route_reason_code
            or not self.router_version
        ):
            raise EconomyTraceError("model trace identity is required")
        for value in (self.input_tokens, self.output_tokens, self.thinking_tokens):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise EconomyTraceError("token trace values must be non-negative integers")
        if self.token_provenance is None and any(
            value is not None for value in (self.input_tokens, self.output_tokens, self.thinking_tokens)
        ):
            raise EconomyTraceError("token counts require an explicit provenance")
        if self.model_tier == "flash" and self.thinking_tokens not in (None, 0):
            raise EconomyTraceError("flash traces cannot report thinking tokens")
        if (self.cost_category is None) != (self.cost_units is None):
            raise EconomyTraceError("cost category and cost units must be supplied together")
        if self.cost_units is not None and (
            not isinstance(self.cost_units, int) or isinstance(self.cost_units, bool) or self.cost_units < 0
        ):
            raise EconomyTraceError("cost units must be a non-negative integer")
        if self.cost_category is not None and not self.cost_category:
            raise EconomyTraceError("cost category must not be empty")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> "ModelCallTrace":
        return cls(
            turn_id=str(value["turn_id"]),
            route_class=value["route_class"],  # type: ignore[arg-type]
            model_call_id=str(value["model_call_id"]),
            attempt_id=str(value["attempt_id"]),
            attempt_index=value["attempt_index"],  # type: ignore[arg-type]
            model_id=value.get("model_id") if isinstance(value.get("model_id"), str) else None,
            model_version=(
                value.get("model_version") if isinstance(value.get("model_version"), str) else None
            ),
            model_tier=value["model_tier"],  # type: ignore[arg-type]
            route_reason_code=str(value["route_reason_code"]),
            router_version=str(value["router_version"]),
            input_tokens=value.get("input_tokens"),  # type: ignore[arg-type]
            output_tokens=value.get("output_tokens"),  # type: ignore[arg-type]
            thinking_tokens=value.get("thinking_tokens"),  # type: ignore[arg-type]
            token_provenance=value.get("token_provenance"),  # type: ignore[arg-type]
            cost_category=value.get("cost_category") if isinstance(value.get("cost_category"), str) else None,
            cost_units=value.get("cost_units"),  # type: ignore[arg-type]
            status=str(value["status"]),
        )

    def as_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TraceViolation:
    code: str
    turn_id: str
    route_class: RouteClass
    detail: str

    def as_json(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CostGateResult:
    profile_id: str
    traces: tuple[ModelCallTrace, ...]
    violations: tuple[TraceViolation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations

    def as_json(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "passed": self.passed,
            "traces": [trace.as_json() for trace in self.traces],
            "violations": [violation.as_json() for violation in self.violations],
        }


class CostProfileGate:
    """Validate a complete trace against a versioned profile."""

    def evaluate(
        self, *, profile: CostProfile, traces: Iterable[ModelCallTrace]
    ) -> CostGateResult:
        materialized = tuple(traces)
        violations: list[TraceViolation] = []
        grouped: dict[tuple[str, RouteClass], list[ModelCallTrace]] = defaultdict(list)
        seen_ids: set[str] = set()
        for trace in materialized:
            if trace.model_call_id in seen_ids:
                violations.append(
                    TraceViolation("duplicate_model_call_id", trace.turn_id, trace.route_class, trace.model_call_id)
                )
            seen_ids.add(trace.model_call_id)
            grouped[(trace.turn_id, trace.route_class)].append(trace)
        for (turn_id, route_class), calls in sorted(grouped.items()):
            if route_class == "unclassified":
                violations.append(
                    TraceViolation(
                        "route_class_unclassified",
                        turn_id,
                        route_class,
                        "persist semantic route class before applying a profile",
                    )
                )
                continue
            limit = profile.per_route[route_class]
            if len(calls) > limit.max_model_calls_per_turn:
                violations.append(
                    TraceViolation(
                        "model_call_budget_exceeded",
                        turn_id,
                        route_class,
                        f"calls={len(calls)} max={limit.max_model_calls_per_turn}",
                    )
                )
            for trace in calls:
                violations.extend(self._validate_call(profile, limit, trace))
            violations.extend(self._validate_aggregate(profile, turn_id, route_class, calls))
            violations.extend(self._validate_recovery_shape(turn_id, route_class, calls))
        violations.extend(self._validate_daily_cost(profile, materialized))
        return CostGateResult(profile.profile_id, materialized, tuple(violations))

    @staticmethod
    def _validate_call(
        profile: CostProfile, limit: RouteCostLimit, trace: ModelCallTrace
    ) -> list[TraceViolation]:
        failures: list[TraceViolation] = []
        def add(code: str, detail: str) -> None:
            failures.append(TraceViolation(code, trace.turn_id, trace.route_class, detail))

        if trace.model_tier not in limit.permitted_model_tiers:
            add("model_tier_not_permitted", trace.model_tier)
        if trace.input_tokens is None or trace.output_tokens is None:
            add("token_accounting_missing", "input/output tokens are required")
        if trace.token_provenance not in profile.accepted_token_provenance:
            add("token_provenance_not_permitted", str(trace.token_provenance))
        if trace.input_tokens is not None and trace.input_tokens > limit.max_input_tokens:
            add("input_token_budget_exceeded", f"input={trace.input_tokens} max={limit.max_input_tokens}")
        if trace.output_tokens is not None and trace.output_tokens > limit.max_output_tokens:
            add("output_token_budget_exceeded", f"output={trace.output_tokens} max={limit.max_output_tokens}")
        if trace.model_tier == "thinking" and trace.thinking_tokens is None:
            add("thinking_token_accounting_missing", "thinking route requires provider-reported thinking tokens")
        if trace.thinking_tokens is not None and trace.thinking_tokens > limit.max_thinking_tokens:
            add(
                "thinking_token_budget_exceeded",
                f"thinking={trace.thinking_tokens} max={limit.max_thinking_tokens}",
            )
        return failures

    @staticmethod
    def _validate_aggregate(
        profile: CostProfile, turn_id: str, route_class: RouteClass, calls: list[ModelCallTrace]
    ) -> list[TraceViolation]:
        limit = profile.per_route[route_class]
        failures: list[TraceViolation] = []
        for name, values, ceiling in (
            ("input", tuple(item.input_tokens for item in calls), limit.max_input_tokens),
            ("output", tuple(item.output_tokens for item in calls), limit.max_output_tokens),
            ("thinking", tuple(item.thinking_tokens for item in calls), limit.max_thinking_tokens),
        ):
            if any(value is None for value in values):
                continue
            total = sum(value for value in values if value is not None)
            if total > ceiling:
                failures.append(
                    TraceViolation(
                        f"{name}_token_turn_budget_exceeded",
                        turn_id,
                        route_class,
                        f"{name}={total} max={ceiling}",
                    )
                )
        return failures

    @staticmethod
    def _validate_daily_cost(
        profile: CostProfile, traces: tuple[ModelCallTrace, ...]
    ) -> list[TraceViolation]:
        failures: list[TraceViolation] = []
        by_category: dict[str, int] = defaultdict(int)
        for trace in traces:
            if profile.requires_cost_accounting and trace.cost_units is None:
                failures.append(
                    TraceViolation("cost_accounting_missing", trace.turn_id, trace.route_class, "missing cost units")
                )
            if trace.cost_category is not None and trace.cost_units is not None:
                by_category[trace.cost_category] += trace.cost_units
                per_action_cap = profile.per_action_caps.get(trace.cost_category)
                if per_action_cap is not None and trace.cost_units > per_action_cap:
                    failures.append(
                        TraceViolation(
                            "per_action_cost_cap_exceeded",
                            trace.turn_id,
                            trace.route_class,
                            f"category={trace.cost_category} cost={trace.cost_units} max={per_action_cap}",
                        )
                    )
        for category, total in by_category.items():
            cap = profile.daily_by_category.get(category)
            if cap is None or total > cap:
                failures.append(
                    TraceViolation(
                        "daily_cost_cap_exceeded",
                        "daily:" + category,
                        "world_action",
                        f"category={category} cost={total} max={cap}",
                    )
                )
        return failures

    @staticmethod
    def _validate_recovery_shape(
        turn_id: str, route_class: RouteClass, calls: list[ModelCallTrace]
    ) -> list[TraceViolation]:
        """The test profile permits one expressive parse recovery, not two mains."""

        if route_class != "expressive" or len(calls) < 2:
            return []
        ordered = sorted(calls, key=lambda item: item.attempt_index)
        main, recovery = ordered[0], ordered[1]
        if (
            main.attempt_index != 0
            or recovery.attempt_index != 1
            or main.status != "main_invalid"
            or recovery.status != "main_invalid_recovered"
        ):
            return [
                TraceViolation(
                    "expressive_recovery_not_structural",
                    turn_id,
                    route_class,
                    "second expressive call requires main_invalid -> main_invalid_recovered lineage",
                )
            ]
        return []


def require_paid_action_profile(
    *, profile: CostProfile | None, category: str, estimated_cost: int
) -> None:
    """Fail closed before a paid Action can be created.

    This belongs at the composition/Action planning seam.  It is kept pure so
    callers can invoke it *before* reserving a budget or emitting an Action.
    """

    if not category or not isinstance(estimated_cost, int) or estimated_cost < 0:
        raise EconomyTraceError("paid action category/cost is invalid")
    if profile is None:
        raise EconomyTraceError("paid_action_profile_missing")
    if not profile.allows_paid_actions:
        raise EconomyTraceError("paid_action_not_permitted_by_profile")
    cap = profile.daily_by_category.get(category)
    if cap is None or estimated_cost > cap:
        raise EconomyTraceError("paid_action_category_cap_exceeded")


RouteClassifier = Callable[[RecordedModelResultAudit, ModelResultAuditProjection], RouteClass]


def default_route_classifier(
    audit: RecordedModelResultAudit, _projection: ModelResultAuditProjection
) -> RouteClass:
    """Conservative classifier for existing audit schema.

    Legacy audit.1 records have tier but no semantic route-class field.  A
    model ID is not authority for one, so all Flash calls remain unclassified.
    audit.2 persists route_class in the provider-bound usage material and is
    read directly by ``model_traces_from_replay`` below.
    """

    if audit.route.tier == "thinking":
        return "deep_deliberation"
    return "unclassified"


def model_traces_from_replay(
    *,
    evidence: ReplayEvidence,
    classifier: RouteClassifier = default_route_classifier,
    token_provenance: TokenProvenance = "unknown",
    thinking_tokens_by_call: Mapping[str, int] | None = None,
) -> tuple[ModelCallTrace, ...]:
    """Extract exactly persisted model call audits from one replay snapshot.

    audit.2 usage material is authoritative for route, token provenance and
    thinking usage.  audit.1 remains replayable, but is deliberately emitted
    as unknown/unclassified so every profile fails closed.  The optional
    arguments exist only for legacy diagnostic tooling and never upgrade an
    audit.1 record into provider-provenance.
    """

    return model_traces_from_projection(
        projection=evidence.projection,
        classifier=classifier,
        token_provenance=token_provenance,
        thinking_tokens_by_call=thinking_tokens_by_call,
    )


def model_traces_from_projection(
    *,
    projection: LedgerProjection,
    classifier: RouteClassifier = default_route_classifier,
    token_provenance: TokenProvenance = "unknown",
    thinking_tokens_by_call: Mapping[str, int] | None = None,
) -> tuple[ModelCallTrace, ...]:
    """Extract persisted model audits from the authenticated current head.

    Production hot-path metrics use the incrementally persisted SQLite head;
    exporting a full replay-evidence bundle merely to read this projection
    would itself perform history work and contaminate the performance gate.
    """

    thinking = dict(thinking_tokens_by_call or {})
    traces: list[ModelCallTrace] = []
    for audit_projection in projection.model_result_audits:
        audit = RecordedModelResultAudit.model_validate_json(audit_projection.audit_json)
        usage = audit.usage
        route_class = (
            usage.route_class
            if usage is not None
            else classifier(audit, audit_projection)
        )
        traces.append(
            ModelCallTrace(
                turn_id=audit_projection.trigger_ref,
                route_class=route_class,
                model_call_id=audit.model_call_id,
                attempt_id=audit.attempt_id,
                attempt_index=audit_projection.attempt_index,
                model_id=audit.model_id,
                model_version=audit.model_version,
                model_tier=audit.route.tier,
                route_reason_code=audit.route.reason_code,
                router_version=audit.route.router_version,
                input_tokens=usage.input_tokens if usage is not None else audit.input_tokens,
                output_tokens=usage.output_tokens if usage is not None else audit.output_tokens,
                thinking_tokens=(
                    usage.thinking_tokens
                    if usage is not None
                    else 0
                    if audit.route.tier == "flash" and audit.input_tokens is not None and audit.output_tokens is not None
                    else thinking.get(audit.model_call_id)
                ),
                token_provenance=(
                    usage.token_provenance
                    if usage is not None
                    else token_provenance
                    if audit.input_tokens is not None and audit.output_tokens is not None
                    else None
                ),
                status=audit.status,
            )
        )
    return tuple(sorted(traces, key=lambda item: item.model_call_id))


@dataclass(frozen=True, slots=True)
class TraceSegmentSample:
    trace_id: str
    startup: StartupClass
    segment: str
    duration_ms: float
    environment: TraceEnvironment

    def __post_init__(self) -> None:
        if self.startup not in {"hot", "cold"}:
            raise EconomyTraceError("latency startup class is invalid")
        if self.environment not in {"offline_in_process", "real_transport"}:
            raise EconomyTraceError("latency environment is invalid")
        if not self.trace_id or not self.segment:
            raise EconomyTraceError("latency trace identity is required")
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise EconomyTraceError("latency duration must be finite and non-negative")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> "TraceSegmentSample":
        return cls(
            trace_id=str(value["trace_id"]),
            startup=value["startup"],  # type: ignore[arg-type]
            segment=str(value["segment"]),
            duration_ms=float(value["duration_ms"]),
            environment=value["environment"],  # type: ignore[arg-type]
        )

    def as_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PercentileMetrics:
    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float

    def as_json(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LatencyReport:
    metrics: Mapping[TraceEnvironment, Mapping[str, Mapping[StartupClass, PercentileMetrics]]]
    hot_cold_ingress_warm_speedup: Mapping[TraceEnvironment, float | None]
    evidence_status: Mapping[TraceEnvironment, LatencyEvidenceStatus]

    def as_json(self) -> dict[str, object]:
        return {
            "metrics": {
                environment: {
                    segment: {startup: values.as_json() for startup, values in starts.items()}
                    for segment, starts in environment_metrics.items()
                }
                for environment, environment_metrics in self.metrics.items()
            },
            "hot_cold_ingress_warm_speedup": dict(self.hot_cold_ingress_warm_speedup),
            "evidence_status": dict(self.evidence_status),
            "real_network_slo_status": self.evidence_status["real_transport"],
        }


def _nearest_rank(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise EconomyTraceError("cannot calculate a percentile without samples")
    return ordered[math.ceil(percentile * len(ordered)) - 1]


class LatencyMetricsExporter:
    """Export segment percentiles without reclassifying offline timings as SLOs."""

    def export(self, samples: Iterable[TraceSegmentSample]) -> LatencyReport:
        materialized = tuple(samples)
        groups: dict[tuple[TraceEnvironment, str, StartupClass], list[float]] = defaultdict(list)
        for sample in materialized:
            groups[(sample.environment, sample.segment, sample.startup)].append(sample.duration_ms)
        metrics: dict[TraceEnvironment, dict[str, dict[StartupClass, PercentileMetrics]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for (environment, segment, startup), values in sorted(groups.items()):
            metrics[environment][segment][startup] = PercentileMetrics(
                samples=len(values),
                p50_ms=_nearest_rank(values, 0.50),
                p95_ms=_nearest_rank(values, 0.95),
                p99_ms=_nearest_rank(values, 0.99),
            )
        speedups: dict[TraceEnvironment, float | None] = {}
        statuses: dict[TraceEnvironment, LatencyEvidenceStatus] = {}
        for environment in ("offline_in_process", "real_transport"):
            environment_metrics = metrics.get(environment, {})
            ingress = environment_metrics.get("ingress_to_visible", {})
            hot, cold = ingress.get("hot"), ingress.get("cold")
            speedups[environment] = (
                1 - hot.p50_ms / cold.p50_ms
                if hot is not None and cold is not None and cold.p50_ms > 0
                else None
            )
            if environment == "offline_in_process" and not environment_metrics:
                statuses[environment] = "not_measured"
            elif environment == "real_transport" and not environment_metrics:
                statuses[environment] = "not_measured"
            elif not _REQUIRED_TRACE_SEGMENTS.issubset(environment_metrics) or hot is None or cold is None:
                statuses[environment] = "incomplete"
            else:
                statuses[environment] = "measured"
        return LatencyReport(
            metrics={
                environment: {segment: dict(starts) for segment, starts in environment_metrics.items()}
                for environment, environment_metrics in metrics.items()
            },
            hot_cold_ingress_warm_speedup=speedups,
            evidence_status=statuses,
        )


@dataclass(frozen=True, slots=True)
class RealTransportLatencyPolicy:
    """The published real-transport SLO, applied only to complete real traces."""

    hot_ingress_p95_ms: float = 5_000
    hot_ingress_p99_ms: float = 8_000
    cold_ingress_p95_ms: float = 8_000
    cold_ingress_p99_ms: float = 12_000
    min_hot_speedup: float = 0.30


def _real_latency_violations(report: LatencyReport, policy: RealTransportLatencyPolicy) -> tuple[str, ...]:
    status = report.evidence_status["real_transport"]
    if status == "not_measured":
        return ()
    if status != "measured":
        return ("real_transport_trace_incomplete",)
    ingress = report.metrics["real_transport"]["ingress_to_visible"]
    hot, cold = ingress["hot"], ingress["cold"]
    failures: list[str] = []
    if hot.p95_ms > policy.hot_ingress_p95_ms:
        failures.append("hot_ingress_p95_exceeded")
    if hot.p99_ms > policy.hot_ingress_p99_ms:
        failures.append("hot_ingress_p99_exceeded")
    if cold.p95_ms > policy.cold_ingress_p95_ms:
        failures.append("cold_ingress_p95_exceeded")
    if cold.p99_ms > policy.cold_ingress_p99_ms:
        failures.append("cold_ingress_p99_exceeded")
    speedup = report.hot_cold_ingress_warm_speedup["real_transport"]
    if speedup is None or speedup < policy.min_hot_speedup:
        failures.append("hot_cold_speedup_below_minimum")
    return tuple(failures)


@dataclass(frozen=True, slots=True)
class MechanicalTraceGateResult:
    cost: CostGateResult
    latency: LatencyReport
    latency_violations: tuple[str, ...]

    @property
    def passed(self) -> bool:
        # No real trace is not a pass claim; it is reported as not measured.
        # A supplied but incomplete/over-budget real trace is a CI failure.
        return self.cost.passed and not self.latency_violations

    def as_json(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "cost": self.cost.as_json(),
            "latency": self.latency.as_json(),
            "latency_violations": self.latency_violations,
        }


class MechanicalTraceGate:
    def evaluate(
        self,
        *,
        profile: CostProfile,
        model_calls: Iterable[ModelCallTrace],
        latency_samples: Iterable[TraceSegmentSample],
        real_transport_policy: RealTransportLatencyPolicy = RealTransportLatencyPolicy(),
    ) -> MechanicalTraceGateResult:
        latency = LatencyMetricsExporter().export(latency_samples)
        return MechanicalTraceGateResult(
            cost=CostProfileGate().evaluate(profile=profile, traces=model_calls),
            latency=latency,
            latency_violations=_real_latency_violations(latency, real_transport_policy),
        )


def trace_input_from_json(value: Mapping[str, object]) -> tuple[tuple[ModelCallTrace, ...], tuple[TraceSegmentSample, ...]]:
    """Strict, file-safe input contract for the CI CLI."""

    calls = value.get("model_calls")
    samples = value.get("latency_samples")
    if not isinstance(calls, list) or not isinstance(samples, list):
        raise EconomyTraceError("trace JSON requires model_calls and latency_samples arrays")
    if not all(isinstance(item, Mapping) for item in calls + samples):
        raise EconomyTraceError("trace JSON records must be objects")
    return (
        tuple(ModelCallTrace.from_json(item) for item in calls),  # type: ignore[arg-type]
        tuple(TraceSegmentSample.from_json(item) for item in samples),  # type: ignore[arg-type]
    )


def trace_output_json(result: MechanicalTraceGateResult) -> str:
    return json.dumps(result.as_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CostGateResult", "CostProfile", "CostProfileGate", "LatencyMetricsExporter", "LatencyReport",
    "MechanicalTraceGate", "MechanicalTraceGateResult", "ModelCallTrace", "RouteCostLimit",
    "EconomyTraceError", "TEST_ECONOMY_V1", "TraceSegmentSample", "default_route_classifier",
    "model_traces_from_projection", "model_traces_from_replay", "require_paid_action_profile",
    "trace_input_from_json", "trace_output_json",
]
