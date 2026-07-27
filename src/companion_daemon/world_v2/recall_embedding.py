"""Optional OpenAI-compatible semantic vectors for the rebuildable Recall Index."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

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
        daily_token_budget: int = 250_000,
        monthly_token_budget: int = 2_000_000,
        daily_budget_cny: float = 0.10,
        monthly_budget_cny: float = 1.0,
        usd_per_million_tokens: float = 0.02,
        cny_per_usd: float = 7.2,
    ) -> None:
        if not api_key or not model:
            raise ValueError("semantic recall embedding requires a key and model")
        if not 1 <= dimensions <= 4_096:
            raise ValueError("semantic recall embedding dimensions are invalid")
        if not 0.1 <= timeout_seconds <= 10.0:
            raise ValueError("semantic recall embedding timeout is invalid")
        if (
            daily_token_budget <= 0
            or monthly_token_budget < daily_token_budget
            or daily_budget_cny <= 0
            or monthly_budget_cny < daily_budget_cny
            or usd_per_million_tokens <= 0
            or cny_per_usd <= 0
        ):
            raise ValueError("semantic recall embedding budget is invalid")
        normalized_base_url = base_url.rstrip("/")
        endpoint_identity = hashlib.sha256(normalized_base_url.encode("utf-8")).hexdigest()[:16]
        self.version = (
            f"openai-compatible:{model}:dimensions={dimensions}:endpoint={endpoint_identity}"
        )
        self.dimensions = dimensions
        self._model = model
        self.daily_token_budget = daily_token_budget
        self.monthly_token_budget = monthly_token_budget
        self.daily_budget_cny = daily_budget_cny
        self.monthly_budget_cny = monthly_budget_cny
        self.usd_per_million_tokens = usd_per_million_tokens
        self.cny_per_usd = cny_per_usd
        self.last_usage_tokens = 0
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
        usage_tokens = 0
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
                raise ValueError("semantic recall provider rejected its configuration") from exc
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
            estimate = _conservative_token_estimate(batch)
            usage = payload.get("usage")
            reported_tokens: int | None = None
            if isinstance(usage, dict):
                total = usage.get("total_tokens", usage.get("prompt_tokens"))
                if isinstance(total, int) and not isinstance(total, bool) and total > 0:
                    reported_tokens = total
            # Provider metering is useful evidence, but an absent, malformed,
            # or implausibly small value may never lower the hard local
            # reservation.  Otherwise a compatible endpoint returning
            # ``{"usage": {}}`` could make unlimited paid calls look free.
            usage_tokens += max(estimate, reported_tokens or 0)
            output.extend(item for item in ordered if item is not None)
        self.last_usage_tokens = usage_tokens
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
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS world_recall_embedding_usage (
                    world_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    embedding_version TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    usage_day TEXT NOT NULL,
                    usage_month TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL,
                    actual_tokens INTEGER,
                    estimated_cost_cny REAL NOT NULL,
                    status TEXT NOT NULL,
                    failure_code TEXT,
                    PRIMARY KEY (world_id, request_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS world_recall_embedding_usage_daily (
                    world_id TEXT NOT NULL,
                    usage_day TEXT NOT NULL,
                    usage_month TEXT NOT NULL,
                    consumed_tokens INTEGER NOT NULL,
                    estimated_cost_cny REAL NOT NULL,
                    request_count INTEGER NOT NULL,
                    succeeded_count INTEGER NOT NULL,
                    failed_count INTEGER NOT NULL,
                    rejected_count INTEGER NOT NULL,
                    last_embedding_version TEXT NOT NULL,
                    last_status TEXT NOT NULL,
                    last_failure_code TEXT,
                    last_requested_at TEXT NOT NULL,
                    PRIMARY KEY (world_id, usage_day)
                )
                """
            )
            self._connection.commit()
            # The legacy SELECT/fold/DELETE must be one cross-process write
            # transaction.  A process-local lock alone cannot stop two daemon
            # processes from reading and double-counting the same old rows.
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_request_usage_to_daily()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        # Reservations are charged into the daily aggregate before the
        # provider call.  This process-local map is needed only to add a
        # positive actual-usage delta; a crash deliberately leaves the
        # conservative reservation charged.
        self._reservations: dict[str, tuple[str, int, float]] = {}

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        hashes = tuple(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts)
        with self._lock:
            try:
                cached = self._read_vectors(hashes)
            except (sqlite3.Error, TypeError, ValueError):
                # The vector cache is disposable, but cache corruption must
                # not bypass the durable provider budget.
                cached = {}
            missing_by_hash: dict[str, str] = {}
            for text_hash, text in zip(hashes, texts, strict=True):
                if text_hash not in cached:
                    missing_by_hash.setdefault(text_hash, text)
            if missing_by_hash:
                missing_hashes = tuple(missing_by_hash)
                missing_texts = tuple(missing_by_hash[item] for item in missing_hashes)
                request_id = self._reserve_usage(missing_texts)
                try:
                    vectors = self._delegate.embed(missing_texts)
                except Exception as exc:
                    self._finalize_usage(
                        request_id,
                        actual_tokens=None,
                        status="failed",
                        failure_code=(str(exc)[:128] or type(exc).__name__),
                    )
                    raise
                estimate = _conservative_token_estimate(missing_texts)
                raw_actual = getattr(self._delegate, "last_usage_tokens", None)
                actual_tokens = (
                    int(raw_actual)
                    if isinstance(raw_actual, int)
                    and not isinstance(raw_actual, bool)
                    and raw_actual > 0
                    else estimate
                )
                self._finalize_usage(
                    request_id,
                    actual_tokens=max(estimate, actual_tokens),
                    status="succeeded",
                    failure_code=None,
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

    def _reserve_usage(self, texts: tuple[str, ...]) -> str:
        token_estimate = _conservative_token_estimate(texts)
        price = float(getattr(self._delegate, "usd_per_million_tokens", 0.0))
        cny_per_usd = float(getattr(self._delegate, "cny_per_usd", 7.2))
        estimated_cost = token_estimate * price * cny_per_usd / 1_000_000
        daily_tokens = int(getattr(self._delegate, "daily_token_budget", 2**63 - 1))
        monthly_tokens = int(getattr(self._delegate, "monthly_token_budget", 2**63 - 1))
        daily_cny = float(getattr(self._delegate, "daily_budget_cny", float("inf")))
        monthly_cny = float(getattr(self._delegate, "monthly_budget_cny", float("inf")))
        now = datetime.now(UTC)
        day = now.date().isoformat()
        month = day[:7]
        request_id = uuid4().hex
        with self._write_lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                day_tokens, day_cost = self._usage_totals(
                    "usage_day",
                    day,
                )
                month_tokens, month_cost = self._usage_totals(
                    "usage_month",
                    month,
                )
                if (
                    day_tokens + token_estimate > daily_tokens
                    or month_tokens + token_estimate > monthly_tokens
                    or day_cost + estimated_cost > daily_cny
                    or month_cost + estimated_cost > monthly_cny
                ):
                    self._connection.execute(
                        """
                        INSERT INTO world_recall_embedding_usage_daily (
                            world_id, usage_day, usage_month, consumed_tokens,
                            estimated_cost_cny, request_count, succeeded_count,
                            failed_count, rejected_count, last_embedding_version,
                            last_status, last_failure_code, last_requested_at
                        ) VALUES (?, ?, ?, 0, 0.0, 0, 0, 0, 1, ?, 'rejected', ?, ?)
                        ON CONFLICT(world_id, usage_day) DO UPDATE SET
                            rejected_count = rejected_count + 1,
                            last_embedding_version = excluded.last_embedding_version,
                            last_status = excluded.last_status,
                            last_failure_code = excluded.last_failure_code,
                            last_requested_at = excluded.last_requested_at
                        """,
                        (
                            self._world_id,
                            day,
                            month,
                            self.version,
                            "semantic_embedding_budget_exhausted",
                            now.isoformat(),
                        ),
                    )
                    self._connection.commit()
                    raise RecallEmbeddingUnavailable("semantic_embedding_budget_exhausted")
                self._connection.execute(
                    """
                    INSERT INTO world_recall_embedding_usage_daily (
                        world_id, usage_day, usage_month, consumed_tokens,
                        estimated_cost_cny, request_count, succeeded_count,
                        failed_count, rejected_count, last_embedding_version,
                        last_status, last_failure_code, last_requested_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 0, 0, 0, ?, 'reserved', NULL, ?)
                    ON CONFLICT(world_id, usage_day) DO UPDATE SET
                        consumed_tokens = consumed_tokens + excluded.consumed_tokens,
                        estimated_cost_cny =
                            estimated_cost_cny + excluded.estimated_cost_cny,
                        request_count = request_count + 1,
                        last_embedding_version = excluded.last_embedding_version,
                        last_status = excluded.last_status,
                        last_failure_code = NULL,
                        last_requested_at = excluded.last_requested_at
                    """,
                    (
                        self._world_id,
                        day,
                        month,
                        token_estimate,
                        estimated_cost,
                        self.version,
                        now.isoformat(),
                    ),
                )
                self._connection.commit()
            except Exception:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
        self._reservations[request_id] = (day, token_estimate, estimated_cost)
        return request_id

    def _usage_totals(self, field: str, value: str) -> tuple[int, float]:
        row = self._connection.execute(
            f"""
            SELECT
                COALESCE(SUM(consumed_tokens), 0),
                COALESCE(SUM(estimated_cost_cny), 0.0)
            FROM world_recall_embedding_usage_daily
            WHERE world_id = ? AND {field} = ?
            """,
            (self._world_id, value),
        ).fetchone()
        return int(row[0]), float(row[1])

    def _finalize_usage(
        self,
        request_id: str,
        *,
        actual_tokens: int | None,
        status: str,
        failure_code: str | None,
    ) -> None:
        reservation = self._reservations.pop(request_id, None)
        if reservation is None:
            # A process crash keeps the already charged reservation.  A
            # duplicate/lost finalizer must never subtract it.
            return
        day, reserved_tokens, reserved_cost = reservation
        charged_tokens = max(reserved_tokens, actual_tokens or 0)
        price = float(getattr(self._delegate, "usd_per_million_tokens", 0.0))
        cny_per_usd = float(getattr(self._delegate, "cny_per_usd", 7.2))
        charged_cost = max(
            reserved_cost,
            charged_tokens * price * cny_per_usd / 1_000_000,
        )
        with self._write_lock:
            self._connection.execute(
                f"""
                UPDATE world_recall_embedding_usage_daily
                SET consumed_tokens = consumed_tokens + ?,
                    estimated_cost_cny = estimated_cost_cny + ?,
                    {status}_count = {status}_count + 1,
                    last_status = ?, last_failure_code = ?
                WHERE world_id = ? AND usage_day = ?
                """,
                (
                    charged_tokens - reserved_tokens,
                    charged_cost - reserved_cost,
                    status,
                    failure_code,
                    self._world_id,
                    day,
                ),
            )
            self._connection.commit()

    def health_snapshot(self) -> dict[str, object]:
        now = datetime.now(UTC)
        day = now.date().isoformat()
        month = day[:7]
        with self._lock:
            day_tokens, day_cost = self._usage_totals("usage_day", day)
            month_tokens, month_cost = self._usage_totals(
                "usage_month",
                month,
            )
            latest = self._connection.execute(
                """
                SELECT status, failure_code, requested_at
                FROM (
                    SELECT last_status AS status,
                           last_failure_code AS failure_code,
                           last_requested_at AS requested_at
                    FROM world_recall_embedding_usage_daily
                    WHERE world_id = ?
                )
                ORDER BY requested_at DESC
                LIMIT 1
                """,
                (self._world_id,),
            ).fetchone()
        return {
            "enabled": True,
            "embedding_version": self.version,
            "daily_tokens": day_tokens,
            "monthly_tokens": month_tokens,
            "daily_estimated_cost_cny": round(day_cost, 8),
            "monthly_estimated_cost_cny": round(month_cost, 8),
            "daily_token_budget": getattr(
                self._delegate,
                "daily_token_budget",
                None,
            ),
            "monthly_token_budget": getattr(
                self._delegate,
                "monthly_token_budget",
                None,
            ),
            "daily_budget_cny": getattr(
                self._delegate,
                "daily_budget_cny",
                None,
            ),
            "monthly_budget_cny": getattr(
                self._delegate,
                "monthly_budget_cny",
                None,
            ),
            "last_status": str(latest[0]) if latest is not None else None,
            "last_failure_code": (
                str(latest[1]) if latest is not None and latest[1] is not None else None
            ),
            "last_requested_at": (str(latest[2]) if latest is not None else None),
        }

    def _migrate_request_usage_to_daily(self) -> None:
        """Fold the old unbounded request log into one durable row per day."""

        rows = tuple(
            self._connection.execute(
                """
                SELECT embedding_version, requested_at, usage_day, usage_month,
                       reserved_tokens, actual_tokens, estimated_cost_cny,
                       status, failure_code
                FROM world_recall_embedding_usage
                WHERE world_id = ?
                ORDER BY requested_at
                """,
                (self._world_id,),
            )
        )
        if not rows:
            return
        price = float(getattr(self._delegate, "usd_per_million_tokens", 0.0))
        cny_per_usd = float(getattr(self._delegate, "cny_per_usd", 7.2))
        grouped: dict[str, dict[str, object]] = {}
        for (
            version,
            requested_at,
            day,
            month,
            reserved,
            actual,
            stored_cost,
            status,
            failure_code,
        ) in rows:
            item = grouped.setdefault(
                str(day),
                {
                    "month": str(month),
                    "tokens": 0,
                    "cost": 0.0,
                    "requests": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "rejected": 0,
                    "version": str(version),
                    "status": str(status),
                    "failure": failure_code,
                    "requested_at": str(requested_at),
                },
            )
            if status == "rejected":
                item["rejected"] = int(item["rejected"]) + 1
            else:
                charged = max(int(reserved), int(actual) if actual is not None else 0)
                item["tokens"] = int(item["tokens"]) + charged
                item["cost"] = float(item["cost"]) + max(
                    float(stored_cost),
                    charged * price * cny_per_usd / 1_000_000,
                )
                item["requests"] = int(item["requests"]) + 1
                if status == "succeeded":
                    item["succeeded"] = int(item["succeeded"]) + 1
                elif status == "failed":
                    item["failed"] = int(item["failed"]) + 1
            item.update(
                {
                    "version": str(version),
                    "status": str(status),
                    "failure": failure_code,
                    "requested_at": str(requested_at),
                }
            )
        for day, item in grouped.items():
            self._connection.execute(
                """
                INSERT INTO world_recall_embedding_usage_daily (
                    world_id, usage_day, usage_month, consumed_tokens,
                    estimated_cost_cny, request_count, succeeded_count,
                    failed_count, rejected_count, last_embedding_version,
                    last_status, last_failure_code, last_requested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(world_id, usage_day) DO UPDATE SET
                    consumed_tokens =
                        world_recall_embedding_usage_daily.consumed_tokens
                        + excluded.consumed_tokens,
                    estimated_cost_cny =
                        world_recall_embedding_usage_daily.estimated_cost_cny
                        + excluded.estimated_cost_cny,
                    request_count =
                        world_recall_embedding_usage_daily.request_count
                        + excluded.request_count,
                    succeeded_count =
                        world_recall_embedding_usage_daily.succeeded_count
                        + excluded.succeeded_count,
                    failed_count =
                        world_recall_embedding_usage_daily.failed_count
                        + excluded.failed_count,
                    rejected_count =
                        world_recall_embedding_usage_daily.rejected_count
                        + excluded.rejected_count,
                    last_embedding_version = excluded.last_embedding_version,
                    last_status = excluded.last_status,
                    last_failure_code = excluded.last_failure_code,
                    last_requested_at = excluded.last_requested_at
                """,
                (
                    self._world_id,
                    day,
                    item["month"],
                    item["tokens"],
                    item["cost"],
                    item["requests"],
                    item["succeeded"],
                    item["failed"],
                    item["rejected"],
                    item["version"],
                    item["status"],
                    item["failure"],
                    item["requested_at"],
                ),
            )
        self._connection.execute(
            "DELETE FROM world_recall_embedding_usage WHERE world_id = ?",
            (self._world_id,),
        )

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
                    or retained_bytes + encoded_bytes > _MAX_CACHED_VECTOR_BYTES_TOTAL
                ):
                    break
                retained += 1
                retained_bytes += encoded_bytes
                minimum_retained_order = int(inserted_order)
            if minimum_retained_order is None:
                self._connection.execute("DELETE FROM world_recall_embedding_cache")
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
        if any(not _is_numeric_vector(value) or len(value) != self.dimensions for value in values):
            raise ValueError("semantic recall cache vector dimensions are invalid")

    def close(self) -> None:
        with self._lock:
            self._connection.close()
            close_delegate = getattr(self._delegate, "close", None)
            if callable(close_delegate):
                close_delegate()


def _is_numeric_vector(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and all(isinstance(item, (int, float)) for item in value)
    )


def configured_recall_embedding(
    settings: Settings,
) -> OpenAICompatibleRecallEmbedding | None:
    """Enable semantic recall when deployment credentials are actually present."""

    if not settings.world_v2_recall_semantic_enabled or settings.openai_api_key is None:
        return None
    return OpenAICompatibleRecallEmbedding(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.world_v2_recall_embedding_model,
        dimensions=settings.world_v2_recall_embedding_dimensions,
        timeout_seconds=settings.world_v2_recall_embedding_timeout_seconds,
        proxy_url=settings.openai_proxy_url,
        daily_token_budget=settings.world_v2_recall_embedding_daily_token_budget,
        monthly_token_budget=settings.world_v2_recall_embedding_monthly_token_budget,
        daily_budget_cny=settings.world_v2_recall_embedding_daily_budget_cny,
        monthly_budget_cny=settings.world_v2_recall_embedding_monthly_budget_cny,
        usd_per_million_tokens=(settings.world_v2_recall_embedding_usd_per_million_tokens),
    )


def _conservative_token_estimate(texts: tuple[str, ...]) -> int:
    # UTF-8 bytes are a safe upper planning bound for the languages used in
    # the project. Provider-reported usage replaces it after the call.
    return sum(max(1, len(text.encode("utf-8"))) for text in texts)


__all__ = [
    "OpenAICompatibleRecallEmbedding",
    "SQLiteCachedRecallEmbedding",
    "configured_recall_embedding",
]
