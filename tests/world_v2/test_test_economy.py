from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.test_economy import (
    CostProfile,
    CostProfileGate,
    LatencyMetricsExporter,
    MechanicalTraceGate,
    ModelCallTrace,
    RouteCostLimit,
    TEST_ECONOMY_V1,
    EconomyTraceError,
    TraceSegmentSample,
    default_route_classifier,
    model_traces_from_replay,
    require_paid_action_profile,
    trace_input_from_json,
)
from companion_daemon.world_v2.test_economy_cli import main
from companion_daemon.world_v2.media_execution_runtime import MediaExecutionRuntime
from companion_daemon.world_v2.proposal_audit_schemas import (
    ModelResultAuditProjection,
    RecordedModelResultAudit,
    canonical_json,
    sha256,
)


def _call(
    *,
    turn_id: str = "event:observation:one",
    route_class: str = "chat",
    call_id: str = "call:one",
    attempt_index: int = 0,
    tier: str = "flash",
    input_tokens: int | None = 100,
    output_tokens: int | None = 20,
    thinking_tokens: int | None = 0,
    provenance: str | None = "offline_estimated",
    cost_category: str | None = None,
    cost_units: int | None = None,
) -> ModelCallTrace:
    return ModelCallTrace(
        turn_id=turn_id,
        route_class=route_class,  # type: ignore[arg-type]
        model_call_id=call_id,
        attempt_id="attempt:one",
        attempt_index=attempt_index,
        model_id="fake-flash",
        model_version="v1",
        model_tier=tier,  # type: ignore[arg-type]
        route_reason_code="fixed_test_route",
        router_version="test.1",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        token_provenance=provenance,  # type: ignore[arg-type]
        status="proposal_validated",
        cost_category=cost_category,
        cost_units=cost_units,
    )


def test_fixed_profile_accepts_one_metered_flash_chat_call() -> None:
    result = CostProfileGate().evaluate(profile=TEST_ECONOMY_V1, traces=(_call(),))

    assert result.passed
    assert result.violations == ()


def test_profile_rejects_extra_chat_call_and_missing_thinking_accounting() -> None:
    result = CostProfileGate().evaluate(
        profile=TEST_ECONOMY_V1,
        traces=(
            _call(call_id="call:one"),
            _call(call_id="call:two"),
            _call(
                turn_id="event:observation:two",
                route_class="deep_deliberation",
                call_id="call:thinking",
                tier="thinking",
                thinking_tokens=None,
            ),
        ),
    )

    assert {item.code for item in result.violations} == {
        "model_call_budget_exceeded",
        "thinking_token_accounting_missing",
    }


def test_expressive_second_call_requires_structural_recovery_lineage() -> None:
    result = CostProfileGate().evaluate(
        profile=TEST_ECONOMY_V1,
        traces=(
            _call(route_class="expressive", call_id="main", attempt_index=0),
            _call(route_class="expressive", call_id="second", attempt_index=1),
        ),
    )

    assert {item.code for item in result.violations} == {"expressive_recovery_not_structural"}


def test_profile_rejects_misrouted_tier_missing_tokens_and_wrong_provenance() -> None:
    result = CostProfileGate().evaluate(
        profile=TEST_ECONOMY_V1,
        traces=(
            _call(
                tier="thinking",
                input_tokens=None,
                output_tokens=None,
                thinking_tokens=None,
                provenance=None,
            ),
        ),
    )

    assert {item.code for item in result.violations} == {
        "model_tier_not_permitted",
        "token_accounting_missing",
        "token_provenance_not_permitted",
        "thinking_token_accounting_missing",
    }


def test_cost_profile_is_required_before_paid_action_creation() -> None:
    with pytest.raises(EconomyTraceError, match="paid_action_profile_missing"):
        require_paid_action_profile(profile=None, category="image", estimated_cost=1)
    with pytest.raises(EconomyTraceError, match="paid_action_not_permitted"):
        require_paid_action_profile(profile=TEST_ECONOMY_V1, category="image", estimated_cost=1)

    production = CostProfile(
        profile_id="production-test",
        currency="CNY_FEN",
        effective_at="2026-07-16T00:00:00Z",
        per_route=TEST_ECONOMY_V1.per_route,
        daily_by_category={"image": 100},
        per_action_caps={"image": 100},
        proactive_daily_cap=0,
        media_daily_cap=100,
        warning_thresholds=(80,),
        hard_stop_thresholds=(100,),
        allows_paid_actions=True,
        accepted_token_provenance=("provider_reported",),
    )
    require_paid_action_profile(profile=production, category="image", estimated_cost=100)
    with pytest.raises(EconomyTraceError, match="category_cap"):
        require_paid_action_profile(profile=production, category="image", estimated_cost=101)


def test_media_execution_seam_checks_profile_before_positive_media_reservation() -> None:
    runtime = MediaExecutionRuntime(ledger=None, sidecar=None)  # type: ignore[arg-type]
    with pytest.raises(EconomyTraceError, match="paid_action_profile_missing"):
        runtime._require_paid_media_profile(amount_limit=1)
    runtime._require_paid_media_profile(amount_limit=0)


def test_profile_accumulates_daily_cost_and_per_action_caps_when_production_requires_cost_trace() -> None:
    production = CostProfile(
        profile_id="production-cost-trace",
        currency="CNY_FEN",
        effective_at="2026-07-16T00:00:00Z",
        per_route=TEST_ECONOMY_V1.per_route,
        daily_by_category={"chat": 10},
        per_action_caps={"chat": 6},
        proactive_daily_cap=0,
        media_daily_cap=0,
        warning_thresholds=(8,),
        hard_stop_thresholds=(10,),
        allows_paid_actions=True,
        accepted_token_provenance=("provider_reported",),
        requires_cost_accounting=True,
    )
    result = CostProfileGate().evaluate(
        profile=production,
        traces=(
            _call(call_id="one", provenance="provider_reported", cost_category="chat", cost_units=6),
            _call(call_id="two", turn_id="event:observation:two", provenance="provider_reported", cost_category="chat", cost_units=6),
        ),
    )

    assert {item.code for item in result.violations} == {"daily_cost_cap_exceeded"}


def test_latency_exporter_reports_percentiles_and_warm_speed_without_fake_network_slo() -> None:
    report = LatencyMetricsExporter().export(
        (
            TraceSegmentSample("hot-1", "hot", "ingress_to_visible", 2_000, "offline_in_process"),
            TraceSegmentSample("hot-2", "hot", "ingress_to_visible", 3_000, "offline_in_process"),
            TraceSegmentSample("cold-1", "cold", "ingress_to_visible", 5_000, "offline_in_process"),
            TraceSegmentSample("cold-2", "cold", "ingress_to_visible", 6_000, "offline_in_process"),
            TraceSegmentSample("hot-ttft", "hot", "model_ttft", 1_100, "offline_in_process"),
        )
    )

    offline = report.metrics["offline_in_process"]["ingress_to_visible"]
    assert offline["hot"].p50_ms == 2_000
    assert offline["cold"].p95_ms == 6_000
    assert report.hot_cold_ingress_warm_speedup["offline_in_process"] == pytest.approx(0.6)
    assert report.evidence_status["real_transport"] == "not_measured"


def test_mechanical_gate_never_requires_real_network_evidence_to_pass_offline() -> None:
    result = MechanicalTraceGate().evaluate(
        profile=TEST_ECONOMY_V1,
        model_calls=(_call(),),
        latency_samples=(
            TraceSegmentSample("hot", "hot", "ingress_to_visible", 50, "offline_in_process"),
        ),
    )

    assert result.passed
    assert result.latency.evidence_status["real_transport"] == "not_measured"


def test_trace_json_and_cli_are_executable_ci_gate(tmp_path, capsys) -> None:
    trace = {
        "model_calls": [_call().as_json()],
        "latency_samples": [
            TraceSegmentSample("hot", "hot", "ingress_to_visible", 12, "offline_in_process").as_json()
        ],
    }
    calls, samples = trace_input_from_json(trace)
    assert calls == (_call(),)
    assert samples[0].startup == "hot"
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace), encoding="utf-8")

    assert main(["--trace-json", str(path)]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["passed"] is True
    assert rendered["latency"]["real_network_slo_status"] == "not_measured"


def test_real_transport_trace_is_neither_mixed_with_offline_nor_called_measured_when_incomplete() -> None:
    result = MechanicalTraceGate().evaluate(
        profile=TEST_ECONOMY_V1,
        model_calls=(_call(),),
        latency_samples=(
            TraceSegmentSample("offline", "hot", "ingress_to_visible", 10, "offline_in_process"),
            TraceSegmentSample("real", "hot", "ingress_to_visible", 100, "real_transport"),
        ),
    )

    assert result.latency.evidence_status["real_transport"] == "incomplete"
    assert result.latency.metrics["offline_in_process"]["ingress_to_visible"]["hot"].p50_ms == 10
    assert result.latency.metrics["real_transport"]["ingress_to_visible"]["hot"].p50_ms == 100
    assert result.latency_violations == ("real_transport_trace_incomplete",)
    assert result.passed is False


def test_cost_profile_requires_all_semantic_routes() -> None:
    with pytest.raises(EconomyTraceError, match="every World v2 route"):
        CostProfile(
            profile_id="bad",
            currency="TEST",
            effective_at="now",
            per_route={"chat": RouteCostLimit(1, 1, 1, 0, 1, ("flash",))},  # type: ignore[arg-type]
            daily_by_category={},
            per_action_caps={},
            proactive_daily_cap=0,
            media_daily_cap=0,
            warning_thresholds=(),
            hard_stop_thresholds=(),
            allows_paid_actions=False,
            accepted_token_provenance=("offline_estimated",),
        )


def test_cli_trace_contract_rejects_unknown_route_instead_of_bypassing_profile() -> None:
    raw = _call().as_json()
    raw["route_class"] = "free_unlimited"
    with pytest.raises(EconomyTraceError, match="route class"):
        trace_input_from_json({"model_calls": [raw], "latency_samples": []})


def test_replay_extraction_uses_persisted_audit_and_never_invents_thinking_usage() -> None:
    request_hash = "a" * 64
    response_hash = "b" * 64
    call_id = "call:thinking"
    audit = RecordedModelResultAudit(
        model_call_id=call_id,
        model_result_ref=f"model-result:{sha256(canonical_json({'model_call_id': call_id, 'response_hash': response_hash}))}",
        attempt_id="attempt:thinking",
        route={"tier": "thinking", "reason_code": "high_ambiguity", "router_version": "test.1"},
        model_id="reasoning-model",
        model_version="v1",
        request_hash=request_hash,
        response_hash=response_hash,
        status="proposal_validated",
        input_tokens=120,
        output_tokens=40,
    )
    audit_json = canonical_json(audit.model_dump(mode="json"))
    projection = ModelResultAuditProjection(
        model_result_ref=audit.model_result_ref,
        deliberation_result_id="deliberation:test",
        proposal_hash="sha256:" + "c" * 64,
        model_call_id=call_id,
        attempt_id=audit.attempt_id,
        capsule_id="d" * 64,
        trigger_ref="event:observation:thinking",
        evaluated_world_revision=1,
        attempt_index=0,
        attempt_count=1,
        audit_json=audit_json,
        audit_hash=sha256(audit_json),
        event_ref="event:model-result:thinking",
        event_payload_hash="e" * 64,
    )
    # The reader requires only the immutable audit projection; this keeps it
    # usable with replay snapshots without adding a second ledger query seam.
    evidence = SimpleNamespace(projection=SimpleNamespace(model_result_audits=(projection,)))

    traces = model_traces_from_replay(evidence=evidence, classifier=default_route_classifier)  # type: ignore[arg-type]

    assert traces[0].route_class == "deep_deliberation"
    assert traces[0].route_reason_code == "high_ambiguity"
    assert traces[0].thinking_tokens is None
    assert CostProfileGate().evaluate(profile=TEST_ECONOMY_V1, traces=traces).passed is False
