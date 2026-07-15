from datetime import UTC, datetime

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.replay_evaluator import ReplayEvaluator
from companion_daemon.world_v2.schemas import WorldEvent


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
    result = ReplayEvaluator().evaluate(projection=ledger.project(), replay=ledger.rebuild())
    assert result.passed
    assert result.replay_hash_matches


def test_replay_evaluator_reports_semantic_divergence() -> None:
    ledger = WorldLedger.in_memory(world_id="world:replay-evaluator")
    projection = ledger.project()
    replay = projection.model_copy(update={"semantic_hash": "a" * 64})
    result = ReplayEvaluator().evaluate(projection=projection, replay=replay)
    assert not result.passed
    assert result.findings[0].code == "replay_hash_mismatch"
