from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

import companion_daemon.app as app_module
from companion_daemon.config import Settings
from companion_daemon.world_v2.world_v2_dashboard_ui import DASHBOARD_SESSION_COOKIE


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
OPERATOR_TOKEN = "dashboard-operator-secret"
LEGACY_BROWSER_REFERENCES = (
    "/debug/users",
    "/world-runtime/",
    "/dashboard-static/",
    "CompanionEngine",
    "WorldKernel",
    "localStorage",
    "sessionStorage",
)


def _dashboard_app(tmp_path: Path, *, name: str = "dashboard.sqlite"):
    return app_module.create_http_asgi_app(
        settings=Settings(
            database_path=tmp_path / name,
            DELIVERY_RECONCILIATION_TOKEN=OPERATOR_TOKEN,
        )
    )


def _local_client(dashboard_app):
    return TestClient(dashboard_app, client=("127.0.0.1", 50000))


def test_local_dashboard_opens_without_password_and_never_leaks_operator_token(
    tmp_path: Path,
) -> None:
    dashboard_app = _dashboard_app(tmp_path)
    app_module._http_v2_capture(asgi_app=dashboard_app, bootstrap_at=NOW)

    with _local_client(dashboard_app) as client:
        page = client.get("/dashboard")
        invalid = client.post(
            "/world-v2/dashboard/session",
            data={"operator_token": "wrong"},
            follow_redirects=False,
        )
        accepted = client.post(
            "/world-v2/dashboard/session",
            data={"operator_token": OPERATOR_TOKEN},
            follow_redirects=False,
        )

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "operator-token" not in page.text
    assert "请输入本机配置的 operator token" not in page.text
    assert OPERATOR_TOKEN not in page.text
    assert invalid.status_code == 401
    assert "set-cookie" not in invalid.headers
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/dashboard"
    cookie = accepted.headers["set-cookie"]
    assert DASHBOARD_SESSION_COOKIE in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert OPERATOR_TOKEN not in cookie


def test_authenticated_cold_dashboard_is_unavailable_without_bootstrapping_host(
    tmp_path: Path,
) -> None:
    dashboard_app = _dashboard_app(tmp_path)

    with _local_client(dashboard_app) as client:
        accepted = client.post(
            "/world-v2/dashboard/session",
            data={"operator_token": OPERATOR_TOKEN},
            follow_redirects=False,
        )
        response = client.get("/dashboard")

        assert accepted.status_code == 303
        assert response.status_code == 503
        assert "unavailable" in response.text
        assert dashboard_app.state.http_v2_capture is None


def test_hot_dashboard_uses_only_v2_dtos_and_static_room_resources(tmp_path: Path) -> None:
    dashboard_app = _dashboard_app(tmp_path)
    app_module._http_v2_capture(asgi_app=dashboard_app, bootstrap_at=NOW)

    with _local_client(dashboard_app) as client:
        page = client.get("/dashboard")
        client.post(
            "/world-v2/dashboard/session",
            data={"operator_token": OPERATOR_TOKEN},
            follow_redirects=False,
        )
        script = client.get("/world-v2/dashboard/app.js")
        dashboard_dto = client.get("/world-v2/dashboard")
        room_dto = client.get("/world-v2/room")
        room_page = client.get("/pixel-home/index.html")
        scene_registry = client.get("/assets/dashboard/rooms/scene-registry.json")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "/world-v2/dashboard/app.js" in page.text
    # The room visual is the live pixel-home prototype, not a static render.
    assert "/pixel-home/index.html" in page.text
    assert "zhizhi-room-isometric" not in page.text
    assert script.status_code == 200
    # The browser shell renders her factual life exclusively from the QQ
    # world's redacted life-state relay; the sandbox HTTP world's DTOs stay
    # available for tooling but the page no longer depends on them.
    assert "/world-v2/life-state" in script.text
    assert dashboard_dto.status_code == 200
    assert dashboard_dto.json()["schema_version"] == "world-v2-dashboard.1"
    assert room_dto.status_code == 200
    assert room_dto.json()["schema_version"] == "world-v2-dashboard-room.1"
    assert room_page.status_code == 200
    assert "js/bridge.js" in room_page.text
    assert scene_registry.status_code == 200
    for forbidden in LEGACY_BROWSER_REFERENCES:
        assert forbidden not in page.text
        assert forbidden not in script.text
    assert OPERATOR_TOKEN not in page.text
    assert OPERATOR_TOKEN not in script.text


def test_legacy_dashboard_session_does_not_gate_local_page_and_header_auth_remains_available(
    tmp_path: Path,
) -> None:
    first_app = _dashboard_app(tmp_path, name="first.sqlite")
    second_app = _dashboard_app(tmp_path, name="second.sqlite")
    app_module._http_v2_capture(asgi_app=first_app, bootstrap_at=NOW)
    app_module._http_v2_capture(asgi_app=second_app, bootstrap_at=NOW)

    with _local_client(first_app) as first_client:
        first_client.post(
            "/world-v2/dashboard/session",
            data={"operator_token": OPERATOR_TOKEN},
            follow_redirects=False,
        )
        first_cookie = first_client.cookies.get(DASHBOARD_SESSION_COOKIE)

    with _local_client(second_app) as second_client:
        second_client.cookies.set(DASHBOARD_SESSION_COOKIE, first_cookie)
        local_page = second_client.get("/dashboard")
        header_access = second_client.get(
            "/world-v2/dashboard",
            headers={"X-World-V2-Internal-Token": OPERATOR_TOKEN},
        )

    assert local_page.status_code == 200
    assert "operator-token" not in local_page.text
    assert header_access.status_code == 200


def test_dashboard_without_configured_operator_token_is_unavailable(tmp_path: Path) -> None:
    dashboard_app = app_module.create_http_asgi_app(
        # This test asserts the unconfigured behavior; do not let the
        # developer's repository-level .env turn it into a configured app.
        settings=Settings(_env_file=None, database_path=tmp_path / "disabled.sqlite")
    )

    with _local_client(dashboard_app) as client:
        page = client.get("/dashboard")
        login = client.post(
            "/world-v2/dashboard/session",
            data={"operator_token": "anything"},
            follow_redirects=False,
        )
        static_resource = client.get("/assets/dashboard/rooms/scene-registry.json")

    assert page.status_code == 503
    assert login.status_code == 503
    assert static_resource.status_code == 200


def test_remote_dashboard_keeps_legacy_session_gate(tmp_path: Path) -> None:
    dashboard_app = _dashboard_app(tmp_path)
    app_module._http_v2_capture(asgi_app=dashboard_app, bootstrap_at=NOW)

    with TestClient(dashboard_app, client=("192.0.2.10", 50000)) as client:
        page = client.get("/dashboard")

    assert page.status_code == 200
    assert "operator-token" in page.text
    assert "请输入本机配置的 operator token" in page.text
