from __future__ import annotations

from datetime import UTC, datetime
import hashlib

import pytest

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.external_result_trigger_runtime import NoopToolResultDeliberator
from companion_daemon.world_v2.read_only_tool import (
    ReadOnlyToolAcceptanceRuntime,
    ReadOnlyToolProposal,
    external_result_trigger_id,
)
from companion_daemon.world_v2.read_only_tool_executor import ReadOnlyToolActionExecutor
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import BudgetAccount, Observation, WorldEvent


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:read-only-tool"


def _hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _source(ledger: WorldLedger) -> WorldEvent:
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:tool-question",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:tool",
        causation_id="source:tool",
        correlation_id="conversation:tool",
        source="test",
        source_event_id="message:tool-question",
        actor="user:primary",
        channel="test",
        payload_ref="payload:user:tool-question",
        payload_hash=_hash("what is the weather?"),
        text="what is the weather?",
        received_at=NOW,
    )
    source = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:tool-question",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source="test",
        trace_id="trace:tool",
        causation_id="source:tool",
        correlation_id="conversation:tool",
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD, payload=observation.model_dump(mode="json")
        ) or "test:tool-question",
        payload=observation.model_dump(mode="json"),
    )
    ledger.commit((source,), expected_world_revision=0, expected_deliberation_revision=0)
    account = BudgetAccount(account_id="account:tool", category="tool", window_id="test", limit=20)
    ledger.commit(
        (
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:budget:tool",
                world_id=WORLD,
                event_type="BudgetAccountConfigured",
                logical_time=NOW,
                created_at=NOW,
                actor="operator:test",
                source="test",
                trace_id="trace:tool",
                causation_id="source:tool",
                correlation_id="conversation:tool",
                idempotency_key="test:budget:tool",
                payload={"account": account.model_dump(mode="json")},
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    return source


class Queries:
    async def resolve(self, action):
        return ("weather", action.payload_ref, action.payload_hash, '{"city":"Shanghai"}')


class Provider:
    provider = "tool:test"

    def __init__(self) -> None:
        self.calls = 0
        self._result = ("result:weather:1", _hash('{"condition":"sunny"}'), "provider:weather:1", 3, NOW)

    async def execute(self, **_kwargs):
        self.calls += 1
        return self._result

    async def lookup(self, *, idempotency_key: str):
        assert idempotency_key
        return self._result


@pytest.mark.asyncio
async def test_source_bound_tool_request_settles_result_and_opens_one_result_trigger() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    source = _source(ledger)
    proposal = ReadOnlyToolProposal(
        proposal_id="proposal:tool:1",
        source_event_ref=source.event_id,
        source_world_revision=1,
        source_payload_hash=source.payload_hash,
        tool_name="weather",
        target="tool:weather",
        query_ref="payload:tool:weather:1",
        query_hash=_hash('{"city":"Shanghai"}'),
        budget_account_id="account:tool",
        budget_limit=5,
    )
    ReadOnlyToolAcceptanceRuntime(ledger=ledger).accept(
        proposal=proposal,
        actor="worker:tool-proposal",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:tool",
        correlation_id="conversation:tool",
    )
    provider = Provider()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=ReadOnlyToolActionExecutor(queries=Queries(), transport=provider),
        action_pump_owner="pump:tool",
        external_result_owner="worker:external-result",
        external_result_deliberator=NoopToolResultDeliberator(),
    )

    outcome = await runtime.drain_actions_once()

    assert outcome is not None and outcome.status == "settled"
    assert provider.calls == 1
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert projection.budget_reservations[0].state == "settled"
    assert projection.tool_results[0].result_ref == "result:weather:1"
    trigger = projection.trigger_processes[-1]
    assert trigger.process_kind == "external_result_deliberation"
    assert trigger.trigger_id == external_result_trigger_id(
        world_id=WORLD, result_id=projection.tool_results[0].result_id
    )
    assert trigger.state == "open"
    completed = await runtime.drain_background_once()
    repeated = await runtime.drain_background_once()
    assert completed.status == "processed"
    assert repeated is None
    assert ledger.project().trigger_processes[-1].state == "terminal"


@pytest.mark.asyncio
async def test_tool_executor_recovery_lookup_keeps_the_same_result_descriptor() -> None:
    provider = Provider()
    executor = ReadOnlyToolActionExecutor(queries=Queries(), transport=provider)
    # The executor's recovery contract is tested through a fully shaped
    # Action in the acceptance path above; reuse that immutable shape here.
    ledger = WorldLedger.in_memory(world_id=WORLD)
    source = _source(ledger)
    proposal = ReadOnlyToolProposal(
        proposal_id="proposal:tool:recovery",
        source_event_ref=source.event_id,
        source_world_revision=1,
        source_payload_hash=source.payload_hash,
        tool_name="weather",
        target="tool:weather",
        query_ref="payload:tool:weather:recovery",
        query_hash=_hash('{"city":"Shanghai"}'),
        budget_account_id="account:tool",
        budget_limit=5,
    )
    ReadOnlyToolAcceptanceRuntime(ledger=ledger).accept(
        proposal=proposal, actor="worker", source="test", logical_time=NOW,
        created_at=NOW, trace_id="trace:tool", correlation_id="conversation:tool",
    )
    action = ledger.project().actions[0]
    recovered = await executor.lookup_result(action)

    assert recovered is not None
    assert recovered.result_ref == "result:weather:1"
    assert recovered.result_hash == _hash('{"condition":"sunny"}')
