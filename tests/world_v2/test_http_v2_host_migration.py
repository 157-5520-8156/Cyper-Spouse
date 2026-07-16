from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
import inspect
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import companion_daemon.app as app_module
from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.action_pump import ActionPumpResult
from companion_daemon.world_v2.http_capture_host import (
    HttpCaptureTransport,
    HttpV2CaptureHost,
    build_http_v2_capture_host,
)
from companion_daemon.world_v2.platform_action_executor import (
    MediaProviderDispatchRequest,
    PlatformDispatchReceipt,
    PlatformDispatchRequest,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class _DurableMediaTransport:
    """Composition fake; no image call is possible without an authorized Action."""

    provider = "media:durable-test"

    async def send(self, request: MediaProviderDispatchRequest) -> PlatformDispatchReceipt:
        raise AssertionError(f"unexpected provider call for {request.action_id}")

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        return None

    async def lookup_execution_result(
        self, *, action_id: str, idempotency_key: str, request_fingerprint: str
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_http_capture_host_runs_one_v2_ingress_action_tick_and_duplicate_without_legacy_write(
    tmp_path: Path,
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )
    try:
        first = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-v2:1",
            text="我今天有点累。",
            observed_at=NOW,
            coalescing_metadata={"channel_id": "http-local"},
        )
        duplicate = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-v2:1",
            text="我今天有点累。",
            observed_at=NOW,
            coalescing_metadata={"channel_id": "http-local"},
        )
        tick_status = await host.tick(
            tick_id="tick:http-v2:1",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:http-v2:tick:1",
            causation_id="scheduler:http-v2:1",
            correlation_id="clock:http-v2:1",
            reason="test_scheduler",
        )
        drained = await host.drain(max_action_units=2, max_background_units=2)
    finally:
        await host.aclose()

    assert first.status == "action_authorized"
    assert first.action_id is not None
    assert first.text
    assert duplicate.action_id == first.action_id
    assert duplicate.text == first.text
    assert tick_status == "observed_only"
    assert isinstance(drained.action_statuses, tuple)
    assert isinstance(drained.background_statuses, tuple)


def test_http_capture_host_composes_only_an_explicit_durable_media_transport(
    tmp_path: Path,
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-v2-media.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        media_transport=_DurableMediaTransport(),
    )
    try:
        # The worker exists only when composition receives a recovery-capable
        # provider; it is not constructed from the HTTP capture transport or
        # the legacy image-machine bridge.
        assert host._host._application._media_execution_worker is not None
    finally:
        host._host.close()


def test_http_messages_route_uses_the_injected_v2_capture_host_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-route-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="scheduler-secret"),
    )
    try:
        client = TestClient(app_module.app)
        response = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:route",
                "text": "你在吗？",
                "sent_at": NOW.isoformat(),
            },
        )
        tick = client.post(
            "/internal/world-v2/tick",
            headers={"X-World-V2-Internal-Token": "scheduler-secret"},
            json={
                "tick_id": "tick:http-v2:route",
                "logical_time_from": NOW.isoformat(),
                "logical_time_to": (NOW + timedelta(minutes=1)).isoformat(),
                "observed_at": (NOW + timedelta(minutes=1)).isoformat(),
                "trace_id": "trace:http-v2:route-tick",
                "causation_id": "scheduler:http-v2:route",
                "correlation_id": "clock:http-v2:route",
                "reason": "test_scheduler",
            },
        )
        drain = client.post(
            "/internal/world-v2/drain",
            headers={"X-World-V2-Internal-Token": "scheduler-secret"},
            json={"max_action_units": 2, "max_background_units": 2},
        )
        denied = client.post("/internal/world-v2/drain", json={})
    finally:
        asyncio.run(host.aclose())

    assert response.status_code == 200
    assert response.json()["world_action_id"].startswith("action:minimal-reply:")
    assert response.json()["text"]
    assert tick.json() == {"status": "observed_only", "tick_id": "tick:http-v2:route"}
    assert drain.status_code == 200
    assert set(drain.json()) == {"action_statuses", "background_statuses"}
    assert denied.status_code == 403


def test_http_attachment_evidence_changes_reused_message_identity_into_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-attachment-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    try:
        client = TestClient(app_module.app)
        first = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:attachment",
                "text": "看看这张图",
                "attachments": [{"kind": "image", "url": "https://example.test/a.png"}],
                "sent_at": NOW.isoformat(),
            },
        )
        changed = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:attachment",
                "text": "看看这张图",
                "attachments": [{"kind": "image", "url": "https://example.test/b.png"}],
                "sent_at": NOW.isoformat(),
            },
        )
    finally:
        asyncio.run(host.aclose())

    assert first.status_code == 200
    assert changed.status_code == 409
    assert "different content" in changed.json()["detail"]


def test_http_dashboard_room_route_is_operator_gated_and_returns_only_the_v2_public_dto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the HTTP route as a black box, not the projection adapter directly."""

    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-dashboard-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"v2 dashboard route touched legacy Engine attribute {name!r}")

    monkeypatch.setattr(app_module, "engine", _NoLegacyEngine())
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-operator-secret"),
    )
    try:
        client = TestClient(app_module.app)
        denied = client.get("/internal/world-v2/dashboard-room")
        response = client.get(
            "/internal/world-v2/dashboard-room",
            headers={"X-World-V2-Internal-Token": "dashboard-operator-secret"},
        )
    finally:
        asyncio.run(host.aclose())

    assert denied.status_code == 403
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"schema_version", "cursor", "projection_hash", "route"}
    assert payload["schema_version"] == "world-v2-dashboard-room.1"
    assert set(payload["cursor"]) == {"world_revision", "ledger_sequence"}
    assert set(payload["route"]) == {"scene_id", "action_id", "availability"}
    assert payload["route"] == {
        "scene_id": "unavailable",
        "action_id": "idle",
        "availability": "unavailable",
    }
    wire = str(payload)
    for forbidden in (
        "world_id",
        "semantic_hash",
        "affect",
        "participant",
        "media",
        "debug",
        "operator",
    ):
        assert forbidden not in wire


def test_http_public_room_route_is_read_only_v2_dto_without_engine_or_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Godot's public route has no archive fallback or write-on-read escape hatch."""

    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-public-room-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"public v2 room route touched legacy Engine attribute {name!r}")

    monkeypatch.setattr(app_module, "engine", _NoLegacyEngine())
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    try:
        response = TestClient(app_module.app).get("/world-v2/room")
    finally:
        asyncio.run(host.aclose())

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"schema_version", "cursor", "projection_hash", "route"}
    assert payload["schema_version"] == "world-v2-dashboard-room.1"
    assert set(payload["cursor"]) == {"world_revision", "ledger_sequence"}
    assert all(isinstance(value, int) and value >= 0 for value in payload["cursor"].values())
    assert payload["route"] == {
        "scene_id": "unavailable",
        "action_id": "idle",
        "availability": "unavailable",
    }
    assert len(payload["projection_hash"]) == 64


def test_http_dashboard_public_route_is_operator_gated_cacheable_and_never_reads_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-dashboard-public-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"public dashboard route touched legacy Engine {name!r}")

    monkeypatch.setattr(app_module, "engine", _NoLegacyEngine())
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-public-secret"),
    )
    try:
        client = TestClient(app_module.app)
        denied = client.get("/world-v2/dashboard")
        response = client.get(
            "/world-v2/dashboard",
            headers={"X-World-V2-Internal-Token": "dashboard-public-secret"},
        )
        not_modified = client.get(
            "/world-v2/dashboard",
            headers={
                "X-World-V2-Internal-Token": "dashboard-public-secret",
                "If-None-Match": response.headers["etag"],
            },
        )
    finally:
        asyncio.run(host.aclose())

    assert denied.status_code == 403
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"] == f'"{response.json()["projection_hash"]}"'
    assert not_modified.status_code == 304
    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "cursor",
        "projection_hash",
        "room",
        "now",
        "agenda",
        "notices",
        "freshness",
    }
    assert payload["schema_version"] == "world-v2-dashboard.1"
    assert set(payload["cursor"]) == {"world_revision", "ledger_sequence"}
    assert set(payload["room"]) == {"scene_id", "action_id", "availability"}
    assert set(payload["now"]) == {"activity_id", "activity_label", "availability"}
    assert payload["agenda"] == []
    assert payload["notices"] == []
    assert set(payload["freshness"]) == {"observed_at", "stale_after_seconds"}
    wire = str(payload)
    for forbidden in (
        "world_id",
        "semantic_hash",
        "affect",
        "participant",
        "media",
        "debug",
        "operator",
        "plan_id",
    ):
        assert forbidden not in wire


def test_http_dashboard_public_route_never_bootstraps_or_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"cold public dashboard route touched legacy Engine {name!r}")

    def _must_not_compose(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dashboard public GET must not construct a writable World v2 host")

    monkeypatch.setattr(app_module, "engine", _NoLegacyEngine())
    monkeypatch.setattr(app_module, "http_v2_capture", None)
    monkeypatch.setattr(app_module, "build_http_v2_capture_host", _must_not_compose)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-public-secret"),
    )

    response = TestClient(app_module.app).get(
        "/world-v2/dashboard",
        headers={"X-World-V2-Internal-Token": "dashboard-public-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "World v2 dashboard projection is unavailable until the platform host is initialized"
    )


def test_http_public_room_route_never_bootstraps_or_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"public room fallback touched legacy Engine {name!r}")

    def _must_not_compose(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("public room GET must not construct a writable World v2 host")

    monkeypatch.setattr(app_module, "engine", _NoLegacyEngine())
    monkeypatch.setattr(app_module, "http_v2_capture", None)
    monkeypatch.setattr(app_module, "build_http_v2_capture_host", _must_not_compose)

    response = TestClient(app_module.app).get("/world-v2/room")

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "World v2 room projection is unavailable until the platform host is initialized"
    )


def test_http_dashboard_room_route_never_falls_back_to_legacy_when_v2_capture_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CaptureWithoutDashboard:
        async def aclose(self) -> None:
            return None

        def dashboard_room(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("World v2 dashboard capture is not configured")

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"dashboard fallback touched legacy Engine {name!r}")

    monkeypatch.setattr(app_module, "engine", _NoLegacyEngine())
    monkeypatch.setattr(app_module, "http_v2_capture", _CaptureWithoutDashboard())
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-operator-secret"),
    )

    response = TestClient(app_module.app).get(
        "/internal/world-v2/dashboard-room",
        headers={"X-World-V2-Internal-Token": "dashboard-operator-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "World v2 dashboard capture is not configured"


def test_http_dashboard_room_route_does_not_bootstrap_a_cold_v2_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator read must not create WorldStarted or budget events on GET."""

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"cold dashboard route touched legacy Engine {name!r}")

    def _must_not_compose(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dashboard GET must not construct a writable World v2 host")

    monkeypatch.setattr(app_module, "engine", _NoLegacyEngine())
    monkeypatch.setattr(app_module, "http_v2_capture", None)
    monkeypatch.setattr(app_module, "build_http_v2_capture_host", _must_not_compose)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-operator-secret"),
    )

    response = TestClient(app_module.app).get(
        "/internal/world-v2/dashboard-room",
        headers={"X-World-V2-Internal-Token": "dashboard-operator-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "World v2 dashboard capture is unavailable until the platform host is initialized"
    )


@pytest.mark.asyncio
async def test_http_capture_only_drains_the_action_authorized_by_its_own_ingress() -> None:
    class _TargetedHost:
        def __init__(self) -> None:
            self.targeted_action_ids: list[str] = []

        async def inbound(self, _message):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=("action:new",),
                scheduled_action_ids=(),
            )

        async def drain_action(self, action_id: str) -> ActionPumpResult:
            self.targeted_action_ids.append(action_id)
            return ActionPumpResult(action_id=action_id, status="settled")

        async def drain_actions_once(self):  # type: ignore[no-untyped-def]
            raise AssertionError("HTTP ingress must not drain an unrelated world Action")

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            return None

        def close(self) -> None:
            return None

    targeted = _TargetedHost()
    host = HttpV2CaptureHost(  # type: ignore[arg-type]
        host=targeted,
        transport=HttpCaptureTransport(),
        primary_user_id="geoff",
    )
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:targeted",
            text="只应投递这一轮的 Action",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert targeted.targeted_action_ids == ["action:new"]
    assert result.action_id == "action:new"


@pytest.mark.asyncio
async def test_http_capture_transport_rejects_same_key_with_a_different_payload() -> None:
    transport = HttpCaptureTransport()
    first = PlatformDispatchRequest(
        action_id="action:http:1",
        kind="reply",
        target="user:geoff",
        payload_ref="payload:http:1",
        payload_hash="sha256:" + "a" * 64,
        content_type="text/plain",
        body="第一版",
        idempotency_key="http:dispatch:1",
    )
    changed = first.model_copy(update={"body": "篡改版"})

    await transport.send(first)
    with pytest.raises(ValueError, match="conflicts with the original payload"):
        await transport.send(changed)


def test_http_migration_blackbox_does_not_grant_the_new_path_legacy_authority() -> None:
    host_path = Path(__file__).parents[2] / "src/companion_daemon/world_v2/http_capture_host.py"
    tree = ast.parse(host_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(
        module.startswith("companion_daemon.engine")
        or module.startswith("companion_daemon.world")
        or module.startswith("companion_daemon.runtime")
        for module in imported_modules
    )

    route_source = inspect.getsource(app_module.post_message)
    forbidden = ("engine", "CompanionTurn", "QQTurnPresenter", "_handle_world_message")
    assert not any(token in route_source for token in forbidden)
