from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

import httpx
import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2 import recall_embedding
from companion_daemon.world_v2.recall_embedding import (
    OpenAICompatibleRecallEmbedding,
    SQLiteCachedRecallEmbedding,
    configured_recall_embedding,
)
from companion_daemon.world_v2.recall_index import (
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallQuery,
    RecallSourceBinding,
    FeatureHashRecallEmbedding,
)
from companion_daemon.world_v2.recall_audit import CharacterRecallRequest
from companion_daemon.world_v2.recall_runtime import (
    RecallCoordinator,
    verify_trusted_recall_trace,
)


NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
CURSOR = RecallCursor(
    world_revision=2,
    deliberation_revision=1,
    ledger_sequence=3,
)


def test_openai_compatible_embedding_preserves_provider_indices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://embedding.test/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    embedding = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        transport=httpx.MockTransport(handler),
    )

    try:
        result = embedding.embed(("茶", "雨"))
    finally:
        embedding.close()

    assert result == ((1.0, 0.0), (0.0, 1.0))
    assert embedding.last_usage_tokens == 6


@pytest.mark.parametrize("usage", ({}, {"total_tokens": 0}, {"total_tokens": "0"}))
def test_embedding_malformed_or_zero_usage_cannot_lower_local_estimate(usage) -> None:
    embedding = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                    "usage": usage,
                },
            )
        ),
    )
    try:
        embedding.embed(("茶",))
    finally:
        embedding.close()

    assert embedding.last_usage_tokens == 3


def test_embedding_outage_degrades_to_exact_recall_channels() -> None:
    embedding = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, json={"error": "down"})),
    )
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(
        cursor=CURSOR,
        documents=(
            RecallDocument(
                document_id="recall:tea",
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
                        immutable_hash="a" * 64,
                    ),
                ),
                source_world_revision=2,
                text="用户喜欢凤凰单丛。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=NOW,
                privacy_class="personal",
            ),
        ),
    )

    try:
        result = index.search(
            RecallQuery(
                query_text="凤凰单丛",
                cursor=CURSOR,
                actor_ref="agent:companion",
                subject_refs=("agent:companion", "user:primary"),
                viewer_privacy_ceiling="withhold",
                at=NOW,
                accessibility_seed="draw:provider-down",
            )
        )
    finally:
        embedding.close()

    assert result.hits[0].document.source_item_ref == "fact:tea"
    assert "lexical" in result.hits[0].match_channels
    assert "dense" not in result.hits[0].match_channels
    assert result.embedding_status == "degraded"
    assert result.embedding_failure_code == "semantic recall provider unavailable"


def test_semantic_outage_is_explicit_in_durable_recall_trace() -> None:
    semantic = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, json={"error": "down"})),
    )
    base = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    document = RecallDocument(
        document_id="recall:trace:tea",
        memory_kind="semantic",
        source_item_ref="fact:trace:tea",
        source_slice="relevant_facts",
        source_refs=("event:fact:trace:tea",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:fact:trace:tea",
                source_world_revision=2,
                immutable_hash="b" * 64,
            ),
        ),
        source_world_revision=2,
        text="用户喜欢凤凰单丛。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=NOW,
        privacy_class="personal",
    )
    base.rebuild(cursor=CURSOR, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=base,
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        semantic_embedding=semantic,
        trigger_ref="event:observation:trace",
    )
    try:
        trusted = coordinator.recall(
            request=CharacterRecallRequest(query_text="凤凰单丛"),
            accessibility_seed="draw:trace:degraded",
            expected_cursor=CURSOR,
            trigger_ref="event:observation:trace",
        )
    finally:
        coordinator.close()
    trace = verify_trusted_recall_trace(trusted)

    assert trace.embedding_status == "degraded"
    assert trace.embedding_failure_code == "semantic recall provider unavailable"
    assert "lexical" in trace.hits[0].match_channels


def test_embedding_schema_errors_do_not_masquerade_as_provider_outages() -> None:
    embedding = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0]}]},
            )
        ),
    )

    try:
        with pytest.raises(ValueError, match="vector"):
            embedding.embed(("茶",))
    finally:
        embedding.close()


def test_embedding_auth_rejection_is_a_hard_configuration_error() -> None:
    embedding = OpenAICompatibleRecallEmbedding(
        api_key="wrong",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(401, json={"error": "unauthorized"})
        ),
    )

    try:
        with pytest.raises(ValueError, match="configuration"):
            embedding.embed(("茶",))
    finally:
        embedding.close()


def test_semantic_embedding_is_on_by_default_with_credentials_and_can_be_disabled() -> None:
    enabled_by_default = Settings(  # type: ignore[call-arg]
        _env_file=None,
        OPENAI_API_KEY="secret",
    )
    disabled = Settings(  # type: ignore[call-arg]
        _env_file=None,
        OPENAI_API_KEY="secret",
        WORLD_V2_RECALL_SEMANTIC_ENABLED=False,
    )
    enabled = Settings(  # type: ignore[call-arg]
        _env_file=None,
        OPENAI_API_KEY="secret",
        OPENAI_BASE_URL="https://embedding.test/v1",
        WORLD_V2_RECALL_EMBEDDING_MODEL="semantic-fixture",
        WORLD_V2_RECALL_EMBEDDING_DIMENSIONS=2,
    )

    assert configured_recall_embedding(disabled) is None
    default_embedding = configured_recall_embedding(enabled_by_default)
    assert default_embedding is not None
    try:
        assert default_embedding.version.startswith(
            "openai-compatible:text-embedding-3-small:dimensions=512:endpoint="
        )
    finally:
        default_embedding.close()
    embedding = configured_recall_embedding(enabled)
    assert embedding is not None
    try:
        assert embedding.version.startswith(
            "openai-compatible:semantic-fixture:dimensions=2:endpoint="
        )
    finally:
        embedding.close()


def test_semantic_embedding_cache_is_incremental_and_survives_restart(tmp_path) -> None:
    class _CountingEmbedding:
        version = "semantic-cache-fixture.1"
        dimensions = 2

        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self.closed = False

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            self.calls.append(texts)
            return tuple((float(len(text)), float(index + 1)) for index, text in enumerate(texts))

        def close(self) -> None:
            self.closed = True

    path = tmp_path / "semantic-cache.sqlite"
    first_delegate = _CountingEmbedding()
    first = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:semantic-cache",
        delegate=first_delegate,
    )
    initial = first.embed(("茶", "雨", "茶"))
    repeated = first.embed(("雨", "茶"))
    first.close()

    second_delegate = _CountingEmbedding()
    reopened = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:semantic-cache",
        delegate=second_delegate,
    )
    after_restart = reopened.embed(("茶", "风"))
    reopened.close()

    assert first_delegate.calls == [("茶", "雨")]
    assert repeated == (initial[1], initial[0])
    assert second_delegate.calls == [("风",)]
    assert after_restart[0] == initial[0]
    assert first_delegate.closed is True
    assert second_delegate.closed is True


def test_semantic_embedding_budget_is_durable_and_rejects_before_network(
    tmp_path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    path = tmp_path / "semantic-budget.sqlite"
    delegate = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        daily_token_budget=3,
        monthly_token_budget=30,
        daily_budget_cny=1.0,
        monthly_budget_cny=2.0,
        transport=httpx.MockTransport(handler),
    )
    embedding = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:semantic-budget",
        delegate=delegate,
    )
    assert embedding.embed(("茶",)) == ((1.0, 0.0),)
    with pytest.raises(
        recall_embedding.RecallEmbeddingUnavailable,
        match="budget_exhausted",
    ):
        embedding.embed(("雨",))
    health = embedding.health_snapshot()
    embedding.close()

    assert calls == 1
    assert health["daily_tokens"] == 3
    assert health["last_status"] == "rejected"
    assert health["last_failure_code"] == "semantic_embedding_budget_exhausted"


def test_semantic_cache_does_not_cross_provider_endpoint_identity(tmp_path) -> None:
    calls: list[str] = []

    def transport(label: str) -> httpx.MockTransport:
        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(label)
            vector = [1.0, 0.0] if label == "first" else [0.0, 1.0]
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": vector}]},
            )

        return httpx.MockTransport(handler)

    path = tmp_path / "endpoint-cache.sqlite"
    first = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:endpoint-cache",
        delegate=OpenAICompatibleRecallEmbedding(
            api_key="secret",
            base_url="https://first.embedding.test/v1",
            model="same-model-name",
            dimensions=2,
            transport=transport("first"),
        ),
    )
    first_vector = first.embed(("相同文本",))
    first.close()
    second = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:endpoint-cache",
        delegate=OpenAICompatibleRecallEmbedding(
            api_key="secret",
            base_url="https://second.embedding.test/v1",
            model="same-model-name",
            dimensions=2,
            transport=transport("second"),
        ),
    )
    second_vector = second.embed(("相同文本",))
    second.close()

    assert calls == ["first", "second"]
    assert first_vector != second_vector


def test_semantic_cache_is_globally_bounded_across_embedding_versions(
    tmp_path,
    monkeypatch,
) -> None:
    class _Embedding:
        dimensions = 2

        def __init__(self, version: str) -> None:
            self.version = version

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            return tuple((1.0, float(index)) for index, _text in enumerate(texts))

    monkeypatch.setattr(recall_embedding, "_MAX_CACHED_VECTORS_TOTAL", 2)
    path = tmp_path / "bounded-cache.sqlite"
    first = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:bounded-cache",
        delegate=_Embedding("version:first"),
    )
    first.embed(("一", "二"))
    first.close()
    second = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:bounded-cache",
        delegate=_Embedding("version:second"),
    )
    second.embed(("三",))
    second.close()

    connection = sqlite3.connect(path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM world_recall_embedding_cache").fetchone()[
            0
        ]
    finally:
        connection.close()

    assert count == 2


def test_semantic_cache_is_bounded_by_serialized_vector_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    class _Embedding:
        version = "byte-bound-fixture.1"
        dimensions = 2

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            return tuple((1.0, 0.0) for _text in texts)

    monkeypatch.setattr(recall_embedding, "_MAX_CACHED_VECTORS_TOTAL", 100)
    monkeypatch.setattr(recall_embedding, "_MAX_CACHED_VECTOR_BYTES_TOTAL", 10)
    path = tmp_path / "byte-bounded-cache.sqlite"
    cache = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:byte-bounded-cache",
        delegate=_Embedding(),
    )
    cache.embed(("一", "二", "三"))
    cache.close()

    connection = sqlite3.connect(path)
    try:
        count, vector_bytes = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(length(vector_json)), 0)
            FROM world_recall_embedding_cache
            """
        ).fetchone()
    finally:
        connection.close()

    assert count == 1
    assert vector_bytes <= 10


def test_corrupt_semantic_cache_fails_open_to_embedding_delegate(tmp_path) -> None:
    class _CountingEmbedding:
        version = "corrupt-cache-fixture.1"
        dimensions = 2

        def __init__(self) -> None:
            self.calls = 0

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            self.calls += 1
            return tuple((1.0, 0.0) for _text in texts)

    path = tmp_path / "corrupt-cache.sqlite"
    original_delegate = _CountingEmbedding()
    original = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:corrupt-cache",
        delegate=original_delegate,
    )
    original.embed(("茶",))
    original.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE world_recall_embedding_cache SET vector_json = ?",
            ("not-json",),
        )
        connection.commit()
    finally:
        connection.close()
    recovery_delegate = _CountingEmbedding()
    recovered = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:corrupt-cache",
        delegate=recovery_delegate,
    )
    try:
        vector = recovered.embed(("茶",))
    finally:
        recovered.close()

    assert vector == ((1.0, 0.0),)
    assert recovery_delegate.calls == 1
    connection = sqlite3.connect(path)
    try:
        daily = connection.execute(
            """
            SELECT consumed_tokens, request_count
            FROM world_recall_embedding_usage_daily
            WHERE world_id = ?
            """,
            ("world:corrupt-cache",),
        ).fetchone()
    finally:
        connection.close()
    assert daily == (6, 2)


def test_corrupt_cache_still_rejects_before_network_when_budget_is_exhausted(
    tmp_path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
        )

    path = tmp_path / "corrupt-cache-budget.sqlite"
    delegate = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        daily_token_budget=3,
        monthly_token_budget=30,
        daily_budget_cny=1.0,
        monthly_budget_cny=2.0,
        transport=httpx.MockTransport(handler),
    )
    cache = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:corrupt-cache-budget",
        delegate=delegate,
    )
    cache.embed(("茶",))
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE world_recall_embedding_cache SET vector_json = ?",
            ("not-json",),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(
        recall_embedding.RecallEmbeddingUnavailable,
        match="budget_exhausted",
    ):
        cache.embed(("茶",))
    cache.close()

    assert calls == 1


def test_failed_embedding_call_keeps_conservative_budget_charge(tmp_path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "down"})

    delegate = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        daily_token_budget=3,
        monthly_token_budget=30,
        daily_budget_cny=1.0,
        monthly_budget_cny=2.0,
        transport=httpx.MockTransport(handler),
    )
    cache = SQLiteCachedRecallEmbedding(
        path=tmp_path / "failed-charge.sqlite",
        world_id="world:failed-charge",
        delegate=delegate,
    )
    with pytest.raises(recall_embedding.RecallEmbeddingUnavailable):
        cache.embed(("茶",))
    with pytest.raises(
        recall_embedding.RecallEmbeddingUnavailable,
        match="budget_exhausted",
    ):
        cache.embed(("雨",))
    health = cache.health_snapshot()
    cache.close()

    assert calls == 1
    assert health["daily_tokens"] == 3
    assert health["last_status"] == "rejected"


def test_embedding_usage_is_daily_aggregate_not_per_request_log(tmp_path) -> None:
    class _Embedding:
        version = "aggregate-fixture.1"
        dimensions = 2

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            return tuple((1.0, 0.0) for _text in texts)

    path = tmp_path / "aggregate.sqlite"
    cache = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:aggregate",
        delegate=_Embedding(),
    )
    cache.embed(("一",))
    cache.embed(("二",))
    cache.embed(("三",))
    cache.close()

    connection = sqlite3.connect(path)
    try:
        aggregate_count = connection.execute(
            "SELECT COUNT(*) FROM world_recall_embedding_usage_daily"
        ).fetchone()[0]
        request_log_count = connection.execute(
            "SELECT COUNT(*) FROM world_recall_embedding_usage"
        ).fetchone()[0]
    finally:
        connection.close()
    assert aggregate_count == 1
    assert request_log_count == 0


def test_legacy_request_usage_migration_is_idempotent(tmp_path) -> None:
    class _Embedding:
        version = "legacy-aggregate-fixture.1"
        dimensions = 2

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            return tuple((1.0, 0.0) for _text in texts)

    path = tmp_path / "legacy-aggregate.sqlite"
    SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:legacy-aggregate",
        delegate=_Embedding(),
    ).close()
    now = datetime.now(UTC)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO world_recall_embedding_usage (
                world_id, request_id, embedding_version, requested_at,
                usage_day, usage_month, reserved_tokens, actual_tokens,
                estimated_cost_cny, status, failure_code
            ) VALUES (?, ?, ?, ?, ?, ?, 3, 0, 0.0, 'failed', ?)
            """,
            (
                "world:legacy-aggregate",
                "request:legacy",
                "legacy-aggregate-fixture.1",
                now.isoformat(),
                now.date().isoformat(),
                now.date().isoformat()[:7],
                "provider_timeout",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    first = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:legacy-aggregate",
        delegate=_Embedding(),
    )
    first_health = first.health_snapshot()
    first.close()
    second = SQLiteCachedRecallEmbedding(
        path=path,
        world_id="world:legacy-aggregate",
        delegate=_Embedding(),
    )
    second_health = second.health_snapshot()
    second.close()

    assert first_health["daily_tokens"] == 3
    assert second_health["daily_tokens"] == 3
