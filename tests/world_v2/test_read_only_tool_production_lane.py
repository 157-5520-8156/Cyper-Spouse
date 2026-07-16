from __future__ import annotations

import hashlib
import json

import pytest

from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.external_result_trigger_runtime import NoopToolResultDeliberator
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload, DecisionProposal, ProposalActionIntent, ProposalEvidenceRef, TypedChange,
)
from companion_daemon.world_v2.read_only_tool_authorization_resolver import ProjectionReadOnlyToolAuthorizationResolver
from companion_daemon.world_v2.read_only_tool_executor import ReadOnlyToolActionExecutor
from companion_daemon.world_v2.read_only_tool_proposal_compiler import ReadOnlyToolProposalCompiler, tool_query_ref
from companion_daemon.world_v2.read_only_tool_query_reader import AuditedReadOnlyToolQueryReader
from companion_daemon.world_v2.read_only_tool_trigger import read_only_tool_trigger_event
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent

from authorization_test_support import enforcement_tool_ledger
from test_proposal_audit import _digest, _result
from test_read_only_tool_vertical import NOW, WORLD, Provider, _source


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
async def test_injected_tool_lane_compiles_enforced_request_then_settles_a_real_result(monkeypatch) -> None:
    ledger, _ = enforcement_tool_ledger(monkeypatch, world_id=WORLD, now=NOW, actor="agent:companion", subject="user:primary")
    source = _source(ledger)
    _claim_tool_trigger(ledger, source)
    head = ledger.project()
    query = '{"city":"Shanghai"}'
    query_hash = "sha256:" + hashlib.sha256(query.encode()).hexdigest()
    change = TypedChange(
        change_id="change:tool:1", kind="read_only_tool_request", target_id="tool:weather", transition="request",
        evidence_refs=("observation:tool-question",),
        payload=CanonicalTypedPayload.from_value(payload_schema="read_only_tool_request.v1", value={
            "tool_name": "weather", "target": "tool:weather", "query": query,
            "budget_account_id": "account:tool", "budget_limit": 5,
        }),
    )
    proposal = DecisionProposal(
        proposal_id="proposal:tool:production:1", trigger_ref=source.event_id,
        evaluated_world_revision=head.world_revision,
        evidence_refs=(ProposalEvidenceRef(ref_id="observation:tool-question", evidence_kind="observed_message", source_world_revision=ledger.lookup_event_commit(source.event_id)[1].world_revision, immutable_hash="sha256:" + source.payload_hash),),
        proposed_changes=(change,), action_intents=(ProposalActionIntent(
            intent_id="intent:tool:1", kind="read_only_tool", layer="read_only_tool", target="tool:weather",
            payload_ref=tool_query_ref(proposal_id="proposal:tool:production:1", change_id="change:tool:1"), payload_hash=query_hash,
            causal_change_id="change:tool:1",
        ),), confidence=9000, brief_rationale="A current weather lookup is useful.",
        behavior_tendency="verify", stance="helpful", display_strategy="private",
    )
    base = _result()
    result = DeliberationResult(
        result_id="deliberation:" + _digest({
            "capsule_id": base.capsule_id,
            "proposal_hash": proposal.proposal_hash,
            "attempt_audits": [base.audit.model_dump(mode="json")],
        }),
        capsule_id=base.capsule_id, proposal=proposal, audit=base.audit, attempt_audits=(base.audit,),
    )
    audited = ProposalAuditRecorder(ledger=ledger).record(result, ProposalAuditContext(
        world_id=WORLD, trigger_ref=source.event_id, logical_time=NOW, created_at=NOW, actor="agent:companion", source="test",
        trace_id="trace:tool", causation_id=source.event_id, correlation_id="conversation:tool", evaluated_world_revision=head.world_revision,
        expected_commit_world_revision=head.world_revision, expected_deliberation_revision=head.deliberation_revision,
    ))
    compiled = ReadOnlyToolProposalCompiler(
        ledger=ledger, authorization_resolver=ProjectionReadOnlyToolAuthorizationResolver(), actor_ref="agent:companion",
        budget_account_id="account:tool", budget_limit=5,
    ).accept(world_id=WORLD, cursor=audited.cursor, proposal_id=proposal.proposal_id, actor="worker:tool", source="test")
    assert compiled.status == "accepted"
    provider = Provider()
    runtime = WorldRuntime(
        world_id=WORLD, ledger=ledger,
        action_executor=ReadOnlyToolActionExecutor(queries=AuditedReadOnlyToolQueryReader(ledger=ledger), transport=provider),
        action_pump_owner="pump:tool", external_result_owner="worker:result", external_result_deliberator=NoopToolResultDeliberator(),
    )
    outcome = await runtime.drain_actions_once()
    assert outcome is not None and outcome.status == "settled" and provider.calls == 1
    assert ledger.project().tool_results[0].result_ref == "result:weather:1"
    background = await runtime.drain_background_once()
    assert background is not None and background.status == "processed"
