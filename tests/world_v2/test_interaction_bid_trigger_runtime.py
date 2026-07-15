from __future__ import annotations

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.context_capsule import ContextCapsuleBudgetPolicy, SliceBudget
from companion_daemon.world_v2.deliberation import Deliberation, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.interaction_bid_acceptance_runtime import InteractionBidAcceptanceRuntime
from companion_daemon.world_v2.interaction_bid_deliberation_turn import InteractionBidDeliberationTurn
from companion_daemon.world_v2.interaction_bid_proposal_compiler import InteractionBidProposalCompiler
from companion_daemon.world_v2.interaction_bid_proposal_worker import InteractionBidProposalWorker
from companion_daemon.world_v2.interaction_bid_trigger_runtime import InteractionBidTriggerRuntime
from companion_daemon.world_v2.media_thread_acceptance_runtime import (
    MediaDeliveryThreadAcceptanceRuntime,
)
from companion_daemon.world_v2.media_thread_proposal_compiler import (
    MediaDeliveryThreadProposalCompiler,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import context_capsule_compiler_from_ledger
from companion_daemon.world_v2.media_delivery_interaction import media_delivery_interaction_trigger_event
from companion_daemon.world_v2.production_proposal_grammar import production_proposal_grammar
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    TypedChange,
)
from companion_daemon.world_v2.schemas import ProjectionCursor
from test_interaction_bid_authority import NOW, _event, _prepared_ledger


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="background", router_version="test.1")


class _BidModel:
    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, request) -> ModelOutput:
        self.calls += 1
        source = request.trigger_evidence[0]
        proposal = DecisionProposal(
            proposal_id="proposal:delivery-interaction:1",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(source,),
            proposed_changes=(
                TypedChange(
                    change_id="change:delivery-interaction:1",
                    kind="interaction_bid_transition",
                    target_id="bid:delivery-interaction:1",
                    transition="open",
                    expected_entity_revision=0,
                    evidence_refs=(source.ref_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="interaction_bid_transition.v1",
                        value={
                            "bid_id": "bid:delivery-interaction:1",
                            "goal": "invite_reply",
                            "hoped_response": "user_comments_on_photo",
                            "pressure": 1200,
                            "audience": "user:1",
                            "due": None,
                        },
                    ),
                ),
            ),
            action_intents=(),
            confidence=7_600,
            brief_rationale="The verified delivery may merit a gentle private bid.",
            behavior_tendency="offer",
            stance="invite",
            display_strategy="private",
        )
        return ModelOutput(
            model_id="test-interaction-bid",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )

    async def recover(self, request, _failure: str) -> ModelOutput:
        return await self.propose(request)


class _NoChangeModel(_BidModel):
    async def propose(self, request) -> ModelOutput:
        self.calls += 1
        source = request.trigger_evidence[0]
        proposal = DecisionProposal(
            proposal_id="proposal:delivery-interaction:none",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(source,),
            proposed_changes=(),
            action_intents=(),
            confidence=6_000,
            brief_rationale="A delivery alone does not require a social bid.",
            behavior_tendency="withhold",
            stance="none",
            display_strategy="private",
        )
        return ModelOutput(
            model_id="test-interaction-bid",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )


class _ThreadModel(_BidModel):
    async def propose(self, request) -> ModelOutput:
        self.calls += 1
        source = request.trigger_evidence[0]
        proposal = DecisionProposal(
            proposal_id="proposal:delivery-thread:1",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(source,),
            proposed_changes=(
                TypedChange(
                    change_id="change:delivery-thread:1",
                    kind="media_delivery_thread_transition",
                    target_id="thread:delivery-media:1",
                    transition="open",
                    expected_entity_revision=0,
                    evidence_refs=(source.ref_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="media_delivery_thread_transition.v1",
                        value={
                            "thread_id": "thread:delivery-media:1",
                            "thread_kind": "topic_open",
                            "subject_ref": "subject:delivered-photo",
                            "conversation_ref": "conversation:1",
                            "importance": 2600,
                            "resolution_contract_ref": "resolution-contract:photo-followup",
                            "expires_at": None,
                            "privacy_class": "private",
                        },
                    ),
                ),
            ),
            action_intents=(),
            confidence=7_100,
            brief_rationale="The delivery can leave a bounded private topic for later.",
            behavior_tendency="remember",
            stance="open",
            display_strategy="private",
        )
        return ModelOutput(
            model_id="test-media-thread",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )


def _runtime(*, model):
    # Reuse the media preconditions from the authority fixture, but reconstruct
    # the event stream with a pinned WorldStarted authority.  Context Capsule
    # compilation intentionally refuses the fixture's compiler-only stream.
    compiler_ledger, source, _revision = _prepared_ledger()
    ledger = WorldLedger.in_memory(world_id=compiler_ledger.world_id)
    ledger.commit([_event("WorldStarted", {}, "started")], expected_world_revision=0, expected_deliberation_revision=0)
    seed = compiler_ledger._state.model_copy(  # noqa: SLF001 - authority fixture seed
        update={"media_deliveries": (), "trigger_processes": (), "logical_time": NOW}
    )
    ledger._state = seed  # noqa: SLF001 - seed prior validated media lifecycle state
    trigger = media_delivery_interaction_trigger_event(source_event=source)
    ledger.commit([source, trigger], expected_world_revision=1, expected_deliberation_revision=0)
    issuer = AcceptedLedgerBatchIssuer()
    ledger._accepted_batch_issuer = issuer  # noqa: SLF001 - fixture composes acceptance capability
    turn = InteractionBidDeliberationTurn(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(
            ledger=ledger,
            policy=ContextCapsuleBudgetPolicy(
                hard_max_characters=100_000,
                current_situation=SliceBudget(max_items=1, max_fields=256, max_characters=20_000),
            ),
        ),
        deliberation=Deliberation(
            router=_Router(),
            main_model=model,
            quick_recovery=model,
            proposal_grammar=production_proposal_grammar("interaction_bid"),
        ),
        companion_actor_ref="actor:companion",
    )
    worker = InteractionBidProposalWorker(
        compiler=InteractionBidProposalCompiler(ledger=ledger),
        acceptance=InteractionBidAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        media_thread_compiler=MediaDeliveryThreadProposalCompiler(ledger=ledger),
        media_thread_acceptance=MediaDeliveryThreadAcceptanceRuntime(
            ledger=ledger, batch_issuer=issuer
        ),
        actor="worker:interaction",
    )
    runtime = InteractionBidTriggerRuntime(
        ledger=ledger, turn=turn, worker=worker, owner_id="worker:interaction"
    )
    return ledger, runtime, turn, worker


@pytest.mark.asyncio
async def test_delivery_trigger_audits_compiles_accepts_and_completes() -> None:
    model = _BidModel()
    ledger, runtime, _turn, _worker = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.status == "processed"
    assert result.work_status == "accepted"
    assert model.calls == 1
    projection = ledger.project()
    assert projection.interaction_bids[0].delivery_id
    assert next(item for item in projection.trigger_processes if item.process_kind == "media_delivery_interaction").state == "terminal"


@pytest.mark.asyncio
async def test_delivery_trigger_records_audited_no_change_and_completes() -> None:
    model = _NoChangeModel()
    ledger, runtime, _turn, _worker = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.status == "processed"
    assert result.work_status == "no_change"
    assert model.calls == 1
    assert ledger.project().interaction_bids == ()
    assert next(item for item in ledger.project().trigger_processes if item.process_kind == "media_delivery_interaction").state == "terminal"


@pytest.mark.asyncio
async def test_delivery_trigger_can_open_dedicated_thread_and_complete() -> None:
    model = _ThreadModel()
    ledger, runtime, _turn, _worker = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.status == "processed"
    assert result.work_status == "accepted"
    assert model.calls == 1
    projection = ledger.project()
    assert projection.interaction_bids == ()
    assert projection.threads[0].thread_id == "thread:delivery-media:1"
    assert any(
        stored.event.event_type == "MediaDeliveryThreadProposalRecorded"
        for stored in ledger._events  # noqa: SLF001 - assert durable compiler hand-off
    )
    assert next(
        item for item in projection.trigger_processes if item.process_kind == "media_delivery_interaction"
    ).state == "terminal"


@pytest.mark.asyncio
async def test_delivery_trigger_recovers_compiled_bid_without_recalling_model() -> None:
    model = _BidModel()
    ledger, runtime, turn, worker = _runtime(model=model)
    before = ledger.project()
    process = next(item for item in before.trigger_processes if item.process_kind == "media_delivery_interaction")
    source = ledger.lookup_event_commit(process.source_evidence_ref or "")
    assert source is not None
    active = await runtime._claim_or_reclaim(  # noqa: SLF001 - explicit crash boundary setup
        process=process, source_event=source[0], projection=before
    )
    assert active is not None
    head = ledger.project()
    audited = await turn.audit_delivery(
        delivery_event=source[0],
        cursor=ProjectionCursor(
            world_revision=head.world_revision,
            deliberation_revision=head.deliberation_revision,
            ledger_sequence=head.ledger_sequence,
        ),
    )
    # Persist the compiler hand-off, then emulate a process crash immediately
    # before the atomic acceptance commit.
    InteractionBidProposalCompiler(ledger=ledger).record(
        world_id=ledger.world_id,
        cursor=audited.commit.cursor,
        proposal_id=audited.commit.proposal_id or "",
    )
    recovered = await InteractionBidTriggerRuntime(
        ledger=ledger, turn=turn, worker=worker, owner_id="worker:interaction"
    ).drain_one()

    assert recovered.status == "processed"
    assert recovered.work_status == "accepted"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_delivery_trigger_recovers_compiled_thread_without_recalling_model() -> None:
    model = _ThreadModel()
    ledger, runtime, turn, worker = _runtime(model=model)
    before = ledger.project()
    process = next(
        item for item in before.trigger_processes if item.process_kind == "media_delivery_interaction"
    )
    source = ledger.lookup_event_commit(process.source_evidence_ref or "")
    assert source is not None
    active = await runtime._claim_or_reclaim(  # noqa: SLF001 - explicit crash boundary setup
        process=process, source_event=source[0], projection=before
    )
    assert active is not None
    head = ledger.project()
    audited = await turn.audit_delivery(
        delivery_event=source[0],
        cursor=ProjectionCursor(
            world_revision=head.world_revision,
            deliberation_revision=head.deliberation_revision,
            ledger_sequence=head.ledger_sequence,
        ),
    )
    MediaDeliveryThreadProposalCompiler(ledger=ledger).record(
        world_id=ledger.world_id,
        cursor=audited.commit.cursor,
        proposal_id=audited.commit.proposal_id or "",
    )

    recovered = await InteractionBidTriggerRuntime(
        ledger=ledger, turn=turn, worker=worker, owner_id="worker:interaction"
    ).drain_one()

    assert recovered.status == "processed"
    assert recovered.work_status == "accepted"
    assert model.calls == 1
    assert ledger.project().threads[0].thread_id == "thread:delivery-media:1"
