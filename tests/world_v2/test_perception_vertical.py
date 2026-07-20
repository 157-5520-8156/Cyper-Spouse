from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json

import pytest

from perception_test_support import perception_authorized_ledger
from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.perception import PerceptionAcceptanceRuntime, PerceptionProposal
from companion_daemon.world_v2.perception_input_source import PerceptionInputDescriptor
from companion_daemon.world_v2.perception_authorization_resolver import (
    ProjectionPerceptionAuthorizationResolver,
)
from companion_daemon.world_v2.perception_proposal_compiler import (
    PerceptionProposalCompiler,
    perception_input_ref,
)
from companion_daemon.world_v2.perception_trigger import perception_trigger_event
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.perception_executor import PerceptionActionExecutor
from companion_daemon.world_v2.perception_result_context import PerceptionResultContent
from companion_daemon.world_v2.perception_result_trigger_runtime import (
    NoopPerceptionResultDeliberator,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger_context_resolver import context_capsule_compiler_from_ledger
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    ClaimLease,
    Observation,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)
from test_proposal_audit import _digest as audit_digest, _result as base_deliberation_result


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:perception"


def _hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _source(ledger, *, attachment_refs: tuple[str, ...] = ()) -> WorldEvent:
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
        attachment_refs=attachment_refs,
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
    def describe(self, *, attachment_ref: str, analysis_kind: str):
        return PerceptionInputDescriptor(
            attachment_ref=attachment_ref,
            analysis_kind=analysis_kind,
            content_hash=_hash("image-bytes-as-sidecar-token"),
        )

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

    def read_exact(self, *, result_ref: str):
        if result_ref != "result:vision:1":
            return None
        return PerceptionResultContent(
            result_ref=result_ref,
            result_hash=_hash('{"labels":["cat"]}'),
            text='{"labels":["cat"]}',
        )


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _claim_perception_trigger(ledger, source: WorldEvent) -> None:
    observation = Observation.model_validate_json(source.payload_json)
    opened = perception_trigger_event(observation=observation, observation_event=source)
    head = ledger.project()
    ledger.commit(
        (opened,),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    process = TriggerProcess.model_validate_json(json.dumps(opened.payload()["process"]))
    lease = ClaimLease(
        owner_id="worker:perception",
        attempt_id="attempt:perception:test",
        acquired_at=NOW,
        expires_at=NOW.replace(hour=13),
    )
    claimed = process.model_copy(
        update={"state": "claimed", "claim_lease": lease, "attempt_ids": (lease.attempt_id,)}
    )
    payload = {"process": claimed.model_dump(mode="json")}
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:perception-trigger:claimed:test",
        world_id=WORLD,
        event_type="TriggerProcessClaimed",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:perception",
        source="test",
        trace_id="trace:perception",
        causation_id=opened.event_id,
        correlation_id="conversation:perception",
        idempotency_key=domain_idempotency_key(
            event_type="TriggerProcessClaimed", world_id=WORLD, payload=payload
        )
        or "perception-trigger:claimed:test",
        payload=payload,
    )
    head = ledger.project()
    ledger.commit(
        (event,),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )


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
    head = ledger.project()
    capsule = context_capsule_compiler_from_ledger(
        ledger=ledger, perception_result_reader=provider
    ).compile(
        query_from_projection(
            head, actor_ref="agent:companion", trigger_ref=source.event_id
        )
    )
    assert capsule.perception_results is not None
    payload = capsule.perception_results.items[0].payload_json
    assert "external_perception_descriptor" in payload
    assert "provider_observation_not_world_fact" in payload
    assert json.loads(payload)["text"] == '{"labels":["cat"]}'

    class ForgedReader:
        def read_exact(self, *, result_ref: str):
            return PerceptionResultContent.model_construct(
                result_ref=result_ref,
                result_hash=_hash('{"labels":["cat"]}'),
                text='{"labels":["fabricated-person"]}',
            )

    forged = context_capsule_compiler_from_ledger(
        ledger=ledger, perception_result_reader=ForgedReader()
    ).compile(
        query_from_projection(
            head, actor_ref="agent:companion", trigger_ref=source.event_id
        )
    )
    assert forged.perception_results is not None
    assert forged.perception_results.items == ()


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


@pytest.mark.asyncio
async def test_audited_attachment_selection_compiles_without_model_supplied_bytes(
    monkeypatch,
) -> None:
    ledger, _auth = perception_authorized_ledger(
        monkeypatch,
        world_id=WORLD,
        now=NOW,
        actor="agent:companion",
        subject="user:primary",
        analysis_kind="vision",
    )
    attachment_ref = "attachment:image:opaque:1"
    source = _source(ledger, attachment_refs=(attachment_ref,))
    _claim_perception_trigger(ledger, source)
    head = ledger.project()
    change = TypedChange(
        change_id="change:perception:1",
        kind="perception_request",
        target_id="perception:vision",
        transition="request",
        evidence_refs=("observation:image",),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="perception_request.v1",
            value={
                "analysis_kind": "vision",
                "attachment_ref": attachment_ref,
                "content_privacy_class": "private",
                "budget_account_id": "account:perception",
                "budget_limit": 3,
            },
        ),
    )
    proposal = DecisionProposal(
        proposal_id="proposal:perception:production:1",
        trigger_ref=source.event_id,
        evaluated_world_revision=head.world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id="observation:image",
                evidence_kind="observed_message",
                source_world_revision=ledger.lookup_event_commit(source.event_id)[1].world_revision,
                immutable_hash="sha256:" + source.payload_hash,
            ),
        ),
        proposed_changes=(change,),
        action_intents=(
            ProposalActionIntent(
                intent_id="intent:perception:1",
                kind="vision",
                layer="perception_tool",
                target="perception:vision",
                payload_ref=perception_input_ref(
                    proposal_id="proposal:perception:production:1",
                    change_id="change:perception:1",
                ),
                payload_hash=_hash(attachment_ref),
                causal_change_id="change:perception:1",
            ),
        ),
        confidence=8100,
        brief_rationale="The attachment may clarify what the user is sharing.",
        behavior_tendency="inspect_if_authorized",
        stance="curious",
        display_strategy="private",
    )
    base = base_deliberation_result()
    deliberated = DeliberationResult(
        result_id="deliberation:"
        + audit_digest(
            {
                "capsule_id": base.capsule_id,
                "proposal_hash": proposal.proposal_hash,
                "attempt_audits": [base.audit.model_dump(mode="json")],
            }
        ),
        capsule_id=base.capsule_id,
        proposal=proposal,
        audit=base.audit,
        attempt_audits=(base.audit,),
    )
    audited = ProposalAuditRecorder(ledger=ledger).record(
        deliberated,
        ProposalAuditContext(
            world_id=WORLD,
            trigger_ref=source.event_id,
            logical_time=NOW,
            created_at=NOW,
            actor="agent:companion",
            source="test",
            trace_id="trace:perception",
            causation_id=source.event_id,
            correlation_id="conversation:perception",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
        ),
    )
    compiled = PerceptionProposalCompiler(
        ledger=ledger,
        authorization_resolver=ProjectionPerceptionAuthorizationResolver(),
        actor_ref="agent:companion",
        budget_account_id="account:perception",
        budget_limit=3,
        input_source=Inputs(),
    ).accept(
        world_id=WORLD,
        cursor=audited.cursor,
        proposal_id=proposal.proposal_id,
        actor="worker:perception",
        source="test",
    )
    assert compiled.status == "accepted"
    action = next(item for item in ledger.project().actions if item.action_id == compiled.action_id)
    assert action.payload_ref == attachment_ref
    assert action.payload_hash == _hash("image-bytes-as-sidecar-token")
    assert attachment_ref not in action.payload_hash
