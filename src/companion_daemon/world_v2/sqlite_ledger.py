from __future__ import annotations

from collections.abc import Sequence
import hashlib
from pathlib import Path
import sqlite3
from threading import RLock

from .errors import ConcurrencyConflict, IdempotencyConflict, LedgerIntegrityError
from .ledger import canonical_event_json, commit_request_hash, derived_commit_id
from .reducers import ReducerState, RevisionClass, event_definition, make_projection, reduce_event
from .schemas import CommitResult, LedgerProjection, WorldEvent


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
                semantic_hash TEXT NOT NULL
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

    @staticmethod
    def _encode_state(state: ReducerState) -> str:
        return state.model_dump_json()

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
                 state_json, semantic_hash)
            VALUES (?, 0, 0, 0, ?, ?)
            """,
            (self._world_id, self._encode_state(ReducerState()), initial.semantic_hash),
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

        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                """SELECT request_hash, result_json FROM world_v2_commits
                   WHERE world_id = ? AND commit_id = ?""",
                (self._world_id, commit_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        f"commit_id {commit_id!r} has different content"
                    )
                result = CommitResult.model_validate_json(existing["result_json"])
                connection.commit()
                return result

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
                       state_json = ?, semantic_hash = ?
                   WHERE world_id = ? AND world_revision = ?
                     AND deliberation_revision = ? AND ledger_sequence = ?""",
                (
                    world_revision,
                    deliberation_revision,
                    ledger_sequence,
                    self._encode_state(state),
                    projection.semantic_hash,
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

    def _project_locked(self) -> LedgerProjection:
        try:
            head = self._connection.execute(
                "SELECT * FROM world_v2_heads WHERE world_id = ?", (self._world_id,)
            ).fetchone()
            if head is None:
                raise LedgerIntegrityError("world head disappeared")
            projection = make_projection(
                world_id=self._world_id,
                world_revision=int(head["world_revision"]),
                deliberation_revision=int(head["deliberation_revision"]),
                ledger_sequence=int(head["ledger_sequence"]),
                state=self._decode_state(head["state_json"]),
            )
            if projection.semantic_hash != head["semantic_hash"]:
                raise LedgerIntegrityError(
                    "head semantic hash does not match persisted state"
                )
            return projection
        except LedgerIntegrityError:
            raise
        except Exception as exc:
            raise LedgerIntegrityError("persisted world head is invalid") from exc

    def rebuild(self) -> LedgerProjection:
        with self._thread_lock:
            return self._rebuild_locked()

    def _rebuild_locked(self) -> LedgerProjection:
        state = ReducerState()
        world_revision = 0
        deliberation_revision = 0
        ledger_sequence = 0
        rows = self._connection.execute(
            """SELECT * FROM world_v2_events WHERE world_id = ?
               ORDER BY ledger_sequence""",
            (self._world_id,),
        )
        for row in rows:
            event_json = row["event_json"]
            if not isinstance(event_json, str):
                raise LedgerIntegrityError("persisted event bytes are invalid")
            actual_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
            if actual_hash != row["event_hash"]:
                raise LedgerIntegrityError("event envelope hash mismatch")
            try:
                event = WorldEvent.model_validate_json(event_json)
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
        rebuilt = make_projection(
            world_id=self._world_id,
            world_revision=world_revision,
            deliberation_revision=deliberation_revision,
            ledger_sequence=ledger_sequence,
            state=state,
        )
        if rebuilt != self.project():
            raise LedgerIntegrityError("rebuilt projection does not match persisted head")
        return rebuilt
