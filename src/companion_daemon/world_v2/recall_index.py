"""Rebuildable, source-bound hybrid recall over World-derived documents.

The ledger remains the only factual authority.  This Module stores disposable
search material at one exact projection cursor and returns evidence candidates
with their original source refs.  Retrieval scores affect accessibility only;
they never change validity, authorize an Action, or recommend behaviour.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Literal, Protocol, Self
import unicodedata

from pydantic import Field, model_validator

from .schema_core import FrozenModel, PrivacyClass
from .sqlite_coordination import configure_shared_sqlite_connection, sqlite_write_lock


RECALL_INDEX_POLICY_VERSION = "world-v2-recall-index.hybrid.1"
RECALL_RESULT_MAX_BYTES = 6_000
_PRIVACY_RANK: dict[PrivacyClass, int] = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class RecallCursor(FrozenModel):
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)


class RecallSourceBinding(FrozenModel):
    """Immutable authority closure retained by a disposable recall document."""

    source_kind: Literal[
        "committed_event",
        "execution_receipt",
        "immutable_payload",
    ]
    authority_type: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=256)
    source_world_revision: int = Field(ge=0)
    immutable_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecallDocument(FrozenModel):
    """One rebuildable search document retaining its complete source closure."""

    document_id: str = Field(min_length=1, max_length=256)
    memory_kind: Literal["episodic", "semantic", "reflective"]
    source_item_ref: str = Field(min_length=1, max_length=256)
    source_slice: Literal[
        "recent_dialogue",
        "open_threads",
        "relevant_facts",
        "recent_experiences",
        "world_life",
        "active_memory_candidates",
        "private_impressions",
    ]
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    source_bindings: tuple[RecallSourceBinding, ...] = Field(min_length=1, max_length=16)
    source_world_revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4_096)
    actor_ref: str = Field(min_length=1, max_length=256)
    subject_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    link_refs: tuple[str, ...] = Field(default=(), max_length=32)
    occurred_from: datetime
    occurred_to: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: Literal[
        "active",
        "historical",
        "superseded",
        "contradicted",
        "expired",
    ] = "active"
    privacy_class: PrivacyClass
    authority: Literal["world_fact", "defeasible_interpretation"] = "world_fact"

    @model_validator(mode="after")
    def source_and_time_are_closed(self) -> Self:
        for label, refs in (
            ("source refs", self.source_refs),
            ("subject refs", self.subject_refs),
            ("link refs", self.link_refs),
        ):
            if refs != tuple(sorted(set(refs))):
                raise ValueError(f"recall {label} must be sorted and unique")
        bindings = tuple(
            sorted(
                self.source_bindings,
                key=lambda item: (
                    item.source_kind,
                    item.authority_type,
                    item.ref,
                    item.source_world_revision,
                    item.immutable_hash,
                ),
            )
        )
        if self.source_bindings != bindings or len(
            {(item.source_kind, item.ref) for item in bindings}
        ) != len(bindings):
            raise ValueError("recall source bindings must be canonical and unique")
        if self.source_refs != tuple(sorted({item.ref for item in bindings})):
            raise ValueError("recall source refs must exactly match source bindings")
        if self.source_world_revision != max(item.source_world_revision for item in bindings):
            raise ValueError("recall source revision must match its authority closure")
        times = (
            self.occurred_from,
            self.occurred_to,
            self.valid_from,
            self.valid_to,
        )
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in times
        ):
            raise ValueError("recall document times must be timezone-aware")
        if self.occurred_to is not None and self.occurred_to < self.occurred_from:
            raise ValueError("recall occurrence interval is reversed")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to <= self.valid_from
        ):
            raise ValueError("recall validity interval is reversed")
        if (self.memory_kind == "reflective") != (self.authority == "defeasible_interpretation"):
            raise ValueError("reflective recall must remain non-factual authority")
        return self


class RecallQuery(FrozenModel):
    query_text: str = Field(min_length=1, max_length=1_024)
    cursor: RecallCursor
    actor_ref: str = Field(min_length=1, max_length=256)
    subject_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    viewer_privacy_ceiling: PrivacyClass
    at: datetime
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    link_refs: tuple[str, ...] = Field(default=(), max_length=32)
    memory_kinds: tuple[Literal["episodic", "semantic", "reflective"], ...] = ()
    include_historical: bool = False
    limit: int = Field(default=6, ge=1, le=12)
    accessibility_seed: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def query_is_canonical(self) -> Self:
        if self.subject_refs != tuple(sorted(set(self.subject_refs))):
            raise ValueError("recall query subjects must be sorted and unique")
        if self.link_refs != tuple(sorted(set(self.link_refs))):
            raise ValueError("recall query links must be sorted and unique")
        if self.memory_kinds != tuple(sorted(set(self.memory_kinds))):
            raise ValueError("recall query kinds must be sorted and unique")
        for value in (self.at, self.occurred_from, self.occurred_to):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("recall query times must be timezone-aware")
        if (
            self.occurred_from is not None
            and self.occurred_to is not None
            and self.occurred_to < self.occurred_from
        ):
            raise ValueError("recall query occurrence interval is reversed")
        return self


class RecallHit(FrozenModel):
    document: RecallDocument
    match_channels: tuple[Literal["lexical", "dense", "temporal", "structured"], ...] = Field(
        min_length=1
    )
    score_bp: int = Field(ge=0, le=10_000)
    lexical_score_bp: int = Field(ge=0, le=10_000)
    dense_score_bp: int = Field(ge=0, le=10_000)
    temporal_score_bp: int = Field(ge=0, le=10_000)
    structured_score_bp: int = Field(ge=0, le=10_000)
    accessibility_offset_bp: int = Field(ge=-500, le=500)


class RecallResult(FrozenModel):
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_version: str = Field(min_length=1, max_length=256)
    embedding_version: str = Field(min_length=1, max_length=256)
    embedding_status: Literal["unknown", "used", "degraded"] = "unknown"
    embedding_failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    index_cursor: RecallCursor
    query: RecallQuery
    hits: tuple[RecallHit, ...]


class RecallRebuildReport(FrozenModel):
    mode: Literal["noop", "cursor_only", "documents_changed"]
    document_count: int = Field(ge=0)
    sqlite_changes: int = Field(ge=0)


class RecallEmbedding(Protocol):
    version: str
    dimensions: int

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class RecallEmbeddingUnavailable(RuntimeError):
    """Recoverable dense-provider outage; other recall channels remain valid."""


class FeatureHashRecallEmbedding:
    """Zero-network dense feature projection used until semantic vectors exist.

    This adapter is intentionally named for what it is: a deterministic dense
    projection of lexical features, not a semantic model.  Deployments may
    inject a real embedding adapter through ``RecallEmbedding`` without
    changing index, audit, replay, or authority contracts.
    """

    version = "feature-hash-ngram.1"
    dimensions = 256

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            values = [0.0] * self.dimensions
            for feature in _lexical_features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
                bucket = int.from_bytes(digest[:8], "big") % self.dimensions
                sign = 1.0 if digest[8] & 1 else -1.0
                values[bucket] += sign
            vectors.append(tuple(values))
        return tuple(vectors)


class RecallIndex(Protocol):
    def rebuild(
        self,
        *,
        cursor: RecallCursor,
        documents: tuple[RecallDocument, ...],
    ) -> RecallRebuildReport: ...

    def search(self, query: RecallQuery) -> RecallResult: ...

    def snapshot(self) -> "RecallIndexSnapshot": ...


class _RecallIndexCore:
    def __init__(self, *, embedding: RecallEmbedding) -> None:
        if not embedding.version or not 1 <= embedding.dimensions <= 4_096:
            raise ValueError("recall embedding identity or dimensions are invalid")
        self._embedding = embedding
        self._index_version = f"{RECALL_INDEX_POLICY_VERSION}+embedding:{embedding.version}"
        self._last_rebuild_embedding_failure: str | None = None

    def _materialize(
        self,
        *,
        cursor: RecallCursor,
        documents: tuple[RecallDocument, ...],
    ) -> tuple[tuple[RecallDocument, tuple[float, ...]], ...]:
        ordered = tuple(sorted(documents, key=lambda item: item.document_id))
        if len({item.document_id for item in ordered}) != len(ordered):
            raise ValueError("recall document identity is duplicated")
        if any(item.source_world_revision > cursor.world_revision for item in ordered):
            raise ValueError("recall document is newer than the index cursor")
        try:
            vectors = self._embedding.embed(tuple(item.text for item in ordered))
        except RecallEmbeddingUnavailable as exc:
            vectors = tuple((0.0,) * self._embedding.dimensions for _ in ordered)
            self._last_rebuild_embedding_failure = str(exc)[:128] or "embedding_unavailable"
        else:
            self._last_rebuild_embedding_failure = None
        if len(vectors) != len(ordered):
            raise ValueError("recall embedding count does not match documents")
        normalized = tuple(self._normalize_vector(value) for value in vectors)
        return tuple(zip(ordered, normalized, strict=True))

    def _search(
        self,
        *,
        query: RecallQuery,
        cursor: RecallCursor,
        rows: tuple[tuple[RecallDocument, tuple[float, ...]], ...],
    ) -> RecallResult:
        if query.cursor != cursor:
            raise ValueError("recall query cursor does not match the sidecar cursor")
        embedding_failure = self._last_rebuild_embedding_failure
        try:
            query_vector_values = self._embedding.embed((query.query_text,))
        except RecallEmbeddingUnavailable as exc:
            query_vector_values = ((0.0,) * self._embedding.dimensions,)
            embedding_failure = str(exc)[:128] or "embedding_unavailable"
        if len(query_vector_values) != 1:
            raise ValueError("recall query embedding count is invalid")
        query_vector = self._normalize_vector(query_vector_values[0])
        query_features = _lexical_features(query.query_text)
        ranked: list[tuple[int, str, RecallHit]] = []
        for document, vector in rows:
            if not self._eligible(document, query):
                continue
            lexical = _lexical_score(
                query_features,
                _lexical_features(document.text),
            )
            dense = max(0, min(10_000, round(_cosine(query_vector, vector) * 10_000)))
            structured = _structured_score(query.link_refs, document.link_refs)
            temporal = _temporal_score(query, document)
            channels: list[Literal["lexical", "dense", "temporal", "structured"]] = []
            if lexical >= 1_000:
                channels.append("lexical")
            if dense >= 5_500:
                channels.append("dense")
            if structured:
                channels.append("structured")
            # Time constrains and explains an otherwise matched recollection;
            # recency alone must never retrieve unrelated material.
            if temporal and (query.occurred_from is not None or query.occurred_to is not None):
                channels.append("temporal")
            if not any(channel in channels for channel in ("lexical", "dense", "structured")):
                continue
            accessibility = _accessibility_offset(
                seed=query.accessibility_seed,
                document_id=document.document_id,
            )
            score = max(
                0,
                min(
                    10_000,
                    round(
                        lexical * 0.35
                        + dense * 0.35
                        + structured * 0.20
                        + temporal * 0.10
                        + accessibility
                    ),
                ),
            )
            hit = RecallHit(
                document=document,
                match_channels=tuple(channels),
                score_bp=score,
                lexical_score_bp=lexical,
                dense_score_bp=dense,
                temporal_score_bp=temporal,
                structured_score_bp=structured,
                accessibility_offset_bp=accessibility,
            )
            ranked.append((score, document.document_id, hit))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected: list[RecallHit] = []
        for _, _, hit in ranked:
            if len(selected) >= query.limit:
                break
            candidate = (*selected, hit)
            if (
                len(
                    _canonical_json([item.model_dump(mode="json") for item in candidate]).encode(
                        "utf-8"
                    )
                )
                > RECALL_RESULT_MAX_BYTES
            ):
                continue
            selected.append(hit)
        hits = tuple(selected)
        query_hash = recall_query_hash(
            index_version=self._index_version,
            query=query,
        )
        result_hash = recall_result_hash(
            query_hash=query_hash,
            cursor=cursor,
            hit_values=[item.model_dump(mode="json") for item in hits],
        )
        return RecallResult(
            query_hash=query_hash,
            result_hash=result_hash,
            index_version=self._index_version,
            embedding_version=self._embedding.version,
            embedding_status=("degraded" if embedding_failure is not None else "used"),
            embedding_failure_code=embedding_failure,
            index_cursor=cursor,
            query=query,
            hits=hits,
        )

    def _normalize_vector(self, value: tuple[float, ...]) -> tuple[float, ...]:
        if len(value) != self._embedding.dimensions:
            raise ValueError("recall embedding dimensions do not match the adapter")
        if any(not isinstance(item, (int, float)) or not math.isfinite(item) for item in value):
            raise ValueError("recall embedding contains a non-finite value")
        magnitude = math.sqrt(sum(float(item) ** 2 for item in value))
        if magnitude <= 0:
            return tuple(0.0 for _ in value)
        return tuple(round(float(item) / magnitude, 12) for item in value)

    @staticmethod
    def _eligible(document: RecallDocument, query: RecallQuery) -> bool:
        if document.actor_ref != query.actor_ref:
            return False
        if not set(document.subject_refs) & set(query.subject_refs):
            return False
        if _PRIVACY_RANK[document.privacy_class] > _PRIVACY_RANK[query.viewer_privacy_ceiling]:
            return False
        if query.memory_kinds and document.memory_kind not in query.memory_kinds:
            return False
        if not query.include_historical and document.status != "active":
            return False
        if (
            document.valid_from is not None
            and query.at < document.valid_from
            and not query.include_historical
        ):
            return False
        if (
            document.valid_to is not None
            and query.at >= document.valid_to
            and not query.include_historical
        ):
            return False
        document_end = document.occurred_to or document.occurred_from
        if query.occurred_from is not None and document_end < query.occurred_from:
            return False
        if query.occurred_to is not None and document.occurred_from > query.occurred_to:
            return False
        return True


class RecallIndexSnapshot:
    """Immutable process-local search view for one exact ledger cursor."""

    __slots__ = ("_core", "_cursor", "_rows")

    def __init__(
        self,
        *,
        core: _RecallIndexCore,
        cursor: RecallCursor,
        rows: tuple[tuple[RecallDocument, tuple[float, ...]], ...],
    ) -> None:
        self._core = core
        self._cursor = cursor
        self._rows = rows

    @property
    def cursor(self) -> RecallCursor:
        return self._cursor

    @property
    def documents(self) -> tuple[RecallDocument, ...]:
        return tuple(document for document, _ in self._rows)

    def search(self, query: RecallQuery) -> RecallResult:
        return self._core._search(
            query=query,
            cursor=self._cursor,
            rows=self._rows,
        )


class InMemoryRecallIndex(_RecallIndexCore):
    def __init__(self, *, embedding: RecallEmbedding) -> None:
        super().__init__(embedding=embedding)
        self._cursor: RecallCursor | None = None
        self._rows: tuple[tuple[RecallDocument, tuple[float, ...]], ...] = ()

    def rebuild(
        self,
        *,
        cursor: RecallCursor,
        documents: tuple[RecallDocument, ...],
    ) -> RecallRebuildReport:
        materialized = self._materialize(cursor=cursor, documents=documents)
        mode: Literal["noop", "cursor_only", "documents_changed"]
        if self._cursor == cursor and self._rows == materialized:
            mode = "noop"
        elif self._rows == materialized:
            mode = "cursor_only"
        else:
            mode = "documents_changed"
        self._rows = materialized
        self._cursor = cursor
        return RecallRebuildReport(
            mode=mode,
            document_count=len(materialized),
            sqlite_changes=0,
        )

    def search(self, query: RecallQuery) -> RecallResult:
        if self._cursor is None:
            raise ValueError("recall index has not been built")
        return self._search(query=query, cursor=self._cursor, rows=self._rows)

    def snapshot(self) -> RecallIndexSnapshot:
        if self._cursor is None:
            raise ValueError("recall index has not been built")
        return RecallIndexSnapshot(
            core=self,
            cursor=self._cursor,
            rows=self._rows,
        )


class SQLiteRecallIndex(_RecallIndexCore):
    """Disposable SQLite adapter; deleting its rows loses no World authority."""

    def __init__(
        self,
        *,
        path: str | Path,
        world_id: str,
        embedding: RecallEmbedding,
    ) -> None:
        super().__init__(embedding=embedding)
        if not world_id:
            raise ValueError("SQLite recall index requires a world")
        self._world_id = world_id
        self._path = str(path)
        self._write_lock = sqlite_write_lock(self._path)
        self._connection = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
        )
        configure_shared_sqlite_connection(self._connection)
        with self._write_lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS world_v2_recall_index_heads (
                    world_id TEXT PRIMARY KEY,
                    world_revision INTEGER NOT NULL,
                    deliberation_revision INTEGER NOT NULL,
                    ledger_sequence INTEGER NOT NULL,
                    index_version TEXT NOT NULL,
                    document_set_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS world_v2_recall_documents (
                    world_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    memory_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    privacy_rank INTEGER NOT NULL,
                    occurred_from TEXT NOT NULL,
                    occurred_to TEXT,
                    PRIMARY KEY (world_id, document_id)
                );
                CREATE INDEX IF NOT EXISTS world_v2_recall_document_filter
                ON world_v2_recall_documents(
                    world_id, status, memory_kind, privacy_rank, occurred_from
                );
                """
            )

    def rebuild(
        self,
        *,
        cursor: RecallCursor,
        documents: tuple[RecallDocument, ...],
    ) -> RecallRebuildReport:
        head = self._connection.execute(
            """
            SELECT world_revision, deliberation_revision, ledger_sequence,
                   index_version, document_set_hash
            FROM world_v2_recall_index_heads WHERE world_id = ?
            """,
            (self._world_id,),
        ).fetchone()
        reusable: dict[str, tuple[str, tuple[float, ...]]] = {}
        if head is not None and str(head[3]) == self._index_version:
            reusable = {
                str(document_id): (
                    str(document_json),
                    tuple(float(item) for item in json.loads(str(embedding_json))),
                )
                for document_id, document_json, embedding_json in self._connection.execute(
                    """
                    SELECT document_id, document_json, embedding_json
                    FROM world_v2_recall_documents
                    WHERE world_id = ?
                    """,
                    (self._world_id,),
                ).fetchall()
            }
        ordered = tuple(sorted(documents, key=lambda item: item.document_id))
        if len({item.document_id for item in ordered}) != len(ordered):
            raise ValueError("recall document identity is duplicated")
        if any(item.source_world_revision > cursor.world_revision for item in ordered):
            raise ValueError("recall document is newer than the index cursor")
        fresh = tuple(
            item
            for item in ordered
            if (
                reusable.get(item.document_id, ("", ()))[0]
                != _canonical_json(item.model_dump(mode="json"))
                # A recoverable provider outage materializes a zero vector so
                # lexical/temporal/structured recall remains available.  Do
                # not cache that degraded vector as if it were a completed
                # semantic embedding; the next refresh gets another chance.
                or not any(reusable.get(item.document_id, ("", ()))[1])
            )
        )
        fresh_rows = {
            item.document_id: vector
            for item, vector in self._materialize(cursor=cursor, documents=fresh)
        }
        rows = tuple(
            (
                item,
                (
                    reusable[item.document_id][1]
                    if item.document_id in reusable
                    and reusable[item.document_id][0]
                    == _canonical_json(item.model_dump(mode="json"))
                    else fresh_rows[item.document_id]
                ),
            )
            for item in ordered
        )
        if any(
            len(vector) != self._embedding.dimensions
            or any(not math.isfinite(value) for value in vector)
            for _, vector in rows
        ):
            raise ValueError("cached recall embedding is incompatible with its adapter")
        set_hash = _digest(
            [
                {
                    "document": document.model_dump(mode="json"),
                    "embedding": vector,
                }
                for document, vector in rows
            ]
        )
        exact_cursor = (
            cursor.world_revision,
            cursor.deliberation_revision,
            cursor.ledger_sequence,
        )
        if (
            head is not None
            and (
                int(head[0]),
                int(head[1]),
                int(head[2]),
            )
            == exact_cursor
            and str(head[3]) == self._index_version
            and str(head[4]) == set_hash
        ):
            return RecallRebuildReport(
                mode="noop",
                document_count=len(rows),
                sqlite_changes=0,
            )
        changes_before = self._connection.total_changes
        with self._write_lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                documents_changed = (
                    head is None or str(head[3]) != self._index_version or str(head[4]) != set_hash
                )
                if documents_changed:
                    retained_ids = tuple(document.document_id for document, _ in rows)
                    if retained_ids:
                        placeholders = ",".join("?" for _ in retained_ids)
                        self._connection.execute(
                            f"""
                            DELETE FROM world_v2_recall_documents
                            WHERE world_id = ? AND document_id NOT IN ({placeholders})
                            """,
                            (self._world_id, *retained_ids),
                        )
                    else:
                        self._connection.execute(
                            "DELETE FROM world_v2_recall_documents WHERE world_id = ?",
                            (self._world_id,),
                        )
                    self._connection.executemany(
                        """
                        INSERT INTO world_v2_recall_documents(
                            world_id, document_id, document_json, embedding_json,
                            memory_kind, status, privacy_rank, occurred_from, occurred_to
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(world_id, document_id) DO UPDATE SET
                            document_json=excluded.document_json,
                            embedding_json=excluded.embedding_json,
                            memory_kind=excluded.memory_kind,
                            status=excluded.status,
                            privacy_rank=excluded.privacy_rank,
                            occurred_from=excluded.occurred_from,
                            occurred_to=excluded.occurred_to
                        WHERE document_json != excluded.document_json
                           OR embedding_json != excluded.embedding_json
                        """,
                        (
                            (
                                self._world_id,
                                document.document_id,
                                _canonical_json(document.model_dump(mode="json")),
                                _canonical_json(vector),
                                document.memory_kind,
                                document.status,
                                _PRIVACY_RANK[document.privacy_class],
                                document.occurred_from.isoformat(),
                                (
                                    document.occurred_to.isoformat()
                                    if document.occurred_to is not None
                                    else None
                                ),
                            )
                            for document, vector in rows
                        ),
                    )
                self._connection.execute(
                    """
                    INSERT INTO world_v2_recall_index_heads(
                        world_id, world_revision, deliberation_revision,
                        ledger_sequence, index_version, document_set_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(world_id) DO UPDATE SET
                        world_revision=excluded.world_revision,
                        deliberation_revision=excluded.deliberation_revision,
                        ledger_sequence=excluded.ledger_sequence,
                        index_version=excluded.index_version,
                        document_set_hash=excluded.document_set_hash
                    """,
                    (
                        self._world_id,
                        cursor.world_revision,
                        cursor.deliberation_revision,
                        cursor.ledger_sequence,
                        self._index_version,
                        set_hash,
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return RecallRebuildReport(
            mode=(
                "documents_changed"
                if head is None or str(head[3]) != self._index_version or str(head[4]) != set_hash
                else "cursor_only"
            ),
            document_count=len(rows),
            sqlite_changes=self._connection.total_changes - changes_before,
        )

    def search(self, query: RecallQuery) -> RecallResult:
        return self.snapshot().search(query)

    def snapshot(self) -> RecallIndexSnapshot:
        head = self._connection.execute(
            """
            SELECT world_revision, deliberation_revision, ledger_sequence, index_version
            FROM world_v2_recall_index_heads WHERE world_id = ?
            """,
            (self._world_id,),
        ).fetchone()
        if head is None:
            raise ValueError("recall index has not been built")
        cursor = RecallCursor(
            world_revision=int(head[0]),
            deliberation_revision=int(head[1]),
            ledger_sequence=int(head[2]),
        )
        if str(head[3]) != self._index_version:
            raise ValueError("recall index adapter version does not match stored rows")
        stored = self._connection.execute(
            """
            SELECT document_json, embedding_json
            FROM world_v2_recall_documents
            WHERE world_id = ?
            ORDER BY document_id
            """,
            (self._world_id,),
        ).fetchall()
        rows = tuple(
            (
                RecallDocument.model_validate_json(str(document_json)),
                tuple(float(item) for item in json.loads(str(embedding_json))),
            )
            for document_json, embedding_json in stored
        )
        return RecallIndexSnapshot(core=self, cursor=cursor, rows=rows)

    def close(self) -> None:
        self._connection.close()
        close_embedding = getattr(self._embedding, "close", None)
        if callable(close_embedding):
            close_embedding()


def _flush_run(
    features: set[str],
    *,
    run: list[str],
    kind: str | None,
) -> None:
    if not run or kind is None:
        return
    value = "".join(run)
    if kind == "cjk":
        for width in (2, 3):
            features.update(
                value[offset : offset + width] for offset in range(max(0, len(value) - width + 1))
            )
    elif len(value) >= 3:
        features.add(value)


def _lexical_features(text: str) -> frozenset[str]:
    features: set[str] = set()
    normalized = unicodedata.normalize("NFKC", text).casefold()
    run: list[str] = []
    kind: str | None = None
    for character in normalized:
        codepoint = ord(character)
        next_kind = (
            "cjk"
            if (
                0x3400 <= codepoint <= 0x4DBF
                or 0x4E00 <= codepoint <= 0x9FFF
                or 0x3040 <= codepoint <= 0x30FF
            )
            else "word"
            if character.isalnum()
            else None
        )
        if next_kind != kind:
            _flush_run(features, run=run, kind=kind)
            run = []
            kind = next_kind
        if next_kind is not None:
            run.append(character)
    _flush_run(features, run=run, kind=kind)
    return frozenset(features)


def _lexical_score(
    query: frozenset[str],
    document: frozenset[str],
) -> int:
    if not query or not document:
        return 0
    overlap = query & document
    if not overlap:
        return 0
    numerator = sum(len(item) ** 2 for item in overlap)
    denominator = max(1, sum(len(item) ** 2 for item in query))
    return min(10_000, round(numerator / denominator * 10_000))


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("recall vectors use different dimensions")
    return sum(a * b for a, b in zip(left, right, strict=True))


def _structured_score(query_refs: tuple[str, ...], document_refs: tuple[str, ...]) -> int:
    if not query_refs or not document_refs:
        return 0
    overlap = len(set(query_refs) & set(document_refs))
    return min(10_000, round(overlap / len(set(query_refs)) * 10_000))


def _temporal_score(query: RecallQuery, document: RecallDocument) -> int:
    end = document.occurred_to or document.occurred_from
    if query.occurred_from is not None and end < query.occurred_from:
        return 0
    if query.occurred_to is not None and document.occurred_from > query.occurred_to:
        return 0
    distance_seconds = max(
        0.0,
        (query.at - end).total_seconds(),
    )
    # Smooth 30-day accessibility decay; validity is handled separately.
    return max(500, round(10_000 / (1 + distance_seconds / (30 * 86_400))))


def _accessibility_offset(*, seed: str, document_id: str) -> int:
    raw = hashlib.sha256(
        _canonical_json(
            {
                "contract": "recall-accessibility-draw.1",
                "seed": seed,
                "document_id": document_id,
            }
        ).encode("utf-8")
    ).digest()
    return int.from_bytes(raw[:2], "big") % 401 - 200


def recall_query_hash(*, index_version: str, query: RecallQuery) -> str:
    return _digest(
        {
            "contract": "world-v2-recall-query.1",
            "index_version": index_version,
            "query": query.model_dump(mode="json"),
        }
    )


def recall_result_hash(
    *,
    query_hash: str,
    cursor: RecallCursor,
    hit_values: list[dict[str, object]],
) -> str:
    return _digest(
        {
            "query_hash": query_hash,
            "cursor": cursor.model_dump(mode="json"),
            "hits": hit_values,
        }
    )


__all__ = [
    "InMemoryRecallIndex",
    "RECALL_INDEX_POLICY_VERSION",
    "RECALL_RESULT_MAX_BYTES",
    "RecallCursor",
    "RecallDocument",
    "RecallEmbedding",
    "RecallEmbeddingUnavailable",
    "RecallHit",
    "RecallIndexSnapshot",
    "RecallQuery",
    "RecallResult",
    "SQLiteRecallIndex",
    "recall_query_hash",
    "recall_result_hash",
]
