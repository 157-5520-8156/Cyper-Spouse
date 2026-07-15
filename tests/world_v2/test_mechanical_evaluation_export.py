from dataclasses import replace
from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.mechanical_evaluation_export import (
    MechanicalEvaluationExporter,
    VisibleActionLatencySample,
)
from companion_daemon.world_v2.mechanical_evaluation_scope import (
    MechanicalEvaluationScope,
    PerformanceSampleExpectation,
    RandomDrawExpectation,
)
from companion_daemon.world_v2.schemas import Action, ClaimLease, WorldEvent


def test_exporter_binds_only_the_fixture_cursor_and_declared_performance_samples() -> None:
    ledger = WorldLedger.in_memory(world_id="world:mechanical-export")
    now = datetime(2026, 7, 15, tzinfo=UTC)
    ledger.commit([
        WorldEvent.from_payload(schema_version="world-v2.1", event_id="event:started", world_id=ledger.world_id,
        event_type="WorldStarted", logical_time=now, created_at=now, actor="system:test", source="test",
        trace_id="trace", causation_id="cause", correlation_id="correlation", idempotency_key="started", payload={})
    ], expected_world_revision=0, expected_deliberation_revision=0)
    evidence = ledger.export_replay_evidence()
    scope = MechanicalEvaluationScope(
        fixture_id="replay.happy", fixture_version="fixtures.1", world_id=ledger.world_id,
        start_ledger_sequence=0, end_ledger_sequence=1, action_ids_expected_to_settle=(), affect_assertions=(),
        random_draw_expectation=RandomDrawExpectation("not_applicable"),
        performance_samples=(PerformanceSampleExpectation("hot.1", "hot"),),
    )

    report = MechanicalEvaluationExporter().export(
        scope=scope, replay_evidence=evidence,
        latency_samples=(VisibleActionLatencySample("hot.1", "hot", 450.0),),
    )

    assert report.evaluation.hard_invariant_violations == 0
    assert report.evaluation.hot_visible_action_p95_ms == 450.0
    assert report.trace.fixture_manifest_hash == scope.fixture_manifest_hash


def test_exporter_reports_a_declared_action_that_is_still_nonterminal() -> None:
    ledger = WorldLedger.in_memory(world_id="world:mechanical-action-leak")
    now = datetime(2026, 7, 15, tzinfo=UTC)
    evidence = ledger.export_replay_evidence()
    action = Action(
        schema_version="world-v2.1", action_id="action:reply", world_id=ledger.world_id,
        logical_time=now, created_at=now, trace_id="trace", causation_id="cause", correlation_id="correlation",
        kind="reply", layer="external_action", intent_ref="intent:reply", actor="agent:companion",
        target="user:primary", payload_ref="payload:reply", payload_hash="sha256:reply",
        idempotency_key="effect:reply", budget_reservation_id="reservation:reply",
        claim_lease=ClaimLease(
            owner_id="pump", attempt_id="attempt", acquired_at=now, expires_at=now + timedelta(seconds=1)
        ),
        state="claimed", recovery_policy="effect_once",
    )
    projection = evidence.projection.model_copy(update={"actions": (action,), "pending_actions": (action,)})
    scoped = replace(evidence, projection=projection, replay=projection)
    scope = MechanicalEvaluationScope(
        fixture_id="replay.action-leak", fixture_version="fixtures.1", world_id=ledger.world_id,
        start_ledger_sequence=0, end_ledger_sequence=0, action_ids_expected_to_settle=(action.action_id,),
        affect_assertions=(), random_draw_expectation=RandomDrawExpectation("not_applicable"),
        performance_samples=(PerformanceSampleExpectation("hot.1", "hot"),),
    )

    report = MechanicalEvaluationExporter().export(
        scope=scope, replay_evidence=scoped,
        latency_samples=(VisibleActionLatencySample("hot.1", "hot", 450.0),),
    )

    assert report.evaluation.nonterminal_action_leaks == 1
