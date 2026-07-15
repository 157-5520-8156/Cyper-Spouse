from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchHandle
from companion_daemon.world_v2.ledger import LedgerPort, WorldLedger
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


WORLD_ID = "world:accepted-manifest-v3-gate"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _direct_v3_acceptance() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:direct-v3-acceptance",
        world_id=WORLD_ID,
        event_type="AcceptanceRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:accepted-manifest-v3-gate",
        causation_id="cause:accepted-manifest-v3-gate",
        correlation_id="correlation:accepted-manifest-v3-gate",
        idempotency_key="identity:direct-v3-acceptance",
        payload={"manifest_version": "acceptance-manifest.3"},
    )


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request: pytest.FixtureRequest, tmp_path: Path) -> LedgerPort:
    if request.param == "memory":
        return WorldLedger.in_memory(world_id=WORLD_ID)
    instance = SQLiteWorldLedger(path=tmp_path / "accepted-v3-gate.sqlite3", world_id=WORLD_ID)
    request.addfinalizer(instance.close)
    return instance


@pytest.mark.parametrize("method", ["commit", "commit_at_cursor"])
def test_normal_ledger_paths_reject_v3_accepted_manifest_without_recorder_capability(
    ledger: LedgerPort, method: str
) -> None:
    event = _direct_v3_acceptance()

    with pytest.raises(ValueError, match="accepted_manifest.recorder_capability_required"):
        if method == "commit":
            ledger.commit(
                (event,), expected_world_revision=0, expected_deliberation_revision=0
            )
        else:
            ledger.commit_at_cursor(
                (event,),
                expected_cursor=ProjectionCursor(
                    world_revision=0, deliberation_revision=0, ledger_sequence=0
                ),
            )

    projection = ledger.project()
    assert projection.world_revision == 0
    assert projection.deliberation_revision == 0
    assert projection.ledger_sequence == 0


def test_accepted_commit_requires_a_configured_batch_issuer(ledger: LedgerPort) -> None:
    with pytest.raises(ValueError, match="accepted_manifest.recorder_capability_required"):
        ledger.commit_accepted(
            AcceptedLedgerBatchHandle(),
            expected_cursor=ProjectionCursor(
                world_revision=0, deliberation_revision=0, ledger_sequence=0
            ),
        )
