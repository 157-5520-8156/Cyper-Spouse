from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.world_turn_runtime import InboundTurn, WorldTurnRuntime


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert (platform, platform_user_id) == ("test", "user.1")
        return "user:user.1", "user:user.1"


@pytest.mark.asyncio
async def test_platform_neutral_turn_ingress_records_one_idempotent_v2_observation() -> None:
    runtime = WorldRuntime.in_memory(world_id="world:turn-runtime")
    turn = WorldTurnRuntime(runtime=runtime, identities=_Identities())
    inbound = InboundTurn(
        platform="test", platform_user_id="user.1", platform_message_id="message.1",
        text="今天有点累。", observed_at=datetime(2026, 7, 15, tzinfo=UTC), trace_id="trace.1",
    )

    first = await turn.respond(inbound)
    duplicate = await turn.respond(inbound)

    assert first == duplicate
    assert first.status == "observed_only"
