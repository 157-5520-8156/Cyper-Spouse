"""The private impression producer: opener, bounded adapter, acceptance."""

from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.batch_invariants import private_impression_trigger_identity
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.private_impression_producer import (
    PrivateImpressionDraftAdapter,
    PrivateImpressionTriggerOpener,
    PrivateImpressionTriggerRuntime,
    private_impression_opportunity,
)

from test_appraisal_authority import (
    accepted_payload as appraisal_payload,
    authorized_batch as appraisal_authorized_batch,
    commit,
    event,
    prepare_claimed_interaction,
    record_proposal as record_appraisal_proposal,
)


WORLD_ID = "world-v2-appraisal-authority"
OWNER = "worker:test:private-impression"


def _ledger_with_active_appraisal():
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(ledger, [event("world-start", "WorldStarted", {})])
    ledger, trigger, evidence = prepare_claimed_interaction(ledger)
    payload = appraisal_payload(ledger, trigger, evidence)
    record_appraisal_proposal(ledger, trigger, evidence, payload)
    commit(ledger, appraisal_authorized_batch(trigger, payload))
    return ledger


class _Model:
    model = "test-private-impression"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.2) -> str:  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        return self.responses.pop(0)


def _retain(hypothesis_ids: list[str]) -> str:
    return json.dumps({
        "retain": True,
        "hypothesis_ids": hypothesis_ids,
        "confidence": 6_000,
        "expiry_condition": "until_counter_evidence",
    })


@pytest.mark.asyncio
async def test_opener_leaves_one_deterministic_trigger_per_accepted_appraisal() -> None:
    ledger = _ledger_with_active_appraisal()
    opener = PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER)

    trigger_id = await opener.open_once()
    assert trigger_id == private_impression_trigger_identity(
        WORLD_ID, "interaction-appraisal-accepted"
    )
    process = next(
        item
        for item in ledger.project().trigger_processes
        if item.trigger_id == trigger_id
    )
    assert process.process_kind == "private_impression_deliberation"
    assert process.source_evidence_ref == "interaction-appraisal-accepted"
    assert process.state == "open"

    # The identity is durable: repeated passes never open a second trigger.
    assert await opener.open_once() is None


@pytest.mark.asyncio
async def test_producer_accepts_one_appraisal_bound_impression() -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model([_retain(["meaning:disappointment"])])
    runtime = PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    )

    result = await runtime.drain_one()
    assert result.status == "processed"
    assert result.work_status == "accepted"
    projection = ledger.project()
    impression = projection.private_impressions[0]
    assert impression.status == "active"
    assert impression.subject_ref == "interaction:user:1"
    assert impression.interpretation_refs == (
        "appraisal:appraisal:interaction:1:meaning:disappointment",
    )
    assert impression.source_refs == ("interaction-appraisal-accepted",)
    assert impression.confidence_bp == 6_000
    assert impression.expiry_condition == "until_counter_evidence"
    # The acceptance consumed the pending typed proposal.
    assert projection.private_impression_proposals == ()
    process = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "private_impression_deliberation"
    )
    assert process.state == "terminal"

    # The lane is idle afterwards, and the opener never reopens an
    # already-interpreted appraisal.
    idle = await runtime.drain_one()
    assert idle.status == "idle"
    assert private_impression_opportunity(projection) is None
    assert (
        await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
        is None
    )


@pytest.mark.asyncio
async def test_model_decline_consumes_the_trigger_without_an_impression() -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model(['{"retain":false}'])
    result = await PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    ).drain_one()
    assert result.status == "processed"
    assert result.work_status == "no_change"
    projection = ledger.project()
    assert projection.private_impressions == ()
    assert all(
        item.state == "terminal"
        for item in projection.trigger_processes
        if item.process_kind == "private_impression_deliberation"
    )


@pytest.mark.asyncio
async def test_adapter_gets_one_corrective_retry_then_fails_closed() -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    # First answer invents an unoffered hypothesis; the corrective retry
    # produces a valid consolidation.
    model = _Model([
        _retain(["meaning:invented"]),
        _retain(["meaning:misunderstanding"]),
    ])
    result = await PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    ).drain_one()
    assert result.work_status == "accepted"
    assert len(model.calls) == 2
    assert "violated the contract" in model.calls[1][-1]["content"]
    impression = ledger.project().private_impressions[0]
    assert impression.interpretation_refs == (
        "appraisal:appraisal:interaction:1:meaning:misunderstanding",
    )

    # A second consecutive violation fails closed: the trigger completes as
    # no-change instead of persisting model prose.
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model(["not json at all {", '{"retain":"yes"}'])
    result = await PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    ).drain_one()
    assert result.work_status == "no_change"
    assert ledger.project().private_impressions == ()
