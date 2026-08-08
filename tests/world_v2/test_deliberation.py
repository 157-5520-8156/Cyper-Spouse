from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from hashlib import sha256

import httpx
import pytest

from companion_daemon.llm import (
    OpenAICompatibleChatModel,
    ProviderCapacityGate,
)
from companion_daemon.world_v2.character_interior.inbound_wire import (
    _ExpressionDraftWire,
)
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
    ModelUsageProvenance,
    PhysicalProviderInvocationAudit,
    RouteRequest,
    TriggerMessage,
    ValidationTechnicalFailure,
    begin_validation_reselection_recovery,
    claim_secondary_provider_slot,
    claim_validation_corrective_provider_slot,
    fit_pre_provider_wait_timeout,
    mark_first_role_provider_completion,
    mark_first_role_provider_entry,
    run_validation_review,
)
from companion_daemon.world_v2.expression_draft import (
    QQ_NAPCAT_EXPRESSION_CAPABILITIES,
)
from companion_daemon.world_v2.interactive_turn_budget import (
    InteractiveTurnBudgetPolicy,
)
from companion_daemon.world_v2.proposal_envelope import MinimalProposal, ProposalEvidenceRef
from companion_daemon.world_v2.proposal_audit_schemas import RecordedModelResultAudit
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallSourceBinding,
)
from companion_daemon.world_v2.recall_runtime import (
    CharacterRecallRequest,
    PresentedPrefetchTrace,
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


@pytest.mark.asyncio
async def test_fast_interface_never_falls_back_to_complete_response_when_stream_is_unavailable() -> None:
    class StreamUnavailable(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.stream_attempted = False
            self.complete_attempted = False

        def stream_provider_available(self, _request: ModelInput) -> bool:
            return False

        async def propose_stream_head(self, _request: ModelInput) -> ModelOutput:
            self.stream_attempted = True
            raise RuntimeError("stream transport unavailable")

        async def propose_stream_tail(self, _request: ModelInput) -> ModelOutput:
            raise RuntimeError("stream transport unavailable")

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.complete_attempted = True
            return await super().propose(request)

    main = StreamUnavailable()
    quick = _Quick()
    deliberation = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
        expression_episode_mode="stream",
    )

    result = await deliberation.deliberate(
        _capsule(),
        attempt_id="attempt:stream-unavailable-no-slow-fallback",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    assert result.proposal is None
    assert main.stream_attempted is True
    assert main.complete_attempted is False
    assert quick.requests == []


class _EpisodeMain(_Main):
    def __init__(self, *, full_delay: float = 0, provisional_delay: float = 0) -> None:
        super().__init__(delay=full_delay)
        self.provisional_delay = provisional_delay
        self.provisional_requests: list[ModelInput] = []

    def shadow_observer_provider_available(self, _request: ModelInput) -> bool:
        return True

    async def propose_shadow_observer(self, request: ModelInput) -> ModelOutput:
        return await self.propose_provisional(request)

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
        self._waiters = [(target, waiter) for target, waiter in self._waiters if not waiter.done()]
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


class _InternallyCancelledMain(_Main):
    """A provider-owned/session Future was invalidated, not the caller task."""

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.requests.append(request)
        invalidated = asyncio.get_running_loop().create_future()
        invalidated.cancel()
        return await invalidated


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
async def test_first_provider_prefetch_wait_uses_one_ingress_relative_budget() -> None:
    clock = _ManualClock()

    class _CapturingMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.prefetch_wait: float | None = None

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.prefetch_wait = fit_pre_provider_wait_timeout(0.45)
            return await super().propose(request)

    main = _CapturingMain()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=5.5,
        hedge_after_seconds=1.5,
        acceptance_dispatch_reserve_seconds=1.2,
        first_provider_entry_seconds=0.5,
        clock=clock,
        sleep=clock.sleep,
    ).start()
    await clock.advance(0.28)

    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
    ).deliberate(
        _capsule(),
        attempt_id="attempt:pre-provider-total-budget",
        budget=budget,
    )

    assert result.proposal is not None
    assert main.prefetch_wait == pytest.approx(0.17)


@pytest.mark.asyncio
async def test_role_adapter_can_mark_actual_first_provider_entry() -> None:
    marks: list[str] = []

    class _MarkingMain(_Main):
        async def propose(self, request: ModelInput) -> ModelOutput:
            mark_first_role_provider_entry(request.call_id)
            result = await super().propose(request)
            mark_first_role_provider_completion(request.call_id)
            return result

    result = await Deliberation(
        router=_Router(),
        main_model=_MarkingMain(),
        quick_recovery=_Quick(),
    ).deliberate(
        _capsule(),
        attempt_id="attempt:first-role-provider-marker",
        budget=InteractiveTurnBudgetPolicy().start(),
        first_role_provider_marker=lambda call_id: marks.append(f"entered:{call_id}"),
        first_role_provider_completion_marker=lambda call_id: marks.append(
            f"completed:{call_id}"
        ),
    )

    assert result.proposal is not None
    assert len(marks) == 2
    assert marks[0].startswith("entered:model-call:")
    assert marks[1] == marks[0].replace("entered:", "completed:", 1)


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
async def test_provider_internal_cancellation_becomes_recoverable_failure() -> None:
    quick = _Quick()

    result = await Deliberation(
        router=_Router(),
        main_model=_InternallyCancelledMain(),
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:provider-internal-cancellation",
        budget=InteractiveTurnBudgetPolicy().start(),
    )

    assert result.proposal is not None
    assert result.audit.model_id == "quick"
    assert quick.failure_codes == ["main_exception"]


@pytest.mark.asyncio
async def test_primary_valid_before_hedge_never_calls_backup() -> None:
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
    assert budget.candidate_deadline == pytest.approx(4.3)
    assert "technical_recovery_started" not in marks


@pytest.mark.asyncio
async def test_latency_hedge_cannot_cool_down_next_turn_formal_recovery() -> None:
    """A role recovery provider is reserve capacity, not a speculative hedge."""

    response_allowed = asyncio.Event()
    provider_requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_requests
        provider_requests += 1
        await response_allowed.wait()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "role recovery completed"}}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 3,
                    "total_tokens": 7,
                },
            },
        )

    capacity = ProviderCapacityGate(
        cooldown_seconds=30.0,
        active_lease_seconds=30.0,
    )
    recovery_provider = OpenAICompatibleChatModel(
        "test-key",
        "https://recovery.invalid",
        "role-recovery",
        transport=httpx.MockTransport(handler),
        capacity_gate=capacity,
    )

    class FormalRecovery:
        def __init__(self) -> None:
            self.requests: list[ModelInput] = []

        def has_hedge_provider(self, request: ModelInput) -> bool:
            del request
            # This provider is reserved for an observed failure.  Deliberation
            # must honor that declared capability without reaching through the
            # unified CharacterInterior into its private wire materializers.
            return False

        async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
            del failure_code
            self.requests.append(request)
            await recovery_provider.complete(
                [{"role": "user", "content": "recover this failed role turn"}]
            )
            return ModelOutput(
                model_id="role-recovery",
                model_version="v1",
                raw_proposal=_minimal_raw(text="这是备用角色模型自己的回复。"),
            )

    formal_recovery = FormalRecovery()
    first_primary = _ControlledMain()
    first_running = asyncio.create_task(
        Deliberation(
            router=_Router(),
            main_model=first_primary,
            quick_recovery=formal_recovery,
        ).deliberate(
            _capsule(),
            attempt_id="attempt:formal-recovery-turn-one",
            budget=InteractiveTurnBudgetPolicy(
                total_seconds=0.5,
                hedge_after_seconds=0.01,
                acceptance_dispatch_reserve_seconds=0.1,
                technical_recovery_seconds=0.3,
            ).start(),
        )
    )
    await first_primary.started.wait()
    # Give the latency hedge boundary a real scheduler turn. The formal
    # recovery provider must remain untouched while the primary is healthy.
    await asyncio.sleep(0.03)
    assert first_primary.result is not None
    first_primary.result.set_result(
        ModelOutput(
            model_id="primary",
            model_version="v1",
            raw_proposal=_decision_raw(),
        )
    )
    first = await first_running
    assert first.proposal is not None

    # A different turn now suffers an actual main-provider failure. The same
    # installed role model must still be immediately admissible.
    response_allowed.set()
    second = await Deliberation(
        router=_Router(),
        main_model=_Main(fail=True),
        quick_recovery=formal_recovery,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:formal-recovery-turn-two",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=0.5,
            hedge_after_seconds=0.01,
            acceptance_dispatch_reserve_seconds=0.1,
            technical_recovery_seconds=0.3,
        ).start(),
    )

    assert second.proposal is not None
    assert second.audit.status == "main_exception_recovered"
    assert provider_requests == 1
    assert capacity.snapshot().ambiguous_cancellations == 0
    await recovery_provider.aclose()


@pytest.mark.asyncio
async def test_flash_route_preserves_provider_reported_auxiliary_reasoning() -> None:
    material = {
        "usage_contract": "model-usage.1",
        "route_class": "expressive",
        "input_tokens": 21,
        "output_tokens": 5,
        "thinking_tokens": 7,
        "token_provenance": "provider_reported",
        "transport": "provider_api",
        "provider": "source-review-aggregate",
        "provider_usage_ref": "usage:source-review-aggregate:1",
    }
    usage = ModelUsageProvenance(
        **material,
        provider_usage_hash=sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )
    output = ModelOutput(
        model_id="flash-with-reviewed-candidate",
        model_version="v1",
        raw_proposal=_decision_raw(),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usage=usage,
    )

    result = await Deliberation(
        router=_Router(),
        main_model=_Main(raw=output),
        quick_recovery=_Quick(),
    ).deliberate(_capsule(), attempt_id="attempt:provider-reasoning-evidence")

    assert result.proposal is not None
    assert result.audit.route.tier == "flash"
    assert result.audit.usage is not None
    assert result.audit.usage.thinking_tokens == 7
    recorded = RecordedModelResultAudit.model_validate(result.audit.model_dump(mode="json"))
    assert recorded.usage is not None
    assert recorded.usage.thinking_tokens == 7


@pytest.mark.asyncio
async def test_primary_deadline_opens_independent_fallback_window_after_real_timeout() -> None:
    class SourceClosureMain(_ControlledMain):
        def source_closure_review_enabled(self) -> bool:
            return True

    class IdentifiedFallback(_ControlledQuick):
        pass

    clock = _ManualClock()
    marks: list[str] = []
    primary = SourceClosureMain()
    fallback = IdentifiedFallback()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=5.5,
        hedge_after_seconds=1.5,
        acceptance_dispatch_reserve_seconds=1.2,
        technical_recovery_seconds=2.0,
        clock=clock,
        sleep=clock.sleep,
    ).start(marker=marks.append)
    running = asyncio.create_task(
        Deliberation(router=_Router(), main_model=primary, quick_recovery=fallback).deliberate(
            _capsule(),
            attempt_id="attempt:post-deadline-fallback",
            budget=budget,
        )
    )

    await primary.started.wait()
    await clock.advance(4.3)
    await asyncio.wait_for(fallback.started.wait(), timeout=0.1)
    assert fallback.result is not None
    fallback.result.set_result(
        ModelOutput(
            model_id="configured-fallback",
            model_version="v1",
            raw_proposal=_minimal_raw(text="这是备用角色模型自己的回复。"),
            winning_model_call_id="model-call:actual-configured-fallback",
            winning_request_hash="e" * 64,
        )
    )

    result = await running

    assert result.proposal is not None
    assert result.audit.status == "main_timeout_recovered"
    assert result.audit.failure_code == "primary_timeout"
    assert result.audit.slot == "backup"
    assert result.audit.model_call_id == "model-call:actual-configured-fallback"
    assert result.audit.request_hash == "e" * 64
    assert result.attempt_audits[0].failure_code == "primary_timeout"
    assert len(primary.requests) == len(fallback.requests) == 1
    assert fallback.failure_codes == ["main_timeout"]
    assert "technical_recovery_started" in marks


@pytest.mark.asyncio
async def test_source_review_failure_retries_review_without_reauthoring_or_author_recovery() -> (
    None
):
    class ReviewRecoveringMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.author_calls = 0
            self.reviewer_calls = 0
            self.second_review_started = asyncio.Event()
            self.release_second_review = asyncio.Event()

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            self.author_calls += 1

            async def review() -> None:
                self.reviewer_calls += 1
                if self.reviewer_calls == 1:
                    raise TimeoutError("review provider timed out")
                self.second_review_started.set()
                await self.release_second_review.wait()

            await run_validation_review(review, timeout_seconds=0.2)
            return ModelOutput(
                model_id="reviewed-main",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    marks: list[str] = []
    main = ReviewRecoveringMain()
    quick = _Quick()
    running = asyncio.create_task(
        Deliberation(
            router=_Router(),
            main_model=main,
            quick_recovery=quick,
        ).deliberate(
            _capsule(),
            attempt_id="attempt:review-only-recovery",
            budget=InteractiveTurnBudgetPolicy(
                total_seconds=1.0,
                hedge_after_seconds=0.2,
                acceptance_dispatch_reserve_seconds=0.2,
                technical_recovery_seconds=0.4,
                validation_recovery_seconds=0.2,
            ).start(marker=marks.append),
        )
    )

    await asyncio.wait_for(main.second_review_started.wait(), timeout=0.2)
    assert not running.done()
    assert main.author_calls == 1
    assert main.reviewer_calls == 2
    assert quick.requests == []
    main.release_second_review.set()
    result = await running

    assert result.proposal is not None
    assert result.audit.model_id == "reviewed-main"
    assert main.author_calls == 1
    assert main.reviewer_calls == 2
    assert quick.requests == []
    assert "validation_recovery_started" in marks
    assert "technical_recovery_started" not in marks


@pytest.mark.asyncio
async def test_terminal_review_retry_preserves_the_last_reviewer_invocation_audit() -> None:
    material = {
        "usage_contract": "model-usage.1",
        "route_class": "expressive",
        "input_tokens": 8,
        "output_tokens": 2,
        "thinking_tokens": 0,
        "token_provenance": "provider_reported",
        "transport": "provider_api",
        "provider": "independent-source-reviewer",
        "provider_usage_ref": "usage:reviewer:second",
    }
    usage = ModelUsageProvenance(
        **material,
        provider_usage_hash=sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )
    calls = 0

    async def review() -> None:
        nonlocal calls
        calls += 1
        raise ValidationTechnicalFailure(
            "source_review_exception",
            model_call_id=f"model-call:reviewer:{calls}",
            request_hash=str(calls) * 64,
            attempted_model_id="independent-source-reviewer",
            attempted_model_version="review-wire.5",
            usage=usage if calls == 2 else None,
        )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await run_validation_review(review, timeout_seconds=0.2)

    assert calls == 2
    assert caught.value.model_call_id == "model-call:reviewer:2"
    assert caught.value.request_hash == "2" * 64
    assert caught.value.attempted_model_id == "independent-source-reviewer"
    assert caught.value.attempted_model_version == "review-wire.5"
    assert caught.value.usage == usage


@pytest.mark.asyncio
async def test_terminal_stream_correction_failure_preserves_physical_audit() -> None:
    physical = PhysicalProviderInvocationAudit(
        model_call_id="model-call:retired-physical-stream",
        request_hash="a" * 64,
        model_id="stream-role",
        model_version="stream-role.1",
        outcome="unresolved",
        failure_code="stream_reselection_unresolved",
        usage_status="unresolved",
        semantic_model_call_ids=(
            "model-call:retired-stream-head",
            "model-call:retired-stream-tail",
        ),
    )

    class FailedCorrection(_Main):
        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            raise ValidationTechnicalFailure(
                "authored_subcall_exception",
                attempted_model_id="stream-role",
                attempted_model_version="stream-role.1",
                physical_provider_audits=(physical,),
            )

    deliberation = Deliberation(
        router=_Router(),
        main_model=FailedCorrection(),
        quick_recovery=_Quick(),
    )
    result = await deliberation.deliberate(
        _capsule(),
        attempt_id="attempt:failed-stream-correction-audit",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    failed = result.attempt_audits[0]
    assert failed.failure_code == "authored_subcall_exception"
    assert failed.physical_provider_audits == (physical,)


@pytest.mark.asyncio
async def test_validation_recovery_from_one_candidate_does_not_starve_the_next_candidate() -> None:
    class ReviewRecoveringMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.author_calls = 0
            self.reviewer_calls_by_author: list[int] = []

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            self.author_calls += 1
            reviewer_calls = 0

            async def review() -> None:
                nonlocal reviewer_calls
                reviewer_calls += 1
                if reviewer_calls == 1:
                    raise TimeoutError("first reviewer attempt failed")

            await run_validation_review(review, timeout_seconds=0.05)
            self.reviewer_calls_by_author.append(reviewer_calls)
            return ModelOutput(
                model_id="reviewed-main",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    marks: list[str] = []
    main = ReviewRecoveringMain()
    quick = _Quick()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=1.2,
        hedge_after_seconds=0.2,
        acceptance_dispatch_reserve_seconds=0.1,
        technical_recovery_seconds=0.3,
        validation_recovery_seconds=0.1,
    ).start(marker=marks.append)

    first = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:review-candidate-one",
        budget=budget,
    )
    # The first candidate's reviewer-only window has expired, while the
    # ordinary turn author window is still live.
    await asyncio.sleep(0.12)
    second = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:review-candidate-two",
        budget=budget,
    )

    assert first.proposal is not None
    assert second.proposal is not None
    assert main.author_calls == 2
    assert main.reviewer_calls_by_author == [2, 2]
    assert quick.requests == []
    assert marks.count("validation_recovery_started") == 2
    assert "technical_recovery_started" not in marks


@pytest.mark.asyncio
async def test_exhausted_source_review_opens_configured_role_recovery() -> None:
    class ReviewFailingMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.author_calls = 0
            self.reviewer_calls = 0

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            self.author_calls += 1

            async def review() -> None:
                self.reviewer_calls += 1
                raise TimeoutError("review provider timed out")

            await run_validation_review(review, timeout_seconds=0.1)
            raise AssertionError("an unreviewed draft must not materialize")

    marks: list[str] = []
    main = ReviewFailingMain()
    quick = _Quick()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:review-exhausted-no-author",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
            technical_recovery_seconds=0.4,
            validation_recovery_seconds=0.2,
        ).start(marker=marks.append),
    )

    assert result.proposal is not None
    assert result.attempt_audits[0].status == "main_timeout"
    assert result.attempt_audits[0].failure_code == "source_review_timeout"
    assert result.audit.status == "main_timeout_recovered"
    assert result.audit.failure_code == "source_review_timeout"
    assert main.author_calls == 1
    assert main.reviewer_calls == 2
    assert len(quick.requests) == 1
    assert "validation_recovery_started" in marks
    assert "technical_recovery_started" in marks


@pytest.mark.asyncio
@pytest.mark.parametrize("with_budget", (False, True))
@pytest.mark.parametrize(
    (
        "failure_code",
        "claims_corrective",
        "expected_status",
        "expected_outcome",
        "expected_slot",
    ),
    (
        ("source_review_timeout", False, "main_timeout", "timeout", "primary"),
        ("source_review_exception", False, "main_exception", "exception", "primary"),
        ("inventory_invalid", False, "main_exception", "invalid", "primary"),
        ("coverage_invalid", False, "main_exception", "invalid", "primary"),
        (
            "authored_expression_reselection_invalid",
            True,
            "main_exception",
            "exception",
            "corrective",
        ),
        (
            "proactive_claim_binding_invalid",
            False,
            "main_exception",
            "exception",
            "primary",
        ),
    ),
)
@pytest.mark.asyncio
async def test_terminal_validation_failure_mapping_is_budget_independent(
    with_budget: bool,
    failure_code: str,
    claims_corrective: bool,
    expected_status: str,
    expected_outcome: str,
    expected_slot: str,
) -> None:
    class TerminalReselectionMain(_Main):
        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            if claims_corrective:
                assert claim_validation_corrective_provider_slot()
            raise ValidationTechnicalFailure(failure_code)  # type: ignore[arg-type]

    main = TerminalReselectionMain()
    quick = _Quick()
    unit = Deliberation(router=_Router(), main_model=main, quick_recovery=quick)
    kwargs = (
        {
            "budget": InteractiveTurnBudgetPolicy(
                total_seconds=1.0,
                hedge_after_seconds=0.2,
                acceptance_dispatch_reserve_seconds=0.2,
            ).start()
        }
        if with_budget
        else {}
    )

    result = await unit.deliberate(
        _capsule(),
        attempt_id=f"attempt:terminal-reselection:{failure_code}:{with_budget}",
        **kwargs,
    )

    assert (result.proposal is not None) is with_budget
    main_audit = result.attempt_audits[0]
    assert main_audit.status == expected_status
    assert main_audit.failure_code == failure_code
    assert main_audit.slot == expected_slot
    assert main_audit.outcome == expected_outcome
    if with_budget:
        assert result.audit.status == (
            "main_timeout_recovered"
            if expected_status == "main_timeout"
            else "main_exception_recovered"
        )
        assert result.audit.failure_code == failure_code
        assert len(result.attempt_audits) == 2
    else:
        assert result.audit == main_audit
        assert len(result.attempt_audits) == 1
    assert (
        RecordedModelResultAudit.model_validate(main_audit.model_dump(mode="python")).failure_code
        == failure_code
    )
    assert len(main.requests) == 1
    assert len(quick.requests) == int(with_budget)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_code", "expected_status", "expected_main_status"),
    (
        (
            "authored_subcall_timeout",
            "main_timeout_recovered",
            "main_timeout",
        ),
        (
            "authored_subcall_exception",
            "main_exception_recovered",
            "main_exception",
        ),
    ),
)
@pytest.mark.asyncio
async def test_nested_role_transport_failure_can_use_the_configured_recovery_author(
    failure_code: str,
    expected_status: str,
    expected_main_status: str,
) -> None:
    class FailedNestedAuthor(_Main):
        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            raise ValidationTechnicalFailure(failure_code)  # type: ignore[arg-type]

    main = FailedNestedAuthor()
    quick = _Quick()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id=f"attempt:recover-nested-author:{failure_code}",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
            technical_recovery_seconds=0.4,
        ).start(),
    )

    assert result.proposal is not None
    assert result.audit.status == expected_status
    assert result.audit.failure_code == failure_code
    assert result.attempt_audits[0].status == expected_main_status
    assert result.attempt_audits[0].failure_code == failure_code
    assert quick.failure_codes == [failure_code]


@pytest.mark.asyncio
async def test_invalid_recall_reselection_is_terminal_through_public_deliberation() -> None:
    class InvalidRecallThenInvalidFinal:
        model = "invalid-recall-final"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete_with_usage(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> tuple[str, dict[str, object]]:
            del temperature
            self.calls.append(messages)
            if len(self.calls) == 1:
                raw = json.dumps(
                    {
                        "private_turn_state": {
                            "inner_state_summary": "我想先回忆再决定。",
                            "attended_source_refs": [],
                        },
                        "recall_request": {
                            "query_text": "非法 Recall 选择",
                            "limit": 7,
                        },
                    },
                    ensure_ascii=False,
                )
            elif len(self.calls) == 2:
                raw = json.dumps(
                    {
                        "timing_choice": "now",
                        "beats": [{"modality": "text", "text": "第二次仍缺少私人状态。"}],
                        "stance": "invalid_without_private_state",
                        "brief_rationale": "Invalid final fixture.",
                        "world_claims": [],
                    },
                    ensure_ascii=False,
                )
            else:
                raise AssertionError("a terminal Recall correction opened a third role call")
            usage_material = {
                "usage_contract": "model-usage.1",
                "route_class": "expressive",
                "input_tokens": 10 + len(self.calls),
                "output_tokens": 3 + len(self.calls),
                "thinking_tokens": len(self.calls),
                "token_provenance": "provider_reported",
                "transport": "provider_api",
                "provider": self.model,
                "provider_usage_ref": f"usage:invalid-recall-final:{len(self.calls)}",
            }
            return raw, {
                **usage_material,
                "provider_usage_hash": sha256(
                    json.dumps(
                        usage_material,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }

    provider = InvalidRecallThenInvalidFinal()
    quick = _Quick()
    result = await Deliberation(
        router=_Router(),
        main_model=_ExpressionDraftWire(
            model=provider,
            expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
                update={"private_turn_state_mode": "required"}
            ),
        ),
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:terminal-invalid-recall-adapter",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=5.5,
            hedge_after_seconds=1.5,
            acceptance_dispatch_reserve_seconds=1.2,
        ).start(),
    )

    assert result.proposal is not None
    main_audit = result.attempt_audits[0]
    assert main_audit.status == "main_exception"
    assert main_audit.failure_code == "recall_choice_reselection_invalid"
    assert main_audit.slot == "corrective"
    assert main_audit.outcome == "exception"
    assert main_audit.attempted_model_id == provider.model
    assert main_audit.attempted_model_version == _ExpressionDraftWire.VERSION
    assert main_audit.usage is not None
    assert main_audit.usage.input_tokens == 23
    assert main_audit.usage.output_tokens == 9
    assert result.audit.status == "main_exception_recovered"
    recorded = RecordedModelResultAudit.model_validate(main_audit.model_dump(mode="python"))
    assert recorded.attempted_model_id == provider.model
    assert recorded.usage is not None
    assert recorded.usage.input_tokens == 23
    assert len(provider.calls) == 2
    assert len(quick.requests) == 1


@pytest.mark.asyncio
async def test_validation_retry_may_finish_after_author_deadline_without_opening_author_recovery() -> (
    None
):
    class LateReviewMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.author_calls = 0
            self.reviewer_calls = 0

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            self.author_calls += 1
            await asyncio.sleep(0.25)

            async def review() -> None:
                self.reviewer_calls += 1
                if self.reviewer_calls == 1:
                    await asyncio.sleep(0.05)
                    raise TimeoutError("first reviewer attempt failed")
                await asyncio.sleep(0.08)

            await run_validation_review(review, timeout_seconds=0.12)
            return ModelOutput(
                model_id="late-reviewed-main",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    marks: list[str] = []
    main = LateReviewMain()
    quick = _Quick()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:late-review-only-recovery",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=0.4,
            hedge_after_seconds=0.1,
            acceptance_dispatch_reserve_seconds=0.05,
            technical_recovery_seconds=0.3,
            validation_recovery_seconds=0.2,
        ).start(marker=marks.append),
    )

    assert result.proposal is not None
    assert main.author_calls == 1
    assert main.reviewer_calls == 2
    assert quick.requests == []
    assert "validation_recovery_started" in marks
    assert "technical_recovery_started" not in marks


@pytest.mark.asyncio
async def test_near_deadline_author_gets_one_complete_review_attempt_before_retry() -> None:
    class NearDeadlineReviewedMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.reviewer_calls = 0

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            # Leave less than one complete review request in the ordinary
            # author window while still returning a valid authored candidate.
            await asyncio.sleep(0.4)

            async def review() -> None:
                self.reviewer_calls += 1
                await asyncio.sleep(0.08)

            await run_validation_review(review, timeout_seconds=0.15)
            return ModelOutput(
                model_id="near-deadline-reviewed-main",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    marks: list[str] = []
    main = NearDeadlineReviewedMain()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
    ).deliberate(
        _capsule(),
        attempt_id="attempt:complete-near-deadline-review",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=0.5,
            hedge_after_seconds=0.1,
            acceptance_dispatch_reserve_seconds=0.05,
            technical_recovery_seconds=0.2,
            validation_recovery_seconds=0.18,
        ).start(marker=marks.append),
    )

    assert result.proposal is not None
    assert main.reviewer_calls == 1
    assert marks.count("validation_recovery_started") == 1
    assert "technical_recovery_started" not in marks


@pytest.mark.asyncio
async def test_full_reviewer_timeout_and_retry_survive_the_author_deadline() -> None:
    class FullRetryReviewedMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.reviewer_calls = 0

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            # Return close to the ordinary author deadline, then scale the
            # production 22s + 22s reviewer attempts down by 100.
            await asyncio.sleep(0.4)

            async def review() -> None:
                self.reviewer_calls += 1
                if self.reviewer_calls == 1:
                    await asyncio.sleep(0.23)
                    raise AssertionError("the first reviewer attempt must time out")
                await asyncio.sleep(0.21)

            await run_validation_review(review, timeout_seconds=0.22)
            return ModelOutput(
                model_id="full-retry-reviewed-main",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    marks: list[str] = []
    main = FullRetryReviewedMain()
    quick = _Quick()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:full-review-retry-after-author-deadline",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=0.5,
            hedge_after_seconds=0.1,
            acceptance_dispatch_reserve_seconds=0.05,
            technical_recovery_seconds=0.2,
            validation_recovery_seconds=0.46,
            validation_reselection_seconds=0.32,
        ).start(marker=marks.append),
    )

    assert result.proposal is not None
    assert result.audit.model_id == "full-retry-reviewed-main"
    assert main.reviewer_calls == 2
    assert quick.requests == []
    assert marks.count("validation_recovery_started") == 1
    assert "technical_recovery_started" not in marks


@pytest.mark.asyncio
async def test_source_reselection_window_fits_role_repair_and_complete_final_review() -> None:
    class LateSourceCorrectionMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.author_calls = 0
            self.reviewer_calls = 0
            self.correction_calls = 0

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            self.author_calls += 1
            await asyncio.sleep(0.25)

            async def first_review() -> None:
                self.reviewer_calls += 1
                await asyncio.sleep(0.02)

            await run_validation_review(first_review, timeout_seconds=0.05)
            assert begin_validation_reselection_recovery()
            self.correction_calls += 1
            # Scale the production 8s repair + 22s final review + 2s margin
            # down by 100 while preserving their deadline relationship.
            await asyncio.sleep(0.08)

            async def corrected_review() -> None:
                self.reviewer_calls += 1
                await asyncio.sleep(0.21)

            await run_validation_review(corrected_review, timeout_seconds=0.22)
            return ModelOutput(
                model_id="late-source-corrected-main",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    marks: list[str] = []
    main = LateSourceCorrectionMain()
    quick = _Quick()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:late-source-correction",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=0.4,
            hedge_after_seconds=0.1,
            acceptance_dispatch_reserve_seconds=0.05,
            technical_recovery_seconds=0.3,
            validation_recovery_seconds=0.2,
            validation_reselection_seconds=0.32,
        ).start(marker=marks.append),
    )

    assert result.proposal is not None
    assert main.author_calls == 1
    assert main.correction_calls == 1
    assert main.reviewer_calls == 2
    assert quick.requests == []
    assert marks.count("validation_reselection_started") == 1
    assert "technical_recovery_started" not in marks


@pytest.mark.asyncio
async def test_reviewer_retry_cannot_consume_correction_final_review_and_appeal_window() -> None:
    class RetriedReviewThenCorrectedMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.review_attempts = 0
            self.initial_appeal_attempts = 0
            self.final_review_attempts = 0
            self.correction_calls = 0
            self.appeal_calls = 0
            self.appeal_attempts = 0

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            await asyncio.sleep(0.12)

            async def initial_review() -> None:
                self.review_attempts += 1
                await asyncio.sleep(0.06 if self.review_attempts == 1 else 0.02)

            # The independent reviewer needed its one technical retry. That
            # retry must not consume the separate doctrine-authorized window
            # for one role correction plus final review and focused appeal.
            await run_validation_review(initial_review, timeout_seconds=0.05)

            async def initial_appeal() -> None:
                self.initial_appeal_attempts += 1
                if self.initial_appeal_attempts == 1:
                    raise RuntimeError("initial appeal wire parse failed")
                await asyncio.sleep(0.01)

            await run_validation_review(initial_appeal, timeout_seconds=0.05)
            assert begin_validation_reselection_recovery()
            self.correction_calls += 1
            await asyncio.sleep(0.08)

            async def final_review() -> None:
                self.review_attempts += 1
                self.final_review_attempts += 1
                if self.final_review_attempts == 1:
                    raise RuntimeError("final reviewer transport reset")
                await asyncio.sleep(0.02)

            await run_validation_review(final_review, timeout_seconds=0.05)
            self.appeal_calls += 1

            async def focused_appeal() -> None:
                self.appeal_attempts += 1
                if self.appeal_attempts == 1:
                    raise RuntimeError("appeal wire parse failed")
                await asyncio.sleep(0.02)

            await run_validation_review(focused_appeal, timeout_seconds=0.05)
            return ModelOutput(
                model_id="review-retry-then-corrected",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    marks: list[str] = []
    main = RetriedReviewThenCorrectedMain()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
    ).deliberate(
        _capsule(),
        attempt_id="attempt:review-retry-before-reselection",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=0.3,
            hedge_after_seconds=0.1,
            acceptance_dispatch_reserve_seconds=0.05,
            # This test exercises the later reselection window. Keep the
            # initial fixed reviewer phase large enough for its full first
            # attempt, retry, and the separate appeal fixture.
            validation_recovery_seconds=0.12,
            validation_reselection_seconds=0.3,
        ).start(marker=marks.append),
    )

    assert result.proposal is not None
    assert main.review_attempts == 4
    assert main.initial_appeal_attempts == 2
    assert main.final_review_attempts == 2
    assert main.correction_calls == 1
    assert main.appeal_calls == 1
    assert main.appeal_attempts == 2
    assert marks.count("validation_recovery_started") == 1
    assert marks.count("validation_reselection_started") == 1
    assert "technical_recovery_started" not in marks


@pytest.mark.asyncio
async def test_independent_fallback_window_has_a_hard_deadline() -> None:
    class SourceClosureMain(_Main):
        def source_closure_review_enabled(self) -> bool:
            return True

    clock = _ManualClock()
    fallback = _ControlledQuick()
    running = asyncio.create_task(
        Deliberation(
            router=_Router(),
            main_model=SourceClosureMain({"bad": True}),
            quick_recovery=fallback,
        ).deliberate(
            _capsule(),
            attempt_id="attempt:bounded-technical-recovery",
            budget=InteractiveTurnBudgetPolicy(
                total_seconds=5.5,
                hedge_after_seconds=1.5,
                acceptance_dispatch_reserve_seconds=1.2,
                technical_recovery_seconds=2.0,
                clock=clock,
                sleep=clock.sleep,
            ).start(),
        )
    )

    await fallback.started.wait()
    await clock.advance(1.99)
    assert not running.done()
    await clock.advance(0.01)
    result = await running

    assert result.proposal is None
    assert result.audit.status == "recovery_failed"
    assert result.audit.failure_code == "backup_timeout"
    assert result.audit.outcome == "budget_exhausted"
    assert len(fallback.requests) == 1


@pytest.mark.asyncio
async def test_actual_failure_recovery_does_not_require_a_speculative_hedge_provider() -> None:
    class NoSpeculativeHedgeQuick(_Quick):
        def has_hedge_provider(self, _request: ModelInput) -> bool:
            return False

    fallback = NoSpeculativeHedgeQuick({"bad": True})
    result = await Deliberation(
        router=_Router(),
        main_model=_Main({"bad": True}),
        quick_recovery=fallback,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:actual-failure-without-hedge",
        budget=InteractiveTurnBudgetPolicy().start(),
    )

    assert result.proposal is None
    assert result.audit.status == "recovery_failed"
    assert result.audit.failure_code == "backup_invalid"
    assert result.attempt_audits[0].status == "main_invalid"
    assert len(fallback.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ("inventory_invalid", "coverage_invalid"))
@pytest.mark.asyncio
async def test_recovery_source_validation_failure_records_durable_invalid_outcome(
    failure_code: str,
) -> None:
    class SourceValidationFailingQuick(_Quick):
        async def recover(self, request: ModelInput, main_failure_code: str) -> ModelOutput:
            self.requests.append(request)
            self.failure_codes.append(main_failure_code)
            raise ValidationTechnicalFailure(failure_code)  # type: ignore[arg-type]

    fallback = SourceValidationFailingQuick()
    result = await Deliberation(
        router=_Router(),
        main_model=_Main({"bad": True}),
        quick_recovery=fallback,
    ).deliberate(
        _capsule(),
        attempt_id=f"attempt:recovery-source-validation:{failure_code}",
        budget=InteractiveTurnBudgetPolicy().start(),
    )

    assert result.proposal is None
    assert result.audit.status == "recovery_failed"
    assert result.audit.failure_code == f"backup_{failure_code}"
    assert result.audit.outcome == "invalid"
    assert result.attempt_audits[0].failure_code == "primary_invalid"
    assert result.attempt_audits[0].outcome == "invalid"
    assert (
        RecordedModelResultAudit.model_validate(result.audit.model_dump(mode="python")).failure_code
        == f"backup_{failure_code}"
    )
    assert len(fallback.requests) == 1


@pytest.mark.asyncio
async def test_actual_failure_reuses_an_open_turn_recovery_window_without_extending_it() -> None:
    clock = _ManualClock()
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=5.5,
        hedge_after_seconds=1.5,
        acceptance_dispatch_reserve_seconds=1.2,
        technical_recovery_seconds=2.0,
        clock=clock,
        sleep=clock.sleep,
    ).start()
    existing_deadline = budget.begin_technical_recovery()
    primary = _ControlledMain()
    fallback = _ControlledQuick()

    running = asyncio.create_task(
        Deliberation(
            router=_Router(),
            main_model=primary,
            quick_recovery=fallback,
        ).deliberate(
            _capsule(),
            attempt_id="attempt:reuse-open-technical-recovery",
            budget=budget,
        )
    )
    await primary.started.wait()
    assert primary.result is not None
    primary.result.set_result(
        ModelOutput(model_id="invalid", model_version="v1", raw_proposal={"bad": True})
    )
    await fallback.started.wait()
    assert fallback.result is not None
    fallback.result.set_result(
        ModelOutput(model_id="invalid", model_version="v1", raw_proposal={"bad": True})
    )
    result = await running

    assert result.proposal is None
    assert result.audit.status == "recovery_failed"
    assert result.audit.failure_code == "backup_invalid"
    assert len(fallback.requests) == 1
    assert budget.candidate_deadline == existing_deadline


@pytest.mark.asyncio
async def test_terminal_character_reselection_failure_uses_started_role_recovery() -> None:
    clock = _ManualClock()
    primary = _ControlledMain()
    fallback = _ControlledQuick()
    running = asyncio.create_task(
        Deliberation(
            router=_Router(),
            main_model=primary,
            quick_recovery=fallback,
        ).deliberate(
            _capsule(),
            attempt_id="attempt:terminal-reselection-after-hedge",
            budget=InteractiveTurnBudgetPolicy(
                total_seconds=5.5,
                hedge_after_seconds=1.5,
                acceptance_dispatch_reserve_seconds=1.2,
                technical_recovery_seconds=2.0,
                clock=clock,
                sleep=clock.sleep,
            ).start(),
        )
    )
    await primary.started.wait()
    await clock.advance(1.6)
    await fallback.started.wait()
    assert primary.result is not None
    assert fallback.result is not None
    fallback.result.set_result(
        ModelOutput(
            model_id="quick-role-recovery",
            model_version="v1",
            raw_proposal=_minimal_raw(text="我在，刚才那次没组织好。"),
        )
    )
    primary.result.set_exception(
        ValidationTechnicalFailure("authored_expression_reselection_invalid")
    )

    result = await running

    assert result.proposal is not None
    assert len(result.attempt_audits) == 2
    assert result.attempt_audits[0].status == "main_exception"
    assert result.attempt_audits[0].failure_code == (
        "authored_expression_reselection_invalid"
    )
    assert result.audit.status == "main_exception_recovered"
    assert result.audit.failure_code == "authored_expression_reselection_invalid"
    assert not fallback.result.cancelled()


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
async def test_failed_primary_corrective_can_use_one_configured_fallback_role_model() -> None:
    class InvalidCorrectiveMain(_Main):
        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            assert claim_validation_corrective_provider_slot()
            return ModelOutput(
                model_id="primary-corrective",
                model_version="v1",
                raw_proposal={"bad": True},
                winning_model_call_id="model-call:actual-failed-corrective",
                winning_request_hash="f" * 64,
            )

    fallback = _Quick(_decision_raw())
    result = await Deliberation(
        router=_Router(),
        main_model=InvalidCorrectiveMain(),
        quick_recovery=fallback,
        recovery_mode="proposal_grammar",
    ).deliberate(
        _capsule(),
        attempt_id="attempt:fallback-after-corrective",
        budget=InteractiveTurnBudgetPolicy().start(),
    )

    assert result.proposal is not None
    assert result.proposal.proposal_kind == "decision"
    assert result.attempt_audits[0].model_call_id == "model-call:actual-failed-corrective"
    assert result.attempt_audits[0].request_hash == "f" * 64
    assert result.attempt_audits[0].slot == "corrective"
    assert result.attempt_audits[0].failure_code == "corrective_invalid"
    assert result.audit.model_id == "quick"
    assert result.audit.slot == "backup"
    assert result.audit.status == "main_invalid_recovered"
    assert result.audit.failure_code == "corrective_invalid"
    assert fallback.failure_codes == ["corrective_invalid"]


@pytest.mark.asyncio
async def test_fallback_after_failed_corrective_gets_one_bounded_reselection() -> None:
    class InvalidCorrectiveMain(_Main):
        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            assert claim_validation_corrective_provider_slot()
            return ModelOutput(
                model_id="primary-corrective",
                model_version="v1",
                raw_proposal={"bad": True},
            )

    class InvalidFallback(_Quick):
        async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
            self.requests.append(request)
            self.failure_codes.append(failure_code)
            assert claim_validation_corrective_provider_slot(allow_after_backup=True)
            assert not claim_validation_corrective_provider_slot(allow_after_backup=True)
            return ModelOutput(
                model_id="invalid-fallback",
                model_version="v1",
                raw_proposal={"still_bad": True},
            )

    main = InvalidCorrectiveMain()
    fallback = InvalidFallback()
    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=fallback,
        recovery_mode="proposal_grammar",
    ).deliberate(
        _capsule(),
        attempt_id="attempt:one-fallback-correction",
        budget=InteractiveTurnBudgetPolicy().start(),
    )

    assert result.proposal is None
    assert result.attempt_audits[0].slot == "corrective"
    assert result.attempt_audits[0].failure_code == "corrective_invalid"
    assert result.audit.slot == "backup"
    assert result.audit.status == "recovery_failed"
    assert result.audit.failure_code == "backup_invalid"
    assert len(main.requests) == len(fallback.requests) == 1


@pytest.mark.asyncio
async def test_primary_audit_uses_the_actual_winning_provider_invocation_identity() -> None:
    actual_call_id = "model-call:actual-primary-corrective"
    actual_request_hash = "c" * 64
    output = ModelOutput(
        model_id="corrected",
        model_version="v1",
        raw_proposal=_decision_raw(),
        winning_model_call_id=actual_call_id,
        winning_request_hash=actual_request_hash,
    )

    result = await Deliberation(
        router=_Router(),
        main_model=_Main(output),
        quick_recovery=_Quick(),
    ).deliberate(_capsule(), attempt_id="attempt:actual-primary-invocation")

    assert result.audit.model_call_id == actual_call_id
    assert result.audit.request_hash == actual_request_hash
    assert result.attempt_audits == (result.audit,)


@pytest.mark.asyncio
async def test_recovery_audit_uses_the_actual_winning_provider_invocation_identity() -> None:
    actual_call_id = "model-call:actual-backup-corrective"
    actual_request_hash = "d" * 64
    output = ModelOutput(
        model_id="backup-corrected",
        model_version="v1",
        raw_proposal=_minimal_raw(),
        winning_model_call_id=actual_call_id,
        winning_request_hash=actual_request_hash,
    )

    class ActualInvocationQuick(_Quick):
        async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
            self.requests.append(request)
            self.failure_codes.append(failure_code)
            return output

    result = await Deliberation(
        router=_Router(),
        main_model=_Main({"bad": True}),
        quick_recovery=ActualInvocationQuick(),
    ).deliberate(
        _capsule(),
        attempt_id="attempt:actual-backup-invocation",
    )

    assert result.audit.model_call_id == actual_call_id
    assert result.audit.request_hash == actual_request_hash
    assert result.attempt_audits[-1] == result.audit
    assert result.attempt_audits[0].model_call_id != actual_call_id


@pytest.mark.asyncio
@pytest.mark.parametrize("second_kind", ["recall", "backup"])
@pytest.mark.asyncio
async def test_validation_correction_may_use_one_bounded_slot_after_recall_or_recovery(
    second_kind: str,
) -> None:
    class RecallThenCorrectingMain(_Main):
        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            assert claim_secondary_provider_slot(second_kind)  # type: ignore[arg-type]
            assert claim_validation_corrective_provider_slot(
                allow_after_backup=second_kind == "backup"
            )
            assert not claim_validation_corrective_provider_slot()
            assert not claim_secondary_provider_slot("backup")
            return ModelOutput(
                model_id="recalled-and-corrected",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

    clock = _ManualClock()
    backup = _ControlledQuick()
    result = await Deliberation(
        router=_Router(),
        main_model=RecallThenCorrectingMain(),
        quick_recovery=backup,
    ).deliberate(
        _capsule(),
        attempt_id=f"attempt:post-{second_kind}-corrective",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=5.5,
            hedge_after_seconds=1.5,
            acceptance_dispatch_reserve_seconds=1.2,
            clock=clock,
            sleep=clock.sleep,
        ).start(),
    )

    assert result.audit.model_id == "recalled-and-corrected"
    assert result.audit.slot == "corrective"
    assert result.audit.outcome == "winner"
    assert backup.requests == []


@pytest.mark.asyncio
async def test_recovery_winner_with_nested_validation_reselection_is_a_corrective_slot() -> None:
    class CorrectingQuick(_Quick):
        async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
            self.requests.append(request)
            self.failure_codes.append(failure_code)
            assert claim_validation_corrective_provider_slot(allow_after_backup=True)
            return ModelOutput(
                model_id="backup-corrected",
                model_version="v1",
                raw_proposal=_minimal_raw(),
            )

    result = await Deliberation(
        router=_Router(),
        main_model=_Main({"bad": True}),
        quick_recovery=CorrectingQuick(),
    ).deliberate(
        _capsule(),
        attempt_id="attempt:backup-nested-corrective",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=5.5,
            hedge_after_seconds=1.5,
            acceptance_dispatch_reserve_seconds=1.2,
            clock=_ManualClock(),
        ).start(),
    )

    assert result.proposal is not None
    assert result.audit.model_id == "backup-corrected"
    assert result.audit.slot == "corrective"
    assert result.audit.outcome == "winner"


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
    # The primary's old deadline no longer erases the independent recovery
    # window.  The fallback is still rejected on its actual invalid result.
    assert result.attempt_audits[0].outcome == "invalid"
    assert result.attempt_audits[1].outcome == "invalid"
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
        expected_cursor=cursor,
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
async def test_ordered_presented_prefetch_union_extends_frozen_evidence() -> None:
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
                document_id="recall:prefetch:first",
                memory_kind="semantic",
                source_item_ref="fact:prefetch:first",
                source_slice="relevant_facts",
                source_refs=("event:source:first-prefetch",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="FactCommitted",
                        ref="event:source:first-prefetch",
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
    first = coordinator.prefetch(
        expected_cursor=cursor,
        query_text="tea preference",
        accessibility_seed="draw:test:first-prefetch",
        trigger_ref=capsule.trigger_ref,
    )
    later = coordinator.prefetch(
        expected_cursor=cursor,
        query_text="unrelated material",
        accessibility_seed="draw:test:later-prefetch",
        trigger_ref=capsule.trigger_ref,
    )
    assert first.audit.hits
    assert later.audit.hits == ()
    raw = _decision_raw(evidence_ref="event:source:first-prefetch")
    raw["evidence_refs"][0]["evidence_kind"] = "committed_fact"
    main = _Main(
        ModelOutput(
            model_id="main",
            model_version="v1",
            raw_proposal=raw,
            prefetch_trace=later,
            presented_prefetch_traces=(
                PresentedPrefetchTrace(
                    phase="initial",
                    model_call_id="model-call:prefetch-initial",
                    trace=first,
                ),
                PresentedPrefetchTrace(
                    phase="recall_followup",
                    model_call_id="model-call:prefetch-followup",
                    trace=later,
                ),
            ),
        )
    )

    result = await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
    ).deliberate(handle, attempt_id="attempt:ordered-prefetch")

    assert result.proposal is not None
    assert tuple(item.model_call_id for item in result.audit.presented_prefetch_traces) == (
        "model-call:prefetch-initial",
        "model-call:prefetch-followup",
    )
    assert result.audit.prefetch_trace is None


@pytest.mark.asyncio
async def test_trigger_message_reaches_model_only_when_bound_to_current_observation_evidence() -> (
    None
):
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
@pytest.mark.asyncio
async def test_invalid_main_output_recovers_without_accepting_unfrozen_claims(raw: object) -> None:
    quick = _Quick()
    result = await Deliberation(
        router=_Router(), main_model=_Main(raw), quick_recovery=quick
    ).deliberate(_capsule(), attempt_id="attempt:invalid")

    assert isinstance(result.proposal, MinimalProposal)
    assert result.audit.status == "main_invalid_recovered"
    assert result.audit.failure_code == "main_invalid_output"
    assert result.attempt_audits[0].status == "main_invalid"
    assert quick.failure_codes[0].startswith("main_invalid_output:")


@pytest.mark.asyncio
async def test_budgeted_invalid_main_passes_precise_diagnostic_to_recovery() -> None:
    quick = _Quick()

    result = await Deliberation(
        router=_Router(),
        main_model=_Main({"proposal_kind": "decision"}),
        quick_recovery=quick,
    ).deliberate(
        _capsule(),
        attempt_id="attempt:budgeted-invalid-detail",
        budget=InteractiveTurnBudgetPolicy(hedge_after_seconds=10.0).start(),
    )

    assert result.audit.status == "main_invalid_recovered"
    assert result.audit.failure_code == "primary_invalid"
    assert quick.failure_codes[0].startswith("main_invalid_output:ValidationError:")


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
@pytest.mark.asyncio
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
async def test_private_turn_state_validation_logs_only_structured_safe_metadata(caplog) -> None:
    secret = "PRIVATE-INNER-STATE-" + ("绝密" * 260)
    invalid = {
        "private_turn_state": {
            "inner_state_summary": secret,
            "attended_source_refs": [],
        },
        "timing_choice": "silent",
        "beats": [],
        "stance": "keep_private",
        "brief_rationale": "The role model chose silence.",
        "world_claims": [],
    }

    class InvalidPrivateStateProvider:
        model = "test-private-state-provider"

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            return json.dumps(invalid, ensure_ascii=False)

    observed = ProposalEvidenceRef(
        ref_id="observation:current:private-state",
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
        text="这句是当前消息。",
    )
    result = await Deliberation(
        router=_Router(),
        main_model=_ExpressionDraftWire(
            model=InvalidPrivateStateProvider(),
            expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        ),
        quick_recovery=_Quick(fail=True),
    ).deliberate(
        _capsule(),
        attempt_id="attempt:private-state-log-privacy",
        trigger_evidence=(_authority_evidence(), observed),
        trigger_message=current,
    )

    assert result.proposal is None
    assert "private_turn_state.string_too_long" in caplog.text
    assert "path=private_turn_state.inner_state_summary" in caplog.text
    assert "PRIVATE-INNER-STATE" not in caplog.text
    assert "绝密" not in caplog.text
    assert "input_value" not in caplog.text


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
async def test_close_detaches_provider_that_keeps_suppressing_cancellation() -> None:
    """Shutdown is bounded while a detached provider retains safe ownership."""

    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def cancellation_suppressing_provider() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
        raise RuntimeError("late provider failure")

    unit = Deliberation(router=_Router(), main_model=_Main(), quick_recovery=_Quick())
    with pytest.raises(TimeoutError):
        await unit._with_deadline(
            cancellation_suppressing_provider(),
            timeout=0.001,
            label="close-cancellation-suppressor",
            lane="main",
        )
    await asyncio.wait_for(cancellation_seen.wait(), timeout=1)

    closing = asyncio.create_task(unit.aclose())
    try:
        await asyncio.sleep(0.2)
        closed_with_provider_still_running = closing.done()
        assert unit.provider_health.main_inflight == 1
    finally:
        release.set()
        await asyncio.wait_for(closing, timeout=1)

    assert closed_with_provider_still_running is True
    for _ in range(10):
        if unit.provider_health.main_inflight == 0:
            break
        await asyncio.sleep(0)
    assert unit.provider_health.main_inflight == 0


@pytest.mark.asyncio
async def test_deadline_bounded_drain_preserves_nested_validation_failure_audit() -> None:
    technical = ValidationTechnicalFailure(
        "source_review_timeout",
        attempted_model_id="nested-source-reviewer",
        attempted_model_version="source-review.1",
    )

    async def nested_reviewer_cleanup() -> ModelOutput:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError as cancelled:
            # Production-shaped nested cancellation needs multiple scheduling
            # turns before the reviewer audit reaches the outer task.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            setattr(
                cancelled,
                "world_v2_validation_technical_failure",
                technical,
            )
            raise

    unit = Deliberation(
        router=_Router(),
        main_model=_Main(),
        quick_recovery=_Quick(),
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await unit._with_deadline(
            nested_reviewer_cleanup(),
            timeout=0.001,
            label="nested-source-review",
            lane="main",
        )

    assert caught.value is technical
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
    async with asyncio.timeout(0.1):
        while unit.expression_episode_diagnostics()["turns"] == 0:
            await asyncio.sleep(0)

    assert result.proposal is not None
    assert result.proposal.proposal_id == "proposal:decision:1"
    assert len(main.requests) == 1
    assert len(main.provisional_requests) == 1
    assert len(result.attempt_audits) == 1
    diagnostics = unit.expression_episode_diagnostics()
    assert diagnostics["mode"] == "shadow"
    assert diagnostics["turns"] == 1
    # Shadow is deliberately post-author observation: the authoritative full
    # lane must settle before the diagnostic candidate is allowed to run.
    assert diagnostics["full_first"] == 1
    assert diagnostics["provisional_first"] == 0
    assert diagnostics["slot_calls"] == 2


@pytest.mark.asyncio
async def test_blocked_shadow_observer_cannot_saturate_quick_recovery() -> None:
    class BlockingShadowMain(_EpisodeMain):
        def __init__(self) -> None:
            super().__init__()
            self.shadow_started = asyncio.Event()
            self.release_shadow = asyncio.Event()

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            if len(self.requests) == 2:
                raise ValueError("the second primary candidate is intentionally invalid")
            return ModelOutput(
                model_id="main",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

        async def propose_provisional(self, request: ModelInput) -> ModelOutput:
            self.provisional_requests.append(request)
            self.shadow_started.set()
            await self.release_shadow.wait()
            raw = _decision_raw()
            raw["proposal_id"] = "proposal:blocked-shadow-observer"
            return ModelOutput(
                model_id="shadow",
                model_version="v1",
                raw_proposal=raw,
            )

    main = BlockingShadowMain()
    quick = _Quick()
    unit = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
        expression_episode_mode="shadow",
    )
    budget_policy = InteractiveTurnBudgetPolicy(
        total_seconds=1.0,
        hedge_after_seconds=0.2,
        acceptance_dispatch_reserve_seconds=0.2,
    )

    first = await unit.deliberate(
        _capsule(),
        attempt_id="attempt:blocked-shadow:first",
        budget=budget_policy.start(),
    )
    await asyncio.wait_for(main.shadow_started.wait(), timeout=0.1)
    try:
        second = await unit.deliberate(
            _capsule(),
            attempt_id="attempt:blocked-shadow:second",
            budget=budget_policy.start(),
        )

        assert first.proposal is not None
        assert second.proposal is not None
        assert second.audit.status == "main_invalid_recovered"
        assert len(quick.requests) == 1
        assert unit.provider_health.quick_inflight == 0
        assert unit.provider_health.quick_circuit_open is False
    finally:
        main.release_shadow.set()
        async with asyncio.timeout(0.1):
            while unit.expression_episode_diagnostics()["turns"] == 0:
                await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_close_cancels_and_joins_a_running_shadow_observer(caplog) -> None:
    """Shutdown must not leave a diagnostic candidate using closed providers."""

    class BlockingShadowMain(_EpisodeMain):
        def __init__(self) -> None:
            super().__init__()
            self.shadow_started = asyncio.Event()
            self.shadow_cancelled = asyncio.Event()
            self.release_shadow = asyncio.Event()

        async def propose_provisional(self, request: ModelInput) -> ModelOutput:
            self.provisional_requests.append(request)
            self.shadow_started.set()
            try:
                await self.release_shadow.wait()
            except asyncio.CancelledError:
                self.shadow_cancelled.set()
                raise
            raise AssertionError("shutdown must cancel the shadow observer")

    main = BlockingShadowMain()
    unit = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
        expression_episode_mode="shadow",
    )

    result = await unit.deliberate(
        _capsule(),
        attempt_id="attempt:shadow-shutdown",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )
    assert result.proposal is not None
    await asyncio.wait_for(main.shadow_started.wait(), timeout=0.1)

    await unit.aclose()

    assert main.shadow_cancelled.is_set()
    assert unit.provider_health.main_inflight == 0
    assert unit.provider_health.quick_inflight == 0
    assert "deliberation candidate raised" not in caplog.text
    assert "ClosedResourceError" not in caplog.text


@pytest.mark.asyncio
async def test_source_reviewed_full_reply_keeps_shadow_episode_isolated() -> None:
    class _SourceReviewedEpisodeMain(_EpisodeMain):
        def __init__(self) -> None:
            super().__init__()
            self.source_review_finished = asyncio.Event()
            self.provisional_called = asyncio.Event()

        def source_closure_review_enabled(self) -> bool:
            return True

        async def propose(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            await asyncio.sleep(0.02)
            # Models the full author's independent truth-boundary review.  A
            # diagnostic shadow must not reserve this authoritative slot.
            assert claim_validation_corrective_provider_slot()
            self.source_review_finished.set()
            return ModelOutput(
                model_id="source-reviewed-full",
                model_version="v1",
                raw_proposal=_decision_raw(),
            )

        async def propose_provisional(self, request: ModelInput) -> ModelOutput:
            # Production uses the same fallback provider for source review and
            # shadow authorship.  Observation must not contend with the
            # authoritative review on the visible path.
            assert self.source_review_finished.is_set()
            self.provisional_called.set()
            return await super().propose_provisional(request)

    main = _SourceReviewedEpisodeMain()
    unit = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
        expression_episode_mode="shadow",
    )

    result = await unit.deliberate(
        _capsule(),
        attempt_id="attempt:source-reviewed-episode-shadow",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )
    await asyncio.wait_for(main.provisional_called.wait(), timeout=0.1)
    async with asyncio.timeout(0.1):
        while unit.expression_episode_diagnostics()["turns"] == 0:
            await asyncio.sleep(0)

    assert result.proposal is not None
    assert result.proposal.proposal_id == "proposal:decision:1"
    assert len(main.requests) == 1
    assert len(main.provisional_requests) == 1
    assert len(result.attempt_audits) == 1
    diagnostics = unit.expression_episode_diagnostics()
    assert diagnostics["mode"] == "shadow"
    assert diagnostics["turns"] == 1
    assert diagnostics["slot_calls"] == 2


def test_expression_episode_on_has_no_live_deliberation_entry() -> None:
    main = _EpisodeMain(full_delay=0.1)
    with pytest.raises(
        ValueError,
        match="expression episode mode must be off, shadow, or stream",
    ):
        Deliberation(
            router=_Router(),
            main_model=main,
            quick_recovery=_Quick(),
            expression_episode_mode="on",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_stream_head_failure_retires_original_tail_before_recovery_wins() -> None:
    class FailingStreamMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.tail_started = asyncio.Event()
            self.tail_cancelled = asyncio.Event()

        def stream_provider_available(self, _request: ModelInput) -> bool:
            return True

        async def propose_stream_head(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            await self.tail_started.wait()
            raise RuntimeError("stream head failed")

        async def propose_stream_tail(self, _request: ModelInput) -> ModelOutput:
            self.tail_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.tail_cancelled.set()
                raise

    class StreamHeadRecovery(_Quick):
        def __init__(self, main: FailingStreamMain) -> None:
            super().__init__()
            self._main = main

        async def recover_stream_head(
            self, request: ModelInput, failure_code: str
        ) -> ModelOutput:
            # Recovery must not race or inherit the rejected physical stream's
            # continuation.  Deliberation settles that tail first.
            assert self._main.tail_cancelled.is_set()
            self.requests.append(request)
            self.failure_codes.append(failure_code)
            return ModelOutput(
                model_id="stream-recovery",
                model_version="v1",
                raw_proposal=self.raw,  # type: ignore[arg-type]
            )

    main = FailingStreamMain()
    quick = StreamHeadRecovery(main)
    deliberation = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=quick,
        expression_episode_mode="stream",
    )

    result = await deliberation.deliberate(
        _capsule(),
        attempt_id="attempt:stream-head-failure-recovery",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            technical_recovery_seconds=0.4,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    assert result.proposal is not None
    assert result.audit.status == "main_exception_recovered"
    assert quick.failure_codes == ["main_exception"]
    assert main.tail_cancelled.is_set()
    assert not deliberation.has_expression_episode_tail(result.proposal.trigger_ref)
    assert [audit.status for audit in result.attempt_audits] == [
        "main_exception",
        "main_exception_recovered",
    ]
    await deliberation.aclose()


@pytest.mark.asyncio
async def test_new_attention_invalidates_tail_that_has_not_registered_yet() -> None:
    class RegistrationRaceMain(_Main):
        def __init__(self) -> None:
            super().__init__()
            self.head_started = asyncio.Event()
            self.tail_started = asyncio.Event()
            self.release_head = asyncio.Event()
            self.release_tail = asyncio.Event()
            self.tail_cancelled = asyncio.Event()

        def stream_provider_available(self, _request: ModelInput) -> bool:
            return True

        async def propose_stream_head(self, request: ModelInput) -> ModelOutput:
            self.requests.append(request)
            self.head_started.set()
            await self.release_head.wait()
            return ModelOutput(
                model_id="stream-head",
                model_version="v1",
                raw_proposal=_decision_raw(),
                episode_disposition="append",
            )

        async def propose_stream_tail(self, request: ModelInput) -> ModelOutput:
            self.tail_started.set()
            try:
                await self.release_tail.wait()
            except asyncio.CancelledError:
                self.tail_cancelled.set()
                raise
            raise AssertionError("stale stream tail must be cancelled before release")

    main = RegistrationRaceMain()
    deliberation = Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
        expression_episode_mode="stream",
    )
    turn = asyncio.create_task(
        deliberation.deliberate(
            _capsule(),
            attempt_id="attempt:stream-registration-race",
            budget=InteractiveTurnBudgetPolicy(
                total_seconds=1.0,
                hedge_after_seconds=0.2,
                acceptance_dispatch_reserve_seconds=0.2,
            ).start(),
        )
    )
    try:
        await asyncio.wait_for(main.head_started.wait(), timeout=0.1)
        await asyncio.wait_for(main.tail_started.wait(), timeout=0.1)

        # The newer inbound arrives before the old head has validated, so the
        # old continuation is not present in the process-local registry yet.
        await deliberation.cancel_superseded_expression_streams(
            "event:observation:newer-attention"
        )
        main.release_head.set()
        result = await asyncio.wait_for(turn, timeout=0.5)

        assert result.proposal is None
        assert result.audit.failure_code == "stream_superseded_by_newer_input"
        assert not deliberation.has_expression_episode_tail(_capsule().capsule.trigger_ref)
        await asyncio.wait_for(main.tail_cancelled.wait(), timeout=0.1)
    finally:
        main.release_head.set()
        main.release_tail.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await deliberation.aclose()


@pytest.mark.asyncio
async def test_completed_physical_stream_records_invalid_semantic_tail_as_completed() -> None:
    parent_call_id = "model-call:physical-stream"
    parent_request_hash = "a" * 64
    head_call_id = "model-call:semantic-head"
    tail_call_id = "model-call:semantic-tail"

    class InvalidTailMain(_Main):
        def stream_provider_available(self, _request: ModelInput) -> bool:
            return True

        async def propose_stream_head(self, request: ModelInput) -> ModelOutput:
            return ModelOutput(
                model_id="stream-role",
                model_version="v1",
                raw_proposal=_decision_raw(),
                winning_model_call_id=head_call_id,
                winning_request_hash=parent_request_hash,
                provider_parent_model_call_id=parent_call_id,
                semantic_stream_part="head",
                episode_disposition="append",
            )

        async def propose_stream_tail(self, request: ModelInput) -> ModelOutput:
            return ModelOutput(
                model_id="stream-role",
                model_version="v1",
                raw_proposal={"not": "an expression proposal"},
                winning_model_call_id=tail_call_id,
                winning_request_hash=parent_request_hash,
                provider_parent_model_call_id=parent_call_id,
                semantic_stream_part="tail",
                physical_provider_audits=(
                    PhysicalProviderInvocationAudit(
                        model_call_id=parent_call_id,
                        request_hash=parent_request_hash,
                        model_id="stream-role",
                        model_version="v1",
                        outcome="completed",
                        response_hash="b" * 64,
                        usage_status="unresolved",
                        semantic_model_call_ids=(head_call_id, tail_call_id),
                    ),
                ),
                episode_disposition="append",
            )

    deliberation = Deliberation(
        router=_Router(),
        main_model=InvalidTailMain(),
        quick_recovery=_Quick(),
        expression_episode_mode="stream",
    )
    result = await deliberation.deliberate(
        _capsule(),
        attempt_id="attempt:completed-invalid-tail",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    assert result.proposal is not None
    tail = await deliberation.await_expression_episode_tail(result.proposal.trigger_ref)
    assert tail is not None
    assert tail.failure_code == "invalid"
    assert tail.deliberation is not None
    audit = tail.deliberation.audit
    assert audit.status == "main_invalid"
    assert audit.failure_code == "main_invalid_output"
    assert audit.physical_provider_audits[0].outcome == "completed"
    await deliberation.aclose()


@pytest.mark.asyncio
async def test_corrected_stream_tail_keeps_original_physical_lineage_separate() -> None:
    parent_call_id = "model-call:physical-corrected-stream"
    parent_request_hash = "c" * 64
    head_call_id = "model-call:corrected-stream-head"
    tail_call_id = "model-call:" + sha256(
        json.dumps(
            {"provider_call_id": parent_call_id, "unit": "tail"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    correction_call_id = "model-call:corrected-stream-reselection"

    class CorrectedTailMain(_Main):
        def stream_provider_available(self, _request: ModelInput) -> bool:
            return True

        async def propose_stream_head(self, request: ModelInput) -> ModelOutput:
            return ModelOutput(
                model_id="stream-role",
                model_version="v1",
                raw_proposal=_decision_raw(),
                winning_model_call_id=head_call_id,
                winning_request_hash=parent_request_hash,
                provider_parent_model_call_id=parent_call_id,
                semantic_stream_part="head",
                episode_disposition="append",
            )

        async def propose_stream_tail(self, request: ModelInput) -> ModelOutput:
            return ModelOutput(
                model_id="stream-role",
                model_version="v1",
                raw_proposal=_decision_raw(),
                winning_model_call_id=correction_call_id,
                winning_request_hash="d" * 64,
                physical_provider_audits=(
                    PhysicalProviderInvocationAudit(
                        model_call_id=parent_call_id,
                        request_hash=parent_request_hash,
                        model_id="stream-role",
                        model_version="v1",
                        outcome="completed",
                        response_hash="e" * 64,
                        usage_status="unresolved",
                        semantic_model_call_ids=(head_call_id, tail_call_id),
                    ),
                ),
                episode_disposition="append",
            )

    deliberation = Deliberation(
        router=_Router(),
        main_model=CorrectedTailMain(),
        quick_recovery=_Quick(),
        expression_episode_mode="stream",
    )
    result = await deliberation.deliberate(
        _capsule(),
        attempt_id="attempt:corrected-stream-tail",
        budget=InteractiveTurnBudgetPolicy(
            total_seconds=1.0,
            hedge_after_seconds=0.2,
            acceptance_dispatch_reserve_seconds=0.2,
        ).start(),
    )

    assert result.proposal is not None
    tail = await deliberation.await_expression_episode_tail(result.proposal.trigger_ref)
    assert tail is not None
    assert tail.disposition == "append"
    assert tail.deliberation is not None
    assert tail.deliberation.proposal is not None
    original, corrected = tail.deliberation.attempt_audits
    assert original.model_call_id == tail_call_id
    assert original.status == "main_invalid"
    assert original.physical_provider_audits[0].outcome == "completed"
    assert corrected.model_call_id == correction_call_id
    assert corrected.status == "main_invalid_recovered"
    assert corrected.physical_provider_audits == ()
    await deliberation.aclose()


@pytest.mark.asyncio
async def test_superseded_tail_cancellation_join_is_bounded_and_observed() -> None:
    deliberation = Deliberation(
        router=_Router(),
        main_model=_Main(),
        quick_recovery=_Quick(),
        expression_episode_mode="stream",
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def cancellation_suppressing_tail() -> None:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    tail = asyncio.create_task(cancellation_suppressing_tail())
    deliberation._episode_tail_tasks["event:observation:old"] = tail  # type: ignore[assignment]
    await asyncio.wait_for(started.wait(), timeout=0.1)
    cancellation = asyncio.create_task(
        deliberation.cancel_superseded_expression_streams(
            "event:observation:newer-attention"
        )
    )
    returned_promptly = False
    try:
        done, _ = await asyncio.wait((cancellation,), timeout=0.05)
        returned_promptly = cancellation in done
        assert returned_promptly
        await cancellation
        assert cancelled.is_set()
        assert deliberation.shutdown_pending_task_count == 1

        release.set()
        await asyncio.wait_for(deliberation.wait_for_shutdown_quiescence(), timeout=0.1)
        assert deliberation.shutdown_pending_task_count == 0
    finally:
        release.set()
        if not returned_promptly:
            await asyncio.wait_for(cancellation, timeout=0.1)
        await deliberation.aclose()
