from __future__ import annotations

import ast
from pathlib import Path

import pytest

from companion_daemon.world_v2.platform_architecture_guard import (
    PlatformArchitectureError,
    assert_v2_platform_architecture,
    scan_default_http_route_paths,
    scan_registered_v2_http_source,
    scan_v2_platform_boundary_source,
    scan_v2_platform_source,
)


REPOSITORY_ROOT = Path(__file__).parents[2]


def test_selected_v2_platform_paths_do_not_reach_legacy_runtime_authority() -> None:
    assert_v2_platform_architecture(REPOSITORY_ROOT)


def test_default_route_guard_rejects_archive_writers_but_not_world_v2() -> None:
    violations = scan_default_http_route_paths(
        (
            "/messages",
            "/world-v2/room",
            "/internal/world-v2/drain",
            "/qq/webhook",
            "/world/demo/commands",
            "/proactive/geoff",
        ),
        source_path=REPOSITORY_ROOT / "src/companion_daemon/app.py",
    )

    assert {item.detail for item in violations} == {
        "/qq/webhook",
        "/world/demo/commands",
        "/proactive/geoff",
    }


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


@pytest.mark.parametrize(
    "source",
    (
        "from companion_daemon.world_v2 import reducers\n",
        "from companion_daemon.world_v2.ledger import LedgerPort\n",
        "from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger\n",
        "from .affect_acceptance_runtime import AffectAcceptanceRuntime\n",
        "from ..world_v2.ledger import LedgerPort\n",
        "from .accepted_ledger_batch import AcceptedLedgerBatchIssuer\n",
    ),
)
def test_platform_boundary_rejects_direct_world_authority_imports(
    tmp_path: Path, source: str,
) -> None:
    path = tmp_path / "v2_adapter.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_v2_platform_boundary_source(path)

    assert any(item.rule == "world_v2_authority_import" for item in violations)


def test_internal_world_module_is_not_subject_to_platform_authority_import_rule(
    tmp_path: Path,
) -> None:
    path = tmp_path / "internal_runtime.py"
    path.write_text("from .ledger import LedgerPort\n", encoding="utf-8")

    assert scan_v2_platform_source(path) == ()


def test_platform_media_executor_may_import_only_the_narrow_delivery_approval_check(
    tmp_path: Path,
) -> None:
    path = tmp_path / "platform_action_executor.py"
    path.write_text(
        "from .media_delivery_runtime import require_current_media_delivery_approval\n",
        encoding="utf-8",
    )

    assert scan_v2_platform_boundary_source(path) == ()

    path.write_text(
        "from .media_delivery_runtime import "
        "require_current_media_delivery_approval, MediaDeliveryRuntime\n",
        encoding="utf-8",
    )
    assert any(
        item.rule == "world_v2_authority_import"
        for item in scan_v2_platform_boundary_source(path)
    )


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


@pytest.mark.parametrize(
    "route_body",
    (
        "return engine.proactive_tick()",
        "return local_projection_helper()",
    ),
)
def test_http_guard_discovers_new_v2_routes_and_transitive_local_helpers(
    tmp_path: Path, route_body: str,
) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        "class _App:\n"
        "    def get(self, _path):\n"
        "        return lambda function: function\n\n"
        "app = _App()\n\n"
        "def local_projection_helper():\n"
        "    return engine.snapshot()\n\n"
        "@app.get('/internal/world-v2/new-route')\n"
        "def new_world_v2_route():\n"
        f"    {route_body}\n",
        encoding="utf-8",
    )

    violations = scan_registered_v2_http_source(path)

    assert any(item.rule == "legacy_http_authority_reference" for item in violations)


def test_http_guard_covers_default_dashboard_route_and_its_helpers(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        "class _App:\n"
        "    def get(self, _path):\n"
        "        return lambda function: function\n\n"
        "app = _App()\n\n"
        "def render_dashboard():\n"
        "    return engine.debug_snapshot()\n\n"
        "@app.get('/dashboard')\n"
        "def dashboard():\n"
        "    return render_dashboard()\n",
        encoding="utf-8",
    )

    violations = scan_registered_v2_http_source(path)

    assert any(item.rule == "legacy_http_authority_reference" for item in violations)


@pytest.mark.parametrize("route_path", ("/world-v2", "/internal/world-v2"))
def test_http_guard_discovers_exact_v2_root_routes(
    tmp_path: Path, route_path: str,
) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        "class _App:\n"
        "    def get(self, _path):\n"
        "        return lambda function: function\n\n"
        "app = _App()\n\n"
        f"@app.get({route_path!r})\n"
        "def world_v2_root():\n"
        "    from companion_daemon.world_v2.ledger import LedgerPort\n"
        "    return LedgerPort\n",
        encoding="utf-8",
    )

    violations = scan_registered_v2_http_source(path)

    assert any(item.rule == "world_v2_authority_import" for item in violations)


def test_http_guard_discovers_keyword_api_route_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        "class _App:\n"
        "    def api_route(self, **_kwargs):\n"
        "        return lambda function: function\n\n"
        "app = _App()\n\n"
        "@app.api_route(path='/internal/world-v2')\n"
        "def world_v2_root():\n"
        "    from companion_daemon.world_v2.runtime import WorldRuntime\n"
        "    return WorldRuntime\n",
        encoding="utf-8",
    )

    violations = scan_registered_v2_http_source(path)

    assert any(item.rule == "world_v2_authority_import" for item in violations)


def test_http_guard_checks_every_path_on_a_multi_decorated_handler(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        "class _App:\n"
        "    def get(self, _path):\n"
        "        return lambda function: function\n\n"
        "app = _App()\n\n"
        "@app.get('/archive-alias')\n"
        "@app.get('/world-v2')\n"
        "def shared_handler():\n"
        "    return authority_helper()\n\n"
        "def authority_helper():\n"
        "    from companion_daemon.world_v2.reducers import apply_event\n"
        "    return apply_event\n",
        encoding="utf-8",
    )

    violations = scan_registered_v2_http_source(path)

    assert any(item.rule == "world_v2_authority_import" for item in violations)


def test_http_entry_guard_includes_module_scope_world_authority_imports(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        "from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger\n\n"
        "class _App:\n"
        "    def get(self, _path):\n"
        "        return lambda function: function\n\n"
        "app = _App()\n\n"
        "@app.get('/world-v2')\n"
        "def world_v2_root():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )

    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_imports = tuple(
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    )
    violations = scan_v2_platform_boundary_source(path, module_imports)

    assert any(item.rule == "world_v2_authority_import" for item in violations)


def test_default_asgi_graph_registers_no_legacy_ingress() -> None:
    """The archived runtime is gone; no composition may re-register its routes."""

    from fastapi.testclient import TestClient

    import companion_daemon.app as app_module

    default_paths = {str(getattr(route, "path", "")) for route in app_module.app.routes}

    assert {
        "/proactive/{canonical_user_id}",
        "/world/{world_id}/commands",
        "/world-runtime/overview",
        "/qq/webhook",
        "/world-console",
    }.isdisjoint(default_paths)
    assert "/messages" in default_paths
    assert not hasattr(app_module, "engine")
    assert not hasattr(app_module, "archive_app")

    with TestClient(app_module.app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/qq/webhook", json={}).status_code == 404
        assert client.post("/proactive/geoff").status_code == 404
        assert client.post("/world/demo/commands", json={}).status_code == 404
        assert client.get("/world-runtime/overview").status_code == 404
