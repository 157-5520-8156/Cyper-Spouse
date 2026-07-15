from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.advisory_compiler import AdvisoryAdapterInput, AdvisoryCompiler
from companion_daemon.world_v2.deliberation import (
    Deliberation,
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.batch_invariants import interaction_appraisal_trigger_identity
from companion_daemon.world_v2.ledger_context_resolver import (
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.minimal_reply_acceptance import ReplyBudgetPolicy
from companion_daemon.world_v2.minimal_reply_atomic_recorder import MinimalReplyAtomicRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    MinimalProposal,
    ProposalActionIntent,
    TypedChange,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import BudgetAccount, Observation, ProjectionCursor, WorldEvent
from companion_daemon.world_v2.matrix_catalog import (
    CandidateDistribution,
    ClassificationCandidate,
    default_matrix_catalog,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:pinned-turn"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _InvalidModel:
    def __init__(self) -> None:
        self.request: ModelInput | None = None

    async def propose(self, _request: ModelInput) -> ModelOutput:
        self.request = _request
        return ModelOutput(
            model_id="test-main",
            model_version="test.1",
            raw_proposal={},
            input_tokens=1,
            output_tokens=1,
        )


class _InvalidQuick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(
            model_id="test-quick",
            model_version="test.1",
            raw_proposal={},
            input_tokens=1,
            output_tokens=1,
        )


class _MinimalReplyModel:
    """A real valid model adapter for the complete reply-acceptance vertical."""

    async def propose(self, request: ModelInput) -> ModelOutput:
        text = "我听见你的失望了，刚刚确实没有接住。"
        payload_hash = "sha256:" + sha256(text.encode("utf-8")).hexdigest()
        proposal = MinimalProposal(
            proposal_id="proposal:pinned-turn:minimal:1",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(),
            proposed_changes=(
                TypedChange(
                    change_id="change:pinned-turn:expression:1",
                    kind="expression_plan_transition",
                    target_id="plan:pinned-turn:reply:1",
                    transition="accept",
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="expression_plan_transition.v1",
                        value={
                            "plan_id": "plan:pinned-turn:reply:1",
                            "overall_intent": "reply",
                            "ordering_policy": "dependencies",
                            "terminal_policy": "settle",
                            "beat_drafts": [
                                {
                                    "beat_id": "beat:pinned-turn:reply:1",
                                    "inline_text": text,
                                    "materialized_payload_ref": "payload:pinned-turn:reply:1",
                                    "payload_hash": payload_hash,
                                    "content_type": "text/plain",
                                    "dependency_beat_ids": [],
                                    "delay_window": None,
                                    "cancel_policy": "cancel-before-dispatch",
                                    "reconsider_policy": "reconsider-on-new-observation",
                                    "merge_policy": "never",
                                }
                            ],
                        },
                    ),
                ),
            ),
            action_intents=(
                ProposalActionIntent(
                    intent_id="intent:pinned-turn:reply:1",
                    kind="reply",
                    layer="external_action",
                    target="user:primary",
                    payload_ref="payload:pinned-turn:reply:1",
                    payload_hash=payload_hash,
                    causal_change_id="change:pinned-turn:expression:1",
                    beat_ref="beat:pinned-turn:reply:1",
                ),
            ),
            confidence=7_000,
            brief_rationale="Acknowledge the user's disappointment without making world claims.",
            source_model_result="model-result:placeholder",
            response_text=text,
            stance="acknowledge_briefly",
        )
        return ModelOutput(
            model_id="test-minimal-main",
            model_version="test.1",
            raw_proposal=proposal.model_dump(mode="json"),
            input_tokens=1,
            output_tokens=1,
        )


class _EmotionAdvice:
    adapter_id = "emotion"
    version = "test.1"

    def __init__(self) -> None:
        self.received: AdvisoryAdapterInput | None = None

    async def classify(self, request: AdvisoryAdapterInput) -> tuple[CandidateDistribution, ...]:
        self.received = request
        return (
            CandidateDistribution(
                catalog_version="world-v2-matrix-1",
                field_id="appraisal.negative",
                candidates=(
                    ClassificationCandidate(
                        value="disappointment",
                        weight=7100,
                        confidence=7800,
                        producer="emotion@test.1",
                        source_refs=(request.trigger_ref,),
                        expires_at=request.expires_at,
                    ),
                ),
                produced_at=request.logical_time,
            ),
        )


class _InvalidAdvice(_EmotionAdvice):
    async def classify(self, request: AdvisoryAdapterInput) -> tuple[CandidateDistribution, ...]:
        output = (await super().classify(request))[0]
        return (output.model_copy(update={"field_id": "unknown.advisory.field"}),)


def _observation() -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id="observation:pinned-turn:1",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:pinned-turn",
        causation_id="cause:pinned-turn",
        correlation_id="correlation:pinned-turn",
        source="test",
        source_event_id="message:pinned-turn:1",
        actor="user:primary",
        channel="test",
        payload_ref="payload:pinned-turn:1",
        payload_hash="sha256:" + "a" * 64,
        text="我好像有点失望，你刚刚没怎么接住我。",
        received_at=NOW,
    )


def _world_started() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:pinned-turn:world-started",
        world_id=WORLD,
        event_type="WorldStarted",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:pinned-turn:start",
        causation_id="cause:pinned-turn:start",
        correlation_id="correlation:pinned-turn:start",
        idempotency_key="world-started:pinned-turn",
        payload={},
    )


def _budget_configured(*, limit: int = 100) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:pinned-turn:budget-account",
        world_id=WORLD,
        event_type="BudgetAccountConfigured",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:pinned-turn:budget",
        causation_id="cause:pinned-turn:budget",
        correlation_id="correlation:pinned-turn:budget",
        idempotency_key="budget-account:pinned-turn",
        payload={
            "account": BudgetAccount(
                account_id="account:pinned-turn:chat",
                category="chat",
                window_id="window:pinned-turn",
                limit=limit,
            ).model_dump(mode="json")
        },
    )


@pytest.mark.asyncio
async def test_runtime_audits_one_cursor_pinned_turn_without_authorizing_effects() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    capsules: ContextCapsuleCompiler = context_capsule_compiler_from_ledger(ledger=ledger)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=capsules,
        deliberation=Deliberation(
            router=_Router(), main_model=_InvalidModel(), quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
    )
    runtime = WorldRuntime(world_id=WORLD, ledger=ledger, pinned_turn=turn)

    first = await runtime.ingest(_observation())
    duplicate = await runtime.ingest(_observation())

    assert first == duplicate
    projection = ledger.project()
    assert projection.world_revision == 2
    assert projection.deliberation_revision == 2
    assert len(projection.model_result_audits) == 2
    assert projection.proposal_audits == ()


@pytest.mark.asyncio
async def test_runtime_accepts_audited_minimal_reply_once_and_replays_its_outcome() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit((_budget_configured(),), expected_world_revision=1, expected_deliberation_revision=0)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(), main_model=_MinimalReplyModel(), quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
    )
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        pinned_turn=turn,
        reply_policy=ReplyBudgetPolicy(
            account_id="account:pinned-turn:chat",
            amount_limit=10,
            actor="agent:companion",
            target="user:primary",
            recovery_policy="effect_once",
        ),
        reply_recorder=MinimalReplyAtomicRecorder(batch_issuer=issuer),
    )

    first = await runtime.ingest(_observation())
    duplicate = await runtime.ingest(_observation())

    assert len(ledger.project().proposal_audits) == 1
    assert first.status == "action_authorized"
    assert duplicate == first
    projection = ledger.project()
    assert len(projection.proposal_audits) == 1
    assert len(projection.stored_message_payloads) == 1
    assert projection.stored_message_payloads[0].text == "我听见你的失望了，刚刚确实没有接住。"
    assert len(projection.expression_beats) == 1
    assert len(projection.pending_actions) == 1
    assert projection.budget_accounts[0].reserved == 10


@pytest.mark.asyncio
async def test_runtime_defers_an_audited_reply_when_the_budget_is_unavailable() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (_budget_configured(limit=5),), expected_world_revision=1, expected_deliberation_revision=0
    )
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(), main_model=_MinimalReplyModel(), quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
    )
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        pinned_turn=turn,
        reply_policy=ReplyBudgetPolicy(
            account_id="account:pinned-turn:chat",
            amount_limit=10,
            actor="agent:companion",
            target="user:primary",
            recovery_policy="effect_once",
        ),
        reply_recorder=MinimalReplyAtomicRecorder(batch_issuer=issuer),
    )

    outcome = await runtime.ingest(_observation())

    assert outcome.status == "deferred"
    assert outcome.deferred_refs == ("minimal_reply_acceptance.budget_unavailable",)
    projection = ledger.project()
    assert len(projection.proposal_audits) == 1
    assert projection.stored_message_payloads == ()
    assert projection.expression_beats == ()
    assert projection.pending_actions == ()
    assert projection.budget_accounts[0].reserved == 0


@pytest.mark.asyncio
async def test_runtime_opens_and_claims_one_source_bound_interaction_appraisal_trigger() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        interaction_appraisal_owner="worker:interaction-appraisal",
    )

    first = await runtime.ingest(_observation())
    duplicate = await runtime.ingest(_observation())

    trigger_id = interaction_appraisal_trigger_identity(WORLD, _observation().observation_id)
    projection = ledger.project()
    trigger = next(item for item in projection.trigger_processes if item.trigger_id == trigger_id)
    assert trigger.process_kind == "interaction_appraisal"
    assert trigger.source_evidence_ref == _observation().observation_id
    assert trigger.state == "claimed"
    assert trigger.claim_lease is not None
    assert trigger.claim_lease.owner_id == "worker:interaction-appraisal"
    assert first == duplicate
    assert projection.world_revision == 2
    assert projection.deliberation_revision == 2


@pytest.mark.asyncio
async def test_pinned_turn_passes_source_bound_advisory_candidates_to_deliberation() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    model = _InvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(), main_model=model, quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
        advisory_compiler=AdvisoryCompiler(
            catalog=default_matrix_catalog(),
            adapters=(advice := _EmotionAdvice(),),
            authority_key=b"pinned-turn-advisory-test-authority-key",
        ),
    )
    runtime = WorldRuntime(world_id=WORLD, ledger=ledger, pinned_turn=turn)

    await runtime.ingest(_observation())

    assert model.request is not None
    content = model.request.model_content_json
    assert '"kind":"appraisal.negative"' in content
    assert '"value":"disappointment"' in content
    assert '"source_refs":["event:trigger:observation:test:message:pinned-turn:1"]' in content
    assert advice.received is not None
    assert advice.received.trigger["text"] == "我好像有点失望，你刚刚没怎么接住我。"
    projection = ledger.project()
    assert projection.world_revision == 2
    assert projection.deliberation_revision == 2


@pytest.mark.asyncio
async def test_invalid_advisory_fails_open_without_blocking_deliberation() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    model = _InvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(), main_model=model, quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
        advisory_compiler=AdvisoryCompiler(
            catalog=default_matrix_catalog(),
            adapters=(_InvalidAdvice(),),
            authority_key=b"pinned-turn-advisory-test-authority-key",
        ),
    )

    await WorldRuntime(world_id=WORLD, ledger=ledger, pinned_turn=turn).ingest(_observation())

    assert model.request is not None
    assert '"advisories":{"availability":"unavailable"' in model.request.model_content_json
    assert '"disappointment"' not in model.request.model_content_json


@pytest.mark.asyncio
async def test_pinned_turn_rejects_observation_not_equal_to_committed_event_payload() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    runtime = WorldRuntime(world_id=WORLD, ledger=ledger)
    observation = _observation()
    await runtime.ingest(observation)
    event_id = "event:trigger:observation:test:message:pinned-turn:1"
    stored = ledger.lookup_event_commit(event_id)
    assert stored is not None
    event, commit = stored
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(), main_model=_InvalidModel(), quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
    )

    with pytest.raises(ValueError, match="does not match its committed authority"):
        await turn.audit_observation(
            observation=observation.model_copy(update={"payload_ref": "forged:payload"}),
            observation_event=event,
            cursor=ProjectionCursor(
                world_revision=commit.world_revision,
                deliberation_revision=commit.deliberation_revision,
                ledger_sequence=commit.ledger_sequence,
            ),
        )
