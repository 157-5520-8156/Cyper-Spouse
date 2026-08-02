from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from companion_daemon.world_v2.interactive_turn_budget import InteractiveTurnBudgetPolicy
from companion_daemon.world_v2.production_latency_health import (
    production_latency_health_snapshot,
)
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
    trace.mark_role_provider_entry("model-call:observed")
    clock.advance_ms(80)
    trace.mark_role_provider_completion("model-call:observed")
    trace.mark_visible()

    samples = {sample.segment: sample.duration_ms for sample in recorder.samples()}
    assert samples == {
        "api_external_overhead": 24.0,
        "context": 7.0,
        "foreground_provider_total": 80.0,
        "ingress_to_first_role_provider": 24.0,
        "ingress_to_visible": 104.0,
        "ledger_commit": 5.0,
        "model_completion": 80.0,
        "role_provider_total": 80.0,
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


def test_first_role_provider_is_ingress_relative_and_first_observation_wins() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start_ingress(
        trace_id="trace:first-role-provider",
        environment="real_transport",
    )

    clock.advance_ms(317)
    trace.mark_first_role_provider()
    clock.advance_ms(41)
    trace.mark_first_role_provider()

    assert {sample.segment: sample.duration_ms for sample in trace.samples()} == {
        "ingress_to_first_role_provider": 317.0,
    }


def test_non_streaming_role_provider_separates_entry_completion_and_unavailable_ttft() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start_ingress(
        trace_id="trace:provider-timing",
        environment="real_transport",
    )

    clock.advance_ms(317)
    trace.mark_role_provider_entry("model-call:first")
    clock.advance_ms(4_200)
    trace.mark_role_provider_completion("model-call:first")

    assert {sample.segment: sample.duration_ms for sample in trace.samples()} == {
        "foreground_provider_total": 4_200.0,
        "ingress_to_first_role_provider": 317.0,
        "model_completion": 4_200.0,
        "role_provider_total": 4_200.0,
    }
    assert trace.role_provider_timing_evidence() == {
        "entry": {
            "status": "observed",
            "segment": "ingress_to_first_role_provider",
            "provider_call_id": "model-call:first",
            "duration_ms": 317.0,
        },
        "ttft": {
            "status": "unavailable",
            "segment": "model_ttft",
            "provider_call_id": None,
            "duration_ms": None,
            "reason": "non_streaming_completion_api",
        },
        "completion": {
            "status": "observed",
            "segment": "model_completion",
            "provider_call_id": "model-call:first",
            "duration_ms": 4_200.0,
        },
        "calls": [
            {
                "provider_call_id": "model-call:first",
                "provider_kind": "role",
                "status": "completed",
                "entry_ms": 317.0,
                "duration_ms": 4_200.0,
            }
        ],
    }


def test_streaming_role_provider_records_real_ttft_separately_from_completion() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start_ingress(
        trace_id="trace:stream-provider-timing",
        environment="real_transport",
    )
    clock.advance_ms(100)
    trace.mark_role_provider_entry("model-call:stream")
    clock.advance_ms(900)
    trace.mark_role_provider_first_token("model-call:stream")
    clock.advance_ms(2_100)
    trace.mark_role_provider_completion("model-call:stream")

    samples = {sample.segment: sample.duration_ms for sample in trace.samples()}
    assert samples["model_ttft"] == 900.0
    assert samples["model_completion"] == 3_000.0
    assert trace.role_provider_timing_evidence()["ttft"] == {
        "status": "observed",
        "segment": "model_ttft",
        "provider_call_id": "model-call:stream",
        "duration_ms": 900.0,
    }


def test_stream_pipeline_records_first_frame_and_validated_frame_from_ingress() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start_ingress(
        trace_id="trace:stream-pipeline",
        environment="real_transport",
    )
    clock.advance_ms(100)
    trace.mark_role_provider_entry("model-call:stream")
    clock.advance_ms(350)
    trace.mark_role_provider_first_token("model-call:stream")
    clock.advance_ms(650)
    trace.mark_interactive_milestone("first_expression_frame")
    clock.advance_ms(500)
    trace.mark_interactive_milestone("source_closure_completed")
    clock.advance_ms(100)
    trace.mark_interactive_milestone("candidate_validated")

    samples = {sample.segment: sample.duration_ms for sample in trace.samples()}
    assert samples["model_ttft"] == 350.0
    assert samples["ingress_to_first_expression_frame"] == 1_100.0
    assert samples["ingress_to_first_source_closure_completed"] == 1_600.0
    assert samples["ingress_to_first_candidate_validated"] == 1_700.0


def test_failover_intervals_are_independent_and_external_overhead_uses_their_union() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start_ingress(
        trace_id="trace:failover-overhead",
        environment="real_transport",
    )

    clock.advance_ms(100)
    trace.mark_role_provider_entry("model-call:primary")
    clock.advance_ms(2_000)
    trace.mark_role_provider_completion("model-call:primary")
    clock.advance_ms(200)
    trace.mark_role_provider_entry("model-call:fallback")
    clock.advance_ms(2_000)
    trace.mark_role_provider_completion("model-call:fallback")
    clock.advance_ms(400)
    trace.mark_visible()

    samples = {sample.segment: sample.duration_ms for sample in trace.samples()}
    assert samples["ingress_to_first_role_provider"] == 100.0
    assert samples["model_completion"] == 2_000.0
    assert samples["role_provider_total"] == 4_000.0
    assert samples["foreground_provider_total"] == 4_000.0
    assert samples["ingress_to_visible"] == 4_700.0
    assert samples["api_external_overhead"] == 700.0
    assert trace.role_provider_timing_evidence()["calls"] == [
        {
            "provider_call_id": "model-call:primary",
            "provider_kind": "role",
            "status": "completed",
            "entry_ms": 100.0,
            "duration_ms": 2_000.0,
        },
        {
            "provider_call_id": "model-call:fallback",
            "provider_kind": "role",
            "status": "completed",
            "entry_ms": 2_300.0,
            "duration_ms": 2_000.0,
        },
    ]


def test_external_overhead_stays_unmeasured_while_a_provider_span_is_open() -> None:
    clock = _Clock()
    trace = ProductionLatencyRecorder(clock_ns=clock).start_ingress(
        trace_id="trace:open-provider-span",
        environment="real_transport",
    )
    clock.advance_ms(100)
    trace.mark_role_provider_entry("model-call:open")
    clock.advance_ms(500)
    trace.mark_visible()

    assert "api_external_overhead" not in {
        sample.segment for sample in trace.samples()
    }


def test_completed_trace_remains_joinable_for_receipts_but_not_background_cognition() -> None:
    recorder = ProductionLatencyRecorder()
    trace = recorder.start_ingress(
        trace_id="trace:active-only-cognition",
        environment="real_transport",
    )

    assert recorder.get_active(trace.trace_id) is trace
    assert recorder.finish_cognition(trace.trace_id) is True

    assert recorder.get_active(trace.trace_id) is None
    assert recorder.get(trace.trace_id) is trace


def test_cognition_finish_ignores_late_provider_completion_but_keeps_action_visibility() -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(clock_ns=clock)
    trace = recorder.start_ingress(
        trace_id="trace:late-losing-provider",
        environment="real_transport",
    )
    clock.advance_ms(100)
    trace.mark_role_provider_entry("model-call:losing")

    assert recorder.finish_cognition(trace.trace_id) is True
    clock.advance_ms(900)
    trace.mark_role_provider_completion("model-call:losing")
    trace.record_span(
        "dispatch",
        started_ns=clock.value,
        ended_ns=clock.value + 4_000_000,
    )
    trace.mark_visible(visible_ns=clock.value + 5_000_000)

    assert {sample.segment: sample.duration_ms for sample in trace.samples()} == {
        "dispatch": 4.0,
        "ingress_to_first_role_provider": 100.0,
        "ingress_to_visible": 1_005.0,
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


def test_long_running_recorder_retains_one_bounded_deterministic_health_window() -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(clock_ns=clock, max_retained_traces=4)

    for ordinal in range(100):
        trace = recorder.start_ingress(
            trace_id=f"trace:retention:{ordinal:03d}",
            environment="real_transport",
        )
        clock.advance_ms(100 + ordinal)
        trace.mark_first_role_provider()
        recorder.finish_cognition(trace.trace_id)

    samples = recorder.samples()
    health = production_latency_health_snapshot(samples)

    assert recorder.retained_trace_count == 4
    assert recorder.max_retained_traces == 4
    assert {sample.trace_id for sample in samples} == {
        "trace:retention:096",
        "trace:retention:097",
        "trace:retention:098",
        "trace:retention:099",
    }
    assert recorder.get("trace:retention:000") is None
    assert recorder.get("trace:retention:099") is not None
    assert health["sample_count"] == 4
    assert health["sample_ms_p50"] == 197.0
    assert health["sample_ms_p95"] == 199.0
    assert production_latency_health_snapshot(recorder.samples()) == health


def test_active_trace_survives_more_completed_turns_than_the_join_window() -> None:
    clock = _Clock()
    recorder = ProductionLatencyRecorder(
        clock_ns=clock,
        max_retained_traces=3,
        max_active_traces=2,
    )
    slow = recorder.start_ingress(
        trace_id="trace:slow-active",
        environment="real_transport",
    )

    for ordinal in range(20):
        fast = recorder.start_ingress(
            trace_id=f"trace:fast:{ordinal:02d}",
            environment="real_transport",
        )
        clock.advance_ms(1)
        fast.mark_first_role_provider()
        recorder.finish_cognition(fast.trace_id)

    assert recorder.active_trace_count == 1
    assert recorder.completed_trace_count == 3
    assert recorder.get(slow.trace_id) is slow

    clock.advance_ms(250)
    slow.mark_first_role_provider()
    recorder.finish_cognition(slow.trace_id)

    assert recorder.active_trace_count == 0
    assert recorder.completed_trace_count == 3
    assert recorder.get(slow.trace_id) is slow
    assert any(sample.trace_id == slow.trace_id for sample in recorder.samples())


def test_active_trace_overflow_drops_monitoring_evidence_without_growing_registry() -> None:
    recorder = ProductionLatencyRecorder(
        max_retained_traces=2,
        max_active_traces=2,
    )
    recorder.start_ingress(trace_id="trace:active:1", environment="real_transport")
    recorder.start_ingress(trace_id="trace:active:2", environment="real_transport")
    overflow = recorder.start_ingress(
        trace_id="trace:active:overflow",
        environment="real_transport",
    )

    assert recorder.retained_trace_count == 2
    assert recorder.active_trace_count == 2
    assert recorder.dropped_active_trace_count == 1
    assert recorder.get(overflow.trace_id) is None


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
        trace.record_duration("model_completion", duration_ms=1)
    with pytest.raises(ValueError, match="unsupported"):
        trace.record_duration("ingress_to_visible", duration_ms=1)
    with pytest.raises(ValueError, match="unsupported"):
        trace.record_duration("ingress_to_first_role_provider", duration_ms=1)


def test_real_candidate_failure_can_mark_the_one_shot_recovery_window() -> None:
    trace = ProductionLatencyRecorder().start_ingress(
        trace_id="trace:technical-recovery",
        environment="real_transport",
    )
    budget = InteractiveTurnBudgetPolicy().start(
        marker=lambda event: trace.record_duration(event, duration_ms=0.0),  # type: ignore[arg-type]
    )

    assert budget.begin_technical_recovery() is not None
    assert {sample.segment for sample in trace.samples()} == {
        "technical_recovery_started",
    }


def test_source_review_failure_can_mark_its_independent_recovery_window() -> None:
    trace = ProductionLatencyRecorder().start_ingress(
        trace_id="trace:validation-recovery",
        environment="real_transport",
    )
    budget = InteractiveTurnBudgetPolicy().start(
        marker=lambda event: trace.record_duration(event, duration_ms=0.0),  # type: ignore[arg-type]
    )

    assert (
        budget.begin_validation_recovery(candidate_key="model-call:reviewed")
        is not None
    )
    assert {sample.segment for sample in trace.samples()} == {
        "validation_recovery_started",
    }
