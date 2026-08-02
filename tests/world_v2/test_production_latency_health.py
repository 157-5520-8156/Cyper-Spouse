from __future__ import annotations

from fastapi.testclient import TestClient

from companion_daemon.config import Settings
from companion_daemon.world_v2.production_latency_health import (
    FIRST_ROLE_PROVIDER_WARNING_MS,
    production_latency_health_snapshot,
)
from companion_daemon.world_v2.production_latency_trace import ProductionLatencySample
from companion_daemon.world_v2.qq_c2c_host import QQC2CDrainResult
from companion_daemon.world_v2.qq_c2c_onebot_app import create_qq_c2c_onebot_app


def _sample(
    trace_id: str,
    duration_ms: float,
    *,
    environment: str = "real_transport",
) -> ProductionLatencySample:
    return ProductionLatencySample(
        trace_id=trace_id,
        startup="hot",
        segment="ingress_to_first_role_provider",
        duration_ms=duration_ms,
        environment=environment,  # type: ignore[arg-type]
    )


def test_first_role_provider_health_warns_for_single_and_statistical_overruns() -> None:
    snapshot = production_latency_health_snapshot(
        (
            _sample("trace:1", 420.0),
            _sample("trace:2", 510.0),
            _sample("trace:3", 840.0),
            _sample("trace:offline", 9_000.0, environment="offline_in_process"),
            ProductionLatencySample(
                trace_id="trace:visible",
                startup="hot",
                segment="ingress_to_visible",
                duration_ms=12_000.0,
                environment="real_transport",
            ),
        )
    )

    stream_pipeline = snapshot.pop("stream_pipeline")
    assert stream_pipeline["qq_ack"]["sample_ms_p50"] == 12_000.0
    assert stream_pipeline["first_expression_frame"]["status"] == "not_measured"
    assert snapshot == {
        "status": "warning",
        "warning": True,
        "warning_reasons": [
            "ingress_to_visible_single_over_threshold",
            "ingress_to_visible_p50_over_threshold",
            "ingress_to_visible_p95_over_threshold",
            "first_role_provider_single_over_threshold",
            "first_role_provider_p50_over_threshold",
            "first_role_provider_p95_over_threshold",
        ],
        "environment": "real_transport",
        "segment": "ingress_to_first_role_provider",
        "threshold_ms": FIRST_ROLE_PROVIDER_WARNING_MS,
        "sample_count": 3,
        "sample_ms_p50": 510.0,
        "sample_ms_p95": 840.0,
        "sample_ms_max": 840.0,
        "over_threshold_count": 2,
        "over_threshold_rate": 0.6667,
        "api_external_overhead": {
            "status": "not_measured",
            "segment": "api_external_overhead",
            "threshold_ms": FIRST_ROLE_PROVIDER_WARNING_MS,
            "sample_count": 0,
            "sample_ms_p50": None,
            "sample_ms_p95": None,
            "sample_ms_max": None,
            "over_threshold_count": 0,
            "over_threshold_rate": None,
        },
        "user_visible_latency": {
            "status": "warning",
            "segment": "ingress_to_visible",
            "threshold_ms": 5_000.0,
            "sample_count": 1,
            "sample_ms_p50": 12_000.0,
            "sample_ms_p95": 12_000.0,
            "sample_ms_max": 12_000.0,
            "over_threshold_count": 1,
            "over_threshold_rate": 1.0,
        },
        "role_provider_timing": {
            "entry": {
                "status": "observed",
                "segment": "ingress_to_first_role_provider",
                "sample_count": 3,
            },
            "ttft": {
                "status": "unavailable",
                "segment": "model_ttft",
                "sample_count": 0,
                "reason": "non_streaming_completion_api",
            },
            "completion": {
                "status": "not_measured",
                "segment": "model_completion",
                "sample_count": 0,
                "sample_ms_p50": None,
                "sample_ms_p95": None,
                "sample_ms_max": None,
            },
        },
    }


def test_first_role_provider_health_is_read_only_and_not_measured_without_samples() -> None:
    empty = production_latency_health_snapshot(())
    at_boundary = production_latency_health_snapshot((_sample("trace:boundary", 500.0),))

    assert empty["status"] == "not_measured"
    assert empty["warning"] is False
    assert empty["sample_count"] == 0
    assert empty["sample_ms_p50"] is None
    assert empty["role_provider_timing"]["ttft"] == {
        "status": "not_measured",
        "segment": "model_ttft",
        "sample_count": 0,
        "reason": "no_role_provider_sample",
    }
    assert at_boundary["status"] == "ok"
    assert at_boundary["warning"] is False
    assert at_boundary["over_threshold_count"] == 0


def test_non_streaming_health_separates_entry_full_completion_and_unavailable_ttft() -> None:
    snapshot = production_latency_health_snapshot(
        (
            _sample("trace:provider-timing", 455.0),
            ProductionLatencySample(
                trace_id="trace:provider-timing",
                startup="hot",
                segment="model_completion",
                duration_ms=6_100.0,
                environment="real_transport",
            ),
        )
    )

    assert snapshot["role_provider_timing"] == {
        "entry": {
            "status": "observed",
            "segment": "ingress_to_first_role_provider",
            "sample_count": 1,
        },
        "ttft": {
            "status": "unavailable",
            "segment": "model_ttft",
            "sample_count": 0,
            "reason": "non_streaming_completion_api",
        },
        "completion": {
            "status": "observed",
            "segment": "model_completion",
            "sample_count": 1,
            "sample_ms_p50": 6_100.0,
            "sample_ms_p95": 6_100.0,
            "sample_ms_max": 6_100.0,
        },
    }


def test_health_warns_on_whole_turn_api_external_overhead_after_fast_first_entry() -> None:
    snapshot = production_latency_health_snapshot(
        (
            _sample("trace:post-provider-work", 100.0),
            ProductionLatencySample(
                trace_id="trace:post-provider-work",
                startup="hot",
                segment="api_external_overhead",
                duration_ms=700.0,
                environment="real_transport",
            ),
        )
    )

    assert snapshot["status"] == "warning"
    assert snapshot["warning"] is True
    assert snapshot["warning_reasons"] == [
        "api_external_overhead_single_over_threshold",
        "api_external_overhead_p50_over_threshold",
        "api_external_overhead_p95_over_threshold",
    ]
    assert snapshot["sample_ms_p95"] == 100.0
    assert snapshot["api_external_overhead"] == {
        "status": "warning",
        "segment": "api_external_overhead",
        "threshold_ms": 500.0,
        "sample_count": 1,
        "sample_ms_p50": 700.0,
        "sample_ms_p95": 700.0,
        "sample_ms_max": 700.0,
        "over_threshold_count": 1,
        "over_threshold_rate": 1.0,
    }


def test_health_reports_actual_ingress_to_visible_receipt_instead_of_stopping_at_model_entry() -> None:
    snapshot = production_latency_health_snapshot(
        (
            _sample("trace:user-visible", 120.0),
            ProductionLatencySample(
                trace_id="trace:user-visible",
                startup="hot",
                segment="model_completion",
                duration_ms=4_200.0,
                environment="real_transport",
            ),
            ProductionLatencySample(
                trace_id="trace:user-visible",
                startup="hot",
                segment="ingress_to_visible",
                duration_ms=8_750.0,
                environment="real_transport",
            ),
        )
    )

    assert snapshot["user_visible_latency"] == {
        "status": "warning",
        "segment": "ingress_to_visible",
        "threshold_ms": 5_000.0,
        "sample_count": 1,
        "sample_ms_p50": 8_750.0,
        "sample_ms_p95": 8_750.0,
        "sample_ms_max": 8_750.0,
        "over_threshold_count": 1,
        "over_threshold_rate": 1.0,
    }
    assert "ingress_to_visible_single_over_threshold" in snapshot["warning_reasons"]


def test_health_exposes_stream_ttft_frame_validation_and_qq_ack_as_distinct_stages() -> None:
    snapshot = production_latency_health_snapshot(
        (
            _sample("trace:stream-stages", 180.0),
            ProductionLatencySample(
                trace_id="trace:stream-stages",
                startup="hot",
                segment="model_ttft",
                duration_ms=420.0,
                environment="real_transport",
            ),
            ProductionLatencySample(
                trace_id="trace:stream-stages",
                startup="hot",
                segment="ingress_to_first_expression_frame",
                duration_ms=1_350.0,
                environment="real_transport",
            ),
            ProductionLatencySample(
                trace_id="trace:stream-stages",
                startup="hot",
                segment="ingress_to_first_source_closure_completed",
                duration_ms=1_780.0,
                environment="real_transport",
            ),
            ProductionLatencySample(
                trace_id="trace:stream-stages",
                startup="hot",
                segment="ingress_to_first_candidate_validated",
                duration_ms=1_900.0,
                environment="real_transport",
            ),
            ProductionLatencySample(
                trace_id="trace:stream-stages",
                startup="hot",
                segment="ingress_to_visible",
                duration_ms=2_150.0,
                environment="real_transport",
            ),
        )
    )

    assert snapshot["stream_pipeline"] == {
        "provider_ttft": {
            "status": "observed",
            "segment": "model_ttft",
            "sample_count": 1,
            "sample_ms_p50": 420.0,
            "sample_ms_p95": 420.0,
            "sample_ms_max": 420.0,
        },
        "first_expression_frame": {
            "status": "observed",
            "segment": "ingress_to_first_expression_frame",
            "sample_count": 1,
            "sample_ms_p50": 1_350.0,
            "sample_ms_p95": 1_350.0,
            "sample_ms_max": 1_350.0,
        },
        "first_candidate_validated": {
            "status": "observed",
            "segment": "ingress_to_first_candidate_validated",
            "sample_count": 1,
            "sample_ms_p50": 1_900.0,
            "sample_ms_p95": 1_900.0,
            "sample_ms_max": 1_900.0,
        },
        "source_closure_completed": {
            "status": "observed",
            "segment": "ingress_to_first_source_closure_completed",
            "sample_count": 1,
            "sample_ms_p50": 1_780.0,
            "sample_ms_p95": 1_780.0,
            "sample_ms_max": 1_780.0,
        },
        "qq_ack": {
            "status": "observed",
            "segment": "ingress_to_visible",
            "sample_count": 1,
            "sample_ms_p50": 2_150.0,
            "sample_ms_p95": 2_150.0,
            "sample_ms_max": 2_150.0,
        },
    }


def test_health_does_not_hide_unmeasured_slow_trace_behind_closed_fast_trace() -> None:
    snapshot = production_latency_health_snapshot(
        (
            _sample("trace:closed-fast", 100.0),
            ProductionLatencySample(
                trace_id="trace:closed-fast",
                startup="hot",
                segment="api_external_overhead",
                duration_ms=100.0,
                environment="real_transport",
            ),
            _sample("trace:open-slow", 900.0),
        )
    )

    assert snapshot["status"] == "warning"
    assert snapshot["warning"] is True
    assert snapshot["warning_reasons"] == [
        "unmeasured_first_role_provider_single_over_threshold",
        "unmeasured_first_role_provider_p50_over_threshold",
        "unmeasured_first_role_provider_p95_over_threshold",
    ]
    assert snapshot["api_external_overhead"]["status"] == "ok"  # type: ignore[index]


def test_production_health_exposes_latency_warning_without_changing_runtime_status(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "qq-latency-health.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
            LOCAL_APPRAISAL_ENABLED=False,
        ),
        use_fake_model=True,
        scheduler_interval_seconds=3_600,
    )

    async def _healthy_without_world_work(**_kwargs: object) -> QQC2CDrainResult:
        return QQC2CDrainResult(action_statuses=(), background_statuses=())

    monkeypatch.setattr(
        app.state.qq_c2c_host,
        "scheduler_once",
        _healthy_without_world_work,
    )
    monkeypatch.setattr(
        app.state.qq_c2c_host,
        "latency_samples",
        lambda: (_sample("trace:slow", 501.0),),
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["scheduler"]["status"] == "running"
    performance = body["scheduler"]["performance"]
    stream_pipeline = performance.pop("stream_pipeline")
    assert stream_pipeline["provider_ttft"]["status"] == "not_measured"
    assert stream_pipeline["qq_ack"]["status"] == "not_measured"
    assert performance == {
        "status": "warning",
        "warning": True,
        "warning_reasons": [
            "first_role_provider_single_over_threshold",
            "first_role_provider_p50_over_threshold",
            "first_role_provider_p95_over_threshold",
        ],
        "environment": "real_transport",
        "segment": "ingress_to_first_role_provider",
        "threshold_ms": 500.0,
        "sample_count": 1,
        "sample_ms_p50": 501.0,
        "sample_ms_p95": 501.0,
        "sample_ms_max": 501.0,
        "over_threshold_count": 1,
        "over_threshold_rate": 1.0,
        "api_external_overhead": {
            "status": "not_measured",
            "segment": "api_external_overhead",
            "threshold_ms": 500.0,
            "sample_count": 0,
            "sample_ms_p50": None,
            "sample_ms_p95": None,
            "sample_ms_max": None,
            "over_threshold_count": 0,
            "over_threshold_rate": None,
        },
        "user_visible_latency": {
            "status": "not_measured",
            "segment": "ingress_to_visible",
            "threshold_ms": 5_000.0,
            "sample_count": 0,
            "sample_ms_p50": None,
            "sample_ms_p95": None,
            "sample_ms_max": None,
            "over_threshold_count": 0,
            "over_threshold_rate": None,
        },
        "role_provider_timing": {
            "entry": {
                "status": "observed",
                "segment": "ingress_to_first_role_provider",
                "sample_count": 1,
            },
            "ttft": {
                "status": "unavailable",
                "segment": "model_ttft",
                "sample_count": 0,
                "reason": "non_streaming_completion_api",
            },
            "completion": {
                "status": "not_measured",
                "segment": "model_completion",
                "sample_count": 0,
                "sample_ms_p50": None,
                "sample_ms_p95": None,
                "sample_ms_max": None,
            },
        },
    }
