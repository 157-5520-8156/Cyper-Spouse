from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.action_pump import ActionPumpResult
from companion_daemon.world_v2.production_turn_application import WorldV2TurnApplication
from companion_daemon.world_v2.schemas import Action


@pytest.mark.asyncio
async def test_approved_media_application_seam_targets_authorized_action_and_returns_pump_result() -> None:
    app = object.__new__(WorldV2TurnApplication)
    calls: list[tuple[str, object]] = []

    async def authorize(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(("authorize", kwargs))
        return Action.model_construct(action_id="action:approved-media")

    async def drain(action_id: str):  # type: ignore[no-untyped-def]
        calls.append(("drain", action_id))
        return ActionPumpResult(action_id=action_id, status="settled")

    app.authorize_media_delivery = authorize  # type: ignore[attr-defined]
    app.drain_action = drain  # type: ignore[attr-defined]

    result = await app.deliver_approved_media_once(
        approval_id="approval:1",
        approval_revision=2,
        actor="agent:companion",
        target="user:user.1",
        account_id="account:image",
        amount_limit=1,
        logical_time=datetime(2026, 7, 19, 12, tzinfo=UTC),
        trace_id="trace:media-seam",
        correlation_id="correlation:media-seam",
    )

    assert result == ActionPumpResult(action_id="action:approved-media", status="settled")
    assert calls[0][0] == "authorize"
    assert calls[1] == ("drain", "action:approved-media")
    assert calls[0][1]["target"] == "user:user.1"  # type: ignore[index]
