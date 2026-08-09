from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import sys

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "run_isolated_daemon_soak.py"
_SPEC = importlib.util.spec_from_file_location("isolated_daemon_soak_script", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

SoakOptions = _MODULE.SoakOptions
validate_options = _MODULE.validate_options
build_soak_report = _MODULE.build_soak_report
restart_due = _MODULE.restart_due
SOAK_PROVIDER_ACCEPTANCE_TIMEOUT_SECONDS = (
    _MODULE.SOAK_PROVIDER_ACCEPTANCE_TIMEOUT_SECONDS
)


def _options(tmp_path: Path) -> object:
    return SoakOptions(
        output=tmp_path / "soak.json",
        duration_seconds=86_400,
        model_mode="loopback-stub",
        allow_real_provider=False,
        confirm_24h=False,
        turn_interval_seconds=3_600,
        restart_interval_seconds=21_600,
        snapshot_interval_seconds=600,
        max_turns=24,
    )


def test_real_provider_requires_explicit_opt_in(tmp_path: Path) -> None:
    options = replace(_options(tmp_path), model_mode="real-provider")

    with pytest.raises(ValueError, match="allow-real-provider"):
        validate_options(options)


def test_24h_requires_explicit_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="confirm-24h"):
        validate_options(_options(tmp_path))


def test_soak_refuses_overwriting_an_existing_report(tmp_path: Path) -> None:
    output = tmp_path / "soak.json"
    output.write_text("existing", encoding="utf-8")
    options = replace(_options(tmp_path), output=output, duration_seconds=60)

    with pytest.raises(ValueError, match="must not already exist"):
        validate_options(options)


def test_restart_due_is_monotonic_and_disabled_for_zero_interval() -> None:
    assert restart_due(started_at=10.0, now=15.0, interval_seconds=5.0)
    assert not restart_due(started_at=10.0, now=14.9, interval_seconds=5.0)
    assert not restart_due(started_at=10.0, now=100.0, interval_seconds=0.0)


def test_real_provider_terminal_wait_has_room_for_strict_stream_latency() -> None:
    assert SOAK_PROVIDER_ACCEPTANCE_TIMEOUT_SECONDS >= 60.0


def test_report_is_explicitly_non_qualification_evidence(tmp_path: Path) -> None:
    options = replace(_options(tmp_path), confirm_24h=True)
    report = build_soak_report(
        options=options,
        started_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        interrupted=False,
        health_samples=[{"status": "running"}],
        turns=[{"source_event_id": "soak-turn-1", "http_status": 200}],
        restarts=[
            {
                "restart_index": 1,
                "healthy": True,
                "duplicate_provider_request_delta": 2,
                "duplicate_authoritative_role_request_delta": 0,
            }
        ],
        duplicate_effect_deltas=[0],
        final_replay={"semantic_hash": "a" * 64},
        captured_effect_count=1,
        captured_provider_request_count=1,
        usage_budget={"monthly_cost_cny": 0.1},
        provenance={"git_revision": "test-revision"},
    )

    assert report["contract"] == "isolated-daemon-soak.1"
    assert report["qualification_status"] == "manual_only"
    assert report["safety"]["production_database_touched"] is False
    assert report["safety"]["real_qq_send_possible"] is False
    assert report["continuity"]["duplicate_effect_deltas"] == [0]
    assert report["continuity"]["duplicate_provider_request_deltas"] == [2]
    assert report["continuity"]["duplicate_authoritative_role_request_deltas"] == [0]
    assert report["provenance"] == {"git_revision": "test-revision"}
    json.dumps(report, ensure_ascii=False)
