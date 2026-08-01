from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import inspect
import json

import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2 import semantic_chat_composition
from companion_daemon.world_v2.affect_acceptance_runtime import AffectAcceptanceRuntime
from companion_daemon.world_v2.appraisal_proposal_worker import AppraisalProposalWorker
from companion_daemon.world_v2.affect_proposal_compiler import AffectProposalCompiler
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.immediate_emotion_proposal_worker import (
    ImmediateEmotionConcurrencyConflict,
)
from companion_daemon.world_v2.interaction_appraisal_trigger_runtime import (
    InteractionAppraisalTriggerRuntime,
)
from companion_daemon.world_v2.http_capture_host import build_http_v2_capture_host
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host
from companion_daemon.world_v2.random_authority import RandomAuthority
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)
from companion_daemon.world_v2.single_call_inbound_cognition import (
    SingleCallInboundCognition,
)


NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


def test_public_composition_has_no_synchronous_emotion_gate() -> None:
    for callable_ in (
        build_qq_c2c_host,
        build_http_v2_capture_host,
        build_semantic_chat_composition,
        SingleCallInboundCognition,
        WorldRuntime,
    ):
        assert "immediate_emotion_gate_model" not in inspect.signature(callable_).parameters
        assert "immediate_emotion_signal_gate" not in inspect.signature(callable_).parameters
        assert "immediate_emotion_semantic_gate" not in inspect.signature(callable_).parameters


class _Delivery:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.second_text_sent = asyncio.Event()

    async def send_text(self, _recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append(text)
        if len(self.sent) == 2:
            self.second_text_sent.set()
        return {"status": "ok", "data": {"message_id": str(len(self.sent))}}

    async def send_reaction(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the probe only expects text delivery")

    async def send_sticker(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the probe only expects text delivery")

    async def send_typing(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the probe only expects text delivery")


class _FastReplyModel:
    model = "fixture:fast-reply"
    semantic_authority_id = "semantic-authority:test:fast-reply"

    async def complete(self, messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        system = str(messages[0]["content"])
        if "candidate-external-proposition-inventory.3" in system:
            return json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.3",
                    "propositions": [
                        {
                            "locator": {
                                "beat_index": 0,
                                "char_start": 0,
                                "char_end": len("收到。"),
                                "text": "收到。",
                            },
                            "semantic_role": "nonassertive_content",
                            "parent_index": None,
                        }
                    ],
                }
            )
        return json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": ("我注意到对方在等回应，现在想直接接住这句话。"),
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [{"modality": "text", "text": "收到。"}],
                "stance": "open",
                "brief_rationale": "Reply to the current message.",
                "confidence": 8_000,
                "world_claims": [],
            },
            ensure_ascii=False,
        )


class _BlockingBackgroundModel:
    model = "fixture:blocking-background"
    semantic_authority_id = "semantic-authority:test:blocking-background"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        self.started.set()
        await self.release.wait()
        system = str(messages[0]["content"])
        if "already verified user Fact" in system:
            return '{"retain":false}'
        if "Assess one verified user message" in system:
            return '{"retain":false}'
        if "immediate inner appraisal" in system:
            return '{"appraise":false,"affect":"no_change"}'
        return '{"decision":"no_change"}'


class _FastInfrastructureModel:
    model = "fixture:fast-infrastructure"
    semantic_authority_id = "semantic-authority:test:fast-infrastructure"

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in {
            "report-relative-entailment-adjudication.3",
            "source-closure-review.7",
        }

    async def complete(self, messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        system = str(messages[0]["content"])
        if "Audit only factual source closure" in system:
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "No unsupported factual claim.",
                }
            )
        raise AssertionError(f"unexpected infrastructure prompt: {system[:120]}")


class _LocalAppraisalInfrastructureModel(_FastInfrastructureModel):
    def __init__(self) -> None:
        self.appraisal_calls = 0

    async def complete(self, messages, **kwargs) -> str:  # type: ignore[no-untyped-def]
        system = str(messages[0]["content"])
        complete_prompt = "\n".join(str(item.get("content", "")) for item in messages)
        if (
            "relationship signal for a virtual companion" in complete_prompt
            or "RelationshipEvaluationDraft" in complete_prompt
        ):
            return '{"decision":"no_change"}'
        if system.startswith("你为上下文中的角色写一份私人的、可出错的当下评价"):
            self.appraisal_calls += 1
            return json.dumps(
                {
                    "appraise": True,
                    "brief_rationale": "角色把这句话理解成了一次关系上的失望。",
                    "behavior_tendency": "先消化失落",
                    "stance": "在意但保留",
                    "display_strategy": "暂时克制",
                    "confidence": 7600,
                    "meaning": "disappointment",
                    "attribution": "user",
                    "severity": 7200,
                    "open_affect": True,
                    "affect_dimension": "sadness",
                    "affect_target_intensity_bp": 6400,
                },
                ensure_ascii=False,
            )
        return await super().complete(messages, **kwargs)


class _FastQuickReactionModel:
    model = "fixture:fast-quick-reaction"

    async def complete(self, _messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        return '{"react":false}'


@pytest.mark.asyncio
async def test_slow_background_model_does_not_hold_the_inbound_world_lock(tmp_path) -> None:
    background = _BlockingBackgroundModel()
    infrastructure = _FastInfrastructureModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "background-nonblocking.sqlite",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        advisory_model=background,
        source_closure_model=infrastructure,
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    background_task: asyncio.Task[object] | None = None
    try:
        first = await host.inbound_text(
            message_id="message:one",
            recipient_id="10001",
            text="你好",
            observed_at=NOW,
        )
        background_task = asyncio.create_task(
            host.scheduler_once(
                observed_at=NOW + timedelta(minutes=1),
                max_action_units=0,
                max_background_units=1,
            )
        )
        await asyncio.wait_for(background.started.wait(), timeout=2)

        started = asyncio.get_running_loop().time()
        # The API fixtures return immediately, so the public inbound path must
        # keep its entire non-provider hot path inside the 500ms production
        # target even while background cognition is indefinitely blocked.
        second = await asyncio.wait_for(
            host.inbound_text(
                message_id="message:two",
                recipient_id="10001",
                text="还在吗？",
                observed_at=NOW + timedelta(minutes=1),
            ),
            timeout=3,
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert first.status == second.status == "action_authorized"
        assert elapsed < 0.5
        assert delivery.sent == ["收到。", "收到。"]
        assert not background_task.done()
    finally:
        background.release.set()
        if background_task is not None:
            await asyncio.wait_for(background_task, timeout=5)
        await host.aclose()


@pytest.mark.asyncio
async def test_regular_host_drain_does_not_hold_the_inbound_world_lock(tmp_path) -> None:
    """The public adapter drain must have the same non-blocking guarantee.

    ``scheduler_once`` has its own lock-free path, so testing it alone would
    miss the production HTTP/QQ ``drain`` entrypoint that is commonly called
    by a passive scheduler task.
    """

    background = _BlockingBackgroundModel()
    infrastructure = _FastInfrastructureModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "background-nonblocking-drain.sqlite",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        advisory_model=background,
        source_closure_model=infrastructure,
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    drain_task: asyncio.Task[object] | None = None
    second_task: asyncio.Task[object] | None = None
    try:
        first = await host.inbound_text(
            message_id="message:one",
            recipient_id="10001",
            text="你好",
            observed_at=NOW,
        )
        drain_task = asyncio.create_task(host.drain(max_action_units=0, max_background_units=1))
        await asyncio.wait_for(background.started.wait(), timeout=2)

        # Observe causal progress instead of treating one wall-clock sample as
        # a microbenchmark. The 100ms packet-coalescing floor plus the separate
        # endpoint/listening opportunity and local
        # SQLite work normally lands near 340ms, but process scheduling noise
        # is unrelated to whether ``drain`` owns the World lock.  If it does,
        # this delivery event cannot fire until ``background.release`` is set.
        second_task = asyncio.create_task(
            host.inbound_text(
                message_id="message:two",
                recipient_id="10001",
                text="还在吗？",
                observed_at=NOW + timedelta(minutes=1),
            )
        )
        await asyncio.wait_for(delivery.second_text_sent.wait(), timeout=3)

        assert not drain_task.done()
        assert not background.release.is_set()
        second = await asyncio.wait_for(second_task, timeout=1)
        assert first.status == second.status == "action_authorized"
        assert delivery.sent == ["收到。", "收到。"]
        # The intentional local wait has durable, segment-level evidence and
        # can be asserted without conflating it with host scheduler load.  The
        # recorder's origin/segment arithmetic is covered independently in
        # ``test_production_latency_trace.py``; repeated end-to-end percentiles
        # belong to ``test_production_performance_evidence.py``.
        coalescing_samples = tuple(
            sample for sample in host.latency_samples() if sample.segment == "coalescing"
        )
        assert len(coalescing_samples) == 2
        assert all(sample.duration_ms == 100.0 for sample in coalescing_samples)
    finally:
        background.release.set()
        if drain_task is not None:
            await asyncio.wait_for(drain_task, timeout=5)
        if second_task is not None and not second_task.done():
            second_task.cancel()
            await asyncio.gather(second_task, return_exceptions=True)
        await host.aclose()


@pytest.mark.asyncio
async def test_production_keeps_persistent_emotion_appraisal_off_the_visible_reply_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listening local endpoint may have lost its generation worker.

    The current message and PrivateTurnState still give the role model same-turn
    emotional agency. Persisting an Appraisal/Affect result is separate durable
    work, so a half-dead local endpoint must not even be consulted before the
    visible role-model call.
    """

    infrastructure = _LocalAppraisalInfrastructureModel()
    monkeypatch.setattr(
        semantic_chat_composition,
        "OpenAICompatibleChatModel",
        lambda **_kwargs: infrastructure,
    )
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "half-dead-local-gate.sqlite",
            PRIMARY_USER_ID="geoff",
            LOCAL_APPRAISAL_ENABLED=True,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        source_closure_model=infrastructure,
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        started = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(
            host.inbound_text(
                message_id="message:half-dead-gate",
                recipient_id="10001",
                text="我今天有点难过。",
                observed_at=NOW,
            ),
            timeout=3,
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert result.status == "action_authorized"
        assert elapsed < 0.5
        assert delivery.sent == ["收到。"]
        assert infrastructure.appraisal_calls == 0

        # Background ordering is intentionally concurrent with the already
        # accepted Action receipt. Drain a bounded number of scheduler passes
        # and assert the durable eventual state, not which worker happened to
        # win the first two-unit slice.
        for _ in range(4):
            await host.scheduler_once(
                observed_at=NOW + timedelta(minutes=1),
                max_action_units=0,
                max_background_units=2,
            )
            diagnostics = await host.world_health_diagnostics()
            affect = diagnostics["mechanisms"]["affect"]
            if affect["episode_count"] == 1:
                break
        # Action receipt and another source-bound background commit can each
        # invalidate a cursor before Appraisal acceptance.  The bounded fresh
        # reconsiderations are valid; duplicate accepted state is not.
        assert 1 <= infrastructure.appraisal_calls <= 3
        assert affect["appraisal_count"] == 1
        assert affect["episode_count"] == 1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_background_appraisal_reconsiders_after_a_real_cas_loss(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent Action receipt must not permanently consume emotion work.

    The first typed Appraisal attempt is deliberately made stale at its
    acceptance seam.  Recovery must ask the same role model for a fresh
    cursor-pinned private judgment, then accept exactly one Appraisal/Affect
    pair.  A CAS loss is technical contention, not evidence that the role
    chose ``no_change`` and not an advisory-validation rejection.
    """

    infrastructure = _LocalAppraisalInfrastructureModel()
    monkeypatch.setattr(
        semantic_chat_composition,
        "OpenAICompatibleChatModel",
        lambda **_kwargs: infrastructure,
    )
    original_process = AppraisalProposalWorker.process
    process_calls = 0

    def lose_first_acceptance(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal process_calls
        process_calls += 1
        if process_calls == 1:
            raise ImmediateEmotionConcurrencyConflict(stage="appraisal")
        return original_process(self, **kwargs)

    monkeypatch.setattr(AppraisalProposalWorker, "process", lose_first_acceptance)
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "appraisal-cas-reconsider.sqlite",
            PRIMARY_USER_ID="geoff",
            LOCAL_APPRAISAL_ENABLED=True,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        source_closure_model=infrastructure,
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        result = await host.inbound_text(
            message_id="message:appraisal-cas-reconsider",
            recipient_id="10001",
            text="我今天有点难过。",
            observed_at=NOW,
        )
        assert result.status == "action_authorized"

        for _ in range(4):
            await host.scheduler_once(
                observed_at=NOW + timedelta(minutes=1),
                max_action_units=0,
                max_background_units=4,
            )
            diagnostics = await host.world_health_diagnostics()
            affect = diagnostics["mechanisms"]["affect"]
            if affect["episode_count"] == 1:
                break

        # The accepted Appraisal may immediately open relationship advisory
        # work on this shared fixture model, so count only the lower bound
        # proving the original and fresh cursor-pinned Appraisal calls.
        assert infrastructure.appraisal_calls >= 2
        assert process_calls >= 2
        assert affect["appraisal_count"] == 1
        assert affect["episode_count"] == 1
        replay = host.export_replay_evidence()
        assert not any(
            item.event.event_type == "AdvisoryAcceptanceRejected"
            for item in replay.events
        )
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_pending_affect_candidate_recovers_after_acceptance_cas_loss(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate committed before a CAS loss must remain acceptably pinned.

    This reproduces the production race where an Action receipt advances the
    head after Affect candidate compilation but before Affect acceptance. The
    model-authored source choice remains reusable; recovery must revalidate and
    materialize it at the fresh head rather than repeatedly retrying its
    historical compile cursor.
    """

    infrastructure = _LocalAppraisalInfrastructureModel()
    monkeypatch.setattr(
        semantic_chat_composition,
        "OpenAICompatibleChatModel",
        lambda **_kwargs: infrastructure,
    )
    original_accept = AffectAcceptanceRuntime.accept_runtime_owned
    affect_acceptance_calls = 0

    def lose_first_affect_acceptance(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal affect_acceptance_calls
        affect_acceptance_calls += 1
        if affect_acceptance_calls == 1:
            projection = self.ledger.project()
            RandomAuthority(ledger=self.ledger).draw(
                attempt_id="attempt:test:post-affect-candidate-race",
                candidate_refs=("candidate:test:head-advance",),
                catalog_version="test.affect-acceptance-race.1",
                logical_time=projection.logical_time,
                actor="test:concurrent-writer",
                trace_id="trace:test:affect-acceptance-race",
                correlation_id="correlation:test:affect-acceptance-race",
            )
            raise ConcurrencyConflict("test injected post-candidate head advance")
        return original_accept(self, **kwargs)

    monkeypatch.setattr(
        AffectAcceptanceRuntime,
        "accept_runtime_owned",
        lose_first_affect_acceptance,
    )
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "affect-acceptance-cas-recovery.sqlite",
            PRIMARY_USER_ID="geoff",
            LOCAL_APPRAISAL_ENABLED=True,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        source_closure_model=infrastructure,
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    try:
        result = await host.inbound_text(
            message_id="message:affect-acceptance-cas-recovery",
            recipient_id="10001",
            text="我今天有点难过。",
            observed_at=NOW,
        )
        assert result.status == "action_authorized"

        await host.drain(
            max_action_units=0,
            max_background_units=8,
        )

        diagnostics = await host.world_health_diagnostics()
        affect = diagnostics["mechanisms"]["affect"]
        replay = host.export_replay_evidence()
        affect_candidates = tuple(
            item.event
            for item in replay.events
            if item.event.event_type == "ProposalRecorded"
            and item.event.payload().get("authority_contract_ref")
            == "affect-proposal-compiler.1"
        )
        candidate_revisions_by_source: dict[str, list[int]] = {}
        for event in affect_candidates:
            payload = event.payload()
            source_audit = payload.get("source_audit")
            assert isinstance(source_audit, dict)
            source_ref = source_audit.get("proposal_event_ref")
            assert isinstance(source_ref, str)
            candidate_revisions_by_source.setdefault(source_ref, []).append(
                payload["evaluated_world_revision"]
            )
        rebased_revision_sets = tuple(
            revisions
            for revisions in candidate_revisions_by_source.values()
            if len(revisions) >= 2
        )
        assert affect_acceptance_calls >= 2
        assert len(rebased_revision_sets) == 1
        assert len(set(rebased_revision_sets[0])) == len(rebased_revision_sets[0])
        assert affect["appraisal_count"] == 1
        assert affect["episode_count"] == 1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_claimed_appraisal_continues_affect_after_two_cas_losses(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable Appraisal must keep its unfinished Affect continuation runnable."""

    infrastructure = _LocalAppraisalInfrastructureModel()
    monkeypatch.setattr(
        semantic_chat_composition,
        "OpenAICompatibleChatModel",
        lambda **_kwargs: infrastructure,
    )
    original_record_rebased = AffectProposalCompiler.record_rebased
    affect_conflicts = 0

    def lose_two_affect_writes(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal affect_conflicts
        affect_conflicts += 1
        if affect_conflicts <= 2:
            raise ConcurrencyConflict("test injected affect head advance")
        return original_record_rebased(self, **kwargs)

    monkeypatch.setattr(
        AffectProposalCompiler,
        "record_rebased",
        lose_two_affect_writes,
    )
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "claimed-appraisal-affect-continuation.sqlite",
            PRIMARY_USER_ID="geoff",
            LOCAL_APPRAISAL_ENABLED=True,
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="shadow",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        advisory_model=_FastReplyModel(),
        source_closure_model=infrastructure,
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    try:
        result = await host.inbound_text(
            message_id="message:claimed-appraisal-affect-continuation",
            recipient_id="10001",
            text="我今天有点难过。",
            observed_at=NOW,
        )
        assert result.status == "action_authorized"

        for _ in range(4):
            await host.scheduler_once(
                observed_at=NOW + timedelta(minutes=1),
                max_action_units=0,
                max_background_units=2,
            )
            diagnostics = await host.world_health_diagnostics()
            affect = diagnostics["mechanisms"]["affect"]
            if affect["episode_count"] == 1:
                break

        replay = host.export_replay_evidence()
        appraisal_events = tuple(
            item.event
            for item in replay.events
            if item.event.event_type == "AppraisalAccepted"
        )
        affect_processes = tuple(
            item.event.payload()["process"]
            for item in replay.events
            if item.event.event_type == "TriggerProcessOpened"
            and item.event.payload()["process"]["process_kind"] == "affect_deliberation"
        )
        assert affect_conflicts == 3
        # This fixture model is shared with downstream background consumers,
        # so its raw call counter can include one relationship-triggered
        # reconsideration in addition to the initial/cursor-raced appraisal.
        # The durable event/process assertions below are the lane-specific
        # effect-once proof; the shared provider work must still stay bounded.
        assert 1 <= infrastructure.appraisal_calls <= 3
        assert len(appraisal_events) == 1
        assert len(affect_processes) == 1
        assert affect_processes[0]["source_evidence_ref"] == appraisal_events[0].event_id
        assert affect["appraisal_count"] == 1
        assert affect["episode_count"] == 1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_fresh_appraisal_context_head_race_stays_technical_not_semantic(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver/head race must leave retryable work, not reject her appraisal."""

    infrastructure = _LocalAppraisalInfrastructureModel()
    monkeypatch.setattr(
        semantic_chat_composition,
        "OpenAICompatibleChatModel",
        lambda **_kwargs: infrastructure,
    )
    original_process = AppraisalProposalWorker.process
    process_calls = 0

    def lose_first_acceptance(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal process_calls
        process_calls += 1
        if process_calls == 1:
            raise ImmediateEmotionConcurrencyConflict(stage="appraisal")
        return original_process(self, **kwargs)

    original_audit = PinnedTurnCompiler.audit_observation
    audit_calls = 0
    context_race = False

    async def fail_fresh_context(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal audit_calls, context_race
        if not self._affect_target_bounds_enabled:  # noqa: SLF001
            return await original_audit(self, **kwargs)
        audit_calls += 1
        if audit_calls == 2:
            context_race = True
            raise ValueError("head advanced while resolving fresh context")
        return await original_audit(self, **kwargs)

    original_project = InteractionAppraisalTriggerRuntime._project

    async def observe_newer_head(self):  # type: ignore[no-untyped-def]
        nonlocal context_race
        projection = await original_project(self)
        if not context_race:
            return projection
        context_race = False
        return projection.model_copy(
            update={
                "world_revision": projection.world_revision + 1,
                "ledger_sequence": projection.ledger_sequence + 1,
            }
        )

    monkeypatch.setattr(AppraisalProposalWorker, "process", lose_first_acceptance)
    monkeypatch.setattr(PinnedTurnCompiler, "audit_observation", fail_fresh_context)
    monkeypatch.setattr(
        InteractionAppraisalTriggerRuntime,
        "_project",
        observe_newer_head,
    )
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "appraisal-fresh-context-race.sqlite",
            PRIMARY_USER_ID="geoff",
            LOCAL_APPRAISAL_ENABLED=True,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        source_closure_model=infrastructure,
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    try:
        result = await host.inbound_text(
            message_id="message:appraisal-fresh-context-race",
            recipient_id="10001",
            text="我今天有点难过。",
            observed_at=NOW,
        )
        assert result.status == "action_authorized"

        # The scheduler may surface the classified contention, but it must not
        # durably reinterpret it as a semantic advisory rejection.
        await host.scheduler_once(
            observed_at=NOW + timedelta(minutes=1),
            max_action_units=0,
            max_background_units=4,
        )
        replay = host.export_replay_evidence()
        assert audit_calls >= 3
        assert not any(
            item.event.event_type == "AdvisoryAcceptanceRejected"
            for item in replay.events
        )
        diagnostics = await host.world_health_diagnostics()
        affect = diagnostics["mechanisms"]["affect"]
        assert affect["appraisal_count"] == 1
        assert affect["episode_count"] == 1
    finally:
        await host.aclose()
