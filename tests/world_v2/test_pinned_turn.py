from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from companion_daemon.world_v2.appraisal_proposal_compiler import AppraisalProposalCompiler
from companion_daemon.world_v2.appraisal_proposal_worker import AppraisalProposalWorker
from companion_daemon.world_v2.affect_acceptance_runtime import AffectAcceptanceRuntime
from companion_daemon.world_v2.affect_deliberation_worker import AffectDeliberationWorker
from companion_daemon.world_v2.affect_proposal_compiler import AffectProposalCompiler
from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.advisory_compiler import AdvisoryAdapterInput, AdvisoryCompiler
from companion_daemon.world_v2.deliberation import (
    Deliberation,
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
)
from companion_daemon.world_v2.expression_episode_lifecycle import (
    expression_episode_claim_event,
    expression_episode_complete_event,
    expression_episode_open_event,
    next_expression_retry_due,
)
from companion_daemon.world_v2.errors import IdempotencyConflict, LedgerIntegrityError
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.batch_invariants import interaction_appraisal_trigger_identity
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.minimal_reply_acceptance import ReplyBudgetPolicy
from companion_daemon.world_v2.minimal_reply_atomic_recorder import MinimalReplyAtomicRecorder
from companion_daemon.world_v2.perception_trigger import perception_trigger_event
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    MinimalProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    Observation,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
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


class _BlockingInvalidModel(_InvalidModel):
    """Hold the provider result while an unrelated durable event advances head."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.request = request
        self.started.set()
        await self.release.wait()
        return ModelOutput(
            model_id="test-main",
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


class _DecisionAppraisalModel:
    def __init__(self, *, evidence_hash: str) -> None:
        self._evidence_hash = evidence_hash
        self.request: ModelInput | None = None

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.request = request
        proposal = DecisionProposal(
            proposal_id="proposal:pinned-turn:appraisal:1",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(
                ProposalEvidenceRef(
                    ref_id=_observation().observation_id,
                    evidence_kind="observed_message",
                    source_world_revision=2,
                    immutable_hash="sha256:" + self._evidence_hash,
                ),
            ),
            proposed_changes=(
                TypedChange(
                    change_id="change:pinned-turn:appraisal:1",
                    kind="appraisal_transition",
                    target_id="appraisal:model-hint",
                    transition="activate",
                    expected_entity_revision=0,
                    evidence_refs=(_observation().observation_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="appraisal_transition.v1",
                        value={
                            "appraisal_id": "appraisal:model-hint",
                            "meaning_candidates": [
                                {"meaning": "disappointment", "confidence": 7000},
                                {"meaning": "misunderstanding", "confidence": 3000},
                            ],
                            "attribution": "user",
                            "severity": 6000,
                            "confidence": 7600,
                            "expiry": None,
                        },
                    ),
                ),
            ),
            action_intents=(),
            confidence=7600,
            brief_rationale="Persist a fallible interpretation for later affect deliberation.",
            behavior_tendency="hold_space",
            stance="attend",
            display_strategy="withhold",
        )
        return ModelOutput(
            model_id="test-decision-main",
            model_version="test.1",
            raw_proposal=proposal.model_dump(mode="json"),
            input_tokens=1,
            output_tokens=1,
        )


class _AffectDecisionModel:
    def __init__(
        self,
        *,
        appraisal_change_id: str,
        evidence_ref: str,
        evidence_hash: str,
        evidence_revision: int,
    ) -> None:
        self._appraisal_change_id = appraisal_change_id
        self._evidence_ref = evidence_ref
        self._evidence_hash = evidence_hash
        self._evidence_revision = evidence_revision
        self.request: ModelInput | None = None
        self.calls = 0

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.calls += 1
        self.request = request
        proposal = DecisionProposal(
            proposal_id="proposal:pinned-turn:affect:1",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(
                ProposalEvidenceRef(
                    ref_id=self._evidence_ref,
                    evidence_kind="committed_world_event",
                    source_world_revision=self._evidence_revision,
                    immutable_hash="sha256:" + self._evidence_hash,
                ),
            ),
            proposed_changes=(
                TypedChange(
                    change_id="change:pinned-turn:affect:1",
                    kind="affect_transition",
                    target_id="affect:model-hint",
                    transition="open",
                    expected_entity_revision=0,
                    evidence_refs=(self._evidence_ref,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="affect_transition.v1",
                        value={
                            "episode_id": "affect:model-hint",
                            "appraisal_change_refs": [self._appraisal_change_id],
                            "component_deltas": [{"name": "hurt", "value": 4200}],
                            "decay_config": {
                                "object_ref": "policy:decay:standard",
                                "schema_version": "affect-decay.1",
                                "payload_hash": "sha256:" + "a" * 64,
                            },
                            "residue_config": {
                                "object_ref": "policy:residue:standard",
                                "schema_version": "affect-residue.1",
                                "payload_hash": "sha256:" + "b" * 64,
                            },
                        },
                    ),
                ),
            ),
            action_intents=(),
            confidence=7300,
            brief_rationale="The appraisal may leave a bounded residual hurt episode.",
            affect_decision="propose",
            behavior_tendency="hold_space",
            stance="care_despite_hurt",
            display_strategy="partial_disclosure",
        )
        return ModelOutput(
            model_id="test-affect-main",
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
                catalog_version="world-v2-matrix-2",
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


def _observation_event(observation: Observation) -> WorldEvent:
    payload = observation.model_dump(mode="json")
    return WorldEvent.from_payload(
        schema_version=observation.schema_version,
        event_id=f"event:trigger:observation:{observation.source}:{observation.source_event_id}",
        world_id=observation.world_id,
        event_type="ObservationRecorded",
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded",
            world_id=observation.world_id,
            payload=payload,
        )
        or f"observation:{observation.source}:{observation.source_event_id}",
        payload=payload,
    )


def _expression_episode_events(
    observation: Observation,
    observation_event: WorldEvent,
) -> tuple[WorldEvent, WorldEvent, TriggerProcess]:
    opened_event = expression_episode_open_event(
        observation=observation,
        observation_event=observation_event,
    )
    opened_process = TriggerProcess.model_validate_json(
        json.dumps(opened_event.payload()["process"])
    )
    claimed_event, claimed_process = expression_episode_claim_event(
        world_id=observation.world_id,
        process=opened_process,
        owner_id="worker:test:expression-episode",
        at=observation.logical_time,
        trace_id=observation.trace_id,
        correlation_id=observation.correlation_id,
        technical_failure_count=0,
    )
    return opened_event, claimed_event, claimed_process


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
    expression_model = _InvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=capsules,
        deliberation=Deliberation(
            router=_Router(), main_model=expression_model, quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
    )
    runtime = WorldRuntime(world_id=WORLD, ledger=ledger, pinned_turn=turn)

    first = await runtime.ingest(_observation())
    duplicate = await runtime.ingest(_observation())

    assert first == duplicate
    projection = ledger.project()
    assert projection.world_revision == 2
    # Two model-result audits plus the open/claim lifecycle that makes the
    # technical failure restart-recoverable.
    assert projection.deliberation_revision == 4
    assert len(projection.model_result_audits) == 2
    assert projection.proposal_audits == ()
    assert expression_model.request is not None
    assert expression_model.request.affect_target_bounds is None
    assert "affect_target_bounds" not in expression_model.request.model_dump(mode="json")
    retry = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "expression_episode"
    )
    assert retry.state == "claimed"


@pytest.mark.asyncio
async def test_content_free_technical_result_rebases_after_unrelated_head_advance(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Receipt-like head progress must not turn a typed model failure into lost speech.

    A content-bearing Proposal remains cursor-pinned.  This regression covers
    only a proposal-free technical result whose exact expression attempt is
    still claimed: recording that audit at the latest head preserves durable
    retry semantics without granting stale text any authority.
    """

    ledger = SQLiteWorldLedger(
        path=tmp_path / "content-free-technical-result-rebase.sqlite",
        world_id=WORLD,
    )
    request.addfinalizer(ledger.close)
    ledger.commit(
        (_world_started(),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    observation = _observation()
    observation_event = _observation_event(observation)
    opened_event, claimed_event, claimed = _expression_episode_events(
        observation,
        observation_event,
    )
    committed = ledger.commit(
        (observation_event, opened_event, claimed_event),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    cursor = ProjectionCursor(
        world_revision=committed.world_revision,
        deliberation_revision=committed.deliberation_revision,
        ledger_sequence=committed.ledger_sequence,
    )
    model = _BlockingInvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(),
            main_model=model,
            quick_recovery=_InvalidQuick(),
        ),
        companion_actor_ref="agent:companion",
    )

    task = asyncio.create_task(
        turn.audit_observation(
            observation=observation,
            observation_event=observation_event,
            cursor=cursor,
            expression_attempt_id=claimed.claim_lease.attempt_id,
        )
    )
    await model.started.wait()
    head = ledger.project()
    ledger.commit(
        (
            perception_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    advanced_head = ledger.project()
    assert advanced_head.deliberation_revision > cursor.deliberation_revision
    assert advanced_head.ledger_sequence > cursor.ledger_sequence
    model.release.set()

    audited = await task

    assert audited.proposal_id is None
    projection = ledger.project()
    assert len(projection.model_result_audits) == 2
    assert {
        item.attempt_id for item in projection.model_result_audits
    } == {claimed.claim_lease.attempt_id}
    assert {
        item.evaluated_world_revision for item in projection.model_result_audits
    } == {cursor.world_revision}
    assert projection.proposal_audits == ()
    assert projection.actions == ()
    process = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "expression_episode"
    )
    assert process.state == "claimed"
    assert process.claim_lease is not None
    assert process.claim_lease.attempt_id == claimed.claim_lease.attempt_id
    assert next_expression_retry_due(projection) == NOW + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_concurrent_content_free_rebases_join_one_exact_audit(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    ledger = SQLiteWorldLedger(
        path=tmp_path / "content-free-technical-result-effect-once.sqlite",
        world_id=WORLD,
    )
    request.addfinalizer(ledger.close)
    ledger.commit(
        (_world_started(),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    observation = _observation()
    observation_event = _observation_event(observation)
    opened_event, claimed_event, claimed = _expression_episode_events(
        observation,
        observation_event,
    )
    committed = ledger.commit(
        (observation_event, opened_event, claimed_event),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    cursor = ProjectionCursor(
        world_revision=committed.world_revision,
        deliberation_revision=committed.deliberation_revision,
        ledger_sequence=committed.ledger_sequence,
    )
    first_model = _BlockingInvalidModel()
    second_model = _BlockingInvalidModel()

    def compiler(model: _BlockingInvalidModel) -> PinnedTurnCompiler:
        return PinnedTurnCompiler(
            ledger=ledger,
            capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
            deliberation=Deliberation(
                router=_Router(),
                main_model=model,
                quick_recovery=_InvalidQuick(),
            ),
            companion_actor_ref="agent:companion",
        )

    first = asyncio.create_task(
        compiler(first_model).audit_observation(
            observation=observation,
            observation_event=observation_event,
            cursor=cursor,
            expression_attempt_id=claimed.claim_lease.attempt_id,
        )
    )
    second = asyncio.create_task(
        compiler(second_model).audit_observation(
            observation=observation,
            observation_event=observation_event,
            cursor=cursor,
            expression_attempt_id=claimed.claim_lease.attempt_id,
        )
    )
    await asyncio.gather(first_model.started.wait(), second_model.started.wait())
    head = ledger.project()
    ledger.commit(
        (
            perception_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    first_model.release.set()
    second_model.release.set()

    first_audit, second_audit = await asyncio.gather(first, second)

    assert first_audit == second_audit
    projection = ledger.project()
    assert len(projection.model_result_audits) == 2
    assert projection.proposal_audits == ()
    assert projection.actions == ()


@pytest.mark.asyncio
async def test_content_free_idempotency_conflict_fails_closed_without_rebase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An identity collision is corruption evidence, never a cursor race."""

    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        (_world_started(),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    observation = _observation()
    observation_event = _observation_event(observation)
    opened_event, claimed_event, claimed = _expression_episode_events(
        observation,
        observation_event,
    )
    committed = ledger.commit(
        (observation_event, opened_event, claimed_event),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(),
            main_model=_InvalidModel(),
            quick_recovery=_InvalidQuick(),
        ),
        companion_actor_ref="agent:companion",
    )
    record_calls = 0

    async def conflicting_record(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal record_calls
        record_calls += 1
        raise IdempotencyConflict("same identity has different persisted content")

    monkeypatch.setattr(turn, "_record", conflicting_record)

    with pytest.raises(IdempotencyConflict, match="different persisted content"):
        await turn.audit_observation(
            observation=observation,
            observation_event=observation_event,
            cursor=ProjectionCursor(
                world_revision=committed.world_revision,
                deliberation_revision=committed.deliberation_revision,
                ledger_sequence=committed.ledger_sequence,
            ),
            expression_attempt_id=claimed.claim_lease.attempt_id,
        )

    assert record_calls == 1
    assert ledger.project().model_result_audits == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "message"),
    (
        (LedgerIntegrityError, "prefix proof mismatch"),
        (ValueError, "capsule contract mismatch"),
        (TypeError, "resolver returned the wrong contract type"),
    ),
)
async def test_snapshot_hard_failure_is_not_recorded_as_model_failure(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    message: str,
) -> None:
    """Integrity and programmer-contract failures must surface unchanged."""

    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        (_world_started(),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    observation = _observation()
    observation_event = _observation_event(observation)
    opened_event, claimed_event, claimed = _expression_episode_events(
        observation,
        observation_event,
    )
    committed = ledger.commit(
        (observation_event, opened_event, claimed_event),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    model = _InvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(),
            main_model=model,
            quick_recovery=_InvalidQuick(),
        ),
        companion_actor_ref="agent:companion",
    )

    async def corrupt_snapshot(_cursor):  # type: ignore[no-untyped-def]
        raise error_type(message)

    monkeypatch.setattr(turn, "_project_at", corrupt_snapshot)

    with pytest.raises(error_type, match=message):
        await turn.audit_observation(
            observation=observation,
            observation_event=observation_event,
            cursor=ProjectionCursor(
                world_revision=committed.world_revision,
                deliberation_revision=committed.deliberation_revision,
                ledger_sequence=committed.ledger_sequence,
            ),
            expression_attempt_id=claimed.claim_lease.attempt_id,
        )

    assert model.request is None
    assert ledger.project().model_result_audits == ()


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
    assert projection.acceptance_decisions[-1].status == "rejected"
    episode = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "expression_episode"
    )
    assert episode.state == "claimed"
    assert next_expression_retry_due(projection) == NOW + timedelta(minutes=10)


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
async def test_runtime_materializes_audited_appraisal_without_a_second_model_call() -> None:
    observation = _observation()
    observation_event = WorldEvent.from_payload(
        schema_version=observation.schema_version,
        event_id=(
            f"event:trigger:observation:{observation.source}:{observation.source_event_id}"
        ),
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=f"observation:{observation.source}:{observation.source_event_id}",
        payload=observation.model_dump(mode="json"),
    )
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    appraisal_model = _DecisionAppraisalModel(evidence_hash=observation_event.payload_hash)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(),
            main_model=appraisal_model,
            quick_recovery=_InvalidQuick(),
        ),
        companion_actor_ref="agent:companion",
        affect_target_bounds_enabled=True,
    )
    acceptance = AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        pinned_turn=turn,
        interaction_appraisal_owner="worker:interaction-appraisal",
        appraisal_acceptance=acceptance,
        appraisal_acceptance_actor="worker:interaction-appraisal",
        affect_deliberation_owner="worker:affect",
        appraisal_worker=AppraisalProposalWorker(
            compiler=AppraisalProposalCompiler(ledger=ledger),
            acceptance=acceptance,
            actor="worker:interaction-appraisal",
        ),
    )

    outcome = await runtime.ingest(observation)

    assert outcome.status == "observed_only", outcome
    projection = ledger.project()
    assert projection.appraisals[0].hypotheses[0].meaning == "disappointment"
    assert appraisal_model.request is not None
    appraisal_bounds = appraisal_model.request.affect_target_bounds
    assert appraisal_bounds is not None
    assert appraisal_bounds.source_world_revision == (
        appraisal_model.request.evaluated_world_revision
    )
    assert appraisal_bounds.source_ledger_sequence == (
        appraisal_model.request.evaluated_ledger_sequence
    )
    assert appraisal_bounds.minimum_for("hurt") == 500
    interaction = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "interaction_appraisal"
    )
    affect = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "affect_deliberation"
    )
    assert interaction.state == "terminal"
    assert affect.state == "claimed"

    appraisal_event_ref = next(
        ref.event_id
        for ref in projection.committed_world_event_refs
        if ref.event_type == "AppraisalAccepted"
    )
    stored = ledger.lookup_event_commit(appraisal_event_ref)
    assert stored is not None
    appraisal_event, appraisal_commit = stored
    # Claiming the source-bound affect trigger is a separate durable commit.
    # The affect turn must still accept the earlier Appraisal as immutable
    # evidence at this later pinned cursor.
    assert appraisal_commit.ledger_sequence < projection.ledger_sequence
    affect_model = _AffectDecisionModel(
        appraisal_change_id=projection.appraisals[0].origin.change_id,
        evidence_ref=appraisal_event.event_id,
        evidence_hash=appraisal_event.payload_hash,
        evidence_revision=appraisal_commit.world_revision,
    )
    affect_turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(
            ledger=ledger,
            relevance_scope=ContextRelevanceScope(
                actor_ref="agent:companion", related_subject_refs=("user:primary",)
            ),
        ),
        deliberation=Deliberation(
            router=_Router(),
            main_model=affect_model,
            quick_recovery=_InvalidQuick(),
        ),
        companion_actor_ref="agent:companion",
        affect_target_bounds_enabled=True,
    )
    affect_acceptance = AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    affect_worker = AffectDeliberationWorker(
        ledger=ledger,
        pinned_turn=affect_turn,
        compiler=AffectProposalCompiler(ledger=ledger),
        acceptance=affect_acceptance,
        actor="worker:affect",
    )
    # First leave only the expensive generic audit durable, then resume through
    # the worker.  It must compile that audit rather than call the model again.
    audited = await affect_turn.audit_appraisal_accepted(
        appraisal_event=appraisal_event,
        cursor=ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        ),
    )
    assert audited.proposal_id is not None
    assert affect_model.request is not None
    affect_bounds = affect_model.request.affect_target_bounds
    assert affect_bounds is not None
    assert (
        affect_bounds.source_world_revision,
        affect_bounds.source_deliberation_revision,
        affect_bounds.source_ledger_sequence,
    ) == (
        projection.world_revision,
        projection.deliberation_revision,
        projection.ledger_sequence,
    )
    assert affect_bounds.minimum_for("hurt") == 500
    after_audit = ledger.project()
    # Simulate a process death after the acceptance batch and before trigger
    # completion. The recovery runtime must detect that durable effect and
    # finish the same trigger without another model call.
    affect_result = await affect_worker.process(
        world_id=WORLD,
        cursor=ProjectionCursor(
            world_revision=after_audit.world_revision,
            deliberation_revision=after_audit.deliberation_revision,
            ledger_sequence=after_audit.ledger_sequence,
        ),
        appraisal_event=appraisal_event,
    )
    assert affect_result.status == "accepted"
    assert next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "affect_deliberation"
    ).state == "claimed"
    affect_runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        affect_deliberation_owner="worker:affect",
        affect_worker=affect_worker,
    )
    recovered = await affect_runtime.drain_background_once()
    assert recovered is not None
    assert recovered.status == "completed_existing"
    assert recovered.work_status == "accepted"
    after_affect = ledger.project()
    assert after_affect.affect_episodes[0].components[0].dimension == "hurt"
    assert after_affect.trigger_processes[1].state == "terminal"
    assert affect_model.calls == 1

    # A restart sees the terminal durable outcome and performs no second turn.
    idle = await affect_runtime.drain_background_once()
    assert idle is not None and idle.status == "idle"


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
    assert projection.deliberation_revision == 4


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


@pytest.mark.asyncio
async def test_pinned_turn_rejects_expression_attempt_claimed_for_another_observation() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    first = _observation()
    first_event = _observation_event(first)
    first_opened, first_claimed, _ = _expression_episode_events(first, first_event)
    first_commit = ledger.commit(
        (first_event, first_opened, first_claimed),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    second = first.model_copy(
        update={
            "observation_id": "observation:pinned-turn:2",
            "source_event_id": "message:pinned-turn:2",
            "payload_ref": "payload:pinned-turn:2",
            "payload_hash": "sha256:" + "b" * 64,
            "text": "第二条消息。",
        }
    )
    second_event = _observation_event(second)
    second_opened, second_claimed, second_process = _expression_episode_events(
        second,
        second_event,
    )
    latest = ledger.commit(
        (second_event, second_opened, second_claimed),
        expected_world_revision=first_commit.world_revision,
        expected_deliberation_revision=first_commit.deliberation_revision,
    )
    model = _InvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(),
            main_model=model,
            quick_recovery=_InvalidQuick(),
        ),
        companion_actor_ref="agent:companion",
    )

    assert second_process.claim_lease is not None
    with pytest.raises(
        ValueError,
        match="current expression episode claim",
    ):
        await turn.audit_observation(
            observation=first,
            observation_event=first_event,
            cursor=ProjectionCursor(
                world_revision=latest.world_revision,
                deliberation_revision=latest.deliberation_revision,
                ledger_sequence=latest.ledger_sequence,
            ),
            expression_attempt_id=second_process.claim_lease.attempt_id,
        )

    assert model.request is None


@pytest.mark.asyncio
async def test_pinned_turn_rejects_a_terminal_expression_attempt() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit((_world_started(),), expected_world_revision=0, expected_deliberation_revision=0)
    observation = _observation()
    observation_event = _observation_event(observation)
    opened_event, claimed_event, claimed_process = _expression_episode_events(
        observation,
        observation_event,
    )
    claimed_commit = ledger.commit(
        (observation_event, opened_event, claimed_event),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    completed_event = expression_episode_complete_event(
        world_id=WORLD,
        process=claimed_process,
        at=observation.logical_time,
        trace_id=observation.trace_id,
        correlation_id=observation.correlation_id,
        outcome_ref="runtime-outcome:pinned-turn:terminal",
    )
    latest = ledger.commit(
        (completed_event,),
        expected_world_revision=claimed_commit.world_revision,
        expected_deliberation_revision=claimed_commit.deliberation_revision,
    )
    model = _InvalidModel()
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(ledger=ledger),
        deliberation=Deliberation(
            router=_Router(),
            main_model=model,
            quick_recovery=_InvalidQuick(),
        ),
        companion_actor_ref="agent:companion",
    )

    assert claimed_process.claim_lease is not None
    with pytest.raises(ValueError, match="current expression episode claim"):
        await turn.audit_observation(
            observation=observation,
            observation_event=observation_event,
            cursor=ProjectionCursor(
                world_revision=latest.world_revision,
                deliberation_revision=latest.deliberation_revision,
                ledger_sequence=latest.ledger_sequence,
            ),
            expression_attempt_id=claimed_process.claim_lease.attempt_id,
        )

    assert model.request is None
