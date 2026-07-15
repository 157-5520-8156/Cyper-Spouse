from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
)
from companion_daemon.world_v2.chat_model_deliberation_adapter import ChatModelDeliberationAdapter
from companion_daemon.world_v2.appraisal_chat_model_adapter import AppraisalDraftDeliberationAdapter
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.world_turn_runtime import InboundTurn


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert (platform, platform_user_id) == ("test", "user.1")
        return "user:user.1", "user:user.1"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _InvalidModel:
    async def propose(self, _request: ModelInput) -> ModelOutput:
        return ModelOutput(model_id="test-main", model_version="test.1", raw_proposal={})


class _InvalidQuick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(model_id="test-quick", model_version="test.1", raw_proposal={})


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("invalid proposal must not create an external dispatch")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _DraftChatModel:
    model = "test-flash"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "response_text": "嗯，我刚刚有点飘走了。你继续说，我在听。",
                "stance": "acknowledge_briefly",
                "brief_rationale": "Own the missed connection without adding a world claim.",
                "confidence": 7200,
            },
            ensure_ascii=False,
        )


class _DeliveredTransport:
    provider = "platform:test"

    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.bodies.append(request.body)
        return PlatformDispatchReceipt(
            provider_receipt_id="receipt:production-application:1",
            provider_ref="message:production-application:1",
            status="delivered",
            received_at=NOW,
            raw_payload_hash="sha256:" + "a" * 64,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _NoChangeAppraisalChat:
    model = "test-appraiser"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "appraise": False,
                "brief_rationale": "The ordinary message does not warrant a durable relational interpretation.",
                "behavior_tendency": "observe",
                "stance": "wait",
                "display_strategy": "withhold",
                "confidence": 3000,
            }
        )


def _config() -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:production-turn-application",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:production-turn-application",
    )


@pytest.mark.asyncio
async def test_production_application_bootstraps_sqlite_once_and_exposes_only_turn_operations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "world-v2.sqlite"
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        now=NOW,
    )
    try:
        assert await app.drain_background_once() is None
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message.1",
                text="今天有点累。",
                observed_at=NOW,
                trace_id="trace:production-turn-application",
            )
        )
        assert outcome.status == "observed_only"
        assert await app.drain_actions_once() is not None
    finally:
        app.close()

    # Rebuilding must reuse the same ledger and not seed a second world or
    # budget account.  The host does not need (and is not given) ledger writes.
    rebuilt = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        now=NOW,
    )
    rebuilt.close()
    ledger = SQLiteWorldLedger(path=path, world_id=_config().world_id)
    try:
        evidence = ledger.export_replay_evidence()
        event_types = [item.event.event_type for item in evidence.events]
        assert event_types.count("WorldStarted") == 1
        assert event_types.count("BudgetAccountConfigured") == 1
        assert ledger.project().budget_accounts[0].account_id == "account:world-v2:chat"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_production_application_materializes_a_chat_draft_and_settles_one_platform_reply(
    tmp_path: Path,
) -> None:
    transport = _DeliveredTransport()
    model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-delivery.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=model,
        quick_recovery=model,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:delivery",
                text="你刚刚没接住我。",
                observed_at=NOW,
                trace_id="trace:production-delivery",
            )
        )
        delivery = await app.drain_actions_once()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert delivery is not None and delivery.status == "settled"
    assert transport.bodies == ["嗯，我刚刚有点飘走了。你继续说，我在听。"]


@pytest.mark.asyncio
async def test_production_application_drains_appraisal_after_the_visible_reply_lane(tmp_path: Path) -> None:
    transport = _DeliveredTransport()
    reply_model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-background-appraisal.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_NoChangeAppraisalChat()),
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:background-appraisal",
                text="今天就是有点累。",
                observed_at=NOW,
                trace_id="trace:production-background-appraisal",
            )
        )
        background = await app.drain_background_once()
        await app.drain_actions_once()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert background is not None
    assert background.status == "processed"
    assert background.work_status == "no_change"
