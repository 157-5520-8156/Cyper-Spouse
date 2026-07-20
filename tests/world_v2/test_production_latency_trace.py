from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from companion_daemon.world_v2.production_latency_trace import ProductionLatencyRecorder
from companion_daemon.world_v2.test_economy import LatencyMetricsExporter, TraceSegmentSample


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000_000_000

    def __call__(self) -> int:
        return self.value

    def advance_ms(self, value: int) -> None:
        self.value += value * 1_000_000


@pytest.mark.asyncio
async def test_trace_records_only_observed_segments_and_exact_visible_latency() -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(clock_ns=clock)
    trace = recorder.start(
        trace_id="trace:hot:1", startup="hot", environment="offline_in_process"
    )

    async with trace.measure("snapshot"):
        clock.advance_ms(12)
    async with trace.measure("context"):
        clock.advance_ms(7)
    with trace.measure_sync("ledger_commit"):
        clock.advance_ms(5)
    async with trace.measure("model_completion"):
        clock.advance_ms(80)
    trace.mark_visible()

    samples = {sample.segment: sample.duration_ms for sample in recorder.samples()}
    assert samples == {
        "context": 7.0,
        "ingress_to_visible": 104.0,
        "ledger_commit": 5.0,
        "model_completion": 80.0,
        "snapshot": 12.0,
    }
    assert "model_ttft" not in samples


def test_repeated_segments_accumulate_and_only_first_visible_receipt_wins() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start(
        trace_id="trace:cold:1", startup="cold", environment="real_transport"
    )
    trace.record_span(
        "queue", started_ns=clock.value, ended_ns=clock.value + 3_000_000
    )
    trace.record_span(
        "queue", started_ns=clock.value + 4_000_000, ended_ns=clock.value + 9_000_000
    )
    trace.mark_visible(visible_ns=clock.value + 10_000_000)
    trace.mark_visible(visible_ns=clock.value + 10_000_000)
    trace.mark_visible(visible_ns=clock.value + 11_000_000)

    assert {sample.segment: sample.duration_ms for sample in trace.samples()} == {
        "ingress_to_visible": 10.0,
        "queue": 8.0,
    }


def test_concurrent_multi_beat_visibility_records_one_first_observation_without_error() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start_ingress(
        trace_id="trace:beats", environment="offline_in_process"
    )
    timestamps = tuple(clock.value + value * 1_000_000 for value in range(1, 17))
    with ThreadPoolExecutor(max_workers=16) as pool:
        tuple(pool.map(lambda observed: trace.mark_visible(visible_ns=observed), timestamps))

    visible = next(sample for sample in trace.samples() if sample.segment == "ingress_to_visible")
    assert visible.duration_ms in {float(value) for value in range(1, 17)}
    assert len([sample for sample in trace.samples() if sample.segment == "ingress_to_visible"]) == 1


def test_trace_registration_is_idempotent_but_cannot_be_rebound() -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(clock_ns=clock)
    first = recorder.start(
        trace_id="trace:stable", startup="cold", environment="real_transport"
    )
    assert recorder.get("trace:stable") is first
    assert recorder.get("trace:missing") is None
    assert recorder.start(
        trace_id="trace:stable",
        startup="cold",
        environment="real_transport",
        ingress_started_ns=clock.value,
    ) is first
    with pytest.raises(ValueError, match="rebound"):
        recorder.start(
            trace_id="trace:stable",
            startup="hot",
            environment="real_transport",
            ingress_started_ns=clock.value,
        )


def test_offline_partial_trace_stays_incomplete_in_the_phase8_exporter() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start(
        trace_id="trace:partial", startup="hot", environment="offline_in_process"
    )
    trace.record_span("snapshot", started_ns=clock.value, ended_ns=clock.value + 1_000_000)
    samples = tuple(
        TraceSegmentSample(
            trace_id=item.trace_id,
            startup=item.startup,
            segment=item.segment,
            duration_ms=item.duration_ms,
            environment=item.environment,
        )
        for item in trace.samples()
    )

    report = LatencyMetricsExporter().export(samples)
    assert report.evidence_status["offline_in_process"] == "incomplete"
    assert report.evidence_status["real_transport"] == "not_measured"


def test_ingress_startup_classification_is_atomic_and_duplicates_join() -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(clock_ns=clock)

    with ThreadPoolExecutor(max_workers=8) as pool:
        traces = tuple(
            pool.map(
                lambda index: recorder.start_ingress(
                    trace_id=f"trace:concurrent:{index}",
                    environment="offline_in_process",
                ),
                range(20),
            )
        )

    samples_by_trace = {}
    for trace in traces:
        trace.record_duration("queue", duration_ms=1)
    for sample in recorder.samples():
        samples_by_trace.setdefault(sample.trace_id, sample.startup)
    assert tuple(samples_by_trace.values()).count("cold") == 1
    assert tuple(samples_by_trace.values()).count("hot") == 19
    assert recorder.start_ingress(
        trace_id="trace:concurrent:0",
        environment="offline_in_process",
        elapsed_before_registration_ms=999,
    ) is traces[0]
    with pytest.raises(ValueError, match="different environment"):
        recorder.start_ingress(
            trace_id="trace:concurrent:0",
            environment="real_transport",
        )


def test_persisted_coalescing_duration_extends_visible_origin_without_fake_ttft() -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(clock_ns=clock)
    trace = recorder.start_ingress(
        trace_id="trace:qq:coalesced",
        environment="real_transport",
        elapsed_before_registration_ms=425,
    )
    trace.record_duration("coalescing", duration_ms=400)
    trace.record_duration("queue", duration_ms=25)
    clock.advance_ms(75)
    trace.mark_visible()

    samples = {sample.segment: sample.duration_ms for sample in trace.samples()}
    assert samples == {
        "coalescing": 400.0,
        "ingress_to_visible": 500.0,
        "queue": 25.0,
    }
    assert "model_ttft" not in samples


def test_duration_api_rejects_synthesized_ttft_and_visibility() -> None:
    trace = ProductionLatencyRecorder().start_ingress(
        trace_id="trace:no-synthesis", environment="offline_in_process"
    )
    with pytest.raises(ValueError, match="unsupported"):
        trace.record_duration("model_ttft", duration_ms=1)
    with pytest.raises(ValueError, match="unsupported"):
        trace.record_duration("ingress_to_visible", duration_ms=1)
