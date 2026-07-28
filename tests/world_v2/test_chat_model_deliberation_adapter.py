from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
import threading
import time

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
    CompanionIdentityFrame,
    RoutedChatModelDeliberationAdapter,
    companion_identity_source_ref,
)
from companion_daemon.world_v2.expression_draft import (
    QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    ModelUsageProvenance,
    TriggerMessage,
)
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallSourceBinding,
)
from companion_daemon.world_v2.recall_audit import CharacterRecallRequest
from companion_daemon.world_v2.recall_corpus import RecallCorpusSources
from companion_daemon.world_v2.recall_runtime import (
    RecallCoordinator,
    perform_character_recall,
    verify_trusted_recall_trace,
)


def _request() -> ModelInput:
    return ModelInput(
        call_id="call:1",
        attempt_id="attempt:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:1",
        evaluated_world_revision=3,
        model_content_json='{"capsule":"authoritative"}',
    )


class _Model:
    model = "deepseek-v4-flash"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append((messages, temperature))
        return self._reply


class _MeteredModel(_Model):
    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        self.calls.append((messages, temperature))
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 12,
            "output_tokens": 3,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "fake-provider",
            "provider_usage_ref": "usage:fake:1",
        }
        digest = sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self._reply, ModelUsageProvenance(**material, provider_usage_hash=digest)


class _JsonModel(_Model):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        raise AssertionError("structured proposal path must request JSON mode when available")

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        self.calls.append((messages, temperature))
        return self._reply


class _SequenceJsonModel(_Model):
    def __init__(self, replies: list[str]) -> None:
        super().__init__("")
        self._replies = list(replies)

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        self.calls.append((messages, temperature))
        return self._replies.pop(0)


class _BlockingPrefetchEmbedding:
    version = "blocking-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.used_after_close = threading.Event()
        self._delegate = FeatureHashRecallEmbedding()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if texts == ("并行预取",):
            self.started.set()
            self.release.wait(timeout=2)
        if self.closed.is_set():
            self.used_after_close.set()
        return self._delegate.embed(texts)

    def close(self) -> None:
        self.closed.set()


class _ObservablePrefetchEmbedding:
    version = "observable-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self.finished = threading.Event()
        self._delegate = FeatureHashRecallEmbedding()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        result = self._delegate.embed(texts)
        if texts == ("窗边听雨",):
            self.finished.set()
        return result


class _MalformedPrefetchEmbedding:
    version = "malformed-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self._delegate = FeatureHashRecallEmbedding()
        self.calls = 0

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        if texts == ("损坏的查询",):
            return ()
        return self._delegate.embed(texts)


class _DelayedSemanticPrefetchEmbedding:
    version = "delayed-semantic-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self._delegate = FeatureHashRecallEmbedding()
        self.calls = 0

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        if texts == ("稍晚完成的语义查询",):
            time.sleep(0.45)
        return self._delegate.embed(texts)


@pytest.mark.asyncio
async def test_character_may_pull_one_source_bound_recall_before_deciding() -> None:
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "recall_request": {
                        "query_text": "凤凰单丛",
                        "memory_kinds": ["semantic"],
                        "limit": 3,
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [],
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "你前两天提到凤凰单丛，后来泡得怎么样？",
                        }
                    ],
                    "stance": "curious",
                    "brief_rationale": "I chose to follow the recalled topic.",
                    "world_claims": [
                        {
                            "claim_text": "对方前两天提到凤凰单丛",
                            "scope": "counterpart_history",
                            "source_refs": ["event:fact:tea"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=2,
        ledger_sequence=5,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:fact:tea",
                memory_kind="semantic",
                source_item_ref="fact:tea",
                source_slice="relevant_facts",
                source_refs=("event:fact:tea",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="FactCommitted",
                        ref="event:fact:tea",
                        source_world_revision=2,
                        immutable_hash="c" * 64,
                    ),
                ),
                source_world_revision=2,
                text="我最近开始用盖碗泡凤凰单丛。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    request = _qq_request().model_copy(
        update={
            "evaluated_deliberation_revision": 2,
            "evaluated_ledger_sequence": 5,
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 2,
                    "ledger_sequence": 5,
                    "logical_time": "2026-07-27T12:00:00+00:00",
                    "slices": {},
                },
                ensure_ascii=False,
            ),
        }
    )
    coordinator.schedule_prefetch(
        query_text=request.trigger_message.text,
        accessibility_seed="recall-prefetch:trigger:1:5",
        trigger_ref=request.trigger_ref,
    )

    output = await ChatModelDeliberationAdapter(
        model=model,
        recall_coordinator=coordinator,
    ).propose(request)

    assert len(model.calls) == 3
    assert output.recall_trace is not None
    assert output.prefetch_trace is not None
    trace = verify_trusted_recall_trace(output.recall_trace)
    assert trace.request.query_text == "凤凰单丛"
    assert trace.query.accessibility_seed.startswith("character-recall:")
    assert trace.query.actor_ref == "agent:companion"
    assert trace.query.subject_refs == ("agent:companion", "user:primary")
    assert trace.hits[0].document.source_refs == ("event:fact:tea",)
    assert trace.hits[0].dense_score_bp >= 0
    assert "凤凰单丛" in model.calls[1][0][-1]["content"]
    assert "parallel_attention_prefetch" in model.calls[1][0][-1]["content"]
    assert any("parallel_attention_prefetch" in item["content"] for item in model.calls[2][0])
    assert output.raw_proposal["timing_choice"] == "now"
    recalled_evidence = next(
        item for item in output.raw_proposal["evidence_refs"] if item["ref_id"] == "event:fact:tea"
    )
    assert recalled_evidence["immutable_hash"] == "sha256:" + "c" * 64


@pytest.mark.asyncio
async def test_parallel_prefetch_cannot_delay_a_first_pass_final_answer() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:parallel",
        memory_kind="semantic",
        source_item_ref="fact:parallel",
        source_slice="relevant_facts",
        source_refs=("event:parallel",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:parallel",
                source_world_revision=2,
                immutable_hash="d" * 64,
            ),
        ),
        source_world_revision=2,
        text="这是一条可丢弃的预取候选。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        trigger_ref="trigger:1",
    )
    coordinator.schedule_prefetch(
        query_text="并行预取",
        accessibility_seed="draw:parallel-prefetch",
        trigger_ref="trigger:1",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "这句我直接回答。"}],
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    try:
        output = await asyncio.wait_for(
            ChatModelDeliberationAdapter(
                model=model,
                recall_coordinator=coordinator,
            ).propose(_qq_request()),
            timeout=0.5,
        )
    finally:
        embedding.release.set()
        coordinator.close()

    assert len(model.calls) == 1
    assert output.prefetch_trace is None
    assert output.recall_trace is None


@pytest.mark.asyncio
async def test_ready_parallel_prefetch_is_visible_in_first_pass_and_audited() -> None:
    embedding = _ObservablePrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:first-pass",
        memory_kind="episodic",
        source_item_ref="experience:first-pass",
        source_slice="recent_experiences",
        source_refs=("event:first-pass",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="ExperienceCommitted",
                ref="event:first-pass",
                source_world_revision=2,
                immutable_hash="e" * 64,
            ),
        ),
        source_world_revision=2,
        text="她之前在窗边听完了那场雨。",
        actor_ref="agent:companion",
        subject_refs=("agent:companion",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="private",
    )
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        trigger_ref="trigger:1",
    )
    coordinator.schedule_prefetch(
        query_text="窗边听雨",
        accessibility_seed="draw:first-pass-prefetch",
        trigger_ref="trigger:1",
    )
    assert await asyncio.to_thread(embedding.finished.wait, 0.5)
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "刚才那阵雨让我想起一件事。"}],
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 0,
                    "ledger_sequence": 0,
                    "logical_time": "2026-07-27T12:00:00+00:00",
                    "slices": {},
                },
                ensure_ascii=False,
            )
        }
    )
    output = await ChatModelDeliberationAdapter(
        model=model,
        recall_coordinator=coordinator,
    ).propose(request)

    assert len(model.calls) == 1
    assert output.prefetch_trace is not None
    assert output.recall_trace is None
    assert "她之前在窗边听完了那场雨" in model.calls[0][0][1]["content"]
    trace = verify_trusted_recall_trace(output.prefetch_trace)
    assert trace.mode == "prefetch"
    assert trace.trigger_ref == "trigger:1"
    assert trace.hits[0].document.source_refs == ("event:first-pass",)


@pytest.mark.asyncio
async def test_first_pass_does_not_wait_for_remote_semantic_query_latency() -> None:
    semantic = _DelayedSemanticPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:delayed-semantic",
        memory_kind="semantic",
        source_item_ref="fact:delayed-semantic",
        source_slice="relevant_facts",
        source_refs=("event:delayed-semantic",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:delayed-semantic",
                source_world_revision=2,
                immutable_hash="a" * 64,
            ),
        ),
        source_world_revision=2,
        text="稍晚完成的语义查询仍应进入首轮上下文。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    primary = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    primary.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=primary,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=semantic,
        trigger_ref="trigger:delayed-semantic",
    )
    coordinator.schedule_prefetch(
        query_text="稍晚完成的语义查询",
        accessibility_seed="draw:delayed-semantic",
        trigger_ref="trigger:delayed-semantic",
    )

    started = time.monotonic()
    first_pass = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:delayed-semantic",
    )
    elapsed = time.monotonic() - started
    trace = None
    for _ in range(40):
        trace = coordinator.take_ready_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref="trigger:delayed-semantic",
        )
        if trace is not None:
            break
        await asyncio.sleep(0.01)
    coordinator.close()

    assert first_pass is not None
    assert (
        verify_trusted_recall_trace(first_pass)
        .hits[0]
        .document.source_item_ref
        == "fact:delayed-semantic"
    )
    assert elapsed < 0.4
    assert trace is not None
    assert (
        verify_trusted_recall_trace(trace).hits[0].document.source_item_ref
        == "fact:delayed-semantic"
    )
    assert semantic.calls > 0


@pytest.mark.asyncio
async def test_blocked_prefetch_is_daemonized_and_close_remains_bounded() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:blocked-close",
    )
    coordinator.schedule_prefetch(
        query_text="并行预取",
        accessibility_seed="draw:blocked-close",
        trigger_ref="trigger:blocked-close",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        coordinator.close()
        elapsed = loop.time() - started
        assert not embedding.closed.is_set()
    finally:
        embedding.release.set()

    assert elapsed < 0.1
    assert await asyncio.to_thread(embedding.closed.wait, 0.5)
    assert not embedding.used_after_close.is_set()


@pytest.mark.asyncio
async def test_close_tracks_prefetch_after_timeout_removed_its_future() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:popped-close",
    )
    coordinator.schedule_prefetch(
        query_text="并行预取",
        accessibility_seed="draw:popped-close",
        trigger_ref="trigger:popped-close",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    assert (
        await coordinator.consume_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref="trigger:popped-close",
            timeout_seconds=0.01,
        )
        is None
    )
    started = asyncio.get_running_loop().time()
    try:
        coordinator.close()
        assert asyncio.get_running_loop().time() - started < 0.1
        assert not embedding.closed.is_set()
    finally:
        embedding.release.set()

    assert await asyncio.to_thread(embedding.closed.wait, 0.5)
    assert not embedding.used_after_close.is_set()


def test_closed_coordinator_cannot_publish_a_new_prefetch_worker() -> None:
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        trigger_ref="trigger:closed-prefetch",
    )
    coordinator.close()

    with pytest.raises(RuntimeError, match="coordinator is closed"):
        coordinator.schedule_prefetch(
            query_text="不该开始",
            accessibility_seed="draw:closed-prefetch",
            trigger_ref="trigger:closed-prefetch",
        )


@pytest.mark.asyncio
async def test_close_defers_embedding_shutdown_until_deep_recall_finishes() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:blocked-deep-recall",
    )
    task = asyncio.create_task(
        perform_character_recall(
            coordinator,
            request=CharacterRecallRequest(query_text="并行预取", limit=2),
            accessibility_seed="draw:blocked-deep-recall",
            expected_cursor=cursor,
            trigger_ref="trigger:blocked-deep-recall",
            timeout_seconds=1.0,
        )
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    started = asyncio.get_running_loop().time()
    try:
        coordinator.close()
        assert asyncio.get_running_loop().time() - started < 0.1
        assert not embedding.closed.is_set()
    finally:
        embedding.release.set()

    await task
    assert await asyncio.to_thread(embedding.closed.wait, 0.5)
    assert not embedding.used_after_close.is_set()


@pytest.mark.asyncio
async def test_first_pass_timeout_preserves_prefetch_and_only_joins_once() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:preserved",
        memory_kind="semantic",
        source_item_ref="fact:preserved",
        source_slice="relevant_facts",
        source_refs=("event:preserved",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:preserved",
                source_world_revision=2,
                immutable_hash="f" * 64,
            ),
        ),
        source_world_revision=2,
        text="并行预取完成后仍应可见。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:preserved",
    )
    coordinator.schedule_prefetch(
        query_text="并行预取",
        accessibility_seed="draw:preserved",
        trigger_ref="trigger:preserved",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)

    fallback = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:preserved",
        timeout_seconds=0.01,
    )
    assert fallback is not None
    assert verify_trusted_recall_trace(fallback).hits
    loop = asyncio.get_running_loop()
    started = loop.time()
    repeated_fallback = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:preserved",
        timeout_seconds=0.2,
    )
    assert repeated_fallback == fallback
    assert loop.time() - started < 0.05

    embedding.release.set()
    trace = None
    for _ in range(50):
        trace = coordinator.take_ready_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref="trigger:preserved",
        )
        if trace is not None:
            break
        await asyncio.sleep(0.01)
    coordinator.close()

    assert trace is not None
    assert verify_trusted_recall_trace(trace).hits[0].document.source_item_ref == "fact:preserved"


@pytest.mark.asyncio
async def test_prefetch_capacity_saturation_keeps_source_bound_local_fallback() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:capacity-fallback",
        memory_kind="semantic",
        source_item_ref="fact:capacity-fallback",
        source_slice="relevant_facts",
        source_refs=("event:capacity-fallback",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:capacity-fallback",
                source_world_revision=2,
                immutable_hash="c" * 64,
            ),
        ),
        source_world_revision=2,
        text="并行预取饱和时仍保留本地回忆。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
    )
    for index_number in range(5):
        coordinator.schedule_prefetch(
            query_text="并行预取",
            accessibility_seed=f"draw:capacity:{index_number}",
            trigger_ref=f"trigger:capacity:{index_number}",
        )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)

    fallback = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:capacity:4",
        timeout_seconds=0.01,
    )
    assert fallback is not None
    audit = verify_trusted_recall_trace(fallback)
    assert audit.hits[0].document.source_item_ref == "fact:capacity-fallback"
    assert audit.embedding_status != "ready"
    health = coordinator.semantic_health()
    assert health["last_prefetch_failure_code"] == "prefetch_capacity"
    assert health["turn_summary"]["hot_context"] == "ready"
    assert health["turn_summary"]["recall"] == "degraded"
    assert health["turn_summary"]["hits"] == 1
    assert health["turn_summary"]["fallback_channels"] == ["lexical"]
    assert (
        health["turn_summary"]["character_outcome"]
        == "reported_by_turn_application"
    )

    embedding.release.set()
    coordinator.close()


@pytest.mark.asyncio
async def test_automatic_prefetch_uses_the_configured_semantic_lane() -> None:
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:malformed",
        memory_kind="semantic",
        source_item_ref="fact:malformed",
        source_slice="relevant_facts",
        source_refs=("event:malformed",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:malformed",
                source_world_revision=2,
                immutable_hash="e" * 64,
            ),
        ),
        source_world_revision=2,
        text="损坏的查询仍共享词面。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    base = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    base.rebuild(cursor=cursor, documents=(document,))
    semantic = _MalformedPrefetchEmbedding()
    coordinator = RecallCoordinator.from_built_index(
        index=base,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=semantic,
        trigger_ref="trigger:malformed",
    )
    coordinator.schedule_prefetch(
        query_text="损坏的查询",
        accessibility_seed="draw:malformed",
        trigger_ref="trigger:malformed",
    )

    trace = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:malformed",
        timeout_seconds=0.5,
    )
    coordinator.close()

    assert trace is not None
    audit = verify_trusted_recall_trace(trace)
    assert audit.hits[0].document.source_item_ref == "fact:malformed"
    assert audit.embedding_status == "degraded"
    assert semantic.calls > 0
    health = coordinator.semantic_health()
    assert health["last_prefetch_status"] == "degraded"
    assert health["last_prefetch_hit_count"] == 1
    assert "lexical" in health["last_prefetch_match_channels"]


def test_character_recall_uses_older_pinned_context_after_newer_refresh() -> None:
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=2,
        ledger_sequence=5,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:fact:tea",
                memory_kind="semantic",
                source_item_ref="fact:tea",
                source_slice="relevant_facts",
                source_refs=("event:fact:tea",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="FactCommitted",
                        ref="event:fact:tea",
                        source_world_revision=2,
                        immutable_hash="c" * 64,
                    ),
                ),
                source_world_revision=2,
                text="我最近开始用盖碗泡凤凰单丛。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    coordinator.refresh(
        cursor=RecallCursor(
            world_revision=4,
            deliberation_revision=3,
            ledger_sequence=6,
        ),
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        sources=RecallCorpusSources(),
    )

    trace = verify_trusted_recall_trace(
        coordinator.recall(
            request=CharacterRecallRequest(query_text="凤凰单丛"),
            accessibility_seed="draw:older-context",
            expected_cursor=cursor,
            trigger_ref="trigger:older-context",
        )
    )

    assert trace.index_cursor == cursor
    assert trace.hits[0].document.source_item_ref == "fact:tea"


class _RaisingModel(_Model):
    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del messages, temperature
        raise KeyError("reviewer fixture has no review contract")


@pytest.mark.asyncio
async def test_prompt_models_a_mutually_established_future_continuation_as_optional_expectation() -> (
    None
):
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "好，晚点见。"}],
                "stance": "leave_the_thread_open",
                "brief_rationale": "The counterpart explicitly plans to return.",
                "response_expectation": {
                    "hoped_response": "对方忙完后回来继续聊天",
                    "pressure_bp": 1000,
                    "importance_bp": 5000,
                    "wait_seconds": 600,
                    "expires_after_seconds": 21600,
                },
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(model=model)
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "我先忙，晚点聊。"}
            )
        }
    )

    output = await adapter.propose(request)

    system = model.calls[0][0][0]["content"]
    assert "genuinely expect a reply" in system
    assert "对方忙完后回来继续聊天" in json.dumps(output.raw_proposal, ensure_ascii=False)


@pytest.mark.asyncio
async def test_pending_expectation_is_assessed_inside_the_normal_inbound_cognition() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "哈哈，听起来确实不太对你胃口。"}],
                "stance": "receive_the_answer",
                "brief_rationale": "The current message directly answers the earlier question.",
                "world_claims": [],
                "response_expectation_assessment": {
                    "status": "fulfilled",
                    "reason": "The counterpart directly said whether the trip was enjoyable.",
                },
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-26T01:07:00+00:00",
                    "slices": {
                        "advisories": {
                            "items": [
                                {
                                    "value": {
                                        "kind": "response_expectation",
                                        "summary": "hoped for how the trip went",
                                    }
                                }
                            ]
                        }
                    },
                }
            ),
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "深圳说实话不是很好玩哈哈哈哈"}
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(request)

    assert output.raw_proposal["response_expectation_assessment"] == {
        "status": "fulfilled",
        "reason": "The counterpart directly said whether the trip was enjoyable.",
    }
    assert "same cognition" in model.calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_missing_expectation_assessment_does_not_discard_a_valid_reply() -> None:
    missing = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我接住你这句。"}],
            "stance": "receive_the_answer",
            "brief_rationale": "Respond to the current message.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    model = _SequenceJsonModel([missing, missing])
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-26T01:07:00+00:00",
                    "slices": {
                        "advisories": {
                            "items": [
                                {
                                    "value": {
                                        "kind": "response_expectation",
                                        "summary": "hoped for an answer",
                                    }
                                }
                            ]
                        }
                    },
                }
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(request)

    assert output.raw_proposal.get("response_expectation_assessment") is None
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_quick_recovery_keeps_reply_when_expectation_assessment_is_missing() -> None:
    missing = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我接住你这句。"}],
            "stance": "receive_the_answer",
            "brief_rationale": "Recover the visible reply.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    model = _JsonModel(missing)
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-26T01:07:00+00:00",
                    "slices": {
                        "advisories": {
                            "items": [
                                {
                                    "value": {
                                        "kind": "response_expectation",
                                        "summary": "hoped for an answer",
                                    }
                                }
                            ]
                        }
                    },
                }
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(model=model).recover(request, "main_attempt_failed")

    assert output.raw_proposal["proposal_kind"] == "minimal"
    assert output.raw_proposal.get("response_expectation_assessment") is None


@pytest.mark.asyncio
async def test_quick_recovery_preserves_a_valid_expectation_assessment() -> None:
    recovered = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "嗯，你这句已经回答我了。"}],
            "stance": "receive_the_answer",
            "brief_rationale": "Recover the reply and preserve the semantic judgement.",
            "world_claims": [],
            "response_expectation_assessment": {
                "status": "fulfilled",
                "reason": "The current message directly answers the open question.",
            },
        },
        ensure_ascii=False,
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-26T01:07:00+00:00",
                    "slices": {
                        "advisories": {
                            "items": [
                                {
                                    "value": {
                                        "kind": "response_expectation",
                                        "summary": "hoped for an answer",
                                    }
                                }
                            ]
                        }
                    },
                }
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(model=_JsonModel(recovered)).recover(
        request, "main_attempt_failed"
    )

    assert output.raw_proposal["proposal_kind"] == "minimal"
    assert output.raw_proposal["response_expectation_assessment"] == {
        "status": "fulfilled",
        "reason": "The current message directly answers the open question.",
    }


@pytest.mark.asyncio
async def test_future_continuation_remains_the_models_choice() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "等你回来再说。"}],
                "stance": "leave_the_thread_open",
                "brief_rationale": "Accept the counterpart's pause.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "我先忙，晚点聊。"}
            )
        }
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(request)

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["response_expectation"] is None


@pytest.mark.asyncio
async def test_paraphrased_mutual_resume_intent_normalizes_without_one_fixed_sentence() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "行，等你忙完我们接着说。"}],
                "stance": "hold_the_topic_lightly",
                "brief_rationale": "Keep a future continuation open.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "我得先处理点事，忙完回来继续聊。"}
            )
        }
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(request)

    assert (
        '"response_expectation":null'
        in output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "reply"),
    [
        ("我先走啦，改天见。", "好，拜拜。"),
        ("晚安，明天见。", "晚安。"),
        ("我先忙。", "好，你先忙。"),
        ("我先忙，晚点聊。", "好，拜拜。"),
    ],
)
async def test_generic_farewell_or_one_sided_pause_does_not_create_response_gap(
    trigger: str, reply: str
) -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": reply}],
                "stance": "close_for_now",
                "brief_rationale": "Do not establish a mutual continuation.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(update={"text": trigger})
        }
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(request)

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["response_expectation"] is None


@pytest.mark.asyncio
async def test_adapter_keeps_chat_model_output_inert_and_binds_request_to_prompt() -> None:
    model = _Model('{"proposal_id":"proposal:1"}')
    adapter = ChatModelDeliberationAdapter(model=model)

    output = await adapter.propose(_request())

    assert output.model_id == "deepseek-v4-flash"
    assert output.raw_proposal == {"proposal_id": "proposal:1"}
    messages, temperature = model.calls[0]
    assert temperature == 0.7
    assert "ExpressionDraft" in messages[0]["content"]
    supplied = json.loads(messages[1]["content"])
    assert supplied["request"]["trigger_ref"] == "trigger:1"
    assert supplied["request"]["evaluated_world_revision"] == 3


@pytest.mark.asyncio
async def test_chat_prompt_keeps_values_but_omits_capsule_proof_noise() -> None:
    noisy_context = json.dumps(
        {
            "world_id": "world:test",
            "actor_ref": "agent:companion",
            "trigger_ref": "event:message:2",
            "world_revision": 9,
            "logical_time": "2026-07-17T00:00:00+00:00",
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "source_refs": ["event:acceptance:1"],
                    "source_hash": "a" * 64,
                    "resolver_proof": {"large": "x" * 4_000},
                    "items": [
                        {
                            "item_ref": "dialogue:user:1",
                            "privacy_class": "private",
                            "source_hash": "b" * 64,
                            "value_hash": "c" * 64,
                            "source_bindings": [{"ref": "event:acceptance:1", "hash": "d" * 64}],
                            "value": {"speaker": "user", "text": "你刚才有点敷衍。"},
                        }
                    ],
                }
            },
        },
        ensure_ascii=False,
    )
    model = _Model('{"proposal_id":"proposal:1"}')
    request = _request().model_copy(update={"model_content_json": noisy_context})

    await ChatModelDeliberationAdapter(model=model).propose(request)

    supplied = json.loads(model.calls[0][0][1]["content"])
    compact = json.loads(supplied["request"]["model_content_json"])
    dialogue = compact["slices"]["recent_dialogue"]
    assert dialogue["items"][0]["value"]["text"] == "你刚才有点敷衍。"
    assert dialogue["items"][0]["source_ref"] == "dialogue:user:1"
    assert "resolver_proof" not in dialogue
    assert len(json.dumps(compact, ensure_ascii=False)) < len(noisy_context) // 4


@pytest.mark.asyncio
async def test_adapter_composes_provider_usage_with_the_same_completion() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_MeteredModel('{"proposal_id":"proposal:metered"}')
    )

    output = await adapter.propose(_request())

    assert output.input_tokens == 12
    assert output.output_tokens == 3
    assert output.usage is not None
    assert output.usage.route_class == "chat"
    assert output.usage.token_provenance == "provider_reported"


@pytest.mark.asyncio
async def test_adapter_requests_provider_json_mode_when_available() -> None:
    adapter = ChatModelDeliberationAdapter(model=_JsonModel('{"proposal_id":"proposal:json"}'))

    output = await adapter.propose(_request())

    assert output.raw_proposal == {"proposal_id": "proposal:json"}


@pytest.mark.asyncio
async def test_identity_frame_carries_personality_boundaries_and_world_claim_discipline() -> None:
    model = _Model('{"proposal_id":"proposal:persona"}')
    adapter = ChatModelDeliberationAdapter(
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
            stable_identity_facts=("汉语言文学专业",),
            personality_frame="慢热，有自己的判断，不无条件附和。",
            values=("真诚比漂亮话重要",),
            speech_frame="中文短句，像私聊。",
            style_rules=("想知道的时候才问",),
            boundaries=("不编造真实线下行动证据",),
        ),
    )

    await adapter.propose(_request())

    system = model.calls[0][0][0]["content"]
    assert all(
        value in system
        for value in ("沈知栀", "慢热", "真诚比漂亮话重要", "不编造真实线下行动证据")
    )
    assert "copy exact matching source_refs" in system


@pytest.mark.asyncio
async def test_private_identity_frame_exposes_one_exact_auditable_source_ref() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        relationship_frame="刚认识",
        stable_identity_facts=("汉语言文学专业",),
    )
    source_ref = companion_identity_source_ref(identity)
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我叫沈知栀，学的是汉语言文学。"}],
                "stance": "introduce_myself",
                "brief_rationale": "Answer with my configured identity.",
                "world_claims": [
                    {
                        "claim_text": "我叫沈知栀，学的是汉语言文学",
                        "scope": "stable_identity",
                        "source_refs": [source_ref],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(model=model, identity_frame=identity)

    output = await adapter.propose(_qq_request())

    payload = output.raw_proposal["proposed_changes"][0]["payload"]
    decoded = json.loads(payload["canonical_json"])
    assert decoded["world_claims"] == [
        {
            "claim_text": "我叫沈知栀，学的是汉语言文学",
            "scope": "stable_identity",
            "source_refs": [source_ref],
        }
    ]
    system = model.calls[0][0][0]["content"]
    assert source_ref in system
    assert "stable_identity claims supported by this frame" in system


@pytest.mark.asyncio
async def test_private_identity_frame_rejects_a_forged_source_ref_after_one_retry() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        relationship_frame="刚认识",
        stable_identity_facts=("汉语言文学专业",),
    )
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我在成都长大。"}],
                "stance": "invent_background",
                "brief_rationale": "Use an unsupported identity detail.",
                "world_claims": [
                    {
                        "claim_text": "我在成都长大",
                        "scope": "stable_identity",
                        "source_refs": ["private_identity_frame"],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(model=model, identity_frame=identity)

    with pytest.raises(ValueError, match="semantic source lane"):
        await adapter.propose(_qq_request())

    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_identity_prompt_keeps_companion_identity_stable_when_challenged() -> None:
    model = _Model('{"proposal_id":"proposal:persona"}')
    adapter = ChatModelDeliberationAdapter(
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
    )

    await adapter.propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "independent person" in system
    assert "Keep companion and counterpart identities distinct" in system


@pytest.mark.asyncio
async def test_identity_prompt_resolves_topic_references_before_defending_self_identity() -> None:
    model = _Model('{"proposal_id":"proposal:topic-reference"}')
    adapter = ChatModelDeliberationAdapter(
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
    )

    await adapter.propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "Keep companion and counterpart identities distinct" in system


def _identity_review(
    *,
    decision: str,
    replacement_text: str | None = None,
    addresses_counterpart_as_companion_name: bool = False,
    contains_counterpart_fact_premise: bool = False,
    premise_source_refs: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        {
            "decision": decision,
            "replacement_text": replacement_text,
            "addresses_counterpart_as_companion_name": addresses_counterpart_as_companion_name,
            "contains_counterpart_fact_premise": contains_counterpart_fact_premise,
            "premise_source_refs": list(premise_source_refs),
            "brief_reason": "Review first-contact identity and counterpart premises.",
        },
        ensure_ascii=False,
    )


def _source_closure_review(
    *,
    decision: str,
    unsupported_claim_indexes: tuple[int, ...] = (),
    undeclared_fact_fragments: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        {
            "decision": decision,
            "unsupported_claim_indexes": list(unsupported_claim_indexes),
            "undeclared_fact_fragments": list(undeclared_fact_fragments),
            "brief_reason": "Check semantic support and subject attribution.",
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_source_closure_repairs_companion_hometown_used_as_user_premise() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        relationship_frame="已经聊过一阵",
        stable_identity_facts=("来自嘉兴",),
    )
    main = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "家里那边怎么了？嘉兴最近天气不太好吗？",
                        }
                    ],
                    "stance": "concerned",
                    "brief_rationale": "Ask what happened.",
                    "confidence": 7600,
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "家里那边怎么了？"}],
                    "stance": "concerned",
                    "brief_rationale": "Ask without inventing a location.",
                    "confidence": 7600,
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                decision="unsupported",
                undeclared_fact_fragments=("嘉兴最近天气不太好吗？",),
            ),
            _source_closure_review(decision="supported"),
        ]
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": "event:older-dialogue",
                                    "value": {
                                        "speaker": "counterpart",
                                        "text": "前面聊过别的事情。",
                                    },
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=main,
        identity_frame=identity,
        source_closure_reviewer=reviewer,
    ).propose(request)

    rendered = json.dumps(output.raw_proposal, ensure_ascii=False)
    assert "家里那边怎么了？" in rendered
    assert "嘉兴" not in rendered
    assert len(main.calls) == 2
    assert len(reviewer.calls) == 2


@pytest.mark.asyncio
async def test_source_closure_fails_closed_when_character_correction_is_still_unsupported() -> None:
    bad = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "嘉兴最近天气不太好吗？"}],
            "stance": "guess",
            "brief_rationale": "Assume the user's location.",
            "confidence": 7000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    main = _SequenceJsonModel([bad, bad])
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                decision="unsupported",
                undeclared_fact_fragments=("嘉兴最近天气不太好吗？",),
            ),
            _source_closure_review(
                decision="unsupported",
                undeclared_fact_fragments=("嘉兴最近天气不太好吗？",),
            ),
        ]
    )

    with pytest.raises(ValueError, match="semantic source closure rejected"):
        await ChatModelDeliberationAdapter(
            model=main,
            source_closure_reviewer=reviewer,
        ).propose(_qq_request())

    assert len(main.calls) == 2
    assert len(reviewer.calls) == 2


@pytest.mark.asyncio
async def test_first_contact_review_replaces_self_name_as_counterpart_and_invented_user_premises() -> (
    None
):
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "嗨，沈知栀。你是群里那个在成都的？"}],
                "stance": "open_with_a_guess",
                "brief_rationale": "Start from an assumed shared context.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            _identity_review(
                decision="replace",
                replacement_text="嗨，刚认识。你平时喜欢聊些什么？",
            )
        ]
    )
    adapter = ChatModelDeliberationAdapter(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(_qq_request())

    assert "嗨，刚认识。你平时喜欢聊些什么？" in json.dumps(output.raw_proposal, ensure_ascii=False)
    review_input = json.loads(reviewer.calls[0][0][1]["content"])
    assert review_input["companion_name"] == "沈知栀"
    assert review_input["counterpart_name"] == "geoff"
    assert review_input["allowed_source_refs"] == ["observation:qq:1"]


@pytest.mark.asyncio
async def test_first_contact_review_removes_an_unsupported_counterpart_location_premise() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你在成都住得还习惯吗？"}],
                "stance": "ask_about_an_assumed_location",
                "brief_rationale": "Assume a location not supplied by the counterpart.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            _identity_review(
                decision="replace",
                replacement_text="你平时更喜欢待在家，还是出去逛？",
            )
        ]
    )
    adapter = ChatModelDeliberationAdapter(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(_qq_request())

    rendered = json.dumps(output.raw_proposal, ensure_ascii=False)
    assert "你平时更喜欢待在家，还是出去逛？" in rendered
    assert "成都" not in rendered


@pytest.mark.asyncio
async def test_first_contact_review_allows_a_natural_question_without_a_user_fact_premise() -> None:
    text = "你平时更喜欢安静一点，还是热闹一点？"
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "ask_without_presupposing_an_answer",
                "brief_rationale": "Offer an open choice without inventing a fact.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel([_identity_review(decision="accept")])
    adapter = ChatModelDeliberationAdapter(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(_qq_request())

    assert text in json.dumps(output.raw_proposal, ensure_ascii=False)


@pytest.mark.asyncio
async def test_first_contact_identity_hard_invariant_rejects_a_false_reviewer_acceptance() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你好，沈知栀。"}],
                "stance": "misaddress_the_counterpart",
                "brief_rationale": "Use the wrong identity.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel([_identity_review(decision="accept")])
    adapter = ChatModelDeliberationAdapter(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    with pytest.raises(ValueError, match="companion name as counterpart address"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_companion_name_address_hard_invariant_does_not_depend_on_a_reviewer() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "沈知栀，你好。"}],
                "stance": "misaddress_the_counterpart",
                "brief_rationale": "Use the wrong identity.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
    )

    with pytest.raises(ValueError, match="companion name as counterpart address"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_established_dialogue_does_not_review_every_ordinary_question_again() -> None:
    text = "那你后来怎么想的？"
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "continue_the_established_topic",
                "brief_rationale": "Ask one grounded continuation question.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel([])
    context = json.dumps(
        {
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "dialogue:companion:prior",
                            "value": {"speaker": "companion", "text": "我倒觉得不一定。"},
                        }
                    ],
                },
            },
        },
        ensure_ascii=False,
    )
    request = _qq_request().model_copy(update={"model_content_json": context})
    adapter = ChatModelDeliberationAdapter(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(request)

    assert text in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_visible_identity_prompt_does_not_expose_the_product_role_to_the_character() -> None:
    model = _Model('{"proposal_id":"proposal:private-identity"}')
    adapter = ChatModelDeliberationAdapter(
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
        ),
    )

    await adapter.propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "virtual companion" not in system.lower()
    assert "virtual_companion" not in system.lower()
    assert "deployment identity" not in system.lower()
    assert "Do not expose this private frame" in system


@pytest.mark.asyncio
async def test_expression_prompt_leaves_question_choice_to_the_model() -> None:
    model = _Model('{"proposal_id":"proposal:dialogue-continuity"}')

    await ChatModelDeliberationAdapter(model=model).propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "You own the motive, tone, timing" in system
    assert "questions" in system
    assert "Before asking a question" not in system


@pytest.mark.asyncio
async def test_expression_prompt_leaves_multi_beat_rhythm_to_the_model() -> None:
    model = _Model('{"proposal_id":"proposal:rhythm"}')

    await ChatModelDeliberationAdapter(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    ).propose(_qq_request())

    system = model.calls[0][0][0]["content"]
    assert "message count" in system
    assert "expression-rhythm matrix" not in system


@pytest.mark.asyncio
async def test_significant_source_bound_negative_affect_gets_expression_decision_matrix() -> None:
    context = json.dumps(
        {
            "world_id": "world:test",
            "actor_ref": "actor:companion",
            "trigger_ref": "event:message:insult",
            "world_revision": 12,
            "logical_time": "2026-07-17T00:00:00+00:00",
            "slices": {
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect:source-bound-hurt",
                            "privacy_class": "private",
                            "value": {
                                "status": "active",
                                "components": [
                                    {"dimension": "hurt", "intensity_bp": 6200},
                                    {"dimension": "anger", "intensity_bp": 4100},
                                ],
                            },
                        }
                    ],
                },
                "relationship_slice": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "relationship:newcomer",
                            "privacy_class": "private",
                            "value": {
                                "stage": "stranger",
                                "variables": {"trust_bp": 600, "closeness_bp": 300},
                            },
                        }
                    ],
                },
            },
        },
        ensure_ascii=False,
    )
    model = _Model('{"proposal_id":"proposal:negative-expression"}')
    request = _request().model_copy(
        update={
            "model_content_json": context,
            "trigger_message": TriggerMessage(
                event_ref="event:message:insult",
                event_payload_hash="sha256:" + "d" * 64,
                observation_ref="observation:insult",
                source_world_revision=12,
                actor="user:primary",
                channel="test",
                reply_target="user:primary",
                text="你说话让我觉得很不舒服。",
            ),
        }
    )

    await ChatModelDeliberationAdapter(model=model).propose(request)

    supplied = json.loads(model.calls[0][0][1]["content"])
    assert "affect_expression_matrix" not in supplied
    assert "affect_episodes" in supplied["request"]["model_content_json"]
    provider_context = json.loads(supplied["request"]["model_content_json"])
    assert provider_context["current_self_state"]["affect"][0]["source_ref"] == (
        "affect:source-bound-hurt"
    )


@pytest.mark.asyncio
async def test_minor_or_positive_affect_does_not_trigger_the_negative_expression_floor() -> None:
    context = json.dumps(
        {
            "slices": {
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect:small-mixed",
                            "value": {
                                "status": "active",
                                "components": [
                                    {"dimension": "hurt", "intensity_bp": 900},
                                    {"dimension": "warmth", "intensity_bp": 8000},
                                ],
                            },
                        }
                    ],
                }
            }
        }
    )
    model = _Model('{"proposal_id":"proposal:minor-affect"}')

    await ChatModelDeliberationAdapter(model=model).propose(
        _request().model_copy(update={"model_content_json": context})
    )

    supplied = json.loads(model.calls[0][0][1]["content"])
    assert "affect_expression_matrix" not in supplied


@pytest.mark.asyncio
async def test_quick_recovery_uses_lower_temperature_and_accepts_fenced_json() -> None:
    model = _Model('```json\n{"proposal_id":"proposal:quick"}\n```')
    adapter = ChatModelDeliberationAdapter(model=model, temperature=1.1)

    output = await adapter.recover(_request(), "main_timeout")

    assert output.raw_proposal == {"proposal_id": "proposal:quick"}
    messages, temperature = model.calls[0]
    assert temperature == 0.25
    assert "recovery attempt" in messages[0]["content"].lower()
    assert json.loads(messages[1]["content"])["quick_recovery_failure"] == "main_timeout"


@pytest.mark.asyncio
async def test_adapter_rejects_non_object_or_malformed_model_output() -> None:
    for reply in ("not json", "[]", "```json\n{}"):
        adapter = ChatModelDeliberationAdapter(model=_Model(reply))
        with pytest.raises(ValueError, match="JSON"):
            await adapter.propose(_request())


@pytest.mark.asyncio
async def test_routed_adapter_uses_thinking_only_for_the_explicit_thinking_route() -> None:
    flash = _Model('{"proposal_id":"proposal:flash"}')
    thinking = _Model('{"proposal_id":"proposal:thinking"}')
    adapter = RoutedChatModelDeliberationAdapter(
        flash_model=flash, thinking_model=thinking, temperature=0.8
    )

    flash_output = await adapter.propose(_request())
    thinking_output = await adapter.propose(
        _request().model_copy(
            update={
                "route": ModelRoute(
                    tier="thinking", reason_code="ambiguity", router_version="test.1"
                )
            }
        )
    )
    quick_output = await adapter.recover(_request(), "main_timeout")

    assert flash_output.raw_proposal == {"proposal_id": "proposal:flash"}
    assert thinking_output.raw_proposal == {"proposal_id": "proposal:thinking"}
    assert quick_output.raw_proposal == {"proposal_id": "proposal:flash"}
    assert len(flash.calls) == 2
    assert len(thinking.calls) == 1


@pytest.mark.asyncio
async def test_routed_adapter_fails_closed_when_thinking_was_selected_without_a_thinking_model() -> (
    None
):
    adapter = RoutedChatModelDeliberationAdapter(flash_model=_Model("{}"))
    thinking_request = _request().model_copy(
        update={
            "route": ModelRoute(tier="thinking", reason_code="ambiguity", router_version="test.1")
        }
    )

    with pytest.raises(RuntimeError, match="not configured"):
        await adapter.propose(thinking_request)


@pytest.mark.asyncio
async def test_adapter_materializes_a_verified_reply_draft_into_a_hash_bound_minimal_proposal() -> (
    None
):
    text = "我刚刚确实有点飘走了。"
    model = _Model(
        json.dumps(
            {
                "response_text": text,
                "stance": "acknowledge_briefly",
                "brief_rationale": "Acknowledge the missed connection without inventing facts.",
                "confidence": 7300,
            },
            ensure_ascii=False,
        )
    )
    request = _request().model_copy(
        update={
            "trigger_message": TriggerMessage(
                event_ref="event:observation:1",
                event_payload_hash="sha256:" + "a" * 64,
                observation_ref="observation:1",
                source_world_revision=3,
                actor="user:primary",
                channel="test",
                reply_target="user:primary",
                text="你刚刚没接住我。",
            )
        }
    )
    adapter = ChatModelDeliberationAdapter(model=model)

    output = await adapter.propose(request)

    assert output.raw_proposal["trigger_ref"] == "trigger:1"
    assert output.raw_proposal["response_text"] == text
    assert output.raw_proposal["action_intents"][0]["target"] == "user:primary"
    assert (
        output.raw_proposal["action_intents"][0]["payload_hash"]
        == "sha256:" + sha256(text.encode("utf-8")).hexdigest()
    )
    assert output.raw_proposal["evidence_refs"][0]["ref_id"] == "observation:1"


@pytest.mark.asyncio
async def test_adapter_accepts_provider_named_expression_draft_wrapper() -> None:
    model = _Model(
        json.dumps(
            {
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "是的，这是我们第一次聊天。你好呀！"}],
                    "stance": "answer_without_world_claims",
                    "brief_rationale": "Answer the current question directly.",
                    "confidence": 9200,
                }
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["timing_choice"] == "now"
    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_adapter_normalizes_an_unambiguous_text_beat_without_modality() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"text": "是的，这是我们第一次聊天。"}],
                "stance": "answer_without_world_claims",
                "brief_rationale": "Answer directly.",
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_expression_world_claim_must_cite_its_semantic_context_lane() -> None:
    reply = {
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "我刚才确实去江边走了一圈。"}],
        "stance": "answer_from_world",
        "brief_rationale": "Report one verified occurrence.",
        "world_claims": [
            {
                "claim_text": "我刚才去江边走了一圈",
                "scope": "past_world",
                "source_refs": ["occurrence:walk:1"],
            }
        ],
    }
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": "occurrence:walk:1",
                                    "source_hash": "c" * 64,
                                    "value_hash": "d" * 64,
                                    "value": {"kind": "walk"},
                                }
                            ],
                        },
                        "recent_experiences": {"availability": "unavailable"},
                    }
                }
            )
        }
    )

    accepted = await ChatModelDeliberationAdapter(
        model=_Model(json.dumps(reply, ensure_ascii=False))
    ).propose(request)
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"

    forged = {
        **reply,
        "world_claims": [
            {
                "claim_text": "我刚才去图书馆看书",
                "scope": "past_world",
                "source_refs": ["occurrence:library:invented"],
            }
        ],
    }
    with pytest.raises(ValueError, match="semantic source lane"):
        await ChatModelDeliberationAdapter(
            model=_Model(json.dumps(forged, ensure_ascii=False))
        ).propose(request)


@pytest.mark.asyncio
async def test_elliptical_just_woke_up_expression_is_model_owned() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "不是难回答，就是刚睡醒脑子还有点懵。",
                    }
                ],
                "stance": "casual",
                "brief_rationale": "Explain the hesitation.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(_qq_request())
    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "message"),
    (
        ("可能先整理一下照片。", "structured life_intent"),
        ("我下午没有已经确定的安排。", "current_world"),
    ),
)
async def test_uncertain_schedule_wording_is_not_keyword_rejected(text: str, message: str) -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "casual",
                "brief_rationale": "Answer the afternoon-plan question.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    del message
    output = await ChatModelDeliberationAdapter(model=model).propose(_qq_request())
    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_subjective_reaction_to_user_story_is_not_companion_autobiography() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "不过你妈随手加需求那段……我听着都麻了。",
                    }
                ],
                "stance": "commiserate_without_defending",
                "brief_rationale": "React to the concrete frustration.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_expression_prompt_does_not_direct_third_party_responses() -> None:
    model = _Model('{"proposal_id":"proposal:third-party-attunement"}')

    await ChatModelDeliberationAdapter(model=model).propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "third party" not in system
    assert "You own the motive, tone, timing" in system


@pytest.mark.asyncio
async def test_current_world_question_without_matching_authority_fails_closed_before_review() -> (
    None
):
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我刚在图书馆看完一本散文。"}],
                "stance": "answer",
                "brief_rationale": "Answer naturally.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "reject",
                    "replacement_text": "今天没有能确认的事件，我不想拿平时爱读书来现编。",
                    "asserts_current_or_recent_world": False,
                    "source_refs": [],
                    "brief_reason": "The draft converted a stable interest into an unverified event.",
                },
                ensure_ascii=False,
            )
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {"availability": "unavailable"},
                        "world_life": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    }
                }
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=main, semantic_boundary_reviewer=reviewer
    ).propose(request)

    intent = output.raw_proposal["action_intents"][0]
    assert intent["payload_hash"] != ""
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_consecutive_unsupported_world_probes_recover_without_template_repetition_or_second_rtt() -> (
    None
):
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我刚去图书馆看书又听了会儿歌。"}],
                "stance": "answer",
                "brief_rationale": "Invent a plausible day.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel([])
    adapter = ChatModelDeliberationAdapter(model=main, semantic_boundary_reviewer=reviewer)
    probes = (
        "你今天发生了什么？",
        "那最近有什么印象深的事？",
        "别说角色设定，我问的是你真的经历了什么？",
    )
    visible: list[str] = []
    for index, probe in enumerate(probes, start=1):
        request = _qq_request().model_copy(
            update={
                "trigger_message": _qq_request().trigger_message.model_copy(
                    update={
                        "event_ref": f"event:observation:qq:world-probe:{index}",
                        "observation_ref": f"observation:qq:world-probe:{index}",
                        "platform_message_id": f"qq-world-probe-{index}",
                        "text": probe,
                    }
                ),
                "model_content_json": json.dumps(
                    {
                        "slices": {
                            "current_situation": {"availability": "unavailable"},
                            "world_life": {"availability": "unavailable"},
                            "recent_experiences": {"availability": "unavailable"},
                            "recent_dialogue": {
                                "availability": "available",
                                "source_refs": [],
                                "items": [
                                    {
                                        "item_ref": f"dialogue:recovery:{position}",
                                        "value": {"speaker": "companion", "text": text},
                                    }
                                    for position, text in enumerate(visible, start=1)
                                ],
                            },
                        },
                    }
                ),
            }
        )

        output = await adapter.propose(request)
        payload = json.loads(
            output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
        )
        visible.append(payload["beat_drafts"][0]["inline_text"])

    assert len(set(visible)) == 1
    assert reviewer.calls == []
    assert len(main.calls) == len(probes)
    joined = "\n".join(visible)
    assert joined
    assert not any(term in joined for term in ("审计", "权威", "校验", "世界状态"))


@pytest.mark.asyncio
async def test_unsupported_setting_probe_distinguishes_setting_from_lived_experience() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "按角色设定我今天去上课了。"}],
                "stance": "answer",
                "brief_rationale": "Convert setting into an event.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={
                    "text": "这是角色设定，还是你今天真的经历了？",
                }
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {"availability": "unavailable"},
                        "world_life": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    },
                }
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=main, semantic_boundary_reviewer=_SequenceJsonModel([])
    ).propose(request)
    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    text = payload["beat_drafts"][0]["inline_text"]

    assert text == "按角色设定我今天去上课了。"


@pytest.mark.asyncio
async def test_current_activity_authority_reaches_independent_grounding_review() -> None:
    reply = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我在收拾桌面。"}],
            "stance": "answer",
            "brief_rationale": "Use current situation.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "accept",
                    "replacement_text": None,
                    "asserts_current_or_recent_world": True,
                    "source_refs": ["event:activity:1"],
                    "brief_reason": "The current activity is source-bound.",
                }
            )
        ]
    )
    situation = {
        "availability": "available",
        "source_refs": ["event:activity:1"],
        "items": [
            {
                "item_ref": "agent:companion",
                "source_bindings": [{"ref": "event:activity:1"}],
                "value": {"activity_slices": [{"activity_id": "activity:tidy"}]},
            }
        ],
    }
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你现在在干什么？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": situation,
                        "world_life": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    }
                }
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=_Model(reply), semantic_boundary_reviewer=reviewer
    ).propose(request)

    assert output.raw_proposal["action_intents"]
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_open_life_probe_retries_claim_free_review_when_settled_evidence_exists() -> None:
    """An invalid draft is not evidence that the companion has no lived event."""

    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我今天去图书馆看散文了。"}],
                "stance": "answer",
                "brief_rationale": "Invent a plausible event.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "replace",
                    "replacement_text": "真要按经历来讲，这一段我现在没法确定。",
                    "asserts_current_or_recent_world": False,
                    "source_refs": [],
                    "brief_reason": "The proposed library visit is unsupported.",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "replace",
                    "replacement_text": "我随手浏览时看到几样有意思的东西，还记下了一个以后想看的主题。",
                    "asserts_current_or_recent_world": True,
                    "source_refs": ["event:life-content:browse:1"],
                    "brief_reason": "A settled life-content item directly answers the open probe.",
                },
                ensure_ascii=False,
            ),
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {"availability": "unavailable"},
                        "world_life": {
                            "availability": "available",
                            "source_refs": ["event:life-content:browse:1"],
                            "items": [
                                {
                                    "item_ref": "event:life-content:browse:1",
                                    "source_hash": "a" * 64,
                                    "value_hash": "b" * 64,
                                    "value": {
                                        "content": {
                                            "text": "随手浏览时看到几样有意思的东西，记下了一个以后想看的主题。",
                                        }
                                    },
                                }
                            ],
                        },
                        "recent_experiences": {"availability": "unavailable"},
                    }
                },
                ensure_ascii=False,
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=main, semantic_boundary_reviewer=reviewer
    ).propose(request)

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert "图书馆" in payload["beat_drafts"][0]["inline_text"]
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_grounding_rewrite_rejects_a_forged_source_ref() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我今天去图书馆看散文了。"}],
                "stance": "answer",
                "brief_rationale": "Invent a plausible event.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "replace",
                    "replacement_text": "我今天在图书馆看了散文。",
                    "asserts_current_or_recent_world": True,
                    "source_refs": ["event:forged:library"],
                    "brief_reason": "Cites a fabricated source.",
                },
                ensure_ascii=False,
            )
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "source_refs": ["event:life-content:browse:1"],
                            "items": [
                                {
                                    "item_ref": "event:life-content:browse:1",
                                    "value": {
                                        "content": {"text": "随手浏览时记下了一个想看的主题。"}
                                    },
                                }
                            ],
                        },
                        "current_situation": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    }
                },
                ensure_ascii=False,
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=main, semantic_boundary_reviewer=reviewer
    ).propose(request)
    assert output.raw_proposal["action_intents"]
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_grounding_review_tolerates_empty_accept_replacement_and_long_reason() -> None:
    reply = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "今天没有能确认的经历。"}],
            "stance": "answer",
            "brief_rationale": "Answer without invention.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "accept",
                    "replacement_text": "",
                    "asserts_current_or_recent_world": False,
                    "source_refs": [],
                    "brief_reason": "x" * 500,
                }
            )
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天真的发生了什么？"}
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=_Model(reply), semantic_boundary_reviewer=reviewer
    ).propose(request)

    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_grounding_reviewer_failure_still_materializes_a_safe_reply() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我刚去图书馆看书了。"}],
                "stance": "answer",
                "brief_rationale": "Answer.",
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=main, semantic_boundary_reviewer=_RaisingModel("")
    ).propose(request)

    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_grounding_reviewer_failure_preserves_available_world_authority_for_recovery() -> (
    None
):
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我随手记下了一个以后想看的主题。"}],
                "stance": "answer_from_world",
                "brief_rationale": "Answer from the supplied experience.",
                "world_claims": [
                    {
                        "claim_text": "我随手记下了一个以后想看的主题",
                        "scope": "past_world",
                        "source_refs": ["experience:topic:1"],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {"availability": "unavailable"},
                        "world_life": {
                            "availability": "available",
                            "source_refs": ["experience:topic:1"],
                            "items": [
                                {
                                    "item_ref": "experience:topic:1",
                                    "source_hash": "a" * 64,
                                    "value_hash": "b" * 64,
                                    "value": {
                                        "summary": "随手浏览时看到几样有意思的东西，记下了一个以后想看的主题"
                                    },
                                }
                            ],
                        },
                        "recent_experiences": {"availability": "unavailable"},
                    }
                },
                ensure_ascii=False,
            ),
        }
    )

    output = await ChatModelDeliberationAdapter(
        model=main, semantic_boundary_reviewer=_RaisingModel("")
    ).propose(request)
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_named_expression_draft_cannot_smuggle_a_complete_proposal() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model('{"expression_draft":{"proposal_id":"proposal:forged"}}'),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError, match="wrapped expression draft"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_quick_recovery_accepts_one_text_expression_draft_as_minimal_reply() -> None:
    model = _Model(
        json.dumps(
            {
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "是第一次，刚认识。"}],
                    "stance": "answer_without_world_claims",
                    "brief_rationale": "Use the smallest valid text recovery.",
                    "confidence": 9000,
                }
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.recover(_qq_request(), "main_invalid_output")

    assert output.raw_proposal["proposal_kind"] == "minimal"
    assert output.raw_proposal["response_text"] == "是第一次，刚认识。"


@pytest.mark.asyncio
async def test_quick_recovery_narrows_open_vocabulary_stance_instead_of_losing_reply() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我叫沈知栀。"}],
                "stance": "clarify_my_name_warmly",
                "brief_rationale": "Answer the direct question.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(model=model)

    output = await adapter.recover(_qq_request(), "main_invalid_output")

    assert output.raw_proposal["proposal_kind"] == "minimal"
    assert output.raw_proposal["response_text"] == "我叫沈知栀。"
    assert output.raw_proposal["stance"] == "answer_without_world_claims"


@pytest.mark.asyncio
async def test_quick_recovery_does_not_apply_keyword_autobiography_gate() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我周末去逛了旧书市集。"}],
                "stance": "recover_with_a_personal_detail",
                "brief_rationale": "Attempt a natural recovery.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await ChatModelDeliberationAdapter(model=model).recover(
        _qq_request(), "main_invalid_output"
    )
    assert output.raw_proposal["response_text"] == "我周末去逛了旧书市集。"


@pytest.mark.parametrize(
    "text",
    ("我正好也翻翻书。晚点聊。", "我去洗澡了。", "那我先出门一趟。"),
)
@pytest.mark.asyncio
async def test_expression_may_choose_a_near_future_self_activity(
    text: str,
) -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "share_a_near_future_action",
                "brief_rationale": "Attempt to narrate a new activity.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_honest_correction_is_not_forced_through_a_keyword_claim_protocol() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "对，你根本没提过成都，是我把上下文接错了。",
                    }
                ],
                "stance": "own_the_mistake",
                "brief_rationale": "Correct the mistaken premise directly.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_user_first_person_future_does_not_become_a_companion_life_intent() -> None:
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={
                    "text": "我要去忙一会儿，晚点回来。",
                }
            ),
        }
    )
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "好，忙完再聊。"}],
                "stance": "accept_their_departure",
                "brief_rationale": "Respond to the counterpart's plan without adopting it.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(request)

    assert output.raw_proposal["proposal_kind"] == "decision"


@pytest.mark.asyncio
async def test_adapter_rejects_a_reply_draft_without_a_verified_current_message() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            '{"response_text":"hi","stance":"plain","brief_rationale":"ordinary response"}'
        )
    )

    with pytest.raises(ValueError, match="verified current message"):
        await adapter.propose(_request())


def _qq_request() -> ModelInput:
    return _request().model_copy(
        update={
            "trigger_message": TriggerMessage(
                event_ref="event:observation:qq:1",
                event_payload_hash="sha256:" + "b" * 64,
                observation_ref="observation:qq:1",
                source_world_revision=3,
                actor="user:primary",
                channel="qq",
                reply_target="conversation:qq:c2c:owner",
                platform_message_id="qq-message-7788",
                text="我今天终于把那件麻烦事做完了。",
            )
        }
    )


@pytest.mark.asyncio
async def test_expression_draft_keeps_visible_text_when_only_audit_metadata_is_missing() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "先不想了也好，吃点东西缓一缓。"}],
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await ChatModelDeliberationAdapter(model=model).propose(_qq_request())

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["beat_drafts"][0]["inline_text"] == "先不想了也好，吃点东西缓一缓。"
    assert output.raw_proposal["stance"] == "compiler_default_unspecified"
    assert output.raw_proposal["brief_rationale"] == (
        "Model omitted draft metadata; compiler preserved separately validated visible content."
    )
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_expression_draft_materializes_model_selected_multimodal_beats_without_provider_authority() -> (
    None
):
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {"modality": "typing"},
                    {"modality": "reaction", "reaction_id": "like"},
                    {"modality": "text", "text": "这下真的可以松口气了。"},
                    {"modality": "sticker", "sticker_id": "qq-face:14"},
                ],
                "stance": "acknowledge_briefly",
                "brief_rationale": "The sequence fits the current relationship and message.",
                "confidence": 7600,
            },
            ensure_ascii=False,
        )
    )
    adapter = ChatModelDeliberationAdapter(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["timing_choice"] == "now"
    intents = output.raw_proposal["action_intents"]
    assert [item["kind"] for item in intents] == ["typing", "reaction", "reply", "sticker"]
    assert intents[0]["dependencies"] == []
    assert intents[1]["dependencies"] == [intents[0]["intent_id"]]
    assert intents[2]["dependencies"] == [intents[1]["intent_id"]]
    assert intents[3]["dependencies"] == [intents[2]["intent_id"]]
    drafts = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])[
        "beat_drafts"
    ]
    reaction = json.loads(drafts[1]["inline_text"])
    assert reaction == {
        "provider_message_id": "qq-message-7788",
        "reaction_id": "like",
        "version": "expression-reaction.1",
    }
    assert drafts[2]["inline_text"] == "这下真的可以松口气了。"
    assert all(intent["target"] == "conversation:qq:c2c:owner" for intent in intents)


@pytest.mark.asyncio
async def test_explicit_shared_history_is_not_keyword_rejected() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "你上次推荐的书店，我后来去搜了。",
                        }
                    ],
                    "stance": "share_a_callback",
                    "brief_rationale": "Create a conversational callback.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_subject_omitted_shared_history_is_not_keyword_rejected() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "之前在群里聊过天呀，还记得吗？",
                        }
                    ],
                    "stance": "recall_our_history",
                    "brief_rationale": "Refer to an earlier shared interaction.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_paraphrased_elliptical_shared_episode_is_not_keyword_rejected() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "那会儿一起讨论过这个，你不记得了？",
                        }
                    ],
                    "stance": "recall_our_history",
                    "brief_rationale": "Invoke a shared earlier episode.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_subject_omitted_shared_history_is_allowed_with_recent_dialogue_authority() -> None:
    source_ref = "dialogue:group-chat:1"
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "之前在群里聊过天呀，还记得吗？",
                        }
                    ],
                    "stance": "recall_our_history",
                    "brief_rationale": "Use source-bound continuity.",
                    "world_claims": [
                        {
                            "claim_text": "之前在群里聊过天",
                            "scope": "shared_history",
                            "source_refs": [source_ref],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": source_ref,
                                    "value": {"speaker": "user", "text": "群里那件事挺有意思。"},
                                }
                            ],
                        },
                        "recent_experiences": {"availability": "unavailable"},
                    },
                }
            ),
        }
    )

    output = await adapter.propose(request)

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_visible_prose_is_not_reclassified_beyond_declared_claims() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "还记得那家你提过的店吗？我周末专门去了一趟。",
                        }
                    ],
                    "stance": "share_a_callback",
                    "brief_rationale": "Continue a shared topic.",
                    "world_claims": [
                        {
                            "claim_text": "你提过那家店",
                            "scope": "shared_history",
                            "source_refs": ["dialogue:bookshop:1"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": "dialogue:bookshop:1",
                                    "value": {"speaker": "user", "text": "那家店还不错。"},
                                }
                            ],
                        },
                        "world_life": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    },
                }
            ),
        }
    )

    output = await adapter.propose(request)
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_unprompted_autobiographical_prose_is_model_owned() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "周末我去逛了旧书市集。"}],
                    "stance": "share_my_day",
                    "brief_rationale": "Offer a personal detail.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_family_business_prose_is_not_keyword_rejected() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "我家里以前有卖过一款冻顶乌龙。",
                        }
                    ],
                    "stance": "share_family_background",
                    "brief_rationale": "Relate a family history detail.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_education_background_prose_is_not_keyword_rejected() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我高中在杭州读过书。"}],
                    "stance": "share_education_background",
                    "brief_rationale": "Relate an education detail.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_family_background_is_allowed_with_character_core_authority() -> None:
    core_ref = "core:companion:family-background"
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "我家里以前有卖过一款冻顶乌龙。",
                        }
                    ],
                    "stance": "share_family_background",
                    "brief_rationale": "Use a source-bound stable background detail.",
                    "world_claims": [
                        {
                            "claim_text": "家里以前卖过冻顶乌龙",
                            "scope": "stable_identity",
                            "source_refs": [core_ref],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "character_core": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": core_ref,
                                    "value": {"family_background_refs": ["background:tea-shop"]},
                                }
                            ],
                        },
                    },
                }
            ),
        }
    )

    output = await adapter.propose(request)

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_family_background_rejects_a_forged_character_core_ref() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "我家里以前有卖过一款冻顶乌龙。",
                        }
                    ],
                    "stance": "share_family_background",
                    "brief_rationale": "Attempt a background callback.",
                    "world_claims": [
                        {
                            "claim_text": "家里以前卖过冻顶乌龙",
                            "scope": "stable_identity",
                            "source_refs": ["core:forged"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )

    with pytest.raises(ValueError, match="semantic source lane"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_subjective_family_concern_does_not_require_background_authority() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我有点担心家里。"}],
                    "stance": "share_concern",
                    "brief_rationale": "Express a subjective feeling.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_subjective_inner_life_does_not_require_occurrence_authority() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "刚才我有点走神，因为还在想你说的那句话。",
                        }
                    ],
                    "stance": "admit_distraction",
                    "brief_rationale": "Share a subjective conversational reaction.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_epistemic_denial_does_not_need_evidence_for_the_denied_event() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "这件事我没有可确认的记录，也不记得我们聊过。",
                        }
                    ],
                    "stance": "decline_to_invent",
                    "brief_rationale": "State the evidence limit.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_temporal_stable_trait_is_not_misclassified_as_an_occurrence() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我以前就是比较慢热。"}],
                    "stance": "describe_my_temperament",
                    "brief_rationale": "Share a stable personality trait.",
                    "world_claims": [
                        {
                            "claim_text": "我比较慢热",
                            "scope": "stable_identity",
                            "source_refs": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_current_first_person_activity_is_not_keyword_rejected() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我现在在收拾桌面。"}],
                    "stance": "share_current_activity",
                    "brief_rationale": "Answer with a current activity.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_expression_draft_rejects_typing_after_visible_content() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {"modality": "text", "text": "我还有个想法。"},
                        {"modality": "typing"},
                    ],
                    "stance": "continue_thought",
                    "brief_rationale": "The provider returned a terminal typing indicator.",
                },
                ensure_ascii=False,
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError, match="typing beats must precede visible content"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_expression_draft_rejects_a_modality_missing_from_the_deployment_profile() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "reaction", "reaction_id": "like"}],
                    "stance": "acknowledge_briefly",
                    "brief_rationale": "A reaction might fit.",
                }
            )
        ),
        expression_capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError, match="not available"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_expression_draft_silent_choice_persists_a_no_action_decision() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "silent",
                    "beats": [],
                    "stance": "defer",
                    "brief_rationale": "The companion notices but chooses not to intrude.",
                }
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["timing_choice"] == "silent"
    assert output.raw_proposal["proposed_changes"] == []
    assert output.raw_proposal["action_intents"] == []


@pytest.mark.asyncio
async def test_expression_draft_later_choice_freezes_relative_window_on_every_beat() -> None:
    request = _qq_request().model_copy(
        update={"model_content_json": '{"logical_time":"2026-07-16T12:00:00+00:00"}'}
    )
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "later",
                    "delay_seconds": 60,
                    "expires_after_seconds": 600,
                    "beats": [
                        {"modality": "text", "text": "等我一下，我晚点认真听你说。"},
                    ],
                    "stance": "defer",
                    "brief_rationale": "The current activity makes an immediate full response implausible.",
                },
                ensure_ascii=False,
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(request)

    assert output.raw_proposal["timing_choice"] == "later"
    intents = output.raw_proposal["action_intents"]
    assert [item["kind"] for item in intents] == ["followup"]
    assert all(
        item["due_window"] == ["2026-07-16T12:01:00Z", "2026-07-16T12:10:00Z"] for item in intents
    )


@pytest.mark.asyncio
async def test_expression_draft_later_rejects_uninstalled_nontext_effect() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "later",
                    "delay_seconds": 4,
                    "expires_after_seconds": 30,
                    "beats": [{"modality": "typing"}],
                    "stance": "hold",
                    "brief_rationale": "Signal that a response will come later.",
                }
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError, match="later expression supports only"):
        await adapter.propose(_qq_request())


def test_expression_prompt_exposes_exact_executable_field_types() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model("{}"),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    system = adapter._messages(  # noqa: SLF001 - contract regression test
        request=_qq_request(),
        quick_recovery=False,
        provisional=False,
        failure_code=None,
    )[0]["content"]

    assert 'modality="text"' in system
    assert "never use content" in system
    assert "confidence is an integer from 0 through 10000" in system
    assert "counterpart_history" in system
    assert "never use conversation or user_fact" in system
    assert "response_expectation_assessment" in system
