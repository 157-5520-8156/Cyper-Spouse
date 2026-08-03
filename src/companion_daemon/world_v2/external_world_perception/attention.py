"""Durable shadow character-attention coordination for external signals.

This module owns only disposable sidecar state.  It has no World ledger writer,
Life Ecology dependency, or message transport and therefore cannot turn a
shadow selection into a character fact or visible behaviour.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
from threading import RLock
from typing import Any, Literal

from pydantic import ValidationError

from .contracts import (
    CharacterAttentionContext,
    CharacterAttentionRequest,
    CharacterAttentionResult,
    CharacterAttentionTechnicalFailure,
    CorrectionEdge,
    ExternalSignalSourceItem,
    LicensedEvidenceView,
    PerceptionAdvanceResult,
    PerceptionDossier,
    PerceptionWindow,
    ShadowAttentionHealthSnapshot,
    ShadowAttentionRuntime,
    SourceDisagreement,
)


_FAILURE_BACKOFF_SECONDS = (600, 1_800, 7_200)
_MAX_RECORDED_RESULT_BYTES = 65_536


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_ref(prefix: str, value: object) -> str:
    return f"{prefix}:" + _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("shadow attention timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


class SQLiteShadowAttentionCoordinator:
    """Internal durable coordinator observed only through the Hub seam."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        lock: RLock,
        database_write_lock: RLock,
        runtime: ShadowAttentionRuntime,
        exposable_source_ids: tuple[str, ...],
        wall_clock: Any,
    ) -> None:
        self._connection = connection
        self._lock = lock
        self._database_write_lock = database_write_lock
        self._runtime = runtime
        self._exposable_source_ids = exposable_source_ids
        self._wall_clock = wall_clock
        with self._database_write_lock, self._lock:
            self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_perception_attention_opportunities (
                opportunity_id TEXT PRIMARY KEY,
                attention_policy_revision TEXT NOT NULL,
                deployment_mode_revision TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                ready_at TEXT NOT NULL,
                status TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL,
                next_attempt_at TEXT,
                last_failure_code TEXT,
                terminal_at TEXT,
                terminal_result TEXT
            );

            CREATE TABLE IF NOT EXISTS external_perception_attention_policies (
                attention_policy_revision TEXT PRIMARY KEY,
                policy_json TEXT NOT NULL,
                registered_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_perception_opportunity_membership (
                opportunity_id TEXT NOT NULL,
                signal_revision_ref TEXT NOT NULL,
                PRIMARY KEY (opportunity_id, signal_revision_ref)
            );

            CREATE TABLE IF NOT EXISTS external_perception_attention_windows (
                window_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL UNIQUE,
                attention_attempt_id TEXT NOT NULL UNIQUE,
                generated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                window_json TEXT NOT NULL,
                context_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_perception_attention_attempts (
                attention_attempt_id TEXT PRIMARY KEY,
                window_id TEXT NOT NULL,
                state TEXT NOT NULL,
                retry_ordinal INTEGER NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                next_attempt_at TEXT,
                model_id TEXT NOT NULL,
                model_call_count INTEGER NOT NULL,
                invalid_result_count INTEGER NOT NULL,
                technical_failure_count INTEGER NOT NULL,
                last_failure_code TEXT,
                final_result_json TEXT,
                final_result_hash TEXT,
                created_at TEXT NOT NULL,
                last_attempt_at TEXT,
                terminal_at TEXT
            );

            CREATE TABLE IF NOT EXISTS external_perception_attention_model_audits (
                attention_attempt_id TEXT NOT NULL,
                retry_ordinal INTEGER NOT NULL,
                selection_ordinal INTEGER NOT NULL,
                request_hash TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                result_json TEXT,
                validation_failure_codes_json TEXT NOT NULL,
                technical_failure_code TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (attention_attempt_id, retry_ordinal, selection_ordinal)
            );

            CREATE TABLE IF NOT EXISTS external_perception_attention_exposures (
                signal_revision_ref TEXT NOT NULL,
                attention_policy_revision TEXT NOT NULL,
                deployment_mode_revision TEXT NOT NULL,
                attention_attempt_id TEXT NOT NULL,
                result TEXT NOT NULL,
                exposed_at TEXT NOT NULL,
                PRIMARY KEY (
                    signal_revision_ref,
                    attention_policy_revision,
                    deployment_mode_revision
                )
            );

            CREATE INDEX IF NOT EXISTS external_perception_opportunities_due
            ON external_perception_attention_opportunities (status, ready_at, next_attempt_at);

            CREATE INDEX IF NOT EXISTS external_perception_attention_attempts_due
            ON external_perception_attention_attempts (state, next_attempt_at, lease_expires_at);

            CREATE INDEX IF NOT EXISTS external_perception_attention_audits_recent
            ON external_perception_attention_model_audits (completed_at);
            """
        )
        policy_json = _canonical_json(
            {
                "attention_policy_revision": self._runtime.attention_policy_revision,
                "deployment_mode_revision": self._runtime.deployment_mode_revision,
                "model_id": self._runtime.model.model_id,
                "merge_wait_seconds": self._runtime.merge_wait_seconds,
                "window_ttl_seconds": self._runtime.window_ttl_seconds,
                "lease_seconds": self._runtime.lease_seconds,
                "model_timeout_seconds": self._runtime.model_timeout_seconds,
                "max_candidate_dossiers": self._runtime.max_candidate_dossiers,
                "attempt_retention_seconds": self._runtime.attempt_retention_seconds,
            }
        )
        existing = self._connection.execute(
            """
            SELECT policy_json FROM external_perception_attention_policies
            WHERE attention_policy_revision = ?
            """,
            (self._runtime.attention_policy_revision,),
        ).fetchone()
        if existing is not None and str(existing["policy_json"]) != policy_json:
            raise ValueError("shadow attention policy revision content changed")
        self._connection.execute(
            """
            INSERT OR IGNORE INTO external_perception_attention_policies (
                attention_policy_revision, policy_json, registered_at
            ) VALUES (?, ?, ?)
            """,
            (
                self._runtime.attention_policy_revision,
                policy_json,
                _iso_utc(self._wall_clock()),
            ),
        )

    async def advance_once(self, *, observed_at: datetime) -> PerceptionAdvanceResult | None:
        self._delete_expired_attempts(observed_at)
        existing = self._recoverable_attempt(observed_at)
        if existing is not None:
            return await self._advance_attempt(row=existing, observed_at=observed_at)

        opportunity = self._sync_pending_opportunity(observed_at)
        if opportunity is None:
            return None
        ready_at = _parse_datetime(opportunity["ready_at"])
        retry_at = (
            _parse_datetime(opportunity["next_attempt_at"])
            if opportunity["next_attempt_at"] is not None
            else ready_at
        )
        if observed_at < retry_at:
            return PerceptionAdvanceResult(
                status="window_wait" if opportunity["next_attempt_at"] is None else "retry_wait",
                progressed_units=0,
                next_wake_at=retry_at,
                more_due=False,
            )

        try:
            context = await self._runtime.context_port.freeze_attention_context(
                world_id=self._runtime.world_id,
                actor_ref=self._runtime.actor_ref,
                observed_at=observed_at,
            )
            self._validate_context(context=context, observed_at=observed_at)
        except Exception as exc:
            failure_code = self._technical_failure_code("context", exc)
            self._record_opportunity_failure(
                opportunity_id=str(opportunity["opportunity_id"]),
                observed_at=observed_at,
                failure_code=failure_code,
            )
            return PerceptionAdvanceResult(
                status="retry_wait",
                progressed_units=1,
                next_wake_at=self.next_wake_at(),
                more_due=False,
            )

        frozen = self._freeze_window(
            opportunity_id=str(opportunity["opportunity_id"]),
            context=context,
            observed_at=observed_at,
        )
        if frozen is None:
            self._terminal_opportunity_without_candidate(
                opportunity_id=str(opportunity["opportunity_id"]),
                observed_at=observed_at,
            )
            return PerceptionAdvanceResult(
                status="progressed",
                progressed_units=1,
                next_wake_at=self.next_wake_at(),
                more_due=self.has_due_work(observed_at),
            )
        attempt = self._attempt_row(frozen.attention_attempt_id)
        if attempt is None:
            raise RuntimeError("shadow attention window was frozen without its attempt")
        return await self._advance_attempt(row=attempt, observed_at=observed_at)

    def _delete_expired_attempts(self, observed_at: datetime) -> None:
        cutoff = observed_at - timedelta(seconds=self._runtime.attempt_retention_seconds)
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                expired_attempts = self._connection.execute(
                    """
                    SELECT attention_attempt_id, window_id
                    FROM external_perception_attention_attempts
                    WHERE terminal_at IS NOT NULL AND terminal_at <= ?
                    """,
                    (_iso_utc(cutoff),),
                ).fetchall()
                for row in expired_attempts:
                    self._connection.execute(
                        "DELETE FROM external_perception_attention_model_audits WHERE attention_attempt_id = ?",
                        (row["attention_attempt_id"],),
                    )
                    self._connection.execute(
                        "DELETE FROM external_perception_attention_attempts WHERE attention_attempt_id = ?",
                        (row["attention_attempt_id"],),
                    )
                    self._connection.execute(
                        "DELETE FROM external_perception_attention_windows WHERE window_id = ?",
                        (row["window_id"],),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _recoverable_attempt(self, observed_at: datetime) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                """
                SELECT * FROM external_perception_attention_attempts
                WHERE (state = 'retry_wait' AND next_attempt_at <= ?)
                   OR state = 'claimed'
                ORDER BY COALESCE(next_attempt_at, lease_expires_at), attention_attempt_id
                LIMIT 1
                """,
                (_iso_utc(observed_at),),
            ).fetchone()

    def _sync_pending_opportunity(self, observed_at: datetime) -> sqlite3.Row | None:
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                pending = self._connection.execute(
                    """
                    SELECT * FROM external_perception_attention_opportunities
                    WHERE attention_policy_revision = ?
                      AND deployment_mode_revision = ?
                      AND status = 'waiting'
                    ORDER BY opened_at LIMIT 1
                    """,
                    (
                        self._runtime.attention_policy_revision,
                        self._runtime.deployment_mode_revision,
                    ),
                ).fetchone()
                eligible = self._eligible_signal_rows(observed_at=observed_at)
                if pending is None and eligible:
                    first_ref = str(eligible[0]["signal_revision_ref"])
                    opportunity_id = _stable_ref(
                        "perception-opportunity",
                        {
                            "world_id": self._runtime.world_id,
                            "first_signal_revision_ref": first_ref,
                            "attention_policy_revision": self._runtime.attention_policy_revision,
                            "deployment_mode_revision": self._runtime.deployment_mode_revision,
                        },
                    )
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO external_perception_attention_opportunities (
                            opportunity_id, attention_policy_revision,
                            deployment_mode_revision, opened_at, ready_at, status,
                            consecutive_failures
                        ) VALUES (?, ?, ?, ?, ?, 'waiting', 0)
                        """,
                        (
                            opportunity_id,
                            self._runtime.attention_policy_revision,
                            self._runtime.deployment_mode_revision,
                            _iso_utc(observed_at),
                            _iso_utc(
                                observed_at + timedelta(seconds=self._runtime.merge_wait_seconds)
                            ),
                        ),
                    )
                    pending = self._connection.execute(
                        "SELECT * FROM external_perception_attention_opportunities WHERE opportunity_id = ?",
                        (opportunity_id,),
                    ).fetchone()
                if pending is not None:
                    for row in eligible:
                        self._connection.execute(
                            """
                            INSERT OR IGNORE INTO external_perception_opportunity_membership (
                                opportunity_id, signal_revision_ref
                            ) VALUES (?, ?)
                            """,
                            (pending["opportunity_id"], row["signal_revision_ref"]),
                        )
                self._connection.execute("COMMIT")
                return pending
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _eligible_signal_rows(self, *, observed_at: datetime) -> tuple[sqlite3.Row, ...]:
        if not self._exposable_source_ids:
            return ()
        placeholders = ",".join("?" for _ in self._exposable_source_ids)
        rows = self._connection.execute(
            f"""
            SELECT revision.signal_revision_ref, revision.source_id, revision.observed_at
            FROM external_signal_observation_state AS state
            JOIN external_signal_revisions AS revision
              ON revision.signal_revision_ref = state.latest_signal_revision_ref
            WHERE state.effective_expires_at > ?
              AND revision.source_id IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM external_perception_attention_exposures AS exposure
                  WHERE exposure.signal_revision_ref = revision.signal_revision_ref
                    AND exposure.attention_policy_revision = ?
                    AND exposure.deployment_mode_revision = ?
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM external_perception_opportunity_membership AS pending_membership
                  JOIN external_perception_attention_opportunities AS pending_opportunity
                    ON pending_opportunity.opportunity_id = pending_membership.opportunity_id
                  WHERE pending_membership.signal_revision_ref = revision.signal_revision_ref
                    AND pending_opportunity.status IN ('waiting', 'attempting')
              )
            ORDER BY revision.observed_at, revision.signal_revision_ref
            """,
            (
                _iso_utc(observed_at),
                *self._exposable_source_ids,
                self._runtime.attention_policy_revision,
                self._runtime.deployment_mode_revision,
            ),
        ).fetchall()
        return tuple(rows)

    @staticmethod
    def _validate_context(*, context: CharacterAttentionContext, observed_at: datetime) -> None:
        # World/actor equality is checked by the caller against its runtime below.
        if any(channel.valid_until <= observed_at for channel in context.available_channels):
            raise ValueError("attention_context_contains_expired_channel")

    def _freeze_window(
        self,
        *,
        opportunity_id: str,
        context: CharacterAttentionContext,
        observed_at: datetime,
    ) -> PerceptionWindow | None:
        if context.world_id != self._runtime.world_id:
            raise ValueError("attention_context_world_mismatch")
        if context.actor_ref != self._runtime.actor_ref:
            raise ValueError("attention_context_actor_mismatch")
        dossiers = self._compile_dossiers(
            opportunity_id=opportunity_id,
            context=context,
            observed_at=observed_at,
        )
        if not dossiers:
            return None
        candidate_snapshot = [item.model_dump(mode="json") for item in dossiers]
        candidate_set_hash = _sha256_bytes(_canonical_json(candidate_snapshot).encode("utf-8"))
        attempt_id = _stable_ref(
            "attention-attempt",
            {
                "world_id": self._runtime.world_id,
                "opportunity_id": opportunity_id,
                "candidate_snapshot_hash": candidate_set_hash,
                "exact_world_cursor": context.pinned_world_cursor,
                "attention_policy_revision": self._runtime.attention_policy_revision,
                "deployment_mode_revision": self._runtime.deployment_mode_revision,
            },
        )
        window_id = _stable_ref("perception-window", {"attention_attempt_id": attempt_id})
        window = PerceptionWindow(
            window_id=window_id,
            attention_attempt_id=attempt_id,
            opportunity_id=opportunity_id,
            world_id=self._runtime.world_id,
            actor_ref=self._runtime.actor_ref,
            pinned_world_cursor=context.pinned_world_cursor,
            attention_policy_revision=self._runtime.attention_policy_revision,
            deployment_mode_revision=self._runtime.deployment_mode_revision,
            generated_at=observed_at,
            expires_at=observed_at + timedelta(seconds=self._runtime.window_ttl_seconds),
            candidates=dossiers,
            candidate_set_hash=candidate_set_hash,
            exposure_draw_ref=_stable_ref(
                "perception-exposure-draw",
                {
                    "opportunity_id": opportunity_id,
                    "candidate_set_hash": candidate_set_hash,
                },
            ),
        )
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO external_perception_attention_windows (
                        window_id, opportunity_id, attention_attempt_id, generated_at,
                        expires_at, window_json, context_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        window.window_id,
                        opportunity_id,
                        attempt_id,
                        _iso_utc(observed_at),
                        _iso_utc(window.expires_at),
                        window.model_dump_json(),
                        context.model_dump_json(),
                    ),
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO external_perception_attention_attempts (
                        attention_attempt_id, window_id, state, retry_ordinal,
                        model_id, model_call_count, invalid_result_count,
                        technical_failure_count, created_at
                    ) VALUES (?, ?, 'open', 0, ?, 0, 0, 0, ?)
                    """,
                    (attempt_id, window_id, self._runtime.model.model_id, _iso_utc(observed_at)),
                )
                self._connection.execute(
                    """
                    UPDATE external_perception_attention_opportunities
                    SET status = 'attempting', consecutive_failures = 0,
                        next_attempt_at = NULL, last_failure_code = NULL
                    WHERE opportunity_id = ? AND status = 'waiting'
                    """,
                    (opportunity_id,),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return window

    def _compile_dossiers(
        self,
        *,
        opportunity_id: str,
        context: CharacterAttentionContext,
        observed_at: datetime,
    ) -> tuple[PerceptionDossier, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT revision.*, membership.cluster_ref
                FROM external_perception_opportunity_membership AS opportunity
                JOIN external_signal_revisions AS revision
                  ON revision.signal_revision_ref = opportunity.signal_revision_ref
                JOIN external_signal_observation_state AS state
                  ON state.latest_signal_revision_ref = revision.signal_revision_ref
                JOIN external_signal_cluster_membership AS membership
                  ON membership.signal_id = revision.signal_id
                WHERE opportunity.opportunity_id = ?
                  AND state.effective_expires_at > ?
                """,
                (opportunity_id, _iso_utc(observed_at)),
            ).fetchall()
        by_cluster: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_cluster.setdefault(str(row["cluster_ref"]), []).append(row)
        ranked_clusters = sorted(
            by_cluster,
            key=lambda cluster_ref: _sha256_bytes(
                f"{opportunity_id}\0{cluster_ref}".encode("utf-8")
            ),
        )[: self._runtime.max_candidate_dossiers]
        dossiers: list[PerceptionDossier] = []
        for cluster_ref in ranked_clusters:
            material_rows: list[tuple[sqlite3.Row, ExternalSignalSourceItem]] = []
            for row in by_cluster[cluster_ref]:
                item = ExternalSignalSourceItem.model_validate_json(str(row["normalized_json"]))
                if any(
                    str(row["source_id"]) in channel.accessible_source_ids
                    for channel in context.available_channels
                ):
                    material_rows.append((row, item))
            if not material_rows:
                continue
            exact_refs = tuple(str(row["signal_revision_ref"]) for row, _ in material_rows)
            channels = tuple(
                channel
                for channel in context.available_channels
                if any(
                    str(row["source_id"]) in channel.accessible_source_ids
                    for row, _ in material_rows
                )
            )
            visible = tuple(
                LicensedEvidenceView(
                    signal_revision_ref=str(row["signal_revision_ref"]),
                    source_id=str(row["source_id"]),
                    upstream_publisher_ref=item.upstream_publisher_ref,
                    signal_kind=item.signal_kind,
                    headline=item.headline,
                    licensed_summary=item.licensed_summary,
                    canonical_url=item.canonical_url,
                    published_at=item.published_at,
                    observed_at=_parse_datetime(row["observed_at"]),
                    updated_at=item.updated_at,
                    expires_at=_parse_datetime(row["effective_expires_at"]),
                    source_provided_certainty=item.source_provided_certainty,
                    place_scope=item.place_scope,
                )
                for row, item in material_rows
            )
            corrections = tuple(
                CorrectionEdge(
                    correction_revision_ref=str(row["signal_revision_ref"]),
                    corrected_revision_ref=str(row["correction_of_ref"]),
                )
                for row, _ in material_rows
                if row["correction_of_ref"] is not None
            )
            disagreements = self._disagreements(visible)
            digest_value = {
                "exact_signal_revisions": exact_refs,
                "corrections": [item.model_dump(mode="json") for item in corrections],
                "source_disagreements": [item.model_dump(mode="json") for item in disagreements],
                "accessible_channel_refs": [item.channel_ref for item in channels],
                "material": [item.model_dump(mode="json") for item in visible],
            }
            dossiers.append(
                PerceptionDossier(
                    candidate_ref=_stable_ref(
                        "perception-candidate",
                        {"opportunity_id": opportunity_id, "cluster_ref": cluster_ref},
                    ),
                    exact_signal_revisions=exact_refs,
                    corrections=corrections,
                    source_disagreements=disagreements,
                    accessible_channels=channels,
                    model_visible_material=visible,
                    evidence_digest=_sha256_bytes(_canonical_json(digest_value).encode("utf-8")),
                )
            )
        return tuple(dossiers)

    @staticmethod
    def _disagreements(
        material: tuple[LicensedEvidenceView, ...],
    ) -> tuple[SourceDisagreement, ...]:
        if len(material) < 2:
            return ()
        differing: list[Literal["headline", "licensed_summary", "certainty"]] = []
        if len({item.headline for item in material}) > 1:
            differing.append("headline")
        if len({item.licensed_summary for item in material}) > 1:
            differing.append("licensed_summary")
        if len({item.source_provided_certainty for item in material}) > 1:
            differing.append("certainty")
        if not differing:
            return ()
        return (
            SourceDisagreement(
                signal_revision_refs=tuple(item.signal_revision_ref for item in material),
                differing_fields=tuple(differing),
            ),
        )

    def _terminal_opportunity_without_candidate(
        self, *, opportunity_id: str, observed_at: datetime
    ) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute(
                """
                UPDATE external_perception_attention_opportunities
                SET status = 'terminal', terminal_at = ?, terminal_result = 'no_candidate'
                WHERE opportunity_id = ?
                """,
                (_iso_utc(observed_at), opportunity_id),
            )
            members = self._connection.execute(
                """
                SELECT signal_revision_ref
                FROM external_perception_opportunity_membership WHERE opportunity_id = ?
                """,
                (opportunity_id,),
            ).fetchall()
            for row in members:
                self._record_exposure(
                    signal_revision_ref=str(row["signal_revision_ref"]),
                    attention_attempt_id=f"filtered:{opportunity_id}",
                    result="no_candidate",
                    observed_at=observed_at,
                )

    def _record_opportunity_failure(
        self, *, opportunity_id: str, observed_at: datetime, failure_code: str
    ) -> None:
        with self._database_write_lock, self._lock:
            row = self._connection.execute(
                """
                SELECT consecutive_failures
                FROM external_perception_attention_opportunities WHERE opportunity_id = ?
                """,
                (opportunity_id,),
            ).fetchone()
            ordinal = int(row["consecutive_failures"]) + 1 if row is not None else 1
            delay = _FAILURE_BACKOFF_SECONDS[min(ordinal - 1, 2)]
            self._connection.execute(
                """
                UPDATE external_perception_attention_opportunities
                SET consecutive_failures = ?, next_attempt_at = ?, last_failure_code = ?
                WHERE opportunity_id = ?
                """,
                (
                    ordinal,
                    _iso_utc(observed_at + timedelta(seconds=delay)),
                    failure_code,
                    opportunity_id,
                ),
            )

    def _attempt_row(self, attention_attempt_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM external_perception_attention_attempts WHERE attention_attempt_id = ?",
                (attention_attempt_id,),
            ).fetchone()

    async def _advance_attempt(
        self, *, row: sqlite3.Row, observed_at: datetime
    ) -> PerceptionAdvanceResult:
        state = str(row["state"])
        if state == "claimed" and row["lease_expires_at"] is not None:
            lease_expires_at = _parse_datetime(row["lease_expires_at"])
            if lease_expires_at > observed_at:
                return PerceptionAdvanceResult(
                    status="joined_existing",
                    progressed_units=0,
                    next_wake_at=lease_expires_at,
                    more_due=False,
                )
        claimed = self._claim_attempt(
            attention_attempt_id=str(row["attention_attempt_id"]),
            observed_at=observed_at,
        )
        if claimed is None:
            return PerceptionAdvanceResult(
                status="joined_existing",
                progressed_units=0,
                next_wake_at=self.next_wake_at(),
                more_due=False,
            )
        window, context = self._load_frozen_request(str(claimed["window_id"]))
        if observed_at >= window.expires_at:
            self._terminal_attempt(
                attention_attempt_id=window.attention_attempt_id,
                window=window,
                observed_at=observed_at,
                result="expired",
                final_result=None,
            )
            return PerceptionAdvanceResult(
                status="progressed",
                progressed_units=1,
                next_wake_at=self.next_wake_at(),
                more_due=self.has_due_work(observed_at),
            )
        return await self._call_model(
            attempt_row=claimed,
            window=window,
            context=context,
            observed_at=observed_at,
        )

    def _claim_attempt(
        self, *, attention_attempt_id: str, observed_at: datetime
    ) -> sqlite3.Row | None:
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM external_perception_attention_attempts WHERE attention_attempt_id = ?",
                    (attention_attempt_id,),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                state = str(row["state"])
                retry_ordinal = int(row["retry_ordinal"])
                if state == "claimed":
                    lease = _parse_datetime(row["lease_expires_at"])
                    if lease > observed_at:
                        self._connection.execute("COMMIT")
                        return None
                    retry_ordinal += 1
                elif state == "retry_wait":
                    if _parse_datetime(row["next_attempt_at"]) > observed_at:
                        self._connection.execute("COMMIT")
                        return None
                    retry_ordinal += 1
                elif state != "open":
                    self._connection.execute("COMMIT")
                    return None
                self._connection.execute(
                    """
                    UPDATE external_perception_attention_attempts
                    SET state = 'claimed', retry_ordinal = ?, lease_owner = ?,
                        lease_expires_at = ?, next_attempt_at = NULL,
                        last_attempt_at = ?
                    WHERE attention_attempt_id = ?
                    """,
                    (
                        retry_ordinal,
                        self._runtime.worker_id,
                        _iso_utc(observed_at + timedelta(seconds=self._runtime.lease_seconds)),
                        _iso_utc(observed_at),
                        attention_attempt_id,
                    ),
                )
                claimed = self._connection.execute(
                    "SELECT * FROM external_perception_attention_attempts WHERE attention_attempt_id = ?",
                    (attention_attempt_id,),
                ).fetchone()
                self._connection.execute("COMMIT")
                return claimed
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _load_frozen_request(
        self, window_id: str
    ) -> tuple[PerceptionWindow, CharacterAttentionContext]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT window_json, context_json
                FROM external_perception_attention_windows WHERE window_id = ?
                """,
                (window_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("shadow attention attempt lost its frozen window")
        return (
            PerceptionWindow.model_validate_json(str(row["window_json"])),
            CharacterAttentionContext.model_validate_json(str(row["context_json"])),
        )

    async def _call_model(
        self,
        *,
        attempt_row: sqlite3.Row,
        window: PerceptionWindow,
        context: CharacterAttentionContext,
        observed_at: datetime,
    ) -> PerceptionAdvanceResult:
        retry_ordinal = int(attempt_row["retry_ordinal"])
        failure_codes: tuple[str, ...] = ()
        rejected_json: str | None = None
        for selection_ordinal in (0, 1):
            request = CharacterAttentionRequest(
                attention_attempt_id=window.attention_attempt_id,
                retry_ordinal=retry_ordinal,
                selection_ordinal=selection_ordinal,
                window=window,
                current_context=context,
                validation_failure_codes=failure_codes,
                rejected_result_json=rejected_json,
            )
            started_at = self._wall_clock()
            raw_result: object | None = None
            self._record_model_call(attention_attempt_id=window.attention_attempt_id)
            try:
                raw_result = await asyncio.wait_for(
                    self._runtime.model.consider_attention(request),
                    timeout=self._runtime.model_timeout_seconds,
                )
                result_json, result_hash = self._serialize_raw_result(raw_result)
                result = CharacterAttentionResult.model_validate_json(result_json)
                semantic_failures = self._validate_result(
                    result=result,
                    window=window,
                    context=context,
                )
            except CharacterAttentionTechnicalFailure as exc:
                completed_at = self._wall_clock()
                self._record_model_failure_audit(
                    request=request,
                    failure_code=f"model:{exc.failure_code}"[:128],
                    started_at=started_at,
                    completed_at=completed_at,
                )
                return self._record_attempt_failure_result(
                    attempt_id=window.attention_attempt_id,
                    observed_at=completed_at,
                    failure_code=f"model:{exc.failure_code}"[:128],
                )
            except TimeoutError:
                completed_at = self._wall_clock()
                self._record_model_failure_audit(
                    request=request,
                    failure_code="model:timeout",
                    started_at=started_at,
                    completed_at=completed_at,
                )
                return self._record_attempt_failure_result(
                    attempt_id=window.attention_attempt_id,
                    observed_at=completed_at,
                    failure_code="model:timeout",
                )
            except Exception as exc:
                if isinstance(exc, (ValidationError, ValueError, TypeError)):
                    result_json, result_hash = self._safe_invalid_result(raw_result)
                    semantic_failures = (self._result_shape_failure(exc),)
                    result = None
                else:
                    completed_at = self._wall_clock()
                    failure_code = self._technical_failure_code("model", exc)
                    self._record_model_failure_audit(
                        request=request,
                        failure_code=failure_code,
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                    return self._record_attempt_failure_result(
                        attempt_id=window.attention_attempt_id,
                        observed_at=completed_at,
                        failure_code=failure_code,
                    )
            completed_at = self._wall_clock()
            self._record_model_audit(
                request=request,
                result_json=result_json,
                result_hash=result_hash,
                validation_failure_codes=semantic_failures,
                started_at=started_at,
                completed_at=completed_at,
            )
            if not semantic_failures and result is not None:
                terminal = "shadow_selected" if result.selections else "attention_no_selection"
                terminal_recorded = self._terminal_attempt(
                    attention_attempt_id=window.attention_attempt_id,
                    window=window,
                    observed_at=completed_at,
                    result=terminal,
                    final_result=result,
                )
                return PerceptionAdvanceResult(
                    status=terminal if terminal_recorded else "joined_existing",
                    progressed_units=1 if terminal_recorded else 0,
                    next_wake_at=self.next_wake_at(),
                    more_due=self.has_due_work(observed_at),
                )
            if selection_ordinal == 0:
                failure_codes = semantic_failures
                rejected_json = result_json
                continue
            return self._record_attempt_failure_result(
                attempt_id=window.attention_attempt_id,
                observed_at=completed_at,
                failure_code="model:invalid_selection_after_reselection",
            )
        raise AssertionError("shadow attention reselection loop fell through")

    @staticmethod
    def _serialize_raw_result(raw_result: object) -> tuple[str, str]:
        if isinstance(raw_result, CharacterAttentionResult):
            value = raw_result.model_dump(mode="json")
        else:
            value = raw_result
        encoded = _canonical_json(value).encode("utf-8")
        if len(encoded) > _MAX_RECORDED_RESULT_BYTES:
            raise ValueError("attention_result_too_large")
        return encoded.decode("utf-8"), _sha256_bytes(encoded)

    @staticmethod
    def _safe_invalid_result(raw_result: object) -> tuple[str, str]:
        try:
            encoded = _canonical_json(raw_result).encode("utf-8")
        except Exception:
            encoded = _canonical_json(
                {"unserializable_result_type": type(raw_result).__name__}
            ).encode("utf-8")
        digest = _sha256_bytes(encoded)
        if len(encoded) > _MAX_RECORDED_RESULT_BYTES:
            encoded = _canonical_json(
                {"oversized_result_sha256": digest, "byte_count": len(encoded)}
            ).encode("utf-8")
        return encoded.decode("utf-8"), digest

    @staticmethod
    def _result_shape_failure(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            first = exc.errors(include_url=False)[0]
            field = ".".join(str(item) for item in first.get("loc", ())) or "root"
            return f"result_shape_invalid:{field}"[:256]
        return f"result_shape_invalid:{exc}"[:256]

    @staticmethod
    def _validate_result(
        *,
        result: CharacterAttentionResult,
        window: PerceptionWindow,
        context: CharacterAttentionContext,
    ) -> tuple[str, ...]:
        dossiers = {item.candidate_ref: item for item in window.candidates}
        context_refs = {
            item.context_ref
            for item in (
                *context.current_self_state,
                *context.situation,
                *context.relevant_context,
            )
        }
        failures: list[str] = []
        seen_candidates: set[str] = set()
        for selection in result.selections:
            if selection.candidate_ref in seen_candidates:
                failures.append(f"duplicate_candidate:{selection.candidate_ref}")
                continue
            seen_candidates.add(selection.candidate_ref)
            dossier = dossiers.get(selection.candidate_ref)
            if dossier is None:
                failures.append(f"unknown_candidate:{selection.candidate_ref}")
                continue
            revision_sources = {
                item.signal_revision_ref: item.source_id for item in dossier.model_visible_material
            }
            if len(selection.exact_signal_revision_refs) != len(
                set(selection.exact_signal_revision_refs)
            ):
                failures.append(f"duplicate_revision_ref:{selection.candidate_ref}")
            unknown_revisions = tuple(
                item
                for item in selection.exact_signal_revision_refs
                if item not in revision_sources
            )
            failures.extend(f"unknown_revision:{item}" for item in unknown_revisions)
            channel = next(
                (
                    item
                    for item in dossier.accessible_channels
                    if item.channel_ref == selection.selected_channel_ref
                ),
                None,
            )
            if channel is None:
                failures.append(f"unknown_channel:{selection.selected_channel_ref}")
            else:
                inaccessible = tuple(
                    revision_ref
                    for revision_ref in selection.exact_signal_revision_refs
                    if revision_ref in revision_sources
                    and revision_sources[revision_ref] not in channel.accessible_source_ids
                )
                failures.extend(
                    f"channel_cannot_access_revision:{revision_ref}"
                    for revision_ref in inaccessible
                )
            failures.extend(
                f"unknown_context_ref:{item}"
                for item in selection.attended_context_refs
                if item not in context_refs
            )
        return tuple(dict.fromkeys(failures))

    def _record_model_audit(
        self,
        *,
        request: CharacterAttentionRequest,
        result_json: str,
        result_hash: str,
        validation_failure_codes: tuple[str, ...],
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO external_perception_attention_model_audits (
                    attention_attempt_id, retry_ordinal, selection_ordinal,
                    request_hash, result_hash, result_json,
                    validation_failure_codes_json, technical_failure_code,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    request.attention_attempt_id,
                    request.retry_ordinal,
                    request.selection_ordinal,
                    _sha256_bytes(request.model_dump_json().encode("utf-8")),
                    result_hash,
                    result_json,
                    _canonical_json(validation_failure_codes),
                    _iso_utc(started_at),
                    _iso_utc(completed_at),
                ),
            )
            self._connection.execute(
                """
                UPDATE external_perception_attention_attempts
                SET invalid_result_count = invalid_result_count + ?
                WHERE attention_attempt_id = ?
                """,
                (bool(validation_failure_codes), request.attention_attempt_id),
            )

    def _record_model_call(self, *, attention_attempt_id: str) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute(
                """
                UPDATE external_perception_attention_attempts
                SET model_call_count = model_call_count + 1
                WHERE attention_attempt_id = ? AND lease_owner = ?
                """,
                (attention_attempt_id, self._runtime.worker_id),
            )

    def _record_model_failure_audit(
        self,
        *,
        request: CharacterAttentionRequest,
        failure_code: str,
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        failure_json = _canonical_json({"technical_failure_code": failure_code})
        with self._database_write_lock, self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO external_perception_attention_model_audits (
                    attention_attempt_id, retry_ordinal, selection_ordinal,
                    request_hash, result_hash, result_json,
                    validation_failure_codes_json, technical_failure_code,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, NULL, '[]', ?, ?, ?)
                """,
                (
                    request.attention_attempt_id,
                    request.retry_ordinal,
                    request.selection_ordinal,
                    _sha256_bytes(request.model_dump_json().encode("utf-8")),
                    _sha256_bytes(failure_json.encode("utf-8")),
                    failure_code,
                    _iso_utc(started_at),
                    _iso_utc(completed_at),
                ),
            )

    def _terminal_attempt(
        self,
        *,
        attention_attempt_id: str,
        window: PerceptionWindow,
        observed_at: datetime,
        result: str,
        final_result: CharacterAttentionResult | None,
    ) -> bool:
        result_json = final_result.model_dump_json() if final_result is not None else None
        result_hash = (
            _sha256_bytes(result_json.encode("utf-8")) if result_json is not None else None
        )
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                updated = self._connection.execute(
                    """
                    UPDATE external_perception_attention_attempts
                    SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                        next_attempt_at = NULL,
                        final_result_json = ?, final_result_hash = ?, terminal_at = ?
                    WHERE attention_attempt_id = ? AND lease_owner = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        result,
                        result_json,
                        result_hash,
                        _iso_utc(observed_at),
                        attention_attempt_id,
                        self._runtime.worker_id,
                        _iso_utc(observed_at),
                    ),
                )
                if updated.rowcount != 1:
                    self._connection.execute("ROLLBACK")
                    return False
                self._connection.execute(
                    """
                    UPDATE external_perception_attention_opportunities
                    SET status = 'terminal', terminal_at = ?, terminal_result = ?
                    WHERE opportunity_id = ?
                    """,
                    (_iso_utc(observed_at), result, window.opportunity_id),
                )
                for dossier in window.candidates:
                    for revision_ref in dossier.exact_signal_revisions:
                        self._record_exposure(
                            signal_revision_ref=revision_ref,
                            attention_attempt_id=attention_attempt_id,
                            result=result,
                            observed_at=observed_at,
                        )
                self._connection.execute("COMMIT")
                return True
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _record_exposure(
        self,
        *,
        signal_revision_ref: str,
        attention_attempt_id: str,
        result: str,
        observed_at: datetime,
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO external_perception_attention_exposures (
                signal_revision_ref, attention_policy_revision,
                deployment_mode_revision, attention_attempt_id, result, exposed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                signal_revision_ref,
                self._runtime.attention_policy_revision,
                self._runtime.deployment_mode_revision,
                attention_attempt_id,
                result,
                _iso_utc(observed_at),
            ),
        )

    def _record_attempt_failure_result(
        self, *, attempt_id: str, observed_at: datetime, failure_code: str
    ) -> PerceptionAdvanceResult:
        with self._database_write_lock, self._lock:
            row = self._connection.execute(
                """
                SELECT retry_ordinal FROM external_perception_attention_attempts
                WHERE attention_attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            ordinal = int(row["retry_ordinal"]) if row is not None else 0
            delay = _FAILURE_BACKOFF_SECONDS[min(ordinal, 2)]
            updated = self._connection.execute(
                """
                UPDATE external_perception_attention_attempts
                SET state = 'retry_wait', lease_owner = NULL, lease_expires_at = NULL,
                    next_attempt_at = ?, technical_failure_count = technical_failure_count + 1,
                    last_failure_code = ?
                WHERE attention_attempt_id = ? AND lease_owner = ?
                  AND lease_expires_at > ?
                """,
                (
                    _iso_utc(observed_at + timedelta(seconds=delay)),
                    failure_code[:128],
                    attempt_id,
                    self._runtime.worker_id,
                    _iso_utc(observed_at),
                ),
            )
            if updated.rowcount != 1:
                return PerceptionAdvanceResult(
                    status="joined_existing",
                    progressed_units=0,
                    next_wake_at=self.next_wake_at(),
                    more_due=False,
                )
        return PerceptionAdvanceResult(
            status="retry_wait",
            progressed_units=1,
            next_wake_at=self.next_wake_at(),
            more_due=self.has_due_work(observed_at),
        )

    @staticmethod
    def _technical_failure_code(stage: str, exc: Exception) -> str:
        if isinstance(exc, CharacterAttentionTechnicalFailure):
            return f"{stage}:{exc.failure_code}"[:128]
        return f"{stage}:{type(exc).__name__}"[:128]

    def has_due_work(self, observed_at: datetime) -> bool:
        with self._lock:
            eligible_signal = bool(self._eligible_signal_rows(observed_at=observed_at))
            due_attempt = self._connection.execute(
                """
                SELECT 1 FROM external_perception_attention_attempts
                WHERE (state = 'retry_wait' AND next_attempt_at <= ?)
                   OR (state = 'claimed' AND lease_expires_at <= ?)
                LIMIT 1
                """,
                (_iso_utc(observed_at), _iso_utc(observed_at)),
            ).fetchone()
            due_opportunity = self._connection.execute(
                """
                SELECT 1 FROM external_perception_attention_opportunities
                WHERE status = 'waiting'
                  AND COALESCE(next_attempt_at, ready_at) <= ?
                LIMIT 1
                """,
                (_iso_utc(observed_at),),
            ).fetchone()
        return eligible_signal or due_attempt is not None or due_opportunity is not None

    def next_wake_at(self) -> datetime | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT MIN(wake_at) AS wake_at FROM (
                    SELECT COALESCE(next_attempt_at, ready_at) AS wake_at
                    FROM external_perception_attention_opportunities
                    WHERE status = 'waiting'
                    UNION ALL
                    SELECT CASE
                        WHEN state = 'claimed' THEN lease_expires_at
                        ELSE next_attempt_at
                    END AS wake_at
                    FROM external_perception_attention_attempts
                    WHERE state IN ('claimed', 'retry_wait')
                )
                """
            ).fetchone()
        return _parse_datetime(row["wake_at"]) if row is not None and row["wake_at"] else None

    def health_snapshot(self, *, as_of: datetime) -> ShadowAttentionHealthSnapshot:
        with self._lock:
            eligible_count = len(self._eligible_signal_rows(observed_at=as_of))
            counts = {
                str(row["state"]): int(row["total"])
                for row in self._connection.execute(
                    """
                    SELECT state, COUNT(*) AS total
                    FROM external_perception_attention_attempts GROUP BY state
                    """
                ).fetchall()
            }
            waiting_count = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM external_perception_attention_opportunities
                    WHERE status = 'waiting'
                    """
                ).fetchone()[0]
            )
            exposed_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM external_perception_attention_exposures"
                ).fetchone()[0]
            )
            recent = self._connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN validation_failure_codes_json != '[]' THEN 1 ELSE 0 END), 0)
                        AS invalids
                FROM external_perception_attention_model_audits
                WHERE completed_at >= ?
                """,
                (_iso_utc(as_of - timedelta(hours=24)),),
            ).fetchone()
            calls_24h = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(SUM(model_call_count), 0)
                    FROM external_perception_attention_attempts
                    WHERE COALESCE(last_attempt_at, created_at) >= ?
                    """,
                    (_iso_utc(as_of - timedelta(hours=24)),),
                ).fetchone()[0]
            )
            technical_24h = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(SUM(technical_failure_count), 0)
                    FROM external_perception_attention_attempts
                    WHERE last_attempt_at >= ?
                    """,
                    (_iso_utc(as_of - timedelta(hours=24)),),
                ).fetchone()[0]
            )
            last = self._connection.execute(
                """
                SELECT attention_attempt_id, state, last_failure_code
                FROM external_perception_attention_attempts
                ORDER BY COALESCE(terminal_at, last_attempt_at, created_at) DESC LIMIT 1
                """
            ).fetchone()
            opportunity_failure = self._connection.execute(
                """
                SELECT last_failure_code FROM external_perception_attention_opportunities
                WHERE last_failure_code IS NOT NULL ORDER BY opened_at DESC LIMIT 1
                """
            ).fetchone()
        state: str
        expired_claim = False
        with self._lock:
            expired_claim = (
                self._connection.execute(
                    """
                SELECT 1 FROM external_perception_attention_attempts
                WHERE state = 'claimed' AND lease_expires_at <= ? LIMIT 1
                """,
                    (_iso_utc(as_of),),
                ).fetchone()
                is not None
            )
        if expired_claim:
            state = "degraded"
        elif counts.get("claimed", 0):
            state = "considering"
        elif counts.get("retry_wait", 0) or opportunity_failure is not None:
            state = "retry_wait"
        elif waiting_count:
            state = "window_wait"
        elif last is not None and str(last["state"]) in {
            "attention_no_selection",
            "shadow_selected",
        }:
            state = str(last["state"])
        else:
            state = "no_candidate"
        last_failure = None
        if last is not None and last["last_failure_code"] is not None:
            last_failure = str(last["last_failure_code"])
        elif opportunity_failure is not None:
            last_failure = str(opportunity_failure["last_failure_code"])
        return ShadowAttentionHealthSnapshot(
            state=state,
            deployment_mode_revision=self._runtime.deployment_mode_revision,
            eligible_signal_count=eligible_count,
            pending_opportunity_count=waiting_count,
            waiting_window_count=waiting_count,
            claimed_attempt_count=counts.get("claimed", 0),
            retry_wait_count=counts.get("retry_wait", 0),
            model_no_selection_count=counts.get("attention_no_selection", 0),
            shadow_selected_count=counts.get("shadow_selected", 0),
            exposed_signal_count=exposed_count,
            model_call_count_24h=calls_24h,
            invalid_result_count_24h=int(recent["invalids"]),
            technical_failure_count_24h=technical_24h,
            last_attempt_id=(str(last["attention_attempt_id"]) if last is not None else None),
            last_result=(str(last["state"]) if last is not None else None),
            last_failure_code=last_failure,
            next_attention_at=self.next_wake_at(),
        )


__all__ = ["SQLiteShadowAttentionCoordinator"]
