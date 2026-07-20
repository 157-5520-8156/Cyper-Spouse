from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
from pathlib import Path
import sqlite3
import time
from threading import RLock
from typing import Literal
from weakref import WeakKeyDictionary

from pydantic import BaseModel

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
    _expression_beat_semantic_dump,
    _expression_plan_manifest_semantic_dump,
    _expression_plan_semantic_dump,
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
from .sqlite_coordination import sqlite_write_lock
from .upcasting import CURRENT_SCHEMA_VERSION, require_target_schema, upcast_event


_LOG = logging.getLogger(__name__)


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
_V19_ONLY_STATE_KEYS = frozenset({"fact_commit_proposal_audits_v2", "acceptance_manifests_v3"})
_V20_ONLY_STATE_KEYS = frozenset(
    {
        "minimal_reply_manifests",
        "stored_message_payloads",
        "expression_plans",
        "expression_beats",
    }
)
_V24_ONLY_STATE_KEYS = frozenset({"expression_plan_manifests"})
_V25_ONLY_STATE_KEYS = frozenset({"provider_media_grants"})
_V26_ONLY_STATE_KEYS = frozenset(
    {"photo_candidates", "media_opportunities", "media_plans", "media_unrenderable_opportunity_ids"}
)
_V27_ONLY_STATE_KEYS = frozenset(
    {"media_artifacts", "media_inspections", "media_previews", "media_failed_plan_ids"}
)
_PREFIX_PROOF_VERSION = "world-v2-prefix-proof.2"
_PREVIOUS_PREFIX_PROOF_VERSION = "world-v2-prefix-proof.1"
_PREFIX_BITS_BYTES = 32
_MAX_PINNED_OBSERVATION_HISTORY_HANDLES = 1_024
# ``world_v2_heads.state_json`` holding this sentinel means the head state
# lives in ``world_v2_head_state_items`` as per-field/per-item canonical JSON
# fragments.  The sentinel is deliberately not valid state JSON so an older
# reader fails closed instead of silently projecting an empty world.
_HEAD_STATE_SENTINEL = "world-v2-head-state-items.1"
_HEAD_STATE_SCALAR_IDX = -1
# Incremental semantic-hash support.  For the current reducer bundle almost
# every ``semantic_payload`` field is the plain ``model_dump(mode="json")`` of
# the same-named state field, so its canonical JSON fragment is byte-identical
# to the split-storage state fragment and can be reused without re-dumping.
# The exceptions are pinned here; ``_warm_semantic_fragment_cache_locked``
# byte-verifies every field (and the assembled hash) against a full
# ``semantic_payload`` before the incremental path is allowed to run, so a
# future divergence in reducers.py disables the fast path instead of ever
# producing different bytes.
_SEMANTIC_HEADER_FIELDS = frozenset(
    {"schema_version", "world_id", "world_revision", "reducer_bundle_version"}
)
_SEMANTIC_CUSTOM_FIELDS = frozenset(
    {
        "logical_time",
        "expression_plans",
        "expression_beats",
        "expression_plan_manifests",
    }
)
# Semantic-payload keys whose presence toggles with field emptiness.
_SEMANTIC_CONDITIONAL_FIELDS = frozenset(
    {
        "appearance_states",
        "visible_physical_states",
        "aspirations",
        "expression_payload_descriptors",
    }
)


def _canonical_fragment(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_ready_item(item: object) -> object:
    """JSON-mode value of one tuple element, matching a whole-state dump.

    Reducer-state tuples hold either frozen Pydantic models or plain JSON
    scalars, and a direct ``model_dump`` of an element is byte-identical to
    the same element inside ``state.model_dump(mode="json")``.  Anything else
    fails closed rather than risking a fragment that diverges from the
    canonical whole-document encoding.
    """

    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    if item is None or isinstance(item, (str, int, float, bool)):
        return item
    raise LedgerIntegrityError("state tuple element cannot be serialized incrementally")


def _split_array_fragment_items(fragment: str) -> tuple[str, ...]:
    """Split one canonical JSON array fragment into canonical item fragments."""

    parsed = json.loads(fragment)
    if not isinstance(parsed, list):
        raise LedgerIntegrityError("head state array fragment is not a JSON array")
    return tuple(_canonical_fragment(item) for item in parsed)


def _assemble_state_json_from_fragments(fragments: dict[str, str]) -> str:
    """Rebuild the exact canonical (sorted-key) state JSON from field fragments."""

    return (
        "{"
        + ",".join(
            json.dumps(field_name, ensure_ascii=False) + ":" + fragments[field_name]
            for field_name in sorted(fragments)
        )
        + "}"
    )


def _head_state_item_rows_from_fragments(
    fragments: dict[str, str],
) -> list[tuple[str, int, str]]:
    """Expand field fragments into their per-item storage rows.

    Non-empty JSON arrays are stored one row per element (``idx`` = position)
    so a commit that appends or replaces single elements rewrites only those
    rows.  Every other fragment (objects, scalars, ``null`` and the empty
    array) is stored whole under ``idx`` = -1.
    """

    rows: list[tuple[str, int, str]] = []
    for field_name, fragment in fragments.items():
        if fragment.startswith("[") and fragment != "[]":
            for index, item_fragment in enumerate(_split_array_fragment_items(fragment)):
                rows.append((field_name, index, item_fragment))
        else:
            rows.append((field_name, _HEAD_STATE_SCALAR_IDX, fragment))
    return rows


def _head_state_fragments_from_item_rows(
    rows: Sequence[tuple[str, int, str]],
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Reassemble field fragments (and per-item fragments) from storage rows."""

    grouped: dict[str, list[tuple[int, str]]] = {}
    for field_name, index, item_json in rows:
        grouped.setdefault(field_name, []).append((int(index), item_json))
    fragments: dict[str, str] = {}
    item_fragments: dict[str, tuple[str, ...]] = {}
    for field_name, entries in grouped.items():
        entries.sort(key=lambda entry: entry[0])
        indexes = [index for index, _ in entries]
        if indexes == [_HEAD_STATE_SCALAR_IDX]:
            fragments[field_name] = entries[0][1]
            continue
        if indexes != list(range(len(entries))):
            raise LedgerIntegrityError(
                f"head state items for field {field_name!r} are not contiguous"
            )
        items = tuple(item_json for _, item_json in entries)
        fragments[field_name] = "[" + ",".join(items) + "]"
        item_fragments[field_name] = items
    return fragments, item_fragments


def load_head_state_json(connection: sqlite3.Connection, world_id: str) -> str:
    """Return one world head's full state JSON regardless of storage format.

    This is the shared read seam for diagnostics and tests: it understands
    both the legacy single-column format and the split per-item format, and
    always returns the complete state document as one JSON object string.
    """

    row = connection.execute(
        "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (world_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"world head {world_id!r} does not exist")
    state_json = row[0]
    if not isinstance(state_json, str):
        raise LedgerIntegrityError("head state is invalid")
    if state_json != _HEAD_STATE_SENTINEL:
        return state_json
    item_rows = tuple(
        (str(item[0]), int(item[1]), str(item[2]))
        for item in connection.execute(
            "SELECT field, idx, item_json FROM world_v2_head_state_items "
            "WHERE world_id = ? ORDER BY field, idx",
            (world_id,),
        )
    )
    if not item_rows:
        raise LedgerIntegrityError("split head state has no stored fields")
    fragments, _ = _head_state_fragments_from_item_rows(item_rows)
    return _assemble_state_json_from_fragments(fragments)


@dataclass(frozen=True, slots=True)
class _HeadStateWriteOps:
    """The exact per-item SQL mutations that persist one commit's state delta.

    ``full_rewrite`` replaces every stored row (legacy-format heads and
    non-incremental encodes).  Otherwise ``field_deletes`` drops fields whose
    representation changed or that left the canonical dump, ``tail_deletes``
    trims shrunken arrays, and ``upserts`` writes only changed rows.
    """

    full_rewrite: bool
    field_deletes: tuple[str, ...]
    tail_deletes: tuple[tuple[str, int], ...]
    upserts: tuple[tuple[str, int, str], ...]


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


@dataclass(frozen=True, slots=True)
class SQLiteProjectionPerformanceCounters:
    """Non-authoritative evidence about projection access shape.

    The counters never affect reads or writes.  Phase-8 performance tests use
    them to distinguish one-row persisted-head projection from historical
    event replay instead of inferring incrementality from wall-clock speed.
    """

    head_projection_reads: int
    head_projection_cache_hits: int
    project_at_head_hits: int
    historical_replay_calls: int
    total_replay_calls: int


@dataclass(frozen=True, slots=True)
class SQLiteWalMaintenanceResult:
    """One bounded, non-authoritative WAL maintenance attempt.

    SQLite's ``wal_checkpoint(PASSIVE)`` never waits for readers.  It may
    still find an active writer and leave frames in the WAL; callers should
    treat ``busy`` as a retry signal, not as a failed ledger operation.
    ``skipped`` means the WAL was below the threshold or the maintenance
    interval had not elapsed.  No result here changes World authority.
    """

    status: Literal["skipped", "checkpointed", "busy"]
    wal_bytes_before: int
    wal_bytes_after: int
    log_frames: int = 0
    checkpointed_frames: int = 0
    busy: bool = False


class SQLiteWorldLedger:
    """Independent crash-consistent persistence adapter for a single World v2 world."""

    def __init__(
        self,
        *,
        path: str | Path,
        world_id: str,
        accepted_batch_issuer: AcceptedLedgerBatchIssuer | None = None,
        latency_recorder: object | None = None,
    ) -> None:
        if not world_id:
            raise ValueError("world_id must not be empty")
        if (
            accepted_batch_issuer is not None
            and type(accepted_batch_issuer) is not AcceptedLedgerBatchIssuer
        ):
            raise TypeError("accepted batch issuer must use its exact capability type")
        self._world_id = world_id
        self._accepted_batch_issuer = accepted_batch_issuer
        self._latency_recorder = latency_recorder
        self._database_path = Path(path).expanduser().absolute()
        self._last_wal_maintenance_monotonic = 0.0
        self._thread_lock = RLock()
        self._database_write_lock = sqlite_write_lock(self._database_path)
        self._head_projection_reads = 0
        self._head_projection_cache_hits = 0
        self._project_at_head_hits = 0
        self._historical_replay_calls = 0
        self._total_replay_calls = 0
        self._head_projection_cache: LedgerProjection | None = None
        self._head_projection_cache_row_identity: tuple[object, ...] | None = None
        # A same-turn advisory may authenticate a proposal at the immediately
        # preceding cursor, then rebase it after Appraisal acceptance.  Those
        # two reads are immutable historical snapshots; retain a small bounded
        # cache so the second authority pass does not replay the full ledger.
        self._historical_projection_cache: dict[tuple[int, int, int], LedgerProjection] = {}
        self._head_state_cache: ReducerState | None = None
        self._head_state_cache_identity: tuple[int, int, int, str, str] | None = None
        # Canonical JSON fragments of the last committed state, keyed by its
        # state hash: per-field fragments plus per-item fragments for fields
        # stored as arrays.  They let the next commit re-serialize only the
        # items it changed instead of re-dumping the whole growing state.
        self._state_fragment_cache: (
            tuple[str, dict[str, str], dict[str, tuple[str, ...]]] | None
        ) = None
        # UTF-8 encodings of the state fragments above, keyed by the same
        # state hash.  The historical state-hash material is a 13MB+ byte
        # string; keeping per-field bytes lets a commit hash it by joining
        # unchanged chunks instead of re-encoding the whole document.
        self._state_fragment_bytes: tuple[str, dict[str, bytes]] | None = None
        # Canonical fragments of the current-bundle semantic payload keyed by
        # the head's semantic hash, plus the open-time proof that every plain
        # field is byte-identical to its state fragment.  Populated only after
        # ``_warm_semantic_fragment_cache_locked`` verified the assembly
        # against the persisted semantic hash.
        self._semantic_fragment_cache: tuple[str, dict[str, bytes]] | None = None
        self._semantic_sharing_verified = False
        # Encode-path diagnostics: regression tests pin that consecutive
        # commits never fall back to whole-state re-serialization.
        self._full_state_encode_count = 0
        self._incremental_state_encode_count = 0
        self._semantic_full_count = 0
        self._semantic_incremental_count = 0
        # Commit-scoped seam between ``_encode_state_and_hash`` and the
        # durable write: the previous state with its verified fragments, and
        # the per-item mutations the encode derived from the delta.
        self._incremental_state_base: (
            tuple[ReducerState, dict[str, str], dict[str, tuple[str, ...]]] | None
        ) = None
        self._pending_head_state_ops: _HeadStateWriteOps | None = None
        # Event envelopes and their owning CommitResult are immutable.  Keep
        # verified values process-local so overlapping Context slices/turns do
        # not re-query, re-project and re-hash the same commit hundreds of
        # times.  ``lookup_event_commit`` still checks SQLite data_version
        # before consulting this cache, so an unsupported cross-connection
        # mutation is rejected by the full-history verifier rather than hidden.
        self._verified_event_commit_cache: dict[str, tuple[WorldEvent, CommitResult]] = {}
        self._verified_observation_event_cache: dict[
            tuple[str, str, str], HistoricalLedgerEvent
        ] = {}
        # The cold replay already parses every current event envelope.  Keep
        # only a small tail so the hot-lookup cache can reuse those exact
        # model objects instead of reparsing the tail a second time during
        # startup.
        self._recent_replay_event_cache: dict[str, WorldEvent] = {}
        connection = sqlite3.connect(
            self._database_path, isolation_level=None, timeout=10, check_same_thread=False
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            # A full reducer snapshot is intentionally written in the same
            # transaction as its events.  SQLite's default 1,000-page WAL
            # auto-checkpoint can therefore run a multi-megabyte checkpoint
            # synchronously on the user's reply path.  WAL frames remain
            # crash-recoverable without a checkpoint; compaction is handled by
            # the bounded maintenance seam instead of this commit.
            connection.execute("PRAGMA wal_autocheckpoint = 0")
            self._connection = connection
            self._create_schema()
            self._ensure_head()
            source_bundle = str(
                self._connection.execute(
                    "SELECT reducer_bundle_version FROM world_v2_heads WHERE world_id = ?",
                    (self._world_id,),
                ).fetchone()[0]
            )
            # A short-lived v30 HTTP fixture wrote observations with the wall
            # clock instead of the then-current World Clock.  Keep those
            # immutable envelopes readable during the one-way migration, but
            # never permit the exception for newly appended events.
            self._legacy_clock_compat_event_ids = (
                self._legacy_clock_compatibility_event_ids(source_bundle)
                if source_bundle == "world-v2-reducers.30"
                else frozenset()
            )
            legacy_prefix_rebuild = self._head_bundle_requires_prefix_rebuild()
            self._migrate_head_bundle()
            self._migrate_head_state_storage()
            if legacy_prefix_rebuild:
                self._discard_legacy_prefix_proof_cache()
            # The derived prefix tables live in the same SQLite file as the
            # ledger.  They are therefore only an acceleration cache: before
            # they can anchor a process-local historical reader, independently
            # stream-verify immutable events, commits, revisions and head.
            self._verify_cold_ledger_history()
            self._ensure_or_restore_prefix_proof_state()
            # Cold start otherwise pays one whole-state re-serialization on
            # the first commit: pre-warm the per-field fragment caches from
            # the state the cold verification just validated.
            self._warm_encode_caches_locked()
            self._verified_data_version = self._sqlite_data_version_locked()
            self._verified_ledger_epoch = self._ledger_mutation_epoch_locked()
        except Exception:
            connection.close()
            raise

    def close(self) -> None:
        with self._database_write_lock, self._thread_lock:
            self._connection.close()

    def maintain_wal_if_needed(
        self,
        *,
        threshold_bytes: int = 8 * 1024 * 1024,
        min_interval_seconds: float = 5.0,
        blocking: bool = False,
    ) -> SQLiteWalMaintenanceResult:
        """Run at most one passive WAL checkpoint when maintenance is due.

        This method is intentionally synchronous because it owns a SQLite
        connection.  Production callers must invoke it from a scheduler
        worker (normally via ``asyncio.to_thread``), never while awaiting a
        visible reply.  A process-local writer lock serializes it with ledger
        and sidecar commits; SQLite remains the authority across processes.

        ``PASSIVE`` does not wait for readers and returns immediately when a
        competing writer prevents progress.  By default the process-local
        locks are also acquired non-blocking: a visible commit wins over
        maintenance and the next scheduler wake retries.  A blocking call is
        available for explicit offline maintenance only.
        """

        if threshold_bytes <= 0:
            raise ValueError("WAL maintenance threshold must be positive")
        if min_interval_seconds < 0:
            raise ValueError("WAL maintenance interval must not be negative")
        wal_path = Path(str(self._database_path) + "-wal")
        acquired_database_lock = self._database_write_lock.acquire(blocking=blocking)
        if not acquired_database_lock:
            return SQLiteWalMaintenanceResult(
                status="skipped", wal_bytes_before=0, wal_bytes_after=0
            )
        acquired_thread_lock = False
        try:
            acquired_thread_lock = self._thread_lock.acquire(blocking=blocking)
            if not acquired_thread_lock:
                return SQLiteWalMaintenanceResult(
                    status="skipped", wal_bytes_before=0, wal_bytes_after=0
                )
            now = time.monotonic()
            try:
                wal_bytes_before = wal_path.stat().st_size
            except FileNotFoundError:
                wal_bytes_before = 0
            if (
                wal_bytes_before < threshold_bytes
                or now - self._last_wal_maintenance_monotonic < min_interval_seconds
            ):
                return SQLiteWalMaintenanceResult(
                    status="skipped",
                    wal_bytes_before=wal_bytes_before,
                    wal_bytes_after=wal_bytes_before,
                )
            self._last_wal_maintenance_monotonic = now
            row = self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            if row is None or len(row) != 3:
                raise LedgerIntegrityError("SQLite WAL checkpoint result is unavailable")
            busy, log_frames, checkpointed_frames = (int(item) for item in row)
            if busy == 0 and checkpointed_frames >= log_frames and wal_bytes_before > (
                4 * threshold_bytes
            ):
                # Every frame is already backfilled, yet the file itself only
                # shrinks via TRUNCATE, which needs a reader-free instant.  A
                # brief bounded attempt on the scheduler lane opportunistically
                # reclaims a bloated WAL during quiet gaps; when a reader is
                # present it gives up within the timeout and the passive
                # result above still stands.
                self._connection.execute("PRAGMA busy_timeout = 300")
                try:
                    # The passive counters above remain the reported work;
                    # truncation only reclaims the file, and a busy result
                    # simply leaves the fully-checkpointed WAL for later.
                    self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                finally:
                    self._connection.execute("PRAGMA busy_timeout = 0")
            try:
                wal_bytes_after = wal_path.stat().st_size
            except FileNotFoundError:
                wal_bytes_after = 0
            # SQLite normally reports ``busy=1`` when a reader prevents a
            # passive checkpoint, but a concurrent writer may also append
            # frames between the checkpoint snapshot and its result.  Treat
            # any uncheckpointed frame count as a retry signal so maintenance
            # never claims the WAL is fully compacted when it is not.
            is_busy = busy != 0 or checkpointed_frames < log_frames
            return SQLiteWalMaintenanceResult(
                status="busy" if is_busy else "checkpointed",
                wal_bytes_before=wal_bytes_before,
                wal_bytes_after=wal_bytes_after,
                log_frames=log_frames,
                checkpointed_frames=checkpointed_frames,
                busy=is_busy,
            )
        finally:
            if acquired_thread_lock:
                self._thread_lock.release()
            self._database_write_lock.release()

    @property
    def world_id(self) -> str:
        return self._world_id

    @property
    def blocks_event_loop(self) -> bool:
        return True

    def performance_counters(self) -> SQLiteProjectionPerformanceCounters:
        """Return a concurrency-consistent diagnostic snapshot."""

        with self._thread_lock:
            return SQLiteProjectionPerformanceCounters(
                head_projection_reads=self._head_projection_reads,
                head_projection_cache_hits=self._head_projection_cache_hits,
                project_at_head_hits=self._project_at_head_hits,
                historical_replay_calls=self._historical_replay_calls,
                total_replay_calls=self._total_replay_calls,
            )

    def _sqlite_data_version_locked(self) -> int:
        row = self._connection.execute("PRAGMA data_version").fetchone()
        if row is None or type(row[0]) is not int:
            raise LedgerIntegrityError("SQLite data version is unavailable")
        return int(row[0])

    def _ledger_mutation_epoch_locked(self) -> int:
        row = self._connection.execute(
            "SELECT mutation_epoch FROM world_v2_ledger_mutation_epochs WHERE world_id = ?",
            (self._world_id,),
        ).fetchone()
        if row is None or type(row[0]) is not int:
            raise LedgerIntegrityError("SQLite ledger mutation epoch is unavailable")
        return int(row[0])

    def _refresh_verified_external_history_locked(self) -> None:
        """Reverify if another SQLite connection changed this database.

        ``PRAGMA data_version`` is connection-local and changes only for writes
        committed by a *different* connection. Normal single-writer hot reads
        remain addressed. A cross-process append or unsupported direct mutation
        pays one genesis verification before its rows join this process's
        trusted immutable prefix.
        """

        current = self._sqlite_data_version_locked()
        if current == self._verified_data_version:
            return
        # Life-content, expression-payload and other immutable sidecars share
        # the SQLite file but are not ledger authority.  Their commits advance
        # PRAGMA data_version as well; replaying all ledger history for such a
        # write caused a multi-second pause immediately before dispatch.  Core
        # ledger tables maintain a separate mutation epoch, so sidecar-only
        # changes can be acknowledged without weakening event/commit checks.
        if self._ledger_mutation_epoch_locked() == self._verified_ledger_epoch:
            self._verified_data_version = current
            return
        started = time.perf_counter()
        self._verified_observation_event_cache.clear()
        # A cross-connection ledger write may have replaced split state item
        # rows without touching the head row itself; re-derive the head from
        # durable bytes so the cold verification below cannot vouch for a
        # stale process-local projection.
        self._head_projection_cache = None
        self._head_projection_cache_row_identity = None
        self._head_state_cache = None
        self._head_state_cache_identity = None
        self._state_fragment_cache = None
        self._state_fragment_bytes = None
        self._semantic_fragment_cache = None
        self._verify_cold_ledger_history()
        self._warm_encode_caches_locked()
        _LOG.warning(
            "world v2 cold ledger reverify duration_ms=%.1f",
            (time.perf_counter() - started) * 1000,
        )
        self._ensure_or_restore_prefix_proof_state()
        self._verified_data_version = self._sqlite_data_version_locked()
        self._verified_ledger_epoch = self._ledger_mutation_epoch_locked()

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
            CREATE TABLE IF NOT EXISTS world_v2_ledger_mutation_epochs (
                world_id TEXT PRIMARY KEY,
                mutation_epoch INTEGER NOT NULL CHECK (mutation_epoch >= 0)
            );
            -- Split storage for the head reducer state: one row per top-level
            -- field (idx = -1) or per element of a non-empty tuple field
            -- (idx = 0..n-1).  A commit rewrites only the rows its events
            -- changed instead of one monotonically growing state_json row.
            CREATE TABLE IF NOT EXISTS world_v2_head_state_items (
                world_id TEXT NOT NULL,
                field TEXT NOT NULL,
                idx INTEGER NOT NULL CHECK (idx >= -1),
                item_json TEXT NOT NULL,
                PRIMARY KEY (world_id, field, idx),
                FOREIGN KEY (world_id) REFERENCES world_v2_heads(world_id)
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
            -- Commit-addressed event reads run on every ledger commit (prefix
            -- proof persistence) and on every verified source lookup.  Without
            -- this index each one was a full scan over all event envelopes,
            -- growing linearly with ledger history.  The trailing
            -- ledger_sequence column satisfies the readers' ORDER BY so the
            -- planner never falls back to the primary key scan for ordering.
            CREATE INDEX IF NOT EXISTS world_v2_events_commit_lookup
                ON world_v2_events (world_id, commit_id, ledger_sequence);
            CREATE TABLE IF NOT EXISTS world_v2_legacy_plan_events (
                world_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                source_reducer_bundle TEXT NOT NULL,
                PRIMARY KEY (world_id, event_id),
                FOREIGN KEY (world_id, event_id)
                    REFERENCES world_v2_events(world_id, event_id)
                DEFERRABLE INITIALLY DEFERRED
            );
            -- Explicit provenance for the one historical v30 clock seam.
            -- This is append-only compatibility metadata; it never changes
            -- immutable event bytes and is consulted only for those IDs.
            CREATE TABLE IF NOT EXISTS world_v2_legacy_clock_events (
                world_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                source_reducer_bundle TEXT NOT NULL,
                reason TEXT NOT NULL,
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
            CREATE TRIGGER IF NOT EXISTS world_v2_heads_epoch_insert
            AFTER INSERT ON world_v2_heads BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = NEW.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_heads_epoch_update
            AFTER UPDATE ON world_v2_heads BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = NEW.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_heads_epoch_delete
            AFTER DELETE ON world_v2_heads BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = OLD.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_commits_epoch_insert
            AFTER INSERT ON world_v2_commits BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = NEW.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_commits_epoch_update
            AFTER UPDATE ON world_v2_commits BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = NEW.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_commits_epoch_delete
            AFTER DELETE ON world_v2_commits BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = OLD.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_events_epoch_insert
            AFTER INSERT ON world_v2_events BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = NEW.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_events_epoch_update
            AFTER UPDATE ON world_v2_events BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = NEW.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_events_epoch_delete
            AFTER DELETE ON world_v2_events BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = OLD.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_head_state_items_epoch_insert
            AFTER INSERT ON world_v2_head_state_items BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = NEW.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_head_state_items_epoch_update
            AFTER UPDATE ON world_v2_head_state_items BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = NEW.world_id;
            END;
            CREATE TRIGGER IF NOT EXISTS world_v2_head_state_items_epoch_delete
            AFTER DELETE ON world_v2_head_state_items BEGIN
                UPDATE world_v2_ledger_mutation_epochs
                   SET mutation_epoch = mutation_epoch + 1 WHERE world_id = OLD.world_id;
            END;
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO world_v2_ledger_mutation_epochs (world_id, mutation_epoch) "
            "VALUES (?, 0)",
            (self._world_id,),
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
        if "storage_epoch" not in columns:
            # Monotonic per-row write counter.  With split state storage the
            # head row no longer embodies the state bytes, so this column is
            # the cheap row identity that invalidates process-local caches
            # without re-reading (or re-hashing) the multi-megabyte state.
            self._connection.execute(
                "ALTER TABLE world_v2_heads ADD COLUMN storage_epoch "
                "INTEGER NOT NULL DEFAULT 0"
            )

    def _legacy_clock_compatibility_event_ids(self, source_bundle: str) -> frozenset[str]:
        """Find the bounded v30 observation envelopes with stale wall time.

        This is migration discovery only.  It does not rewrite event bytes or
        alter the current reducer's clock invariant; the resulting IDs are
        accepted solely while replaying this pre-v31 history into the current
        reducer bundle.
        """

        persisted = {
            str(row["event_id"])
            for row in self._connection.execute(
                "SELECT event_id FROM world_v2_legacy_clock_events WHERE world_id = ?",
                (self._world_id,),
            )
        }
        if source_bundle != "world-v2-reducers.30":
            return frozenset(persisted)
        current_time = None
        drifted: set[str] = set()
        rows = self._connection.execute(
            "SELECT event_id, event_json FROM world_v2_events "
            "WHERE world_id = ? ORDER BY ledger_sequence",
            (self._world_id,),
        )
        for row in rows:
            try:
                raw_event = json.loads(str(row["event_json"]))
                event = upcast_event(raw_event, target_schema_version=CURRENT_SCHEMA_VERSION)
            except Exception:
                continue
            if event.event_type == "WorldStarted":
                current_time = event.logical_time
            elif event.event_type == "ClockAdvanced":
                payload = event.payload()
                current_time = payload.get("logical_time_to", event.logical_time)
            elif (
                event.event_type == "ObservationRecorded"
                and current_time is not None
                and event.logical_time != current_time
            ):
                drifted.add(str(row["event_id"]))
        return frozenset(persisted | drifted)

    @staticmethod
    def _state_dump(state: ReducerState) -> dict[str, object]:
        """Dump reducer state for durable bytes and hashes.

        ``aspirations`` was added to ``ReducerState`` within one bundle
        version.  Mirroring the semantic payload's conditionality, an empty
        tuple stays out of the dump entirely so every pre-existing head's
        persisted state hash remains byte-identical; the key appears only
        once a world actually plants an aspiration.
        """

        dumped = state.model_dump(mode="json")
        if not state.aspirations:
            dumped.pop("aspirations", None)
        return dumped

    @classmethod
    def _encode_state(cls, state: ReducerState) -> str:
        return json.dumps(cls._state_dump(state), ensure_ascii=False, separators=(",", ":"))

    def _state_hash(self, state: ReducerState, cursor: ProjectionCursor) -> str:
        encoded = json.dumps(
            {
                "cursor": cursor.model_dump(mode="json"),
                "reducer_bundle_version": REDUCER_BUNDLE_VERSION,
                "state": self._state_dump(state),
                "world_id": self._world_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _state_hash_material(
        self,
        *,
        canonical_state: str,
        cursor: ProjectionCursor,
        reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
        world_id: str | None = None,
    ) -> bytes:
        """Assemble the exact byte material the historical state hash covers."""

        cursor_json = json.dumps(
            cursor.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            '{"cursor":'
            + cursor_json
            + ',"reducer_bundle_version":'
            + json.dumps(reducer_bundle_version)
            + ',"state":'
            + canonical_state
            + ',"world_id":'
            + json.dumps(world_id if world_id is not None else self._world_id, ensure_ascii=False)
            + "}"
        ).encode("utf-8")

    def _encode_state_and_hash(
        self, state: ReducerState, cursor: ProjectionCursor
    ) -> tuple[str, str]:
        """Serialize the large reducer state once for both durable artifacts.

        The byte construction is exactly the existing sorted-key state-hash
        contract; only redundant Pydantic traversals are removed.  When
        ``_incremental_state_base`` carries the immediately preceding state
        with its verified canonical fragments, only changed fields — and for
        tuple fields only changed elements — are re-serialized, and the exact
        per-item storage mutations are left in ``_pending_head_state_ops``.
        """

        base = self._incremental_state_base
        self._incremental_state_base = None
        if base is None:
            self._full_state_encode_count += 1
            fragments = {
                field_name: _canonical_fragment(value)
                for field_name, value in self._state_dump(state).items()
            }
            item_fragments = {
                field_name: _split_array_fragment_items(fragment)
                for field_name, fragment in fragments.items()
                if fragment.startswith("[") and fragment != "[]"
            }
            ops = _HeadStateWriteOps(
                full_rewrite=True,
                field_deletes=(),
                tail_deletes=(),
                upserts=tuple(_head_state_item_rows_from_fragments(fragments)),
            )
        else:
            previous, fragments, item_fragments = base
            fragments = dict(fragments)
            item_fragments = dict(item_fragments)
            fragments, item_fragments, ops, _ = self._apply_state_delta_to_fragments(
                previous=previous,
                state=state,
                fragments=fragments,
                item_fragments=item_fragments,
            )
        canonical_state = _assemble_state_json_from_fragments(fragments)
        state_hash = hashlib.sha256(
            self._state_hash_material(canonical_state=canonical_state, cursor=cursor)
        ).hexdigest()
        self._state_fragment_cache = (state_hash, fragments, item_fragments)
        self._state_fragment_bytes = (
            state_hash,
            {name: fragment.encode("utf-8") for name, fragment in fragments.items()},
        )
        self._pending_head_state_ops = ops
        return canonical_state, state_hash

    def _state_bytes_map_for(
        self, state_hash: str, fragments: dict[str, str]
    ) -> dict[str, bytes]:
        """Return (building lazily) the UTF-8 chunks of one fragment set."""

        cached = self._state_fragment_bytes
        if cached is not None and cached[0] == state_hash:
            return cached[1]
        bytes_map = {name: fragment.encode("utf-8") for name, fragment in fragments.items()}
        self._state_fragment_bytes = (state_hash, bytes_map)
        return bytes_map

    def _state_hash_from_fragment_bytes(
        self, *, fragment_bytes: dict[str, bytes], cursor: ProjectionCursor
    ) -> str:
        """Hash the exact ``_state_hash_material`` bytes from per-field chunks.

        UTF-8 encoding distributes over concatenation, so joining the encoded
        fragments reproduces byte-for-byte the encoding of the assembled
        document; only fields changed by this commit were re-encoded.
        """

        cursor_json = json.dumps(
            cursor.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        state_chunks = b",".join(
            json.dumps(field_name, ensure_ascii=False).encode("utf-8")
            + b":"
            + fragment_bytes[field_name]
            for field_name in sorted(fragment_bytes)
        )
        material = (
            b'{"cursor":'
            + cursor_json.encode("utf-8")
            + b',"reducer_bundle_version":'
            + json.dumps(REDUCER_BUNDLE_VERSION).encode("utf-8")
            + b',"state":{'
            + state_chunks
            + b'},"world_id":'
            + json.dumps(self._world_id, ensure_ascii=False).encode("utf-8")
            + b"}"
        )
        return hashlib.sha256(material).hexdigest()

    def _encode_state_delta(
        self, state: ReducerState, cursor: ProjectionCursor
    ) -> tuple[str, tuple[str, ...]]:
        """Incremental commit encode: patch fragments, hash without reassembly.

        Unlike ``_encode_state_and_hash`` this never materializes the
        multi-megabyte canonical state string: unchanged fields contribute
        their cached UTF-8 chunks directly to the hash material.  Returns the
        state hash and the exact top-level fields this commit changed (the
        driver for the incremental semantic hash and projection reuse).
        """

        base = self._incremental_state_base
        self._incremental_state_base = None
        cache = self._state_fragment_cache
        if base is None or cache is None:
            raise LedgerIntegrityError("incremental state encode requires a verified base")
        previous, base_fragments, base_item_fragments = base
        previous_bytes = self._state_bytes_map_for(cache[0], base_fragments)
        fragments, item_fragments, ops, changed_fields = self._apply_state_delta_to_fragments(
            previous=previous,
            state=state,
            fragments=dict(base_fragments),
            item_fragments=dict(base_item_fragments),
        )
        fragment_bytes = dict(previous_bytes)
        for field_name in changed_fields:
            fragment = fragments.get(field_name)
            if fragment is None:
                fragment_bytes.pop(field_name, None)
            else:
                fragment_bytes[field_name] = fragment.encode("utf-8")
        state_hash = self._state_hash_from_fragment_bytes(
            fragment_bytes=fragment_bytes, cursor=cursor
        )
        self._state_fragment_cache = (state_hash, fragments, item_fragments)
        self._state_fragment_bytes = (state_hash, fragment_bytes)
        self._pending_head_state_ops = ops
        self._incremental_state_encode_count += 1
        return state_hash, changed_fields

    @staticmethod
    def _assemble_semantic_material(fragments: dict[str, bytes]) -> bytes:
        """Reassemble the exact sorted-key semantic JSON from field chunks."""

        return (
            b"{"
            + b",".join(
                b'"' + key.encode("ascii") + b'":' + fragments[key] for key in sorted(fragments)
            )
            + b"}"
        )

    def _semantic_custom_fragment(self, field_name: str, state: ReducerState) -> bytes:
        """Canonical bytes of the semantic fields that are not plain dumps."""

        if field_name == "logical_time":
            value: object = state.logical_time.isoformat() if state.logical_time else None
        elif field_name == "expression_plans":
            value = tuple(
                _expression_plan_semantic_dump(
                    item, reducer_bundle_version=REDUCER_BUNDLE_VERSION
                )
                for item in state.expression_plans
            )
        elif field_name == "expression_beats":
            value = tuple(
                _expression_beat_semantic_dump(
                    item, reducer_bundle_version=REDUCER_BUNDLE_VERSION
                )
                for item in state.expression_beats
            )
        elif field_name == "expression_plan_manifests":
            value = tuple(
                _expression_plan_manifest_semantic_dump(item)
                for item in state.expression_plan_manifests
            )
        else:
            raise LedgerIntegrityError(
                f"semantic field {field_name!r} has no incremental rule"
            )
        return _canonical_fragment(value).encode("utf-8")

    def _warm_encode_caches_locked(self) -> None:
        """Pre-warm per-field encode caches from the verified head state.

        Runs after cold verification (open and cross-connection reverify) so
        the first commit is already incremental instead of paying one
        whole-state re-serialization.  Only split heads at the current bundle
        qualify; every other shape keeps the fail-closed full path.
        """

        head = self._connection.execute(
            "SELECT * FROM world_v2_heads WHERE world_id = ?", (self._world_id,)
        ).fetchone()
        if (
            head is None
            or str(head["reducer_bundle_version"]) != REDUCER_BUNDLE_VERSION
            or head["state_json"] != _HEAD_STATE_SENTINEL
        ):
            return
        fragment_cache = self._state_fragment_cache
        if fragment_cache is None or fragment_cache[0] != str(head["state_hash"]):
            self._load_split_head_fragments_locked(head)
            fragment_cache = self._state_fragment_cache
        if fragment_cache is None:
            return
        state = self._head_state_cache
        head_state_identity = (
            int(head["world_revision"]),
            int(head["deliberation_revision"]),
            int(head["ledger_sequence"]),
            str(head["semantic_hash"]),
            str(head["state_hash"]),
        )
        if state is None or self._head_state_cache_identity != head_state_identity:
            state = self._decode_state(self._head_state_json_locked(head))
            self._head_state_cache = state
            self._head_state_cache_identity = head_state_identity
        self._state_bytes_map_for(fragment_cache[0], fragment_cache[1])
        self._warm_semantic_fragment_cache_locked(head=head, state=state)

    def _warm_semantic_fragment_cache_locked(
        self, *, head: sqlite3.Row, state: ReducerState
    ) -> None:
        """Byte-verify and install per-field semantic fragments for the head.

        One full ``semantic_payload`` proves, field by field, that the plain
        fields reuse the exact state-fragment bytes and the pinned custom
        fields reproduce their canonical dumps, then proves the assembled
        document hashes to the persisted semantic hash.  Any mismatch leaves
        incremental semantic hashing disabled rather than ever risking
        different bytes.
        """

        self._semantic_fragment_cache = None
        self._semantic_sharing_verified = False
        persisted_semantic_hash = head["semantic_hash"]
        fragment_cache = self._state_fragment_cache
        if not persisted_semantic_hash or fragment_cache is None:
            return
        state_bytes = self._state_bytes_map_for(fragment_cache[0], fragment_cache[1])
        payload = state.semantic_payload(
            world_id=self._world_id, world_revision=int(head["world_revision"])
        )
        fragments: dict[str, bytes] = {}
        for key, value in payload.items():
            candidate = _canonical_fragment(value).encode("utf-8")
            if key in _SEMANTIC_HEADER_FIELDS:
                fragments[key] = candidate
                continue
            if key in _SEMANTIC_CUSTOM_FIELDS:
                if self._semantic_custom_fragment(key, state) != candidate:
                    _LOG.warning(
                        "world v2 semantic field %r custom rule mismatch; "
                        "incremental semantic hashing disabled",
                        key,
                    )
                    return
                fragments[key] = candidate
                continue
            if state_bytes.get(key) != candidate:
                _LOG.warning(
                    "world v2 semantic field %r diverges from its state fragment; "
                    "incremental semantic hashing disabled",
                    key,
                )
                return
            fragments[key] = state_bytes[key]
        digest = hashlib.sha256(self._assemble_semantic_material(fragments)).hexdigest()
        if not hmac.compare_digest(digest, str(persisted_semantic_hash)):
            _LOG.warning(
                "world v2 semantic fragment assembly does not reproduce the head "
                "semantic hash; incremental semantic hashing disabled"
            )
            return
        self._semantic_fragment_cache = (digest, fragments)
        self._semantic_sharing_verified = True

    def _incremental_semantic_hash(
        self,
        *,
        head: sqlite3.Row,
        state: ReducerState,
        world_revision: int,
        changed_fields: tuple[str, ...],
    ) -> str | None:
        """Patch the verified semantic fragments with one commit's delta.

        Requires the cached fragments to be exactly the pre-commit head's
        semantic document and the freshly encoded state fragments to belong to
        the state being committed; otherwise the caller falls back to the full
        ``make_projection`` computation.
        """

        cache = self._semantic_fragment_cache
        if cache is None or not self._semantic_sharing_verified:
            return None
        previous_hash, previous_fragments = cache
        if not hmac.compare_digest(previous_hash, str(head["semantic_hash"])):
            return None
        state_bytes_entry = self._state_fragment_bytes
        fragment_cache = self._state_fragment_cache
        if (
            state_bytes_entry is None
            or fragment_cache is None
            or state_bytes_entry[0] != fragment_cache[0]
        ):
            return None
        state_bytes = state_bytes_entry[1]
        fragments = dict(previous_fragments)
        for field_name in changed_fields:
            if field_name in _SEMANTIC_CUSTOM_FIELDS:
                fragments[field_name] = self._semantic_custom_fragment(field_name, state)
            elif field_name in _SEMANTIC_CONDITIONAL_FIELDS:
                if getattr(state, field_name):
                    new_bytes = state_bytes.get(field_name)
                    if new_bytes is None:
                        return None
                    fragments[field_name] = new_bytes
                else:
                    fragments.pop(field_name, None)
            elif field_name in fragments:
                new_bytes = state_bytes.get(field_name)
                if new_bytes is None:
                    return None
                fragments[field_name] = new_bytes
            # Fields outside the semantic payload (trigger bookkeeping,
            # proposal audit lanes, ...) do not affect the semantic hash.
        fragments["world_revision"] = str(int(world_revision)).encode("ascii")
        digest = hashlib.sha256(self._assemble_semantic_material(fragments)).hexdigest()
        self._semantic_fragment_cache = (digest, fragments)
        self._semantic_incremental_count += 1
        return digest

    def _projection_for_commit(
        self,
        *,
        head: sqlite3.Row,
        state: ReducerState,
        world_revision: int,
        deliberation_revision: int,
        ledger_sequence: int,
        changed_fields: tuple[str, ...] | None,
    ) -> LedgerProjection:
        """Build the post-commit projection, reusing the previous head's work.

        When this commit was encoded incrementally and the process-local head
        projection matches the row this transaction verified, the projection
        is a bounded ``model_copy`` of the previous one plus an incremental
        semantic hash.  The projection's own integrity validators still run
        explicitly.  Every other shape falls back to ``make_projection``.
        """

        if changed_fields is not None:
            semantic_hash_value = self._incremental_semantic_hash(
                head=head,
                state=state,
                world_revision=world_revision,
                changed_fields=changed_fields,
            )
            base = self._head_projection_cache
            raw_state = head["state_json"]
            expected_identity = (
                int(head["world_revision"]),
                int(head["deliberation_revision"]),
                int(head["ledger_sequence"]),
                str(head["semantic_hash"]),
                str(head["state_hash"]),
                (
                    int(head["storage_epoch"])
                    if raw_state == _HEAD_STATE_SENTINEL
                    else hashlib.sha256(raw_state.encode("utf-8")).digest()
                ),
            )
            if (
                semantic_hash_value is not None
                and base is not None
                and self._head_projection_cache_row_identity == expected_identity
            ):
                update: dict[str, object] = {
                    name: getattr(state, name)
                    for name in changed_fields
                    if name in LedgerProjection.model_fields
                }
                update["world_revision"] = world_revision
                update["deliberation_revision"] = deliberation_revision
                update["ledger_sequence"] = ledger_sequence
                update["semantic_hash"] = semantic_hash_value
                projection = base.model_copy(update=update)
                # ``model_copy`` skips validation; replay the projection's own
                # integrity validators so a reducer bug still fails the commit
                # exactly like a full construction would.
                projection.datetimes_are_timezone_aware()
                projection.pending_index_matches_actions()
                return projection
        self._semantic_full_count += 1
        projection = make_projection(
            world_id=self._world_id,
            world_revision=world_revision,
            deliberation_revision=deliberation_revision,
            ledger_sequence=ledger_sequence,
            state=state,
        )
        if self._semantic_sharing_verified:
            # Re-anchor the fragment cache on the full computation so one
            # fallback commit does not disable the incremental path forever.
            payload = state.semantic_payload(
                world_id=self._world_id, world_revision=world_revision
            )
            fragments = {
                key: _canonical_fragment(value).encode("utf-8")
                for key, value in payload.items()
            }
            digest = hashlib.sha256(self._assemble_semantic_material(fragments)).hexdigest()
            if hmac.compare_digest(digest, projection.semantic_hash):
                self._semantic_fragment_cache = (digest, fragments)
            else:
                self._semantic_fragment_cache = None
                self._semantic_sharing_verified = False
                _LOG.warning(
                    "world v2 semantic fragment reassembly diverged from "
                    "make_projection; incremental semantic hashing disabled"
                )
        return projection

    def _apply_state_delta_to_fragments(
        self,
        *,
        previous: ReducerState,
        state: ReducerState,
        fragments: dict[str, str],
        item_fragments: dict[str, tuple[str, ...]],
    ) -> tuple[dict[str, str], dict[str, tuple[str, ...]], _HeadStateWriteOps, tuple[str, ...]]:
        """Patch verified previous-state fragments with one commit's delta.

        Reducers use immutable top-level tuples and typically append or
        replace single elements, so unchanged fields — and unchanged elements
        inside a changed tuple — retain object identity across
        ``model_copy(update=...)``.  The identity/equality prefix comparison
        finds the exact changed elements; only those values are re-serialized.
        The final element of the return tuple lists the top-level fields whose
        canonical fragment actually changed.
        """

        field_deletes: list[str] = []
        tail_deletes: list[tuple[str, int]] = []
        upserts: list[tuple[str, int, str]] = []
        include_map: dict[str, object] = {}
        item_changes: dict[str, tuple[int, ...]] = {}
        changed_fields: list[str] = []
        for field_name in ReducerState.model_fields:
            old_value = getattr(previous, field_name)
            new_value = getattr(state, field_name)
            if old_value is new_value:
                continue
            if field_name == "aspirations" and not state.aspirations:
                # `_state_dump` keeps an empty aspirations tuple out of the
                # durable bytes for hash compatibility.
                if fragments.pop(field_name, None) is not None:
                    field_deletes.append(field_name)
                    changed_fields.append(field_name)
                item_fragments.pop(field_name, None)
                continue
            old_items = item_fragments.get(field_name)
            if (
                type(old_value) is tuple
                and type(new_value) is tuple
                and new_value
                and old_items is not None
                and len(old_items) == len(old_value)
            ):
                shared = min(len(old_value), len(new_value))
                changed_indexes = tuple(
                    index
                    for index in range(shared)
                    if old_value[index] is not new_value[index]
                    and old_value[index] != new_value[index]
                ) + tuple(range(shared, len(new_value)))
                if not changed_indexes and len(new_value) == len(old_value):
                    continue
                item_changes[field_name] = changed_indexes
            else:
                if old_value == new_value:
                    continue
                include_map[field_name] = True
        # A whole-state ``model_dump`` walks every element of every tuple even
        # under an ``include`` filter, which grows with world age.  Changed
        # tuple elements are therefore dumped directly; the bounded include
        # dump remains only for whole-field (non-tuple-delta) changes.
        dumped: dict[str, object] = (
            state.model_dump(mode="json", include=include_map) if include_map else {}
        )
        for field_name in sorted(set(include_map) | set(item_changes)):
            new_value = getattr(state, field_name)
            if field_name in item_changes:
                changed_indexes = item_changes[field_name]
                old_items = item_fragments[field_name]
                new_items = list(old_items[: len(new_value)])
                new_items.extend([""] * (len(new_value) - len(new_items)))
                for index in sorted(changed_indexes):
                    item_fragment = _canonical_fragment(_json_ready_item(new_value[index]))
                    new_items[index] = item_fragment
                    upserts.append((field_name, index, item_fragment))
                if len(new_value) < len(old_items):
                    tail_deletes.append((field_name, len(new_value)))
                item_fragments[field_name] = tuple(new_items)
                fragments[field_name] = "[" + ",".join(new_items) + "]"
                changed_fields.append(field_name)
            else:
                fragment = _canonical_fragment(dumped[field_name])
                if fragments.get(field_name) == fragment:
                    continue
                # The representation may flip between per-item rows and one
                # whole-fragment row; drop the old rows and rewrite the field.
                field_deletes.append(field_name)
                changed_fields.append(field_name)
                fragments[field_name] = fragment
                if fragment.startswith("[") and fragment != "[]":
                    items = _split_array_fragment_items(fragment)
                    item_fragments[field_name] = items
                    upserts.extend(
                        (field_name, index, item) for index, item in enumerate(items)
                    )
                else:
                    item_fragments.pop(field_name, None)
                    upserts.append((field_name, _HEAD_STATE_SCALAR_IDX, fragment))
        ops = _HeadStateWriteOps(
            full_rewrite=False,
            field_deletes=tuple(field_deletes),
            tail_deletes=tuple(tail_deletes),
            upserts=tuple(upserts),
        )
        return fragments, item_fragments, ops, tuple(changed_fields)

    def _load_split_head_fragments_locked(
        self, head: sqlite3.Row
    ) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
        """Load the per-item fragments of one split head row.

        A current-bundle head must reproduce the durable ``state_hash`` from
        its reassembled canonical bytes, or the read fails closed.  A head
        that claims an older reducer bundle cannot be byte-verified here (its
        hash contract used another bundle string); the bundle migration's
        semantic-hash check and full replay remain its authority.
        """

        item_rows = tuple(
            (str(row["field"]), int(row["idx"]), str(row["item_json"]))
            for row in self._connection.execute(
                "SELECT field, idx, item_json FROM world_v2_head_state_items "
                "WHERE world_id = ? ORDER BY field, idx",
                (self._world_id,),
            )
        )
        if not item_rows:
            raise LedgerIntegrityError("split head state has no stored fields")
        fragments, item_fragments = _head_state_fragments_from_item_rows(item_rows)
        if str(head["reducer_bundle_version"]) != REDUCER_BUNDLE_VERSION:
            return fragments, item_fragments
        cursor = ProjectionCursor(
            world_revision=int(head["world_revision"]),
            deliberation_revision=int(head["deliberation_revision"]),
            ledger_sequence=int(head["ledger_sequence"]),
        )
        material = self._state_hash_material(
            canonical_state=_assemble_state_json_from_fragments(fragments),
            cursor=cursor,
        )
        persisted_state_hash = head["state_hash"]
        if not persisted_state_hash or not hmac.compare_digest(
            hashlib.sha256(material).hexdigest(), str(persisted_state_hash)
        ):
            raise LedgerIntegrityError("split head state does not match its state hash")
        self._state_fragment_cache = (
            str(persisted_state_hash),
            fragments,
            item_fragments,
        )
        return fragments, item_fragments

    def _head_state_json_locked(self, head: sqlite3.Row) -> str:
        """Return the head's full state JSON for either storage format."""

        raw_state = head["state_json"]
        if not isinstance(raw_state, str):
            raise LedgerIntegrityError("head state is invalid")
        if raw_state != _HEAD_STATE_SENTINEL:
            return raw_state
        fragments, _ = self._load_split_head_fragments_locked(head)
        return _assemble_state_json_from_fragments(fragments)

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
            owns_transaction = not connection.in_transaction
            try:
                if owns_transaction:
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
                # Cold verification must remain fail-closed, but it does not
                # need to issue a new SQLite query for every commit and every
                # legacy-event membership check.  These rows are loaded inside
                # the same read transaction as the replay, so the batch is
                # still one immutable snapshot; only the query shape changes.
                commit_rows = {
                    str(row["commit_id"]): row
                    for row in connection.execute(
                        "SELECT commit_id, request_hash, result_json "
                        "FROM world_v2_commits WHERE world_id = ?",
                        (self._world_id,),
                    )
                }
                event_rows_by_commit: dict[str, list[sqlite3.Row]] = {}
                for event_row in connection.execute(
                    "SELECT * FROM world_v2_events WHERE world_id = ? ORDER BY ledger_sequence",
                    (self._world_id,),
                ):
                    event_rows_by_commit.setdefault(str(event_row["commit_id"]), []).append(
                        event_row
                    )
                legacy_plan_event_ids = {
                    str(row["event_id"])
                    for row in connection.execute(
                        "SELECT event_id FROM world_v2_legacy_plan_events WHERE world_id = ?",
                        (self._world_id,),
                    )
                }
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
                    commit_id = str(row["commit_id"])
                    self._verify_cold_commit_locked(
                        commit_id,
                        expected_cursor=head_cursor,
                        commit_row=commit_rows.get(commit_id),
                        event_rows=tuple(event_rows_by_commit.get(commit_id, ())),
                        legacy_plan_event_ids=legacy_plan_event_ids,
                    )
                    self._cache_verified_commit_rows_locked(
                        commit_id,
                        commit_row=commit_rows.get(commit_id),
                        event_rows=tuple(event_rows_by_commit.get(commit_id, ())),
                        # Context's recent-dialogue and authority slices are
                        # bounded by item count, but their source events are
                        # not necessarily the last physical ledger rows (a
                        # long action/settlement tail can sit between two
                        # observations).  Keep a bounded, useful prefix of
                        # verified envelopes so a hot Context build does not
                        # re-verify one old commit per retained message.
                        minimum_sequence=max(0, head_cursor.ledger_sequence - 4096),
                    )
                    previous_last_sequence = last_sequence
                    verified_commit_count += 1
                if verified_commit_count != commit_count:
                    raise LedgerIntegrityError(
                        "prefix proof rebuild found an empty or orphaned commit"
                    )
                if owns_transaction:
                    connection.commit()
            except sqlite3.DatabaseError as exc:
                if owns_transaction:
                    try:
                        connection.rollback()
                    except sqlite3.DatabaseError:
                        pass
                raise LedgerIntegrityError("cold ledger verification failed") from exc
            except Exception:
                if owns_transaction:
                    try:
                        connection.rollback()
                    except sqlite3.DatabaseError:
                        pass
                raise

    def _cache_verified_commit_rows_locked(
        self,
        commit_id: str,
        *,
        commit_row: sqlite3.Row | None,
        event_rows: tuple[sqlite3.Row, ...],
        minimum_sequence: int = 0,
    ) -> None:
        """Install current-format cold-verified rows for exact hot lookups.

        Context compilation repeatedly asks for the same recent observation,
        expression, and source events.  Cold verification has already checked
        every envelope and commit/result binding, so reparsing the owning
        commit once per source during a hot turn adds no authority.  Legacy
        envelopes deliberately stay uncached and continue through the normal
        migration-aware lookup path.
        """

        if commit_row is None or not event_rows:
            return
        event_rows = tuple(
            row for row in event_rows if int(row["ledger_sequence"]) >= minimum_sequence
        )
        if not event_rows:
            return
        try:
            events = tuple(
                self._recent_replay_event_cache.get(str(row["event_id"]))
                or WorldEvent.model_validate_json(str(row["event_json"]))
                for row in event_rows
            )
            if any(event.schema_version != CURRENT_SCHEMA_VERSION for event in events):
                return
            result = CommitResult.model_validate_json(str(commit_row["result_json"]))
        except Exception:
            # The preceding cold verifier is authoritative.  This cache is
            # merely an optimization and must never turn a verified reopen
            # into a startup failure.
            return
        for event in events:
            self._verified_event_commit_cache[event.event_id] = (event, result)

    def _verify_cold_commit_locked(
        self,
        commit_id: str,
        *,
        expected_cursor: ProjectionCursor,
        commit_row: sqlite3.Row | None = None,
        event_rows: tuple[sqlite3.Row, ...] | None = None,
        legacy_plan_event_ids: set[str] | None = None,
    ) -> None:
        """Verify a persisted commit without reinterpreting legacy event bytes.

        Current identity and reducer semantics are already checked by the
        genesis replay.  This companion check binds the exact stored envelope
        bytes to its original request/result record.  It intentionally avoids
        applying the *current* event-identity rule to legacy rows whose replay
        path performs a documented upcast before validation.
        """

        if commit_row is None:
            commit_row = self._connection.execute(
                """SELECT request_hash, result_json FROM world_v2_commits
                   WHERE world_id = ? AND commit_id = ?""",
                (self._world_id, commit_id),
            ).fetchone()
        if commit_row is None:
            raise LedgerIntegrityError("event owning commit is missing")
        rows = (
            event_rows
            if event_rows is not None
            else tuple(
                self._connection.execute(
                    """SELECT * FROM world_v2_events WHERE world_id = ? AND commit_id = ?
                       ORDER BY ledger_sequence""",
                    (self._world_id, commit_id),
                )
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
                    or int(row["deliberation_revision"]) > expected_cursor.deliberation_revision
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
                        raw_payload = (
                            json.loads(payload_json) if isinstance(payload_json, str) else None
                        )
                    except (TypeError, json.JSONDecodeError):
                        raw_payload = None
                    if not isinstance(raw_payload, dict) or "manifest_version" not in raw_payload:
                        # The replay path explicitly converts these pre-v18
                        # audit records to inert legacy events.  Their old
                        # request hash cannot be checked using v18 bytes.
                        legacy_bytes_present = True
                is_legacy_plan_event = (
                    str(event_id) in legacy_plan_event_ids
                    if legacy_plan_event_ids is not None
                    else self._connection.execute(
                        """SELECT 1 FROM world_v2_legacy_plan_events
                           WHERE world_id = ? AND event_id = ?""",
                        (self._world_id, str(event_id)),
                    ).fetchone()
                    is not None
                )
                if is_legacy_plan_event:
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
            elif (
                prefix_head is not None
                and str(prefix_head["proof_version"]) != _PREFIX_PROOF_VERSION
            ):
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
                        raise LedgerIntegrityError(
                            "legacy ledger commit/event rows are inconsistent"
                        )
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
            if (
                root_row is None
                and self._connection.execute(
                    """SELECT 1 FROM world_v2_prefix_locator_values
                   WHERE world_id = ? LIMIT 1""",
                    (self._world_id,),
                ).fetchone()
                is not None
            ):
                raise LedgerIntegrityError("prefix locator root node is missing")
            actual_locator_root = (
                expected_locator_root if root_row is None else bytes(root_row["node_hash"])
            )
            if not hmac.compare_digest(actual_locator_root, expected_locator_root):
                raise LedgerIntegrityError("prefix locator root does not match persisted head")
            checkpoint_rows = tuple(
                self._connection.execute(
                    """SELECT * FROM world_v2_prefix_checkpoints
                       WHERE world_id = ? ORDER BY ledger_sequence""",
                    (self._world_id,),
                )
            )
            if (
                len(checkpoint_rows) != commit_count
                or int(prefix_head["checkpoint_count"]) != commit_count
            ):
                raise LedgerIntegrityError("prefix checkpoint count does not match ledger")
            head = self._connection.execute(
                "SELECT world_revision, deliberation_revision, ledger_sequence FROM world_v2_heads WHERE world_id = ?",
                (self._world_id,),
            ).fetchone()
            for row in checkpoint_rows:
                checkpoint = self._prefix_checkpoint_from_row(row)
                leaf_index = checkpoint.mmr_leaf_count - 1
                if (
                    leaf_index < 0
                    or self._prefix_mmr_node_lookup_locked(0, leaf_index) != checkpoint.digest()
                ):
                    raise LedgerIntegrityError("prefix checkpoint MMR leaf is invalid")
            if checkpoint_rows:
                latest = checkpoint_rows[-1]
                if (
                    int(latest["world_revision"]),
                    int(latest["deliberation_revision"]),
                    int(latest["ledger_sequence"]),
                ) != (
                    int(head["world_revision"]),
                    int(head["deliberation_revision"]),
                    int(head["ledger_sequence"]),
                ):
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
                (int(row["depth"]), _prefix_bits_int(bytes(row["prefix_bits"]))): bytes(
                    row["node_hash"]
                )
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
            if (
                len(checkpoint_rows) != commit_count
                or int(prefix_head["checkpoint_count"]) != commit_count
            ):
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
                ) != (
                    int(head["world_revision"]),
                    int(head["deliberation_revision"]),
                    int(head["ledger_sequence"]),
                ):
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
            (
                (self._world_id, node_height, node_index, mmr.nodes[(node_height, node_index)])
                for node_height, node_index in addresses
            ),
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
            (
                (
                    self._world_id,
                    depth,
                    _prefix_bits_blob(prefix),
                    locator_map.nodes[(depth, prefix)],
                )
                for depth, prefix in addresses
            ),
        )
        self._connection.execute(
            """INSERT INTO world_v2_prefix_locator_values
                 (world_id, locator_key, value_hash, observation_id, event_type, event_id,
                  ledger_sequence, world_revision, deliberation_revision, event_leaf_index,
                  event_leaf_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._world_id,
                key,
                value.digest(),
                value.observation_id,
                value.event_type,
                value.event_id,
                value.ledger_sequence,
                value.world_revision,
                value.deliberation_revision,
                value.event_leaf_index,
                value.event_leaf_hash,
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
            if (
                event.world_id != self._world_id
                or event.event_id != row["event_id"]
                or event.idempotency_key != row["idempotency_key"]
            ):
                raise LedgerIntegrityError("legacy event envelope does not match ledger row")
            if current_commit is None:
                current_commit = row_commit
            current_commit = row_commit
            leaf_hash = LedgerLeafV1(
                world_id=self._world_id,
                ledger_sequence=int(row["ledger_sequence"]),
                world_revision=int(row["world_revision"]),
                deliberation_revision=int(row["deliberation_revision"]),
                commit_id=row_commit,
                event_id=event.event_id,
                idempotency_key=event.idempotency_key,
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
                    observation_id=observation_id,
                    event_type=event.event_type,
                    event_id=event.event_id,
                    ledger_sequence=int(row["ledger_sequence"]),
                    world_revision=int(row["world_revision"]),
                    deliberation_revision=int(row["deliberation_revision"]),
                    event_leaf_index=leaf_index,
                    event_leaf_hash=leaf_hash,
                )
                self._persist_prefix_locator_put_locked(
                    locator_map,
                    key=observation_locator_key(
                        world_id=self._world_id,
                        event_type=event.event_type,
                        idempotency_key=event.idempotency_key,
                    ),
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
                    + tuple((depth, key_int >> (256 - depth)) for depth in range(255, -1, -1))
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
            locator_root=locator_map.root.hex(),
            mmr_leaf_count=mmr.leaf_count + 1,
        )
        self._persist_prefix_mmr_append_locked(mmr, checkpoint.digest())
        self._connection.execute(
            """INSERT INTO world_v2_prefix_checkpoints
                 (world_id, world_revision, deliberation_revision, ledger_sequence, commit_id,
                  first_ledger_sequence, last_ledger_sequence, request_hash, result_hash,
                  ordered_event_ids_hash, locator_root, mmr_leaf_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._world_id,
                checkpoint.world_revision,
                checkpoint.deliberation_revision,
                checkpoint.last_ledger_sequence,
                checkpoint.commit_id,
                checkpoint.first_ledger_sequence,
                checkpoint.last_ledger_sequence,
                checkpoint.request_hash,
                checkpoint.result_hash,
                checkpoint.ordered_event_ids_hash,
                bytes.fromhex(checkpoint.locator_root),
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
                state = self._decode_state(self._head_state_json_locked(head))
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
                "world-v2-reducers.30",
                "world-v2-reducers.31",
                "world-v2-reducers.24",
                "world-v2-reducers.25",
                "world-v2-reducers.26",
                "world-v2-reducers.27",
                "world-v2-reducers.28",
                "world-v2-reducers.29",
                "world-v2-reducers.30",
                "world-v2-reducers.31",
                "world-v2-reducers.31",
                REDUCER_BUNDLE_VERSION,
            }:
                raise LedgerIntegrityError(
                    f"head reducer bundle {installed!r} has no migration path"
                )
            rebuilt: LedgerProjection | None = None
            if installed != REDUCER_BUNDLE_VERSION:
                legacy_hash = self._legacy_semantic_hash(
                    state_json=self._head_state_json_locked(head),
                    world_revision=world_revision,
                    reducer_bundle_version=installed,
                )
                if not hmac.compare_digest(legacy_hash, str(head["semantic_hash"])):
                    if installed != "world-v2-reducers.30":
                        raise LedgerIntegrityError("legacy head semantic hash is invalid")
                    # v30's HTTP fixture persisted a derived head hash from a
                    # pre-release semantic payload.  Do not trust or repair
                    # that cache in place: replay the immutable event history
                    # first, with only the discovered stale-clock envelopes
                    # admitted by the bounded compatibility seam below.  If
                    # replay fails, the old head remains a hard integrity
                    # failure just like every other bundle.
                    try:
                        rebuilt = self._replay_locked(
                            target_cursor=cursor,
                            target_schema_version=CURRENT_SCHEMA_VERSION,
                            reducer_bundle_version=REDUCER_BUNDLE_VERSION,
                        )
                    except Exception as exc:
                        raise LedgerIntegrityError("legacy head semantic hash is invalid") from exc
                self._mark_legacy_ownerless_plan_events_locked(installed)
            if rebuilt is None:
                rebuilt = self._replay_locked(
                    target_cursor=cursor,
                    target_schema_version=CURRENT_SCHEMA_VERSION,
                    reducer_bundle_version=REDUCER_BUNDLE_VERSION,
                )
            if installed == "world-v2-reducers.30" and self._legacy_clock_compat_event_ids:
                connection.executemany(
                    """INSERT OR IGNORE INTO world_v2_legacy_clock_events
                       (world_id, event_id, source_reducer_bundle, reason)
                       VALUES (?, ?, ?, ?)""",
                    (
                        (self._world_id, event_id, installed, "observation_logical_time_drift")
                        for event_id in self._legacy_clock_compat_event_ids
                    ),
                )
            rebuilt_state = self._state_from_projection(rebuilt)
            # The bundle migration writes one legacy full-text head; stale
            # split rows from the previous bundle must not survive next to
            # it.  The storage migration re-splits this row immediately after
            # inside its own transaction.
            connection.execute(
                "DELETE FROM world_v2_head_state_items WHERE world_id = ?",
                (self._world_id,),
            )
            updated = connection.execute(
                """UPDATE world_v2_heads
                   SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?,
                       state_hash = ?, storage_epoch = storage_epoch + 1
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

    def _migrate_head_state_storage(self) -> None:
        """Split legacy full-row head states into per-item rows, atomically.

        The split representation re-canonicalizes each top-level field with
        the exact sorted-key contract the state hash covers, so a row is only
        converted when its reassembled bytes reproduce the durable
        ``state_hash`` under that row's own reducer bundle version.  This
        ledger's world fails closed on a mismatch; other worlds sharing the
        file are converted opportunistically and left legacy when they cannot
        be verified (their own ledger migrates or rejects them on open).
        """

        connection = self._connection
        candidates = [
            str(row[0])
            for row in connection.execute(
                "SELECT world_id FROM world_v2_heads WHERE state_json != ?",
                (_HEAD_STATE_SENTINEL,),
            )
        ]
        if not candidates:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            for world_id in candidates:
                head = connection.execute(
                    "SELECT * FROM world_v2_heads WHERE world_id = ?", (world_id,)
                ).fetchone()
                if head is None:
                    continue
                raw_state = head["state_json"]
                persisted_state_hash = head["state_hash"]
                own_world = world_id == self._world_id
                if (
                    not isinstance(raw_state, str)
                    or raw_state == _HEAD_STATE_SENTINEL
                    or not persisted_state_hash
                ):
                    if own_world:
                        raise LedgerIntegrityError(
                            "world head cannot be split without a verified state hash"
                        )
                    continue
                cursor = ProjectionCursor(
                    world_revision=int(head["world_revision"]),
                    deliberation_revision=int(head["deliberation_revision"]),
                    ledger_sequence=int(head["ledger_sequence"]),
                )

                def _verified_fragments(fragments: dict[str, str]) -> bool:
                    material = self._state_hash_material(
                        canonical_state=_assemble_state_json_from_fragments(fragments),
                        cursor=cursor,
                        reducer_bundle_version=str(head["reducer_bundle_version"]),
                        world_id=world_id,
                    )
                    return hmac.compare_digest(
                        hashlib.sha256(material).hexdigest(), str(persisted_state_hash)
                    )

                fragments: dict[str, str] | None = None
                try:
                    parsed = json.loads(raw_state)
                    if isinstance(parsed, dict):
                        candidate = {
                            field_name: _canonical_fragment(value)
                            for field_name, value in parsed.items()
                        }
                        if _verified_fragments(candidate):
                            fragments = candidate
                except Exception:
                    fragments = None
                if fragments is None and own_world:
                    # The raw bytes could not be re-canonicalized to the exact
                    # hash contract (they may predate canonical persistence).
                    # This world is already at the current bundle, so derive
                    # the fragments from the validated model instead.
                    state = self._decode_state(raw_state)
                    candidate = {
                        field_name: _canonical_fragment(value)
                        for field_name, value in self._state_dump(state).items()
                    }
                    if _verified_fragments(candidate):
                        fragments = candidate
                if fragments is None:
                    if own_world:
                        raise LedgerIntegrityError(
                            "legacy head state does not match its state hash"
                        )
                    continue
                item_rows = _head_state_item_rows_from_fragments(fragments)
                connection.execute(
                    "DELETE FROM world_v2_head_state_items WHERE world_id = ?",
                    (world_id,),
                )
                connection.executemany(
                    "INSERT INTO world_v2_head_state_items "
                    "(world_id, field, idx, item_json) VALUES (?, ?, ?, ?)",
                    (
                        (world_id, field_name, index, item_json)
                        for field_name, index, item_json in item_rows
                    ),
                )
                connection.execute(
                    "UPDATE world_v2_heads SET state_json = ?, "
                    "storage_epoch = storage_epoch + 1 WHERE world_id = ?",
                    (_HEAD_STATE_SENTINEL, world_id),
                )
                _LOG.info(
                    "world v2 head state split world_id=%s fields=%d item_rows=%d bytes=%d",
                    world_id,
                    len(fragments),
                    len(item_rows),
                    len(raw_state),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _mark_legacy_ownerless_plan_events_locked(self, source_bundle: str) -> None:
        rows = tuple(
            self._connection.execute(
                "SELECT event_id, event_json FROM world_v2_events WHERE world_id = ?",
                (self._world_id,),
            )
        )
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
            injected_v27_keys = tuple(
                sorted(
                    key
                    for key in _V27_ONLY_STATE_KEYS.intersection(raw_state)
                    if raw_state.get(key) not in (None, [], {})
                )
            )
            if injected_v27_keys and reducer_bundle_version not in {
                "world-v2-reducers.27",
                "world-v2-reducers.28",
                "world-v2-reducers.29",
                "world-v2-reducers.30",
                "world-v2-reducers.31",
                REDUCER_BUNDLE_VERSION,
            }:
                raise ValueError(
                    f"legacy head cannot claim v27 media execution fields {injected_v27_keys!r}"
                )
            injected_v26_keys = tuple(
                sorted(
                    key
                    for key in _V26_ONLY_STATE_KEYS.intersection(raw_state)
                    if raw_state.get(key) not in (None, [], {})
                )
            )
            if injected_v26_keys and reducer_bundle_version not in {
                "world-v2-reducers.26",
                "world-v2-reducers.27",
                "world-v2-reducers.28",
                "world-v2-reducers.29",
                "world-v2-reducers.30",
                "world-v2-reducers.31",
                REDUCER_BUNDLE_VERSION,
            }:
                raise ValueError(f"legacy head cannot claim v26 media fields {injected_v26_keys!r}")
            injected_v25_keys = tuple(
                sorted(
                    key
                    for key in _V25_ONLY_STATE_KEYS.intersection(raw_state)
                    if raw_state.get(key) not in (None, [], {})
                )
            )
            if injected_v25_keys and reducer_bundle_version not in {
                "world-v2-reducers.25",
                "world-v2-reducers.26",
                "world-v2-reducers.27",
                "world-v2-reducers.28",
                "world-v2-reducers.29",
                "world-v2-reducers.30",
                "world-v2-reducers.31",
                REDUCER_BUNDLE_VERSION,
            }:
                raise ValueError(
                    f"legacy head cannot claim v25 provider media fields {injected_v25_keys!r}"
                )
            injected_v24_keys = tuple(
                sorted(
                    key
                    for key in _V24_ONLY_STATE_KEYS.intersection(raw_state)
                    if raw_state.get(key) not in (None, [], {})
                )
            )
            if injected_v24_keys and reducer_bundle_version not in {
                "world-v2-reducers.24",
                "world-v2-reducers.25",
                "world-v2-reducers.26",
                "world-v2-reducers.27",
                "world-v2-reducers.28",
                "world-v2-reducers.29",
                "world-v2-reducers.30",
                "world-v2-reducers.31",
                REDUCER_BUNDLE_VERSION,
            }:
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
                "world-v2-reducers.30",
                "world-v2-reducers.31",
            }:
                raise ValueError(f"legacy head cannot claim v20 reply fields {injected_v20_keys!r}")
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
                raise ValueError(f"legacy head cannot claim v19 Fact fields {injected_v19_keys!r}")
            injected_v18_keys = tuple(
                key
                for key in _V18_ONLY_STATE_KEYS.intersection(raw_state)
                if raw_state.get(key) not in (None, [], {})
            )
            if injected_v18_keys and reducer_bundle_version not in {
                "world-v2-reducers.18",
                "world-v2-reducers.30",
            }:
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
                "world-v2-reducers.30",
                "world-v2-reducers.31",
            }:
                raise ValueError(f"legacy head cannot claim v17 audit fields {injected_v17_keys!r}")
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
                "world-v2-reducers.30",
                "world-v2-reducers.31",
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
                    "world-v2-reducers.30",
                    "world-v2-reducers.31",
                }
                and isinstance(actor_transitions, list)
                and any(
                    isinstance(transition, dict) and actor_binding_keys.intersection(transition)
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
                    isinstance(occurrence, dict) and "settled_outcome_ref" in occurrence
                    for occurrence in occurrences
                )
            ):
                raise ValueError("legacy world occurrence cannot claim a v16 settled outcome")
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
            provider_media_grants=projection.provider_media_grants,
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
            photo_candidates=projection.photo_candidates,
            media_opportunities=projection.media_opportunities,
            media_plans=projection.media_plans,
            media_unrenderable_opportunity_ids=projection.media_unrenderable_opportunity_ids,
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
            private_impressions=projection.private_impressions,
            private_impression_proposals=projection.private_impression_proposals,
            private_impression_proposal_ids=projection.private_impression_proposal_ids,
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
            expression_payload_descriptors=projection.expression_payload_descriptors,
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
        with self._database_write_lock, self._thread_lock:
            with self._measure_commit(events):
                started = time.perf_counter()
                result = self._commit_locked(
                    events,
                    expected_world_revision=expected_world_revision,
                    expected_deliberation_revision=expected_deliberation_revision,
                    commit_id=commit_id,
                )
                elapsed = (time.perf_counter() - started) * 1000
                if elapsed >= 1000:
                    _LOG.warning(
                        "world v2 ledger commit duration_ms=%.1f events=%s",
                        elapsed,
                        ",".join(event.event_type for event in events),
                    )
                return result

    def commit_at_cursor(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_cursor: ProjectionCursor,
        commit_id: str | None = None,
    ) -> CommitResult:
        events = _preflight_commit_events(events)
        with self._database_write_lock, self._thread_lock:
            with self._measure_commit(events):
                started = time.perf_counter()
                result = self._commit_locked(
                    events,
                    expected_world_revision=expected_cursor.world_revision,
                    expected_deliberation_revision=expected_cursor.deliberation_revision,
                    expected_ledger_sequence=expected_cursor.ledger_sequence,
                    commit_id=commit_id,
                )
                elapsed = (time.perf_counter() - started) * 1000
                if elapsed >= 1000:
                    _LOG.warning(
                        "world v2 ledger commit_at_cursor duration_ms=%.1f events=%s",
                        elapsed,
                        ",".join(event.event_type for event in events),
                    )
                return result

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
        with self._database_write_lock, self._thread_lock:
            with self._measure_commit(events):
                return self._commit_locked(
                    events,
                    expected_world_revision=expected_cursor.world_revision,
                    expected_deliberation_revision=expected_cursor.deliberation_revision,
                    expected_ledger_sequence=expected_cursor.ledger_sequence,
                    accepted_manifest_v3_authorized=True,
                    commit_id=commit_id,
                )

    def _measure_commit(self, events: Sequence[WorldEvent]):
        get = getattr(self._latency_recorder, "get", None)
        trace_ids = {event.trace_id for event in events}
        trace = get(next(iter(trace_ids))) if callable(get) and len(trace_ids) == 1 else None
        measure = getattr(trace, "measure_sync", None)
        return measure("ledger_commit") if callable(measure) else nullcontext()

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
        phase_started = time.perf_counter()
        # A different connection may have advanced or mutated the file since
        # this process installed its state cache.  Verify that change before
        # the write transaction; same-connection commits leave data_version
        # unchanged and retain their already-verified head state.
        self._refresh_verified_external_history_locked()
        refresh_ms = (time.perf_counter() - phase_started) * 1000
        phase_started = time.perf_counter()
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
            validate_ms = (time.perf_counter() - phase_started) * 1000
            phase_started = time.perf_counter()

            # Two separate probes let SQLite answer each from its covering
            # unique index.  The combined ``OR`` form degraded to a full scan
            # of every event row (including multi-KB envelopes), which grew
            # linearly with ledger history on the reply-critical path.
            placeholders = ",".join("?" for _ in events)
            duplicate = connection.execute(
                f"""SELECT event_id FROM world_v2_events
                    WHERE world_id = ? AND event_id IN ({placeholders})
                    LIMIT 1""",
                (self._world_id, *event_ids),
            ).fetchone() or connection.execute(
                f"""SELECT idempotency_key FROM world_v2_events
                    WHERE world_id = ? AND idempotency_key IN ({placeholders})
                    LIMIT 1""",
                (self._world_id, *idempotency_keys),
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
            head_is_split = head["state_json"] == _HEAD_STATE_SENTINEL
            head_state_identity = (
                int(head["world_revision"]),
                int(head["deliberation_revision"]),
                int(head["ledger_sequence"]),
                str(head["semantic_hash"]),
                str(head["state_hash"]),
            )
            if (
                self._head_state_cache is not None
                and self._head_state_cache_identity == head_state_identity
            ):
                state = self._head_state_cache
            else:
                state = self._decode_state(self._head_state_json_locked(head))
            previous_state = state
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
            reduce_ms = (time.perf_counter() - phase_started) * 1000
            phase_started = time.perf_counter()

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
            prefix_ms = (time.perf_counter() - phase_started) * 1000
            phase_started = time.perf_counter()
            cursor = ProjectionCursor(
                world_revision=world_revision,
                deliberation_revision=deliberation_revision,
                ledger_sequence=ledger_sequence,
            )
            fragment_cache = self._state_fragment_cache
            if head_is_split and (
                fragment_cache is None or fragment_cache[0] != str(head["state_hash"])
            ):
                # The process-local base is stale or absent: rebuild it from
                # the durable item rows inside this same transaction instead
                # of re-dumping the whole model graph.
                self._load_split_head_fragments_locked(head)
                fragment_cache = self._state_fragment_cache
            changed_fields: tuple[str, ...] | None = None
            if (
                head_is_split
                and fragment_cache is not None
                and fragment_cache[0] == str(head["state_hash"])
            ):
                self._incremental_state_base = (
                    previous_state,
                    fragment_cache[1],
                    fragment_cache[2],
                )
            try:
                if self._incremental_state_base is not None:
                    state_hash, changed_fields = self._encode_state_delta(state, cursor)
                else:
                    _, state_hash = self._encode_state_and_hash(state, cursor)
            finally:
                self._incremental_state_base = None
            state_ops = self._pending_head_state_ops
            self._pending_head_state_ops = None
            if state_ops is None:
                raise LedgerIntegrityError("state encode produced no storage operations")
            if not head_is_split:
                # A legacy full-row head has no item rows to patch; splitting
                # it is part of this same crash-consistent transaction.
                _, new_fragments, _ = self._state_fragment_cache
                state_ops = _HeadStateWriteOps(
                    full_rewrite=True,
                    field_deletes=(),
                    tail_deletes=(),
                    upserts=tuple(_head_state_item_rows_from_fragments(new_fragments)),
                )
            projection = self._projection_for_commit(
                head=head,
                state=state,
                world_revision=world_revision,
                deliberation_revision=deliberation_revision,
                ledger_sequence=ledger_sequence,
                changed_fields=changed_fields,
            )
            encode_ms = (time.perf_counter() - phase_started) * 1000
            phase_started = time.perf_counter()
            if state_ops.full_rewrite:
                connection.execute(
                    "DELETE FROM world_v2_head_state_items WHERE world_id = ?",
                    (self._world_id,),
                )
            else:
                for field_name in state_ops.field_deletes:
                    connection.execute(
                        "DELETE FROM world_v2_head_state_items "
                        "WHERE world_id = ? AND field = ?",
                        (self._world_id, field_name),
                    )
                for field_name, from_index in state_ops.tail_deletes:
                    connection.execute(
                        "DELETE FROM world_v2_head_state_items "
                        "WHERE world_id = ? AND field = ? AND idx >= ?",
                        (self._world_id, field_name, from_index),
                    )
            if state_ops.upserts:
                connection.executemany(
                    "INSERT OR REPLACE INTO world_v2_head_state_items "
                    "(world_id, field, idx, item_json) VALUES (?, ?, ?, ?)",
                    (
                        (self._world_id, field_name, index, item_json)
                        for field_name, index, item_json in state_ops.upserts
                    ),
                )
            storage_epoch = int(head["storage_epoch"]) + 1
            updated = connection.execute(
                """UPDATE world_v2_heads
                   SET world_revision = ?, deliberation_revision = ?, ledger_sequence = ?,
                       state_json = ?, semantic_hash = ?, reducer_bundle_version = ?,
                       state_hash = ?, storage_epoch = ?
                   WHERE world_id = ? AND world_revision = ?
                     AND deliberation_revision = ? AND ledger_sequence = ?""",
                (
                    world_revision,
                    deliberation_revision,
                    ledger_sequence,
                    _HEAD_STATE_SENTINEL,
                    projection.semantic_hash,
                    REDUCER_BUNDLE_VERSION,
                    state_hash,
                    storage_epoch,
                    self._world_id,
                    head["world_revision"],
                    head["deliberation_revision"],
                    head["ledger_sequence"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict("world head changed during commit")
            connection.commit()
            commit_ms = (time.perf_counter() - phase_started) * 1000
            total_ms = refresh_ms + validate_ms + reduce_ms + prefix_ms + encode_ms + commit_ms
            if total_ms >= 1000:
                _LOG.warning(
                    "world v2 ledger commit phases events=%s refresh_ms=%.1f validate_ms=%.1f reduce_ms=%.1f prefix_ms=%.1f encode_ms=%.1f sqlite_commit_ms=%.1f total_ms=%.1f",
                    ",".join(event.event_type for event in events),
                    refresh_ms,
                    validate_ms,
                    reduce_ms,
                    prefix_ms,
                    encode_ms,
                    commit_ms,
                    total_ms,
                )
            self._verified_ledger_epoch = self._ledger_mutation_epoch_locked()
            self._verified_data_version = self._sqlite_data_version_locked()
            for event in events:
                self._verified_event_commit_cache[event.event_id] = (event, result)
            # This exact projection produced the state bytes and hashes that
            # were atomically committed above.  Seed the process-local head so
            # the next Context build does not decode and revalidate the entire
            # growing state.  Every read still compares the durable row
            # identity (cursor, hashes and the monotonic storage epoch), so an
            # external append or mutation cannot reuse it as authority for a
            # different head.
            self._head_projection_cache = projection
            self._head_projection_cache_row_identity = (
                world_revision,
                deliberation_revision,
                ledger_sequence,
                projection.semantic_hash,
                state_hash,
                storage_epoch,
            )
            self._head_state_cache = state
            self._head_state_cache_identity = (
                world_revision,
                deliberation_revision,
                ledger_sequence,
                projection.semantic_hash,
                state_hash,
            )
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
            if event_json != row["event_json"] or not hmac.compare_digest(
                event_hash, str(row["event_hash"])
            ):
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
            if (
                self._connection.execute(
                    """SELECT 1 FROM world_v2_prefix_locator_values
                   WHERE world_id = ? AND locator_key = ?""",
                    (self._world_id, key),
                ).fetchone()
                is not None
            ):
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
            self._persist_locator_put_plan_locked(
                key=key, value=value, node_updates=plan.node_updates
            )
            changed_locator_nodes.update(
                {(depth, prefix): node_hash for depth, prefix, node_hash in plan.node_updates}
            )
            locator_root = plan.root

        if changed_locator_nodes:
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
                        node_hash,
                    )
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
                self._world_id,
                checkpoint.world_revision,
                checkpoint.deliberation_revision,
                checkpoint.last_ledger_sequence,
                checkpoint.commit_id,
                checkpoint.first_ledger_sequence,
                checkpoint.last_ledger_sequence,
                checkpoint.request_hash,
                checkpoint.result_hash,
                checkpoint.ordered_event_ids_hash,
                bytes.fromhex(checkpoint.locator_root),
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
            (
                (self._world_id, height, node_index, node_hash)
                for height, node_index, node_hash in plan.node_writes
            ),
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
            (
                (self._world_id, depth, _prefix_bits_blob(prefix), node_hash)
                for depth, prefix, node_hash in node_updates
            ),
        )
        self._connection.execute(
            """INSERT INTO world_v2_prefix_locator_values
                 (world_id, locator_key, value_hash, observation_id, event_type, event_id,
                  ledger_sequence, world_revision, deliberation_revision, event_leaf_index,
                  event_leaf_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._world_id,
                key,
                value.digest(),
                value.observation_id,
                value.event_type,
                value.event_id,
                value.ledger_sequence,
                value.world_revision,
                value.deliberation_revision,
                value.event_leaf_index,
                value.event_leaf_hash,
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
                self._project_at_head_hits += 1
                return head
            cache_key = (
                cursor.world_revision,
                cursor.deliberation_revision,
                cursor.ledger_sequence,
            )
            cached = self._historical_projection_cache.get(cache_key)
            if cached is not None:
                return cached
            self._historical_replay_calls += 1
            projection = self._replay_locked(
                target_cursor=cursor,
                target_schema_version=CURRENT_SCHEMA_VERSION,
                reducer_bundle_version=REDUCER_BUNDLE_VERSION,
            )
            self._historical_projection_cache[cache_key] = projection
            # Keep memory bounded even during long-running multi-turn audits.
            if len(self._historical_projection_cache) > 8:
                self._historical_projection_cache.pop(next(iter(self._historical_projection_cache)))
            return projection

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

    def _prefix_mmr_proof_at_leaf_count_locked(self, *, leaf_index: int, leaf_count: int):
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
                (key_int >> (256 - depth) << 1) | (1 - ((key_int >> (255 - depth)) & 1)),
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
            ).verify(leaf_hash=leaf, expected_root=anchor_root)
        except Exception as exc:
            raise LedgerIntegrityError(
                "proof-backed observation event MMR proof is invalid"
            ) from exc
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
                # Keep the history read one explicit snapshot.  The refresh
                # runs after BEGIN so tracing/observability still sees the
                # transaction boundary as the first statement.
                self._refresh_verified_external_history_locked()
                result = self._observation_events_at_locked(validated, cursor=cursor)
                connection.commit()
                return result
            except sqlite3.DatabaseError as exc:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                raise LedgerIntegrityError("observation history snapshot read failed") from exc
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
            zero = ProjectionCursor(world_revision=0, deliberation_revision=0, ledger_sequence=0)
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
            cached_by_identity: dict[str, HistoricalLedgerEvent] = {}
            cached_candidates: list[tuple[str, HistoricalLedgerEvent]] = []
            uncached: list[ObservationEventLocator] = []
            for locator in validated:
                hit = self._verified_observation_event_cache.get(
                    (locator.observation_id, locator.event_type, locator.idempotency_key)
                )
                if hit is None or hit.event_cursor.ledger_sequence > cursor.ledger_sequence:
                    uncached.append(locator)
                else:
                    cached_by_identity[locator.idempotency_key] = hit
                    cached_candidates.append((locator.observation_id, hit))
            if not uncached:
                cached = sorted(
                    cached_by_identity.values(),
                    key=lambda item: (item.event.event_id, item.event.event_type),
                )
                return tuple(cached)
            # The immutable prefix cache is intentionally partial: a new Fact
            # source should cost one proof lookup, not re-open every older
            # message retained in the same Context slice.
            validated = tuple(uncached)
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
            candidates: list[tuple[str, HistoricalLedgerEvent]] = cached_candidates
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
            result = tuple(candidate for _, candidate in candidates)
            candidate_by_identity = {
                historical.event.idempotency_key: historical for _, historical in candidates
            }
            for locator in validated:
                historical = candidate_by_identity.get(locator.idempotency_key)
                if historical is not None:
                    self._verified_observation_event_cache[
                        (locator.observation_id, locator.event_type, locator.idempotency_key)
                    ] = historical
            if len(self._verified_observation_event_cache) > 512:
                for key in tuple(self._verified_observation_event_cache)[:128]:
                    self._verified_observation_event_cache.pop(key, None)
            return result

    def _require_observation_commit_budget_locked(self, commit_ids: Sequence[str]) -> None:
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
                raise LedgerIntegrityError("observation history commit event budget exceeded")
            if int(row["byte_count"]) > OBSERVATION_HISTORY_MAX_BYTES:
                raise LedgerIntegrityError("observation history byte budget exceeded")

    def lookup_event_commit(self, event_id: str) -> tuple[WorldEvent, CommitResult] | None:
        """Return verified persisted bytes and the result of their original commit."""

        with self._thread_lock:
            self._refresh_verified_external_history_locked()
            cached = self._verified_event_commit_cache.get(event_id)
            if cached is not None:
                return cached
            row = self._connection.execute(
                """SELECT commit_id FROM world_v2_events
                   WHERE world_id = ? AND event_id = ?""",
                (self._world_id, event_id),
            ).fetchone()
            if row is None:
                return None
            # Startup verifies the immutable event/commit history from genesis,
            # and every later append updates the authenticated prefix in the
            # same SQLite transaction.  Bind this addressed lookup to that
            # already-verified process prefix.  Replaying genesis-to-predecessor
            # for every source ref made Context compilation O(commits * history)
            # while adding no new evidence: the event rows below are still
            # independently envelope-hashed and checked against their owning
            # commit/result bytes by ``_verified_commit_locked``.
            # ``_refresh_verified_external_history_locked`` above has already
            # bound this process to the current SQLite data_version.  Startup,
            # external refresh, and same-connection commit all install the
            # exact verified head projection.  Re-reading and hashing the
            # multi-megabyte state_json once per source ref made a cold Context
            # build O(required refs * full head size), despite every read being
            # reported as a cache hit.
            head = self._head_projection_cache
            if head is None:
                head = self._project_locked()
            verified_prefix_cursor = ProjectionCursor(
                world_revision=head.world_revision,
                deliberation_revision=head.deliberation_revision,
                ledger_sequence=head.ledger_sequence,
            )
            events, result, _ = self._verified_commit_locked(
                str(row["commit_id"]),
                verified_prefix_cursor=verified_prefix_cursor,
            )
            for verified_event in events:
                self._verified_event_commit_cache[verified_event.event_id] = (
                    verified_event,
                    result,
                )
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

    def resolve_initial_world_event_ref(self, *, at_world_revision: int) -> CommittedWorldEventRef:
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
            resolved = self._committed_ref_from_row(row, at_world_revision=at_world_revision)
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
        if event.world_id != self._world_id or event.event_id != row["event_id"]:
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
                        previous_cursor.ledger_sequence > verified_prefix_cursor.ledger_sequence
                        or last_sequence > verified_prefix_cursor.ledger_sequence
                    ):
                        raise LedgerIntegrityError("commit is outside the verified history prefix")
                    expected_sequence = previous_cursor.ledger_sequence
                    expected_world_revision = previous_cursor.world_revision
                    expected_deliberation_revision = previous_cursor.deliberation_revision
                else:
                    verified_prefix = self._replay_locked(
                        target_cursor=previous_cursor,
                        target_schema_version=CURRENT_SCHEMA_VERSION,
                        reducer_bundle_version=REDUCER_BUNDLE_VERSION,
                    )
                    expected_sequence = verified_prefix.ledger_sequence
                    expected_world_revision = verified_prefix.world_revision
                    expected_deliberation_revision = verified_prefix.deliberation_revision
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
        self._head_projection_reads += 1
        try:
            head = self._connection.execute(
                "SELECT * FROM world_v2_heads WHERE world_id = ?", (self._world_id,)
            ).fetchone()
            if head is None:
                raise LedgerIntegrityError("world head disappeared")
            if head["reducer_bundle_version"] != REDUCER_BUNDLE_VERSION:
                raise LedgerIntegrityError("world head reducer bundle is not installed")
            raw_state = head["state_json"]
            if not isinstance(raw_state, str):
                raise LedgerIntegrityError("head state is invalid")
            row_identity = (
                int(head["world_revision"]),
                int(head["deliberation_revision"]),
                int(head["ledger_sequence"]),
                str(head["semantic_hash"]),
                str(head["state_hash"]),
                (
                    int(head["storage_epoch"])
                    if raw_state == _HEAD_STATE_SENTINEL
                    else hashlib.sha256(raw_state.encode("utf-8")).digest()
                ),
            )
            if (
                self._head_projection_cache is not None
                and self._head_projection_cache_row_identity == row_identity
            ):
                self._head_projection_cache_hits += 1
                return self._head_projection_cache
            state = self._decode_state(self._head_state_json_locked(head))
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
            self._head_projection_cache = projection
            self._head_projection_cache_row_identity = row_identity
            self._head_state_cache = state
            self._head_state_cache_identity = (
                cursor.world_revision,
                cursor.deliberation_revision,
                cursor.ledger_sequence,
                projection.semantic_hash,
                str(persisted_state_hash),
            )
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
                if (
                    cursor != zero
                    and connection.execute(
                        """SELECT 1 FROM world_v2_prefix_checkpoints
                       WHERE world_id = ? AND world_revision = ?
                         AND deliberation_revision = ? AND ledger_sequence = ?""",
                        (
                            self._world_id,
                            cursor.world_revision,
                            cursor.deliberation_revision,
                            cursor.ledger_sequence,
                        ),
                    ).fetchone()
                    is None
                ):
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
                raise LedgerIntegrityError(
                    "replay evidence commit request hash does not match events"
                )
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
        self._total_replay_calls += 1
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
                    raise LedgerIntegrityError("event envelope does not match its ledger row")
            except LedgerIntegrityError:
                raise
            except Exception as exc:
                raise LedgerIntegrityError("persisted event is invalid") from exc
            ledger_sequence += 1
            if target_cursor is None and event.schema_version == CURRENT_SCHEMA_VERSION:
                self._recent_replay_event_cache[event.event_id] = event
                if len(self._recent_replay_event_cache) > 128:
                    self._recent_replay_event_cache.pop(next(iter(self._recent_replay_event_cache)))
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
                    allow_legacy_clock_drift=(
                        event.event_id in self._legacy_clock_compat_event_ids
                    ),
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
