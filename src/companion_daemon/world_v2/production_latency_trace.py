"""Non-authoritative segmented latency evidence for production World-v2 turns.

The ledger remains the source of domain truth.  This module records monotonic
process timings only, so a missing segment stays missing instead of being
fabricated from a model completion or a delivery timestamp.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import math
from threading import Lock
import time
from typing import AsyncIterator, Callable, Iterator, Literal


StartupClass = Literal["hot", "cold"]
TraceEnvironment = Literal["offline_in_process", "real_transport"]
ProviderKind = Literal["role", "auxiliary"]
TraceSegment = Literal[
    "coalescing",
    "queue",
    "snapshot",
    "context",
    "ledger_commit",
    "advisor",
    "model_ttft",
    "model_completion",
    "foreground_provider_total",
    "role_provider_total",
    "api_external_overhead",
    "primary",
    "hedge_started",
    "candidate_validated",
    "winner",
    "provisional",
    "full",
    "first_semantic_action",
    "hedge_cancelled",
    "hedge_lost",
    "budget_exhausted",
    "technical_recovery_started",
    "validation_recovery_started",
    "validation_reselection_started",
    "acceptance",
    "dispatch",
    "receipt",
    "ingress_to_first_role_provider",
    "ingress_to_visible",
]

TRACE_SEGMENTS: frozenset[str] = frozenset(
    {
        "coalescing",
        "queue",
        "snapshot",
        "context",
        "ledger_commit",
        "advisor",
        "model_ttft",
        "model_completion",
        "foreground_provider_total",
        "role_provider_total",
        "api_external_overhead",
        "primary",
        "hedge_started",
        "candidate_validated",
        "winner",
        "provisional",
        "full",
        "first_semantic_action",
        "hedge_cancelled",
        "hedge_lost",
        "budget_exhausted",
        "technical_recovery_started",
        "validation_recovery_started",
        "validation_reselection_started",
        "acceptance",
        "dispatch",
        "receipt",
        "ingress_to_first_role_provider",
        "ingress_to_visible",
    }
)

# A trace remains joinable across the context, model, multi-beat dispatch and
# receipt phases.  Keeping the most recently touched 1,024 traces comfortably
# exceeds the process concurrency ceilings while making health work and memory
# independent of daemon uptime.
DEFAULT_MAX_RETAINED_TRACES = 1_024
DEFAULT_MAX_ACTIVE_TRACES = 128


@dataclass(frozen=True, slots=True)
class ProductionLatencySample:
    trace_id: str
    startup: StartupClass
    segment: TraceSegment
    duration_ms: float
    environment: TraceEnvironment

    def __post_init__(self) -> None:
        if not self.trace_id:
            raise ValueError("latency sample trace id is required")
        if self.segment not in TRACE_SEGMENTS:
            raise ValueError("latency sample segment is unsupported")
        if self.duration_ms < 0:
            raise ValueError("latency sample duration cannot be negative")


class TurnLatencyTrace:
    """One concurrency-safe process trace with additive repeated segments."""

    def __init__(
        self,
        *,
        trace_id: str,
        startup: StartupClass,
        environment: TraceEnvironment,
        ingress_started_ns: int,
        clock_ns: Callable[[], int],
    ) -> None:
        if not trace_id or startup not in {"hot", "cold"}:
            raise ValueError("turn latency trace identity is invalid")
        if environment not in {"offline_in_process", "real_transport"}:
            raise ValueError("turn latency trace environment is invalid")
        if ingress_started_ns < 0:
            raise ValueError("turn latency trace start cannot be negative")
        self._trace_id = trace_id
        self._startup = startup
        self._environment = environment
        self._ingress_started_ns = ingress_started_ns
        self._clock_ns = clock_ns
        self._durations_ns: dict[str, int] = {}
        self._first_role_provider_ns: int | None = None
        self._first_role_provider_call_id: str | None = None
        self._provider_entries_ns: dict[str, int] = {}
        self._provider_completions_ns: dict[str, int] = {}
        self._provider_first_tokens_ns: dict[str, int] = {}
        self._provider_kinds: dict[str, ProviderKind] = {}
        self._first_role_provider_completion_call_id: str | None = None
        self._cognition_finished = False
        self._provider_accepted_candidate_ns: int | None = None
        self._visible_ns: int | None = None
        self._lock = Lock()

    @property
    def trace_id(self) -> str:
        return self._trace_id

    def matches_registration(
        self,
        *,
        startup: StartupClass,
        environment: TraceEnvironment,
        ingress_started_ns: int,
    ) -> bool:
        return (
            self._startup == startup
            and self._environment == environment
            and self._ingress_started_ns == ingress_started_ns
        )

    def matches_environment(self, environment: TraceEnvironment) -> bool:
        return self._environment == environment

    def record_span(self, segment: TraceSegment, *, started_ns: int, ended_ns: int) -> None:
        if segment not in TRACE_SEGMENTS or segment in {
            "ingress_to_first_role_provider",
            "ingress_to_visible",
            "model_completion",
            "model_ttft",
            "foreground_provider_total",
            "role_provider_total",
            "api_external_overhead",
        }:
            raise ValueError("latency span segment is unsupported")
        if started_ns < self._ingress_started_ns or ended_ns < started_ns:
            raise ValueError("latency span is outside the turn timeline")
        with self._lock:
            self._durations_ns[segment] = self._durations_ns.get(segment, 0) + (
                ended_ns - started_ns
            )

    def record_duration(self, segment: TraceSegment, *, duration_ms: float) -> None:
        """Record a provider/adapter duration whose endpoints are already frozen.

        QQ coalescing timestamps survive a process restart as wall-clock evidence,
        while this process recorder uses a monotonic clock.  Accepting the exact
        duration keeps that evidence usable without pretending those clocks share
        an epoch.  It may not be used to synthesize visibility, first role-provider
        entry, or model TTFT.
        """

        if segment not in TRACE_SEGMENTS or segment in {
            "ingress_to_first_role_provider",
            "ingress_to_visible",
            "model_completion",
            "model_ttft",
            "foreground_provider_total",
            "role_provider_total",
            "api_external_overhead",
        }:
            raise ValueError("latency duration segment is unsupported")
        if not math.isfinite(duration_ms) or duration_ms < 0:
            raise ValueError("latency duration must be finite and non-negative")
        duration_ns = round(duration_ms * 1_000_000)
        with self._lock:
            self._durations_ns[segment] = self._durations_ns.get(segment, 0) + duration_ns

    @asynccontextmanager
    async def measure(self, segment: TraceSegment) -> AsyncIterator[None]:
        started = self._clock_ns()
        try:
            yield
        finally:
            self.record_span(segment, started_ns=started, ended_ns=self._clock_ns())

    @contextmanager
    def measure_sync(self, segment: TraceSegment) -> Iterator[None]:
        started = self._clock_ns()
        try:
            yield
        finally:
            self.record_span(segment, started_ns=started, ended_ns=self._clock_ns())

    def mark_visible(self, *, visible_ns: int | None = None) -> None:
        observed = self._clock_ns() if visible_ns is None else visible_ns
        if observed < self._ingress_started_ns:
            raise ValueError("visible timestamp precedes ingress")
        with self._lock:
            # One expression may contain multiple beats/Actions.  The SLO is
            # defined by the first provider-visible receipt, so later receipts
            # join the trace without rebinding or crashing the ActionPump.
            if self._visible_ns is None:
                self._visible_ns = observed

    def mark_provider_accepted_candidate(self, *, observed_ns: int | None = None) -> None:
        """Remember an ACK boundary without claiming that it proves visibility."""

        observed = self._clock_ns() if observed_ns is None else observed_ns
        if observed < self._ingress_started_ns:
            raise ValueError("provider acceptance timestamp precedes ingress")
        with self._lock:
            if self._provider_accepted_candidate_ns is None:
                self._provider_accepted_candidate_ns = observed

    def mark_verified_visible(self) -> None:
        """Confirm visibility at the earlier ACK boundary after strong lookup proof."""

        with self._lock:
            observed = self._provider_accepted_candidate_ns
        self.mark_visible(visible_ns=observed)

    def mark_first_role_provider(self, *, observed_ns: int | None = None) -> None:
        """Backward-compatible marker for an unattributed provider entry."""

        self.mark_role_provider_entry(
            "model-call:unattributed-first-role-provider",
            observed_ns=observed_ns,
        )

    def mark_role_provider_entry(
        self,
        provider_call_id: str,
        *,
        observed_ns: int | None = None,
    ) -> None:
        """Record an actual role-provider request boundary.

        The transport is currently a non-streaming completion API.  Entry is
        therefore observable, but it is not time-to-first-token evidence.
        """

        self._mark_provider_entry(
            provider_call_id,
            provider_kind="role",
            observed_ns=observed_ns,
        )

    def mark_auxiliary_provider_entry(
        self,
        provider_call_id: str,
        *,
        observed_ns: int | None = None,
    ) -> None:
        """Record a foreground embedding/advisory provider request boundary."""

        self._mark_provider_entry(
            provider_call_id,
            provider_kind="auxiliary",
            observed_ns=observed_ns,
        )

    def _mark_provider_entry(
        self,
        provider_call_id: str,
        *,
        provider_kind: ProviderKind,
        observed_ns: int | None,
    ) -> None:
        if not provider_call_id:
            raise ValueError("provider call id is required")

        observed = self._clock_ns() if observed_ns is None else observed_ns
        if observed < self._ingress_started_ns:
            raise ValueError("provider timestamp precedes ingress")
        with self._lock:
            if self._cognition_finished:
                return
            existing_kind = self._provider_kinds.get(provider_call_id)
            if existing_kind is not None and existing_kind != provider_kind:
                raise ValueError("provider call id cannot change kind")
            self._provider_entries_ns.setdefault(provider_call_id, observed)
            self._provider_kinds.setdefault(provider_call_id, provider_kind)
            if provider_kind == "role" and self._first_role_provider_ns is None:
                self._first_role_provider_ns = observed
                self._first_role_provider_call_id = provider_call_id

    def mark_role_provider_completion(
        self,
        provider_call_id: str,
        *,
        observed_ns: int | None = None,
    ) -> None:
        """Record the first complete non-streaming role-provider response."""

        self._mark_provider_completion(
            provider_call_id,
            provider_kind="role",
            observed_ns=observed_ns,
        )

    def mark_role_provider_first_token(
        self,
        provider_call_id: str,
        *,
        observed_ns: int | None = None,
    ) -> None:
        """Record actual first streamed content, never infer it from completion."""

        if not provider_call_id:
            raise ValueError("provider call id is required")
        observed = self._clock_ns() if observed_ns is None else observed_ns
        with self._lock:
            if self._cognition_finished:
                return
            started = self._provider_entries_ns.get(provider_call_id)
            if started is None:
                raise ValueError("provider first token has no matching entry")
            if self._provider_kinds.get(provider_call_id) != "role":
                raise ValueError("only a role provider can report role-model TTFT")
            if observed < started:
                raise ValueError("provider first token precedes entry")
            self._provider_first_tokens_ns.setdefault(provider_call_id, observed)
            if "model_ttft" not in self._durations_ns:
                self._durations_ns["model_ttft"] = observed - started

    def mark_auxiliary_provider_completion(
        self,
        provider_call_id: str,
        *,
        observed_ns: int | None = None,
    ) -> None:
        """Record completion of a foreground embedding/advisory request."""

        self._mark_provider_completion(
            provider_call_id,
            provider_kind="auxiliary",
            observed_ns=observed_ns,
        )

    def _mark_provider_completion(
        self,
        provider_call_id: str,
        *,
        provider_kind: ProviderKind,
        observed_ns: int | None,
    ) -> None:
        if not provider_call_id:
            raise ValueError("provider call id is required")
        observed = self._clock_ns() if observed_ns is None else observed_ns
        with self._lock:
            if self._cognition_finished:
                return
            started = self._provider_entries_ns.get(provider_call_id)
            if started is None:
                raise ValueError("provider completion has no matching entry")
            if self._provider_kinds.get(provider_call_id) != provider_kind:
                raise ValueError("provider completion kind does not match entry")
            if observed < started:
                raise ValueError("provider completion precedes entry")
            self._provider_completions_ns.setdefault(provider_call_id, observed)
            if (
                provider_kind == "role"
                and self._first_role_provider_completion_call_id is None
            ):
                self._first_role_provider_completion_call_id = provider_call_id
                self._durations_ns["model_completion"] = observed - started

    def finish_cognition_timing(self) -> None:
        """Freeze role-provider markers while leaving Action evidence joinable."""

        with self._lock:
            self._cognition_finished = True

    def role_provider_timing_evidence(self) -> dict[str, object]:
        """Return explicit entry/completion evidence and TTFT availability."""

        with self._lock:
            entry_ns = self._first_role_provider_ns
            entry_call_id = self._first_role_provider_call_id
            completion_call_id = self._first_role_provider_completion_call_id
            completion_ns = self._durations_ns.get("model_completion")
            entries = dict(self._provider_entries_ns)
            completions = dict(self._provider_completions_ns)
            first_tokens = dict(self._provider_first_tokens_ns)
            provider_kinds = dict(self._provider_kinds)
        return {
            "entry": {
                "status": "observed" if entry_ns is not None else "not_observed",
                "segment": "ingress_to_first_role_provider",
                "provider_call_id": entry_call_id,
                "duration_ms": (
                    None
                    if entry_ns is None
                    else (entry_ns - self._ingress_started_ns) / 1_000_000
                ),
            },
            "ttft": {
                "status": "observed" if first_tokens else "unavailable",
                "segment": "model_ttft",
                "provider_call_id": (
                    min(first_tokens, key=first_tokens.get) if first_tokens else None
                ),
                "duration_ms": (
                    None
                    if not first_tokens
                    else self._durations_ns.get("model_ttft", 0) / 1_000_000
                ),
                **(
                    {"reason": "non_streaming_completion_api"}
                    if not first_tokens
                    else {}
                ),
            },
            "completion": {
                "status": (
                    "observed" if completion_ns is not None else "not_observed"
                ),
                "segment": "model_completion",
                "provider_call_id": completion_call_id,
                "duration_ms": (
                    None if completion_ns is None else completion_ns / 1_000_000
                ),
            },
            "calls": [
                {
                    "provider_call_id": call_id,
                    "provider_kind": provider_kinds[call_id],
                    "status": (
                        "completed" if call_id in completions else "in_progress"
                    ),
                    "entry_ms": (started - self._ingress_started_ns) / 1_000_000,
                    "duration_ms": (
                        None
                        if call_id not in completions
                        else (completions[call_id] - started) / 1_000_000
                    ),
                }
                for call_id, started in sorted(
                    entries.items(), key=lambda item: (item[1], item[0])
                )
            ],
        }

    def samples(self) -> tuple[ProductionLatencySample, ...]:
        with self._lock:
            durations = dict(self._durations_ns)
            entries = dict(self._provider_entries_ns)
            completions = dict(self._provider_completions_ns)
            provider_kinds = dict(self._provider_kinds)
            if self._first_role_provider_ns is not None:
                durations["ingress_to_first_role_provider"] = (
                    self._first_role_provider_ns - self._ingress_started_ns
                )
            if self._visible_ns is not None:
                durations["ingress_to_visible"] = self._visible_ns - self._ingress_started_ns
            if entries and set(entries) <= set(completions):
                intervals = tuple(
                    (started, completions[call_id])
                    for call_id, started in entries.items()
                )
                durations["foreground_provider_total"] = _merged_interval_duration_ns(
                    intervals
                )
                role_intervals = tuple(
                    (started, completions[call_id])
                    for call_id, started in entries.items()
                    if provider_kinds[call_id] == "role"
                )
                if role_intervals:
                    durations["role_provider_total"] = _merged_interval_duration_ns(
                        role_intervals
                    )
                if self._visible_ns is not None:
                    visible_intervals = tuple(
                        (
                            max(self._ingress_started_ns, started),
                            min(self._visible_ns, ended),
                        )
                        for started, ended in intervals
                        if ended > self._ingress_started_ns
                        and started < self._visible_ns
                    )
                    provider_visible_ns = _merged_interval_duration_ns(
                        visible_intervals
                    )
                    durations["api_external_overhead"] = max(
                        0,
                        self._visible_ns
                        - self._ingress_started_ns
                        - provider_visible_ns,
                    )
        return tuple(
            ProductionLatencySample(
                trace_id=self._trace_id,
                startup=self._startup,
                segment=segment,  # type: ignore[arg-type]
                duration_ms=duration_ns / 1_000_000,
                environment=self._environment,
            )
            for segment, duration_ns in sorted(durations.items())
        )


def _merged_interval_duration_ns(intervals: tuple[tuple[int, int], ...]) -> int:
    """Return union duration so concurrent hedges are subtracted only once."""

    ordered = sorted(
        (started, ended)
        for started, ended in intervals
        if ended > started
    )
    if not ordered:
        return 0
    total = 0
    current_start, current_end = ordered[0]
    for started, ended in ordered[1:]:
        if started <= current_end:
            current_end = max(current_end, ended)
            continue
        total += current_end - current_start
        current_start, current_end = started, ended
    return total + current_end - current_start


class ProductionLatencyRecorder:
    """Bounded active and post-cognition trace windows.

    Active cognition traces are protected from completed-turn churn.  Once
    cognition ends, :meth:`finish_cognition` moves the trace into a bounded
    recently-touched window so immediate or recovering Action dispatch can
    still append receipt/visibility evidence.  Exceeding the separate active
    ceiling drops monitoring evidence instead of delaying a user turn.
    """

    def __init__(
        self,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        max_retained_traces: int = DEFAULT_MAX_RETAINED_TRACES,
        max_active_traces: int = DEFAULT_MAX_ACTIVE_TRACES,
    ) -> None:
        if type(max_retained_traces) is not int or max_retained_traces < 1:
            raise ValueError("maximum retained latency traces must be a positive integer")
        if type(max_active_traces) is not int or max_active_traces < 1:
            raise ValueError("maximum active latency traces must be a positive integer")
        self._clock_ns = clock_ns
        self._max_retained_traces = max_retained_traces
        self._max_active_traces = max_active_traces
        self._active_traces: dict[str, TurnLatencyTrace] = {}
        self._completed_traces: OrderedDict[str, TurnLatencyTrace] = OrderedDict()
        self._dropped_active_trace_count = 0
        self._has_started_trace = False
        self._lock = Lock()

    def _existing_locked(self, trace_id: str) -> TurnLatencyTrace | None:
        return self._active_traces.get(trace_id) or self._completed_traces.get(trace_id)

    def _activate_locked(self, trace: TurnLatencyTrace) -> bool:
        if trace.trace_id in self._active_traces:
            return True
        if len(self._active_traces) >= self._max_active_traces:
            self._dropped_active_trace_count += 1
            return False
        self._completed_traces.pop(trace.trace_id, None)
        self._active_traces[trace.trace_id] = trace
        return True

    def _retain_completed_locked(self, trace: TurnLatencyTrace) -> None:
        self._completed_traces[trace.trace_id] = trace
        self._completed_traces.move_to_end(trace.trace_id)
        while len(self._completed_traces) > self._max_retained_traces:
            self._completed_traces.popitem(last=False)

    def start(
        self,
        *,
        trace_id: str,
        startup: StartupClass,
        environment: TraceEnvironment,
        ingress_started_ns: int | None = None,
    ) -> TurnLatencyTrace:
        started = self._clock_ns() if ingress_started_ns is None else ingress_started_ns
        trace = TurnLatencyTrace(
            trace_id=trace_id,
            startup=startup,
            environment=environment,
            ingress_started_ns=started,
            clock_ns=self._clock_ns,
        )
        with self._lock:
            existing = self._existing_locked(trace_id)
            if existing is not None:
                if not existing.matches_registration(
                    startup=startup,
                    environment=environment,
                    ingress_started_ns=started,
                ):
                    raise ValueError("latency trace id was rebound to different ingress evidence")
                self._activate_locked(existing)
                return existing
            self._has_started_trace = True
            self._activate_locked(trace)
        return trace

    def start_ingress(
        self,
        *,
        trace_id: str,
        environment: TraceEnvironment,
        elapsed_before_registration_ms: float = 0.0,
    ) -> TurnLatencyTrace:
        """Atomically classify and register one ingress as cold or hot.

        The first unique ingress handled by a recorder instance is cold; every
        later unique ingress is hot.  Duplicate registration joins the original
        trace and cannot consume or change startup classification.  A host may
        supply already elapsed, persisted coalescing/queue time so the monotonic
        ingress origin still covers the full user-visible interval.
        """

        if not trace_id:
            raise ValueError("latency trace id is required")
        if environment not in {"offline_in_process", "real_transport"}:
            raise ValueError("latency trace environment is invalid")
        if not math.isfinite(elapsed_before_registration_ms) or elapsed_before_registration_ms < 0:
            raise ValueError("elapsed ingress duration must be finite and non-negative")
        with self._lock:
            existing = self._existing_locked(trace_id)
            if existing is not None:
                if not existing.matches_environment(environment):
                    raise ValueError("latency trace id was rebound to a different environment")
                self._activate_locked(existing)
                return existing
            now = self._clock_ns()
            elapsed_ns = round(elapsed_before_registration_ms * 1_000_000)
            if elapsed_ns > now:
                raise ValueError("elapsed ingress duration precedes the monotonic clock epoch")
            trace = TurnLatencyTrace(
                trace_id=trace_id,
                startup="cold" if not self._has_started_trace else "hot",
                environment=environment,
                ingress_started_ns=now - elapsed_ns,
                clock_ns=self._clock_ns,
            )
            self._has_started_trace = True
            self._activate_locked(trace)
            return trace

    def finish_cognition(self, trace_id: str) -> bool:
        """Move one trace into the bounded Action/visibility join window.

        This is not a claim that an external Action is terminal.  A later
        :meth:`get` still finds and renews the trace until newer completed
        turns displace it from the fixed-size window.
        """

        if not trace_id:
            raise ValueError("latency trace id is required")
        with self._lock:
            trace = self._active_traces.pop(trace_id, None)
            if trace is None:
                trace = self._completed_traces.get(trace_id)
            if trace is None:
                return False
            trace.finish_cognition_timing()
            self._retain_completed_locked(trace)
            return True

    def samples(self) -> tuple[ProductionLatencySample, ...]:
        with self._lock:
            traces = (
                *self._active_traces.values(),
                *self._completed_traces.values(),
            )
        return tuple(sample for trace in traces for sample in trace.samples())

    def get(self, trace_id: str) -> TurnLatencyTrace | None:
        """Return an existing process trace without implicitly creating evidence."""

        if not trace_id:
            raise ValueError("latency trace id is required")
        with self._lock:
            trace = self._active_traces.get(trace_id)
            if trace is not None:
                return trace
            trace = self._completed_traces.get(trace_id)
            if trace is not None:
                # Later Action/receipt phases renew the bounded post-cognition
                # join lease without changing any timing evidence.
                self._completed_traces.move_to_end(trace_id)
            return trace

    def get_active(self, trace_id: str) -> TurnLatencyTrace | None:
        """Return a trace only while foreground cognition still owns it."""

        if not trace_id:
            raise ValueError("latency trace id is required")
        with self._lock:
            return self._active_traces.get(trace_id)

    @property
    def retained_trace_count(self) -> int:
        with self._lock:
            return len(self._active_traces) + len(self._completed_traces)

    @property
    def active_trace_count(self) -> int:
        with self._lock:
            return len(self._active_traces)

    @property
    def completed_trace_count(self) -> int:
        with self._lock:
            return len(self._completed_traces)

    @property
    def dropped_active_trace_count(self) -> int:
        with self._lock:
            return self._dropped_active_trace_count

    @property
    def max_retained_traces(self) -> int:
        return self._max_retained_traces

    @property
    def max_active_traces(self) -> int:
        return self._max_active_traces


__all__ = [
    "DEFAULT_MAX_ACTIVE_TRACES",
    "DEFAULT_MAX_RETAINED_TRACES",
    "ProductionLatencyRecorder",
    "ProductionLatencySample",
    "TRACE_SEGMENTS",
    "TurnLatencyTrace",
]
