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
from companion_daemon.world_v2.http_capture_host import build_http_v2_capture_host
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host
from companion_daemon.world_v2.random_authority import RandomAuthority
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)
from companion_daemon.world_v2.character_interior.inbound_author import (
    _InboundCharacterAuthor as InboundCharacterAuthor,
)


NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


def test_public_composition_has_no_synchronous_emotion_gate() -> None:
    for callable_ in (
        build_qq_c2c_host,
        build_http_v2_capture_host,
        build_semantic_chat_composition,
        InboundCharacterAuthor,
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

    def __init__(self) -> None:
        self.calls = 0
        self.character_calls = 0

    async def complete(self, messages, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        self.calls += 1
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
        if "COMBINED OUTPUT ENVELOPE" in system:
            self.character_calls += 1
        expression = {
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
        }
        if "COMBINED OUTPUT ENVELOPE" not in system:
            return json.dumps(expression, ensure_ascii=False)
        joined = "\n".join(str(message.get("content", "")) for message in messages)
        appraisal = (
            {
                "appraise": True,
                "brief_rationale": "角色把这句话理解成了一次值得当下感受的难过。",
                "behavior_tendency": "先接住对方再消化感受",
                "stance": "在意",
                "display_strategy": "自然回应",
                "confidence": 7600,
                "affect": "open",
                "meanings": [
                    {"meaning": "disappointment", "confidence": 7600}
                ],
                "attribution": "user",
                "severity": 7200,
                "components": [
                    {"dimension": "sadness", "target_intensity_bp": 6400}
                ],
            }
            if "我今天有点难过" in joined
            else {
                "appraise": False,
                "brief_rationale": "This fixture needs no durable appraisal.",
                "behavior_tendency": "observe",
                "stance": "open",
                "display_strategy": "natural",
                "confidence": 3000,
            }
        )
        return json.dumps(
            {
                "appraisal_draft": appraisal,
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )

    async def complete_json_stream_with_usage(
        self,
        messages,  # type: ignore[no-untyped-def]
        *,
        temperature=0.8,  # type: ignore[no-untyped-def]
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ):
        raw = await self.complete(messages, temperature=temperature)
        if on_text_delta is not None:
            on_text_delta(raw)
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 1,
            "output_tokens": 1,
            "thinking_tokens": 0,
            "token_provenance": "offline_estimated",
            "transport": "offline_fixture",
            "provider": "fixture",
            "provider_usage_ref": "usage:fixture:fast-reply",
        }
        import hashlib

        material["provider_usage_hash"] = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return raw, material


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
        world_support_model=background,
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
        world_support_model=background,
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
async def test_text_endpoint_cannot_author_or_delay_same_turn_emotion(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint only predicts more typing; CharacterInterior owns emotion."""

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
            WORLD_V2_TEXT_ENDPOINT_ENABLED=True,
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

        diagnostics = await host.world_health_diagnostics()
        affect = diagnostics["mechanisms"]["affect"]
        # Appraisal/Affect were authored in the same InnerTurn as the visible
        # response and settled before inbound returned.  The retired local
        # appraisal endpoint never receives a semantic role call.
        assert infrastructure.appraisal_calls == 0
        assert affect["appraisal_count"] == 1
        assert affect["episode_count"] == 1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_same_turn_appraisal_settlement_retries_without_a_second_character_call(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CAS loss retries typed settlement, not the character author."""

    infrastructure = _LocalAppraisalInfrastructureModel()
    monkeypatch.setattr(
        semantic_chat_composition,
        "OpenAICompatibleChatModel",
        lambda **_kwargs: infrastructure,
    )
    original_process = AppraisalProposalWorker.process_rebased
    process_calls = 0

    def lose_first_acceptance(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal process_calls
        process_calls += 1
        if process_calls == 1:
            raise ImmediateEmotionConcurrencyConflict(stage="appraisal")
        return original_process(self, **kwargs)

    monkeypatch.setattr(AppraisalProposalWorker, "process_rebased", lose_first_acceptance)
    delivery = _Delivery()
    model = _FastReplyModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "appraisal-cas-reconsider.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=True,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
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

        assert process_calls >= 2
        assert model.character_calls == 1
        assert infrastructure.appraisal_calls == 0
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
    settings = Settings(
        database_path=tmp_path / "affect-acceptance-cas-recovery.sqlite",
        PRIMARY_USER_ID="geoff",
        WORLD_V2_TEXT_ENDPOINT_ENABLED=True,
    )
    host = build_qq_c2c_host(
        settings=settings,
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

        # The generic CharacterInterior proposal and its partial typed
        # settlement are the recovery journal. Prove that no process-local
        # cache or legacy Affect worker is required after a daemon restart.
        await host.aclose()
        host = build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=NOW,
            model=_FastReplyModel(),
            source_closure_model=infrastructure,
            delivery=_Delivery(),
            use_configured_recall_embedding=False,
        )
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
async def test_audited_inner_turn_continues_affect_after_two_cas_losses(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audited InnerTurn is the sole durable Affect continuation authority."""

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
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="shadow",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_FastReplyModel(),
        world_support_model=_FastReplyModel(),
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

        # Other bounded background continuations may consume a unit before
        # Affect under a loaded full-suite event loop. Keep the assertion on
        # the exact three CAS attempts, but allow enough scheduler slices for
        # the durable continuation to reach its third claim.
        for _ in range(8):
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
        legacy_affect_processes = tuple(
            item.event.payload()["process"]
            for item in replay.events
            if item.event.event_type == "TriggerProcessOpened"
            and item.event.payload()["process"]["process_kind"] == "affect_deliberation"
        )
        assert affect_conflicts == 3
        assert infrastructure.appraisal_calls == 0
        assert len(appraisal_events) == 1
        # Recovery reuses the same audited CharacterInterior result directly.
        # Opening the retired independent Affect-deliberation lifecycle here
        # would recreate a second semantic path and permit a second model call.
        assert legacy_affect_processes == ()
        assert affect["appraisal_count"] == 1
        assert affect["episode_count"] == 1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_appraisal_settlement_contention_reuses_the_audited_inner_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settlement race never re-enters PinnedTurn or invents a rejection."""

    infrastructure = _LocalAppraisalInfrastructureModel()
    monkeypatch.setattr(
        semantic_chat_composition,
        "OpenAICompatibleChatModel",
        lambda **_kwargs: infrastructure,
    )
    original_process = AppraisalProposalWorker.process_rebased
    process_calls = 0

    def lose_first_acceptance(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal process_calls
        process_calls += 1
        if process_calls == 1:
            raise ImmediateEmotionConcurrencyConflict(stage="appraisal")
        return original_process(self, **kwargs)

    original_audit = PinnedTurnCompiler.audit_observation
    audit_calls = 0

    async def forbid_second_character_call(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal audit_calls
        audit_calls += 1
        if audit_calls > 1:
            raise AssertionError("typed settlement must not call the character again")
        return await original_audit(self, **kwargs)

    monkeypatch.setattr(AppraisalProposalWorker, "process_rebased", lose_first_acceptance)
    monkeypatch.setattr(
        PinnedTurnCompiler,
        "audit_observation",
        forbid_second_character_call,
    )
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "appraisal-fresh-context-race.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=True,
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

        for _ in range(4):
            await host.scheduler_once(
                observed_at=NOW + timedelta(minutes=1),
                max_action_units=0,
                max_background_units=4,
            )
            diagnostics = await host.world_health_diagnostics()
            if diagnostics["mechanisms"]["affect"]["episode_count"] == 1:
                break
        replay = host.export_replay_evidence()
        assert audit_calls == 1
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
