from __future__ import annotations

from datetime import UTC, datetime
import hashlib

import pytest

from perception_test_support import perception_authorized_ledger
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.perception import PerceptionAcceptanceRuntime, PerceptionProposal
from companion_daemon.world_v2.perception_executor import PerceptionActionExecutor
from companion_daemon.world_v2.perception_result_trigger_runtime import (
    NoopPerceptionResultDeliberator,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import BudgetAccount, Observation, WorldEvent


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:perception"


def _hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _source(ledger) -> WorldEvent:
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:image",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:perception",
        causation_id="source:image",
        correlation_id="conversation:perception",
        source="test",
        source_event_id="message:image",
        actor="user:primary",
        channel="test",
        payload_ref="payload:user:image-message",
        payload_hash=_hash("user sent image"),
        text="look at this",
        received_at=NOW,
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:image",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source="test",
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded",
            world_id=WORLD,
            payload=observation.model_dump(mode="json"),
        )
        or "observation:image",
        payload=observation.model_dump(mode="json"),
    )
    head = ledger.project()
    ledger.commit(
        (event,),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    account = BudgetAccount(
        account_id="account:perception", category="tool", window_id="test", limit=10
    )
    budget = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:budget:perception",
        world_id=WORLD,
        event_type="BudgetAccountConfigured",
        logical_time=NOW,
        created_at=NOW,
        actor="operator:test",
        source="test",
        trace_id=observation.trace_id,
        causation_id=event.event_id,
        correlation_id=observation.correlation_id,
        idempotency_key="budget:perception",
        payload={"account": account.model_dump(mode="json")},
    )
    head = ledger.project()
    ledger.commit(
        (budget,),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    return event


class Inputs:
    async def resolve(self, action):
        return action.payload_ref, action.payload_hash, "image-bytes-as-sidecar-token"


class Provider:
    provider = "perception:test"

    def __init__(self):
        self.calls = 0

    async def analyze(self, **_kwargs):
        self.calls += 1
        return "result:vision:1", _hash('{"labels":["cat"]}'), "provider:vision:1", 2, NOW

    async def lookup(self, **_kwargs):
        return "result:vision:1", _hash('{"labels":["cat"]}'), "provider:vision:1", 2, NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("analysis_kind", ("vision", "transcription"))
async def test_injected_perception_provider_is_source_bound_private_and_result_triggered_once(
    monkeypatch, analysis_kind
) -> None:
    ledger, auth = perception_authorized_ledger(
        monkeypatch,
        world_id=WORLD,
        now=NOW,
        actor="agent:companion",
        subject="user:primary",
        analysis_kind=analysis_kind,
    )
    source = _source(ledger)
    input_body = "image-bytes-as-sidecar-token"
    proposal = PerceptionProposal(
        proposal_id=f"proposal:{analysis_kind}:1",
        source_event_ref=source.event_id,
        source_world_revision=ledger.lookup_event_commit(source.event_id)[1].world_revision,
        source_payload_hash=source.payload_hash,
        analysis_kind=analysis_kind,
        input_ref=f"sidecar:{analysis_kind}:1",
        input_hash=_hash(input_body),
        content_privacy_class="private",
        budget_account_id="account:perception",
        budget_limit=3,
        authorization=auth,
    )
    PerceptionAcceptanceRuntime(ledger=ledger).accept(
        proposal=proposal,
        actor="worker:vision",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:perception",
        correlation_id="conversation:perception",
    )
    provider = Provider()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=PerceptionActionExecutor(inputs=Inputs(), transport=provider),
        action_pump_owner="pump:perception",
        perception_result_owner="worker:perception-result",
        perception_result_deliberator=NoopPerceptionResultDeliberator(),
    )
    settled = await runtime.drain_actions_once()
    assert settled is not None and settled.status == "settled" and provider.calls == 1
    projection = ledger.project()
    assert projection.perception_requests[0].content_privacy_class == "private"
    assert projection.perception_results[0].analysis_kind == analysis_kind
    assert projection.perception_results[0].result_ref == "result:vision:1"
    assert projection.trigger_processes[-1].process_kind == "perception_result_deliberation"
    assert (await runtime.drain_background_once()).status == "processed"
    assert await runtime.drain_background_once() is None


@pytest.mark.asyncio
async def test_perception_executor_fails_closed_without_final_pump_authorization(
    monkeypatch,
) -> None:
    ledger, auth = perception_authorized_ledger(
        monkeypatch,
        world_id=WORLD,
        now=NOW,
        actor="agent:companion",
        subject="user:primary",
        analysis_kind="vision",
    )
    source = _source(ledger)
    proposal = PerceptionProposal(
        proposal_id="proposal:vision:closed",
        source_event_ref=source.event_id,
        source_world_revision=ledger.lookup_event_commit(source.event_id)[1].world_revision,
        source_payload_hash=source.payload_hash,
        analysis_kind="vision",
        input_ref="sidecar:image:closed",
        input_hash=_hash("image-bytes-as-sidecar-token"),
        content_privacy_class="private",
        budget_account_id="account:perception",
        budget_limit=3,
        authorization=auth,
    )
    PerceptionAcceptanceRuntime(ledger=ledger).accept(
        proposal=proposal,
        actor="worker",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:perception",
        correlation_id="conversation:perception",
    )
    with pytest.raises(ValueError, match="not authorized by ActionPump"):
        await PerceptionActionExecutor(inputs=Inputs(), transport=Provider()).dispatch(
            ledger.project().actions[0]
        )
