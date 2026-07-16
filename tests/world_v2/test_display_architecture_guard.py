from __future__ import annotations

from pathlib import Path

import pytest

from companion_daemon.world_v2.display_architecture_guard import (
    DisplayArchitectureError,
    assert_v2_display_architecture,
    scan_v2_display_architecture,
)


REPOSITORY_ROOT = Path(__file__).parents[2]


def test_selected_v2_display_consumers_remain_public_projection_readers() -> None:
    assert_v2_display_architecture(REPOSITORY_ROOT)


def test_display_guard_reports_a_missing_v2_read_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import companion_daemon.world_v2.display_architecture_guard as guard

    path = tmp_path / "consumer.gd"
    path.write_text("extends Node\n", encoding="utf-8")
    violation = guard.DisplayArchitectureViolation(path, "missing_v2_read_seam", "daemon_room_url")
    monkeypatch.setattr(guard, "scan_v2_display_architecture", lambda _root: (violation,))

    with pytest.raises(DisplayArchitectureError, match="missing_v2_read_seam"):
        guard.assert_v2_display_architecture(REPOSITORY_ROOT)


def test_display_guard_scans_selected_godot_consumers_not_user_dashboard_ui() -> None:
    paths = {
        violation.path.relative_to(REPOSITORY_ROOT)
        for violation in scan_v2_display_architecture(REPOSITORY_ROOT)
    }
    assert Path("src/companion_daemon/dashboard_ui.py") not in paths
