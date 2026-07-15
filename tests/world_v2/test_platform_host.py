from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.world_v2.platform_host import (
    PlatformClockTick,
    PlatformInbound,
    WorldV2PlatformHost,
)


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
    }
    assert [operation for operation, _ in application.calls] == [
        "inbound",
        "actions",
        "background",
    ]


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


def test_platform_host_is_clean_application_adapter_without_legacy_or_ledger_imports() -> None:
    path = Path(__file__).parents[2] / "src/companion_daemon/world_v2/platform_host.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert imports == {"__future__", "dataclasses", "datetime", "typing", "production_turn_application"}
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
