from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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

    async def drain_media_preview_once(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("media_preview", kwargs))
        return {"status": "planned"}

    async def drain_media_planning_once(self):  # type: ignore[no-untyped-def]
        self.calls.append(("media_planning", None))
        return SimpleNamespace(status="idle")

    async def drain_media_results_once(self, *, logical_time: datetime):  # type: ignore[no-untyped-def]
        self.calls.append(("media_results", logical_time))
        return None

    async def current_logical_time(self) -> datetime:
        self.calls.append(("logical_time", None))
        return NOW

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
    assert await host.drain_media_preview_once(
        trace_id="trace:media-preview", correlation_id="correlation:media-preview",
    ) == {"status": "planned"}

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
        "media_preview",
    ]
    assert application.calls[-1] == (
        "media_preview",
        {"trace_id": "trace:media-preview", "correlation_id": "correlation:media-preview"},
    )


@pytest.mark.asyncio
async def test_platform_scheduler_zero_budgets_do_not_enter_any_media_or_background_worker() -> None:
    application = _FakeApplication()
    host = WorldV2PlatformHost(application=application)  # type: ignore[arg-type]

    result = await host.drain_scheduled_work(
        max_action_units=0,
        max_background_units=0,
        media_preview_trace_id="trace:zero-budget",
        media_preview_correlation_id="correlation:zero-budget",
    )

    assert result.action_units_used == 0
    assert result.background_units_used == 0
    assert result.action_statuses == ()
    assert result.background_statuses == ()
    assert application.calls == []


@pytest.mark.asyncio
async def test_platform_scheduler_zero_action_budget_allows_only_non_media_background() -> None:
    class _BackgroundOnlyApplication(_FakeApplication):
        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.calls.append(("background", None))
            return SimpleNamespace(work_status="accepted")

        async def drain_media_preview_once(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            raise AssertionError("zero action budget must not enter media preview")

        async def drain_media_planning_once(self):  # type: ignore[no-untyped-def]
            raise AssertionError("zero action budget must not enter media planning")

        async def drain_media_results_once(self, *, logical_time: datetime):  # type: ignore[no-untyped-def]
            raise AssertionError(f"zero action budget materialized media at {logical_time}")

    application = _BackgroundOnlyApplication()
    host = WorldV2PlatformHost(application=application)  # type: ignore[arg-type]

    result = await host.drain_scheduled_work(
        max_action_units=0,
        max_background_units=1,
        media_preview_trace_id="trace:background-only",
        media_preview_correlation_id="correlation:background-only",
    )

    assert result.action_units_used == 0
    assert result.background_units_used == 1
    assert result.background_statuses == ("accepted",)
    assert result.background_statuses == ("accepted",)
    assert application.calls == [("background", None)]


@pytest.mark.asyncio
async def test_platform_scheduler_shares_each_budget_across_all_worker_classes() -> None:
    class _BudgetedApplication(_FakeApplication):
        def __init__(self) -> None:
            super().__init__()
            self._actions = [SimpleNamespace(status="settled"), SimpleNamespace(status="idle")]
            self._planning = [SimpleNamespace(status="planned")]

        async def drain_actions_once(self):  # type: ignore[no-untyped-def]
            self.calls.append(("actions", None))
            return self._actions.pop(0)

        async def drain_media_preview_once(self, **kwargs: object):  # type: ignore[no-untyped-def]
            self.calls.append(("media_preview", kwargs))
            return SimpleNamespace(
                status="planned",
                reason_code=None,
                selection=SimpleNamespace(status="proposed"),
                planning=SimpleNamespace(status="planned"),
            )

        async def drain_media_planning_once(self):  # type: ignore[no-untyped-def]
            self.calls.append(("media_planning", None))
            return self._planning.pop(0)

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.calls.append(("background", None))
            return SimpleNamespace(work_status="accepted")

        async def drain_media_results_once(self, *, logical_time: datetime):  # type: ignore[no-untyped-def]
            raise AssertionError(f"action budget was exceeded at {logical_time.isoformat()}")

    application = _BudgetedApplication()
    host = WorldV2PlatformHost(application=application)  # type: ignore[arg-type]

    result = await host.drain_scheduled_work(
        max_action_units=3,
        max_background_units=2,
        media_preview_trace_id="trace:shared-budget",
        media_preview_correlation_id="correlation:shared-budget",
    )

    assert result.action_units_used == 3
    assert result.background_units_used == 2
    assert result.action_statuses == ("settled",)
    assert result.background_statuses == (
        "media-preview:planned",
        "media-plan:planned",
        "accepted",
    )
    assert [operation for operation, _ in application.calls] == [
        "actions",
        "actions",
        "media_preview",
        "media_planning",
        "background",
    ]


@pytest.mark.asyncio
async def test_platform_scheduler_reserves_dispatch_capacity_for_action_authorized_by_background() -> None:
    class _InitiativeApplication(_FakeApplication):
        def __init__(self) -> None:
            super().__init__()
            self.background_ran = False
            self.proactive_dispatched = False

        async def drain_actions_once(self):  # type: ignore[no-untyped-def]
            self.calls.append(("actions", None))
            if not self.background_ran:
                return SimpleNamespace(status="unknown")
            if not self.proactive_dispatched:
                self.proactive_dispatched = True
                return SimpleNamespace(status="delivered")
            return SimpleNamespace(status="idle")

        async def drain_media_preview_once(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="blocked", reason_code="media_preview.conductor_unavailable",
                selection=None, planning=None,
            )

        async def drain_media_planning_once(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(status="idle")

        async def drain_media_continuation_once(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            return None

        async def drain_media_results_once(self, *, logical_time: datetime):  # type: ignore[no-untyped-def]
            del logical_time
            return None

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.calls.append(("background", None))
            self.background_ran = True
            return SimpleNamespace(work_status="proactive:authorized")

    application = _InitiativeApplication()
    result = await WorldV2PlatformHost(
        application=application  # type: ignore[arg-type]
    ).drain_scheduled_work(
        max_action_units=2,
        max_background_units=1,
        media_preview_trace_id="trace:initiative-reserve",
        media_preview_correlation_id="correlation:initiative-reserve",
    )

    assert result.action_units_used == 2
    assert result.action_statuses == ("unknown", "delivered")
    assert application.proactive_dispatched is True


@pytest.mark.asyncio
async def test_platform_scheduler_does_not_charge_unavailable_preview_against_other_work() -> None:
    class _UnavailablePreviewApplication(_FakeApplication):
        async def drain_actions_once(self):  # type: ignore[no-untyped-def]
            self.calls.append(("actions", None))
            return None

        async def drain_media_preview_once(self, **kwargs: object):  # type: ignore[no-untyped-def]
            self.calls.append(("media_preview", kwargs))
            return SimpleNamespace(
                status="blocked",
                reason_code="media_preview.conductor_unavailable",
                selection=None,
                planning=None,
            )

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.calls.append(("background", None))
            return SimpleNamespace(work_status="accepted")

    application = _UnavailablePreviewApplication()
    host = WorldV2PlatformHost(application=application)  # type: ignore[arg-type]

    result = await host.drain_scheduled_work(
        max_action_units=1,
        max_background_units=1,
        media_preview_trace_id="trace:unavailable-preview",
        media_preview_correlation_id="correlation:unavailable-preview",
    )

    assert result.action_units_used == 0
    assert result.background_units_used == 1


@pytest.mark.asyncio
async def test_platform_scheduler_advances_media_continuation_as_one_action_unit() -> None:
    class _ContinuationApplication(_FakeApplication):
        async def drain_actions_once(self):  # type: ignore[no-untyped-def]
            return None

        async def drain_media_preview_once(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="blocked", reason_code="media_preview.conductor_unavailable",
                selection=None, planning=None,
            )

        async def drain_media_continuation_once(self, **kwargs: object):  # type: ignore[no-untyped-def]
            self.calls.append(("media_continuation", kwargs))
            return "action:media-render:test"

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            return None

    application = _ContinuationApplication()
    result = await WorldV2PlatformHost(
        application=application  # type: ignore[arg-type]
    ).drain_scheduled_work(
        max_action_units=1, max_background_units=0,
        media_preview_trace_id="trace:continuation",
        media_preview_correlation_id="correlation:continuation",
    )
    assert result.action_units_used == 1
    assert result.background_statuses == (
        "media-continuation:action:media-render:test",
    )
    assert [item[0] for item in application.calls] == [
        "media_planning", "logical_time", "media_continuation",
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
            "production_latency_trace",
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
