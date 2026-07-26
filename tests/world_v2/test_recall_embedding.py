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


def test_embedding_outage_degrades_to_exact_recall_channels() -> None:
    embedding = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"error": "down"})
        ),
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


def test_semantic_embedding_requires_explicit_deployment_opt_in() -> None:
    disabled = Settings(_env_file=None, OPENAI_API_KEY="secret")  # type: ignore[call-arg]
    enabled = Settings(  # type: ignore[call-arg]
        _env_file=None,
        OPENAI_API_KEY="secret",
        OPENAI_BASE_URL="https://embedding.test/v1",
        WORLD_V2_RECALL_EMBEDDING_MODEL="semantic-fixture",
        WORLD_V2_RECALL_EMBEDDING_DIMENSIONS=2,
    )

    assert configured_recall_embedding(disabled) is None
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
            return tuple(
                (float(len(text)), float(index + 1))
                for index, text in enumerate(texts)
            )

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
        count = connection.execute(
            "SELECT COUNT(*) FROM world_recall_embedding_cache"
        ).fetchone()[0]
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
