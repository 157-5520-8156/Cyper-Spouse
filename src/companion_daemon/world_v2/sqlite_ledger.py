from __future__ import annotations

from collections.abc import Sequence
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from threading import RLock

from .batch_invariants import validate_commit_batch
from .errors import ConcurrencyConflict, IdempotencyConflict, LedgerIntegrityError
from .event_identity import validate_event_identity
from .ledger import canonical_event_json, commit_request_hash, derived_commit_id
from .reducers import (
    REDUCER_BUNDLE_VERSION,
    ReducerState,
    RevisionClass,
    event_definition,
    make_projection,
    reduce_event,
    require_reducer_bundle,
)
from .schemas import CommitResult, LedgerProjection, ProjectionCursor, WorldEvent
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
    }
)


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

    def __init__(self, *, path: str | Path, world_id: str) -> None:
        if not world_id:
            raise ValueError("world_id must not be empty")
        self._world_id = world_id
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
            self._migrate_head_bundle()
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
            injected_v16_keys = tuple(sorted(_V16_ONLY_STATE_KEYS.intersection(raw_state)))
            if injected_v16_keys:
                raise ValueError(
                    f"legacy head cannot claim v16 authority fields {injected_v16_keys!r}"
                )
            occurrences = raw_state.get("world_occurrences", [])
            if isinstance(occurrences, list) and any(
                isinstance(occurrence, dict)
                and "settled_outcome_ref" in occurrence
                for occurrence in occurrences
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
        with self._thread_lock:
            return self._commit_locked(
                events,
                expected_world_revision=expected_world_revision,
                expected_deliberation_revision=expected_deliberation_revision,
                commit_id=commit_id,
            )

    def _commit_locked(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_world_revision: int,
        expected_deliberation_revision: int,
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

            validate_commit_batch(events, expected_world_revision=expected_world_revision)

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

    def _verified_commit_locked(
        self, commit_id: str
    ) -> tuple[tuple[WorldEvent, ...], CommitResult, str]:
        """Rebuild one commit result from verified immutable event rows."""

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
                    try:
                        # Old bundles allowed arbitrary audit extensions on an
                        # Acceptance.  Keep it authoritative only when the
                        # already-replayed proposal state proves every current
                        # reducer precondition; otherwise preserve it as a
                        # revision-bearing, migration-only audit fact.
                        reduce_event(state, event)
                    except Exception:
                        event = upcast_event(
                            {
                                **raw_event,
                                "event_type": "LegacyAcceptanceAuditRecorded",
                            },
                            target_schema_version=target_schema_version,
                        )
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
                state = reduce_event(state, event)
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
