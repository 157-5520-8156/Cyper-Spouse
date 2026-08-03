"""Durable live attention bridge from sourced sidecar windows to V2 acceptance."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import sqlite3
import unicodedata

from pydantic import ValidationError

from ..errors import ConcurrencyConflict
from ..external_perception_acceptance import ExternalPerceptionAcceptanceReceipt
from ..external_perception_events import (
    ExternalPerceptionChannelProof,
    ExternalPerceptionLiveDelivery,
    ExternalPerceptionSelection,
    FrozenExternalSignalSnapshot,
    canonical_external_perception_json,
    external_perception_value_hash,
)
from .attention import (
    SQLiteShadowAttentionCoordinator,
    _FAILURE_BACKOFF_SECONDS,
    _canonical_json,
    _iso_utc,
    _parse_datetime,
    _sha256_bytes,
    _stable_ref,
)
from .contracts import (
    AuditedLiveCharacterAttentionResult,
    CharacterAttentionTechnicalFailure,
    ExternalSignalSourceItem,
    LiveAttentionRuntime,
    LiveCharacterAttentionContext,
    LiveCharacterAttentionRequest,
    LivePerceptionWindow,
    PerceptionAdvanceResult,
)


def _quote_comparison_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _reproduces_non_quotable_text(
    *, output: str, source: str, minimum_protected_characters: int = 8
) -> bool:
    """Detect exact protected prose reuse without making a semantic choice.

    This is a narrow licensing boundary, not a style heuristic. Short source
    summaries must be copied in full to fail; longer summaries fail on any
    exact 32-character span after Unicode/whitespace normalization.
    """

    protected = _quote_comparison_text(source)
    candidate = _quote_comparison_text(output)
    if len(protected) < minimum_protected_characters:
        return False
    if len(protected) <= 32:
        return protected in candidate
    return any(protected[index : index + 32] in candidate for index in range(len(protected) - 31))


class SQLiteLiveAttentionCoordinator(SQLiteShadowAttentionCoordinator):
    """Live-only coordinator with a durable acceptance outbox."""

    _runtime: LiveAttentionRuntime

    def _create_schema(self) -> None:
        super()._create_schema()
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_perception_live_outbox (
                attention_attempt_id TEXT PRIMARY KEY,
                delivery_json TEXT NOT NULL,
                delivery_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                acceptance_failure_count INTEGER NOT NULL,
                next_attempt_at TEXT,
                last_failure_code TEXT,
                receipt_json TEXT,
                last_acceptance_attempt_at TEXT,
                committed_at TEXT,
                superseded_at TEXT
            );

            CREATE INDEX IF NOT EXISTS external_perception_live_outbox_due
            ON external_perception_live_outbox (state, next_attempt_at);
            """
        )
        columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(external_perception_live_outbox)"
            ).fetchall()
        }
        if "last_acceptance_attempt_at" not in columns:
            self._connection.execute(
                """
                ALTER TABLE external_perception_live_outbox
                ADD COLUMN last_acceptance_attempt_at TEXT
                """
            )

    def _delete_expired_attempts(self, observed_at: datetime) -> None:
        super()._delete_expired_attempts(observed_at)
        with self._database_write_lock, self._lock:
            self._connection.execute(
                """
                DELETE FROM external_perception_live_outbox
                WHERE attention_attempt_id NOT IN (
                    SELECT attention_attempt_id
                    FROM external_perception_attention_attempts
                )
                """
            )

    def _sync_pending_opportunity(self, observed_at: datetime) -> sqlite3.Row | None:
        """Open a fresh epoch after stale CAS without mutating the old attempt."""

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
                    epoch = int(
                        self._connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM external_perception_attention_opportunities
                            WHERE attention_policy_revision = ?
                              AND deployment_mode_revision = ?
                            """,
                            (
                                self._runtime.attention_policy_revision,
                                self._runtime.deployment_mode_revision,
                            ),
                        ).fetchone()[0]
                    )
                    opportunity_id = _stable_ref(
                        "perception-opportunity",
                        {
                            "world_id": self._runtime.world_id,
                            "first_signal_revision_ref": first_ref,
                            "attention_policy_revision": self._runtime.attention_policy_revision,
                            "deployment_mode_revision": self._runtime.deployment_mode_revision,
                            "consideration_epoch": epoch,
                        },
                    )
                    self._connection.execute(
                        """
                        INSERT INTO external_perception_attention_opportunities (
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
                        """
                        SELECT * FROM external_perception_attention_opportunities
                        WHERE opportunity_id = ?
                        """,
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

    def _freeze_window(
        self,
        *,
        opportunity_id: str,
        context: LiveCharacterAttentionContext,
        observed_at: datetime,
    ) -> LivePerceptionWindow | None:
        if context.world_id != self._runtime.world_id:
            raise ValueError("attention_context_world_mismatch")
        if context.actor_ref != self._runtime.actor_ref:
            raise ValueError("attention_context_actor_mismatch")
        dossiers = self._compile_dossiers(
            opportunity_id=opportunity_id,
            context=context,  # type: ignore[arg-type]
            observed_at=observed_at,
        )
        if not dossiers:
            return None
        snapshots = self._freeze_durable_snapshots(
            dossiers=dossiers,
            observed_at=observed_at,
        )
        candidate_snapshot = {
            "candidates": [item.model_dump(mode="json") for item in dossiers],
            "durable_snapshots": [item.model_dump(mode="json") for item in snapshots],
        }
        candidate_set_hash = _sha256_bytes(_canonical_json(candidate_snapshot).encode("utf-8"))
        attempt_id = _stable_ref(
            "attention-attempt",
            {
                "world_id": self._runtime.world_id,
                "opportunity_id": opportunity_id,
                "candidate_snapshot_hash": candidate_set_hash,
                "exact_world_cursor": context.pinned_world_cursor.model_dump(mode="json"),
                "attention_policy_revision": self._runtime.attention_policy_revision,
                "deployment_mode_revision": self._runtime.deployment_mode_revision,
            },
        )
        window_id = _stable_ref("perception-window", {"attention_attempt_id": attempt_id})
        window = LivePerceptionWindow(
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
            durable_snapshots=snapshots,
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
                    (
                        attempt_id,
                        window_id,
                        self._runtime.model.model_id,
                        _iso_utc(observed_at),
                    ),
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

    def _freeze_durable_snapshots(
        self,
        *,
        dossiers: tuple,
        observed_at: datetime,
    ) -> tuple[FrozenExternalSignalSnapshot, ...]:
        revisions = tuple(
            revision for dossier in dossiers for revision in dossier.exact_signal_revisions
        )
        snapshots: list[FrozenExternalSignalSnapshot] = []
        for revision_ref in revisions:
            with self._lock:
                row = self._connection.execute(
                    """
                    SELECT revision.*, evidence.evidence_hash, policy.policy_json
                    FROM external_signal_revisions AS revision
                    JOIN external_perception_raw_evidence AS evidence
                      ON evidence.evidence_ref = revision.evidence_ref
                    JOIN external_perception_source_policies AS policy
                      ON policy.policy_revision = revision.source_policy_revision
                    WHERE revision.signal_revision_ref = ?
                    """,
                    (revision_ref,),
                ).fetchone()
            if row is None:
                raise ValueError("live_snapshot_source_revision_missing")
            item = ExternalSignalSourceItem.model_validate_json(str(row["normalized_json"]))
            policy = json.loads(str(row["policy_json"]))
            visible = next(
                item
                for dossier in dossiers
                for item in dossier.model_visible_material
                if item.signal_revision_ref == revision_ref
            )
            visible_json = canonical_external_perception_json(visible.model_dump(mode="json"))
            lineage = tuple(
                dict.fromkeys(
                    str(value)
                    for value in (row["supersedes_ref"], row["correction_of_ref"])
                    if value is not None
                )
            )
            material_hash = _sha256_bytes(visible_json.encode("utf-8"))
            snapshots.append(
                FrozenExternalSignalSnapshot(
                    snapshot_ref=_stable_ref(
                        "external-snapshot",
                        {
                            "signal_revision_ref": revision_ref,
                            "model_visible_material_hash": material_hash,
                        },
                    ),
                    signal_revision_ref=revision_ref,
                    source_id=str(row["source_id"]),
                    upstream_publisher_ref=item.upstream_publisher_ref,
                    upstream_item_id=item.upstream_item_id,
                    source_policy_revision=str(row["source_policy_revision"]),
                    source_payload_hash=str(row["evidence_hash"]),
                    normalized_hash=str(row["normalized_hash"]),
                    headline=item.headline,
                    licensed_summary=item.licensed_summary,
                    canonical_url=item.canonical_url,
                    occurred_at=item.occurred_at,
                    published_at=item.published_at,
                    observed_at=_parse_datetime(row["observed_at"]),
                    expires_at=_parse_datetime(row["effective_expires_at"]),
                    correction_lineage_refs=lineage,
                    model_visible_material_json=visible_json,
                    model_visible_material_hash=material_hash,
                    may_expose_to_character_model=bool(policy.get("may_expose_to_character_model")),
                    may_quote=bool(policy.get("may_quote")),
                    may_freeze_durable_snapshot=bool(policy.get("may_freeze_durable_snapshot")),
                )
            )
        return tuple(snapshots)

    def _load_frozen_request(
        self, window_id: str
    ) -> tuple[LivePerceptionWindow, LiveCharacterAttentionContext]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT window_json, context_json
                FROM external_perception_attention_windows WHERE window_id = ?
                """,
                (window_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("live attention attempt lost its frozen window")
        return (
            LivePerceptionWindow.model_validate_json(str(row["window_json"])),
            LiveCharacterAttentionContext.model_validate_json(str(row["context_json"])),
        )

    def _recoverable_attempt(self, observed_at: datetime) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                """
                SELECT attempt.*
                FROM external_perception_attention_attempts AS attempt
                LEFT JOIN external_perception_live_outbox AS outbox
                  ON outbox.attention_attempt_id = attempt.attention_attempt_id
                WHERE (attempt.state = 'retry_wait' AND attempt.next_attempt_at <= ?)
                   OR (attempt.state = 'delivery_pending'
                       AND (outbox.next_attempt_at IS NULL OR outbox.next_attempt_at <= ?))
                   OR attempt.state = 'claimed'
                ORDER BY COALESCE(
                    outbox.next_attempt_at,
                    attempt.next_attempt_at,
                    attempt.lease_expires_at
                ), attempt.attention_attempt_id
                LIMIT 1
                """,
                (_iso_utc(observed_at), _iso_utc(observed_at)),
            ).fetchone()

    async def _advance_attempt(
        self, *, row: sqlite3.Row, observed_at: datetime
    ) -> PerceptionAdvanceResult:
        with self._lock:
            outbox = self._connection.execute(
                """
                SELECT 1 FROM external_perception_live_outbox
                WHERE attention_attempt_id = ? AND state = 'pending'
                """,
                (row["attention_attempt_id"],),
            ).fetchone()
        if outbox is not None:
            return await self._advance_delivery(
                attention_attempt_id=str(row["attention_attempt_id"]),
                observed_at=observed_at,
            )
        return await super()._advance_attempt(row=row, observed_at=observed_at)

    async def _call_model(
        self,
        *,
        attempt_row: sqlite3.Row,
        window: LivePerceptionWindow,
        context: LiveCharacterAttentionContext,
        observed_at: datetime,
    ) -> PerceptionAdvanceResult:
        retry_ordinal = int(attempt_row["retry_ordinal"])
        failure_codes: tuple[str, ...] = ()
        rejected_json: str | None = None
        for selection_ordinal in (0, 1):
            request = LiveCharacterAttentionRequest(
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
                audited = AuditedLiveCharacterAttentionResult.model_validate_json(result_json)
                semantic_failures = self._validate_live_result(
                    result=audited,
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
                    audited = None
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
            if not semantic_failures and audited is not None:
                if not audited.decision.selections:
                    recorded = self._terminal_attempt(
                        attention_attempt_id=window.attention_attempt_id,
                        window=window,  # type: ignore[arg-type]
                        observed_at=completed_at,
                        result="attention_no_selection",
                        final_result=audited.decision,  # type: ignore[arg-type]
                    )
                    return PerceptionAdvanceResult(
                        status="attention_no_selection" if recorded else "joined_existing",
                        progressed_units=1 if recorded else 0,
                        next_wake_at=self.next_wake_at(),
                        more_due=self.has_due_work(observed_at),
                    )
                delivery = self._build_delivery(
                    audited=audited,
                    window=window,
                    context=context,
                    observed_at=completed_at,
                )
                if not self._persist_delivery(delivery=delivery, observed_at=completed_at):
                    return PerceptionAdvanceResult(
                        status="joined_existing",
                        progressed_units=0,
                        next_wake_at=self.next_wake_at(),
                    )
                return await self._advance_delivery(
                    attention_attempt_id=window.attention_attempt_id,
                    observed_at=completed_at,
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
        raise AssertionError("live attention reselection loop fell through")

    @staticmethod
    def _serialize_raw_result(raw_result: object) -> tuple[str, str]:
        if isinstance(raw_result, AuditedLiveCharacterAttentionResult):
            value = raw_result.model_dump(mode="json")
        else:
            value = raw_result
        encoded = _canonical_json(value).encode("utf-8")
        if len(encoded) > 262_144:
            raise ValueError("attention_result_too_large")
        return encoded.decode("utf-8"), _sha256_bytes(encoded)

    @classmethod
    def _validate_live_result(
        cls,
        *,
        result: AuditedLiveCharacterAttentionResult,
        window: LivePerceptionWindow,
        context: LiveCharacterAttentionContext,
    ) -> tuple[str, ...]:
        failures = list(
            cls._validate_result(
                result=result.decision,  # type: ignore[arg-type]
                window=window,  # type: ignore[arg-type]
                context=context,  # type: ignore[arg-type]
            )
        )
        snapshots = {item.signal_revision_ref: item for item in window.durable_snapshots}
        for selection in result.decision.selections:
            if not selection.epistemic_notes.strip():
                failures.append(f"epistemic_notes_required:{selection.candidate_ref}")
            authored_text = selection.subjective_summary + "\n" + selection.epistemic_notes
            for revision_ref in selection.exact_signal_revision_refs:
                snapshot = snapshots.get(revision_ref)
                reproduces_protected_source = (
                    snapshot is not None
                    and not snapshot.may_quote
                    and (
                        _reproduces_non_quotable_text(
                            output=authored_text,
                            source=snapshot.headline,
                            minimum_protected_characters=4,
                        )
                        or _reproduces_non_quotable_text(
                            output=authored_text,
                            source=snapshot.licensed_summary,
                        )
                    )
                )
                if reproduces_protected_source:
                    failures.append(f"non_quotable_source_reproduced:{revision_ref}")
        audit = result.model_result
        expected_proposal_hash = "sha256:" + external_perception_value_hash(
            result.decision.model_dump(mode="json")
        )
        if audit.attempt_id != window.attention_attempt_id:
            failures.append("audit_attempt_mismatch")
        if audit.trigger_ref != window.attention_attempt_id:
            failures.append("audit_trigger_mismatch")
        if audit.capsule_id != window.candidate_set_hash:
            failures.append("audit_capsule_mismatch")
        if audit.evaluated_world_revision != window.pinned_world_cursor.world_revision:
            failures.append("audit_world_revision_mismatch")
        if audit.proposal_hash != expected_proposal_hash:
            failures.append("audit_proposal_hash_mismatch")
        return tuple(dict.fromkeys(failures))

    def _build_delivery(
        self,
        *,
        audited: AuditedLiveCharacterAttentionResult,
        window: LivePerceptionWindow,
        context: LiveCharacterAttentionContext,
        observed_at: datetime,
    ) -> ExternalPerceptionLiveDelivery:
        dossiers = {item.candidate_ref: item for item in window.candidates}
        snapshots = {item.signal_revision_ref: item for item in window.durable_snapshots}
        selections: list[ExternalPerceptionSelection] = []
        for model_selection in audited.decision.selections:
            dossier = dossiers[model_selection.candidate_ref]
            channel = next(
                item
                for item in dossier.accessible_channels
                if item.channel_ref == model_selection.selected_channel_ref
            )
            proof_value = channel.model_dump(mode="json")
            for revision_ref in model_selection.exact_signal_revision_refs:
                selections.append(
                    ExternalPerceptionSelection(
                        perception_id=_stable_ref(
                            "external-perception",
                            {
                                "attention_attempt_id": window.attention_attempt_id,
                                "candidate_ref": model_selection.candidate_ref,
                                "signal_revision_ref": revision_ref,
                                "channel_ref": channel.channel_ref,
                            },
                        ),
                        candidate_ref=model_selection.candidate_ref,
                        snapshot=snapshots[revision_ref],
                        channel=ExternalPerceptionChannelProof(
                            channel_ref=channel.channel_ref,
                            channel_kind=channel.channel_kind,
                            proof_refs=channel.evidence_refs,
                            proof_hash=external_perception_value_hash(proof_value),
                            access_summary=canonical_external_perception_json(proof_value),
                        ),
                        subjective_summary=model_selection.subjective_summary,
                        epistemic_notes=model_selection.epistemic_notes,
                        attended_context_refs=model_selection.attended_context_refs,
                        privacy_class=model_selection.privacy_class,
                    )
                )
        return ExternalPerceptionLiveDelivery(
            world_id=window.world_id,
            deployment_mode_revision=window.deployment_mode_revision,
            attention_attempt_id=window.attention_attempt_id,
            window_id=window.window_id,
            candidate_snapshot_hash=window.candidate_set_hash,
            pinned_cursor=window.pinned_world_cursor,
            actor_ref=window.actor_ref,
            encountered_world_time=context.world_logical_time,
            observed_wall_time=observed_at,
            attention_model_result=audited.model_result,
            selections=tuple(selections),
        )

    def _persist_delivery(
        self, *, delivery: ExternalPerceptionLiveDelivery, observed_at: datetime
    ) -> bool:
        delivery_json = delivery.model_dump_json()
        delivery_hash = _sha256_bytes(delivery_json.encode("utf-8"))
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    """
                    SELECT delivery_hash FROM external_perception_live_outbox
                    WHERE attention_attempt_id = ?
                    """,
                    (delivery.attention_attempt_id,),
                ).fetchone()
                if existing is not None and str(existing["delivery_hash"]) != delivery_hash:
                    raise RuntimeError("live attention delivery identity changed")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO external_perception_live_outbox (
                        attention_attempt_id, delivery_json, delivery_hash, state,
                        acceptance_failure_count
                    ) VALUES (?, ?, ?, 'pending', 0)
                    """,
                    (delivery.attention_attempt_id, delivery_json, delivery_hash),
                )
                updated = self._connection.execute(
                    """
                    UPDATE external_perception_attention_attempts
                    SET state = 'delivery_pending', lease_owner = NULL,
                        lease_expires_at = NULL, next_attempt_at = NULL,
                        final_result_json = ?, final_result_hash = ?
                    WHERE attention_attempt_id = ? AND lease_owner = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        delivery_json,
                        delivery_hash,
                        delivery.attention_attempt_id,
                        self._runtime.worker_id,
                        _iso_utc(observed_at),
                    ),
                )
                if updated.rowcount != 1:
                    self._connection.execute("ROLLBACK")
                    return False
                self._connection.execute("COMMIT")
                return True
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    async def _advance_delivery(
        self, *, attention_attempt_id: str, observed_at: datetime
    ) -> PerceptionAdvanceResult:
        claimed = self._claim_delivery(
            attention_attempt_id=attention_attempt_id,
            observed_at=observed_at,
        )
        if claimed is None:
            return PerceptionAdvanceResult(
                status="joined_existing",
                progressed_units=0,
                next_wake_at=self.next_wake_at(),
            )
        delivery = ExternalPerceptionLiveDelivery.model_validate_json(str(claimed["delivery_json"]))
        try:
            raw_receipt = await self._runtime.acceptance_port.accept_external_perception(delivery)
            receipt = ExternalPerceptionAcceptanceReceipt.model_validate(raw_receipt)
            if receipt.attention_attempt_id != attention_attempt_id or len(
                receipt.perceptions
            ) != len(delivery.selections):
                raise ValueError("acceptance_receipt_identity_mismatch")
        except ConcurrencyConflict:
            self._mark_superseded(
                attention_attempt_id=attention_attempt_id,
                window_id=delivery.window_id,
                observed_at=self._wall_clock(),
            )
            return PerceptionAdvanceResult(
                status="superseded",
                progressed_units=1,
                next_wake_at=self.next_wake_at(),
            )
        except Exception as exc:
            return self._record_delivery_failure(
                attention_attempt_id=attention_attempt_id,
                observed_at=self._wall_clock(),
                failure_code=self._technical_failure_code("acceptance", exc),
            )
        completed_at = self._wall_clock()
        committed = self._finalize_delivery(
            attention_attempt_id=attention_attempt_id,
            window_id=delivery.window_id,
            receipt=receipt,
            observed_at=completed_at,
        )
        return PerceptionAdvanceResult(
            status="perception_committed" if committed else "joined_existing",
            progressed_units=1 if committed else 0,
            committed_perception_count=len(receipt.perceptions) if committed else 0,
            next_wake_at=self.next_wake_at(),
            more_due=self.has_due_work(observed_at),
        )

    def _claim_delivery(
        self, *, attention_attempt_id: str, observed_at: datetime
    ) -> sqlite3.Row | None:
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT attempt.state, attempt.lease_expires_at,
                           outbox.*
                    FROM external_perception_attention_attempts AS attempt
                    JOIN external_perception_live_outbox AS outbox
                      ON outbox.attention_attempt_id = attempt.attention_attempt_id
                    WHERE attempt.attention_attempt_id = ? AND outbox.state = 'pending'
                    """,
                    (attention_attempt_id,),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                if (
                    row["next_attempt_at"] is not None
                    and _parse_datetime(row["next_attempt_at"]) > observed_at
                ):
                    self._connection.execute("COMMIT")
                    return None
                if (
                    str(row["state"]) == "claimed"
                    and row["lease_expires_at"] is not None
                    and _parse_datetime(row["lease_expires_at"]) > observed_at
                ):
                    self._connection.execute("COMMIT")
                    return None
                self._connection.execute(
                    """
                    UPDATE external_perception_attention_attempts
                    SET state = 'claimed', lease_owner = ?, lease_expires_at = ?
                    WHERE attention_attempt_id = ?
                    """,
                    (
                        self._runtime.worker_id,
                        _iso_utc(observed_at + timedelta(seconds=self._runtime.lease_seconds)),
                        attention_attempt_id,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE external_perception_live_outbox
                    SET last_acceptance_attempt_at = ?
                    WHERE attention_attempt_id = ?
                    """,
                    (_iso_utc(observed_at), attention_attempt_id),
                )
                claimed = self._connection.execute(
                    """
                    SELECT outbox.*
                    FROM external_perception_live_outbox AS outbox
                    WHERE outbox.attention_attempt_id = ?
                    """,
                    (attention_attempt_id,),
                ).fetchone()
                self._connection.execute("COMMIT")
                return claimed
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _record_delivery_failure(
        self, *, attention_attempt_id: str, observed_at: datetime, failure_code: str
    ) -> PerceptionAdvanceResult:
        with self._database_write_lock, self._lock:
            row = self._connection.execute(
                """
                SELECT acceptance_failure_count FROM external_perception_live_outbox
                WHERE attention_attempt_id = ?
                """,
                (attention_attempt_id,),
            ).fetchone()
            ordinal = int(row["acceptance_failure_count"]) + 1 if row is not None else 1
            delay = _FAILURE_BACKOFF_SECONDS[min(ordinal - 1, 2)]
            wake = observed_at + timedelta(seconds=delay)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    UPDATE external_perception_live_outbox
                    SET acceptance_failure_count = ?, next_attempt_at = ?,
                        last_failure_code = ?
                    WHERE attention_attempt_id = ?
                    """,
                    (ordinal, _iso_utc(wake), failure_code[:128], attention_attempt_id),
                )
                self._connection.execute(
                    """
                    UPDATE external_perception_attention_attempts
                    SET state = 'delivery_pending', lease_owner = NULL,
                        lease_expires_at = NULL, technical_failure_count =
                        technical_failure_count + 1, last_failure_code = ?
                    WHERE attention_attempt_id = ? AND lease_owner = ?
                    """,
                    (failure_code[:128], attention_attempt_id, self._runtime.worker_id),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return PerceptionAdvanceResult(
            status="retry_wait",
            progressed_units=1,
            next_wake_at=wake,
        )

    def _mark_superseded(
        self, *, attention_attempt_id: str, window_id: str, observed_at: datetime
    ) -> None:
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                opportunity = self._connection.execute(
                    """
                    SELECT opportunity_id FROM external_perception_attention_windows
                    WHERE window_id = ?
                    """,
                    (window_id,),
                ).fetchone()
                self._connection.execute(
                    """
                    UPDATE external_perception_attention_attempts
                    SET state = 'superseded', lease_owner = NULL, lease_expires_at = NULL,
                        next_attempt_at = NULL, terminal_at = ?,
                        last_failure_code = 'acceptance:stale_cursor'
                    WHERE attention_attempt_id = ? AND lease_owner = ?
                    """,
                    (_iso_utc(observed_at), attention_attempt_id, self._runtime.worker_id),
                )
                self._connection.execute(
                    """
                    UPDATE external_perception_live_outbox
                    SET state = 'superseded', superseded_at = ?,
                        last_failure_code = 'acceptance:stale_cursor'
                    WHERE attention_attempt_id = ?
                    """,
                    (_iso_utc(observed_at), attention_attempt_id),
                )
                if opportunity is not None:
                    self._connection.execute(
                        """
                        UPDATE external_perception_attention_opportunities
                        SET status = 'terminal', terminal_at = ?, terminal_result = 'superseded'
                        WHERE opportunity_id = ?
                        """,
                        (_iso_utc(observed_at), opportunity["opportunity_id"]),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _finalize_delivery(
        self,
        *,
        attention_attempt_id: str,
        window_id: str,
        receipt: ExternalPerceptionAcceptanceReceipt,
        observed_at: datetime,
    ) -> bool:
        with self._database_write_lock, self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                opportunity = self._connection.execute(
                    """
                    SELECT opportunity_id, window_json
                    FROM external_perception_attention_windows WHERE window_id = ?
                    """,
                    (window_id,),
                ).fetchone()
                if opportunity is None:
                    raise RuntimeError("committed live delivery lost its frozen window")
                updated = self._connection.execute(
                    """
                    UPDATE external_perception_attention_attempts
                    SET state = 'perception_committed', lease_owner = NULL,
                        lease_expires_at = NULL, next_attempt_at = NULL,
                        terminal_at = ?, last_failure_code = NULL
                    WHERE attention_attempt_id = ? AND lease_owner = ?
                    """,
                    (_iso_utc(observed_at), attention_attempt_id, self._runtime.worker_id),
                )
                if updated.rowcount != 1:
                    self._connection.execute("ROLLBACK")
                    return False
                self._connection.execute(
                    """
                    UPDATE external_perception_live_outbox
                    SET state = 'committed', receipt_json = ?, committed_at = ?,
                        next_attempt_at = NULL, last_failure_code = NULL
                    WHERE attention_attempt_id = ?
                    """,
                    (receipt.model_dump_json(), _iso_utc(observed_at), attention_attempt_id),
                )
                self._connection.execute(
                    """
                    UPDATE external_perception_attention_opportunities
                    SET status = 'terminal', terminal_at = ?,
                        terminal_result = 'perception_committed'
                    WHERE opportunity_id = ?
                    """,
                    (_iso_utc(observed_at), opportunity["opportunity_id"]),
                )
                window = LivePerceptionWindow.model_validate_json(str(opportunity["window_json"]))
                for dossier in window.candidates:
                    for revision_ref in dossier.exact_signal_revisions:
                        self._record_exposure(
                            signal_revision_ref=revision_ref,
                            attention_attempt_id=attention_attempt_id,
                            result="perception_committed",
                            observed_at=observed_at,
                        )
                self._connection.execute("COMMIT")
                return True
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def has_due_work(self, observed_at: datetime) -> bool:
        with self._lock:
            pending_delivery = self._connection.execute(
                """
                SELECT 1 FROM external_perception_live_outbox
                WHERE state = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                LIMIT 1
                """,
                (_iso_utc(observed_at),),
            ).fetchone()
        return pending_delivery is not None or super().has_due_work(observed_at)

    def next_wake_at(self) -> datetime | None:
        base = super().next_wake_at()
        with self._lock:
            row = self._connection.execute(
                """
                SELECT MIN(next_attempt_at) AS wake_at
                FROM external_perception_live_outbox
                WHERE state = 'pending' AND next_attempt_at IS NOT NULL
                """
            ).fetchone()
        delivery = _parse_datetime(row["wake_at"]) if row is not None and row["wake_at"] else None
        return (
            min(item for item in (base, delivery) if item is not None)
            if any(item is not None for item in (base, delivery))
            else None
        )

    def health_snapshot(self, *, as_of: datetime):
        base = super().health_snapshot(as_of=as_of)
        with self._lock:
            counts = {
                str(row["state"]): int(row["total"])
                for row in self._connection.execute(
                    """
                    SELECT state, COUNT(*) AS total
                    FROM external_perception_live_outbox GROUP BY state
                    """
                ).fetchall()
            }
            recent_failures = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(SUM(acceptance_failure_count), 0)
                    FROM external_perception_live_outbox
                    WHERE last_acceptance_attempt_at >= ?
                    """,
                    (_iso_utc(as_of - timedelta(hours=24)),),
                ).fetchone()[0]
            )
            last = self._connection.execute(
                """
                SELECT state, last_failure_code
                FROM external_perception_attention_attempts
                ORDER BY COALESCE(terminal_at, last_attempt_at, created_at) DESC LIMIT 1
                """
            ).fetchone()
        pending = counts.get("pending", 0)
        state = base.state
        if pending:
            state = "delivery_pending"
        elif last is not None and str(last["state"]) in {
            "perception_committed",
            "superseded",
        }:
            state = str(last["state"])
        return base.model_copy(
            update={
                "state": state,
                "last_result": str(last["state"]) if last is not None else base.last_result,
                "last_failure_code": (
                    str(last["last_failure_code"])
                    if last is not None and last["last_failure_code"] is not None
                    else base.last_failure_code
                ),
                "live_delivery_pending_count": pending,
                "live_committed_count": counts.get("committed", 0),
                "live_superseded_count": counts.get("superseded", 0),
                "acceptance_failure_count_24h": recent_failures,
                "outbox_backlog_count": pending,
            }
        )


__all__ = ["SQLiteLiveAttentionCoordinator"]
