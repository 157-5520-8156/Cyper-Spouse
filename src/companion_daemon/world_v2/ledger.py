from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
from itertools import islice
import json
import math
from threading import RLock
from typing import Protocol

from .batch_invariants import validate_commit_batch
from .errors import ConcurrencyConflict, IdempotencyConflict, LedgerIntegrityError
from .event_identity import domain_idempotency_key, validate_event_identity
from .ledger_prefix_proof import (
    IncrementalMmrV1,
    IncrementalSparseMerkleMapV1,
    LedgerLeafV1,
    ObservationLocatorValueV1,
    PrefixCheckpointLeafV1,
    commit_result_hash_v1,
    observation_locator_key,
    ordered_event_ids_hash_v1,
)
from .reducers import (
    REDUCER_BUNDLE_VERSION,
    RevisionClass,
    ReducerState,
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


OBSERVATION_HISTORY_MAX_BYTES = 16 * 1024 * 1024
OBSERVATION_HISTORY_MAX_COMMIT_EVENTS = 4096
_COMMIT_PREFLIGHT_MAX_DEPTH = 32
_COMMIT_PREFLIGHT_MAX_NODES = 16_384
_COMMIT_PREFLIGHT_MAX_KEY_CHARS = 512
_COMMIT_PREFLIGHT_MAX_INTEGER = (1 << 63) - 1


class LedgerPort(Protocol):
    """Persistence seam consumed by WorldRuntime; adapters own concurrency semantics."""

    @property
    def world_id(self) -> str: ...

    @property
    def blocks_event_loop(self) -> bool: ...

    def commit(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_world_revision: int,
        expected_deliberation_revision: int,
        commit_id: str | None = None,
    ) -> CommitResult: ...

    def commit_at_cursor(
        self,
        events: Sequence[WorldEvent],
        *,
        expected_cursor: ProjectionCursor,
        commit_id: str | None = None,
    ) -> CommitResult:
        """Append only when the complete projection cursor is still current.

        Unlike ``commit``, this is suitable for an audited manifest recorder:
        the ledger sequence is part of the compare-and-swap precondition rather
        than merely an informational result field.
        """
        ...

    def project(self) -> LedgerProjection: ...

    def project_at(self, cursor: ProjectionCursor) -> LedgerProjection: ...

    def observation_events_at(
        self, locators: Sequence[ObservationEventLocator], *, cursor: ProjectionCursor
    ) -> tuple[HistoricalLedgerEvent, ...]:
        """Read exact pinned events without granting authority.

        The caller must enumerate every message/operator locator for an observation ref
        from one pinned projection. Missing results must be rejected upstream; callers
        must not fall back to aliases or a different event family.
        """
        ...

    def lookup_event_commit(self, event_id: str) -> tuple[WorldEvent, CommitResult] | None: ...

    def resolve_committed_event_refs(
        self, event_ids: Sequence[str], *, at_world_revision: int
    ) -> dict[str, CommittedWorldEventRef]: ...

    def resolve_initial_world_event_ref(
        self, *, at_world_revision: int
    ) -> CommittedWorldEventRef: ...


@dataclass(frozen=True, slots=True)
class HistoricalLedgerEvent:
    event: WorldEvent
    event_cursor: ProjectionCursor
    event_envelope_hash: str


@dataclass(frozen=True, slots=True)
class ObservationEventLocator:
    """Exact, non-authorizing identity derived from a pinned projection.

    Callers must enumerate both message and operator refs sharing an observation id.
    A locator only enables exact ledger reads; missing events require upstream rejection
    and must never trigger an alias or event-family fallback.
    """

    observation_id: str
    event_type: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if type(self.observation_id) is not str or not 1 <= len(
            self.observation_id
        ) <= 512:
            raise ValueError("observation_id must contain between 1 and 512 chars")
        if type(self.event_type) is not str or self.event_type not in {
            "ObservationRecorded",
            "OperatorObservationRecorded",
        }:
            raise ValueError("locator event_type is not an observation event")
        if type(self.idempotency_key) is not str or not 1 <= len(
            self.idempotency_key
        ) <= 512:
            raise ValueError("idempotency_key must contain between 1 and 512 chars")

    @classmethod
    def for_message(
        cls,
        *,
        world_id: str,
        observation_id: str,
        source: str,
        source_event_id: str,
    ) -> ObservationEventLocator:
        for name, value in {
            "world_id": world_id,
            "observation_id": observation_id,
            "source": source,
            "source_event_id": source_event_id,
        }.items():
            if type(value) is not str or not 1 <= len(value) <= 512:
                raise ValueError(f"{name} must contain between 1 and 512 chars")
        identity = domain_idempotency_key(
            event_type="ObservationRecorded",
            world_id=world_id,
            payload={
                "observation_kind": "message",
                "source": source,
                "source_event_id": source_event_id,
            },
        )
        if identity is None:
            raise ValueError("message observation has no domain identity")
        return cls(
            observation_id=observation_id,
            event_type="ObservationRecorded",
            idempotency_key=identity,
        )

    @classmethod
    def for_operator(
        cls, *, world_id: str, observation_id: str
    ) -> ObservationEventLocator:
        for name, value in {
            "world_id": world_id,
            "observation_id": observation_id,
        }.items():
            if type(value) is not str or not 1 <= len(value) <= 512:
                raise ValueError(f"{name} must contain between 1 and 512 chars")
        identity = domain_idempotency_key(
            event_type="OperatorObservationRecorded",
            world_id=world_id,
            payload={"observation_id": observation_id},
        )
        if identity is None:
            raise ValueError("operator observation has no domain identity")
        return cls(
            observation_id=observation_id,
            event_type="OperatorObservationRecorded",
            idempotency_key=identity,
        )


@dataclass(frozen=True, slots=True)
class _StoredEvent:
    ledger_sequence: int
    world_revision: int
    deliberation_revision: int
    event: WorldEvent


@dataclass(frozen=True, slots=True)
class _StoredCommit:
    request_hash: str
    result: CommitResult


def canonical_event_json(event: WorldEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bounded_plain_storage_string_count(value: object, *, limit: int) -> int:
    active: set[int] = set()
    nodes = 0
    string_characters = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes, string_characters
        nodes += 1
        if nodes > _COMMIT_PREFLIGHT_MAX_NODES:
            raise ValueError("commit event storage exceeds the node budget")
        if depth > _COMMIT_PREFLIGHT_MAX_DEPTH:
            raise ValueError("commit event storage exceeds the depth budget")
        if type(item) is str:
            string_characters += len(item)
            if string_characters > limit:
                raise ValueError("commit event bytes exceed the write contract")
            return
        if item is None or type(item) is bool or type(item) is datetime:
            return
        if type(item) is int:
            if not -_COMMIT_PREFLIGHT_MAX_INTEGER <= item <= _COMMIT_PREFLIGHT_MAX_INTEGER:
                raise ValueError("commit event integer is outside the storage contract")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("commit event float must be finite")
            return
        if type(item) not in {dict, list, tuple}:
            raise ValueError("commit event storage contains a non-plain value")
        identity = id(item)
        if identity in active:
            raise ValueError("commit event storage contains a cycle")
        active.add(identity)
        try:
            if type(item) is dict:
                for key, nested in item.items():
                    if type(key) is not str or not 1 <= len(key) <= _COMMIT_PREFLIGHT_MAX_KEY_CHARS:
                        raise ValueError("commit event storage contains an invalid key")
                    visit(key, depth + 1)
                    visit(nested, depth + 1)
            else:
                for nested in item:
                    visit(nested, depth + 1)
        finally:
            active.remove(identity)

    visit(value, 0)
    return string_characters


def _rebuild_preflight_world_event(
    event: object, *, remaining_bytes: int
) -> WorldEvent:
    if type(event) is not WorldEvent:
        raise ValueError("commit events must contain exact WorldEvent values")
    storage = object.__getattribute__(event, "__dict__")
    extras = object.__getattribute__(event, "__pydantic_extra__")
    expected_fields = frozenset(WorldEvent.model_fields)
    if type(storage) is not dict or frozenset(storage) != expected_fields:
        raise ValueError("commit event storage must contain exactly the schema fields")
    if extras is not None and (type(extras) is not dict or extras):
        raise ValueError("commit event storage must not contain extras")
    _bounded_plain_storage_string_count(storage, limit=remaining_bytes)
    try:
        return WorldEvent.model_validate(dict(storage), strict=True)
    except Exception as exc:
        raise ValueError("commit event storage is invalid") from exc


def _preflight_commit_events(events: Sequence[WorldEvent]) -> tuple[WorldEvent, ...]:
    """Materialize and enforce the shared adapter write budget before side effects."""

    if isinstance(events, (str, bytes)):
        raise ValueError("commit events must be a sequence")
    if type(events) in {list, tuple}:
        if not events:
            raise ValueError("commit requires at least one event")
        if len(events) > OBSERVATION_HISTORY_MAX_COMMIT_EVENTS:
            raise ValueError("commit event count exceeds the write contract")
        materialized = tuple(events)
    else:
        try:
            materialized = tuple(
                islice(iter(events), OBSERVATION_HISTORY_MAX_COMMIT_EVENTS + 1)
            )
        except TypeError as exc:
            raise ValueError("commit events must be iterable") from exc
        if not materialized:
            raise ValueError("commit requires at least one event")
        if len(materialized) > OBSERVATION_HISTORY_MAX_COMMIT_EVENTS:
            raise ValueError("commit event count exceeds the write contract")
    total_bytes = 0
    rebuilt: list[WorldEvent] = []
    for event in materialized:
        validated = _rebuild_preflight_world_event(
            event,
            remaining_bytes=OBSERVATION_HISTORY_MAX_BYTES - total_bytes,
        )
        total_bytes += len(canonical_event_json(validated).encode("utf-8"))
        if total_bytes > OBSERVATION_HISTORY_MAX_BYTES:
            raise ValueError("commit event bytes exceed the write contract")
        rebuilt.append(validated)
    return tuple(rebuilt)


def _validated_observation_locators(
    locators: Sequence[ObservationEventLocator],
) -> tuple[ObservationEventLocator, ...]:
    if isinstance(locators, (str, bytes)):
        raise ValueError("locators must be a sequence")
    if type(locators) in {list, tuple}:
        if not 1 <= len(locators) <= 128:
            raise ValueError("locators must contain between 1 and 128 entries")
        raw_locators = tuple(locators)
    else:
        try:
            raw_locators = tuple(islice(iter(locators), 129))
        except TypeError as exc:
            raise ValueError("locators must be iterable") from exc
        if not 1 <= len(raw_locators) <= 128:
            raise ValueError("locators must contain between 1 and 128 entries")
    reconstructed: list[ObservationEventLocator] = []
    for locator in raw_locators:
        if type(locator) is not ObservationEventLocator:
            raise ValueError("locators must contain exact ObservationEventLocator values")
        fields = (locator.observation_id, locator.event_type, locator.idempotency_key)
        if any(type(value) is not str or not 1 <= len(value) <= 512 for value in fields):
            raise ValueError("locator fields must be bounded strings")
        reconstructed.append(
            ObservationEventLocator(
                observation_id=fields[0],
                event_type=fields[1],
                idempotency_key=fields[2],
            )
        )
    validated = tuple(reconstructed)
    canonical = tuple(
        sorted(
            validated,
            key=lambda item: (item.observation_id, item.event_type, item.idempotency_key),
        )
    )
    if validated != canonical or len(set(validated)) != len(validated):
        raise ValueError("locators must be canonically sorted and unique")
    idempotency_keys = tuple(locator.idempotency_key for locator in validated)
    if len(set(idempotency_keys)) != len(idempotency_keys):
        raise ValueError("locator idempotency keys must be unique")
    return validated


def _observation_id(event: WorldEvent) -> str | None:
    if event.event_type not in {"ObservationRecorded", "OperatorObservationRecorded"}:
        return None
    observation_id = event.payload().get("observation_id")
    return observation_id if isinstance(observation_id, str) and observation_id else None


def commit_request_hash(events: Sequence[WorldEvent]) -> str:
    encoded = json.dumps(
        [json.loads(canonical_event_json(event)) for event in events],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derived_commit_id(events: Sequence[WorldEvent]) -> str:
    return f"commit:{commit_request_hash(events)}"


class WorldLedger:
    """Append-only World v2 ledger with separated revision streams."""

    def __init__(self, *, world_id: str) -> None:
        self._world_id = world_id
        self._events: list[_StoredEvent] = []
        self._thread_lock = RLock()
        self._by_idempotency: dict[str, _StoredEvent] = {}
        self._by_event_id: dict[str, _StoredEvent] = {}
        self._commits: dict[str, _StoredCommit] = {}
        self._commit_events: dict[str, tuple[_StoredEvent, ...]] = {}
        self._event_commit_ids: dict[str, str] = {}
        self._commit_cursors: set[tuple[int, int, int]] = {(0, 0, 0)}
        self._prefix_mmr = IncrementalMmrV1()
        self._prefix_locator_map = IncrementalSparseMerkleMapV1()
        self._prefix_checkpoints: dict[tuple[int, int, int], PrefixCheckpointLeafV1] = {}
        self._world_revision = 0
        self._deliberation_revision = 0
        self._state = ReducerState()

    @classmethod
    def in_memory(cls, *, world_id: str) -> WorldLedger:
        return cls(world_id=world_id)

    @property
    def world_id(self) -> str:
        return self._world_id

    @property
    def blocks_event_loop(self) -> bool:
        return False

    def commit(
        self,
        events: Sequence[WorldEvent],
        *,
        commit_id: str | None = None,
        expected_world_revision: int,
        expected_deliberation_revision: int,
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

    def _commit_locked(
        self,
        events: Sequence[WorldEvent],
        *,
        commit_id: str | None = None,
        expected_world_revision: int,
        expected_deliberation_revision: int,
        expected_ledger_sequence: int | None = None,
    ) -> CommitResult:
        commit_id = commit_id or derived_commit_id(events)
        if not commit_id:
            raise ValueError("commit_id must not be empty")
        request_hash = commit_request_hash(events)
        existing_commit = self._commits.get(commit_id)
        if existing_commit is not None:
            if existing_commit.request_hash != request_hash:
                raise IdempotencyConflict(f"commit_id {commit_id!r} has different content")
            return existing_commit.result

        validate_commit_batch(events, expected_world_revision=expected_world_revision)

        event_ids = [event.event_id for event in events]
        idempotency_keys = [event.idempotency_key for event in events]
        if len(set(event_ids)) != len(event_ids):
            raise IdempotencyConflict("duplicate event_id inside one commit")
        if len(set(idempotency_keys)) != len(idempotency_keys):
            raise IdempotencyConflict("duplicate idempotency key inside one commit")

        definitions = []
        for event in events:
            if event.world_id != self._world_id:
                raise ValueError("event belongs to another world")
            validate_event_identity(event)
            definitions.append(event_definition(event.event_type))
            existing_by_id = self._by_event_id.get(event.event_id)
            if existing_by_id is not None:
                raise IdempotencyConflict(f"event_id {event.event_id!r} already exists")
            existing = self._by_idempotency.get(event.idempotency_key)
            if existing is not None:
                raise IdempotencyConflict(
                    f"idempotency key {event.idempotency_key!r} already exists under "
                    "a different commit"
                )

        revision_classes = {definition.revision_class for definition in definitions}
        if (
            RevisionClass.WORLD in revision_classes
            and expected_world_revision != self._world_revision
        ):
            raise ConcurrencyConflict("stale world revision")
        if (
            RevisionClass.DELIBERATION in revision_classes
            and expected_deliberation_revision != self._deliberation_revision
        ):
            raise ConcurrencyConflict("stale deliberation revision")
        if expected_ledger_sequence is not None and (
            expected_world_revision != self._world_revision
            or expected_deliberation_revision != self._deliberation_revision
            or expected_ledger_sequence != len(self._events)
        ):
            raise ConcurrencyConflict("stale projection cursor")

        next_world_revision = self._world_revision
        next_deliberation_revision = self._deliberation_revision
        next_state = self._state
        staged: list[_StoredEvent] = []
        for event, definition in zip(events, definitions, strict=True):
            if definition.revision_class is RevisionClass.WORLD:
                next_world_revision += 1
            else:
                next_deliberation_revision += 1
            next_state = reduce_event(next_state, event)
            staged.append(
                _StoredEvent(
                    ledger_sequence=len(self._events) + len(staged) + 1,
                    world_revision=next_world_revision,
                    deliberation_revision=next_deliberation_revision,
                    event=event,
                )
            )

        result = CommitResult(
            world_revision=next_world_revision,
            deliberation_revision=next_deliberation_revision,
            ledger_sequence=len(self._events) + len(staged),
            event_ids=tuple(event.event_id for event in events),
        )
        # Mutations below cannot fail after reducer/identity validation: MMR and
        # sparse-map appends only consume deterministic, bounded ledger values.
        prefix_leaf_hashes: list[tuple[_StoredEvent, bytes, int]] = []
        for stored in staged:
            event = stored.event
            leaf_hash = LedgerLeafV1(
                world_id=self._world_id,
                ledger_sequence=stored.ledger_sequence,
                world_revision=stored.world_revision,
                deliberation_revision=stored.deliberation_revision,
                commit_id=commit_id,
                event_id=event.event_id,
                idempotency_key=event.idempotency_key,
                event_envelope_hash=hashlib.sha256(canonical_event_json(event).encode("utf-8")).hexdigest(),
            ).digest()
            prefix_leaf_hashes.append((stored, leaf_hash, self._prefix_mmr.leaf_count + len(prefix_leaf_hashes)))
        for stored, leaf_hash, leaf_index in prefix_leaf_hashes:
            self._prefix_mmr.append(leaf_hash)
            event = stored.event
            observation_id = _observation_id(event)
            if observation_id is not None:
                self._prefix_locator_map.put(
                    key=observation_locator_key(world_id=self._world_id, event_type=event.event_type, idempotency_key=event.idempotency_key),
                    value_hash=ObservationLocatorValueV1(
                        observation_id=observation_id, event_type=event.event_type, event_id=event.event_id,
                        ledger_sequence=stored.ledger_sequence, world_revision=stored.world_revision,
                        deliberation_revision=stored.deliberation_revision, event_leaf_index=leaf_index,
                        event_leaf_hash=leaf_hash,
                    ).digest(),
                )
        checkpoint = PrefixCheckpointLeafV1(
            world_id=self._world_id, commit_id=commit_id,
            first_ledger_sequence=staged[0].ledger_sequence, last_ledger_sequence=staged[-1].ledger_sequence,
            world_revision=result.world_revision, deliberation_revision=result.deliberation_revision,
            request_hash=request_hash,
            result_hash=commit_result_hash_v1(world_revision=result.world_revision, deliberation_revision=result.deliberation_revision, ledger_sequence=result.ledger_sequence, event_ids=result.event_ids),
            ordered_event_ids_hash=ordered_event_ids_hash_v1(result.event_ids),
            locator_root=self._prefix_locator_map.root.hex(), mmr_leaf_count=self._prefix_mmr.leaf_count + 1,
        )
        self._prefix_mmr.append(checkpoint.digest())

        self._events.extend(staged)
        self._by_idempotency.update((stored.event.idempotency_key, stored) for stored in staged)
        self._by_event_id.update((stored.event.event_id, stored) for stored in staged)
        self._world_revision = next_world_revision
        self._deliberation_revision = next_deliberation_revision
        self._state = next_state
        self._prefix_checkpoints[(result.world_revision, result.deliberation_revision, result.ledger_sequence)] = checkpoint
        self._commits[commit_id] = _StoredCommit(request_hash=request_hash, result=result)
        self._commit_events[commit_id] = tuple(staged)
        self._event_commit_ids.update((stored.event.event_id, commit_id) for stored in staged)
        self._commit_cursors.add(
            (result.world_revision, result.deliberation_revision, result.ledger_sequence)
        )
        return result

    def project(self) -> LedgerProjection:
        return make_projection(
            world_id=self._world_id,
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=len(self._events),
            state=self._state,
        )

    def project_at(self, cursor: ProjectionCursor) -> LedgerProjection:
        head_cursor = ProjectionCursor(
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=len(self._events),
        )
        if cursor.ledger_sequence > len(self._events):
            raise ValueError("requested projection cursor is outside the ledger range")
        if cursor == head_cursor:
            return self.project()
        state = ReducerState()
        reached_world_revision = 0
        reached_deliberation_revision = 0
        ledger_sequence = 0
        for stored in self._events:
            if stored.ledger_sequence > cursor.ledger_sequence:
                break
            state = reduce_event(state, stored.event)
            reached_world_revision = stored.world_revision
            reached_deliberation_revision = stored.deliberation_revision
            ledger_sequence = stored.ledger_sequence
        reached = ProjectionCursor(
            world_revision=reached_world_revision,
            deliberation_revision=reached_deliberation_revision,
            ledger_sequence=ledger_sequence,
        )
        if reached != cursor:
            raise ValueError("requested projection cursor is not present in the ledger")
        return make_projection(
            world_id=self._world_id,
            world_revision=reached_world_revision,
            deliberation_revision=reached_deliberation_revision,
            ledger_sequence=ledger_sequence,
            state=state,
        )

    def observation_events_at(
        self, locators: Sequence[ObservationEventLocator], *, cursor: ProjectionCursor
    ) -> tuple[HistoricalLedgerEvent, ...]:
        validated = _validated_observation_locators(locators)
        cursor_identity = (
            cursor.world_revision,
            cursor.deliberation_revision,
            cursor.ledger_sequence,
        )
        if cursor_identity not in self._commit_cursors:
            raise ValueError("requested cursor is not a committed batch boundary")
        candidates: list[tuple[str, HistoricalLedgerEvent]] = []
        budgeted_commits: set[str] = set()
        for locator in validated:
            stored = self._by_idempotency.get(locator.idempotency_key)
            if stored is None or stored.ledger_sequence > cursor.ledger_sequence:
                continue
            event = stored.event
            commit_id = self._event_commit_ids.get(event.event_id)
            if commit_id is None:
                raise LedgerIntegrityError("observation event has no owning commit")
            if commit_id not in budgeted_commits:
                self._require_observation_commit_budget(commit_id)
                budgeted_commits.add(commit_id)
            if (
                event.idempotency_key != locator.idempotency_key
                or event.event_type != locator.event_type
                or _observation_id(event) != locator.observation_id
            ):
                raise LedgerIntegrityError("observation locator does not match its event")
            encoded = canonical_event_json(event).encode("utf-8")
            candidates.append(
                (
                    locator.observation_id,
                    HistoricalLedgerEvent(
                        event=event,
                        event_cursor=ProjectionCursor(
                            world_revision=stored.world_revision,
                            deliberation_revision=stored.deliberation_revision,
                            ledger_sequence=stored.ledger_sequence,
                        ),
                        event_envelope_hash=hashlib.sha256(encoded).hexdigest(),
                    ),
                )
            )
        candidates.sort(
            key=lambda item: (item[0], item[1].event.event_type, item[1].event.event_id)
        )
        return tuple(candidate for _, candidate in candidates)

    def _require_observation_commit_budget(self, commit_id: str) -> None:
        stored_events = self._commit_events.get(commit_id)
        if stored_events is None:
            raise LedgerIntegrityError("observation owning commit is unavailable")
        if len(stored_events) > OBSERVATION_HISTORY_MAX_COMMIT_EVENTS:
            raise LedgerIntegrityError("observation history commit event budget exceeded")
        total_bytes = 0
        try:
            for stored in stored_events:
                event = _rebuild_preflight_world_event(
                    stored.event,
                    remaining_bytes=OBSERVATION_HISTORY_MAX_BYTES - total_bytes,
                )
                total_bytes += len(canonical_event_json(event).encode("utf-8"))
                if total_bytes > OBSERVATION_HISTORY_MAX_BYTES:
                    raise ValueError("commit event bytes exceed the write contract")
        except ValueError as exc:
            raise LedgerIntegrityError("observation history byte budget exceeded") from exc

    def lookup_event_commit(self, event_id: str) -> tuple[WorldEvent, CommitResult] | None:
        """Return the immutable event and its original commit result, if present."""

        stored = self._by_event_id.get(event_id)
        if stored is None:
            return None
        for commit in self._commits.values():
            if event_id in commit.result.event_ids:
                return stored.event, commit.result
        raise RuntimeError(f"event {event_id!r} has no owning commit")

    def resolve_committed_event_refs(
        self, event_ids: Sequence[str], *, at_world_revision: int
    ) -> dict[str, CommittedWorldEventRef]:
        """Resolve a bounded source set through the in-memory event-id index."""

        identities = tuple(sorted(set(event_ids)))
        if len(identities) != len(event_ids):
            raise ValueError("committed event source identities must be unique")
        resolved: dict[str, CommittedWorldEventRef] = {}
        for event_id in identities:
            stored = self._by_event_id.get(event_id)
            if stored is None:
                raise ValueError(f"committed event {event_id!r} is unavailable")
            if stored.world_revision > at_world_revision:
                raise ValueError("committed event source is newer than the pinned projection")
            if event_definition(stored.event.event_type).revision_class is not RevisionClass.WORLD:
                raise ValueError("Situation source is not a committed world event")
            resolved[event_id] = _committed_ref(stored)
        return resolved

    def resolve_initial_world_event_ref(
        self, *, at_world_revision: int
    ) -> CommittedWorldEventRef:
        if not self._events:
            raise ValueError("world has no initial event authority")
        stored = self._events[0]
        if stored.event.event_type != "WorldStarted" or stored.world_revision > at_world_revision:
            raise ValueError("world has no pinned WorldStarted authority")
        return _committed_ref(stored)

    def rebuild(self, *, reducer_bundle_version: str = REDUCER_BUNDLE_VERSION) -> LedgerProjection:
        require_reducer_bundle(reducer_bundle_version)
        state = ReducerState()
        world_revision = 0
        deliberation_revision = 0
        for stored in self._events:
            definition = event_definition(stored.event.event_type)
            if definition.revision_class is RevisionClass.WORLD:
                world_revision += 1
            else:
                deliberation_revision += 1
            state = reduce_event(state, stored.event)
        return make_projection(
            world_id=self._world_id,
            world_revision=world_revision,
            deliberation_revision=deliberation_revision,
            ledger_sequence=len(self._events),
            state=state,
            reducer_bundle_version=reducer_bundle_version,
        )


def _committed_ref(stored: _StoredEvent) -> CommittedWorldEventRef:
    event = stored.event
    return CommittedWorldEventRef(
        event_id=event.event_id,
        event_type=event.event_type,
        world_revision=stored.world_revision,
        payload_hash=event.payload_hash,
        logical_time=event.logical_time,
        continuation_refs=(
            (str(event.payload()["appraisal_trigger_ref"]),)
            if event.event_type == "WorldOccurrenceSettled"
            else ()
        ),
    )
