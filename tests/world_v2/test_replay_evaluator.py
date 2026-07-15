from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.replay_evaluator import ReplayEvaluator
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


def test_replay_evaluator_accepts_identical_deterministic_rebuild() -> None:
    ledger = WorldLedger.in_memory(world_id="world:replay-evaluator")
    now = datetime(2026, 7, 15, tzinfo=UTC)
    ledger.commit(
        [
            WorldEvent.from_payload(
                schema_version="world-v2.1", event_id="event:started", world_id=ledger.world_id,
                event_type="WorldStarted", logical_time=now, created_at=now, actor="system:test",
                source="test", trace_id="trace", causation_id="cause", correlation_id="correlation",
                idempotency_key="world-started", payload={},
            )
        ], expected_world_revision=0, expected_deliberation_revision=0,
    )
    result = ReplayEvaluator().evaluate(evidence=ledger.export_replay_evidence())
    assert result.passed
    assert result.replay_hash_matches
    assert result.mechanism_checks[0] == "same_cursor_replay_evidence"


def test_replay_evidence_is_exactly_bound_to_a_committed_cursor() -> None:
    ledger = WorldLedger.in_memory(world_id="world:replay-evidence")
    evidence = ledger.export_replay_evidence()
    assert evidence.cursor.ledger_sequence == 0
    assert evidence.events == ()
    assert evidence.commits == ()

    with pytest.raises(ValueError, match="committed batch boundary"):
        ledger.export_replay_evidence(
            at_cursor=ProjectionCursor(
                world_revision=0, deliberation_revision=0, ledger_sequence=1
            )
        )


def test_replay_evaluator_reports_semantic_divergence() -> None:
    ledger = WorldLedger.in_memory(world_id="world:replay-evaluator")
    projection = ledger.project()
    replay = projection.model_copy(update={"semantic_hash": "a" * 64})
    result = ReplayEvaluator().evaluate(projection=projection, replay=replay)
    assert not result.passed
    assert result.findings[0].code == "replay_hash_mismatch"


def test_sqlite_ledger_exports_same_transaction_replay_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    with SQLiteWorldLedger(
        path=tmp_path / "replay-evidence.sqlite3", world_id="world:sqlite-replay-evidence"
    ) as ledger:
        ledger.commit(
            [
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id="event:started",
                    world_id=ledger.world_id,
                    event_type="WorldStarted",
                    logical_time=now,
                    created_at=now,
                    actor="system:test",
                    source="test",
                    trace_id="trace",
                    causation_id="cause",
                    correlation_id="correlation",
                    idempotency_key="world-started",
                    payload={},
                )
            ],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
        evidence = ledger.export_replay_evidence()
    assert evidence.cursor.ledger_sequence == 1
    assert evidence.projection == evidence.replay
    assert tuple(item.result.event_ids for item in evidence.commits) == (("event:started",),)
    assert ReplayEvaluator().evaluate(evidence=evidence).passed


@pytest.mark.asyncio
async def test_runtime_exposes_read_only_replay_evaluation() -> None:
    runtime = WorldRuntime.in_memory(world_id="world:runtime-replay-evaluator")
    result = await runtime.evaluate_replay()
    assert result.passed


@pytest.mark.asyncio
async def test_runtime_uses_sqlite_same_transaction_replay_evidence(tmp_path: Path) -> None:
    with SQLiteWorldLedger(
        path=tmp_path / "runtime-replay-evidence.sqlite3", world_id="world:runtime-sqlite-replay"
    ) as ledger:
        result = await WorldRuntime(world_id=ledger.world_id, ledger=ledger).evaluate_replay()
    assert result.passed
    assert result.mechanism_checks[0] == "same_cursor_replay_evidence"
