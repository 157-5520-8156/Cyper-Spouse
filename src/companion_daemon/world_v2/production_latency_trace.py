"""Non-authoritative segmented latency evidence for production World-v2 turns.

The ledger remains the source of domain truth.  This module records monotonic
process timings only, so a missing segment stays missing instead of being
fabricated from a model completion or a delivery timestamp.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import math
from threading import Lock
import time
from typing import AsyncIterator, Callable, Iterator, Literal


StartupClass = Literal["hot", "cold"]
TraceEnvironment = Literal["offline_in_process", "real_transport"]
TraceSegment = Literal[
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
        "acceptance",
        "dispatch",
        "receipt",
        "ingress_to_visible",
    }
)


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
        if segment not in TRACE_SEGMENTS or segment == "ingress_to_visible":
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
        an epoch.  It may not be used to synthesize visibility or model TTFT.
        """

        if segment not in TRACE_SEGMENTS or segment in {"ingress_to_visible", "model_ttft"}:
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

    def samples(self) -> tuple[ProductionLatencySample, ...]:
        with self._lock:
            durations = dict(self._durations_ns)
            if self._visible_ns is not None:
                durations["ingress_to_visible"] = self._visible_ns - self._ingress_started_ns
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


class ProductionLatencyRecorder:
    """Process-local trace registry; never a second world-state authority."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.perf_counter_ns) -> None:
        self._clock_ns = clock_ns
        self._traces: dict[str, TurnLatencyTrace] = {}
        self._lock = Lock()

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
            existing = self._traces.get(trace_id)
            if existing is not None:
                if not existing.matches_registration(
                    startup=startup,
                    environment=environment,
                    ingress_started_ns=started,
                ):
                    raise ValueError("latency trace id was rebound to different ingress evidence")
                return existing
            self._traces[trace_id] = trace
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
            existing = self._traces.get(trace_id)
            if existing is not None:
                if not existing.matches_environment(environment):
                    raise ValueError("latency trace id was rebound to a different environment")
                return existing
            now = self._clock_ns()
            elapsed_ns = round(elapsed_before_registration_ms * 1_000_000)
            if elapsed_ns > now:
                raise ValueError("elapsed ingress duration precedes the monotonic clock epoch")
            trace = TurnLatencyTrace(
                trace_id=trace_id,
                startup="cold" if not self._traces else "hot",
                environment=environment,
                ingress_started_ns=now - elapsed_ns,
                clock_ns=self._clock_ns,
            )
            self._traces[trace_id] = trace
            return trace

    def samples(self) -> tuple[ProductionLatencySample, ...]:
        with self._lock:
            traces = tuple(self._traces[key] for key in sorted(self._traces))
        return tuple(sample for trace in traces for sample in trace.samples())

    def get(self, trace_id: str) -> TurnLatencyTrace | None:
        """Return an existing process trace without implicitly creating evidence."""

        if not trace_id:
            raise ValueError("latency trace id is required")
        with self._lock:
            return self._traces.get(trace_id)


__all__ = [
    "ProductionLatencyRecorder",
    "ProductionLatencySample",
    "TRACE_SEGMENTS",
    "TurnLatencyTrace",
]
