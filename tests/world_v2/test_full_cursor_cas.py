from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import LedgerPort, WorldLedger
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


WORLD_ID = "world:full-cursor-cas"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _event(event_id: str) -> WorldEvent:
    payload: dict[str, object] = {}
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type="WorldStarted",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:full-cursor-cas",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:full-cursor-cas",
        idempotency_key=domain_idempotency_key(
            event_type="WorldStarted", world_id=WORLD_ID, payload=payload
        )
        or f"identity:{event_id}",
        payload=payload,
    )


def _cursor(ledger: LedgerPort) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request: pytest.FixtureRequest, tmp_path: Path) -> LedgerPort:
    if request.param == "memory":
        return WorldLedger.in_memory(world_id=WORLD_ID)
    instance = SQLiteWorldLedger(path=tmp_path / "full-cursor-cas.sqlite3", world_id=WORLD_ID)
    request.addfinalizer(instance.close)
    return instance


def test_commit_at_cursor_accepts_the_exact_current_cursor(ledger: LedgerPort) -> None:
    zero = ProjectionCursor(world_revision=0, deliberation_revision=0, ledger_sequence=0)
    first = ledger.commit_at_cursor((_event("event:full-cursor:first"),), expected_cursor=zero)

    assert first.ledger_sequence == 1
    second = ledger.commit_at_cursor(
        (_event("event:full-cursor:second"),), expected_cursor=_cursor(ledger)
    )

    assert second.world_revision == 2
    assert second.deliberation_revision == 0
    assert second.ledger_sequence == 2


def test_commit_at_cursor_rejects_a_cursor_with_only_the_sequence_forged(
    ledger: LedgerPort,
) -> None:
    ledger.commit(
        (_event("event:full-cursor:seed"),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    actual = _cursor(ledger)
    forged = actual.model_copy(update={"ledger_sequence": 0})

    with pytest.raises(ConcurrencyConflict, match="stale projection cursor"):
        ledger.commit_at_cursor(
            (_event("event:full-cursor:reject"),), expected_cursor=forged
        )

    assert _cursor(ledger) == actual


def test_commit_at_cursor_checks_both_revision_streams_even_for_one_stream_writes(
    ledger: LedgerPort,
) -> None:
    ledger.commit(
        (_event("event:full-cursor:seed-revisions"),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    actual = _cursor(ledger)
    forged = actual.model_copy(update={"deliberation_revision": 1})

    with pytest.raises(ConcurrencyConflict, match="stale projection cursor"):
        ledger.commit_at_cursor(
            (_event("event:full-cursor:reject-revisions"),), expected_cursor=forged
        )

    assert _cursor(ledger) == actual
