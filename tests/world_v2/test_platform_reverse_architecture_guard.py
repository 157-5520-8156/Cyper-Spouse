from __future__ import annotations

from pathlib import Path

import pytest

from companion_daemon.world_v2.platform_architecture_guard import (
    PlatformArchitectureError,
    assert_v2_platform_architecture,
    scan_v2_platform_source,
)


REPOSITORY_ROOT = Path(__file__).parents[2]


def test_selected_v2_platform_paths_do_not_reach_legacy_runtime_authority() -> None:
    assert_v2_platform_architecture(REPOSITORY_ROOT)


@pytest.mark.parametrize(
    ("source", "expected_rule"),
    (
        ("from companion_daemon.engine import CompanionEngine\n", "legacy_import"),
        ("from companion_daemon import world\n", "legacy_import"),
        ("await self._handle_world_message(message)\n", "legacy_symbol_reference"),
    ),
)
def test_reverse_guard_rejects_import_aliases_and_direct_legacy_calls(
    tmp_path: Path, source: str, expected_rule: str
) -> None:
    path = tmp_path / "v2_adapter.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_v2_platform_source(path)

    assert any(violation.rule == expected_rule for violation in violations)


def test_reverse_guard_reports_unsafe_paths_in_a_single_operator_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import companion_daemon.world_v2.platform_architecture_guard as guard

    unsafe = tmp_path / "unsafe.py"
    unsafe.write_text(
        "from companion_daemon.companion_turn import CompanionTurn\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        guard, "scan_v2_platform_architecture", lambda _root: scan_v2_platform_source(unsafe)
    )

    with pytest.raises(PlatformArchitectureError, match="reverse-dependency guard failed"):
        guard.assert_v2_platform_architecture(REPOSITORY_ROOT)


def test_http_v2_module_defers_archive_engine_construction_until_an_archive_route_uses_it() -> None:
    import companion_daemon.app as app_module

    assert type(app_module.engine).__name__ == "_LazyArchiveEngine"
    assert app_module.engine._instance is None


def test_selected_http_v2_route_does_not_resolve_the_archive_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    import companion_daemon.app as app_module

    class _V2CaptureOnly:
        async def respond(self, **_kwargs: object):
            return type(
                "Result",
                (),
                {
                    "status": "action_authorized",
                    "action_id": "action:v2:guard",
                    "text": "这是 v2 回复。",
                    "canonical_user_id": "geoff",
                },
            )()

        async def aclose(self) -> None:
            return None

    assert type(app_module.engine).__name__ == "_LazyArchiveEngine"
    monkeypatch.setattr(app_module, "http_v2_capture", _V2CaptureOnly())
    with TestClient(app_module.app) as client:
        response = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:reverse-guard",
                "text": "在吗？",
            },
        )

    assert response.status_code == 200
    assert app_module.engine._instance is None
