from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Iterator

import pytest

import companion_daemon.world_v2.ledger as ledger_module
import companion_daemon.world_v2.sqlite_ledger as sqlite_ledger_module
from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import (
    HistoricalLedgerEvent,
    LedgerPort,
    ObservationEventLocator,
    WorldLedger,
    canonical_event_json,
)
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world-observation-history"


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    if event_type == "ObservationRecorded" and "observation_kind" not in payload:
        observation_id = payload["observation_id"]
        assert isinstance(observation_id, str)
        payload = {
            "schema_version": "world-v2.1",
            "observation_kind": "message",
            "observation_id": observation_id,
            "world_id": WORLD,
            "logical_time": NOW.isoformat(),
            "created_at": NOW.isoformat(),
            "trace_id": "trace:observation-history",
            "causation_id": f"cause:{event_id}",
            "correlation_id": "correlation:observation-history",
            "source": "test",
            "source_event_id": f"source:{observation_id}",
            "actor": "system:test",
            "channel": "chat",
            "payload_ref": f"payload:{observation_id}",
            "payload_hash": "f" * 64,
            "received_at": NOW.isoformat(),
        }
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id=WORLD,
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:observation-history",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:observation-history",
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )


def _locator(event: WorldEvent) -> ObservationEventLocator:
    observation_id = event.payload()["observation_id"]
    assert isinstance(observation_id, str)
    identity = domain_idempotency_key(
        event_type=event.event_type,
        world_id=event.world_id,
        payload=event.payload(),
    )
    assert identity == event.idempotency_key
    if event.event_type == "ObservationRecorded":
        payload = event.payload()
        return ObservationEventLocator.for_message(
            world_id=event.world_id,
            observation_id=observation_id,
            source=str(payload["source"]),
            source_event_id=str(payload["source_event_id"]),
        )
    return ObservationEventLocator.for_operator(
        world_id=event.world_id, observation_id=observation_id
    )


def _commit(ledger: LedgerPort, events: tuple[WorldEvent, ...]) -> ProjectionCursor:
    head = ledger.project()
    result = ledger.commit(
        events,
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    return ProjectionCursor(
        world_revision=result.world_revision,
        deliberation_revision=result.deliberation_revision,
        ledger_sequence=result.ledger_sequence,
    )


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request: pytest.FixtureRequest, tmp_path: Path) -> LedgerPort:
    if request.param == "memory":
        return WorldLedger.in_memory(world_id=WORLD)
    return SQLiteWorldLedger(path=tmp_path / "history.sqlite3", world_id=WORLD)


def test_locator_uses_domain_identity_and_returns_exact_envelope(
    ledger: LedgerPort,
) -> None:
    event = _event("event:one", "ObservationRecorded", {"observation_id": "one"})
    locator = _locator(event)
    cursor = _commit(ledger, (event,))

    found = ledger.observation_events_at((locator,), cursor=cursor)

    assert found == (
        HistoricalLedgerEvent(
            event=event,
            event_cursor=ProjectionCursor(
                world_revision=1,
                deliberation_revision=0,
                ledger_sequence=1,
            ),
            event_envelope_hash=hashlib.sha256(
                canonical_event_json(event).encode("utf-8")
            ).hexdigest(),
        ),
    )


def test_same_observation_ref_across_types_uses_two_explicit_locators(
    ledger: LedgerPort,
) -> None:
    generic = _event(
        "event:generic:same", "ObservationRecorded", {"observation_id": "same"}
    )
    operator = _event(
        "event:operator:same",
        "OperatorObservationRecorded",
        {"observation_id": "same", "observation_hash": "a" * 64},
    )
    cursor = _commit(ledger, (generic, operator))
    projection = ledger.project_at(cursor)
    message_ref = next(
        item for item in projection.message_observations if item.observation_id == "same"
    )
    operator_ref = next(
        item for item in projection.operator_observations if item.observation_id == "same"
    )
    locators = tuple(
        sorted(
            (
                ObservationEventLocator.for_message(
                    world_id=WORLD,
                    observation_id=message_ref.observation_id,
                    source=message_ref.source,
                    source_event_id=message_ref.source_event_id,
                ),
                ObservationEventLocator.for_operator(
                    world_id=WORLD,
                    observation_id=operator_ref.observation_id,
                ),
            ),
            key=_locator_key,
        )
    )

    found = ledger.observation_events_at(locators, cursor=cursor)

    assert tuple((item.event.event_type, item.event.event_id) for item in found) == (
        ("ObservationRecorded", "event:generic:same"),
        ("OperatorObservationRecorded", "event:operator:same"),
    )


def _locator_key(locator: ObservationEventLocator) -> tuple[str, str, str]:
    return locator.observation_id, locator.event_type, locator.idempotency_key


def test_mixed_commit_returns_each_events_exact_cursor_but_only_accepts_tail(
    ledger: LedgerPort,
) -> None:
    operator = _event(
        "event:operator",
        "OperatorObservationRecorded",
        {"observation_id": "operator", "observation_hash": "b" * 64},
    )
    generic = _event(
        "event:generic", "ObservationRecorded", {"observation_id": "generic"}
    )
    tail = _commit(
        ledger,
        (_event("event:start", "WorldStarted", {}), operator, generic),
    )
    locators = tuple(sorted((_locator(generic), _locator(operator)), key=_locator_key))
    mid_commit = ProjectionCursor(
        world_revision=1,
        deliberation_revision=1,
        ledger_sequence=2,
    )

    with pytest.raises(ValueError, match="batch boundary"):
        ledger.observation_events_at(locators, cursor=mid_commit)
    found = ledger.observation_events_at(locators, cursor=tail)

    by_event = {item.event.event_id: item.event_cursor for item in found}
    assert by_event == {
        "event:operator": ProjectionCursor(
            world_revision=1, deliberation_revision=1, ledger_sequence=2
        ),
        "event:generic": ProjectionCursor(
            world_revision=2, deliberation_revision=1, ledger_sequence=3
        ),
    }


def test_old_commit_tail_is_stable_after_head_advances(ledger: LedgerPort) -> None:
    old = _event("event:old", "ObservationRecorded", {"observation_id": "old"})
    old_cursor = _commit(ledger, (old,))
    before = ledger.observation_events_at((_locator(old),), cursor=old_cursor)
    _commit(ledger, (_event("event:later", "WorldStarted", {}),))

    assert ledger.observation_events_at((_locator(old),), cursor=old_cursor) == before


def test_zero_cursor_is_valid_and_future_or_fake_cursor_is_rejected(
    ledger: LedgerPort,
) -> None:
    event = _event("event:later", "ObservationRecorded", {"observation_id": "later"})
    _commit(ledger, (event,))
    zero = ProjectionCursor(world_revision=0, deliberation_revision=0, ledger_sequence=0)

    assert ledger.observation_events_at((_locator(event),), cursor=zero) == ()
    for invalid in (
        ProjectionCursor(world_revision=0, deliberation_revision=0, ledger_sequence=1),
        ProjectionCursor(world_revision=99, deliberation_revision=99, ledger_sequence=99),
    ):
        with pytest.raises(ValueError, match="batch boundary"):
            ledger.observation_events_at((_locator(event),), cursor=invalid)


class _LyingLocators:
    def __len__(self) -> int:
        return 1

    def __iter__(self) -> Iterator[ObservationEventLocator]:
        for index in range(129):
            yield ObservationEventLocator(
                observation_id=f"observation:{index:03}",
                event_type="ObservationRecorded",
                idempotency_key=f"observation:{index:03}",
            )


class _LocatorSubclass(ObservationEventLocator):
    pass


@pytest.mark.parametrize("case", ["empty", "duplicate", "unsorted", "too_many", "lying"])
def test_locator_request_is_bounded_canonical_and_unique(
    ledger: LedgerPort, case: str
) -> None:
    event_a = _event("event:a", "ObservationRecorded", {"observation_id": "a"})
    event_b = _event("event:b", "ObservationRecorded", {"observation_id": "b"})
    cursor = _commit(ledger, (event_a, event_b))
    a = _locator(event_a)
    b = _locator(event_b)
    requests: object
    if case == "empty":
        requests = ()
    elif case == "duplicate":
        requests = (a, a)
    elif case == "unsorted":
        requests = (b, a)
    elif case == "too_many":
        requests = [
            ObservationEventLocator(
                observation_id=f"observation:{index:03}",
                event_type="ObservationRecorded",
                idempotency_key=f"identity:{index:03}",
            )
            for index in range(129)
        ]
    else:
        requests = _LyingLocators()

    with pytest.raises(ValueError):
        ledger.observation_events_at(requests, cursor=cursor)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observation_id", ""),
        ("observation_id", "x" * 513),
        ("event_type", "WorldStarted"),
        ("idempotency_key", ""),
        ("idempotency_key", "x" * 513),
    ],
)
def test_locator_fields_are_bounded(field: str, value: str) -> None:
    values = {
        "observation_id": "observation",
        "event_type": "ObservationRecorded",
        "idempotency_key": "identity",
    }
    values[field] = value
    with pytest.raises(ValueError):
        ObservationEventLocator(**values)


@pytest.mark.parametrize("attack", ["subclass", "mutated_type", "mutated_length"])
def test_locator_request_reconstructs_exact_bounded_values(
    ledger: LedgerPort, attack: str
) -> None:
    event = _event("event:guarded", "ObservationRecorded", {"observation_id": "guarded"})
    cursor = _commit(ledger, (event,))
    valid = _locator(event)
    if attack == "subclass":
        locator: object = _LocatorSubclass(
            observation_id=valid.observation_id,
            event_type=valid.event_type,
            idempotency_key=valid.idempotency_key,
        )
    else:
        locator = valid
        object.__setattr__(
            locator,
            "observation_id",
            7 if attack == "mutated_type" else "x" * 513,
        )

    with pytest.raises(ValueError):
        ledger.observation_events_at((locator,), cursor=cursor)  # type: ignore[arg-type]


def test_128_locators_are_supported(ledger: LedgerPort) -> None:
    events = tuple(
        _event(
            f"event:{index:03}",
            "ObservationRecorded",
            {"observation_id": f"observation:{index:03}"},
        )
        for index in range(128)
    )
    cursor = _commit(ledger, events)
    locators = tuple(sorted((_locator(event) for event in events), key=_locator_key))

    assert len(ledger.observation_events_at(locators, cursor=cursor)) == 128


class _NoHistoryScan(list[object]):
    def __iter__(self):
        raise AssertionError("history scanned")


def test_memory_uses_identity_index_without_project_or_history_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    event = _event("event:indexed", "ObservationRecorded", {"observation_id": "indexed"})
    cursor = _commit(ledger, (event,))
    monkeypatch.setattr(
        ledger,
        "project_at",
        lambda _cursor: (_ for _ in ()).throw(AssertionError("project_at called")),
    )
    ledger._events = _NoHistoryScan(ledger._events)  # type: ignore[assignment]  # noqa: SLF001

    assert ledger.observation_events_at((_locator(event),), cursor=cursor)[0].event == event


def test_sqlite_routes_by_unique_idempotency_index_without_json_scan(tmp_path: Path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "routing.sqlite3", world_id=WORLD)
    event = _event("event:routing", "ObservationRecorded", {"observation_id": "routing"})
    cursor = _commit(ledger, (event,))
    statements: list[str] = []
    ledger._connection.set_trace_callback(statements.append)  # noqa: SLF001

    ledger.observation_events_at((_locator(event),), cursor=cursor)

    assert any("IDEMPOTENCY_KEY IN" in statement.upper() for statement in statements)
    assert all("JSON_EXTRACT" not in statement.upper() for statement in statements)


@pytest.mark.parametrize("field", ["event_type", "observation_id"])
def test_sqlite_routing_field_tamper_is_still_located_and_fails_closed(
    tmp_path: Path, field: str
) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / f"tamper-{field}.sqlite3", world_id=WORLD)
    event = _event(
        "event:tamper",
        "OperatorObservationRecorded",
        {"observation_id": "tamper", "observation_hash": "c" * 64},
    )
    locator = _locator(event)
    cursor = _commit(ledger, (event,))
    raw = json.loads(canonical_event_json(event))
    if field == "event_type":
        raw["event_type"] = "WorldStarted"
    else:
        payload = json.loads(raw["payload_json"])
        payload["observation_id"] = "forged"
        raw["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    ledger._connection.execute(  # noqa: SLF001
        "UPDATE world_v2_events SET event_json = ? WHERE world_id = ?",
        (json.dumps(raw, sort_keys=True, separators=(",", ":")), WORLD),
    )

    with pytest.raises(LedgerIntegrityError):
        ledger.observation_events_at((locator,), cursor=cursor)


def test_sqlite_tampered_unique_index_column_fails_during_cursor_proof(
    tmp_path: Path,
) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "idempotency-tamper.sqlite3", world_id=WORLD)
    event = _event("event:target", "ObservationRecorded", {"observation_id": "target"})
    _commit(ledger, (event,))
    tail = _commit(ledger, (_event("event:tail", "WorldStarted", {}),))
    ledger._connection.execute(  # noqa: SLF001
        "UPDATE world_v2_events SET idempotency_key = ? WHERE event_id = ?",
        ("forged:index-key", event.event_id),
    )

    with pytest.raises(LedgerIntegrityError, match="ledger row"):
        ledger.observation_events_at((_locator(event),), cursor=tail)


def test_sqlite_verifies_each_candidate_commit_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "cache.sqlite3", world_id=WORLD)
    first = _event("event:first", "ObservationRecorded", {"observation_id": "first"})
    second = _event("event:second", "ObservationRecorded", {"observation_id": "second"})
    cursor = _commit(ledger, (first, second))
    locators = tuple(sorted((_locator(first), _locator(second)), key=_locator_key))
    original = ledger._verified_commit_locked  # noqa: SLF001
    calls = 0

    def counted(commit_id: str):
        nonlocal calls
        calls += 1
        return original(commit_id)

    monkeypatch.setattr(ledger, "_verified_commit_locked", counted)

    ledger.observation_events_at(locators, cursor=cursor)

    assert calls == 1


def test_sqlite_boundary_verification_prevents_candidate_prefix_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "prefix-cache.sqlite3", world_id=WORLD)
    first = _event("event:prefix:first", "ObservationRecorded", {"observation_id": "first"})
    _commit(ledger, (first,))
    second = _event(
        "event:prefix:second", "ObservationRecorded", {"observation_id": "second"}
    )
    _commit(ledger, (second,))
    tail = _commit(ledger, (_event("event:prefix:tail", "WorldStarted", {}),))
    locators = tuple(sorted((_locator(first), _locator(second)), key=_locator_key))
    original = ledger._replay_locked  # noqa: SLF001
    replay_calls = 0

    def counted(**kwargs):
        nonlocal replay_calls
        replay_calls += 1
        return original(**kwargs)

    monkeypatch.setattr(ledger, "_replay_locked", counted)

    ledger.observation_events_at(locators, cursor=tail)

    assert replay_calls == 1


def test_sqlite_adds_no_observation_schema(tmp_path: Path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "schema.sqlite3", world_id=WORLD)
    tables = {
        str(row[0])
        for row in ledger._connection.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    head_columns = {
        str(row[1])
        for row in ledger._connection.execute(  # noqa: SLF001
            "PRAGMA table_info(world_v2_heads)"
        )
    }

    assert "world_v2_observation_index" not in tables
    assert "observation_index_hash" not in head_columns


def test_shared_commit_count_boundary_and_heterogeneous_rejection(
    ledger: LedgerPort, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_module, "OBSERVATION_HISTORY_MAX_COMMIT_EVENTS", 2)
    accepted = (
        _event("event:count:one", "WorldStarted", {}),
        _event("event:count:two", "WorldStarted", {}),
    )
    _commit(ledger, accepted)
    before = ledger.project()

    with pytest.raises(ValueError, match="count"):
        ledger.commit(
            tuple(_event(f"event:excess:{index}", "WorldStarted", {}) for index in range(3)),
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    with pytest.raises(ValueError, match="WorldEvent"):
        ledger.commit(
            [object()],  # type: ignore[list-item]
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    assert ledger.project() == before


def test_shared_commit_canonical_byte_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = _event("event:byte-boundary", "WorldStarted", {})
    exact_size = len(canonical_event_json(event).encode("utf-8"))
    monkeypatch.setattr(ledger_module, "OBSERVATION_HISTORY_MAX_BYTES", exact_size)
    adapters: tuple[LedgerPort, ...] = (
        WorldLedger.in_memory(world_id=WORLD),
        SQLiteWorldLedger(path=tmp_path / "bytes-ok.sqlite3", world_id=WORLD),
    )
    for adapter in adapters:
        _commit(adapter, (event,))

    monkeypatch.setattr(ledger_module, "OBSERVATION_HISTORY_MAX_BYTES", exact_size - 1)
    rejected: tuple[LedgerPort, ...] = (
        WorldLedger.in_memory(world_id=WORLD),
        SQLiteWorldLedger(path=tmp_path / "bytes-reject.sqlite3", world_id=WORLD),
    )
    for adapter in rejected:
        with pytest.raises(ValueError, match="bytes"):
            _commit(adapter, (event,))
        assert adapter.project().ledger_sequence == 0


def test_stored_oversized_history_is_rejected_by_both_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = _event("event:stored-budget", "ObservationRecorded", {"observation_id": "stored"})
    memory = WorldLedger.in_memory(world_id=WORLD)
    memory_cursor = _commit(memory, (event,))
    sqlite = SQLiteWorldLedger(path=tmp_path / "stored-budget.sqlite3", world_id=WORLD)
    sqlite_cursor = _commit(sqlite, (event,))
    monkeypatch.setattr(ledger_module, "OBSERVATION_HISTORY_MAX_BYTES", 1)
    monkeypatch.setattr(sqlite_ledger_module, "OBSERVATION_HISTORY_MAX_BYTES", 1)

    with pytest.raises(LedgerIntegrityError, match="budget"):
        memory.observation_events_at((_locator(event),), cursor=memory_cursor)
    with pytest.raises(LedgerIntegrityError, match="budget"):
        sqlite.observation_events_at((_locator(event),), cursor=sqlite_cursor)


def test_history_budget_is_per_commit_not_aggregate_across_small_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _event("event:small:first", "ObservationRecorded", {"observation_id": "first"})
    second = _event(
        "event:small:second", "ObservationRecorded", {"observation_id": "second"}
    )
    per_commit_bytes = max(
        len(canonical_event_json(first).encode("utf-8")),
        len(canonical_event_json(second).encode("utf-8")),
    )
    memory = WorldLedger.in_memory(world_id=WORLD)
    _commit(memory, (first,))
    memory_cursor = _commit(memory, (second,))
    sqlite = SQLiteWorldLedger(path=tmp_path / "per-commit.sqlite3", world_id=WORLD)
    _commit(sqlite, (first,))
    sqlite_cursor = _commit(sqlite, (second,))
    monkeypatch.setattr(ledger_module, "OBSERVATION_HISTORY_MAX_COMMIT_EVENTS", 1)
    monkeypatch.setattr(sqlite_ledger_module, "OBSERVATION_HISTORY_MAX_COMMIT_EVENTS", 1)
    monkeypatch.setattr(ledger_module, "OBSERVATION_HISTORY_MAX_BYTES", per_commit_bytes)
    monkeypatch.setattr(sqlite_ledger_module, "OBSERVATION_HISTORY_MAX_BYTES", per_commit_bytes)
    locators = tuple(sorted((_locator(first), _locator(second)), key=_locator_key))

    assert len(memory.observation_events_at(locators, cursor=memory_cursor)) == 2
    assert len(sqlite.observation_events_at(locators, cursor=sqlite_cursor)) == 2


class _WorldEventSubclass(WorldEvent):
    pass


@pytest.mark.parametrize(
    "attack",
    [
        "subclass",
        "model_construct",
        "extra",
        "instance_method",
        "cycle",
        "huge_key",
        "huge_string",
        "huge_integer",
    ],
)
def test_commit_preflight_rebuilds_closed_plain_world_event_storage(
    ledger: LedgerPort,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    valid = _event("event:hostile", "WorldStarted", {})
    storage = dict(object.__getattribute__(valid, "__dict__"))
    if attack == "subclass":
        hostile: object = _WorldEventSubclass.model_validate(storage)
    elif attack == "model_construct":
        storage.pop("payload_hash")
        hostile = WorldEvent.model_construct(**storage)
    else:
        hostile = WorldEvent.model_validate(storage)
        if attack == "extra":
            object.__setattr__(hostile, "__pydantic_extra__", {"forged": "value"})
        elif attack == "instance_method":
            def explode(*_args, **_kwargs):
                raise AssertionError("untrusted model_dump was invoked")

            object.__getattribute__(hostile, "__dict__")["model_dump"] = explode
        elif attack == "cycle":
            cycle: list[object] = []
            cycle.append(cycle)
            object.__setattr__(hostile, "actor", cycle)
        elif attack == "huge_key":
            object.__setattr__(hostile, "actor", {"k" * 513: "value"})
        elif attack == "huge_string":
            monkeypatch.setattr(ledger_module, "OBSERVATION_HISTORY_MAX_BYTES", 512)
            object.__setattr__(hostile, "actor", "x" * 513)
        else:
            object.__setattr__(hostile, "actor", 1 << 100)
    before = ledger.project()

    with pytest.raises(ValueError):
        ledger.commit(
            (hostile,),  # type: ignore[arg-type]
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    assert ledger.project() == before


def test_sqlite_history_read_uses_one_snapshot_and_rolls_back_errors(tmp_path: Path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "snapshot.sqlite3", world_id=WORLD)
    event = _event("event:snapshot", "ObservationRecorded", {"observation_id": "snapshot"})
    cursor = _commit(ledger, (event, _event("event:snapshot:tail", "WorldStarted", {})))
    statements: list[str] = []
    ledger._connection.set_trace_callback(statements.append)  # noqa: SLF001

    ledger.observation_events_at((_locator(event),), cursor=cursor)

    assert statements[0].upper() == "BEGIN"
    assert statements[-1].upper() == "COMMIT"
    statements.clear()
    mid = ProjectionCursor(world_revision=1, deliberation_revision=0, ledger_sequence=1)
    with pytest.raises(ValueError, match="batch boundary"):
        ledger.observation_events_at((_locator(event),), cursor=mid)
    assert statements[0].upper() == "BEGIN"
    assert statements[-1].upper() == "ROLLBACK"
    assert not ledger._connection.in_transaction  # noqa: SLF001


def test_sqlite_database_errors_are_normalized_after_snapshot_rollback(tmp_path: Path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "closed.sqlite3", world_id=WORLD)
    event = _event("event:closed", "ObservationRecorded", {"observation_id": "closed"})
    cursor = _commit(ledger, (event,))
    ledger.close()

    with pytest.raises(LedgerIntegrityError, match="snapshot read failed"):
        ledger.observation_events_at((_locator(event),), cursor=cursor)
