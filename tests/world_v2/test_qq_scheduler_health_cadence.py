from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.qq_c2c_onebot_app import QQC2CSchedulerDiagnostics


DUE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _due_world(*, warning_reasons: list[str] | None = None) -> dict[str, object]:
    reasons = list(warning_reasons or [])
    return {
        "initiative_state": "consideration_due",
        "initiative_next_consideration_at": DUE.isoformat(),
        "initiative_warning": bool(reasons),
        "initiative_warning_reasons": reasons,
    }


def test_consideration_warning_uses_two_actual_scheduler_cycles() -> None:
    diagnostics = QQC2CSchedulerDiagnostics(interval_seconds=30)

    boundary = diagnostics.snapshot(
        now=DUE + timedelta(seconds=60),
        world=_due_world(),
    )["initiative"]
    overdue = diagnostics.snapshot(
        now=DUE + timedelta(seconds=60, microseconds=1),
        world=_due_world(),
    )["initiative"]

    assert boundary["warning"] is False
    assert boundary["warning_reasons"] == []
    assert overdue["warning"] is True
    assert overdue["warning_reasons"] == ["consideration_overdue"]


def test_consideration_warning_recomputes_a_stale_platform_threshold() -> None:
    diagnostics = QQC2CSchedulerDiagnostics(interval_seconds=90)
    stale_world = _due_world(
        warning_reasons=["technical_failure_not_scheduled", "consideration_overdue"]
    )

    before_two_cycles = diagnostics.snapshot(
        now=DUE + timedelta(seconds=121),
        world=stale_world,
    )["initiative"]
    after_two_cycles = diagnostics.snapshot(
        now=DUE + timedelta(seconds=181),
        world=stale_world,
    )["initiative"]

    assert before_two_cycles["warning"] is True
    assert before_two_cycles["warning_reasons"] == [
        "technical_failure_not_scheduled"
    ]
    assert after_two_cycles["warning"] is True
    assert after_two_cycles["warning_reasons"] == [
        "technical_failure_not_scheduled",
        "consideration_overdue",
    ]


def test_scheduler_health_exposes_the_single_character_interior_topology() -> None:
    character_interior = {
        "contract": "character-interior-runtime-health.2",
        "status": "ready",
        "parallel_character_author_conflicts": 0,
        "legacy_interface_invocations": 0,
    }

    snapshot = QQC2CSchedulerDiagnostics(interval_seconds=30).snapshot(
        now=DUE,
        world={"character_interior": character_interior},
    )

    assert snapshot["character_interior"] == character_interior
