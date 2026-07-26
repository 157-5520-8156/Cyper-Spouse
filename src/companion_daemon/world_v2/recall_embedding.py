"""Optional OpenAI-compatible semantic vectors for the rebuildable Recall Index."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock

import httpx

from companion_daemon.config import Settings

from .recall_index import RecallEmbedding, RecallEmbeddingUnavailable
from .sqlite_coordination import configure_shared_sqlite_connection, sqlite_write_lock


_MAX_CACHED_VECTORS_TOTAL = 8_192
_MAX_CACHED_VECTOR_BYTES_TOTAL = 32 * 1024 * 1024


class OpenAICompatibleRecallEmbedding:
    """Small synchronous adapter used by the synchronous Context compiler.

    Dense recall is optional and disposable.  Transport/provider outages use
    ``RecallEmbeddingUnavailable`` so the index can continue with its exact
    lexical, temporal and structured channels instead of failing a chat turn.
    Invalid response schemas remain hard configuration errors.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dimensions: int,
        timeout_seconds: float = 2.0,
        proxy_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not model:
            raise ValueError("semantic recall embedding requires a key and model")
        if not 1 <= dimensions <= 4_096:
            raise ValueError("semantic recall embedding dimensions are invalid")
        if not 0.1 <= timeout_seconds <= 10.0:
            raise ValueError("semantic recall embedding timeout is invalid")
        normalized_base_url = base_url.rstrip("/")
        endpoint_identity = hashlib.sha256(
            normalized_base_url.encode("utf-8")
        ).hexdigest()[:16]
        self.version = (
            f"openai-compatible:{model}:dimensions={dimensions}:"
            f"endpoint={endpoint_identity}"
        )
        self.dimensions = dimensions
        self._model = model
        options: dict[str, object] = {
            "timeout": timeout_seconds,
            "trust_env": False,
            "transport": transport,
        }
        if proxy_url:
            options["proxy"] = proxy_url
        self._client = httpx.Client(**options)
        self._url = f"{normalized_base_url}/embeddings"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        output: list[tuple[float, ...]] = []
        for offset in range(0, len(texts), 64):
            batch = texts[offset : offset + 64]
            try:
                response = self._client.post(
                    self._url,
                    headers=self._headers,
                    json={
                        "model": self._model,
                        "input": list(batch),
                        "dimensions": self.dimensions,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {408, 429} or exc.response.status_code >= 500:
                    raise RecallEmbeddingUnavailable(
                        "semantic recall provider unavailable"
                    ) from exc
                raise ValueError(
                    "semantic recall provider rejected its configuration"
                ) from exc
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                raise RecallEmbeddingUnavailable("semantic recall provider unavailable") from exc
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or len(data) != len(batch):
                raise ValueError("semantic recall response count is invalid")
            ordered: list[tuple[float, ...] | None] = [None] * len(batch)
            for fallback_index, item in enumerate(data):
                if not isinstance(item, dict):
                    raise ValueError("semantic recall response item is invalid")
                index = item.get("index", fallback_index)
                vector = item.get("embedding")
                if (
                    not isinstance(index, int)
                    or not 0 <= index < len(batch)
                    or not _is_numeric_vector(vector)
                    or len(vector) != self.dimensions
                    or ordered[index] is not None
                ):
                    raise ValueError("semantic recall response vector is invalid")
                ordered[index] = tuple(float(value) for value in vector)
            if any(item is None for item in ordered):
                raise ValueError("semantic recall response indices are incomplete")
            output.extend(item for item in ordered if item is not None)
        return tuple(output)

    def close(self) -> None:
        self._client.close()


class SQLiteCachedRecallEmbedding:
    """Persistent, rebuildable vector cache keyed by provider identity and text hash."""

    def __init__(
        self,
        *,
        path: str | Path,
        world_id: str,
        delegate: RecallEmbedding,
    ) -> None:
        if not world_id:
            raise ValueError("semantic recall cache requires a world id")
        self.version = delegate.version
        self.dimensions = delegate.dimensions
        self._delegate = delegate
        self._world_id = world_id
        self._lock = RLock()
        self._write_lock = sqlite_write_lock(path)
        with self._write_lock:
            self._connection = sqlite3.connect(
                str(path),
                timeout=30.0,
                check_same_thread=False,
            )
            configure_shared_sqlite_connection(self._connection)
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS world_recall_embedding_cache (
                    world_id TEXT NOT NULL,
                    embedding_version TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    inserted_order INTEGER PRIMARY KEY AUTOINCREMENT,
                    UNIQUE (world_id, embedding_version, text_hash)
                )
                """
            )
            self._connection.commit()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        hashes = tuple(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts)
        with self._lock:
            try:
                cached = self._read_vectors(hashes)
            except (sqlite3.Error, TypeError, ValueError):
                return self._delegate.embed(texts)
            missing_by_hash: dict[str, str] = {}
            for text_hash, text in zip(hashes, texts, strict=True):
                if text_hash not in cached:
                    missing_by_hash.setdefault(text_hash, text)
            if missing_by_hash:
                missing_hashes = tuple(missing_by_hash)
                vectors = self._delegate.embed(
                    tuple(missing_by_hash[item] for item in missing_hashes)
                )
                if len(vectors) != len(missing_hashes):
                    raise ValueError("semantic recall cache delegate count is invalid")
                additions = dict(zip(missing_hashes, vectors, strict=True))
                self._validate_vectors(additions.values())
                try:
                    self._write_vectors(additions)
                except sqlite3.Error:
                    # The cache is disposable.  Fresh provider vectors remain
                    # usable for this pull even when its SQLite sidecar is
                    # locked or damaged.
                    try:
                        self._connection.rollback()
                    except sqlite3.Error:
                        pass
                cached.update(additions)
            result = tuple(cached[item] for item in hashes)
            self._validate_vectors(result)
            return result

    def _read_vectors(
        self,
        hashes: tuple[str, ...],
    ) -> dict[str, tuple[float, ...]]:
        values: dict[str, tuple[float, ...]] = {}
        for offset in range(0, len(hashes), 256):
            batch = tuple(dict.fromkeys(hashes[offset : offset + 256]))
            placeholders = ",".join("?" for _ in batch)
            rows = self._connection.execute(
                f"""
                SELECT text_hash, vector_json
                FROM world_recall_embedding_cache
                WHERE world_id = ? AND embedding_version = ?
                  AND text_hash IN ({placeholders})
                """,
                (self._world_id, self.version, *batch),
            )
            for text_hash, vector_json in rows:
                value = json.loads(str(vector_json))
                if not _is_numeric_vector(value) or len(value) != self.dimensions:
                    raise ValueError("cached semantic recall vector is invalid")
                values[str(text_hash)] = tuple(float(item) for item in value)
        return values

    def _write_vectors(self, values: dict[str, tuple[float, ...]]) -> None:
        with self._write_lock:
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO world_recall_embedding_cache (
                    world_id, embedding_version, text_hash, vector_json
                ) VALUES (?, ?, ?, ?)
                """,
                tuple(
                    (
                        self._world_id,
                        self.version,
                        text_hash,
                        json.dumps(vector, separators=(",", ":"), allow_nan=False),
                    )
                    for text_hash, vector in values.items()
                ),
            )
            rows = tuple(
                self._connection.execute(
                    """
                    SELECT inserted_order, length(vector_json)
                    FROM world_recall_embedding_cache
                    ORDER BY inserted_order DESC
                    """
                )
            )
            retained = 0
            retained_bytes = 0
            minimum_retained_order: int | None = None
            for inserted_order, vector_bytes in rows:
                encoded_bytes = int(vector_bytes)
                if (
                    retained >= _MAX_CACHED_VECTORS_TOTAL
                    or retained_bytes + encoded_bytes
                    > _MAX_CACHED_VECTOR_BYTES_TOTAL
                ):
                    break
                retained += 1
                retained_bytes += encoded_bytes
                minimum_retained_order = int(inserted_order)
            if minimum_retained_order is None:
                self._connection.execute(
                    "DELETE FROM world_recall_embedding_cache"
                )
            else:
                self._connection.execute(
                    """
                    DELETE FROM world_recall_embedding_cache
                    WHERE inserted_order < ?
                    """,
                    (minimum_retained_order,),
                )
            self._connection.commit()

    def _validate_vectors(self, values: Sequence[Sequence[float]]) -> None:
        if any(
            not _is_numeric_vector(value) or len(value) != self.dimensions
            for value in values
        ):
            raise ValueError("semantic recall cache vector dimensions are invalid")

    def close(self) -> None:
        with self._lock:
            self._connection.close()
            close_delegate = getattr(self._delegate, "close", None)
            if callable(close_delegate):
                close_delegate()


def _is_numeric_vector(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ) and all(isinstance(item, (int, float)) for item in value)


def configured_recall_embedding(
    settings: Settings,
) -> OpenAICompatibleRecallEmbedding | None:
    """Resolve the explicit deployment opt-in without inventing credentials."""

    model = settings.world_v2_recall_embedding_model
    if model is None:
        return None
    if settings.openai_api_key is None:
        raise ValueError(
            "WORLD_V2_RECALL_EMBEDDING_MODEL requires OPENAI_API_KEY"
        )
    return OpenAICompatibleRecallEmbedding(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=model,
        dimensions=settings.world_v2_recall_embedding_dimensions,
        timeout_seconds=settings.world_v2_recall_embedding_timeout_seconds,
        proxy_url=settings.openai_proxy_url,
    )


__all__ = [
    "OpenAICompatibleRecallEmbedding",
    "SQLiteCachedRecallEmbedding",
    "configured_recall_embedding",
]
