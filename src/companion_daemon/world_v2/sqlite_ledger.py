from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Literal
from weakref import WeakKeyDictionary

from .accepted_ledger_batch import (
    AcceptedLedgerBatchHandle,
    AcceptedLedgerBatchIssuer,
)
from .batch_invariants import (
    reject_accepted_manifest_v3_without_recorder,
    validate_commit_batch,
)
from .errors import ConcurrencyConflict, IdempotencyConflict, LedgerIntegrityError
from .event_identity import validate_event_identity
from .ledger import (
    HistoricalLedgerEvent,
    OBSERVATION_HISTORY_MAX_BYTES,
    OBSERVATION_HISTORY_MAX_COMMIT_EVENTS,
    ObservationEventLocator,
    _observation_id,
    _preflight_commit_events,
    _validated_observation_locators,
    canonical_event_json,
    commit_request_hash,
    derived_commit_id,
)
from .ledger_prefix_proof import (
    IncrementalMmrV1,
    IncrementalSparseMerkleMapV1,
    LedgerLeafV1,
    MmrAppendPlanV1,
    ObservationLocatorValueV1,
    PrefixCheckpointLeafV1,
    SparseMerkleProofV1,
    commit_result_hash_v1,
    mmr_append_plan_from_node_lookup_v1,
    mmr_inclusion_proof_from_node_lookup_v1,
    mmr_root_from_node_lookup_v1,
    observation_locator_key,
    ordered_event_ids_hash_v1,
    sparse_merkle_proof_from_nodes_v1,
    sparse_merkle_put_from_node_lookup_v1,
    verify_checkpoint_in_prefix,
)
from .replay_evidence import ReplayCommitEvidence, ReplayEvidence, ReplayEventEvidence
from .reducers import (
    REDUCER_BUNDLE_VERSION,
    ReducerState,
    RevisionClass,
    event_definition,
    make_projection,
    reduce_event,
    require_reducer_bundle,
)
from .schemas import (
    CommitResult,
    CommittedWorldEventRef,
    LedgerProjection,
    ProjectionCursor,
    WorldEvent,
)
from .upcasting import CURRENT_SCHEMA_VERSION, require_target_schema, upcast_event


_V16_ONLY_STATE_KEYS = frozenset(
    {
        "clock_transition_history",
        "goals",
        "goal_transitions",
        "goal_proposals",
        "goal_proposal_ids",
        "locations",
        "location_transitions",
        "location_proposals",
        "location_proposal_ids",
        "resources",
        "resource_transitions",
        "resource_proposals",
        "resource_proposal_ids",
        "attentions",
        "attention_transitions",
        "attention_proposals",
        "attention_proposal_ids",
    }
)
_V17_ONLY_STATE_KEYS = frozenset({"model_result_audits", "proposal_audits"})
_V18_ONLY_STATE_KEYS = frozenset({"acceptance_manifests_v2"})
_V19_ONLY_STATE_KEYS = frozenset(
    {"fact_commit_proposal_audits_v2", "acceptance_manifests_v3"}
)
_V20_ONLY_STATE_KEYS = frozenset(
    {
        "minimal_reply_manifests",
        "stored_message_payloads",
        "expression_plans",
        "expression_beats",
    }
)
_V24_ONLY_STATE_KEYS = frozenset({"expression_plan_manifests"})
_PREFIX_PROOF_VERSION = "world-v2-prefix-proof.2"
_PREVIOUS_PREFIX_PROOF_VERSION = "world-v2-prefix-proof.1"
_PREFIX_BITS_BYTES = 32
_MAX_PINNED_OBSERVATION_HISTORY_HANDLES = 1_024


@dataclass(frozen=True, slots=True)
class ProofBackedObservationLookup:
    """One exact locator result with an authenticated, non-ambiguous status."""

    locator: ObservationEventLocator
    status: Literal["found", "locator_missing"]
    event: HistoricalLedgerEvent | None


@dataclass(frozen=True, slots=True)
class _PinnedObservationHistoryProof:
    """Reader-private metadata for one verified historical prefix."""

    world_id: str
    cursor: ProjectionCursor
    anchor_leaf_count: int
    anchor_mmr_root: bytes
    checkpoint: PrefixCheckpointLeafV1 | None
    proof_version: str


class PinnedObservationHistoryHandle:
    """Opaque, non-serializable identity for one reader-issued pin.

    The associated cursor, roots, and checkpoint deliberately live only in the
    issuing reader's private registry.  Constructing another instance cannot
    produce a usable capability because it has no registry entry.
    """

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("pinned observation history handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("pinned observation history handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("pinned observation history handles cannot be copied")


class SQLiteProofBackedObservationReader:
    """Narrow SQLite-only reader for exact historical observation evidence.

    A caller receives one lookup per requested locator.  It must decide whether
    an authenticated absence is acceptable; this reader never aliases a locator
    or falls back to an unpinned event family.
    """

    __slots__ = ("__ledger", "__pins", "__pins_lock")

    def __init__(self, *, ledger: "SQLiteWorldLedger") -> None:
        self.__ledger = ledger
        self.__pins: WeakKeyDictionary[
            PinnedObservationHistoryHandle, _PinnedObservationHistoryProof
        ] = WeakKeyDictionary()
        self.__pins_lock = RLock()

    def pin(self, *, world_id: str, cursor: ProjectionCursor) -> PinnedObservationHistoryHandle:
        if world_id != self.__ledger.world_id:
            raise ValueError("proof reader belongs to another world")
        pin = self.__ledger._pin_observation_history_proof(cursor=cursor)
        handle = PinnedObservationHistoryHandle()
        with self.__pins_lock:
            if len(self.__pins) >= _MAX_PINNED_OBSERVATION_HISTORY_HANDLES:
                raise ValueError("observation proof reader has too many live pins")
            self.__pins[handle] = pin
        return handle

    def read(
        self,
        *,
        handle: PinnedObservationHistoryHandle,
        locators: Sequence[ObservationEventLocator],
    ) -> tuple[ProofBackedObservationLookup, ...]:
        if type(handle) is not PinnedObservationHistoryHandle:
            raise ValueError("observation proof handle is not owned by this reader")
        with self.__pins_lock:
            pin = self.__pins.get(handle)
        if pin is None:
            raise ValueError("observation proof handle is not owned by this reader")
        if pin.proof_version != _PREFIX_PROOF_VERSION:
            raise LedgerIntegrityError("pinned observation history proof version is stale")
        return self.__ledger._read_observation_history_proof(pin=pin, locators=locators)


def _prefix_bits_blob(prefix: int) -> bytes:
    if type(prefix) is not int or prefix < 0 or prefix.bit_length() > 256:
        raise LedgerIntegrityError("persisted locator-node prefix is invalid")
    return prefix.to_bytes(_PREFIX_BITS_BYTES, "big")


def _prefix_bits_int(value: object) -> int:
    if type(value) is not bytes or len(value) != _PREFIX_BITS_BYTES:
        raise LedgerIntegrityError("persisted locator-node prefix is invalid")
    return int.from_bytes(value, "big")


def _upcast_legacy_appraisal_trigger(
    raw_event: dict[str, object],
    *,
    settlement_sources: dict[str, str],
    trigger_sources: dict[str, str],
) -> dict[str, object]:
    """Add provenance introduced after the v3 LIFE bundle to replay bytes."""

    event_type = raw_event.get("event_type")
    payload_json = raw_event.get("payload_json")
    if not isinstance(payload_json, str):
        return raw_event
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        return raw_event
    if event_type == "WorldOccurrenceSettled":
        trigger_ref = payload.get("appraisal_trigger_ref")
        event_id = raw_event.get("event_id")
        if isinstance(trigger_ref, str) and isinstance(event_id, str):
            settlement_sources[trigger_ref] = event_id
        return raw_event
    if event_type not in {
        "TriggerProcessOpened",
        "TriggerProcessClaimed",
        "TriggerProcessReclaimed",
    }:
        return raw_event
    process = payload.get("process")
    if not isinstance(process, dict) or process.get("process_kind") != "npc_world_appraisal":
        return raw_event
    trigger_id = process.get("trigger_id")
    trigger_ref = process.get("trigger_ref")
    if not isinstance(trigger_id, str) or not isinstance(trigger_ref, str):
        return raw_event
    source = process.get("source_evidence_ref")
    if not isinstance(source, str):
        source = trigger_sources.get(trigger_id) or settlement_sources.get(trigger_ref)
        if source is None:
            return raw_event
        process = {**process, "source_evidence_ref": source}
        payload = {**payload, "process": process}
    trigger_sources[trigger_id] = source
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        **raw_event,
        "payload_json": encoded,
        "payload_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _upcast_legacy_experience(raw_event: dict[str, object]) -> dict[str, object]:
    """Mark pre-A2 Experience bytes as migration-only, unverified authority."""

    if raw_event.get("event_type") != "ExperienceCommitted":
        return raw_event
    payload_json = raw_event.get("payload_json")
    if not isinstance(payload_json, str):
        return raw_event
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        return raw_event
    experience = payload.get("experience")
    if not isinstance(experience, dict) or "authority_contract_version" in experience:
        return raw_event
    marked = {
        **experience,
        "authority_contract_version": "legacy-unverified",
        "status": "legacy-unverified",
    }
    encoded = json.dumps(
        {**payload, "experience": marked},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        **raw_event,
        "event_type": "LegacyExperienceCommitted",
        "payload_json": encoded,
        "payload_hash": hashlib.sha256(encoded.encode()).hexdigest(),
    }


class SQLiteWorldLedger:
    """Independent crash-consistent persistence adapter for a single World v2 world."""

    def __init__(
        self,
        *,
        path: str | Path,
        world_id: str,
        accepted_batch_issuer: AcceptedLedgerBatchIssuer | None = None,
    ) -> None:
        if not world_id:
            raise ValueError("world_id must not be empty")
        if accepted_batch_issuer is not None and type(accepted_batch_issuer) is not AcceptedLedgerBatchIssuer:
            raise TypeError("accepted batch issuer must use its exact capability type")
        self._world_id = world_id
        self._accepted_batch_issuer = accepted_batch_issuer
        self._thread_lock = RLock()
        connection = sqlite3.connect(
            path, isolation_level=None, timeout=10, check_same_thread=False
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            self._connection = connection
            self._create_schema()
            self._ensure_head()
            legacy_prefix_rebuild = self._head_bundle_requires_prefix_rebuild()
            self._migrate_head_bundle()
            if legacy_prefix_rebuild:
                self._discard_legacy_prefix_proof_cache()
            # The derived prefix tables live in the same SQLite file as the
            # ledger.  They are therefore only an acceleration cache: before
            # they can anchor a process-local historical reader, independently
            # stream-verify immutable events, commits, revisions and head.
            self._verify_cold_ledger_history()
            self._ensure_or_restore_prefix_proof_state()
        except Exception:
            connection.close()
            raise

    def close(self) -> None:
        with self._thread_lock:
            self._connection.close()

    @property
    def world_id(self) -> str:
        return self._world_id

    @property
    def blocks_event_loop(self) -> bool:
        return True

    def __enter__(self) -> SQLiteWorldLedger:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS world_v2_heads (
                world_id TEXT PRIMARY KEY,
                world_revision INTEGER NOT NULL,
                deliberation_revision INTEGER NOT NULL,
                ledger_sequence INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                semantic_hash TEXT NOT NULL,
                reducer_bundle_version TEXT NOT NULL,
                state_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS world_v2_commits (
                world_id TEXT NOT NULL,
                commit_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY (world_id, commit_id),
                FOREIGN KEY (world_id) REFERENCES world_v2_heads(world_id)
            );
            CREATE TABLE IF NOT EXISTS world_v2_events (
                world_id TEXT NOT NULL,
                ledger_sequence INTEGER NOT NULL,
                world_revision INTEGER NOT NULL,
                deliberation_revision INTEGER NOT NULL,
                commit_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                event_json TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                PRIMARY KEY (world_id, ledger_sequence),
                UNIQUE (world_id, event_id),
                UNIQUE (world_id, idempotency_key),
                FOREIGN KEY (world_id, commit_id)
                    REFERENCES world_v2_commits(world_id, commit_id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TABLE IF NOT EXISTS world_v2_legacy_plan_events (
                world_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                source_reducer_bundle TEXT NOT NULL,
                PRIMARY KEY (world_id, event_id),
                FOREIGN KEY (world_id, event_id)
                    REFERENCES world_v2_events(world_id, event_id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TABLE IF NOT EXISTS world_v2_prefix_mmr_nodes (
                world_id TEXT NOT NULL,
                height INTEGER NOT NULL CHECK (height >= 0),
                node_index INTEGER NOT NULL CHECK (node_index >= 0),
                node_hash BLOB NOT NULL CHECK (length(node_hash) = 32),
                PRIMARY KEY (world_id, height, node_index),
                FOREIGN KEY (world_id) REFERENCES world_v2_heads(world_id)
            );
            CREATE TABLE IF NOT EXISTS world_v2_prefix_locator_nodes (
                world_id TEXT NOT NULL,
                depth INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 256),
                prefix_bits BLOB NOT NULL CHECK (length(prefix_bits) = 32),
                node_hash BLOB NOT NULL CHECK (length(node_hash) = 32),
                PRIMARY KEY (world_id, depth, prefix_bits),
                FOREIGN KEY (world_id) REFERENCES world_v2_heads(world_id)
            );
            -- Locator nodes are mutable at the current head, but a pinned
            -- historical checkpoint needs the path that existed at its own
            -- commit boundary.  This is an append-only changed-node journal,
            -- not another authority: its proof must still verify to the
            -- checkpoint's authenticated locator root.
            CREATE TABLE IF NOT EXISTS world_v2_prefix_locator_node_history (
                world_id TEXT NOT NULL,
                ledger_sequence INTEGER NOT NULL CHECK (ledger_sequence > 0),
                depth INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 256),
                prefix_bits BLOB NOT NULL CHECK (length(prefix_bits) = 32),
                node_hash BLOB NOT NULL CHECK (length(node_hash) = 32),
                PRIMARY KEY (world_id, ledger_sequence, depth, prefix_bits),
                FOREIGN KEY (world_id) REFERENCES world_v2_heads(world_id)
            );
            CREATE INDEX IF NOT EXISTS world_v2_prefix_locator_node_history_lookup
                ON world_v2_prefix_locator_node_history
                   (world_id, depth, prefix_bits, ledger_sequence DESC);
            CREATE TABLE IF NOT EXISTS world_v2_prefix_locator_values (
                world_id TEXT NOT NULL,
                locator_key BLOB NOT NULL CHECK (length(locator_key) = 32),
                value_hash BLOB NOT NULL CHECK (length(value_hash) = 32),
                observation_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('ObservationRecorded', 'OperatorObservationRecorded')
                ),
                event_id TEXT NOT NULL,
                ledger_sequence INTEGER NOT NULL,
                world_revision INTEGER NOT NULL,
                deliberation_revision INTEGER NOT NULL,
                event_leaf_index INTEGER NOT NULL,
                event_leaf_hash BLOB NOT NULL CHECK (length(event_leaf_hash) = 32),
                PRIMARY KEY (world_id, locator_key),
                UNIQUE (world_id, event_id),
                FOREIGN KEY (world_id, event_id)
                    REFERENCES world_v2_events(world_id, event_id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TABLE IF NOT EXISTS world_v2_prefix_checkpoints (
                world_id TEXT NOT NULL,
                world_revision INTEGER NOT NULL,
                deliberation_revision INTEGER NOT NULL,
                ledger_sequence INTEGER NOT NULL,
                commit_id TEXT NOT NULL,
                first_ledger_sequence INTEGER NOT NULL,
                last_ledger_sequence INTEGER NOT NULL,
                request_hash TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                ordered_event_ids_hash TEXT NOT NULL,
                locator_root BLOB NOT NULL CHECK (length(locator_root) = 32),
                mmr_leaf_count INTEGER NOT NULL,
                PRIMARY KEY (world_id, world_revision, deliberation_revision, ledger_sequence),
                UNIQUE (world_id, commit_id),
                FOREIGN KEY (world_id, commit_id)
                    REFERENCES world_v2_commits(world_id, commit_id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TABLE IF NOT EXISTS world_v2_prefix_heads (
                world_id TEXT PRIMARY KEY,
                proof_version TEXT NOT NULL,
                mmr_leaf_count INTEGER NOT NULL,
                mmr_root BLOB NOT NULL CHECK (length(mmr_root) = 32),
                locator_root BLOB NOT NULL CHECK (length(locator_root) = 32),
                checkpoint_count INTEGER NOT NULL,
                FOREIGN KEY (world_id) REFERENCES world_v2_heads(world_id)
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(world_v2_heads)")
        }
        if "reducer_bundle_version" not in columns:
            self._connection.execute(
                "ALTER TABLE world_v2_heads ADD COLUMN reducer_bundle_version "
                "TEXT NOT NULL DEFAULT 'world-v2-reducers.1'"
            )
        if "state_hash" not in columns:
            self._connection.execute("ALTER TABLE world_v2_heads ADD COLUMN state_hash TEXT")

    @staticmethod
    def _encode_state(state: ReducerState) -> str:
        return state.model_dump_json()

    def _state_hash(self, state: ReducerState, cursor: ProjectionCursor) -> str:
        encoded = json.dumps(
            {
                "cursor": cursor.model_dump(mode="json"),
                "reducer_bundle_version": REDUCER_BUNDLE_VERSION,
                "state": state.model_dump(mode="json"),
                "world_id": self._world_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _decode_state(value: str) -> ReducerState:
        try:
            return ReducerState.model_validate_json(value)
        except LedgerIntegrityError:
            raise
        except Exception as exc:
            raise LedgerIntegrityError("head state is invalid") from exc

    def _ensure_head(self) -> None:
        initial = make_projection(
            world_id=self._world_id,
            world_revision=0,
            deliberation_revision=0,
            ledger_sequence=0,
            state=ReducerState(),
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO world_v2_heads
                (world_id, world_revision, deliberation_revision, ledger_sequence,
                 state_json, semantic_hash, reducer_bundle_version, state_hash)
            VALUES (?, 0, 0, 0, ?, ?, ?, ?)
            """,
            (
                self._world_id,
                self._encode_state(ReducerState()),
                initial.semantic_hash,
                REDUCER_BUNDLE_VERSION,
                self._state_hash(
                    ReducerState(),
                    ProjectionCursor(
                        world_revision=0,
                        deliberation_revision=0,
                        ledger_sequence=0,
                    ),
                ),
            ),
        )

    def _verify_cold_ledger_history(self) -> None:
        """Verify immutable history from genesis before trusting proof caches.

        The event cursor is streamed in ledger order by ``_replay_locked``;
        commit validation is then streamed in first-event order and bounded by
        the shared commit contract.  No prefix-table row is an input to this
        check, so mutually rewritten derived roots cannot create a verified
        reader capability on process startup.
        """

        with self._thread_lock:
            connection = self._connection
            try:
                connection.execute("BEGIN")
                head = self._project_locked()
                rebuilt = self._replay_locked(
                    target_cursor=None,
                    target_schema_version=CURRENT_SCHEMA_VERSION,
                    reducer_bundle_version=REDUCER_BUNDLE_VERSION,
                )
                if rebuilt != head:
                    raise LedgerIntegrityError("cold replay does not match persisted head")
                head_cursor = ProjectionCursor(
                    world_revision=head.world_revision,
                    deliberation_revision=head.deliberation_revision,
                    ledger_sequence=head.ledger_sequence,
                )
                commit_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM world_v2_commits WHERE world_id = ?",
                        (self._world_id,),
                    ).fetchone()[0]
                )
                previous_last_sequence = 0
                verified_commit_count = 0
                for row in connection.execute(
                    """SELECT commit_id, MIN(ledger_sequence) AS first_sequence,
                              MAX(ledger_sequence) AS last_sequence,
                              COUNT(*) AS event_count
                       FROM world_v2_events WHERE world_id = ?
                       GROUP BY commit_id ORDER BY MIN(ledger_sequence)""",
                    (self._world_id,),
                ):
                    first_sequence = int(row["first_sequence"])
                    last_sequence = int(row["last_sequence"])
                    if (
                        first_sequence != previous_last_sequence + 1
                        or last_sequence - first_sequence + 1 != int(row["event_count"])
                    ):
                        raise LedgerIntegrityError("commit event rows are not contiguous")
                    self._verify_cold_commit_locked(
                        str(row["commit_id"]), expected_cursor=head_cursor
                    )
                    previous_last_sequence = last_sequence
                    verified_commit_count += 1
                if verified_commit_count != commit_count:
                    raise LedgerIntegrityError("prefix proof rebuild found an empty or orphaned commit")
                connection.commit()
            except sqlite3.DatabaseError as exc:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise LedgerIntegrityError("cold ledger verification failed") from exc
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise

    def _verify_cold_commit_locked(
        self, commit_id: str, *, expected_cursor: ProjectionCursor
    ) -> None:
        """Verify a persisted commit without reinterpreting legacy event bytes.

        Current identity and reducer semantics are already checked by the
        genesis replay.  This companion check binds the exact stored envelope
        bytes to its original request/result record.  It intentionally avoids
        applying the *current* event-identity rule to legacy rows whose replay
        path performs a documented upcast before validation.
        """

        commit_row = self._connection.execute(
            """SELECT request_hash, result_json FROM world_v2_commits
               WHERE world_id = ? AND commit_id = ?""",
            (self._world_id, commit_id),
        ).fetchone()
        if commit_row is None:
            raise LedgerIntegrityError("event owning commit is missing")
        rows = tuple(
            self._connection.execute(
                """SELECT * FROM world_v2_events WHERE world_id = ? AND commit_id = ?
                   ORDER BY ledger_sequence""",
                (self._world_id, commit_id),
            )
        )
        if not rows:
            raise LedgerIntegrityError("prefix proof rebuild found an empty or orphaned commit")
        try:
            events: list[WorldEvent] = []
            event_ids: list[str] = []
            legacy_bytes_present = False
            for row in rows:
                event_json = row["event_json"]
                if not isinstance(event_json, str) or not hmac.compare_digest(
                    hashlib.sha256(event_json.encode("utf-8")).hexdigest(),
                    str(row["event_hash"]),
                ):
                    raise LedgerIntegrityError("event envelope hash mismatch")
                raw_event = json.loads(event_json)
                if not isinstance(raw_event, dict):
                    raise LedgerIntegrityError("persisted event is invalid")
                event_id = raw_event.get("event_id")
                event_world_id = raw_event.get("world_id")
                idempotency_key = raw_event.get("idempotency_key")
                if (
                    event_world_id != self._world_id
                    or event_id != row["event_id"]
                    or idempotency_key != row["idempotency_key"]
                    or int(row["ledger_sequence"]) > expected_cursor.ledger_sequence
                    or int(row["world_revision"]) > expected_cursor.world_revision
                    or int(row["deliberation_revision"])
                    > expected_cursor.deliberation_revision
                ):
                    raise LedgerIntegrityError("event envelope does not match its ledger row")
                if raw_event.get("schema_version") == CURRENT_SCHEMA_VERSION:
                    events.append(WorldEvent.model_validate_json(event_json))
                else:
                    # Legacy envelopes are accepted only through the replay
                    # upcast policy above.  Their historical request hash was
                    # not defined over the current canonical event contract.
                    legacy_bytes_present = True
                if raw_event.get("event_type") == "AcceptanceRecorded":
                    payload_json = raw_event.get("payload_json")
                    try:
                        raw_payload = json.loads(payload_json) if isinstance(payload_json, str) else None
                    except (TypeError, json.JSONDecodeError):
                        raw_payload = None
                    if not isinstance(raw_payload, dict) or "manifest_version" not in raw_payload:
                        # The replay path explicitly converts these pre-v18
                        # audit records to inert legacy events.  Their old
                        # request hash cannot be checked using v18 bytes.
                        legacy_bytes_present = True
                if self._connection.execute(
                    """SELECT 1 FROM world_v2_legacy_plan_events
                       WHERE world_id = ? AND event_id = ?""",
                    (self._world_id, str(event_id)),
                ).fetchone() is not None:
                    # v15 ownerless plan lifecycle rows are rewritten only by
                    # the documented migration replay policy.  Their source
                    # request hash binds pre-migration event bytes.
                    legacy_bytes_present = True
                event_ids.append(str(event_id))
            if not legacy_bytes_present and not hmac.compare_digest(
                commit_request_hash(events), str(commit_row["request_hash"])
            ):
                raise LedgerIntegrityError("commit request hash does not match event rows")
            last = rows[-1]
            expected_result = CommitResult(
                world_revision=int(last["world_revision"]),
                deliberation_revision=int(last["deliberation_revision"]),
                ledger_sequence=int(last["ledger_sequence"]),
                event_ids=tuple(event_ids),
            )
            if CommitResult.model_validate_json(commit_row["result_json"]) != expected_result:
                raise LedgerIntegrityError("commit result does not match event rows")
        except LedgerIntegrityError:
            raise
        except Exception as exc:
            raise LedgerIntegrityError("persisted commit is invalid") from exc

    def _ensure_or_restore_prefix_proof_state(self) -> None:
        """Restore derived proof state, or atomically derive it for a legacy ledger.

        The event/commit tables remain the only authority.  The proof tables are
        a mechanically checked, append-only acceleration structure; a partially
        written structure is therefore an integrity failure rather than a cue to
        silently replace evidence.
        """

        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            prefix_head = connection.execute(
                "SELECT * FROM world_v2_prefix_heads WHERE world_id = ?",
                (self._world_id,),
            ).fetchone()
            if (
                prefix_head is not None
                and str(prefix_head["proof_version"]) == _PREVIOUS_PREFIX_PROOF_VERSION
            ):
                # v1 retained only the current locator map, which cannot prove
                # an older checkpoint's root.  The immutable ledger is still
                # authoritative, so atomically rederive the cache with v2's
                # changed-node history instead of accepting an unverifiable
                # historical read path.
                self._discard_prefix_proof_cache_locked()
                prefix_head = None
            elif prefix_head is not None and str(prefix_head["proof_version"]) != _PREFIX_PROOF_VERSION:
                raise LedgerIntegrityError("prefix proof version is unsupported")
            event_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM world_v2_events WHERE world_id = ?",
                    (self._world_id,),
                ).fetchone()[0]
            )
            commit_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM world_v2_commits WHERE world_id = ?",
                    (self._world_id,),
                ).fetchone()[0]
            )
            derived_count = sum(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE world_id = ?",
                        (self._world_id,),
                    ).fetchone()[0]
                )
                for table in (
                    "world_v2_prefix_mmr_nodes",
                    "world_v2_prefix_locator_nodes",
                    "world_v2_prefix_locator_values",
                    "world_v2_prefix_checkpoints",
                )
            )
            if prefix_head is None and derived_count == 0:
                # A pre-v2 cleanup can leave the newly introduced history table
                # behind while removing all v1 cache tables.  It has no root to
                # authenticate it, so discard and derive it again from events.
                connection.execute(
                    "DELETE FROM world_v2_prefix_locator_node_history WHERE world_id = ?",
                    (self._world_id,),
                )
            legacy_head = connection.execute(
                "SELECT reducer_bundle_version FROM world_v2_heads WHERE world_id = ?",
                (self._world_id,),
            ).fetchone()
            # A database created before prefix proofs has a zero/genesis proof
            # head if a test or migration seeded legacy rows after construction.
            # Only that demonstrably pristine cache may be rebuilt; any nonempty
            # mismatch remains fail-closed.
            if (
                prefix_head is not None
                and event_count
                and derived_count == 0
                and int(prefix_head["mmr_leaf_count"]) == 0
                and int(prefix_head["checkpoint_count"]) == 0
                and legacy_head is not None
                and str(legacy_head["reducer_bundle_version"]) != REDUCER_BUNDLE_VERSION
            ):
                connection.execute(
                    "DELETE FROM world_v2_prefix_heads WHERE world_id = ?", (self._world_id,)
                )
                prefix_head = None
            if prefix_head is None:
                if derived_count:
                    raise LedgerIntegrityError("prefix proof state is partial")
                mmr = IncrementalMmrV1()
                locator_map = IncrementalSparseMerkleMapV1()
                if event_count or commit_count:
                    if not event_count or not commit_count:
                        raise LedgerIntegrityError("legacy ledger commit/event rows are inconsistent")
                    self._rebuild_prefix_proof_state_locked(mmr=mmr, locator_map=locator_map)
                self._write_prefix_head_locked(
                    mmr_leaf_count=mmr.leaf_count,
                    mmr_root=mmr.root,
                    locator_root=locator_map.root,
                )
            else:
                self._verify_prefix_proof_state_locked(
                    prefix_head=prefix_head,
                    event_count=event_count,
                    commit_count=commit_count,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _head_bundle_requires_prefix_rebuild(self) -> bool:
        row = self._connection.execute(
            "SELECT reducer_bundle_version FROM world_v2_heads WHERE world_id = ?",
            (self._world_id,),
        ).fetchone()
        if row is None:
            raise LedgerIntegrityError("world head disappeared before prefix migration")
        return str(row["reducer_bundle_version"]) != REDUCER_BUNDLE_VERSION

    def _discard_legacy_prefix_proof_cache(self) -> None:
        """A reducer-bundle migration invalidates any previously derived cache."""

        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._discard_prefix_proof_cache_locked()
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _discard_prefix_proof_cache_locked(self) -> None:
        for table in (
            "world_v2_prefix_mmr_nodes",
            "world_v2_prefix_locator_nodes",
            "world_v2_prefix_locator_node_history",
            "world_v2_prefix_locator_values",
            "world_v2_prefix_checkpoints",
            "world_v2_prefix_heads",
        ):
            self._connection.execute(f"DELETE FROM {table} WHERE world_id = ?", (self._world_id,))

    def _write_prefix_head_locked(
        self,
        *,
        mmr_leaf_count: int,
        mmr_root: bytes,
        locator_root: bytes,
        checkpoint_count: int | None = None,
    ) -> None:
        if checkpoint_count is None:
            checkpoint_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM world_v2_prefix_checkpoints WHERE world_id = ?",
                    (self._world_id,),
                ).fetchone()[0]
            )
        self._connection.execute(
            """INSERT INTO world_v2_prefix_heads
                 (world_id, proof_version, mmr_leaf_count, mmr_root, locator_root, checkpoint_count)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(world_id) DO UPDATE SET
                 proof_version = excluded.proof_version,
                 mmr_leaf_count = excluded.mmr_leaf_count,
                 mmr_root = excluded.mmr_root,
                 locator_root = excluded.locator_root,
                 checkpoint_count = excluded.checkpoint_count""",
            (
                self._world_id,
                _PREFIX_PROOF_VERSION,
                mmr_leaf_count,
                mmr_root,
                locator_root,
                checkpoint_count,
            ),
        )

    def _verify_prefix_proof_state_locked(
        self,
        *,
        prefix_head: sqlite3.Row,
        event_count: int,
        commit_count: int,
    ) -> None:
        """Validate durable prefix metadata without restoring all proof nodes.

        Normal process startup deliberately verifies only the addressed peaks,
        current sparse root, and checkpoint leaves required to bind the cache to
        the immutable ledger.  Proof consumers read the remaining paths on
        demand and verify them against these roots.  Full mutable builders are
        reserved for one-time legacy cache reconstruction.
        """

        try:
            if prefix_head["proof_version"] != _PREFIX_PROOF_VERSION:
                raise LedgerIntegrityError("prefix proof version is unsupported")
            leaf_count = int(prefix_head["mmr_leaf_count"])
            if leaf_count != event_count + commit_count:
                raise LedgerIntegrityError("prefix proof leaf count does not match ledger")
            mmr_root = mmr_root_from_node_lookup_v1(
                leaf_count=leaf_count,
                node_lookup=self._prefix_mmr_node_lookup_locked,
            )
            if not hmac.compare_digest(mmr_root, bytes(prefix_head["mmr_root"])):
                raise LedgerIntegrityError("prefix MMR root does not match persisted head")
            root_row = self._connection.execute(
                """SELECT node_hash FROM world_v2_prefix_locator_nodes
                   WHERE world_id = ? AND depth = 0 AND prefix_bits = ?""",
                (self._world_id, _prefix_bits_blob(0)),
            ).fetchone()
            expected_locator_root = bytes(prefix_head["locator_root"])
            if root_row is None and self._connection.execute(
                """SELECT 1 FROM world_v2_prefix_locator_values
                   WHERE world_id = ? LIMIT 1""",
                (self._world_id,),
            ).fetchone() is not None:
                raise LedgerIntegrityError("prefix locator root node is missing")
            actual_locator_root = expected_locator_root if root_row is None else bytes(root_row["node_hash"])
            if not hmac.compare_digest(actual_locator_root, expected_locator_root):
                raise LedgerIntegrityError("prefix locator root does not match persisted head")
            checkpoint_rows = tuple(
                self._connection.execute(
                    """SELECT * FROM world_v2_prefix_checkpoints
                       WHERE world_id = ? ORDER BY ledger_sequence""",
                    (self._world_id,),
                )
            )
            if len(checkpoint_rows) != commit_count or int(prefix_head["checkpoint_count"]) != commit_count:
                raise LedgerIntegrityError("prefix checkpoint count does not match ledger")
            head = self._connection.execute(
                "SELECT world_revision, deliberation_revision, ledger_sequence FROM world_v2_heads WHERE world_id = ?",
                (self._world_id,),
            ).fetchone()
            for row in checkpoint_rows:
                checkpoint = self._prefix_checkpoint_from_row(row)
                leaf_index = checkpoint.mmr_leaf_count - 1
                if leaf_index < 0 or self._prefix_mmr_node_lookup_locked(0, leaf_index) != checkpoint.digest():
                    raise LedgerIntegrityError("prefix checkpoint MMR leaf is invalid")
            if checkpoint_rows:
                latest = checkpoint_rows[-1]
                if (
                    int(latest["world_revision"]),
                    int(latest["deliberation_revision"]),
                    int(latest["ledger_sequence"]),
                ) != (int(head["world_revision"]), int(head["deliberation_revision"]), int(head["ledger_sequence"])):
                    raise LedgerIntegrityError("prefix checkpoint does not match ledger head")
            elif event_count:
                raise LedgerIntegrityError("ledger events are missing prefix checkpoints")
        except LedgerIntegrityError:
            raise
        except Exception as exc:
            raise LedgerIntegrityError("persisted prefix proof state is invalid") from exc

    def _load_prefix_proof_state_locked(
        self,
        *,
        prefix_head: sqlite3.Row,
        event_count: int,
        commit_count: int,
    ) -> tuple[IncrementalMmrV1, IncrementalSparseMerkleMapV1]:
        try:
            if prefix_head["proof_version"] != _PREFIX_PROOF_VERSION:
                raise LedgerIntegrityError("prefix proof version is unsupported")
            leaf_count = int(prefix_head["mmr_leaf_count"])
            if leaf_count != event_count + commit_count:
                raise LedgerIntegrityError("prefix proof leaf count does not match ledger")
            mmr_nodes = {
                (int(row["height"]), int(row["node_index"])): bytes(row["node_hash"])
                for row in self._connection.execute(
                    """SELECT height, node_index, node_hash FROM world_v2_prefix_mmr_nodes
                       WHERE world_id = ?""",
                    (self._world_id,),
                )
            }
            mmr = IncrementalMmrV1.restore(leaf_count=leaf_count, nodes=mmr_nodes)
            if not hmac.compare_digest(mmr.root, bytes(prefix_head["mmr_root"])):
                raise LedgerIntegrityError("prefix MMR root does not match persisted head")
            locator_nodes = {
                (int(row["depth"]), _prefix_bits_int(bytes(row["prefix_bits"]))): bytes(row["node_hash"])
                for row in self._connection.execute(
                    """SELECT depth, prefix_bits, node_hash FROM world_v2_prefix_locator_nodes
                       WHERE world_id = ?""",
                    (self._world_id,),
                )
            }
            locator_values = {
                bytes(row["locator_key"]): bytes(row["value_hash"])
                for row in self._connection.execute(
                    """SELECT locator_key, value_hash FROM world_v2_prefix_locator_values
                       WHERE world_id = ?""",
                    (self._world_id,),
                )
            }
            locator_map = IncrementalSparseMerkleMapV1.restore(
                nodes=locator_nodes, values=locator_values
            )
            if not hmac.compare_digest(locator_map.root, bytes(prefix_head["locator_root"])):
                raise LedgerIntegrityError("prefix locator root does not match persisted head")
            checkpoint_rows = tuple(
                self._connection.execute(
                    """SELECT * FROM world_v2_prefix_checkpoints
                       WHERE world_id = ? ORDER BY ledger_sequence""",
                    (self._world_id,),
                )
            )
            if len(checkpoint_rows) != commit_count or int(prefix_head["checkpoint_count"]) != commit_count:
                raise LedgerIntegrityError("prefix checkpoint count does not match ledger")
            head = self._connection.execute(
                "SELECT world_revision, deliberation_revision, ledger_sequence FROM world_v2_heads WHERE world_id = ?",
                (self._world_id,),
            ).fetchone()
            for row in checkpoint_rows:
                checkpoint = self._prefix_checkpoint_from_row(row)
                leaf_index = checkpoint.mmr_leaf_count - 1
                if leaf_index < 0 or mmr.nodes.get((0, leaf_index)) != checkpoint.digest():
                    raise LedgerIntegrityError("prefix checkpoint MMR leaf is invalid")
            if checkpoint_rows:
                latest = checkpoint_rows[-1]
                if (
                    int(latest["world_revision"]),
                    int(latest["deliberation_revision"]),
                    int(latest["ledger_sequence"]),
                ) != (int(head["world_revision"]), int(head["deliberation_revision"]), int(head["ledger_sequence"])):
                    raise LedgerIntegrityError("prefix checkpoint does not match ledger head")
            elif event_count:
                raise LedgerIntegrityError("ledger events are missing prefix checkpoints")
            return mmr, locator_map
        except LedgerIntegrityError:
            raise
        except Exception as exc:
            raise LedgerIntegrityError("persisted prefix proof state is invalid") from exc

    def _prefix_checkpoint_from_row(self, row: sqlite3.Row) -> PrefixCheckpointLeafV1:
        return PrefixCheckpointLeafV1(
            world_id=self._world_id,
            commit_id=str(row["commit_id"]),
            first_ledger_sequence=int(row["first_ledger_sequence"]),
            last_ledger_sequence=int(row["last_ledger_sequence"]),
            world_revision=int(row["world_revision"]),
            deliberation_revision=int(row["deliberation_revision"]),
            request_hash=str(row["request_hash"]),
            result_hash=str(row["result_hash"]),
            ordered_event_ids_hash=str(row["ordered_event_ids_hash"]),
            locator_root=bytes(row["locator_root"]).hex(),
            mmr_leaf_count=int(row["mmr_leaf_count"]),
        )

    def _persist_prefix_mmr_append_locked(self, mmr: IncrementalMmrV1, leaf_hash: bytes) -> int:
        leaf_index = mmr.leaf_count
        addresses = [(0, leaf_index)]
        height = 0
        while (leaf_index >> height) & 1:
            addresses.append((height + 1, leaf_index >> (height + 1)))
            height += 1
        appended = mmr.append(leaf_hash)
        if appended != leaf_index:
            raise AssertionError("MMR append index changed unexpectedly")
        self._connection.executemany(
            """INSERT INTO world_v2_prefix_mmr_nodes
                 (world_id, height, node_index, node_hash) VALUES (?, ?, ?, ?)""",
            ((self._world_id, node_height, node_index, mmr.nodes[(node_height, node_index)])
             for node_height, node_index in addresses),
        )
        return leaf_index

    def _persist_prefix_locator_put_locked(
        self,
        locator_map: IncrementalSparseMerkleMapV1,
        *,
        key: bytes,
        value: ObservationLocatorValueV1,
    ) -> None:
        locator_map.put(key=key, value_hash=value.digest())
        key_int = int.from_bytes(key, "big")
        addresses = [
            (256, key_int),
            *((depth, key_int >> (256 - depth)) for depth in range(255, -1, -1)),
        ]
        self._connection.executemany(
            """INSERT INTO world_v2_prefix_locator_nodes
                 (world_id, depth, prefix_bits, node_hash) VALUES (?, ?, ?, ?)
               ON CONFLICT(world_id, depth, prefix_bits) DO UPDATE
                 SET node_hash = excluded.node_hash""",
            ((self._world_id, depth, _prefix_bits_blob(prefix), locator_map.nodes[(depth, prefix)])
             for depth, prefix in addresses),
        )
        self._connection.execute(
            """INSERT INTO world_v2_prefix_locator_values
                 (world_id, locator_key, value_hash, observation_id, event_type, event_id,
                  ledger_sequence, world_revision, deliberation_revision, event_leaf_index,
                  event_leaf_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._world_id, key, value.digest(), value.observation_id, value.event_type,
                value.event_id, value.ledger_sequence, value.world_revision,
                value.deliberation_revision, value.event_leaf_index, value.event_leaf_hash,
            ),
        )

    def _rebuild_prefix_proof_state_locked(
        self, *, mmr: IncrementalMmrV1, locator_map: IncrementalSparseMerkleMapV1
    ) -> None:
        """One-time legacy migration from immutable commits/events, inside a transaction."""

        rows = tuple(
            self._connection.execute(
                """SELECT e.*, c.request_hash, c.result_json
                   FROM world_v2_events AS e JOIN world_v2_commits AS c
                     ON c.world_id = e.world_id AND c.commit_id = e.commit_id
                   WHERE e.world_id = ? ORDER BY e.ledger_sequence""",
                (self._world_id,),
            )
        )
        expected_commit_count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM world_v2_commits WHERE world_id = ?",
                (self._world_id,),
            ).fetchone()[0]
        )
        current_commit: str | None = None
        staged: list[tuple[sqlite3.Row, WorldEvent, bytes, int]] = []
        completed: set[str] = set()
        for row in rows + (None,):
            row_commit = None if row is None else str(row["commit_id"])
            if current_commit is not None and row_commit != current_commit:
                if current_commit in completed or not staged:
                    raise LedgerIntegrityError("legacy commit events are not contiguous")
                completed.add(current_commit)
                self._persist_prefix_commit_locked(
                    mmr=mmr,
                    locator_map=locator_map,
                    commit_id=current_commit,
                    staged=staged,
                    request_hash=str(staged[0][0]["request_hash"]),
                    result=CommitResult.model_validate_json(str(staged[0][0]["result_json"])),
                    strict_metadata=False,
                )
                staged = []
            if row is None:
                continue
            event_json = row["event_json"]
            if not isinstance(event_json, str) or not hmac.compare_digest(
                hashlib.sha256(event_json.encode("utf-8")).hexdigest(), str(row["event_hash"])
            ):
                raise LedgerIntegrityError("legacy event envelope hash mismatch")
            try:
                event = WorldEvent.model_validate_json(event_json)
            except Exception as exc:
                raise LedgerIntegrityError("legacy event is invalid") from exc
            if event.world_id != self._world_id or event.event_id != row["event_id"] or event.idempotency_key != row["idempotency_key"]:
                raise LedgerIntegrityError("legacy event envelope does not match ledger row")
            if current_commit is None:
                current_commit = row_commit
            current_commit = row_commit
            leaf_hash = LedgerLeafV1(
                world_id=self._world_id, ledger_sequence=int(row["ledger_sequence"]),
                world_revision=int(row["world_revision"]), deliberation_revision=int(row["deliberation_revision"]),
                commit_id=row_commit, event_id=event.event_id, idempotency_key=event.idempotency_key,
                event_envelope_hash=hashlib.sha256(event_json.encode("utf-8")).hexdigest(),
            ).digest()
            staged.append((row, event, leaf_hash, mmr.leaf_count))
        if not rows:
            if expected_commit_count:
                raise LedgerIntegrityError("legacy ledger contains an empty commit")
            return
        if len(completed) != expected_commit_count:
            raise LedgerIntegrityError("legacy ledger contains an empty or orphaned commit")
        durable_commit_count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM world_v2_commits WHERE world_id = ?",
                (self._world_id,),
            ).fetchone()[0]
        )
        if len(completed) != durable_commit_count:
            raise LedgerIntegrityError("legacy ledger contains an empty or orphaned commit")

    def _persist_prefix_commit_locked(
        self,
        *,
        mmr: IncrementalMmrV1,
        locator_map: IncrementalSparseMerkleMapV1,
        commit_id: str,
        staged: Sequence[tuple[sqlite3.Row, WorldEvent, bytes, int]],
        request_hash: str,
        result: CommitResult,
        strict_metadata: bool = True,
    ) -> None:
        events = tuple(item[1] for item in staged)
        rebuilt_result = CommitResult(
            world_revision=int(staged[-1][0]["world_revision"]),
            deliberation_revision=int(staged[-1][0]["deliberation_revision"]),
            ledger_sequence=int(staged[-1][0]["ledger_sequence"]),
            event_ids=tuple(event.event_id for event in events),
        )
        if strict_metadata:
            if result != rebuilt_result or request_hash != commit_request_hash(events):
                raise LedgerIntegrityError("prefix commit metadata does not match event rows")
        else:
            # Bundle migration has already accepted/upcast these immutable rows.
            # Older commit records may not use the current canonical shape.
            result = rebuilt_result
            request_hash = commit_request_hash(events)
        changed_locator_addresses: set[tuple[int, int]] = set()
        for row, event, leaf_hash, _old_leaf_index in staged:
            leaf_index = self._persist_prefix_mmr_append_locked(mmr, leaf_hash)
            observation_id = _observation_id(event)
            if observation_id is not None:
                value = ObservationLocatorValueV1(
                    observation_id=observation_id, event_type=event.event_type, event_id=event.event_id,
                    ledger_sequence=int(row["ledger_sequence"]), world_revision=int(row["world_revision"]),
                    deliberation_revision=int(row["deliberation_revision"]), event_leaf_index=leaf_index,
                    event_leaf_hash=leaf_hash,
                )
                self._persist_prefix_locator_put_locked(
                    locator_map,
                    key=observation_locator_key(world_id=self._world_id, event_type=event.event_type, idempotency_key=event.idempotency_key),
                    value=value,
                )
                key_int = int.from_bytes(
                    observation_locator_key(
                        world_id=self._world_id,
                        event_type=event.event_type,
                        idempotency_key=event.idempotency_key,
                    ),
                    "big",
                )
                changed_locator_addresses.update(
                    ((256, key_int),)
                    + tuple(
                        (depth, key_int >> (256 - depth))
                        for depth in range(255, -1, -1)
                    )
                )
        # Store the *final* value of every changed address once for this commit.
        # Multiple observation events in a batch therefore authenticate exactly
        # the locator root carried by its checkpoint.
        if changed_locator_addresses:
            checkpoint_sequence = int(staged[-1][0]["ledger_sequence"])
            self._connection.executemany(
                """INSERT INTO world_v2_prefix_locator_node_history
                     (world_id, ledger_sequence, depth, prefix_bits, node_hash)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (
                        self._world_id,
                        checkpoint_sequence,
                        depth,
                        _prefix_bits_blob(prefix),
                        locator_map.nodes[(depth, prefix)],
                    )
                    for depth, prefix in sorted(changed_locator_addresses)
                ),
            )
        checkpoint = PrefixCheckpointLeafV1(
            world_id=self._world_id, commit_id=commit_id,
            first_ledger_sequence=int(staged[0][0]["ledger_sequence"]),
            last_ledger_sequence=int(staged[-1][0]["ledger_sequence"]),
            world_revision=result.world_revision, deliberation_revision=result.deliberation_revision,
            request_hash=request_hash,
            result_hash=commit_result_hash_v1(world_revision=result.world_revision, deliberation_revision=result.deliberation_revision, ledger_sequence=result.ledger_sequence, event_ids=result.event_ids),
            ordered_event_ids_hash=ordered_event_ids_hash_v1(result.event_ids),
            locator_root=locator_map.root.hex(), mmr_leaf_count=mmr.leaf_count + 1,
        )
        self._persist_prefix_mmr_append_locked(mmr, checkpoint.digest())
        self._connection.execute(
            """INSERT INTO world_v2_prefix_checkpoints
                 (world_id, world_revision, deliberation_revision, ledger_sequence, commit_id,
                  first_ledger_sequence, last_ledger_sequence, request_hash, result_hash,
                  ordered_event_ids_hash, locator_root, mmr_leaf_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._world_id, checkpoint.world_revision, checkpoint.deliberation_revision,
                checkpoint.last_ledger_sequence, checkpoint.commit_id, checkpoint.first_ledger_sequence,
                checkpoint.last_ledger_sequence, checkpoint.request_hash, checkpoint.result_hash,
                checkpoint.ordered_event_ids_hash, bytes.fromhex(checkpoint.locator_root),
                checkpoint.mmr_leaf_count,
            ),
        )
    def _migrate_head_bundle(self) -> None:
        """Atomically rebuild a verified legacy checkpoint from immutable events."""

        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            head = connection.execute(
                "SELECT * FROM world_v2_heads WHERE world_id = ?", (self._world_id,)
            ).fetchone()
            if head is None:
                raise LedgerIntegrityError("world head disappeared during migration")
            installed = str(head["reducer_bundle_version"])
            try:
                world_revision = int(head["world_revision"])
                cursor = ProjectionCursor(
                    world_revision=world_revision,
                    deliberation_revision=int(head["deliberation_revision"]),
                    ledger_sequence=int(head["ledger_sequence"]),
                )
            except Exception as exc:
                raise LedgerIntegrityError("head cursor is invalid") from exc
            persisted_state_hash = head["state_hash"]
            if installed == REDUCER_BUNDLE_VERSION and persisted_state_hash:
                state = self._decode_state(str(head["state_json"]))
                if not hmac.compare_digest(
                    self._state_hash(state, cursor), str(persisted_state_hash)
                ):
                    raise LedgerIntegrityError("head state hash is invalid")
                connection.commit()
                return
            if installed not in {
                # .4 was an uncommitted development bundle.  It never had a
                # durable event protocol, so treating it as migratable would
                # silently reinterpret old projection-only appraisal hashes.
                "world-v2-reducers.1",
                "world-v2-reducers.2",
                "world-v2-reducers.3",
                "world-v2-reducers.5",
                "world-v2-reducers.6",
                "world-v2-reducers.7",
                "world-v2-reducers.8",
                "world-v2-reducers.9",
                "world-v2-reducers.10",
                "world-v2-reducers.11",
                "world-v2-reducers.12",
                "world-v2-reducers.13",
                "world-v2-reducers.14",
                "world-v2-reducers.15",
                "world-v2-reducers.16",
                "world-v2-reducers.17",
                "world-v2-reducers.18",
                "world-v2-reducers.19",
                "world-v2-reducers.20",
                "world-v2-reducers.21",
                "world-v2-reducers.22",
                "world-v2-reducers.23",
                REDUCER_BUNDLE_VERSION,
            }:
                raise LedgerIntegrityError(
                    f"head reducer bundle {installed!r} has no migration path"
                )
            if installed != REDUCER_BUNDLE_VERSION:
                legacy_hash = self._legacy_semantic_hash(
                    state_json=str(head["state_json"]),
                    world_revision=world_revision,
                    reducer_bundle_version=installed,
                )
                if not hmac.compare_digest(legacy_hash, str(head["semantic_hash"])):
                    raise LedgerIntegrityError("legacy head semantic hash is invalid")
                self._mark_legacy_ownerless_plan_events_locked(installed)
            rebuilt = self._replay_locked(
                target_cursor=cursor,
                target_schema_version=CURRENT_SCHEMA_VERSION,
                reducer_bundle_version=REDUCER_BUNDLE_VERSION,
            )
            rebuilt_state = self._state_from_projection(rebuilt)
            updated = connection.execute(
                """UPDATE world_v2_heads
                   SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?,
                       state_hash = ?
                   WHERE world_id = ? AND world_revision = ?
                     AND deliberation_revision = ? AND ledger_sequence = ?
                     AND reducer_bundle_version = ?""",
                (
                    self._encode_state(rebuilt_state),
                    rebuilt.semantic_hash,
                    REDUCER_BUNDLE_VERSION,
                    self._state_hash(rebuilt_state, cursor),
                    self._world_id,
                    cursor.world_revision,
                    cursor.deliberation_revision,
                    cursor.ledger_sequence,
                    installed,
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict("world head changed during bundle migration")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _mark_legacy_ownerless_plan_events_locked(self, source_bundle: str) -> None:
        rows = tuple(self._connection.execute(
            "SELECT event_id, event_json FROM world_v2_events WHERE world_id = ?",
            (self._world_id,),
        ))
        decoded: list[tuple[str, dict[str, object], dict[str, object]]] = []
        ownerless_plan_ids: set[str] = set()
        for row in rows:
            try:
                raw_event = json.loads(str(row["event_json"]))
                payload = json.loads(str(raw_event.get("payload_json", "")))
            except Exception:
                continue
            if not isinstance(raw_event, dict) or not isinstance(payload, dict):
                continue
            decoded.append((str(row["event_id"]), raw_event, payload))
            plan = payload.get("plan")
            if (
                raw_event.get("event_type") == "ActivityPlanned"
                and isinstance(plan, dict)
                and "owner_actor_ref" not in plan
            ):
                plan_id = plan.get("plan_id")
                if isinstance(plan_id, str):
                    ownerless_plan_ids.add(plan_id)
        lifecycle_types = {
            "ActivityStarted",
            "ActivityPaused",
            "ActivityResumed",
            "ActivityCompleted",
            "ActivityAbandoned",
        }
        for event_id, raw_event, payload in decoded:
            plan = payload.get("plan")
            is_ownerless_create = (
                raw_event.get("event_type") == "ActivityPlanned"
                and isinstance(plan, dict)
                and plan.get("plan_id") in ownerless_plan_ids
            )
            is_ownerless_transition = (
                raw_event.get("event_type") in lifecycle_types
                and payload.get("plan_id") in ownerless_plan_ids
            )
            if is_ownerless_create or is_ownerless_transition:
                self._connection.execute(
                    """INSERT OR IGNORE INTO world_v2_legacy_plan_events
                       (world_id, event_id, source_reducer_bundle) VALUES (?, ?, ?)""",
                    (self._world_id, event_id, source_bundle),
                )

    def _legacy_semantic_hash(
        self,
        *,
        state_json: str,
        world_revision: int,
        reducer_bundle_version: str,
    ) -> str:
        try:
            raw_state = json.loads(state_json)
            if not isinstance(raw_state, dict):
                raise ValueError("legacy state is not an object")
            raw_state = dict(raw_state)
            injected_v24_keys = tuple(
                sorted(
                    key
                    for key in _V24_ONLY_STATE_KEYS.intersection(raw_state)
                    if raw_state.get(key) not in (None, [], {})
                )
            )
            if injected_v24_keys and reducer_bundle_version != REDUCER_BUNDLE_VERSION:
                raise ValueError(
                    f"legacy head cannot claim v24 expression fields {injected_v24_keys!r}"
                )
            injected_v20_keys = tuple(
                sorted(
                    key
                    for key in _V20_ONLY_STATE_KEYS.intersection(raw_state)
                    if raw_state.get(key) not in (None, [], {})
                )
            )
            if injected_v20_keys and reducer_bundle_version not in {
                "world-v2-reducers.20",
                "world-v2-reducers.21",
                "world-v2-reducers.22",
                "world-v2-reducers.23",
            }:
                raise ValueError(
                    f"legacy head cannot claim v20 reply fields {injected_v20_keys!r}"
                )
            injected_v19_keys = tuple(
                sorted(
                    key
                    for key in _V19_ONLY_STATE_KEYS.intersection(raw_state)
                    if raw_state.get(key) not in (None, [], {})
                )
            )
            if injected_v19_keys and reducer_bundle_version not in {
                "world-v2-reducers.19",
                "world-v2-reducers.20",
                "world-v2-reducers.21",
                "world-v2-reducers.22",
                "world-v2-reducers.23",
            }:
                raise ValueError(
                    f"legacy head cannot claim v19 Fact fields {injected_v19_keys!r}"
                )
            injected_v18_keys = tuple(
                key
                for key in _V18_ONLY_STATE_KEYS.intersection(raw_state)
                if raw_state.get(key) not in (None, [], {})
            )
            if injected_v18_keys and reducer_bundle_version != "world-v2-reducers.18":
                raise ValueError("legacy head cannot claim v18 manifest fields")
            injected_v17_keys = tuple(
                sorted(
                    key
                    for key in _V17_ONLY_STATE_KEYS.intersection(raw_state)
                    if raw_state.get(key) not in (None, [], {})
                )
            )
            if injected_v17_keys and reducer_bundle_version not in {
                "world-v2-reducers.17",
                "world-v2-reducers.18",
                "world-v2-reducers.19",
                "world-v2-reducers.20",
                "world-v2-reducers.21",
                "world-v2-reducers.22",
                "world-v2-reducers.23",
            }:
                raise ValueError(
                    f"legacy head cannot claim v17 audit fields {injected_v17_keys!r}"
                )
            injected_v16_keys = tuple(sorted(_V16_ONLY_STATE_KEYS.intersection(raw_state)))
            if injected_v16_keys and reducer_bundle_version not in {
                "world-v2-reducers.16",
                "world-v2-reducers.17",
                "world-v2-reducers.18",
                "world-v2-reducers.19",
                "world-v2-reducers.20",
                "world-v2-reducers.21",
                "world-v2-reducers.22",
                "world-v2-reducers.23",
            }:
                raise ValueError(
                    f"legacy head cannot claim v16 authority fields {injected_v16_keys!r}"
                )
            actor_transitions = raw_state.get("actor_authority_transitions", [])
            actor_binding_keys = {
                "accepted_event_ref",
                "accepted_world_revision",
                "accepted_payload_hash",
            }
            if (
                reducer_bundle_version
                not in {
                    "world-v2-reducers.16",
                    "world-v2-reducers.17",
                    "world-v2-reducers.18",
                    "world-v2-reducers.19",
                    "world-v2-reducers.20",
                    "world-v2-reducers.21",
                "world-v2-reducers.22",
                "world-v2-reducers.23",
                }
                and isinstance(actor_transitions, list)
                and any(
                isinstance(transition, dict)
                and actor_binding_keys.intersection(transition)
                for transition in actor_transitions
                )
            ):
                raise ValueError(
                    "legacy ActorAuthority transition cannot claim a v16 event binding"
                )
            plans = raw_state.get("plans", [])
            plan_authority_keys = {"owner_actor_ref", "authority_origin"}
            if (
                reducer_bundle_version
                not in {
                    "world-v2-reducers.16",
                    "world-v2-reducers.17",
                    "world-v2-reducers.18",
                    "world-v2-reducers.19",
                    "world-v2-reducers.20",
                    "world-v2-reducers.21",
                    "world-v2-reducers.22",
                    "world-v2-reducers.23",
                }
                and isinstance(plans, list)
                and any(
                    isinstance(plan, dict) and plan_authority_keys.intersection(plan)
                    for plan in plans
                )
            ):
                raise ValueError("legacy Plan cannot claim v16 owner authority")
            occurrences = raw_state.get("world_occurrences", [])
            if (
                reducer_bundle_version
                not in {
                    "world-v2-reducers.16",
                    "world-v2-reducers.17",
                    "world-v2-reducers.18",
                    "world-v2-reducers.19",
                    "world-v2-reducers.20",
                    "world-v2-reducers.21",
                    "world-v2-reducers.22",
                    "world-v2-reducers.23",
                }
                and isinstance(occurrences, list)
                and any(
                    isinstance(occurrence, dict)
                    and "settled_outcome_ref" in occurrence
                    for occurrence in occurrences
                )
            ):
                raise ValueError(
                    "legacy world occurrence cannot claim a v16 settled outcome"
                )
            experiences = raw_state.get("experiences", [])
            if isinstance(experiences, list):
                raw_state["experiences"] = [
                    {
                        **experience,
                        "authority_contract_version": "legacy-unverified",
                        "status": "legacy-unverified",
                    }
                    if isinstance(experience, dict)
                    and "authority_contract_version" not in experience
                    else experience
                    for experience in experiences
                ]
            actions = raw_state.get("actions", [])
            terminal = {"delivered", "failed", "unknown", "cancelled", "expired"}
            raw_state["pending_actions"] = [
                action
                for action in actions
                if isinstance(action, dict) and action.get("state") not in terminal
            ]
            state = ReducerState.model_validate_json(
                json.dumps(raw_state, ensure_ascii=False, separators=(",", ":")),
                context={"source_reducer_bundle": reducer_bundle_version},
            )
        except Exception as exc:
            raise LedgerIntegrityError("legacy head state is invalid") from exc
        payload = state.semantic_payload(
            world_id=self._world_id,
            world_revision=world_revision,
            reducer_bundle_version=reducer_bundle_version,
        )
        if reducer_bundle_version == "world-v2-reducers.1":
            payload.pop("pending_actions", None)
        if reducer_bundle_version in {
            "world-v2-reducers.1",
            "world-v2-reducers.2",
            "world-v2-reducers.3",
        }:
            payload.pop("message_observations", None)
            payload.pop("operator_observations", None)
        if reducer_bundle_version in {"world-v2-reducers.1", "world-v2-reducers.2"}:
            for key in (
                "npcs",
                "plans",
                "world_occurrences",
                "outcome_observations",
                "experiences",
                "committed_world_event_refs",
            ):
                payload.pop(key, None)
        if reducer_bundle_version == "world-v2-reducers.3":
            for ref in payload.get("committed_world_event_refs", ()):
                ref.pop("continuation_refs", None)
        if reducer_bundle_version in {
            "world-v2-reducers.1",
            "world-v2-reducers.2",
            "world-v2-reducers.3",
            "world-v2-reducers.5",
        }:
            payload.pop("affect_baselines", None)
            payload.pop("affect_episodes", None)
        if reducer_bundle_version in {
            "world-v2-reducers.1",
            "world-v2-reducers.2",
            "world-v2-reducers.3",
            "world-v2-reducers.5",
            "world-v2-reducers.6",
        }:
            payload.pop("relationship_signals", None)
            payload.pop("relationship_adjustments", None)
            payload.pop("relationship_states", None)
            payload.pop("boundaries", None)
        if reducer_bundle_version in {
            "world-v2-reducers.1",
            "world-v2-reducers.2",
            "world-v2-reducers.3",
            "world-v2-reducers.5",
            "world-v2-reducers.6",
            "world-v2-reducers.7",
        }:
            payload.pop("actor_authorities", None)
            payload.pop("actor_authority_transitions", None)
            payload.pop("consumed_actor_root_nonces", None)
        if reducer_bundle_version in {
            "world-v2-reducers.1",
            "world-v2-reducers.2",
            "world-v2-reducers.3",
            "world-v2-reducers.5",
            "world-v2-reducers.6",
            "world-v2-reducers.7",
            "world-v2-reducers.8",
        }:
            payload.pop("capability_grants", None)
            payload.pop("capability_transitions", None)
            payload.pop("consent_grants", None)
            payload.pop("consent_transitions", None)
            payload.pop("privacy_policies", None)
            payload.pop("privacy_transitions", None)
            payload.pop("consumed_authorization_root_nonces", None)
            payload.pop("consumed_authorization_challenge_ids", None)
            payload.pop("consumed_authorization_source_ids", None)
        if reducer_bundle_version in {
            "world-v2-reducers.1",
            "world-v2-reducers.2",
            "world-v2-reducers.3",
        }:
            payload.pop("appraisals", None)
        if reducer_bundle_version in {
            "world-v2-reducers.1",
            "world-v2-reducers.2",
            "world-v2-reducers.3",
            "world-v2-reducers.5",
            "world-v2-reducers.6",
            "world-v2-reducers.7",
            "world-v2-reducers.8",
            "world-v2-reducers.9",
        }:
            payload.pop("threads", None)
            payload.pop("thread_transitions", None)
        if reducer_bundle_version not in {
            "world-v2-reducers.11",
            "world-v2-reducers.12",
            "world-v2-reducers.13",
            "world-v2-reducers.14",
            "world-v2-reducers.15",
            "world-v2-reducers.16",
            "world-v2-reducers.17",
            "world-v2-reducers.18",
            "world-v2-reducers.19",
            "world-v2-reducers.20",
            "world-v2-reducers.21",
                    "world-v2-reducers.22",
                    "world-v2-reducers.23",
            REDUCER_BUNDLE_VERSION,
        }:
            payload.pop("commitments", None)
            payload.pop("commitment_transitions", None)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _state_from_projection(projection: LedgerProjection) -> ReducerState:
        return ReducerState(
            actor_authorities=projection.actor_authorities,
            actor_authority_transitions=projection.actor_authority_transitions,
            consumed_actor_root_nonces=projection.consumed_actor_root_nonces,
            capability_grants=projection.capability_grants,
            capability_transitions=projection.capability_transitions,
            consent_grants=projection.consent_grants,
            consent_transitions=projection.consent_transitions,
            privacy_policies=projection.privacy_policies,
            privacy_transitions=projection.privacy_transitions,
            consumed_authorization_root_nonces=projection.consumed_authorization_root_nonces,
            consumed_authorization_challenge_ids=projection.consumed_authorization_challenge_ids,
            consumed_authorization_source_ids=projection.consumed_authorization_source_ids,
            observation_refs=projection.observation_refs,
            message_observations=projection.message_observations,
            operator_observations=projection.operator_observations,
            committed_world_event_refs=projection.committed_world_event_refs,
            clock_transition_history=projection.clock_transition_history,
            goals=projection.goals,
            goal_transitions=projection.goal_transitions,
            goal_proposals=projection.goal_proposals,
            goal_proposal_ids=projection.goal_proposal_ids,
            locations=projection.locations,
            location_transitions=projection.location_transitions,
            location_proposals=projection.location_proposals,
            location_proposal_ids=projection.location_proposal_ids,
            resources=projection.resources,
            resource_transitions=projection.resource_transitions,
            resource_proposals=projection.resource_proposals,
            resource_proposal_ids=projection.resource_proposal_ids,
            attentions=projection.attentions,
            attention_transitions=projection.attention_transitions,
            attention_proposals=projection.attention_proposals,
            attention_proposal_ids=projection.attention_proposal_ids,
            logical_time=projection.logical_time,
            actions=projection.actions,
            pending_actions=projection.pending_actions,
            budget_accounts=projection.budget_accounts,
            budget_reservations=projection.budget_reservations,
            trigger_processes=projection.trigger_processes,
            pending_external_observations=projection.pending_external_observations,
            execution_receipts=projection.execution_receipts,
            budget_settlements=projection.budget_settlements,
            reconciliations=projection.reconciliations,
            completed_trigger_ids=projection.completed_trigger_ids,
            npcs=projection.npcs,
            plans=projection.plans,
            world_occurrences=projection.world_occurrences,
            outcome_observations=projection.outcome_observations,
            experiences=projection.experiences,
            experience_transitions=projection.experience_transitions,
            experience_proposals=projection.experience_proposals,
            experience_proposal_ids=projection.experience_proposal_ids,
            memory_candidates=projection.memory_candidates,
            memory_candidate_transitions=projection.memory_candidate_transitions,
            memory_candidate_proposals=projection.memory_candidate_proposals,
            memory_candidate_proposal_ids=projection.memory_candidate_proposal_ids,
            character_core=projection.character_core,
            character_core_transitions=projection.character_core_transitions,
            character_core_proposals=projection.character_core_proposals,
            character_core_proposal_ids=projection.character_core_proposal_ids,
            appraisals=projection.appraisals,
            affect_baselines=projection.affect_baselines,
            affect_episodes=projection.affect_episodes,
            appraisal_proposals=projection.appraisal_proposals,
            appraisal_proposal_ids=projection.appraisal_proposal_ids,
            affect_proposals=projection.affect_proposals,
            affect_proposal_ids=projection.affect_proposal_ids,
            relationship_signals=projection.relationship_signals,
            relationship_adjustments=projection.relationship_adjustments,
            relationship_states=projection.relationship_states,
            boundaries=projection.boundaries,
            relationship_proposals=projection.relationship_proposals,
            relationship_proposal_ids=projection.relationship_proposal_ids,
            threads=projection.threads,
            thread_transitions=projection.thread_transitions,
            thread_proposals=projection.thread_proposals,
            thread_proposal_ids=projection.thread_proposal_ids,
            commitments=projection.commitments,
            commitment_transitions=projection.commitment_transitions,
            commitment_proposals=projection.commitment_proposals,
            commitment_proposal_ids=projection.commitment_proposal_ids,
            facts=projection.facts,
            fact_transitions=projection.fact_transitions,
            fact_proposals=projection.fact_proposals,
            fact_proposal_ids=projection.fact_proposal_ids,
            proposal_ids=projection.proposal_ids,
            proposal_revisions=projection.proposal_revisions,
            model_result_audits=projection.model_result_audits,
            proposal_audits=projection.proposal_audits,
            acceptance_manifests_v2=projection.acceptance_manifests_v2,
            fact_commit_proposal_audits_v2=projection.fact_commit_proposal_audits_v2,
            acceptance_manifests_v3=projection.acceptance_manifests_v3,
            minimal_reply_manifests=projection.minimal_reply_manifests,
            expression_plan_manifests=projection.expression_plan_manifests,
            stored_message_payloads=projection.stored_message_payloads,
            expression_plans=projection.expression_plans,
            expression_beats=projection.expression_beats,
            acceptance_decisions=projection.acceptance_decisions,
            outcome_proposals=projection.outcome_proposals,
        )

    def commit(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_world_revision: int,
        expected_deliberation_revision: int,
        commit_id: str | None = None,
    ) -> CommitResult:
        events = _preflight_commit_events(events)
        with self._thread_lock:
            return self._commit_locked(
                events,
                expected_world_revision=expected_world_revision,
                expected_deliberation_revision=expected_deliberation_revision,
                commit_id=commit_id,
            )

    def commit_at_cursor(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_cursor: ProjectionCursor,
        commit_id: str | None = None,
    ) -> CommitResult:
        events = _preflight_commit_events(events)
        with self._thread_lock:
            return self._commit_locked(
                events,
                expected_world_revision=expected_cursor.world_revision,
                expected_deliberation_revision=expected_cursor.deliberation_revision,
                expected_ledger_sequence=expected_cursor.ledger_sequence,
                commit_id=commit_id,
            )

    def commit_accepted(
        self,
        batch: AcceptedLedgerBatchHandle,
        *,
        expected_cursor: ProjectionCursor,
    ) -> CommitResult:
        issuer = self._accepted_batch_issuer
        if issuer is None:
            raise ValueError("accepted_manifest.recorder_capability_required")
        events, commit_id = issuer.verify(
            handle=batch, world_id=self._world_id, expected_cursor=expected_cursor
        )
        events = _preflight_commit_events(events)
        with self._thread_lock:
            return self._commit_locked(
                events,
                expected_world_revision=expected_cursor.world_revision,
                expected_deliberation_revision=expected_cursor.deliberation_revision,
                expected_ledger_sequence=expected_cursor.ledger_sequence,
                accepted_manifest_v3_authorized=True,
                commit_id=commit_id,
            )

    def _commit_locked(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_world_revision: int,
        expected_deliberation_revision: int,
        expected_ledger_sequence: int | None = None,
        accepted_manifest_v3_authorized: bool = False,
        commit_id: str | None = None,
    ) -> CommitResult:
        if not events:
            raise ValueError("commit requires at least one event")
        commit_id = commit_id or derived_commit_id(events)
        if not commit_id:
            raise ValueError("commit_id must not be empty")
        request_hash = commit_request_hash(events)
        event_ids = [event.event_id for event in events]
        idempotency_keys = [event.idempotency_key for event in events]
        if len(set(event_ids)) != len(event_ids):
            raise IdempotencyConflict("duplicate event_id inside one commit")
        if len(set(idempotency_keys)) != len(idempotency_keys):
            raise IdempotencyConflict("duplicate idempotency key inside one commit")
        if not accepted_manifest_v3_authorized:
            reject_accepted_manifest_v3_without_recorder(events)
        for event in events:
            if event.world_id != self._world_id:
                raise ValueError("event belongs to another world")
            validate_event_identity(event)

        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                """SELECT commit_id FROM world_v2_commits
                   WHERE world_id = ? AND commit_id = ?""",
                (self._world_id, commit_id),
            ).fetchone()
            if existing is not None:
                _, result, persisted_request_hash = self._verified_commit_locked(commit_id)
                if not hmac.compare_digest(persisted_request_hash, request_hash):
                    raise IdempotencyConflict(f"commit_id {commit_id!r} has different content")
                connection.commit()
                return result

            validate_commit_batch(
                events,
                expected_world_revision=expected_world_revision,
                accepted_manifest_v3_authorized=accepted_manifest_v3_authorized,
            )

            placeholders = ",".join("?" for _ in events)
            duplicate = connection.execute(
                f"""SELECT event_id, idempotency_key FROM world_v2_events
                    WHERE world_id = ? AND
                    (event_id IN ({placeholders}) OR idempotency_key IN ({placeholders}))
                    LIMIT 1""",
                (self._world_id, *event_ids, *idempotency_keys),
            ).fetchone()
            if duplicate is not None:
                raise IdempotencyConflict("event identity already exists under another commit")

            head = connection.execute(
                "SELECT * FROM world_v2_heads WHERE world_id = ?", (self._world_id,)
            ).fetchone()
            if head is None:
                raise LedgerIntegrityError("world head disappeared")
            definitions = [event_definition(event.event_type) for event in events]
            revision_classes = {definition.revision_class for definition in definitions}
            if (
                RevisionClass.WORLD in revision_classes
                and expected_world_revision != head["world_revision"]
            ):
                raise ConcurrencyConflict("stale world revision")
            if (
                RevisionClass.DELIBERATION in revision_classes
                and expected_deliberation_revision != head["deliberation_revision"]
            ):
                raise ConcurrencyConflict("stale deliberation revision")
            if expected_ledger_sequence is not None and (
                expected_world_revision != head["world_revision"]
                or expected_deliberation_revision != head["deliberation_revision"]
                or expected_ledger_sequence != head["ledger_sequence"]
            ):
                raise ConcurrencyConflict("stale projection cursor")

            world_revision = int(head["world_revision"])
            deliberation_revision = int(head["deliberation_revision"])
            ledger_sequence = int(head["ledger_sequence"])
            state = self._decode_state(head["state_json"])
            staged: list[tuple[WorldEvent, int, int, int, str, str]] = []
            for event, definition in zip(events, definitions, strict=True):
                ledger_sequence += 1
                if definition.revision_class is RevisionClass.WORLD:
                    world_revision += 1
                else:
                    deliberation_revision += 1
                state = reduce_event(state, event)
                event_json = canonical_event_json(event)
                event_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
                staged.append(
                    (
                        event,
                        ledger_sequence,
                        world_revision,
                        deliberation_revision,
                        event_json,
                        event_hash,
                    )
                )

            result = CommitResult(
                world_revision=world_revision,
                deliberation_revision=deliberation_revision,
                ledger_sequence=ledger_sequence,
                event_ids=tuple(event_ids),
            )
            connection.execute(
                """INSERT INTO world_v2_commits
                   (world_id, commit_id, request_hash, result_json) VALUES (?, ?, ?, ?)""",
                (
                    self._world_id,
                    commit_id,
                    request_hash,
                    result.model_dump_json(),
                ),
            )
            for event, sequence, world_rev, deliberation_rev, event_json, event_hash in staged:
                connection.execute(
                    """INSERT INTO world_v2_events
                       (world_id, ledger_sequence, world_revision, deliberation_revision,
                        commit_id, event_id, idempotency_key, event_json, event_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self._world_id,
                        sequence,
                        world_rev,
                        deliberation_rev,
                        commit_id,
                        event.event_id,
                        event.idempotency_key,
                        event_json,
                        event_hash,
                    ),
                )
            # Derived proof tables are updated in this same transaction using
            # addressed SQLite reads/writes.  No mutable prefix builder is
            # retained by the normal write or rollback path.
            self._persist_prefix_new_commit_locked(
                events=events,
                commit_id=commit_id,
                request_hash=request_hash,
                result=result,
            )
            projection = make_projection(
                world_id=self._world_id,
                world_revision=world_revision,
                deliberation_revision=deliberation_revision,
                ledger_sequence=ledger_sequence,
                state=state,
            )
            updated = connection.execute(
                """UPDATE world_v2_heads
                   SET world_revision = ?, deliberation_revision = ?, ledger_sequence = ?,
                       state_json = ?, semantic_hash = ?, reducer_bundle_version = ?,
                       state_hash = ?
                   WHERE world_id = ? AND world_revision = ?
                     AND deliberation_revision = ? AND ledger_sequence = ?""",
                (
                    world_revision,
                    deliberation_revision,
                    ledger_sequence,
                    self._encode_state(state),
                    projection.semantic_hash,
                    REDUCER_BUNDLE_VERSION,
                    self._state_hash(
                        state,
                        ProjectionCursor(
                            world_revision=world_revision,
                            deliberation_revision=deliberation_revision,
                            ledger_sequence=ledger_sequence,
                        ),
                    ),
                    self._world_id,
                    head["world_revision"],
                    head["deliberation_revision"],
                    head["ledger_sequence"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict("world head changed during commit")
            connection.commit()
            return result
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise IdempotencyConflict("ledger identity already exists") from exc
        except Exception:
            connection.rollback()
            raise

    def _persist_prefix_new_commit_locked(
        self,
        *,
        events: Sequence[WorldEvent],
        commit_id: str,
        request_hash: str,
        result: CommitResult,
    ) -> None:
        """Persist one current commit through durable, addressed proof updates.

        Unlike legacy cache reconstruction this method intentionally does not
        instantiate ``IncrementalMmrV1`` or ``IncrementalSparseMerkleMapV1``.
        Every MMR append reads only its carry chain; every locator update reads
        only its 256 sibling nodes.  SQLite rolls all derived rows back with
        the immutable commit if a later write fails.
        """
        rows = tuple(
            self._connection.execute(
                """SELECT * FROM world_v2_events WHERE world_id = ? AND commit_id = ?
                   ORDER BY ledger_sequence""",
                (self._world_id, commit_id),
            )
        )
        if len(rows) != len(events):
            raise LedgerIntegrityError("prefix commit rows are unavailable")
        prefix_head = self._connection.execute(
            "SELECT * FROM world_v2_prefix_heads WHERE world_id = ?", (self._world_id,)
        ).fetchone()
        if prefix_head is None or str(prefix_head["proof_version"]) != _PREFIX_PROOF_VERSION:
            raise LedgerIntegrityError("prefix proof head is unavailable")
        leaf_count = int(prefix_head["mmr_leaf_count"])
        checkpoint_count = int(prefix_head["checkpoint_count"])
        locator_root = bytes(prefix_head["locator_root"])
        if len(locator_root) != 32:
            raise LedgerIntegrityError("prefix locator root is invalid")
        if not rows or leaf_count != int(rows[0]["ledger_sequence"]) - 1 + checkpoint_count:
            raise LedgerIntegrityError("prefix proof head is not aligned with commit rows")
        events_by_id = {event.event_id: event for event in events}
        staged: list[tuple[sqlite3.Row, WorldEvent, bytes, int]] = []
        for row in rows:
            event = events_by_id.get(str(row["event_id"]))
            if event is None:
                raise LedgerIntegrityError("prefix commit rows do not match staged events")
            event_json = canonical_event_json(event)
            event_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
            if event_json != row["event_json"] or not hmac.compare_digest(event_hash, str(row["event_hash"])):
                raise LedgerIntegrityError("prefix commit event envelope mismatch")
            leaf_hash = LedgerLeafV1(
                world_id=self._world_id,
                ledger_sequence=int(row["ledger_sequence"]),
                world_revision=int(row["world_revision"]),
                deliberation_revision=int(row["deliberation_revision"]),
                commit_id=commit_id,
                event_id=event.event_id,
                idempotency_key=event.idempotency_key,
                event_envelope_hash=event_hash,
            ).digest()
            plan = mmr_append_plan_from_node_lookup_v1(
                leaf_count=leaf_count,
                leaf_hash=leaf_hash,
                node_lookup=self._prefix_mmr_node_lookup_locked,
            )
            self._persist_mmr_append_plan_locked(plan)
            staged.append((row, event, leaf_hash, plan.leaf_index))
            leaf_count = plan.leaf_count

        changed_locator_nodes: dict[tuple[int, int], bytes] = {}
        for row, event, leaf_hash, leaf_index in staged:
            observation_id = _observation_id(event)
            if observation_id is None:
                continue
            key = observation_locator_key(
                world_id=self._world_id,
                event_type=event.event_type,
                idempotency_key=event.idempotency_key,
            )
            if self._connection.execute(
                """SELECT 1 FROM world_v2_prefix_locator_values
                   WHERE world_id = ? AND locator_key = ?""",
                (self._world_id, key),
            ).fetchone() is not None:
                raise LedgerIntegrityError("prefix locator key is not append-only")
            value = ObservationLocatorValueV1(
                observation_id=observation_id,
                event_type=event.event_type,
                event_id=event.event_id,
                ledger_sequence=int(row["ledger_sequence"]),
                world_revision=int(row["world_revision"]),
                deliberation_revision=int(row["deliberation_revision"]),
                event_leaf_index=leaf_index,
                event_leaf_hash=leaf_hash,
            )
            plan = sparse_merkle_put_from_node_lookup_v1(
                key=key,
                value_hash=value.digest(),
                node_lookup=self._prefix_locator_node_lookup_locked,
            )
            SparseMerkleProofV1(
                key=key, value_hash=None, siblings=plan.prior_siblings
            ).verify_nonmembership(expected_root=locator_root, expected_key=key)
            self._persist_locator_put_plan_locked(key=key, value=value, node_updates=plan.node_updates)
            changed_locator_nodes.update(
                { (depth, prefix): node_hash for depth, prefix, node_hash in plan.node_updates }
            )
            locator_root = plan.root

        if changed_locator_nodes:
            checkpoint_sequence = int(staged[-1][0]["ledger_sequence"])
            self._connection.executemany(
                """INSERT INTO world_v2_prefix_locator_node_history
                     (world_id, ledger_sequence, depth, prefix_bits, node_hash)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (self._world_id, checkpoint_sequence, depth, _prefix_bits_blob(prefix), node_hash)
                    for (depth, prefix), node_hash in sorted(changed_locator_nodes.items())
                ),
            )

        checkpoint = PrefixCheckpointLeafV1(
            world_id=self._world_id,
            commit_id=commit_id,
            first_ledger_sequence=int(staged[0][0]["ledger_sequence"]),
            last_ledger_sequence=int(staged[-1][0]["ledger_sequence"]),
            world_revision=result.world_revision,
            deliberation_revision=result.deliberation_revision,
            request_hash=request_hash,
            result_hash=commit_result_hash_v1(
                world_revision=result.world_revision,
                deliberation_revision=result.deliberation_revision,
                ledger_sequence=result.ledger_sequence,
                event_ids=result.event_ids,
            ),
            ordered_event_ids_hash=ordered_event_ids_hash_v1(result.event_ids),
            locator_root=locator_root.hex(),
            mmr_leaf_count=leaf_count + 1,
        )
        checkpoint_plan = mmr_append_plan_from_node_lookup_v1(
            leaf_count=leaf_count,
            leaf_hash=checkpoint.digest(),
            node_lookup=self._prefix_mmr_node_lookup_locked,
        )
        self._persist_mmr_append_plan_locked(checkpoint_plan)
        self._connection.execute(
            """INSERT INTO world_v2_prefix_checkpoints
                 (world_id, world_revision, deliberation_revision, ledger_sequence, commit_id,
                  first_ledger_sequence, last_ledger_sequence, request_hash, result_hash,
                  ordered_event_ids_hash, locator_root, mmr_leaf_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._world_id, checkpoint.world_revision, checkpoint.deliberation_revision,
                checkpoint.last_ledger_sequence, checkpoint.commit_id,
                checkpoint.first_ledger_sequence, checkpoint.last_ledger_sequence,
                checkpoint.request_hash, checkpoint.result_hash,
                checkpoint.ordered_event_ids_hash, bytes.fromhex(checkpoint.locator_root),
                checkpoint.mmr_leaf_count,
            ),
        )
        self._write_prefix_head_locked(
            mmr_leaf_count=checkpoint_plan.leaf_count,
            mmr_root=checkpoint_plan.root,
            locator_root=locator_root,
            checkpoint_count=checkpoint_count + 1,
        )

    def _persist_mmr_append_plan_locked(self, plan: MmrAppendPlanV1) -> None:
        """Persist a validated pure-core MMR append plan in the active transaction."""

        self._connection.executemany(
            """INSERT INTO world_v2_prefix_mmr_nodes
                 (world_id, height, node_index, node_hash) VALUES (?, ?, ?, ?)""",
            ((self._world_id, height, node_index, node_hash)
             for height, node_index, node_hash in plan.node_writes),
        )

    def _persist_locator_put_plan_locked(
        self,
        *,
        key: bytes,
        value: ObservationLocatorValueV1,
        node_updates: tuple[tuple[int, int, bytes], ...],
    ) -> None:
        self._connection.executemany(
            """INSERT INTO world_v2_prefix_locator_nodes
                 (world_id, depth, prefix_bits, node_hash) VALUES (?, ?, ?, ?)
               ON CONFLICT(world_id, depth, prefix_bits) DO UPDATE
                 SET node_hash = excluded.node_hash""",
            ((self._world_id, depth, _prefix_bits_blob(prefix), node_hash)
             for depth, prefix, node_hash in node_updates),
        )
        self._connection.execute(
            """INSERT INTO world_v2_prefix_locator_values
                 (world_id, locator_key, value_hash, observation_id, event_type, event_id,
                  ledger_sequence, world_revision, deliberation_revision, event_leaf_index,
                  event_leaf_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._world_id, key, value.digest(), value.observation_id, value.event_type,
                value.event_id, value.ledger_sequence, value.world_revision,
                value.deliberation_revision, value.event_leaf_index, value.event_leaf_hash,
            ),
        )

    def project(self) -> LedgerProjection:
        with self._thread_lock:
            return self._project_locked()

    def project_at(self, cursor: ProjectionCursor) -> LedgerProjection:
        with self._thread_lock:
            head = self._project_locked()
            head_cursor = ProjectionCursor(
                world_revision=head.world_revision,
                deliberation_revision=head.deliberation_revision,
                ledger_sequence=head.ledger_sequence,
            )
            if cursor.ledger_sequence > head.ledger_sequence:
                raise ValueError("requested projection cursor is outside the ledger range")
            if cursor == head_cursor:
                return head
            return self._replay_locked(
                target_cursor=cursor,
                target_schema_version=CURRENT_SCHEMA_VERSION,
                reducer_bundle_version=REDUCER_BUNDLE_VERSION,
            )

    def _pin_observation_history_proof(
        self, *, cursor: ProjectionCursor
    ) -> _PinnedObservationHistoryProof:
        """Freeze a verified-prefix anchor without exposing proof internals."""

        if type(cursor) is not ProjectionCursor:
            raise ValueError("cursor must be an exact ProjectionCursor")
        with self._thread_lock:
            connection = self._connection
            try:
                connection.execute("BEGIN")
                prefix_head = connection.execute(
                    "SELECT * FROM world_v2_prefix_heads WHERE world_id = ?", (self._world_id,)
                ).fetchone()
                if prefix_head is None:
                    raise LedgerIntegrityError("prefix proof head is unavailable")
                anchor_leaf_count = int(prefix_head["mmr_leaf_count"])
                anchor_root = bytes(prefix_head["mmr_root"])
                computed_anchor_root = self._prefix_mmr_root_at_leaf_count_locked(anchor_leaf_count)
                if not hmac.compare_digest(computed_anchor_root, anchor_root):
                    raise LedgerIntegrityError("prefix proof anchor does not match persisted head")
                zero = ProjectionCursor(
                    world_revision=0, deliberation_revision=0, ledger_sequence=0
                )
                checkpoint: PrefixCheckpointLeafV1 | None = None
                if cursor == zero:
                    locator_root = IncrementalSparseMerkleMapV1().root
                else:
                    row = connection.execute(
                        """SELECT * FROM world_v2_prefix_checkpoints
                           WHERE world_id = ? AND world_revision = ?
                             AND deliberation_revision = ? AND ledger_sequence = ?""",
                        (
                            self._world_id,
                            cursor.world_revision,
                            cursor.deliberation_revision,
                            cursor.ledger_sequence,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ValueError("requested cursor is not a committed batch boundary")
                    checkpoint = self._prefix_checkpoint_from_row(row)
                    if checkpoint.mmr_leaf_count > anchor_leaf_count:
                        raise LedgerIntegrityError("checkpoint is after its prefix anchor")
                    verify_checkpoint_in_prefix(
                        checkpoint=checkpoint,
                        proof=self._prefix_mmr_proof_at_leaf_count_locked(
                            leaf_index=checkpoint.mmr_leaf_count - 1,
                            leaf_count=anchor_leaf_count,
                        ),
                        expected_root=anchor_root,
                        expected_world_id=self._world_id,
                        expected_commit_id=checkpoint.commit_id,
                        expected_cursor=(
                            cursor.world_revision,
                            cursor.deliberation_revision,
                            cursor.ledger_sequence,
                        ),
                    )
                    locator_root = bytes.fromhex(checkpoint.locator_root)
                pin = _PinnedObservationHistoryProof(
                    world_id=self._world_id,
                    cursor=cursor,
                    anchor_leaf_count=anchor_leaf_count,
                    anchor_mmr_root=anchor_root,
                    checkpoint=checkpoint,
                    proof_version=_PREFIX_PROOF_VERSION,
                )
                # Checking the root while the snapshot is open makes an empty
                # cursor just as explicit as a checkpoint cursor.
                if not isinstance(locator_root, bytes) or len(locator_root) != 32:
                    raise LedgerIntegrityError("pinned locator root is invalid")
                connection.commit()
                return pin
            except sqlite3.DatabaseError as exc:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise LedgerIntegrityError("prefix proof pin snapshot failed") from exc
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise

    def _read_observation_history_proof(
        self,
        *,
        pin: _PinnedObservationHistoryProof,
        locators: Sequence[ObservationEventLocator],
    ) -> tuple[ProofBackedObservationLookup, ...]:
        validated = _validated_observation_locators(locators)
        with self._thread_lock:
            connection = self._connection
            try:
                connection.execute("BEGIN")
                computed_anchor_root = self._prefix_mmr_root_at_leaf_count_locked(
                    pin.anchor_leaf_count
                )
                if not hmac.compare_digest(computed_anchor_root, pin.anchor_mmr_root):
                    raise LedgerIntegrityError("pinned prefix anchor no longer verifies")
                if pin.checkpoint is None:
                    if pin.cursor != ProjectionCursor(
                        world_revision=0, deliberation_revision=0, ledger_sequence=0
                    ):
                        raise LedgerIntegrityError("pinned historical cursor lacks a checkpoint")
                    locator_root = IncrementalSparseMerkleMapV1().root
                else:
                    row = connection.execute(
                        """SELECT * FROM world_v2_prefix_checkpoints
                           WHERE world_id = ? AND world_revision = ?
                             AND deliberation_revision = ? AND ledger_sequence = ?""",
                        (
                            self._world_id,
                            pin.cursor.world_revision,
                            pin.cursor.deliberation_revision,
                            pin.cursor.ledger_sequence,
                        ),
                    ).fetchone()
                    if row is None or self._prefix_checkpoint_from_row(row) != pin.checkpoint:
                        raise LedgerIntegrityError("pinned checkpoint changed or disappeared")
                    verify_checkpoint_in_prefix(
                        checkpoint=pin.checkpoint,
                        proof=self._prefix_mmr_proof_at_leaf_count_locked(
                            leaf_index=pin.checkpoint.mmr_leaf_count - 1,
                            leaf_count=pin.anchor_leaf_count,
                        ),
                        expected_root=pin.anchor_mmr_root,
                        expected_world_id=self._world_id,
                        expected_commit_id=pin.checkpoint.commit_id,
                        expected_cursor=(
                            pin.cursor.world_revision,
                            pin.cursor.deliberation_revision,
                            pin.cursor.ledger_sequence,
                        ),
                    )
                    locator_root = bytes.fromhex(pin.checkpoint.locator_root)
                lookups = tuple(
                    self._proof_lookup_observation_locator_locked(
                        locator=locator,
                        cursor=pin.cursor,
                        locator_root=locator_root,
                        anchor_leaf_count=pin.anchor_leaf_count,
                        anchor_root=pin.anchor_mmr_root,
                    )
                    for locator in validated
                )
                returned_bytes = sum(
                    len(canonical_event_json(item.event.event).encode("utf-8"))
                    for item in lookups
                    if item.event is not None
                )
                if returned_bytes > OBSERVATION_HISTORY_MAX_BYTES:
                    raise LedgerIntegrityError("proof-backed observation read exceeds byte budget")
                connection.commit()
                return lookups
            except sqlite3.DatabaseError as exc:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise LedgerIntegrityError("observation proof snapshot read failed") from exc
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise

    def _prefix_mmr_node_lookup_locked(self, height: int, node_index: int) -> bytes | None:
        """Read one persisted MMR node at its stable logical address."""

        if type(height) is not int or type(node_index) is not int or height < 0 or node_index < 0:
            raise LedgerIntegrityError("persisted MMR node address is invalid")
        row = self._connection.execute(
            """SELECT node_hash FROM world_v2_prefix_mmr_nodes
               WHERE world_id = ? AND height = ? AND node_index = ?""",
            (self._world_id, height, node_index),
        ).fetchone()
        if row is None:
            return None
        node_hash = bytes(row["node_hash"])
        if len(node_hash) != 32:
            raise LedgerIntegrityError("persisted MMR node hash is invalid")
        return node_hash

    def _prefix_locator_node_lookup_locked(self, depth: int, prefix: int) -> bytes | None:
        """Read one current sparse-map node for a durable put path."""

        if type(depth) is not int or not 0 <= depth <= 256:
            raise LedgerIntegrityError("persisted locator-node depth is invalid")
        if type(prefix) is not int or prefix < 0 or prefix.bit_length() > depth:
            raise LedgerIntegrityError("persisted locator-node prefix is invalid")
        row = self._connection.execute(
            """SELECT node_hash FROM world_v2_prefix_locator_nodes
               WHERE world_id = ? AND depth = ? AND prefix_bits = ?""",
            (self._world_id, depth, _prefix_bits_blob(prefix)),
        ).fetchone()
        if row is None:
            return None
        node_hash = bytes(row["node_hash"])
        if len(node_hash) != 32:
            raise LedgerIntegrityError("persisted locator-node hash is invalid")
        return node_hash

    def _prefix_mmr_root_at_leaf_count_locked(self, leaf_count: int) -> bytes:
        if type(leaf_count) is not int or leaf_count < 0:
            raise LedgerIntegrityError("pinned MMR leaf count is invalid")
        try:
            return mmr_root_from_node_lookup_v1(
                leaf_count=leaf_count,
                node_lookup=self._prefix_mmr_node_lookup_locked,
            )
        except Exception as exc:
            raise LedgerIntegrityError("pinned MMR state is invalid") from exc

    def _prefix_mmr_proof_at_leaf_count_locked(
        self, *, leaf_index: int, leaf_count: int
    ):
        if type(leaf_count) is not int or leaf_count < 0:
            raise LedgerIntegrityError("pinned MMR leaf count is invalid")
        try:
            return mmr_inclusion_proof_from_node_lookup_v1(
                leaf_index=leaf_index,
                leaf_count=leaf_count,
                node_lookup=self._prefix_mmr_node_lookup_locked,
            )
        except Exception as exc:
            raise LedgerIntegrityError("pinned MMR proof state is invalid") from exc

    def _proof_lookup_observation_locator_locked(
        self,
        *,
        locator: ObservationEventLocator,
        cursor: ProjectionCursor,
        locator_root: bytes,
        anchor_leaf_count: int,
        anchor_root: bytes,
    ) -> ProofBackedObservationLookup:
        key = observation_locator_key(
            world_id=self._world_id,
            event_type=locator.event_type,
            idempotency_key=locator.idempotency_key,
        )
        value_row = self._connection.execute(
            """SELECT * FROM world_v2_prefix_locator_values
               WHERE world_id = ? AND locator_key = ?""",
            (self._world_id, key),
        ).fetchone()
        value: ObservationLocatorValueV1 | None = None
        value_hash: bytes | None = None
        if value_row is not None and int(value_row["ledger_sequence"]) <= cursor.ledger_sequence:
            value = ObservationLocatorValueV1(
                observation_id=str(value_row["observation_id"]),
                event_type=str(value_row["event_type"]),
                event_id=str(value_row["event_id"]),
                ledger_sequence=int(value_row["ledger_sequence"]),
                world_revision=int(value_row["world_revision"]),
                deliberation_revision=int(value_row["deliberation_revision"]),
                event_leaf_index=int(value_row["event_leaf_index"]),
                event_leaf_hash=bytes(value_row["event_leaf_hash"]),
            )
            value_hash = value.digest()
            if not hmac.compare_digest(value_hash, bytes(value_row["value_hash"])):
                raise LedgerIntegrityError("observation locator value hash is invalid")
        historical_nodes = self._historical_locator_sibling_nodes_locked(
            key=key, ledger_sequence=cursor.ledger_sequence
        )
        proof = sparse_merkle_proof_from_nodes_v1(
            key=key, value_hash=value_hash, nodes=historical_nodes
        )
        if value is None:
            proof.verify_nonmembership(expected_root=locator_root, expected_key=key)
            return ProofBackedObservationLookup(
                locator=locator, status="locator_missing", event=None
            )
        if (
            value.observation_id != locator.observation_id
            or value.event_type != locator.event_type
            or value.ledger_sequence > cursor.ledger_sequence
        ):
            raise LedgerIntegrityError("observation locator value does not match request")
        proof.verify_membership(
            expected_root=locator_root, expected_key=key, expected_value_hash=value_hash
        )
        event = self._proof_backed_event_locked(
            locator=locator,
            value=value,
            anchor_leaf_count=anchor_leaf_count,
            anchor_root=anchor_root,
            cursor=cursor,
        )
        return ProofBackedObservationLookup(locator=locator, status="found", event=event)

    def _historical_locator_sibling_nodes_locked(
        self, *, key: bytes, ledger_sequence: int
    ) -> dict[tuple[int, int], bytes]:
        key_int = int.from_bytes(key, "big")
        addresses = tuple(
            (
                depth + 1,
                (key_int >> (256 - depth) << 1)
                | (1 - ((key_int >> (255 - depth)) & 1)),
            )
            for depth in range(256)
        )
        values = ", ".join("(?, ?)" for _ in addresses)
        params: tuple[object, ...] = tuple(
            value for depth, prefix in addresses for value in (depth, _prefix_bits_blob(prefix))
        ) + (self._world_id, ledger_sequence)
        rows = tuple(
            self._connection.execute(
                f"""WITH requested(depth, prefix_bits) AS (VALUES {values})
                    SELECT requested.depth, requested.prefix_bits,
                           (
                               SELECT node_hash
                               FROM world_v2_prefix_locator_node_history AS history
                               WHERE history.world_id = ?
                                 AND history.depth = requested.depth
                                 AND history.prefix_bits = requested.prefix_bits
                                 AND history.ledger_sequence <= ?
                               ORDER BY history.ledger_sequence DESC LIMIT 1
                           ) AS node_hash
                    FROM requested""",
                params,
            )
        )
        nodes: dict[tuple[int, int], bytes] = {}
        for row in rows:
            node_hash = row["node_hash"]
            if node_hash is not None:
                nodes[(int(row["depth"]), _prefix_bits_int(bytes(row["prefix_bits"])))] = bytes(
                    node_hash
                )
        return nodes

    def _proof_backed_event_locked(
        self,
        *,
        locator: ObservationEventLocator,
        value: ObservationLocatorValueV1,
        anchor_leaf_count: int,
        anchor_root: bytes,
        cursor: ProjectionCursor,
    ) -> HistoricalLedgerEvent:
        row = self._connection.execute(
            """SELECT * FROM world_v2_events WHERE world_id = ? AND event_id = ?""",
            (self._world_id, value.event_id),
        ).fetchone()
        if row is None:
            raise LedgerIntegrityError("proof-backed observation event is unavailable")
        event_json = row["event_json"]
        if not isinstance(event_json, str):
            raise LedgerIntegrityError("proof-backed observation bytes are invalid")
        try:
            if len(event_json.encode("utf-8")) > OBSERVATION_HISTORY_MAX_BYTES:
                raise LedgerIntegrityError("proof-backed observation event exceeds byte budget")
        except UnicodeError as exc:
            raise LedgerIntegrityError("proof-backed observation bytes are invalid") from exc
        envelope_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(envelope_hash, str(row["event_hash"])):
            raise LedgerIntegrityError("proof-backed observation envelope hash is invalid")
        try:
            event = WorldEvent.model_validate_json(event_json)
            validate_event_identity(event)
        except Exception as exc:
            raise LedgerIntegrityError("proof-backed observation event is invalid") from exc
        if (
            event.world_id != self._world_id
            or event.event_id != value.event_id
            or event.event_type != locator.event_type
            or event.idempotency_key != locator.idempotency_key
            or _observation_id(event) != locator.observation_id
            or int(row["ledger_sequence"]) != value.ledger_sequence
            or int(row["world_revision"]) != value.world_revision
            or int(row["deliberation_revision"]) != value.deliberation_revision
            or value.ledger_sequence > cursor.ledger_sequence
        ):
            raise LedgerIntegrityError("proof-backed observation row does not match locator value")
        leaf = LedgerLeafV1(
            world_id=self._world_id,
            ledger_sequence=value.ledger_sequence,
            world_revision=value.world_revision,
            deliberation_revision=value.deliberation_revision,
            commit_id=str(row["commit_id"]),
            event_id=event.event_id,
            idempotency_key=event.idempotency_key,
            event_envelope_hash=envelope_hash,
        ).digest()
        if not hmac.compare_digest(leaf, value.event_leaf_hash):
            raise LedgerIntegrityError("proof-backed observation leaf does not match locator value")
        try:
            self._prefix_mmr_proof_at_leaf_count_locked(
                leaf_index=value.event_leaf_index,
                leaf_count=anchor_leaf_count,
            ).verify(
                leaf_hash=leaf, expected_root=anchor_root
            )
        except Exception as exc:
            raise LedgerIntegrityError("proof-backed observation event MMR proof is invalid") from exc
        return HistoricalLedgerEvent(
            event=event,
            event_cursor=ProjectionCursor(
                world_revision=value.world_revision,
                deliberation_revision=value.deliberation_revision,
                ledger_sequence=value.ledger_sequence,
            ),
            event_envelope_hash=envelope_hash,
        )

    def observation_events_at(
        self, locators: Sequence[ObservationEventLocator], *, cursor: ProjectionCursor
    ) -> tuple[HistoricalLedgerEvent, ...]:
        validated = _validated_observation_locators(locators)
        with self._thread_lock:
            connection = self._connection
            try:
                connection.execute("BEGIN")
                result = self._observation_events_at_locked(validated, cursor=cursor)
                connection.commit()
                return result
            except sqlite3.DatabaseError as exc:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise LedgerIntegrityError(
                    "observation history snapshot read failed"
                ) from exc
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise

    def _observation_events_at_locked(
        self,
        validated: tuple[ObservationEventLocator, ...],
        *,
        cursor: ProjectionCursor,
    ) -> tuple[HistoricalLedgerEvent, ...]:
        placeholders = ",".join("?" for _ in validated)
        with self._thread_lock:
            verified_commits: dict[str, tuple[tuple[WorldEvent, ...], CommitResult]] = {}
            zero = ProjectionCursor(
                world_revision=0, deliberation_revision=0, ledger_sequence=0
            )
            if cursor != zero:
                boundary_row = self._connection.execute(
                    """SELECT commit_id FROM world_v2_events
                       WHERE world_id = ? AND ledger_sequence = ?""",
                    (self._world_id, cursor.ledger_sequence),
                ).fetchone()
                if boundary_row is None:
                    raise ValueError("requested cursor is not a committed batch boundary")
                boundary_commit_id = str(boundary_row["commit_id"])
                self._require_observation_commit_budget_locked((boundary_commit_id,))
                boundary_events, boundary_result, _ = self._verified_commit_locked(
                    boundary_commit_id
                )
                boundary_cursor = ProjectionCursor(
                    world_revision=boundary_result.world_revision,
                    deliberation_revision=boundary_result.deliberation_revision,
                    ledger_sequence=boundary_result.ledger_sequence,
                )
                if boundary_cursor != cursor:
                    raise ValueError("requested cursor is not a committed batch boundary")
                verified_commits[boundary_commit_id] = (
                    boundary_events,
                    boundary_result,
                )
            rows = tuple(
                self._connection.execute(
                    f"""SELECT * FROM world_v2_events
                        WHERE world_id = ? AND idempotency_key IN ({placeholders})
                          AND ledger_sequence <= ?
                        ORDER BY ledger_sequence""",
                    (
                        self._world_id,
                        *(locator.idempotency_key for locator in validated),
                        cursor.ledger_sequence,
                    ),
                )
            )
            if len(rows) > len(validated):
                raise LedgerIntegrityError("locator query returned too many candidates")
            candidate_commit_ids = tuple(sorted({str(row["commit_id"]) for row in rows}))
            self._require_observation_commit_budget_locked(candidate_commit_ids)
            by_idempotency = {locator.idempotency_key: locator for locator in validated}
            candidates: list[tuple[str, HistoricalLedgerEvent]] = []
            for row in rows:
                locator = by_idempotency.get(str(row["idempotency_key"]))
                if locator is None:
                    raise LedgerIntegrityError("locator query returned an unknown identity")
                commit_id = str(row["commit_id"])
                verified = verified_commits.get(commit_id)
                if verified is None:
                    events, result, _ = self._verified_commit_locked(
                        commit_id, verified_prefix_cursor=cursor
                    )
                    verified_commits[commit_id] = (events, result)
                else:
                    events, _ = verified
                event = next(
                    (item for item in events if item.event_id == str(row["event_id"])), None
                )
                if event is None:
                    raise LedgerIntegrityError("candidate event is absent from its owning commit")
                encoded = canonical_event_json(event)
                envelope_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                if encoded != row["event_json"] or not hmac.compare_digest(
                    envelope_hash, str(row["event_hash"])
                ):
                    raise LedgerIntegrityError("candidate event envelope does not match its row")
                if (
                    event.world_id != self._world_id
                    or event.event_id != row["event_id"]
                    or event.idempotency_key != row["idempotency_key"]
                ):
                    raise LedgerIntegrityError("candidate event identity does not match its row")
                observation_id = _observation_id(event)
                if (
                    observation_id != locator.observation_id
                    or event.event_type != locator.event_type
                    or event.idempotency_key != locator.idempotency_key
                ):
                    raise LedgerIntegrityError("observation locator does not match its event row")
                event_cursor = ProjectionCursor(
                    world_revision=int(row["world_revision"]),
                    deliberation_revision=int(row["deliberation_revision"]),
                    ledger_sequence=int(row["ledger_sequence"]),
                )
                if event_cursor.ledger_sequence > cursor.ledger_sequence:
                    raise LedgerIntegrityError("candidate observation is newer than the cursor")
                candidates.append(
                    (
                        locator.observation_id,
                        HistoricalLedgerEvent(
                            event=event,
                            event_cursor=event_cursor,
                            event_envelope_hash=envelope_hash,
                        ),
                    )
                )
            candidates.sort(
                key=lambda item: (item[0], item[1].event.event_type, item[1].event.event_id)
            )
            return tuple(candidate for _, candidate in candidates)

    def _require_observation_commit_budget_locked(
        self, commit_ids: Sequence[str]
    ) -> None:
        identities = tuple(sorted(set(commit_ids)))
        if not identities:
            return
        placeholders = ",".join("?" for _ in identities)
        rows = tuple(
            self._connection.execute(
                f"""SELECT commit_id, COUNT(*) AS event_count,
                       COALESCE(SUM(LENGTH(CAST(event_json AS BLOB))), 0) AS byte_count
                FROM world_v2_events
                WHERE world_id = ? AND commit_id IN ({placeholders})
                GROUP BY commit_id""",
                (self._world_id, *identities),
            )
        )
        if {str(row["commit_id"]) for row in rows} != set(identities):
            raise LedgerIntegrityError("observation history owning commit is unavailable")
        for row in rows:
            if int(row["event_count"]) > OBSERVATION_HISTORY_MAX_COMMIT_EVENTS:
                raise LedgerIntegrityError(
                    "observation history commit event budget exceeded"
                )
            if int(row["byte_count"]) > OBSERVATION_HISTORY_MAX_BYTES:
                raise LedgerIntegrityError("observation history byte budget exceeded")

    def lookup_event_commit(self, event_id: str) -> tuple[WorldEvent, CommitResult] | None:
        """Return verified persisted bytes and the result of their original commit."""

        with self._thread_lock:
            row = self._connection.execute(
                """SELECT commit_id FROM world_v2_events
                   WHERE world_id = ? AND event_id = ?""",
                (self._world_id, event_id),
            ).fetchone()
            if row is None:
                return None
            events, result, _ = self._verified_commit_locked(str(row["commit_id"]))
            event = next((item for item in events if item.event_id == event_id), None)
            if event is None:
                raise LedgerIntegrityError("event is absent from its owning commit")
            return event, result

    def _find_appraisal_proposal_event(
        self, *, proposal_id: str, cursor: ProjectionCursor
    ) -> WorldEvent | None:
        """Internal exact lookup used by the Appraisal acceptance reader."""

        with self._thread_lock:
            rows = tuple(
                self._connection.execute(
                    """SELECT event_id FROM world_v2_events
                       WHERE world_id = ?
                         AND ledger_sequence <= ?
                         AND json_extract(event_json, '$.event_type') = 'ProposalRecorded'
                         AND json_extract(json_extract(event_json, '$.payload_json'), '$.proposal_id') = ?
                         AND json_extract(json_extract(event_json, '$.payload_json'), '$.proposal_kind') = 'appraisal_transition'""",
                    (self._world_id, cursor.ledger_sequence, proposal_id),
                )
            )
            if len(rows) > 1:
                raise LedgerIntegrityError("Appraisal proposal identity has multiple envelopes")
            if not rows:
                return None
            located = self.lookup_event_commit(str(rows[0]["event_id"]))
            if located is None:
                raise LedgerIntegrityError("Appraisal proposal event disappeared")
            return located[0]

    def _find_affect_proposal_event(
        self, *, proposal_id: str, cursor: ProjectionCursor
    ) -> WorldEvent | None:
        """Internal exact lookup used by the Affect acceptance reader."""

        with self._thread_lock:
            rows = tuple(
                self._connection.execute(
                    """SELECT event_id FROM world_v2_events
                       WHERE world_id = ?
                         AND ledger_sequence <= ?
                         AND json_extract(event_json, '$.event_type') = 'ProposalRecorded'
                         AND json_extract(json_extract(event_json, '$.payload_json'), '$.proposal_id') = ?
                         AND json_extract(json_extract(event_json, '$.payload_json'), '$.proposal_kind') = 'affect_transition'""",
                    (self._world_id, cursor.ledger_sequence, proposal_id),
                )
            )
            if len(rows) > 1:
                raise LedgerIntegrityError("Affect proposal identity has multiple envelopes")
            if not rows:
                return None
            located = self.lookup_event_commit(str(rows[0]["event_id"]))
            if located is None:
                raise LedgerIntegrityError("Affect proposal event disappeared")
            return located[0]

    def resolve_committed_event_refs(
        self, event_ids: Sequence[str], *, at_world_revision: int
    ) -> dict[str, CommittedWorldEventRef]:
        """Resolve only requested sources through the unique event-id index."""

        identities = tuple(sorted(set(event_ids)))
        if len(identities) != len(event_ids):
            raise ValueError("committed event source identities must be unique")
        if not identities:
            return {}
        placeholders = ",".join("?" for _ in identities)
        with self._thread_lock:
            rows = tuple(
                self._connection.execute(
                    f"""SELECT event_id, world_revision, event_json, event_hash
                        FROM world_v2_events
                        WHERE world_id = ? AND event_id IN ({placeholders})""",
                    (self._world_id, *identities),
                )
            )
            if len(rows) != len(identities):
                raise ValueError("one or more committed Situation sources are unavailable")
            resolved = {
                str(row["event_id"]): self._committed_ref_from_row(
                    row, at_world_revision=at_world_revision
                )
                for row in rows
            }
            if set(resolved) != set(identities):
                raise LedgerIntegrityError("event source query returned the wrong identities")
            return resolved

    def resolve_initial_world_event_ref(
        self, *, at_world_revision: int
    ) -> CommittedWorldEventRef:
        with self._thread_lock:
            row = self._connection.execute(
                """SELECT event_id, world_revision, event_json, event_hash
                   FROM world_v2_events
                   WHERE world_id = ?
                   ORDER BY ledger_sequence ASC LIMIT 1""",
                (self._world_id,),
            ).fetchone()
            if row is None:
                raise ValueError("world has no initial event authority")
            resolved = self._committed_ref_from_row(
                row, at_world_revision=at_world_revision
            )
            if resolved.event_type != "WorldStarted":
                raise ValueError("world has no pinned WorldStarted authority")
            return resolved

    def _committed_ref_from_row(
        self, row: sqlite3.Row, *, at_world_revision: int
    ) -> CommittedWorldEventRef:
        event_json = row["event_json"]
        if not isinstance(event_json, str) or not hmac.compare_digest(
            hashlib.sha256(event_json.encode()).hexdigest(), str(row["event_hash"])
        ):
            raise LedgerIntegrityError("event source envelope hash mismatch")
        event = WorldEvent.model_validate_json(event_json)
        validate_event_identity(event)
        world_revision = int(row["world_revision"])
        if (
            event.world_id != self._world_id
            or event.event_id != row["event_id"]
        ):
            raise LedgerIntegrityError("event source row does not match its authority")
        if world_revision > at_world_revision:
            raise ValueError("committed event source is newer than the pinned projection")
        if event_definition(event.event_type).revision_class is not RevisionClass.WORLD:
            raise ValueError("Situation source is not a committed world event")
        return CommittedWorldEventRef(
            event_id=event.event_id,
            event_type=event.event_type,
            world_revision=world_revision,
            payload_hash=event.payload_hash,
            logical_time=event.logical_time,
            continuation_refs=(
                (str(event.payload()["appraisal_trigger_ref"]),)
                if event.event_type == "WorldOccurrenceSettled"
                else ()
            ),
        )

    def _verified_commit_locked(
        self,
        commit_id: str,
        *,
        verified_prefix_cursor: ProjectionCursor | None = None,
    ) -> tuple[tuple[WorldEvent, ...], CommitResult, str]:
        """Rebuild a commit, optionally trusting a caller-verified history prefix."""

        commit_row = self._connection.execute(
            """SELECT request_hash, result_json FROM world_v2_commits
               WHERE world_id = ? AND commit_id = ?""",
            (self._world_id, commit_id),
        ).fetchone()
        if commit_row is None:
            raise LedgerIntegrityError("event owning commit is missing")
        rows = tuple(
            self._connection.execute(
                """SELECT * FROM world_v2_events
                   WHERE world_id = ? AND commit_id = ?
                   ORDER BY ledger_sequence""",
                (self._world_id, commit_id),
            )
        )
        if not rows:
            raise LedgerIntegrityError("commit has no event rows")
        events: list[WorldEvent] = []
        try:
            first_sequence = int(rows[0]["ledger_sequence"])
            if first_sequence == 1:
                expected_sequence = 0
                expected_world_revision = 0
                expected_deliberation_revision = 0
            else:
                previous = self._connection.execute(
                    """SELECT ledger_sequence, world_revision, deliberation_revision
                       FROM world_v2_events
                       WHERE world_id = ? AND ledger_sequence = ?""",
                    (self._world_id, first_sequence - 1),
                ).fetchone()
                if previous is None:
                    raise LedgerIntegrityError("commit event rows are not contiguous")
                previous_cursor = ProjectionCursor(
                    ledger_sequence=int(previous["ledger_sequence"]),
                    world_revision=int(previous["world_revision"]),
                    deliberation_revision=int(previous["deliberation_revision"]),
                )
                if verified_prefix_cursor is not None:
                    last_sequence = int(rows[-1]["ledger_sequence"])
                    if (
                        previous_cursor.ledger_sequence
                        > verified_prefix_cursor.ledger_sequence
                        or last_sequence > verified_prefix_cursor.ledger_sequence
                    ):
                        raise LedgerIntegrityError(
                            "commit is outside the verified history prefix"
                        )
                    expected_sequence = previous_cursor.ledger_sequence
                    expected_world_revision = previous_cursor.world_revision
                    expected_deliberation_revision = (
                        previous_cursor.deliberation_revision
                    )
                else:
                    verified_prefix = self._replay_locked(
                        target_cursor=previous_cursor,
                        target_schema_version=CURRENT_SCHEMA_VERSION,
                        reducer_bundle_version=REDUCER_BUNDLE_VERSION,
                    )
                    expected_sequence = verified_prefix.ledger_sequence
                    expected_world_revision = verified_prefix.world_revision
                    expected_deliberation_revision = (
                        verified_prefix.deliberation_revision
                    )
            for row in rows:
                event_json = row["event_json"]
                if not isinstance(event_json, str):
                    raise LedgerIntegrityError("persisted event bytes are invalid")
                actual_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
                if not hmac.compare_digest(actual_hash, str(row["event_hash"])):
                    raise LedgerIntegrityError("event envelope hash mismatch")
                event = WorldEvent.model_validate_json(event_json)
                validate_event_identity(event)
                if (
                    event.world_id != self._world_id
                    or event.event_id != row["event_id"]
                    or event.idempotency_key != row["idempotency_key"]
                ):
                    raise LedgerIntegrityError("event envelope does not match its ledger row")
                expected_sequence += 1
                definition = event_definition(event.event_type)
                if definition.revision_class is RevisionClass.WORLD:
                    expected_world_revision += 1
                else:
                    expected_deliberation_revision += 1
                if (
                    int(row["ledger_sequence"]) != expected_sequence
                    or int(row["world_revision"]) != expected_world_revision
                    or int(row["deliberation_revision"]) != expected_deliberation_revision
                ):
                    raise LedgerIntegrityError("commit event revisions are discontinuous")
                events.append(event)
            calculated_request_hash = commit_request_hash(events)
            persisted_request_hash = str(commit_row["request_hash"])
            if not hmac.compare_digest(calculated_request_hash, persisted_request_hash):
                raise LedgerIntegrityError("commit request hash does not match event rows")
            last = rows[-1]
            rebuilt_result = CommitResult(
                world_revision=int(last["world_revision"]),
                deliberation_revision=int(last["deliberation_revision"]),
                ledger_sequence=int(last["ledger_sequence"]),
                event_ids=tuple(event.event_id for event in events),
            )
            persisted_result = CommitResult.model_validate_json(commit_row["result_json"])
            if persisted_result != rebuilt_result:
                raise LedgerIntegrityError("commit result does not match event rows")
        except LedgerIntegrityError:
            raise
        except Exception as exc:
            raise LedgerIntegrityError("persisted commit is invalid") from exc
        return tuple(events), rebuilt_result, persisted_request_hash

    def _project_locked(self) -> LedgerProjection:
        try:
            head = self._connection.execute(
                "SELECT * FROM world_v2_heads WHERE world_id = ?", (self._world_id,)
            ).fetchone()
            if head is None:
                raise LedgerIntegrityError("world head disappeared")
            if head["reducer_bundle_version"] != REDUCER_BUNDLE_VERSION:
                raise LedgerIntegrityError("world head reducer bundle is not installed")
            state = self._decode_state(head["state_json"])
            cursor = ProjectionCursor(
                world_revision=int(head["world_revision"]),
                deliberation_revision=int(head["deliberation_revision"]),
                ledger_sequence=int(head["ledger_sequence"]),
            )
            persisted_state_hash = head["state_hash"]
            if not persisted_state_hash or not hmac.compare_digest(
                self._state_hash(state, cursor), str(persisted_state_hash)
            ):
                raise LedgerIntegrityError("head state hash does not match persisted state")
            projection = make_projection(
                world_id=self._world_id,
                world_revision=int(head["world_revision"]),
                deliberation_revision=int(head["deliberation_revision"]),
                ledger_sequence=int(head["ledger_sequence"]),
                state=state,
            )
            if projection.semantic_hash != head["semantic_hash"]:
                raise LedgerIntegrityError("head semantic hash does not match persisted state")
            return projection
        except LedgerIntegrityError:
            raise
        except Exception as exc:
            raise LedgerIntegrityError("persisted world head is invalid") from exc

    def rebuild(
        self,
        *,
        target_schema_version: str = CURRENT_SCHEMA_VERSION,
        reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
    ) -> LedgerProjection:
        with self._thread_lock:
            return self._rebuild_locked(
                target_schema_version=target_schema_version,
                reducer_bundle_version=reducer_bundle_version,
            )

    def export_replay_evidence(
        self, *, at_cursor: ProjectionCursor | None = None
    ) -> ReplayEvidence:
        """Export one transactionally consistent replay-evaluation snapshot.

        The read transaction binds persisted head state, independent reducer
        replay, immutable event envelopes and their commit records to one
        cursor.  The evaluator need not make further adapter calls, so a
        writer cannot produce a mixed-head diagnostic.
        """

        with self._thread_lock:
            connection = self._connection
            try:
                connection.execute("BEGIN")
                head = self._project_locked()
                head_cursor = ProjectionCursor(
                    world_revision=head.world_revision,
                    deliberation_revision=head.deliberation_revision,
                    ledger_sequence=head.ledger_sequence,
                )
                cursor = at_cursor or head_cursor
                zero = ProjectionCursor(
                    world_revision=0, deliberation_revision=0, ledger_sequence=0
                )
                if cursor != zero and connection.execute(
                    """SELECT 1 FROM world_v2_prefix_checkpoints
                       WHERE world_id = ? AND world_revision = ?
                         AND deliberation_revision = ? AND ledger_sequence = ?""",
                    (
                        self._world_id,
                        cursor.world_revision,
                        cursor.deliberation_revision,
                        cursor.ledger_sequence,
                    ),
                ).fetchone() is None:
                    raise ValueError("replay evidence cursor is not a committed batch boundary")
                if cursor.ledger_sequence > head_cursor.ledger_sequence:
                    raise ValueError("replay evidence cursor is outside the ledger range")

                projection = (
                    head
                    if cursor == head_cursor
                    else self._replay_locked(
                        target_cursor=cursor,
                        target_schema_version=CURRENT_SCHEMA_VERSION,
                        reducer_bundle_version=REDUCER_BUNDLE_VERSION,
                    )
                )
                replay = self._replay_locked(
                    target_cursor=cursor,
                    target_schema_version=CURRENT_SCHEMA_VERSION,
                    reducer_bundle_version=REDUCER_BUNDLE_VERSION,
                )
                if replay != projection:
                    raise LedgerIntegrityError("replay evidence does not match projection")
                events, commits = self._replay_evidence_rows_locked(cursor=cursor)
                connection.commit()
                return ReplayEvidence(
                    world_id=self._world_id,
                    cursor=cursor,
                    reducer_bundle_version=REDUCER_BUNDLE_VERSION,
                    projection=projection,
                    replay=replay,
                    events=events,
                    commits=commits,
                )
            except sqlite3.DatabaseError as exc:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise LedgerIntegrityError("replay evidence export failed") from exc
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise

    def _replay_evidence_rows_locked(
        self, *, cursor: ProjectionCursor
    ) -> tuple[tuple[ReplayEventEvidence, ...], tuple[ReplayCommitEvidence, ...]]:
        """Materialize current-format event/commit evidence inside an open read transaction."""

        commit_count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM world_v2_commits WHERE world_id = ?", (self._world_id,)
            ).fetchone()[0]
        )
        event_commit_count = int(
            self._connection.execute(
                """SELECT COUNT(DISTINCT commit_id) FROM world_v2_events
                   WHERE world_id = ?""",
                (self._world_id,),
            ).fetchone()[0]
        )
        if commit_count != event_commit_count:
            raise LedgerIntegrityError("replay evidence ledger has an empty or orphaned commit")
        rows = tuple(
            self._connection.execute(
                """SELECT e.*, c.request_hash, c.result_json
                   FROM world_v2_events AS e JOIN world_v2_commits AS c
                     ON c.world_id = e.world_id AND c.commit_id = e.commit_id
                   WHERE e.world_id = ? AND e.ledger_sequence <= ?
                   ORDER BY e.ledger_sequence""",
                (self._world_id, cursor.ledger_sequence),
            )
        )
        event_evidence: list[ReplayEventEvidence] = []
        commit_evidence: list[ReplayCommitEvidence] = []
        current_commit_id: str | None = None
        current_rows: list[sqlite3.Row] = []
        completed_commits: set[str] = set()

        def finish_commit() -> None:
            nonlocal current_commit_id, current_rows
            if current_commit_id is None:
                return
            if current_commit_id in completed_commits or not current_rows:
                raise LedgerIntegrityError("replay evidence commit rows are not contiguous")
            completed_commits.add(current_commit_id)
            events = tuple(
                WorldEvent.model_validate_json(str(row["event_json"])) for row in current_rows
            )
            if any(event.schema_version != CURRENT_SCHEMA_VERSION for event in events):
                raise LedgerIntegrityError("replay evidence requires current event envelopes")
            request_hash = str(current_rows[0]["request_hash"])
            if not hmac.compare_digest(commit_request_hash(events), request_hash):
                raise LedgerIntegrityError("replay evidence commit request hash does not match events")
            last = current_rows[-1]
            result = CommitResult.model_validate_json(str(last["result_json"]))
            expected_result = CommitResult(
                world_revision=int(last["world_revision"]),
                deliberation_revision=int(last["deliberation_revision"]),
                ledger_sequence=int(last["ledger_sequence"]),
                event_ids=tuple(event.event_id for event in events),
            )
            if result != expected_result:
                raise LedgerIntegrityError("replay evidence commit result does not match events")
            commit_evidence.append(
                ReplayCommitEvidence(
                    commit_id=current_commit_id,
                    request_hash=request_hash,
                    result=result,
                )
            )
            current_commit_id = None
            current_rows = []

        expected_sequence = 0
        for row in rows:
            sequence = int(row["ledger_sequence"])
            if sequence != expected_sequence + 1:
                raise LedgerIntegrityError("replay evidence event sequence is discontinuous")
            expected_sequence = sequence
            commit_id = str(row["commit_id"])
            if current_commit_id is not None and commit_id != current_commit_id:
                finish_commit()
            if current_commit_id is None:
                current_commit_id = commit_id
            event_json = row["event_json"]
            if not isinstance(event_json, str):
                raise LedgerIntegrityError("replay evidence event bytes are invalid")
            event_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(event_hash, str(row["event_hash"])):
                raise LedgerIntegrityError("replay evidence event envelope hash mismatch")
            event = WorldEvent.model_validate_json(event_json)
            if (
                event.schema_version != CURRENT_SCHEMA_VERSION
                or canonical_event_json(event) != event_json
                or event.world_id != self._world_id
                or event.event_id != row["event_id"]
                or event.idempotency_key != row["idempotency_key"]
            ):
                raise LedgerIntegrityError("replay evidence event envelope does not match row")
            event_evidence.append(
                ReplayEventEvidence(
                    event=event,
                    commit_id=commit_id,
                    cursor=ProjectionCursor(
                        world_revision=int(row["world_revision"]),
                        deliberation_revision=int(row["deliberation_revision"]),
                        ledger_sequence=sequence,
                    ),
                    event_envelope_hash=event_hash,
                )
            )
            current_rows.append(row)
        finish_commit()
        if expected_sequence != cursor.ledger_sequence:
            raise LedgerIntegrityError("replay evidence event tail does not match cursor")
        return tuple(event_evidence), tuple(commit_evidence)

    def _rebuild_locked(
        self,
        *,
        target_schema_version: str = CURRENT_SCHEMA_VERSION,
        reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
    ) -> LedgerProjection:
        rebuilt = self._replay_locked(
            target_cursor=None,
            target_schema_version=target_schema_version,
            reducer_bundle_version=reducer_bundle_version,
        )
        if rebuilt != self._project_locked():
            raise LedgerIntegrityError("rebuilt projection does not match persisted head")
        return rebuilt

    def _replay_locked(
        self,
        *,
        target_cursor: ProjectionCursor | None,
        target_schema_version: str,
        reducer_bundle_version: str,
    ) -> LedgerProjection:
        require_reducer_bundle(reducer_bundle_version)
        require_target_schema(target_schema_version)
        state = ReducerState()
        world_revision = 0
        deliberation_revision = 0
        ledger_sequence = 0
        legacy_trigger_sources: dict[str, str] = {}
        legacy_settlement_sources: dict[str, str] = {}
        legacy_plan_event_ids = {
            str(row["event_id"])
            for row in self._connection.execute(
                "SELECT event_id FROM world_v2_legacy_plan_events WHERE world_id = ?",
                (self._world_id,),
            )
        }
        rows = self._connection.execute(
            """SELECT * FROM world_v2_events WHERE world_id = ?
               ORDER BY ledger_sequence""",
            (self._world_id,),
        )
        for row in rows:
            if (
                target_cursor is not None
                and int(row["ledger_sequence"]) > target_cursor.ledger_sequence
            ):
                break
            event_json = row["event_json"]
            if not isinstance(event_json, str):
                raise LedgerIntegrityError("persisted event bytes are invalid")
            actual_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
            if actual_hash != row["event_hash"]:
                raise LedgerIntegrityError("event envelope hash mismatch")
            try:
                raw_event = json.loads(event_json)
                if not isinstance(raw_event, dict):
                    raise ValueError("event envelope must be an object")
                raw_event = _upcast_legacy_appraisal_trigger(
                    raw_event,
                    settlement_sources=legacy_settlement_sources,
                    trigger_sources=legacy_trigger_sources,
                )
                raw_event = _upcast_legacy_experience(raw_event)
                event = upcast_event(raw_event, target_schema_version=target_schema_version)
                if event.event_type == "AcceptanceRecorded":
                    has_manifest_version = "manifest_version" in event.payload()
                    try:
                        # Old bundles allowed arbitrary audit extensions on an
                        # Acceptance.  Keep it authoritative only when the
                        # already-replayed proposal state proves every current
                        # reducer precondition; otherwise preserve it as a
                        # revision-bearing, migration-only audit fact.
                        reduce_event(state, event)
                    except Exception:
                        if has_manifest_version:
                            raise
                        event = upcast_event(
                            {
                                **raw_event,
                                "event_type": "LegacyAcceptanceAuditRecorded",
                            },
                            target_schema_version=target_schema_version,
                        )
                if event.event_type not in {
                    "LegacyAcceptanceAuditRecorded",
                    "LegacyExperienceCommitted",
                }:
                    validate_event_identity(event)
                if (
                    event.world_id != self._world_id
                    or event.event_id != row["event_id"]
                    or event.idempotency_key != row["idempotency_key"]
                ):
                    raise LedgerIntegrityError(
                        "event envelope does not match its ledger row"
                    )
            except LedgerIntegrityError:
                raise
            except Exception as exc:
                raise LedgerIntegrityError("persisted event is invalid") from exc
            ledger_sequence += 1
            try:
                definition = event_definition(event.event_type)
            except Exception as exc:
                raise LedgerIntegrityError("persisted event type is invalid") from exc
            if definition.revision_class is RevisionClass.WORLD:
                world_revision += 1
            else:
                deliberation_revision += 1
            if (
                row["ledger_sequence"] != ledger_sequence
                or row["world_revision"] != world_revision
                or row["deliberation_revision"] != deliberation_revision
            ):
                raise LedgerIntegrityError("persisted event revisions are discontinuous")
            try:
                state = reduce_event(
                    state,
                    event,
                    allow_legacy_plan_owner=event.event_id in legacy_plan_event_ids,
                )
            except Exception as exc:
                raise LedgerIntegrityError("persisted event cannot be reduced") from exc
        if (
            target_cursor is not None
            and ProjectionCursor(
                world_revision=world_revision,
                deliberation_revision=deliberation_revision,
                ledger_sequence=ledger_sequence,
            )
            != target_cursor
        ):
            raise ValueError("requested projection cursor is not present in the ledger")
        return make_projection(
            world_id=self._world_id,
            world_revision=world_revision,
            deliberation_revision=deliberation_revision,
            ledger_sequence=ledger_sequence,
            state=state,
            reducer_bundle_version=reducer_bundle_version,
        )
