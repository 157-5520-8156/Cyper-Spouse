"""SQLite-backed Phase-1 World Perception Hub.

The Hub owns disposable external-signal state only.  It receives no ledger,
character model, Life Ecology runtime, or message transport, making it
structurally unable to claim perception or cause visible behaviour.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from ..sqlite_coordination import configure_shared_sqlite_connection, sqlite_write_lock
from .attention import SQLiteShadowAttentionCoordinator
from .live_attention import SQLiteLiveAttentionCoordinator
from .contracts import (
    ExternalSignalSourceFailure,
    ExternalSignalEmbedding,
    ExternalSignalSourceItem,
    ExternalSignalSourcePage,
    PerceptionAdvanceResult,
    PerceptionHealthSnapshot,
    SourceCursor,
    SourceHealthSnapshot,
    SourcePolicyRevision,
    SourceProfile,
    ShadowAttentionRuntime,
    LiveAttentionRuntime,
    WallClock,
)


_FAILURE_BACKOFF_SECONDS = (600, 1_800, 7_200)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_ref(prefix: str, value: object) -> str:
    return f"{prefix}:" + _sha256(_canonical_json(value).encode("utf-8"))


class SQLiteWorldPerceptionHub:
    """Advance one acquisition unit behind the deep World Perception Interface."""

    def __init__(
        self,
        *,
        path: str | Path,
        sources: tuple[SourceProfile, ...],
        wall_clock: WallClock,
        embedding: ExternalSignalEmbedding | None = None,
        shadow_attention: ShadowAttentionRuntime | None = None,
        live_attention: LiveAttentionRuntime | None = None,
    ) -> None:
        path_value = str(path)
        if not path_value or not sources:
            raise ValueError("World Perception Hub requires a path and at least one source")
        source_ids = tuple(profile.adapter.source_id for profile in sources)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("World Perception Hub source ids must be unique")
        now = wall_clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("World Perception Hub wall clock must be timezone-aware")
        self._profiles = {profile.adapter.source_id: profile for profile in sources}
        self._path = Path(path_value)
        if embedding is not None and (
            not embedding.version or not 1 <= embedding.dimensions <= 4_096
        ):
            raise ValueError("external signal embedding contract is invalid")
        self._embedding = embedding
        self._wall_clock = wall_clock
        self._advance_lock = asyncio.Lock()
        self._lock = RLock()
        self._database_write_lock = sqlite_write_lock(path_value)
        self._connection = sqlite3.connect(
            path_value,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._database_write_lock, self._lock:
            configure_shared_sqlite_connection(self._connection)
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._create_schema()
            self._connection.execute(
                "UPDATE external_perception_source_state SET is_configured = 0"
            )
            for source_id in source_ids:
                self._register_source_policy(
                    self._profiles[source_id].policy,
                    registered_at=now,
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO external_perception_source_state (
                        source_id, policy_revision, consecutive_failures, accepted_revision_count,
                        duplicate_suppressed_count, rejected_item_count,
                        last_page_rejected_item_count, is_configured, last_result
                    ) VALUES (?, ?, 0, 0, 0, 0, 0, 1, 'never_polled')
                    """,
                    (source_id, self._profiles[source_id].policy.policy_revision),
                )
                self._connection.execute(
                    """
                    UPDATE external_perception_source_state
                    SET policy_revision = ?, is_configured = 1 WHERE source_id = ?
                    """,
                    (self._profiles[source_id].policy.policy_revision, source_id),
                )
            self._reconcile_embedding_jobs(observed_at=now)
            self._record_storage_sample(observed_at=now, force=True)
        if shadow_attention is not None and live_attention is not None:
            raise ValueError("World Perception Hub cannot run shadow and live attention together")
        self._attention_coordinator = (
            SQLiteShadowAttentionCoordinator(
                connection=self._connection,
                lock=self._lock,
                database_write_lock=self._database_write_lock,
                runtime=shadow_attention,
                exposable_source_ids=tuple(
                    profile.adapter.source_id
                    for profile in sources
                    if profile.policy.may_expose_to_character_model
                ),
                wall_clock=wall_clock,
            )
            if shadow_attention is not None
            else (
                SQLiteLiveAttentionCoordinator(
                    connection=self._connection,
                    lock=self._lock,
                    database_write_lock=self._database_write_lock,
                    runtime=live_attention,
                    exposable_source_ids=tuple(
                        profile.adapter.source_id
                        for profile in sources
                        if profile.policy.may_expose_to_character_model
                        and profile.policy.may_freeze_durable_snapshot
                    ),
                    wall_clock=wall_clock,
                )
                if live_attention is not None
                else None
            )
        )

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_perception_source_state (
                source_id TEXT PRIMARY KEY,
                policy_revision TEXT NOT NULL,
                cursor_value TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                next_refresh_at TEXT,
                consecutive_failures INTEGER NOT NULL,
                last_failure_code TEXT,
                accepted_revision_count INTEGER NOT NULL,
                duplicate_suppressed_count INTEGER NOT NULL,
                rejected_item_count INTEGER NOT NULL,
                last_page_rejected_item_count INTEGER NOT NULL,
                is_configured INTEGER NOT NULL CHECK (is_configured IN (0, 1)),
                last_result TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_perception_source_policies (
                policy_revision TEXT PRIMARY KEY,
                policy_json TEXT NOT NULL,
                registered_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_perception_raw_evidence (
                evidence_ref TEXT PRIMARY KEY,
                evidence_hash TEXT NOT NULL,
                media_type TEXT NOT NULL,
                content BLOB NOT NULL,
                first_observed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_perception_source_evidence (
                source_id TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (source_id, evidence_ref)
            );

            CREATE TABLE IF NOT EXISTS external_signal_revisions (
                signal_revision_ref TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                source_id TEXT NOT NULL,
                upstream_item_id TEXT NOT NULL,
                normalized_json TEXT NOT NULL,
                normalized_hash TEXT NOT NULL,
                source_policy_revision TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                effective_expires_at TEXT NOT NULL,
                supersedes_ref TEXT,
                correction_of_ref TEXT,
                UNIQUE (source_id, upstream_item_id, revision)
            );

            CREATE TABLE IF NOT EXISTS external_signal_observation_state (
                signal_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                latest_signal_revision_ref TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                effective_expires_at TEXT NOT NULL,
                normalized_delete_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_perception_rejected_items (
                source_id TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                upstream_item_id TEXT NOT NULL,
                item_hash TEXT NOT NULL,
                failure_code TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (source_id, evidence_ref, upstream_item_id)
            );

            CREATE TABLE IF NOT EXISTS external_signal_cluster_membership (
                signal_id TEXT PRIMARY KEY,
                cluster_ref TEXT NOT NULL,
                basis_key TEXT NOT NULL,
                assigned_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS external_signal_cluster_basis
            ON external_signal_cluster_membership (basis_key, assigned_at);

            CREATE TABLE IF NOT EXISTS external_signal_search_documents (
                signal_revision_ref TEXT PRIMARY KEY,
                search_text TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS external_signal_fts
            USING fts5(signal_revision_ref UNINDEXED, search_text, tokenize='unicode61');

            CREATE TABLE IF NOT EXISTS external_signal_embeddings (
                signal_revision_ref TEXT PRIMARY KEY,
                embedding_version TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                embedding_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_signal_embedding_jobs (
                signal_revision_ref TEXT PRIMARY KEY,
                search_text TEXT NOT NULL,
                target_embedding_version TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL,
                next_attempt_at TEXT NOT NULL,
                last_failure_code TEXT
            );

            CREATE TABLE IF NOT EXISTS external_perception_index_health (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                last_embedding_failure_code TEXT,
                last_embedding_attempt_at TEXT
            );

            INSERT OR IGNORE INTO external_perception_index_health (
                singleton, last_embedding_failure_code, last_embedding_attempt_at
            ) VALUES (1, NULL, NULL);

            CREATE TABLE IF NOT EXISTS external_perception_storage_samples (
                recorded_at TEXT PRIMARY KEY,
                sidecar_bytes INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS external_signal_revisions_latest
            ON external_signal_revisions (source_id, upstream_item_id, revision DESC);

            CREATE INDEX IF NOT EXISTS external_signal_revisions_expiry
            ON external_signal_revisions (effective_expires_at);

            CREATE INDEX IF NOT EXISTS external_signal_revisions_observed
            ON external_signal_revisions (observed_at);

            CREATE INDEX IF NOT EXISTS external_signal_revisions_correction
            ON external_signal_revisions (correction_of_ref)
            WHERE correction_of_ref IS NOT NULL;

            CREATE INDEX IF NOT EXISTS external_signal_observation_expiry
            ON external_signal_observation_state (effective_expires_at);

            CREATE INDEX IF NOT EXISTS external_signal_observation_retention
            ON external_signal_observation_state (normalized_delete_at);

            CREATE INDEX IF NOT EXISTS external_signal_embedding_jobs_due
            ON external_signal_embedding_jobs (next_attempt_at);

            CREATE INDEX IF NOT EXISTS external_perception_sources_due
            ON external_perception_source_state (is_configured, next_refresh_at);
            """
        )

    def _register_source_policy(
        self,
        policy: SourcePolicyRevision,
        *,
        registered_at: datetime,
    ) -> None:
        policy_json = _canonical_json(policy.model_dump(mode="json"))
        existing = self._connection.execute(
            """
            SELECT policy_json FROM external_perception_source_policies
            WHERE policy_revision = ?
            """,
            (policy.policy_revision,),
        ).fetchone()
        if existing is not None and str(existing["policy_json"]) != policy_json:
            raise ValueError("source policy revision content changed")
        self._connection.execute(
            """
            INSERT OR IGNORE INTO external_perception_source_policies (
                policy_revision, policy_json, registered_at
            ) VALUES (?, ?, ?)
            """,
            (policy.policy_revision, policy_json, _iso_utc(registered_at)),
        )

    async def advance_once(self, *, observed_at: datetime) -> PerceptionAdvanceResult:
        async with self._advance_lock:
            result = await self._advance_once(observed_at=observed_at)
            self._record_storage_sample(observed_at=observed_at)
            return result

    def _record_storage_sample(
        self,
        *,
        observed_at: datetime,
        force: bool = False,
    ) -> None:
        latest = self._connection.execute(
            """
            SELECT recorded_at FROM external_perception_storage_samples
            ORDER BY recorded_at DESC LIMIT 1
            """
        ).fetchone()
        if (
            not force
            and latest is not None
            and observed_at < _parse_datetime(str(latest["recorded_at"])) + timedelta(hours=1)
        ):
            return
        current_bytes = _file_size(self._path) + _file_size(Path(f"{self._path}-wal"))
        with self._database_write_lock, self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO external_perception_storage_samples (
                    recorded_at, sidecar_bytes
                ) VALUES (?, ?)
                """,
                (_iso_utc(observed_at), current_bytes),
            )
            self._connection.execute(
                """
                DELETE FROM external_perception_storage_samples
                WHERE recorded_at < ?
                """,
                (_iso_utc(observed_at - timedelta(hours=48)),),
            )

    async def _advance_once(self, *, observed_at: datetime) -> PerceptionAdvanceResult:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("World Perception advance time must be timezone-aware")
        self._delete_expired_raw_evidence(observed_at)
        self._delete_expired_search_index(observed_at)
        self._delete_expired_normalized_signals(observed_at)
        due = self._due_sources(observed_at)
        if due:
            source_id = due[0]
            profile = self._profiles[source_id]
            cursor = self._source_cursor(source_id)
            try:
                page = await profile.adapter.fetch(
                    after=cursor,
                    observed_at=observed_at,
                    deadline_at=observed_at + timedelta(seconds=profile.fetch_deadline_seconds),
                    limit=profile.page_limit,
                )
            except ExternalSignalSourceFailure as exc:
                self._record_failure(
                    source_id=source_id,
                    observed_at=observed_at,
                    failure_code=exc.failure_code,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                return PerceptionAdvanceResult(
                    status="retry_wait",
                    progressed_units=1,
                    next_wake_at=self._next_wake_at(),
                    more_due=self._has_due_work(observed_at),
                )
            except Exception as exc:  # external adapters may fail before typed parsing
                self._record_failure(
                    source_id=source_id,
                    observed_at=observed_at,
                    failure_code=f"source_exception:{type(exc).__name__}"[:128],
                    retry_after_seconds=None,
                )
                return PerceptionAdvanceResult(
                    status="retry_wait",
                    progressed_units=1,
                    next_wake_at=self._next_wake_at(),
                    more_due=self._has_due_work(observed_at),
                )
            self._record_page(
                source_id=source_id,
                profile=profile,
                observed_at=observed_at,
                page=page,
            )
            return PerceptionAdvanceResult(
                status="progressed",
                progressed_units=1,
                next_wake_at=self._next_wake_at(),
                more_due=self._has_due_work(observed_at),
            )
        if self._attention_coordinator is not None:
            attention_result = await self._attention_coordinator.advance_once(
                observed_at=observed_at
            )
            if attention_result is not None:
                return attention_result
        embedding_jobs = self._due_embedding_jobs(observed_at)
        if embedding_jobs:
            return await self._advance_embedding_job(
                signal_revision_ref=embedding_jobs[0],
                observed_at=observed_at,
            )
        return PerceptionAdvanceResult(
            status="idle",
            progressed_units=0,
            next_wake_at=self._next_wake_at(),
            more_due=False,
        )

    def _has_due_work(self, observed_at: datetime) -> bool:
        return bool(
            self._due_sources(observed_at)
            or (
                self._attention_coordinator is not None
                and self._attention_coordinator.has_due_work(observed_at)
            )
            or self._due_embedding_jobs(observed_at)
        )

    def _delete_expired_raw_evidence(self, observed_at: datetime) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    DELETE FROM external_perception_source_evidence
                    WHERE expires_at <= ?
                    """,
                    (_iso_utc(observed_at),),
                )
                self._connection.execute(
                    """
                    DELETE FROM external_perception_raw_evidence AS evidence
                    WHERE NOT EXISTS (
                        SELECT 1 FROM external_perception_source_evidence AS observation
                        WHERE observation.evidence_ref = evidence.evidence_ref
                    )
                    """
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _delete_expired_search_index(self, observed_at: datetime) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                expired = self._connection.execute(
                    """
                    SELECT latest_signal_revision_ref
                    FROM external_signal_observation_state
                    WHERE effective_expires_at <= ?
                    """,
                    (_iso_utc(observed_at),),
                ).fetchall()
                for row in expired:
                    self._delete_revision_index(str(row["latest_signal_revision_ref"]))
                self._update_embedding_health(observed_at=observed_at)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _delete_revision_index(self, signal_revision_ref: str) -> None:
        self._connection.execute(
            "DELETE FROM external_signal_fts WHERE signal_revision_ref = ?",
            (signal_revision_ref,),
        )
        self._connection.execute(
            "DELETE FROM external_signal_embeddings WHERE signal_revision_ref = ?",
            (signal_revision_ref,),
        )
        self._connection.execute(
            "DELETE FROM external_signal_embedding_jobs WHERE signal_revision_ref = ?",
            (signal_revision_ref,),
        )
        self._connection.execute(
            "DELETE FROM external_signal_search_documents WHERE signal_revision_ref = ?",
            (signal_revision_ref,),
        )

    def _delete_expired_normalized_signals(self, observed_at: datetime) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                expired = self._connection.execute(
                    """
                    SELECT state.signal_id, state.source_id
                    FROM external_signal_observation_state AS state
                    WHERE state.normalized_delete_at <= ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM external_signal_revisions AS target
                          JOIN external_signal_revisions AS correction
                            ON correction.correction_of_ref = target.signal_revision_ref
                          JOIN external_signal_observation_state AS correction_state
                            ON correction_state.signal_id = correction.signal_id
                          WHERE target.signal_id = state.signal_id
                            AND correction_state.normalized_delete_at > ?
                      )
                    """,
                    (_iso_utc(observed_at), _iso_utc(observed_at)),
                ).fetchall()
                for row in expired:
                    signal_id = str(row["signal_id"])
                    revision_rows = self._connection.execute(
                        """
                        SELECT signal_revision_ref FROM external_signal_revisions
                        WHERE signal_id = ?
                        """,
                        (signal_id,),
                    ).fetchall()
                    for revision in revision_rows:
                        self._delete_revision_index(str(revision["signal_revision_ref"]))
                    self._connection.execute(
                        "DELETE FROM external_signal_cluster_membership WHERE signal_id = ?",
                        (signal_id,),
                    )
                    self._connection.execute(
                        "DELETE FROM external_signal_revisions WHERE signal_id = ?",
                        (signal_id,),
                    )
                    self._connection.execute(
                        "DELETE FROM external_signal_observation_state WHERE signal_id = ?",
                        (signal_id,),
                    )
                    self._connection.execute(
                        """
                        UPDATE external_perception_source_state
                        SET cursor_value = NULL, next_refresh_at = NULL
                        WHERE source_id = ? AND is_configured = 1
                        """,
                        (row["source_id"],),
                    )
                self._update_embedding_health(observed_at=observed_at)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _due_sources(self, observed_at: datetime) -> tuple[str, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT source_id, next_refresh_at
                FROM external_perception_source_state
                WHERE is_configured = 1
                ORDER BY COALESCE(next_refresh_at, ''), source_id
                """
            ).fetchall()
        return tuple(
            str(row["source_id"])
            for row in rows
            if row["next_refresh_at"] is None
            or observed_at >= _parse_datetime(str(row["next_refresh_at"]))
        )

    def _due_embedding_jobs(self, observed_at: datetime) -> tuple[str, ...]:
        if self._embedding is None:
            return ()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT signal_revision_ref
                FROM external_signal_embedding_jobs
                WHERE next_attempt_at <= ?
                ORDER BY next_attempt_at, signal_revision_ref
                """,
                (_iso_utc(observed_at),),
            ).fetchall()
        return tuple(str(row["signal_revision_ref"]) for row in rows)

    def _source_cursor(self, source_id: str) -> SourceCursor | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT cursor_value FROM external_perception_source_state
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
        value = row["cursor_value"] if row is not None else None
        return SourceCursor(opaque_value=str(value)) if value is not None else None

    def _record_page(
        self,
        *,
        source_id: str,
        profile: SourceProfile,
        observed_at: datetime,
        page: ExternalSignalSourcePage,
    ) -> None:
        evidence_hash = _sha256(page.evidence_bytes)
        evidence_ref = f"external-evidence:sha256:{evidence_hash}"
        accepted = 0
        duplicates = 0
        rejected = page.parser_rejected_item_count
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if not page.not_modified:
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO external_perception_raw_evidence (
                            evidence_ref, evidence_hash, media_type,
                            content, first_observed_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_ref,
                            evidence_hash,
                            page.evidence_media_type,
                            page.evidence_bytes,
                            _iso_utc(observed_at),
                        ),
                    )
                    self._connection.execute(
                        """
                        INSERT INTO external_perception_source_evidence (
                            source_id, evidence_ref, last_observed_at, expires_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(source_id, evidence_ref) DO UPDATE SET
                            last_observed_at = excluded.last_observed_at,
                            expires_at = CASE
                                WHEN excluded.expires_at > expires_at
                                THEN excluded.expires_at ELSE expires_at
                            END
                        """,
                        (
                            source_id,
                            evidence_ref,
                            _iso_utc(observed_at),
                            _iso_utc(
                                observed_at + timedelta(seconds=profile.raw_retention_seconds)
                            ),
                        ),
                    )
                    for item in page.items:
                        self._connection.execute("SAVEPOINT external_perception_item")
                        try:
                            outcome = self._record_item(
                                source_id=source_id,
                                evidence_ref=evidence_ref,
                                observed_at=observed_at,
                                signal_ttl_seconds=profile.signal_ttl_seconds,
                                normalized_retention_seconds=(
                                    profile.effective_normalized_retention_seconds
                                ),
                                policy=profile.policy,
                                item=item,
                            )
                        except ValueError as exc:
                            self._connection.execute(
                                "ROLLBACK TO SAVEPOINT external_perception_item"
                            )
                            self._connection.execute("RELEASE SAVEPOINT external_perception_item")
                            self._record_rejected_item(
                                source_id=source_id,
                                evidence_ref=evidence_ref,
                                observed_at=observed_at,
                                item=item,
                                failure_code=str(exc),
                            )
                            rejected += 1
                            continue
                        self._connection.execute("RELEASE SAVEPOINT external_perception_item")
                        accepted += outcome == "accepted"
                        duplicates += outcome == "duplicate"
                self._connection.execute(
                    """
                    UPDATE external_perception_source_state
                    SET cursor_value = ?, last_attempt_at = ?, last_success_at = ?,
                        next_refresh_at = ?, consecutive_failures = 0,
                        last_failure_code = NULL,
                        accepted_revision_count = accepted_revision_count + ?,
                        duplicate_suppressed_count = duplicate_suppressed_count + ?,
                        rejected_item_count = rejected_item_count + ?,
                        last_page_rejected_item_count = ?, last_result = ?
                    WHERE source_id = ?
                    """,
                    (
                        page.next_cursor.opaque_value
                        if page.next_cursor is not None
                        else (
                            self._source_cursor_value_in_transaction(source_id)
                            if page.not_modified
                            else None
                        ),
                        _iso_utc(observed_at),
                        _iso_utc(observed_at),
                        _iso_utc(observed_at + timedelta(seconds=profile.poll_interval_seconds)),
                        accepted,
                        duplicates,
                        rejected,
                        rejected,
                        _source_page_result(
                            page=page,
                            accepted=accepted,
                            duplicates=duplicates,
                            rejected=rejected,
                        ),
                        source_id,
                    ),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _record_rejected_item(
        self,
        *,
        source_id: str,
        evidence_ref: str,
        observed_at: datetime,
        item: ExternalSignalSourceItem,
        failure_code: str,
    ) -> None:
        item_json = _canonical_json(item.model_dump(mode="json"))
        self._connection.execute(
            """
            INSERT OR IGNORE INTO external_perception_rejected_items (
                source_id, evidence_ref, upstream_item_id, item_hash,
                failure_code, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                evidence_ref,
                item.upstream_item_id,
                _sha256(item_json.encode("utf-8")),
                failure_code[:128],
                _iso_utc(observed_at),
            ),
        )

    def _source_cursor_value_in_transaction(self, source_id: str) -> str | None:
        row = self._connection.execute(
            """
            SELECT cursor_value FROM external_perception_source_state WHERE source_id = ?
            """,
            (source_id,),
        ).fetchone()
        return str(row["cursor_value"]) if row is not None and row["cursor_value"] else None

    def _record_item(
        self,
        *,
        source_id: str,
        evidence_ref: str,
        observed_at: datetime,
        signal_ttl_seconds: int,
        normalized_retention_seconds: int,
        policy: SourcePolicyRevision,
        item: ExternalSignalSourceItem,
    ) -> str:
        normalized_value: dict[str, Any] = item.model_dump(mode="json")
        if not policy.may_store_normalized_summary:
            normalized_value["licensed_summary"] = ""
        normalized_json = _canonical_json(normalized_value)
        normalized_hash = _sha256(normalized_json.encode("utf-8"))
        signal_id = _stable_ref(
            "external-signal",
            {"source_id": source_id, "upstream_item_id": item.upstream_item_id},
        )
        latest = self._connection.execute(
            """
            SELECT signal_revision_ref, revision, normalized_hash,
                   source_policy_revision
            FROM external_signal_revisions
            WHERE source_id = ? AND upstream_item_id = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (source_id, item.upstream_item_id),
        ).fetchone()
        configured_expiry = observed_at + timedelta(seconds=signal_ttl_seconds)
        effective_expires_at = min(
            item.expires_at or configured_expiry,
            configured_expiry,
        )
        if (
            latest is not None
            and latest["normalized_hash"] == normalized_hash
            and latest["source_policy_revision"] == policy.policy_revision
        ):
            self._record_signal_observation(
                signal_id=signal_id,
                source_id=source_id,
                signal_revision_ref=str(latest["signal_revision_ref"]),
                observed_at=observed_at,
                effective_expires_at=effective_expires_at,
                normalized_delete_at=(
                    observed_at + timedelta(seconds=normalized_retention_seconds)
                ),
            )
            self._index_signal_revision(
                signal_revision_ref=str(latest["signal_revision_ref"]),
                item=item,
                policy=policy,
                observed_at=observed_at,
            )
            return "duplicate"
        revision = int(latest["revision"]) + 1 if latest is not None else 1
        signal_revision_ref = f"{signal_id}:revision:{revision}"
        if latest is not None:
            self._delete_revision_index(str(latest["signal_revision_ref"]))
        correction_of_ref = None
        if item.correction_of_upstream_item_id is not None:
            corrected = self._connection.execute(
                """
                SELECT signal_revision_ref
                FROM external_signal_revisions
                WHERE source_id = ? AND upstream_item_id = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (source_id, item.correction_of_upstream_item_id),
            ).fetchone()
            if corrected is None:
                raise ValueError("source correction target has not been observed")
            correction_of_ref = str(corrected["signal_revision_ref"])
        self._connection.execute(
            """
            INSERT INTO external_signal_revisions (
                signal_revision_ref, signal_id, revision, source_id,
                upstream_item_id, normalized_json, normalized_hash, evidence_ref,
                source_policy_revision,
                observed_at, effective_expires_at,
                supersedes_ref, correction_of_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_revision_ref,
                signal_id,
                revision,
                source_id,
                item.upstream_item_id,
                normalized_json,
                normalized_hash,
                evidence_ref,
                policy.policy_revision,
                _iso_utc(observed_at),
                _iso_utc(effective_expires_at),
                str(latest["signal_revision_ref"]) if latest is not None else None,
                correction_of_ref,
            ),
        )
        self._record_signal_observation(
            signal_id=signal_id,
            source_id=source_id,
            signal_revision_ref=signal_revision_ref,
            observed_at=observed_at,
            effective_expires_at=effective_expires_at,
            normalized_delete_at=(observed_at + timedelta(seconds=normalized_retention_seconds)),
        )
        self._assign_cluster(
            signal_id=signal_id,
            item=item,
            correction_of_ref=correction_of_ref,
            observed_at=observed_at,
        )
        self._index_signal_revision(
            signal_revision_ref=signal_revision_ref,
            item=item,
            policy=policy,
            observed_at=observed_at,
        )
        return "accepted"

    def _record_signal_observation(
        self,
        *,
        signal_id: str,
        source_id: str,
        signal_revision_ref: str,
        observed_at: datetime,
        effective_expires_at: datetime,
        normalized_delete_at: datetime,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO external_signal_observation_state (
                signal_id, source_id, latest_signal_revision_ref, last_observed_at,
                effective_expires_at, normalized_delete_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_id) DO UPDATE SET
                source_id = excluded.source_id,
                latest_signal_revision_ref = excluded.latest_signal_revision_ref,
                last_observed_at = excluded.last_observed_at,
                effective_expires_at = excluded.effective_expires_at,
                normalized_delete_at = excluded.normalized_delete_at
            """,
            (
                signal_id,
                source_id,
                signal_revision_ref,
                _iso_utc(observed_at),
                _iso_utc(effective_expires_at),
                _iso_utc(normalized_delete_at),
            ),
        )

    def _index_signal_revision(
        self,
        *,
        signal_revision_ref: str,
        item: ExternalSignalSourceItem,
        policy: SourcePolicyRevision,
        observed_at: datetime,
    ) -> None:
        search_text = " ".join(
            part
            for part in (
                item.headline,
                item.licensed_summary if policy.may_store_normalized_summary else "",
                " ".join(item.entities),
                item.upstream_publisher_ref,
            )
            if part
        )
        inserted = self._connection.execute(
            """
            INSERT OR IGNORE INTO external_signal_search_documents (
                signal_revision_ref, search_text
            ) VALUES (?, ?)
            """,
            (signal_revision_ref, search_text),
        )
        if inserted.rowcount:
            self._connection.execute(
                """
                INSERT INTO external_signal_fts (signal_revision_ref, search_text)
                VALUES (?, ?)
                """,
                (signal_revision_ref, search_text),
            )
        if self._embedding is None or not policy.may_embed:
            return
        self._connection.execute(
            """
            INSERT OR IGNORE INTO external_signal_embedding_jobs (
                signal_revision_ref, search_text, target_embedding_version,
                consecutive_failures,
                next_attempt_at, last_failure_code
            ) VALUES (?, ?, ?, 0, ?, NULL)
            """,
            (
                signal_revision_ref,
                search_text,
                self._embedding.version,
                _iso_utc(observed_at),
            ),
        )

    def _reconcile_embedding_jobs(self, *, observed_at: datetime) -> None:
        if self._embedding is None:
            return
        rows = self._connection.execute(
            """
            SELECT document.signal_revision_ref, document.search_text,
                   policy.policy_json, embedding.embedding_version
            FROM external_signal_search_documents AS document
            JOIN external_signal_revisions AS revision
              ON revision.signal_revision_ref = document.signal_revision_ref
            JOIN external_perception_source_policies AS policy
              ON policy.policy_revision = revision.source_policy_revision
            LEFT JOIN external_signal_embeddings AS embedding
              ON embedding.signal_revision_ref = document.signal_revision_ref
            """
        ).fetchall()
        for row in rows:
            policy = json.loads(str(row["policy_json"]))
            if not policy.get("may_embed"):
                continue
            if row["embedding_version"] == self._embedding.version:
                continue
            self._connection.execute(
                """
                INSERT INTO external_signal_embedding_jobs (
                    signal_revision_ref, search_text, target_embedding_version,
                    consecutive_failures,
                    next_attempt_at, last_failure_code
                ) VALUES (?, ?, ?, 0, ?, NULL)
                ON CONFLICT(signal_revision_ref) DO UPDATE SET
                    search_text = excluded.search_text,
                    consecutive_failures = CASE
                        WHEN target_embedding_version != excluded.target_embedding_version
                        THEN 0 ELSE consecutive_failures
                    END,
                    next_attempt_at = CASE
                        WHEN target_embedding_version != excluded.target_embedding_version
                        THEN excluded.next_attempt_at ELSE next_attempt_at
                    END,
                    last_failure_code = CASE
                        WHEN target_embedding_version != excluded.target_embedding_version
                        THEN NULL ELSE last_failure_code
                    END,
                    target_embedding_version = excluded.target_embedding_version
                """,
                (
                    row["signal_revision_ref"],
                    row["search_text"],
                    self._embedding.version,
                    _iso_utc(observed_at),
                ),
            )

    async def _advance_embedding_job(
        self,
        *,
        signal_revision_ref: str,
        observed_at: datetime,
    ) -> PerceptionAdvanceResult:
        if self._embedding is None:
            raise RuntimeError("embedding job selected without an embedding provider")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT search_text, target_embedding_version, consecutive_failures
                FROM external_signal_embedding_jobs
                WHERE signal_revision_ref = ?
                """,
                (signal_revision_ref,),
            ).fetchone()
        if row is None:
            return PerceptionAdvanceResult(
                status="idle",
                progressed_units=0,
                next_wake_at=self._next_wake_at(),
                more_due=self._has_due_work(observed_at),
            )
        if str(row["target_embedding_version"]) != self._embedding.version:
            raise RuntimeError("embedding job target version does not match provider")
        search_text = str(row["search_text"])
        try:
            vectors = await asyncio.to_thread(self._embedding.embed, (search_text,))
            if len(vectors) != 1 or len(vectors[0]) != self._embedding.dimensions:
                raise ValueError("embedding_shape_invalid")
            vector = tuple(float(value) for value in vectors[0])
            if any(not math.isfinite(value) for value in vector):
                raise ValueError("embedding_value_invalid")
        except Exception as exc:
            ordinal = int(row["consecutive_failures"]) + 1
            delay = _FAILURE_BACKOFF_SECONDS[min(ordinal - 1, len(_FAILURE_BACKOFF_SECONDS) - 1)]
            failure_code = f"embedding:{type(exc).__name__}:{exc}"[:128]
            with self._database_write_lock, self._lock:
                self._connection.execute(
                    """
                    UPDATE external_signal_embedding_jobs
                    SET consecutive_failures = ?, next_attempt_at = ?,
                        last_failure_code = ?
                    WHERE signal_revision_ref = ?
                    """,
                    (
                        ordinal,
                        _iso_utc(observed_at + timedelta(seconds=delay)),
                        failure_code,
                        signal_revision_ref,
                    ),
                )
                self._update_embedding_health(observed_at=observed_at)
            return PerceptionAdvanceResult(
                status="retry_wait",
                progressed_units=1,
                next_wake_at=self._next_wake_at(),
                more_due=self._has_due_work(observed_at),
            )
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO external_signal_embeddings (
                        signal_revision_ref, embedding_version, dimensions,
                        embedding_json
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(signal_revision_ref) DO UPDATE SET
                        embedding_version = excluded.embedding_version,
                        dimensions = excluded.dimensions,
                        embedding_json = excluded.embedding_json
                    """,
                    (
                        signal_revision_ref,
                        self._embedding.version,
                        self._embedding.dimensions,
                        _canonical_json(vector),
                    ),
                )
                self._connection.execute(
                    """
                    DELETE FROM external_signal_embedding_jobs
                    WHERE signal_revision_ref = ?
                    """,
                    (signal_revision_ref,),
                )
                self._update_embedding_health(observed_at=observed_at)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return PerceptionAdvanceResult(
            status="progressed",
            progressed_units=1,
            next_wake_at=self._next_wake_at(),
            more_due=self._has_due_work(observed_at),
        )

    def _update_embedding_health(self, *, observed_at: datetime) -> None:
        failure = self._connection.execute(
            """
            SELECT last_failure_code
            FROM external_signal_embedding_jobs
            WHERE last_failure_code IS NOT NULL
            ORDER BY next_attempt_at DESC
            LIMIT 1
            """
        ).fetchone()
        self._connection.execute(
            """
            UPDATE external_perception_index_health
            SET last_embedding_failure_code = ?, last_embedding_attempt_at = ?
            WHERE singleton = 1
            """,
            (
                str(failure["last_failure_code"]) if failure is not None else None,
                _iso_utc(observed_at),
            ),
        )

    def _assign_cluster(
        self,
        *,
        signal_id: str,
        item: ExternalSignalSourceItem,
        correction_of_ref: str | None,
        observed_at: datetime,
    ) -> None:
        existing = self._connection.execute(
            """
            SELECT cluster_ref, basis_key
            FROM external_signal_cluster_membership
            WHERE signal_id = ?
            """,
            (signal_id,),
        ).fetchone()
        if existing is not None:
            return
        inherited = None
        if correction_of_ref is not None:
            inherited = self._connection.execute(
                """
                SELECT membership.cluster_ref, membership.basis_key
                FROM external_signal_revisions AS revision
                JOIN external_signal_cluster_membership AS membership
                  ON membership.signal_id = revision.signal_id
                WHERE revision.signal_revision_ref = ?
                """,
                (correction_of_ref,),
            ).fetchone()
        basis_key = _cluster_basis_key(item)
        matching = (
            inherited
            or self._connection.execute(
                """
                SELECT membership.cluster_ref, membership.basis_key
                FROM external_signal_cluster_membership AS membership
                JOIN external_signal_observation_state AS state
                  ON state.signal_id = membership.signal_id
                WHERE membership.basis_key = ?
                  AND state.effective_expires_at > ?
                ORDER BY membership.assigned_at, membership.signal_id
                LIMIT 1
                """,
                (basis_key, _iso_utc(observed_at)),
            ).fetchone()
        )
        cluster_ref = (
            str(matching["cluster_ref"])
            if matching is not None
            else _stable_ref("external-cluster", {"basis_key": basis_key})
        )
        stored_basis = str(matching["basis_key"]) if matching is not None else basis_key
        self._connection.execute(
            """
            INSERT INTO external_signal_cluster_membership (
                signal_id, cluster_ref, basis_key, assigned_at
            ) VALUES (?, ?, ?, ?)
            """,
            (signal_id, cluster_ref, stored_basis, _iso_utc(observed_at)),
        )

    def _record_failure(
        self,
        *,
        source_id: str,
        observed_at: datetime,
        failure_code: str,
        retry_after_seconds: int | None,
    ) -> None:
        with self._database_write_lock, self._lock:
            row = self._connection.execute(
                """
                SELECT consecutive_failures FROM external_perception_source_state
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
            ordinal = int(row["consecutive_failures"]) + 1 if row is not None else 1
            delay = (
                retry_after_seconds
                or _FAILURE_BACKOFF_SECONDS[min(ordinal - 1, len(_FAILURE_BACKOFF_SECONDS) - 1)]
            )
            self._connection.execute(
                """
                UPDATE external_perception_source_state
                SET last_attempt_at = ?, next_refresh_at = ?,
                    consecutive_failures = ?, last_failure_code = ?,
                    last_result = 'technical_failure'
                WHERE source_id = ?
                """,
                (
                    _iso_utc(observed_at),
                    _iso_utc(observed_at + timedelta(seconds=delay)),
                    ordinal,
                    failure_code,
                    source_id,
                ),
            )

    def _next_wake_at(self) -> datetime | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT MIN(next_wake_at) AS next_wake_at
                FROM (
                    SELECT next_refresh_at AS next_wake_at
                    FROM external_perception_source_state
                    WHERE next_refresh_at IS NOT NULL AND is_configured = 1
                    UNION ALL
                    SELECT next_attempt_at AS next_wake_at
                    FROM external_signal_embedding_jobs
                )
                """
            ).fetchone()
        value = row["next_wake_at"] if row is not None else None
        acquisition_wake = _parse_datetime(str(value)) if value is not None else None
        attention_wake = (
            self._attention_coordinator.next_wake_at()
            if self._attention_coordinator is not None
            else None
        )
        return min(
            (item for item in (acquisition_wake, attention_wake) if item is not None),
            default=None,
        )

    def health_snapshot(self) -> PerceptionHealthSnapshot:
        as_of = self._wall_clock()
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("World Perception health clock must be timezone-aware")
        with self._lock:
            source_rows = self._connection.execute(
                """
                SELECT * FROM external_perception_source_state
                WHERE is_configured = 1 ORDER BY source_id
                """
            ).fetchall()
            signal_revision_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM external_signal_revisions"
                ).fetchone()[0]
            )
            signal_revisions_last_24h = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM external_signal_revisions
                    WHERE observed_at >= ?
                    """,
                    (_iso_utc(as_of - timedelta(hours=24)),),
                ).fetchone()[0]
            )
            active_signal_count = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM external_signal_observation_state
                    WHERE effective_expires_at > ?
                    """,
                    (_iso_utc(as_of),),
                ).fetchone()[0]
            )
            expired_signal_count = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM external_signal_observation_state
                    WHERE effective_expires_at <= ?
                    """,
                    (_iso_utc(as_of),),
                ).fetchone()[0]
            )
            superseded_revision_count = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM external_signal_revisions AS previous
                    WHERE EXISTS (
                        SELECT 1
                        FROM external_signal_revisions AS newer
                        WHERE newer.supersedes_ref = previous.signal_revision_ref
                    )
                    """
                ).fetchone()[0]
            )
            correction_edge_count = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM external_signal_revisions
                    WHERE correction_of_ref IS NOT NULL
                    """
                ).fetchone()[0]
            )
            cluster_count = int(
                self._connection.execute(
                    """
                    SELECT COUNT(DISTINCT cluster_ref)
                    FROM external_signal_cluster_membership AS membership
                    JOIN external_signal_observation_state AS state
                      ON state.signal_id = membership.signal_id
                    WHERE state.effective_expires_at > ?
                    """,
                    (_iso_utc(as_of),),
                ).fetchone()[0]
            )
            rejected_item_count = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(SUM(rejected_item_count), 0)
                    FROM external_perception_source_state
                    WHERE is_configured = 1
                    """
                ).fetchone()[0]
            )
            search_indexed_revision_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM external_signal_search_documents"
                ).fetchone()[0]
            )
            embedding_indexed_revision_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM external_signal_embeddings"
                ).fetchone()[0]
            )
            embedding_pending_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM external_signal_embedding_jobs"
                ).fetchone()[0]
            )
            index_health = self._connection.execute(
                """
                SELECT last_embedding_failure_code
                FROM external_perception_index_health WHERE singleton = 1
                """
            ).fetchone()
            raw_row = self._connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(length(content)), 0)
                FROM external_perception_raw_evidence
                """
            ).fetchone()
            growth_baseline = self._connection.execute(
                """
                SELECT sidecar_bytes FROM external_perception_storage_samples
                WHERE recorded_at >= ?
                ORDER BY recorded_at LIMIT 1
                """,
                (_iso_utc(as_of - timedelta(hours=24)),),
            ).fetchone()
        source_states = tuple(self._source_health(row=row, as_of=as_of) for row in source_rows)
        shadow_attention = (
            self._attention_coordinator.health_snapshot(as_of=as_of)
            if self._attention_coordinator is not None
            else None
        )
        warning_reasons = tuple(
            f"source:{item.source_id}:{item.state}"
            for item in source_states
            if item.state in {"retry_wait", "stale", "malformed"}
        )
        state = "healthy"
        if warning_reasons:
            state = (
                "degraded"
                if any(item.state == "retry_wait" for item in source_states)
                else "warning"
            )
        embedding_failure_code = (
            str(index_health["last_embedding_failure_code"])
            if index_health is not None and index_health["last_embedding_failure_code"] is not None
            else None
        )
        if embedding_failure_code is not None:
            warning_reasons += (f"index:{embedding_failure_code}",)
            state = "degraded"
        if shadow_attention is not None and shadow_attention.state in {
            "retry_wait",
            "degraded",
        }:
            warning_reasons += (f"attention:{shadow_attention.last_failure_code or 'retry_wait'}",)
            state = "degraded"
        current_sidecar_bytes = _file_size(self._path) + _file_size(Path(f"{self._path}-wal"))
        baseline_bytes = int(growth_baseline["sidecar_bytes"]) if growth_baseline else 0
        return PerceptionHealthSnapshot(
            state=state,
            as_of=as_of,
            source_states=source_states,
            signal_revision_count=signal_revision_count,
            superseded_revision_count=superseded_revision_count,
            correction_edge_count=correction_edge_count,
            cluster_count=cluster_count,
            rejected_item_count=rejected_item_count,
            search_indexed_revision_count=search_indexed_revision_count,
            fts_state="healthy",
            embedding_state=(
                "not_configured"
                if self._embedding is None
                else ("degraded" if embedding_failure_code is not None else "healthy")
            ),
            embedding_version=(self._embedding.version if self._embedding is not None else None),
            embedding_indexed_revision_count=embedding_indexed_revision_count,
            embedding_pending_count=embedding_pending_count,
            last_embedding_failure_code=embedding_failure_code,
            signal_revisions_last_24h=signal_revisions_last_24h,
            sidecar_main_bytes=_file_size(self._path),
            sidecar_wal_bytes=_file_size(Path(f"{self._path}-wal")),
            sidecar_growth_24h_bytes=max(0, current_sidecar_bytes - baseline_bytes),
            active_signal_count=active_signal_count,
            expired_signal_count=expired_signal_count,
            raw_evidence_count=int(raw_row[0]),
            raw_evidence_bytes=int(raw_row[1]),
            duplicate_suppressed_count=sum(
                item.duplicate_suppressed_count for item in source_states
            ),
            **({"shadow_attention": shadow_attention} if shadow_attention is not None else {}),
            warning_reasons=warning_reasons,
        )

    def _source_health(
        self,
        *,
        row: sqlite3.Row,
        as_of: datetime,
    ) -> SourceHealthSnapshot:
        last_attempt_at = _optional_datetime(row["last_attempt_at"])
        last_success_at = _optional_datetime(row["last_success_at"])
        next_refresh_at = _optional_datetime(row["next_refresh_at"])
        failures = int(row["consecutive_failures"])
        if last_attempt_at is None:
            state = "never_polled"
        elif failures:
            state = "retry_wait"
        elif int(row["last_page_rejected_item_count"]):
            state = "malformed"
        elif next_refresh_at is not None and as_of > next_refresh_at + timedelta(hours=1):
            state = "stale"
        else:
            state = "healthy"
        return SourceHealthSnapshot(
            source_id=str(row["source_id"]),
            policy_revision=str(row["policy_revision"]),
            state=state,
            last_result=str(row["last_result"]),
            last_cursor=str(row["cursor_value"]) if row["cursor_value"] is not None else None,
            last_attempt_at=last_attempt_at,
            last_success_at=last_success_at,
            next_refresh_at=next_refresh_at,
            consecutive_failures=failures,
            last_failure_code=(
                str(row["last_failure_code"]) if row["last_failure_code"] is not None else None
            ),
            accepted_revision_count=int(row["accepted_revision_count"]),
            duplicate_suppressed_count=int(row["duplicate_suppressed_count"]),
            rejected_item_count=int(row["rejected_item_count"]),
            last_page_rejected_item_count=int(row["last_page_rejected_item_count"]),
        )

    async def aclose(self) -> None:
        async with self._advance_lock:
            with self._lock:
                self._connection.close()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _optional_datetime(value: object) -> datetime | None:
    return _parse_datetime(str(value)) if value is not None else None


def _cluster_basis_key(item: ExternalSignalSourceItem) -> str:
    if item.canonical_url:
        parsed = urlsplit(item.canonical_url)
        normalized_url = urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
        )
        return f"canonical-url:{normalized_url}"
    normalized_headline = " ".join(unicodedata.normalize("NFKC", item.headline).casefold().split())
    publication_day = item.published_at.astimezone(UTC).date()
    return _stable_ref(
        "headline-publication-day",
        {"headline": normalized_headline, "publication_day": publication_day.isoformat()},
    )


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external perception timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _source_page_result(
    *,
    page: ExternalSignalSourcePage,
    accepted: int,
    duplicates: int,
    rejected: int,
) -> str:
    if page.not_modified:
        return "not_modified"
    if accepted:
        return "new_revisions_with_rejections" if rejected else "new_revisions"
    if rejected:
        return "malformed"
    if duplicates:
        return "duplicates_only"
    return "no_new_signal"


__all__ = ["SQLiteWorldPerceptionHub"]
