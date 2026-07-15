from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
)
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
