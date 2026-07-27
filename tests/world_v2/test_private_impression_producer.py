"""The private impression producer: opener, bounded adapter, acceptance."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.batch_invariants import private_impression_trigger_identity
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.private_impression_producer import (
    PrivateImpressionDraftAdapter,
    PrivateImpressionTriggerOpener,
    PrivateImpressionTriggerRuntime,
    compile_private_impression_reflection_capsule,
    private_impression_opportunity,
)
from companion_daemon.world_v2.private_impression_events import (
    private_impression_mutation_hash,
)
from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    CompanionIdentityFrame,
)

from test_appraisal_authority import (
    accepted_payload as appraisal_payload,
    authorized_batch as appraisal_authorized_batch,
    commit,
    event,
    message_payload,
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


def test_reflection_capsule_combines_role_relationship_affect_and_lived_layers() -> None:
    projection = _ledger_with_active_appraisal().project()
    anchor = projection.appraisals[0]
    accepted_at = projection.logical_time
    assert accepted_at is not None
    layered = SimpleNamespace(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
        logical_time=accepted_at,
        appraisals=(anchor,),
        character_core=SimpleNamespace(
            core_id="core:zhizhi",
            entity_revision=2,
            actor_ref="agent:companion",
            origin=SimpleNamespace(accepted_event_ref="event:core:2"),
            values=SimpleNamespace(
                model_dump=lambda **_: {"slow_evolving": {"temperament_refs": ["observant"]}}
            ),
        ),
        relationship_states=(
            SimpleNamespace(
                relationship_id="relationship:primary",
                entity_revision=3,
                subject_ref=anchor.subject_ref,
                origin=SimpleNamespace(accepted_event_ref="event:relationship:3"),
                stage="friend",
                variables=SimpleNamespace(
                    model_dump=lambda **_: {"trust_bp": 7200, "closeness_bp": 6800}
                ),
                temperature="warm",
                commitment_refs=(),
                last_adjusted_at=accepted_at,
            ),
        ),
        affect_episodes=(
            SimpleNamespace(
                episode_id="affect:1",
                entity_revision=1,
                status="active",
                origin=SimpleNamespace(accepted_event_ref="event:affect:1"),
                components=(
                    SimpleNamespace(
                        appraisal_refs=(SimpleNamespace(appraisal_id=anchor.appraisal_id),),
                        dimension="warmth",
                        intensity_bp=4300,
                        residue_bp=600,
                        last_updated_at=accepted_at,
                    ),
                ),
                updated_at=accepted_at,
            ),
        ),
        experiences=(
            SimpleNamespace(
                experience_id="experience:tea",
                status="committed",
                origin=SimpleNamespace(accepted_event_ref="event:experience:tea"),
                values=SimpleNamespace(
                    participant_refs=(anchor.subject_ref,),
                    summary_ref="content:shared-tea",
                    occurred_from=accepted_at,
                    occurred_to=accepted_at,
                ),
            ),
        ),
        private_impressions=(
            SimpleNamespace(
                impression_id="impression:older",
                status="active",
                subject_ref=anchor.subject_ref,
                origin=SimpleNamespace(accepted_event_ref="event:impression:older"),
                reflection_summary="我之前觉得她在意被认真听见。",
                confidence_bp=5400,
                last_supported=accepted_at,
                expiry_condition="until_counter_evidence",
                interpretation_refs=(f"appraisal:{anchor.appraisal_id}:meaning:misunderstanding",),
            ),
        ),
    )

    capsule = compile_private_impression_reflection_capsule(
        projection=layered,
        appraisal=anchor,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="Geoff",
            relationship_frame="朋友",
        ),
        world_id=WORLD_ID,
        content_reader=lambda ref: "一起喝茶时聊了很久。" if ref == "content:shared-tea" else None,
    )

    assert {item.source_kind for item in capsule.sources} == {
        "appraisal",
        "character_core",
        "relationship",
        "affect",
        "experience",
        "existing_impression",
    }
    experience = next(item for item in capsule.sources if item.source_kind == "experience")
    assert json.loads(experience.value_json)["summary_text"] == "一起喝茶时聊了很久。"


class _Model:
    model = "test-private-impression"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.2) -> str:  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        return self.responses.pop(0)


def _retain(
    source_refs: list[str],
    *,
    reflection_summary: str = "我暂时觉得这更像是失望，不一定是在否定我。",
) -> str:
    return json.dumps(
        {
            "retain": True,
            "source_refs": source_refs,
            "reflection_summary": reflection_summary,
            "confidence": 6_000,
            "expiry_condition": "until_counter_evidence",
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_opener_leaves_one_deterministic_trigger_per_accepted_appraisal() -> None:
    ledger = _ledger_with_active_appraisal()
    opener = PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER)

    trigger_id = await opener.open_once()
    assert trigger_id == private_impression_trigger_identity(
        WORLD_ID, "interaction-appraisal-accepted"
    )
    process = next(
        item for item in ledger.project().trigger_processes if item.trigger_id == trigger_id
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
    model = _Model([_retain(["appraisal:appraisal:interaction:1:meaning:disappointment"])])
    adapter = PrivateImpressionDraftAdapter(model=model)
    runtime = PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=adapter,
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
    assert impression.reflection_summary == "我暂时觉得这更像是失望，不一定是在否定我。"
    assert impression.source_refs == ("interaction-appraisal-accepted",)
    assert impression.confidence_bp == 6_000
    assert impression.expiry_condition == "until_counter_evidence"
    assert len(projection.model_result_audits) == 1
    audit = projection.model_result_audits[0]
    assert audit.model_result_ref
    assert audit.capsule_id
    reflection_audit = next(
        item
        for item in projection.proposal_audits
        if item.model_result_ref == audit.model_result_ref
    )
    assert "private-reflection-draft:" in reflection_audit.proposal_json
    prompt = json.loads(model.calls[0][1]["content"])
    assert prompt["anchor_appraisal_id"] == "appraisal:interaction:1"
    assert prompt["identity_frame"]["companion_name"] == "沈知栀"
    assert any(item["source_kind"] == "appraisal" for item in prompt["sources"])
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
    assert await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once() is None


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
    assert len(projection.model_result_audits) == 1
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
    model = _Model(
        [
            _retain(
                ["appraisal:appraisal:interaction:1:meaning:invented"],
                reflection_summary="我先保留这个猜测。",
            ),
            _retain(
                ["appraisal:appraisal:interaction:1:meaning:misunderstanding"],
                reflection_summary="也许只是彼此理解岔了，我不想过早给对方定性。",
            ),
        ]
    )
    result = await PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    ).drain_one()
    assert result.work_status == "accepted"
    assert len(model.calls) == 2
    assert len(ledger.project().model_result_audits) == 2
    assert "violated the contract" in model.calls[1][-1]["content"]
    impression = ledger.project().private_impressions[0]
    assert impression.interpretation_refs == (
        "appraisal:appraisal:interaction:1:meaning:misunderstanding",
    )
    assert impression.reflection_summary == "也许只是彼此理解岔了，我不想过早给对方定性。"

    # A second consecutive violation is technical failure, not a fabricated
    # character decision. The claimed trigger remains recoverable.
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model(["not json at all {", '{"retain":"yes"}'])
    result = await PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    ).drain_one()
    assert result.work_status == "technical_failure"
    assert ledger.project().private_impressions == ()
    assert len(ledger.project().model_result_audits) == 2
    process = next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "private_impression_deliberation"
    )
    assert process.state == "claimed"
    repeated = await PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    ).drain_one()
    assert repeated.status == "owned_elsewhere"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_provider_failure_is_audited_once_and_waits_for_a_fresh_attempt() -> None:
    class _TimeoutModel:
        model = "test-timeout-role"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=0.1):  # type: ignore[no-untyped-def]
            del messages, temperature
            self.calls += 1
            raise TimeoutError("provider timed out")

    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _TimeoutModel()
    runtime = PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    )

    first = await runtime.drain_one()
    second = await runtime.drain_one()
    audit = json.loads(ledger.project().model_result_audits[-1].audit_json)

    assert first.work_status == "technical_failure"
    assert second.status == "owned_elsewhere"
    assert model.calls == 1
    assert audit["outcome"] == "timeout"
    assert audit["attempted_model_id"] == "test-timeout-role"
    assert audit["attempted_model_version"] == "private-impression-draft.3"


@pytest.mark.asyncio
async def test_audit_storage_failure_is_exposed_and_blocks_same_attempt_reentry(
    monkeypatch,
) -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model([_retain(["appraisal:appraisal:interaction:1:meaning:disappointment"])])
    adapter = PrivateImpressionDraftAdapter(model=model)
    runtime = PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=adapter,
        owner_id=OWNER,
    )

    async def storage_failed(**_kwargs) -> None:
        raise ValueError("durable audit storage failed")

    monkeypatch.setattr(runtime, "_record_model_run", storage_failed)
    with pytest.raises(ValueError, match="durable audit storage failed"):
        await runtime.drain_one()
    # A daemon restart reconstructs both runtime and adapter.  The durable
    # claimed lease still prevents reuse of the old provider-call identity.
    repeated = await PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    ).drain_one()

    assert repeated.status == "owned_elsewhere"
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        '{"retain":false}',
        _retain(["appraisal:appraisal:interaction:1:meaning:disappointment"]),
    ),
)
async def test_interleaved_world_commit_never_applies_or_consumes_stale_reflection(
    response: str,
) -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model([response])
    runtime = PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    )
    record = runtime._record_model_run

    async def record_then_advance_world(**kwargs) -> None:  # type: ignore[no-untyped-def]
        await record(**kwargs)
        commit(
            ledger,
            [
                event(
                    "message-event:interleaved",
                    "ObservationRecorded",
                    message_payload("message:interleaved"),
                )
            ],
        )

    runtime._record_model_run = record_then_advance_world  # type: ignore[method-assign]
    result = await runtime.drain_one()
    repeated = await runtime.drain_one()
    projection = ledger.project()

    assert result.work_status == "technical_failure"
    assert repeated.status == "owned_elsewhere"
    assert model.calls and len(model.calls) == 1
    assert projection.private_impressions == ()
    process = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "private_impression_deliberation"
    )
    assert process.state == "claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tamper_kind", "error"),
    (
        ("summary", "final role-model reflection audit"),
        ("appraisal", "appraisal refs do not match selected sources"),
    ),
)
async def test_typed_reflection_cannot_diverge_from_final_model_proposal(
    monkeypatch,
    tamper_kind: str,
    error: str,
) -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model([_retain(["appraisal:appraisal:interaction:1:meaning:disappointment"])])
    runtime = PrivateImpressionTriggerRuntime(
        ledger=ledger,
        adapter=PrivateImpressionDraftAdapter(model=model),
        owner_id=OWNER,
    )
    original_commit_at_cursor = ledger.commit_at_cursor
    captured = None

    class _CapturedTypedProposal(RuntimeError):
        pass

    def intercept(events, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal captured
        if any(
            item.event_type == "ProposalRecorded"
            and item.payload().get("proposal_kind") == "private_impression_transition"
            for item in events
        ):
            captured = events[0]
            raise _CapturedTypedProposal
        return original_commit_at_cursor(events, **kwargs)

    monkeypatch.setattr(ledger, "commit_at_cursor", intercept)
    with pytest.raises(_CapturedTypedProposal):
        await runtime.drain_one()
    assert captured is not None
    monkeypatch.setattr(ledger, "commit_at_cursor", original_commit_at_cursor)

    proposal = captured.payload()
    mutation = json.loads(proposal["proposed_mutation"]["payload_json"])
    if tamper_kind == "summary":
        mutation["impression"]["reflection_summary"] = "这段话并不是模型最后给出的反思。"
    else:
        mutation["appraisal_refs"][0]["hypothesis_id"] = "meaning:misunderstanding"
        mutation["impression"]["interpretation_refs"] = [
            "appraisal:appraisal:interaction:1:meaning:misunderstanding"
        ]
        proposal["appraisal_refs"] = mutation["appraisal_refs"]
    mutation["accepted_change_hash"] = private_impression_mutation_hash(mutation)
    proposal["proposed_change_hash"] = mutation["accepted_change_hash"]
    proposal["proposed_mutation"]["payload_json"] = json.dumps(
        mutation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(ValueError, match=error):
        commit(
            ledger,
            [event("private-impression-tampered-proposal", "ProposalRecorded", proposal)],
        )
