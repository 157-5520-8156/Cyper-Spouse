"""Read-only health summary for provider timing and whole-turn local overhead.

Latency traces are process evidence, never World facts.  This module only
summarizes the recorder's completed real-transport samples for operators; its
warning result has no path back into deliberation, expression, or dispatch.
"""

from __future__ import annotations

import math
from typing import Iterable

from .production_latency_trace import ProductionLatencySample


API_EXTERNAL_OVERHEAD_WARNING_MS = 500.0
# Retained for health clients that still label the original first-entry field.
FIRST_ROLE_PROVIDER_WARNING_MS = API_EXTERNAL_OVERHEAD_WARNING_MS
_SEGMENT = "ingress_to_first_role_provider"
_EXTERNAL_OVERHEAD_SEGMENT = "api_external_overhead"
_ENVIRONMENT = "real_transport"


def _nearest_rank(values: tuple[float, ...], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _provider_timing_summary(
    samples: tuple[ProductionLatencySample, ...],
    *,
    entry_count: int,
) -> dict[str, object]:
    ttft = tuple(
        sample.duration_ms
        for sample in samples
        if sample.environment == _ENVIRONMENT and sample.segment == "model_ttft"
    )
    completions = tuple(
        sample.duration_ms
        for sample in samples
        if sample.environment == _ENVIRONMENT
        and sample.segment == "model_completion"
    )
    return {
        "entry": {
            "status": "observed" if entry_count else "not_measured",
            "segment": _SEGMENT,
            "sample_count": entry_count,
        },
        "ttft": {
            "status": "observed" if ttft else "unavailable",
            "segment": "model_ttft",
            "sample_count": len(ttft),
            "reason": None if ttft else "non_streaming_completion_api",
        },
        "completion": {
            "status": "observed" if completions else "not_measured",
            "segment": "model_completion",
            "sample_count": len(completions),
            "sample_ms_p50": _nearest_rank(completions, 0.50) if completions else None,
            "sample_ms_p95": _nearest_rank(completions, 0.95) if completions else None,
            "sample_ms_max": max(completions) if completions else None,
        },
    }


def _external_overhead_summary(
    values: tuple[float, ...],
    *,
    threshold_ms: float,
) -> dict[str, object]:
    if not values:
        return {
            "status": "not_measured",
            "segment": _EXTERNAL_OVERHEAD_SEGMENT,
            "threshold_ms": threshold_ms,
            "sample_count": 0,
            "sample_ms_p50": None,
            "sample_ms_p95": None,
            "sample_ms_max": None,
            "over_threshold_count": 0,
            "over_threshold_rate": None,
        }
    over_threshold = sum(value > threshold_ms for value in values)
    return {
        "status": "warning" if over_threshold else "ok",
        "segment": _EXTERNAL_OVERHEAD_SEGMENT,
        "threshold_ms": threshold_ms,
        "sample_count": len(values),
        "sample_ms_p50": _nearest_rank(values, 0.50),
        "sample_ms_p95": _nearest_rank(values, 0.95),
        "sample_ms_max": max(values),
        "over_threshold_count": over_threshold,
        "over_threshold_rate": round(over_threshold / len(values), 4),
    }


def production_latency_health_snapshot(
    samples: Iterable[ProductionLatencySample],
) -> dict[str, object]:
    """Summarize role-provider entry/completion without changing runtime behavior."""

    threshold_ms = API_EXTERNAL_OVERHEAD_WARNING_MS
    observed_samples = tuple(samples)
    entry_samples = tuple(
        sample
        for sample in observed_samples
        if sample.environment == _ENVIRONMENT and sample.segment == _SEGMENT
    )
    values = tuple(sample.duration_ms for sample in entry_samples)
    external_samples = tuple(
        sample
        for sample in observed_samples
        if sample.environment == _ENVIRONMENT
        and sample.segment == _EXTERNAL_OVERHEAD_SEGMENT
    )
    external_values = tuple(sample.duration_ms for sample in external_samples)
    external_overhead = _external_overhead_summary(
        external_values,
        threshold_ms=threshold_ms,
    )
    role_provider_timing = _provider_timing_summary(
        observed_samples,
        entry_count=len(values),
    )
    if not values:
        return {
            "status": "not_measured",
            "warning": False,
            "warning_reasons": [],
            "environment": _ENVIRONMENT,
            "segment": _SEGMENT,
            "threshold_ms": threshold_ms,
            "sample_count": 0,
            "sample_ms_p50": None,
            "sample_ms_p95": None,
            "sample_ms_max": None,
            "over_threshold_count": 0,
            "over_threshold_rate": None,
            "api_external_overhead": external_overhead,
            "role_provider_timing": role_provider_timing,
        }

    p50 = _nearest_rank(values, 0.50)
    p95 = _nearest_rank(values, 0.95)
    maximum = max(values)
    over_threshold = sum(value > threshold_ms for value in values)
    warning_reasons: list[str] = []
    if external_values:
        external_p50 = _nearest_rank(external_values, 0.50)
        external_p95 = _nearest_rank(external_values, 0.95)
        if any(value > threshold_ms for value in external_values):
            warning_reasons.append("api_external_overhead_single_over_threshold")
        if external_p50 > threshold_ms:
            warning_reasons.append("api_external_overhead_p50_over_threshold")
        if external_p95 > threshold_ms:
            warning_reasons.append("api_external_overhead_p95_over_threshold")
        # A completed fast trace must not hide another trace whose foreground
        # provider coverage is still open/unmatched.  Pair by trace identity
        # and retain the first-role entry as a conservative compatibility
        # signal only for those uncovered turns; never substitute it into the
        # measured api_external_overhead distribution.
        measured_trace_ids = {sample.trace_id for sample in external_samples}
        uncovered_values = tuple(
            sample.duration_ms
            for sample in entry_samples
            if sample.trace_id not in measured_trace_ids
        )
        if uncovered_values:
            uncovered_p50 = _nearest_rank(uncovered_values, 0.50)
            uncovered_p95 = _nearest_rank(uncovered_values, 0.95)
            if any(value > threshold_ms for value in uncovered_values):
                warning_reasons.append(
                    "unmeasured_first_role_provider_single_over_threshold"
                )
            if uncovered_p50 > threshold_ms:
                warning_reasons.append(
                    "unmeasured_first_role_provider_p50_over_threshold"
                )
            if uncovered_p95 > threshold_ms:
                warning_reasons.append(
                    "unmeasured_first_role_provider_p95_over_threshold"
                )
    else:
        # Older process samples do not contain closed provider intervals. Keep
        # the historical first-entry warning until enough new traces replace
        # that bounded health window; never pretend the whole-turn objective
        # was measured from those legacy samples.
        if over_threshold:
            warning_reasons.append("first_role_provider_single_over_threshold")
        if p50 > threshold_ms:
            warning_reasons.append("first_role_provider_p50_over_threshold")
        if p95 > threshold_ms:
            warning_reasons.append("first_role_provider_p95_over_threshold")
    warning = bool(warning_reasons)
    return {
        "status": "warning" if warning else "ok",
        "warning": warning,
        "warning_reasons": warning_reasons,
        "environment": _ENVIRONMENT,
        "segment": _SEGMENT,
        "threshold_ms": threshold_ms,
        "sample_count": len(values),
        "sample_ms_p50": p50,
        "sample_ms_p95": p95,
        "sample_ms_max": maximum,
        "over_threshold_count": over_threshold,
        "over_threshold_rate": round(over_threshold / len(values), 4),
        "api_external_overhead": external_overhead,
        "role_provider_timing": role_provider_timing,
    }


__all__ = [
    "API_EXTERNAL_OVERHEAD_WARNING_MS",
    "FIRST_ROLE_PROVIDER_WARNING_MS",
    "production_latency_health_snapshot",
]
