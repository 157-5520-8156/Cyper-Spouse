from __future__ import annotations

import hashlib
import json

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.read_only_tool import (
    ReadOnlyToolAcceptanceRuntime,
    ReadOnlyToolProposal,
)
from companion_daemon.world_v2.read_only_tool_executor import ReadOnlyToolActionExecutor
from companion_daemon.world_v2.read_only_tool_trigger import read_only_tool_trigger_event
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent

from authorization_test_support import enforcement_tool_ledger
from test_read_only_tool_vertical import NOW, WORLD, Provider, Queries, _source


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)


def _claim_tool_trigger(ledger, source) -> None:
    # The trigger helper deliberately verifies the original Observation.  Read it back instead
    # of relying on an untrusted caller-owned object.
    from companion_daemon.world_v2.schemas import Observation
    observation = Observation.model_validate_json(source.payload_json)
    opened = read_only_tool_trigger_event(observation=observation, observation_event=source)
    head = ledger.project()
    ledger.commit((opened,), expected_world_revision=head.world_revision, expected_deliberation_revision=head.deliberation_revision)
    process = opened.payload()["process"]
    opened_process = TriggerProcess.model_validate_json(json.dumps(process))
    lease = ClaimLease(owner_id="worker:tool", attempt_id="attempt:tool:1", acquired_at=NOW, expires_at=NOW.replace(hour=13))
    claimed = opened_process.model_copy(update={"state": "claimed", "claim_lease": lease, "attempt_ids": (lease.attempt_id,)})
    payload = {"process": claimed.model_dump(mode="json")}
    event = WorldEvent.from_payload(
        schema_version="world-v2.1", event_id="event:tool-trigger:claimed", world_id=WORLD,
        event_type="TriggerProcessClaimed", logical_time=NOW, created_at=NOW, actor="worker:tool",
        source="test", trace_id="trace:tool", causation_id=opened.event_id, correlation_id="conversation:tool",
        idempotency_key=domain_idempotency_key(event_type="TriggerProcessClaimed", world_id=WORLD, payload=payload) or "tool-claim",
        payload=payload,
    )
    head = ledger.project()
    ledger.commit((event,), expected_world_revision=head.world_revision, expected_deliberation_revision=head.deliberation_revision)


@pytest.mark.asyncio
async def test_injected_tool_lane_settles_result_without_retired_parallel_author(monkeypatch) -> None:
    # The independent read-only-tool author was retired by the CharacterInterior
    # migration.  Opening (and even claiming) the legacy tool trigger now
    # derives a deterministic retired-technical outcome; the production lane
    # must settle tool results through the typed acceptance runtime instead of
    # a retired parallel author.  This test pins the retirement semantics and
    # proves the injected tool lane still works end to end.
    ledger, authorization = enforcement_tool_ledger(monkeypatch, world_id=WORLD, now=NOW, actor="agent:companion", subject="user:primary")
    source = _source(ledger)
    _claim_tool_trigger(ledger, source)
    projection = ledger.project()
    retired = next(
        (
            item
            for item in projection.trigger_processes
            if item.process_kind == "read_only_tool_deliberation"
        ),
        None,
    )
    assert retired is not None
    assert retired.state == "terminal"
    assert retired.runtime_outcome_ref == (
        "retired-technical:read-only-tool-independent-author-removed"
    )
    assert retired.trigger_id in projection.completed_trigger_ids

    # The typed acceptance lane (the production path) still settles the tool
    # result without ever re-arming the retired author trigger.
    proposal = ReadOnlyToolProposal(
        proposal_id="proposal:tool:production:1",
        source_event_ref=source.event_id,
        source_world_revision=ledger.lookup_event_commit(source.event_id)[1].world_revision,
        source_payload_hash=source.payload_hash,
        tool_name="weather",
        target="tool:weather",
        query_ref="payload:tool:weather:1",
        query_hash="sha256:" + hashlib.sha256('{"city":"Shanghai"}'.encode()).hexdigest(),
        budget_account_id="account:tool",
        budget_limit=5,
        authorization=authorization,
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
        world_id=WORLD, ledger=ledger,
        action_executor=ReadOnlyToolActionExecutor(queries=Queries(), transport=provider),
        action_pump_owner="pump:tool",
    )
    outcome = await runtime.drain_actions_once()
    assert outcome is not None and outcome.status == "settled" and provider.calls == 1
    projection = ledger.project()
    assert projection.tool_results[0].result_ref == "result:weather:1"
    assert not any(
        item.process_kind == "external_result_deliberation"
        for item in projection.trigger_processes
    )
    background = await runtime.drain_background_once()
    assert background is None
