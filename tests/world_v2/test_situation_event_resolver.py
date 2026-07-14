from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)


def _event(world_id: str, event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type, world_id=world_id, payload=payload
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:situation-resolver",
        causation_id="cause:situation-resolver",
        correlation_id="correlation:situation-resolver",
        idempotency_key=identity or event_id,
        payload=payload,
    )


def _populate(ledger: WorldLedger | SQLiteWorldLedger, *, world_id: str) -> tuple[WorldEvent, ...]:
    events = (
        _event(world_id, "event:start", "WorldStarted", {}),
        *(
            _event(
                world_id,
                f"event:observation:{index}",
                "ObservationRecorded",
                {"observation_id": f"observation:{index}"},
            )
            for index in range(1, 100)
        ),
    )
    ledger.commit(
        events,
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return events


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_ledger_resolves_only_requested_situation_sources_by_identity(
    adapter: str, tmp_path
) -> None:
    world_id = f"world:resolver:{adapter}"
    ledger: WorldLedger | SQLiteWorldLedger
    if adapter == "memory":
        ledger = WorldLedger.in_memory(world_id=world_id)
    else:
        ledger = SQLiteWorldLedger(path=tmp_path / "resolver.sqlite3", world_id=world_id)
    try:
        events = _populate(ledger, world_id=world_id)
        projection = ledger.project()
        requested = (events[0].event_id, events[-1].event_id)
        resolved = ledger.resolve_committed_event_refs(
            requested, at_world_revision=projection.world_revision
        )
        assert tuple(sorted(resolved)) == tuple(sorted(requested))
        assert resolved[events[-1].event_id].payload_hash == events[-1].payload_hash
        assert ledger.resolve_initial_world_event_ref(
            at_world_revision=projection.world_revision
        ).event_id == events[0].event_id

        with pytest.raises(ValueError, match="newer than|unavailable"):
            ledger.resolve_committed_event_refs(
                (events[-1].event_id,), at_world_revision=1
            )
    finally:
        if isinstance(ledger, SQLiteWorldLedger):
            ledger.close()
