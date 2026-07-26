from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from companion_daemon.world_v2.context_capsule import (
    ContextCapsuleCompiler,
    InnerAdvisoryProjection,
    _compile_resolved_context,
)
from companion_daemon.world_v2.context_resolver import (
    ContextCompileQuery,
    ResolvedContextResult,
    TrustedInternalContextResolver,
    context_query_hash,
)
from companion_daemon.world_v2.deliberation import (
    Deliberation,
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
    TriggerMessage,
    claim_secondary_provider_slot,
)
from companion_daemon.world_v2.interactive_turn_budget import (
    InteractiveTurnBudgetPolicy,
)
from companion_daemon.world_v2.proposal_envelope import MinimalProposal, ProposalEvidenceRef
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallSourceBinding,
)
from companion_daemon.world_v2.recall_runtime import (
    CharacterRecallRequest,
    RecallCoordinator,
)
from test_context_capsule import HASH_B, NOW, _bound, _request
from test_proposal_envelope import (
    _decision,
    _evidence,
    _minimal_expression_change,
    _minimal_reply_intent,
)


class _Resolver(TrustedInternalContextResolver):
    def __init__(self, resolved) -> None:
        super().__init__()
        self._resolved = resolved

    def resolve(self, query: ContextCompileQuery) -> ResolvedContextResult:
        return ResolvedContextResult(
            query_hash=context_query_hash(query),
            capability=self.capability,
            resolved_context=self._resolved,
        )


def _capsule():
    advisory = InnerAdvisoryProjection(
        advisory_id="advisory:message:1",
        kind="user_message_signal",
        source_refs=("event:source:1",),
        candidate_refs=("candidate:reply:1",),
        confidence_bp=8000,
        expiry=NOW + timedelta(minutes=5),
        producer_version="test.1",
    )
    request = _request(
        advisories=_bound((advisory,), source_ref="event:source:1", slice_name="advisories")
    )
    query = ContextCompileQuery(
        world_id=request.world_id,
        snapshot_id=request.snapshot_id,
        snapshot_hash=request.snapshot_hash,
        actor_ref=request.actor_ref,
        consumer_scope=request.consumer_scope,
        trigger_ref=request.trigger_ref,
        world_revision=request.world_revision,
        deliberation_revision=request.deliberation_revision,
        ledger_sequence=request.ledger_sequence,
        logical_time=request.logical_time,
    )
    return ContextCapsuleCompiler(resolver=_Resolver(request)).compile_for_deliberation(query)


def _authority_evidence(ref: str = "event:source:1"):
    return _evidence(ref).model_copy(update={"immutable_hash": f"sha256:{HASH_B}"})


def _decision_raw(*, evidence_ref: str = "event:source:1") -> dict[str, object]:
    proposal = _decision()
    change = proposal.proposed_changes[0].model_copy(update={"evidence_refs": (evidence_ref,)})
    return proposal.model_copy(
        update={
            "trigger_ref": "event:observation:1",
            "evidence_refs": (_authority_evidence(evidence_ref),),
            "proposed_changes": (change,),
        }
    ).model_dump(mode="python")


def _minimal_raw(
    *,
    trigger_ref: str = "event:observation:1",
    text: str = "I saw that; give me a moment.",
) -> dict[str, object]:
    change = _minimal_expression_change(text).model_copy(
        update={"evidence_refs": ("event:source:1",)}
    )
    return MinimalProposal(
        proposal_id="proposal:minimal:deliberation",
        trigger_ref=trigger_ref,
        evaluated_world_revision=7,
        schema_registry_version="world-v2-proposals.1",
        evidence_refs=(_authority_evidence(),),
        proposed_changes=(change,),
        action_intents=(_minimal_reply_intent(text),),
        confidence=4000,
        brief_rationale="Bounded recovery acknowledges without adding world claims.",
        source_model_result="model-result:recovery:1",
        response_text=text,
        stance="defer",
        fact_claims=(),
    ).model_dump(mode="python")


class _Router:
    def __init__(self, value: object | None = None, *, fail: bool = False) -> None:
        self.value = value or ModelRoute(
            tier="flash", reason_code="ordinary_turn", router_version="router.1"
        )
        self.fail = fail
        self.requests: list[RouteRequest] = []

    async def route(self, request: RouteRequest) -> ModelRoute:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("router unavailable")
        return self.value  # type: ignore[return-value]


class _Main:
    def __init__(
        self,
        raw: object | None = None,
        *,
        fail: bool = False,
        delay: float = 0,
    ) -> None:
        self.raw = _decision_raw() if raw is None else raw
        self.fail = fail
        self.delay = delay
        self.requests: list[ModelInput] = []

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider failed")
        if isinstance(self.raw, ModelOutput):
            return self.raw
        return ModelOutput(
            model_id="main",
            model_version="v1",
            raw_proposal=self.raw,  # type: ignore[arg-type]
        )


class _Quick:
    def __init__(self, raw: object | None = None, *, fail: bool = False) -> None:
        self.raw = _minimal_raw() if raw is None else raw
        self.fail = fail
        self.failure_codes: list[str] = []
        self.requests: list[ModelInput] = []

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        self.requests.append(request)
        self.failure_codes.append(failure_code)
        if self.fail:
            raise RuntimeError("quick provider failed")
        return ModelOutput(
            model_id="quick",
            model_version="v1",
            raw_proposal=self.raw,  # type: ignore[arg-type]
        )


class _EpisodeMain(_Main):
    def __init__(self, *, full_delay: float = 0, provisional_delay: float = 0) -> None:
        super().__init__(delay=full_delay)
        self.provisional_delay = provisional_delay
        self.provisional_requests: list[ModelInput] = []

    async def propose_provisional(self, request: ModelInput) -> ModelOutput:
        self.provisional_requests.append(request)
        if self.provisional_delay:
            await asyncio.sleep(self.provisional_delay)
        raw = _decision_raw()
        raw["proposal_id"] = "proposal:episode:provisional"
        return ModelOutput(
            model_id="provisional",
            model_version="v1",
            raw_proposal=raw,
        )


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        target = self.now + seconds
        if target <= self.now:
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append((target, waiter))
        await waiter

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for target, waiter in tuple(self._waiters):
            if target <= self.now and not waiter.done():
                waiter.set_result(None)
        self._waiters = [
            (target, waiter) for target, waiter in self._waiters if not waiter.done()
        ]
        await asyncio.sleep(0)


class _ControlledMain(_Main):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.result: asyncio.Future[ModelOutput] | None = None

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.requests.append(request)
        self.result = asyncio.get_running_loop().create_future()
        self.started.set()
        try:
            return await self.result
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _ControlledQuick(_Quick):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.result: asyncio.Future[ModelOutput] | None = None

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        self.requests.append(request)
        self.failure_codes.append(failure_code)
        self.result = asyncio.get_running_loop().create_future()
        self.started.set()
        return await self.result


class _HedgeThenLocalQuick(_Quick):
    """Models production: remote hedge hangs, local reserve failsafe is sync."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.result: asyncio.Future[ModelOutput] | None = None

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        self.requests.append(request)
        self.failure_codes.append(failure_code)
        if len(self.requests) == 1:
            self.result = asyncio.get_running_loop().create_future()
            self.started.set()
            return await self.result
        return ModelOutput(
            model_id="local-expression-failsafe",
            model_version="local-expression-failsafe.1",
            raw_proposal=_minimal_raw(text="刚才我没接好，先回你一声。"),
        )


@pytest.mark.asyncio
async def test_first_valid_hedge_waits_for_validation_and_cancels_slow_primary() -> None:
    clock = _ManualClock()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=5.5,
        hedge_after_seconds=1.5,
        acceptance_dispatch_reserve_seconds=1.2,
        clock=clock,
        sleep=clock.sleep,
    ).start()
    primary = _ControlledMain()
    backup = _ControlledQuick()
    unit = Deliberation(router=_Router(), main_model=primary, quick_recovery=backup)

    running = asyncio.create_task(
        unit.deliberate(_capsule(), attempt_id="attempt:first-valid", budget=budget)
    )
    await primary.started.wait()
    await clock.advance(1.49)
    assert not backup.started.is_set()
    await clock.advance(0.01)
    await backup.started.wait()
    assert backup.result is not None
    backup.result.set_result(
        ModelOutput(model_id="backup", model_version="v1", raw_proposal=_minimal_raw())
    )

    result = await running

    assert result.proposal is not None
    assert result.audit.model_id == "backup"
    assert primary.cancelled.is_set()
    assert len(primary.requests) == len(backup.requests) == 1


@pytest.mark.asyncio
async def test_first_completed_invalid_does_not_beat_later_valid_candidate() -> None:
    clock = _ManualClock()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=5.5,
        hedge_after_seconds=1.5,
        acceptance_dispatch_reserve_seconds=1.2,
        clock=clock,
        sleep=clock.sleep,
    ).start()
    primary = _ControlledMain()
    backup = _ControlledQuick()
    running = asyncio.create_task(
        Deliberation(router=_Router(), main_model=primary, quick_recovery=backup).deliberate(
            _capsule(), attempt_id="attempt:invalid-first", budget=budget
        )
    )
    await primary.started.wait()
    assert primary.result is not None
    primary.result.set_result(
        ModelOutput(model_id="invalid", model_version="v1", raw_proposal={"bad": True})
    )
    await backup.started.wait()
    assert backup.result is not None
    backup.result.set_result(
        ModelOutput(model_id="backup", model_version="v1", raw_proposal=_minimal_raw())
    )

    result = await running

    assert result.proposal is not None
    assert result.audit.model_id == "backup"
    assert len(result.attempt_audits) == 2


@pytest.mark.asyncio
async def test_primary_valid_before_hedge_never_calls_backup() -> None:
    clock = _ManualClock()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=5.5,
        hedge_after_seconds=1.5,
        acceptance_dispatch_reserve_seconds=1.2,
        clock=clock,
        sleep=clock.sleep,
    ).start()
    primary = _ControlledMain()
    backup = _ControlledQuick()
    running = asyncio.create_task(
        Deliberation(router=_Router(), main_model=primary, quick_recovery=backup).deliberate(
            _capsule(), attempt_id="attempt:primary-fast", budget=budget
        )
    )
    await primary.started.wait()
    assert primary.result is not None
    primary.result.set_result(
        ModelOutput(model_id="primary", model_version="v1", raw_proposal=_decision_raw())
    )

    result = await running

    assert result.audit.model_id == "primary"
    assert result.audit.slot == "primary"
    assert result.audit.outcome == "winner"
    assert backup.requests == []


@pytest.mark.asyncio
async def test_corrective_claims_second_slot_and_prevents_hedge_or_third_call() -> None:
    class CorrectingMain(_Main):
        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            assert claim_secondary_provider_slot("corrective")
            assert not claim_secondary_provider_slot("backup")
            return ModelOutput(
                model_id="corrected",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    clock = _ManualClock()
    backup = _ControlledQuick()
    result = await Deliberation(
        router=_Router(),
        main_model=CorrectingMain(),
        quick_recovery=backup,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:corrective-second-slot",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=5.5,
            hedge_after_seconds=1.5,
            acceptance_dispatch_reserve_seconds=1.2,
            clock=clock,
            sleep=clock.sleep,
        ).start(),
    )

    assert result.audit.model_id == "corrected"
    assert result.audit.slot == "corrective"
    assert result.audit.outcome == "winner"
    assert backup.requests == []


@pytest.mark.asyncio
async def test_two_slow_slots_record_technical_failure_without_a_third_role_call() -> None:
    """Primary plus one recovery exhaust the bounded role-call allowance."""

    clock = _ManualClock()
    primary = _ControlledMain()
    backup = _HedgeThenLocalQuick()
    running = asyncio.create_task(
        Deliberation(router=_Router(), main_model=primary, quick_recovery=backup).deliberate(
            _capsule(),
            attempt_id="attempt:budget-exhausted-failsafe",
            budget=InteractiveTurnBudgetPolicy(
                total_seconds=5.5,
                hedge_after_seconds=1.5,
                acceptance_dispatch_reserve_seconds=1.2,
                clock=clock,
                sleep=clock.sleep,
            ).start(),
        )
    )
    await primary.started.wait()
    await clock.advance(1.5)
    await backup.started.wait()
    await clock.advance(2.8)

    result = await running

    assert clock.now == pytest.approx(4.3)
    assert result.proposal is None
    assert result.audit.status == "recovery_failed"
    assert result.audit.failure_code == "backup_timeout"
    assert result.audit.outcome == "budget_exhausted"
    assert result.attempt_audits[0].outcome == "budget_exhausted"
    assert result.attempt_audits[0].failure_code == "primary_timeout"
    assert len(primary.requests) == 1
    assert len(backup.requests) == 1
    assert backup.failure_codes == ["main_timeout"]


@pytest.mark.asyncio
async def test_dual_invalid_after_full_budget_stops_after_one_corrective_call() -> None:
    """A failed bounded correction becomes technical failure, never local prose."""

    clock = _ManualClock()

    class _InvalidThenExhaustLocal(_Quick):
        async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
            self.requests.append(request)
            self.failure_codes.append(failure_code)
            if len(self.requests) == 1:
                # Remote hedge spent the whole absolute budget before returning
                # an unusable draft — the production race that skipped failsafe.
                clock.now = 5.5
                return ModelOutput(
                    model_id="invalid-backup",
                    model_version="v1",
                    raw_proposal={"bad": True},
                )
            return ModelOutput(
                model_id="local-expression-failsafe",
                model_version="local-expression-failsafe.1",
                raw_proposal=_minimal_raw(text="刚才我没接好，先回你一声。"),
            )

    result = await Deliberation(
        router=_Router(),
        main_model=_Main({"bad": True}),
        quick_recovery=_InvalidThenExhaustLocal(),
    ).deliberate(
        _capsule(),
        attempt_id="attempt:dual-invalid-past-deadline",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=5.5,
            hedge_after_seconds=1.5,
            acceptance_dispatch_reserve_seconds=1.2,
            clock=clock,
            sleep=clock.sleep,
        ).start(),
    )

    assert result.proposal is None
    assert result.audit.status == "recovery_failed"
    assert result.audit.failure_code == "backup_invalid"
    assert result.attempt_audits[0].failure_code == "primary_invalid"
    assert result.attempt_audits[0].outcome == "budget_exhausted"
    assert result.attempt_audits[1].outcome == "budget_exhausted"
    assert len(result.attempt_audits) == 2


@pytest.mark.asyncio
async def test_backup_timeout_is_durable_when_local_failsafe_also_fails() -> None:
    clock = _ManualClock()
    primary = _ControlledMain()

    class _HangThenFailQuick(_Quick):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.result: asyncio.Future[ModelOutput] | None = None

        async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
            self.requests.append(request)
            self.failure_codes.append(failure_code)
            if len(self.requests) == 1:
                self.result = asyncio.get_running_loop().create_future()
                self.started.set()
                return await self.result
            raise RuntimeError("local failsafe unavailable")

    backup = _HangThenFailQuick()
    running = asyncio.create_task(
        Deliberation(router=_Router(), main_model=primary, quick_recovery=backup).deliberate(
            _capsule(),
            attempt_id="attempt:backup-timeout-code",
            budget=InteractiveTurnBudgetPolicy(
                total_seconds=5.5,
                hedge_after_seconds=1.5,
                acceptance_dispatch_reserve_seconds=1.2,
                clock=clock,
                sleep=clock.sleep,
            ).start(),
        )
    )
    await primary.started.wait()
    await clock.advance(1.5)
    await backup.started.wait()
    await clock.advance(4.0)
    result = await running
    assert result.proposal is None
    assert result.audit.failure_code == "backup_timeout"
    assert result.audit.outcome == "budget_exhausted"


@pytest.mark.asyncio
async def test_first_valid_race_records_latency_markers() -> None:
    clock = _ManualClock()
    marks: list[str] = []
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=5.5,
        hedge_after_seconds=1.5,
        acceptance_dispatch_reserve_seconds=1.2,
        clock=clock,
        sleep=clock.sleep,
    ).start(marker=marks.append)
    primary = _ControlledMain()
    backup = _ControlledQuick()
    running = asyncio.create_task(
        Deliberation(router=_Router(), main_model=primary, quick_recovery=backup).deliberate(
            _capsule(), attempt_id="attempt:latency-markers", budget=budget
        )
    )
    await primary.started.wait()
    await clock.advance(1.5)
    await backup.started.wait()
    assert backup.result is not None
    backup.result.set_result(
        ModelOutput(model_id="backup", model_version="v1", raw_proposal=_minimal_raw())
    )
    result = await running
    assert result.proposal is not None
    assert "primary" in marks
    assert "hedge_started" in marks
    assert "candidate_validated" in marks
    assert "winner" in marks


@pytest.mark.asyncio
async def test_primary_can_win_after_hedge_starts_and_loser_is_audited() -> None:
    clock = _ManualClock()
    primary = _ControlledMain()
    backup = _ControlledQuick()
    running = asyncio.create_task(
        Deliberation(router=_Router(), main_model=primary, quick_recovery=backup).deliberate(
            _capsule(),
            attempt_id="attempt:primary-after-hedge",
            budget=InteractiveTurnBudgetPolicy(
                total_seconds=5.5,
                hedge_after_seconds=1.5,
                acceptance_dispatch_reserve_seconds=1.2,
                clock=clock,
                sleep=clock.sleep,
            ).start(),
        )
    )
    await primary.started.wait()
    await clock.advance(1.5)
    await backup.started.wait()
    assert primary.result is not None
    primary.result.set_result(
        ModelOutput(model_id="primary", model_version="v1", raw_proposal=_decision_raw())
    )

    result = await running

    assert result.audit.model_id == "primary"
    assert len(result.attempt_audits) == 2
    assert result.attempt_audits[0].failure_code == "backup_cancelled"
    assert result.attempt_audits[0].outcome == "hedge_cancelled"


@pytest.mark.asyncio
async def test_normal_flash_deliberation_returns_inert_validated_proposal_and_audit() -> None:
    main = _Main()
    quick = _Quick()
    router = _Router()
    unit = Deliberation(router=router, main_model=main, quick_recovery=quick)

    result = await unit.deliberate(_capsule(), attempt_id="attempt:1")

    assert result.proposal is not None
    assert result.audit.status == "proposal_validated"
    assert result.audit.route.tier == "flash"
    assert result.audit.model_call_id == main.requests[0].call_id
    assert result.audit.model_call_id.startswith("model-call:")
    assert quick.failure_codes == []
    assert router.requests[0].route_hints.source == "trusted_capsule"
    assert router.requests[0].route_hints.source_capsule_id == result.capsule_id
    assert not hasattr(unit, "_ledger")
    assert not hasattr(unit, "_action_executor")


@pytest.mark.asyncio
async def test_trusted_recall_trace_extends_frozen_evidence_and_is_audited() -> None:
    handle = _capsule()
    capsule = handle.capsule
    cursor = RecallCursor(
        world_revision=capsule.world_revision,
        deliberation_revision=capsule.deliberation_revision,
        ledger_sequence=capsule.ledger_sequence,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:fact:source",
                memory_kind="semantic",
                source_item_ref="fact:source",
                source_slice="relevant_facts",
                source_refs=("event:source:recall",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="FactCommitted",
                        ref="event:source:recall",
                        source_world_revision=7,
                        immutable_hash=HASH_B,
                    ),
                ),
                source_world_revision=7,
                text="The counterpart previously described a tea preference.",
                actor_ref="actor:companion",
                subject_refs=("actor:companion",),
                occurred_from=NOW,
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="actor:companion",
        subject_refs=("actor:companion",),
        logical_time=NOW,
    )
    trace = coordinator.recall(
        request=CharacterRecallRequest(query_text="tea preference"),
        accessibility_seed="draw:test:recall",
        expected_cursor=cursor,
        trigger_ref=capsule.trigger_ref,
    )
    raw = _decision_raw(evidence_ref="event:source:recall")
    raw["evidence_refs"][0]["evidence_kind"] = "committed_fact"
    main = _Main(
        ModelOutput(
            model_id="main",
            model_version="v1",
            raw_proposal=raw,
            recall_trace=trace,
        )
    )

    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
    ).deliberate(handle, attempt_id="attempt:recall")

    assert result.proposal is not None
    assert result.audit.recall_trace is not None
    assert result.audit.recall_trace.result_hash == trace.audit.result_hash


@pytest.mark.asyncio
async def test_seen_prefetch_fact_extends_frozen_evidence_and_is_audited() -> None:
    handle = _capsule()
    capsule = handle.capsule
    cursor = RecallCursor(
        world_revision=capsule.world_revision,
        deliberation_revision=capsule.deliberation_revision,
        ledger_sequence=capsule.ledger_sequence,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:prefetch:source",
                memory_kind="semantic",
                source_item_ref="fact:prefetch:source",
                source_slice="relevant_facts",
                source_refs=("event:source:prefetch",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="FactCommitted",
                        ref="event:source:prefetch",
                        source_world_revision=7,
                        immutable_hash=HASH_B,
                    ),
                ),
                source_world_revision=7,
                text="The counterpart previously described a tea preference.",
                actor_ref="actor:companion",
                subject_refs=("actor:companion",),
                occurred_from=NOW,
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="actor:companion",
        subject_refs=("actor:companion",),
        logical_time=NOW,
    )
    trace = coordinator.prefetch(
        query_text="tea preference",
        accessibility_seed="draw:test:prefetch",
        trigger_ref=capsule.trigger_ref,
    )
    raw = _decision_raw(evidence_ref="event:source:prefetch")
    raw["evidence_refs"][0]["evidence_kind"] = "committed_fact"
    main = _Main(
        ModelOutput(
            model_id="main",
            model_version="v1",
            raw_proposal=raw,
            prefetch_trace=trace,
        )
    )

    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
    ).deliberate(handle, attempt_id="attempt:prefetch")

    assert result.proposal is not None
    assert result.audit.prefetch_trace is not None
    assert result.audit.prefetch_trace.result_hash == trace.audit.result_hash


@pytest.mark.asyncio
async def test_trigger_message_reaches_model_only_when_bound_to_current_observation_evidence() -> None:
    main = _Main()
    unit = Deliberation(router=_Router(), main_model=main, quick_recovery=_Quick())
    observed = ProposalEvidenceRef(
        ref_id="observation:current:1",
        evidence_kind="observed_message",
        source_world_revision=7,
        immutable_hash=f"sha256:{HASH_B}",
    )
    current = TriggerMessage(
        event_ref="event:observation:1",
        event_payload_hash=f"sha256:{HASH_B}",
        observation_ref=observed.ref_id,
        source_world_revision=observed.source_world_revision,
        actor="user:primary",
        channel="test",
        reply_target="user:primary",
        text="你刚刚没有接住我的意思。",
    )

    await unit.deliberate(
        _capsule(),
        attempt_id="attempt:current-message",
        trigger_evidence=(_authority_evidence(), observed),
        trigger_message=current,
    )

    assert main.requests[0].trigger_message == current
    forged = current.model_copy(update={"observation_ref": "observation:substituted"})
    with pytest.raises(ValueError, match="observed-message evidence"):
        await unit.deliberate(
            _capsule(),
            attempt_id="attempt:forged-current-message",
            trigger_evidence=(_authority_evidence(), observed),
            trigger_message=forged,
        )
    assert len(main.requests) == 1


@pytest.mark.asyncio
async def test_thinking_route_is_preserved_but_router_failure_defaults_to_flash() -> None:
    thinking = ModelRoute(
        tier="thinking", reason_code="cross_domain_conflict", router_version="router.2"
    )
    first = await Deliberation(
        router=_Router(thinking), main_model=_Main(), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:thinking")
    fallback = await Deliberation(
        router=_Router(fail=True), main_model=_Main(), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:fallback")

    assert first.audit.route.tier == "thinking"
    assert fallback.audit.route.tier == "flash"
    assert fallback.audit.route.reason_code == "router_exception_default"


@pytest.mark.asyncio
async def test_main_timeout_uses_only_minimal_quick_recovery() -> None:
    quick = _Quick()
    result = await Deliberation(
        router=_Router(),
        main_model=_Main(delay=0.05),
        quick_recovery=quick,
        main_timeout_seconds=0.001,
    ).deliberate(_capsule(), attempt_id="attempt:timeout")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.audit.status == "main_timeout_recovered"
    assert result.audit.failure_code == "main_timeout"
    assert quick.failure_codes == ["main_timeout"]
    assert len(result.attempt_audits) == 2
    assert result.attempt_audits[0].status == "main_timeout"
    assert result.attempt_audits[0].model_call_id != result.attempt_audits[1].model_call_id
    assert quick.requests[0].call_id == result.audit.model_call_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        {"proposal_kind": "decision"},
        _decision_raw(evidence_ref="event:not-in-capsule"),
        {**_decision_raw(), "trigger_ref": "event:other"},
        {**_decision_raw(), "evaluated_world_revision": 6},
    ],
)
async def test_invalid_main_output_recovers_without_accepting_unfrozen_claims(raw: object) -> None:
    result = await Deliberation(
        router=_Router(), main_model=_Main(raw), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:invalid")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.audit.status == "main_invalid_recovered"
    assert result.audit.failure_code == "main_invalid_output"
    assert result.attempt_audits[0].status == "main_invalid"


@pytest.mark.asyncio
async def test_quick_recovery_cannot_return_full_decision_and_failure_is_explicit() -> None:
    result = await Deliberation(
        router=_Router(),
        main_model=_Main(fail=True),
        quick_recovery=_Quick(_decision_raw()),
    ).deliberate(_capsule(), attempt_id="attempt:bad-recovery")

    assert result.proposal is None
    assert result.audit.status == "recovery_failed"
    assert result.audit.failure_code == "quick_invalid_output"
    assert result.attempt_audits[0].status == "main_exception"
    assert result.attempt_audits[0].failure_code == "main_exception"


@pytest.mark.asyncio
async def test_adapter_model_construct_cannot_bypass_output_size_preflight() -> None:
    bypass = ModelOutput.model_construct(
        model_id="main",
        model_version="v1",
        raw_proposal={"nested": ["x"] * 20_000},
    )
    result = await Deliberation(
        router=_Router(), main_model=_Main(bypass), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:oversized")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.audit.status == "main_invalid_recovered"


@pytest.mark.asyncio
async def test_adapter_model_construct_cannot_escape_with_huge_token_counter() -> None:
    bypass = ModelOutput.model_construct(
        model_id="main",
        model_version="v1",
        raw_proposal=_decision_raw(),
        input_tokens=1 << 1_000_000,
    )
    result = await Deliberation(
        router=_Router(), main_model=_Main(bypass), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:huge-token-counter")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.audit.status == "main_invalid_recovered"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence_update",
    [
        {"source_world_revision": 6},
        {"immutable_hash": "sha256:" + "f" * 64},
        {"evidence_kind": "settled_external_result"},
        {"evidence_kind": "settled_world_event"},
    ],
)
async def test_evidence_must_match_exact_capsule_authority(
    evidence_update: dict[str, object],
) -> None:
    raw = _decision_raw()
    raw["evidence_refs"] = (
        _authority_evidence().model_copy(update=evidence_update).model_dump(mode="python"),
    )
    result = await Deliberation(
        router=_Router(), main_model=_Main(raw), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:forged-evidence")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.attempt_audits[0].status == "main_invalid"


@pytest.mark.asyncio
async def test_minimal_source_model_result_is_rebuilt_from_quick_response() -> None:
    result = await Deliberation(
        router=_Router(),
        main_model=_Main(fail=True),
        quick_recovery=_Quick(),
    ).deliberate(_capsule(), attempt_id="attempt:forged-model-result")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.proposal.source_model_result == result.audit.model_result_ref
    assert result.proposal.source_model_result != "model-result:recovery:1"


@pytest.mark.asyncio
async def test_same_attempt_different_responses_have_distinct_model_result_refs() -> None:
    handle = _capsule()
    first = await Deliberation(
        router=_Router(), main_model=_Main(fail=True), quick_recovery=_Quick(_minimal_raw())
    ).deliberate(handle, attempt_id="attempt:retry")
    second = await Deliberation(
        router=_Router(),
        main_model=_Main(fail=True),
        quick_recovery=_Quick(_minimal_raw(text="I noticed; let me answer in a moment.")),
    ).deliberate(handle, attempt_id="attempt:retry")

    assert first.audit.model_call_id == second.audit.model_call_id
    assert first.audit.response_hash != second.audit.response_hash
    assert first.audit.model_result_ref != second.audit.model_result_ref


@pytest.mark.asyncio
async def test_main_minimal_source_is_also_rebuilt_from_actual_response() -> None:
    result = await Deliberation(
        router=_Router(), main_model=_Main(_minimal_raw()), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:main-minimal")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.audit.status == "proposal_validated"
    assert result.proposal.source_model_result == result.audit.model_result_ref
    assert result.proposal.source_model_result != "model-result:recovery:1"


@pytest.mark.asyncio
async def test_deliberation_result_rejects_tampered_identity_or_attempt_sequence() -> None:
    result = await Deliberation(
        router=_Router(), main_model=_Main(), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:result-integrity")
    bad_audit = result.audit.model_copy(
        update={"status": "main_timeout", "failure_code": "main_timeout"}
    )
    material = result.model_dump(mode="python")
    material.update(
        {
            "result_id": "deliberation:arbitrary",
            "audit": bad_audit.model_dump(mode="python"),
            "attempt_audits": (bad_audit.model_dump(mode="python"),),
        }
    )

    with pytest.raises(ValueError):
        type(result).model_validate(material)


@pytest.mark.asyncio
async def test_dict_adapter_extra_payload_is_bounded_before_pydantic_error_path() -> None:
    class RawMain(_Main):
        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            return {  # type: ignore[return-value]
                "model_id": "main",
                "model_version": "v1",
                "raw_proposal": _decision_raw(),
                "extra": ["x"] * 20_000,
            }

    result = await Deliberation(
        router=_Router(), main_model=RawMain(), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:outer-dos")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.attempt_audits[0].status == "main_invalid"


@pytest.mark.asyncio
async def test_model_audit_rejects_impossible_status_output_matrix() -> None:
    result = await Deliberation(
        router=_Router(), main_model=_Main(), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:audit-matrix")
    material = result.audit.model_dump(mode="python")
    material.update({"status": "main_timeout", "failure_code": "main_timeout"})

    with pytest.raises(ValueError, match="terminal main audit"):
        type(result.audit).model_validate(material)


@pytest.mark.asyncio
async def test_handled_provider_exception_is_not_logged_as_detached(caplog) -> None:
    result = await Deliberation(
        router=_Router(), main_model=_Main(fail=True), quick_recovery=_Quick()
    ).deliberate(_capsule(), attempt_id="attempt:handled-exception")

    assert result.audit.status == "main_exception_recovered"
    assert "detached provider task failed" not in caplog.text


@pytest.mark.asyncio
async def test_untrusted_test_capsule_is_rejected_before_any_model_call() -> None:
    main = _Main()
    untrusted = _compile_resolved_context(_request())

    with pytest.raises(TypeError, match="compiler-issued"):
        await Deliberation(router=_Router(), main_model=main, quick_recovery=_Quick()).deliberate(
            untrusted, attempt_id="attempt:untrusted"
        )
    assert main.requests == []


@pytest.mark.asyncio
async def test_metadata_refs_are_individually_bounded_before_request_hashing() -> None:
    unit = Deliberation(router=_Router(), main_model=_Main(), quick_recovery=_Quick())
    with pytest.raises(ValueError, match="invalid reference"):
        await unit.deliberate(
            _capsule(), attempt_id="attempt:metadata", catalog_versions=("x" * 257,)
        )


@pytest.mark.asyncio
async def test_provider_suppressing_cancellation_cannot_extend_caller_deadline() -> None:
    class CancellationSuppressingMain(_Main):
        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
            return ModelOutput(model_id="late", model_version="v1", raw_proposal=_decision_raw())

    loop = asyncio.get_running_loop()
    started = loop.time()
    unit = Deliberation(
        router=_Router(),
        main_model=CancellationSuppressingMain(),
        quick_recovery=_Quick(),
        main_timeout_seconds=0.001,
    )
    result = await unit.deliberate(_capsule(), attempt_id="attempt:hard-deadline")
    elapsed = loop.time() - started

    assert result.audit.status == "main_timeout_recovered"
    # Wall-clock scheduling jitter grows under the full concurrent suite.  The
    # lane-health assertion below is the stronger proof that Deliberation
    # returned while the cancellation-suppressing provider was still running,
    # rather than waiting for its extra 50 ms cleanup.
    assert elapsed < 0.1
    assert unit.provider_health.main_inflight == 1
    assert unit.provider_health.quick_inflight == 0
    assert unit.provider_health.main_circuit_open is False
    assert unit.provider_health.quick_circuit_open is False
    await asyncio.sleep(0.06)
    assert unit.provider_health.main_inflight == 0


@pytest.mark.asyncio
async def test_expression_episode_off_is_original_single_provider_path() -> None:
    main = _EpisodeMain()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
        expression_episode_mode="off",
    ).deliberate(
        _capsule(),
        attempt_id="attempt:episode-off",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    assert result.proposal is not None
    assert main.provisional_requests == []
    assert len(result.attempt_audits) == 1


@pytest.mark.asyncio
async def test_expression_episode_shadow_runs_candidate_without_changing_full_result() -> None:
    main = _EpisodeMain(full_delay=0.02)
    unit = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
        expression_episode_mode="shadow",
    )
    result = await unit.deliberate(
        _capsule(),
        attempt_id="attempt:episode-shadow",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    assert result.proposal is not None
    assert result.proposal.proposal_id == "proposal:decision:1"
    assert len(main.requests) == 1
    assert len(main.provisional_requests) == 1
    assert len(result.attempt_audits) == 1
    diagnostics = unit.expression_episode_diagnostics()
    assert diagnostics["mode"] == "shadow"
    assert diagnostics["turns"] == 1
    assert diagnostics["provisional_first"] == 1
    assert diagnostics["slot_calls"] == 2


@pytest.mark.asyncio
async def test_expression_episode_on_authorizes_first_valid_provisional_only() -> None:
    main = _EpisodeMain(full_delay=0.1)
    deliberation = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
        expression_episode_mode="on",
    )
    result = await deliberation.deliberate(
        _capsule(),
        attempt_id="attempt:episode-on",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    assert result.proposal is not None
    assert result.proposal.proposal_id == "proposal:episode:provisional"
    assert len(main.requests) == 1
    assert len(main.provisional_requests) == 1
    assert len(result.attempt_audits) == 1
    tail = await deliberation.await_expression_episode_tail(
        result.proposal.trigger_ref
    )
    assert tail is not None
    assert tail.disposition == "complete_without_more"


@pytest.mark.asyncio
async def test_expression_episode_on_retains_auditable_full_append_tail() -> None:
    class AppendMain(_EpisodeMain):
        async def propose(self, request: ModelInput) -> ModelOutput:
            output = await super().propose(request)
            return output.model_copy(
                update={
                    "episode_disposition": "append",
                    "raw_proposal": {
                        **output.raw_proposal,
                        "episode_disposition": "append",
                    },
                }
            )

    main = AppendMain(full_delay=0.05)
    deliberation = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
        expression_episode_mode="on",
    )
    result = await deliberation.deliberate(
        _capsule(),
        attempt_id="attempt:episode-on-append",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    assert result.proposal is not None
    tail = await deliberation.await_expression_episode_tail(
        result.proposal.trigger_ref
    )
    assert tail is not None
    assert tail.disposition == "append"
    assert tail.deliberation is not None
    assert tail.deliberation.proposal is not None
    assert tail.deliberation.audit.status == "proposal_validated"
