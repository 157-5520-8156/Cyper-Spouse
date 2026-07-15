from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.world_v2.platform_host import (
    DashboardProjectionCapture,
    PlatformClockTick,
    PlatformInbound,
    PlatformReceipt,
    WorldV2PlatformHost,
)
from companion_daemon.world_v2.dashboard_projection_adapter import DashboardRoomProjectionDTO, DashboardSceneRoute
from companion_daemon.world_v2.schemas import ProjectionRequest


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class _FakeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    async def inbound(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("inbound", kwargs))
        return {"status": "observed_only", "source": kwargs["platform_message_id"]}

    async def tick(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("tick", kwargs))
        return {"status": "observed_only", "tick": kwargs["tick_id"]}

    async def receipt(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("receipt", kwargs))
        return {"status": "action_executed", "receipt": kwargs["source_event_id"]}

    async def drain_actions_once(self) -> str:
        self.calls.append(("actions", None))
        return "action-settled"

    async def drain_background_once(self) -> str:
        self.calls.append(("background", None))
        return "affect-idle"

    def close(self) -> None:
        self.closed = True


class _FakeInboundTransport:
    def __init__(self, messages: list[PlatformInbound]) -> None:
        self._messages = messages

    async def receive(self) -> PlatformInbound | None:
        if not self._messages:
            return None
        return self._messages.pop(0)


class _FakeReceiptTransport:
    def __init__(self, receipts: list[PlatformReceipt]) -> None:
        self._receipts = receipts

    async def receive_receipt(self) -> PlatformReceipt | None:
        if not self._receipts:
            return None
        return self._receipts.pop(0)


class _FakeDashboardCapture:
    def __init__(self) -> None:
        self.requests: list[ProjectionRequest] = []

    def capture(self, request: ProjectionRequest) -> DashboardRoomProjectionDTO:
        self.requests.append(request)
        return DashboardRoomProjectionDTO(
            schema_version="world-v2-dashboard-room.1",
            world_revision=4,
            ledger_sequence=9,
            projection_hash="c" * 64,
            route=DashboardSceneRoute(
                scene_id="zhizhi-home", action_id="study", availability="busy"
            ),
        )


def _message(message_id: str = "message:1") -> PlatformInbound:
    return PlatformInbound(
        platform="fake",
        platform_user_id="user:1",
        platform_message_id=message_id,
        text="我今天有点累。",
        observed_at=NOW,
        trace_id="trace:platform-host",
    )


@pytest.mark.asyncio
async def test_platform_host_drains_fake_transport_and_delegates_only_application_primitives() -> None:
    application = _FakeApplication()
    host = WorldV2PlatformHost(application=application)  # type: ignore[arg-type]
    transport = _FakeInboundTransport([_message()])

    assert await host.drain_inbound_once(transport) == {
        "status": "observed_only",
        "source": "message:1",
    }
    assert await host.drain_inbound_once(transport) is None
    assert await host.drain_actions_once() == "action-settled"
    assert await host.drain_background_once() == "affect-idle"

    operation, inbound = application.calls[0]
    assert operation == "inbound"
    assert inbound == {
        "platform": "fake",
        "platform_user_id": "user:1",
        "platform_message_id": "message:1",
        "text": "我今天有点累。",
        "observed_at": NOW,
        "trace_id": "trace:platform-host",
        "attachment_refs": (),
        "coalescing_metadata": {},
    }
    assert [operation for operation, _ in application.calls] == [
        "inbound",
        "actions",
        "background",
    ]


@pytest.mark.asyncio
async def test_platform_host_preserves_media_only_ingress_and_settles_duplicate_receipts() -> None:
    application = _FakeApplication()
    host = WorldV2PlatformHost(application=application)  # type: ignore[arg-type]
    media_only = PlatformInbound(
        platform="fake",
        platform_user_id="user:1",
        platform_message_id="message:media-only",
        text=None,
        attachment_refs=("attachment:image:1",),
        coalescing_metadata={"provider_event": "attachment"},
        observed_at=NOW,
        trace_id="trace:media-only",
    )
    receipt = PlatformReceipt(
        source="platform:fake",
        source_event_id="receipt:1",
        action_id="action:reply:1",
        idempotency_key="platform:reply:1",
        status="delivered",
        provider_ref="message:outbound:1",
        observed_at=NOW,
        trace_id="trace:receipt",
        causation_id="action:reply:1",
        correlation_id="conversation:1",
        raw_payload_hash="sha256:" + "a" * 64,
    )

    assert await host.inbound(media_only) == {
        "status": "observed_only",
        "source": "message:media-only",
    }
    transport = _FakeReceiptTransport([receipt, receipt])
    assert await host.drain_receipts_once(transport) == {
        "status": "action_executed",
        "receipt": "receipt:1",
    }
    assert await host.drain_receipts_once(transport) == {
        "status": "action_executed",
        "receipt": "receipt:1",
    }
    assert await host.drain_receipts_once(transport) is None

    inbound = application.calls[0][1]
    assert inbound == {
        "platform": "fake",
        "platform_user_id": "user:1",
        "platform_message_id": "message:media-only",
        "text": None,
        "observed_at": NOW,
        "trace_id": "trace:media-only",
        "attachment_refs": ("attachment:image:1",),
        "coalescing_metadata": {"provider_event": "attachment"},
    }
    receipt_calls = [payload for operation, payload in application.calls if operation == "receipt"]
    assert receipt_calls == [
        {
            "source": "platform:fake",
            "source_event_id": "receipt:1",
            "action_id": "action:reply:1",
            "idempotency_key": "platform:reply:1",
            "status": "delivered",
            "provider_ref": "message:outbound:1",
            "observed_at": NOW,
            "trace_id": "trace:receipt",
            "causation_id": "action:reply:1",
            "correlation_id": "conversation:1",
            "raw_payload_hash": "sha256:" + "a" * 64,
            "kind": "execution_receipt",
            "artifact_refs": (),
            "cost_actual": 0,
            "error_class": None,
            "retryability": None,
        },
    ] * 2


@pytest.mark.asyncio
async def test_platform_host_forwards_tick_and_close_without_exposing_world_internals() -> None:
    application = _FakeApplication()
    host = WorldV2PlatformHost(application=application)  # type: ignore[arg-type]
    tick = PlatformClockTick(
        tick_id="tick:1",
        logical_time_from=NOW,
        logical_time_to=NOW + timedelta(seconds=1),
        observed_at=NOW,
        trace_id="trace:tick",
        causation_id="scheduler:tick:1",
        correlation_id="clock:world:1",
        reason="test",
    )

    assert await host.tick(tick) == {"status": "observed_only", "tick": "tick:1"}
    assert application.calls == [
        (
            "tick",
            {
                "tick_id": "tick:1",
                "logical_time_from": NOW,
                "logical_time_to": NOW + timedelta(seconds=1),
                "observed_at": NOW,
                "trace_id": "trace:tick",
                "causation_id": "scheduler:tick:1",
                "correlation_id": "clock:world:1",
                "reason": "test",
                "policy_version": None,
                "policy_digest": None,
            },
        )
    ]
    host.close()
    assert application.closed is True


def test_platform_host_exposes_dashboard_capture_without_an_http_dependency() -> None:
    capture: DashboardProjectionCapture = _FakeDashboardCapture()
    host = WorldV2PlatformHost(
        application=_FakeApplication(),  # type: ignore[arg-type]
        dashboard_capture=capture,
    )
    request = ProjectionRequest(
        schema_version="world-v2.1",
        request_id="request:dashboard",
        world_id="world:platform-host",
        viewer_kind="room_renderer",
        viewer_id="room:dashboard",
        permissions=frozenset(),
        trace_id="trace:dashboard",
        redaction_policy="room-public-v1",
    )

    assert host.capture_dashboard_room(request).to_payload()["route"] == {
        "scene_id": "zhizhi-home",
        "action_id": "study",
        "availability": "busy",
    }
    assert capture.requests == [request]  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="not configured"):
        WorldV2PlatformHost(application=_FakeApplication()).capture_dashboard_room(request)  # type: ignore[arg-type]


def test_platform_host_is_clean_application_adapter_without_legacy_or_ledger_imports() -> None:
    path = Path(__file__).parents[2] / "src/companion_daemon/world_v2/platform_host.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert imports == {
        "__future__",
        "dataclasses",
        "dashboard_projection_adapter",
        "datetime",
        "schemas",
        "typing",
        "production_turn_application",
    }
    source = path.read_text(encoding="utf-8")
    forbidden = (
        "companion_daemon.engine",
        "companion_daemon.world",
        "companion_daemon.runtime",
        "companion_daemon.qq_",
        "WorldRuntime",
        "WorldLedger",
        "SQLiteWorldLedger",
        "_ledger",
        "http",
    )
    assert not any(token in source for token in forbidden)


def test_platform_host_rejects_invalid_envelopes_before_the_application_is_called() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PlatformInbound(
            platform="fake",
            platform_user_id="user:1",
            platform_message_id="message:1",
            text="hello",
            observed_at=datetime(2026, 7, 16, 12, 0),
            trace_id="trace:1",
        )

    with pytest.raises(ValueError, match="after logical_time_from"):
        PlatformClockTick(
            tick_id="tick:1",
            logical_time_from=NOW,
            logical_time_to=NOW,
            observed_at=NOW,
            trace_id="trace:1",
            causation_id="cause:1",
            correlation_id="correlation:1",
            reason="test",
        )
