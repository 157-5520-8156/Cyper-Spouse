from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

from companion_daemon.world_v2.npc_ecology_health import npc_ecology_health_snapshot
from companion_daemon.world_v2.schemas import WorldEvent


NOW = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)


class _Ledger:
    def __init__(self, events: tuple[WorldEvent, ...]) -> None:
        self._events = {event.event_id: (event, None) for event in events}

    def lookup_event_commit(self, event_id: str):
        return self._events.get(event_id)


def _event(
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    logical_time: datetime = NOW,
    actor: str = "worker:test",
    causation_id: str | None = None,
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id="world:test",
        event_type=event_type,
        logical_time=logical_time,
        created_at=logical_time,
        actor=actor,
        source="test:npc-health",
        trace_id="trace:npc-health",
        causation_id=causation_id or "event:cause:npc-health",
        correlation_id="correlation:npc-health",
        idempotency_key="idempotency:" + event_id,
        payload=payload,
    )


def _authority(events: tuple[WorldEvent, ...]):
    return tuple(
        SimpleNamespace(
            event_id=event.event_id,
            event_type=event.event_type,
            payload_hash=event.payload_hash,
            logical_time=event.logical_time,
        )
        for event in events
    )


def test_npc_health_reports_identity_calls_no_ops_reappearance_and_failures() -> None:
    actor_attempt = _event(
        event_id="event:npc-ecology:model-result:actor:recent",
        event_type="ModelResultRecorded",
        actor="npc:lin",
        payload={
            "audit_json": json.dumps(
                {
                    "route": {"reason_code": "npc_ecology_actor"},
                    "status": "proposal_validated",
                    "outcome": "winner",
                }
            )
        },
    )
    world_attempt = _event(
        event_id="event:npc-ecology:model-result:world:recent",
        event_type="ModelResultRecorded",
        causation_id="event:clock:recent",
        payload={
            "audit_json": json.dumps(
                {
                    "route": {"reason_code": "npc_ecology_world"},
                    "status": "main_timeout",
                    "outcome": "timeout",
                    "failure_code": "provider_timeout",
                }
            )
        },
    )
    old_actor = _event(
        event_id="event:npc-ecology:actor:old",
        event_type="ProposalRecorded",
        logical_time=NOW - timedelta(days=2),
        actor="npc:lin",
        payload={
            "proposal_id": "proposal:npc-ecology:old",
            "proposal_kind": "npc_ecology",
            "trigger_id": "event:clock:old",
            "decision_payload": {"decision": "no_op", "npc_ref": "npc:lin"},
        },
    )
    actor = _event(
        event_id="event:npc-ecology:actor:recent",
        event_type="ProposalRecorded",
        actor="npc:lin",
        payload={
            "proposal_id": "proposal:npc-ecology:recent",
            "proposal_kind": "npc_ecology",
            "trigger_id": "event:clock:recent",
            "decision_payload": {"decision": "propose", "npc_ref": "npc:lin"},
        },
    )
    world = _event(
        event_id="event:npc-ecology:world:recent",
        event_type="ProposalRecorded",
        payload={
            "proposal_id": "proposal:npc-ecology-world:recent",
            "proposal_kind": "npc_ecology_world_adjudication",
            "actor_decision_event_ref": actor.event_id,
            "decision_payload": {"decision": "no_op", "outcomes": []},
        },
    )
    attributed_failure = _event(
        event_id="event:life-ecology:completed:recent",
        event_type="TriggerProcessCompleted",
        causation_id="event:clock:recent",
        payload={
            "runtime_outcome_ref": (
                "life-ecology:technical_failure.npc_ecology.npc_ecology.world_author_failure"
            )
        },
    )
    unattributed_failure = _event(
        event_id="event:life-ecology:completed:actor-failed",
        event_type="TriggerProcessCompleted",
        causation_id="event:clock:actor-failed",
        payload={
            "runtime_outcome_ref": (
                "life-ecology:technical_failure.npc_ecology.npc_ecology.actor_model_failure"
            )
        },
    )
    reactivated = _event(
        event_id="event:npc:lin:reactivated",
        event_type="NpcStatusChanged",
        payload={
            "npc_before": {"npc_id": "lin", "status": "dormant"},
            "npc_after": {"npc_id": "lin", "status": "active"},
        },
    )
    events = (
        actor_attempt,
        world_attempt,
        old_actor,
        actor,
        world,
        attributed_failure,
        unattributed_failure,
        reactivated,
    )
    projection = SimpleNamespace(
        logical_time=NOW,
        committed_world_event_refs=_authority(events),
        npcs=(
            SimpleNamespace(
                npc_id="lin",
                source_event_ref="event:settlement:first",
                promotion_edge=SimpleNamespace(provisional_entity_ref="provisional:npc:lin"),
            ),
            SimpleNamespace(
                npc_id="orphan",
                source_event_ref="event:settlement:orphan",
                promotion_edge=SimpleNamespace(provisional_entity_ref="provisional:npc:orphan"),
            ),
        ),
        world_occurrences=(
            SimpleNamespace(status="settled", participant_refs=("provisional:npc:lin",)),
            SimpleNamespace(status="settled", participant_refs=("npc:lin",)),
        ),
    )
    identity = SimpleNamespace(npc_ref="npc:lin", provisional_entity_ref="provisional:npc:lin")

    health = npc_ecology_health_snapshot(
        projection=projection,
        ledger=_Ledger(events),
        identity_views=(identity,),
    )

    assert health["dynamic_count"] == 2
    assert health["promotion_closed_count"] == 1
    assert health["promotion_closure_failure_count"] == 1
    assert health["promotion_closure_failure_refs"] == ["npc:orphan"]
    assert health["actor_completed_call_count"] == 2
    assert health["actor_completed_call_count_24h"] == 1
    assert health["actor_no_op_rate"] == {
        "status": "measured",
        "sample_count": 2,
        "value_bp": 5000,
    }
    assert health["world_completed_call_count"] == 1
    assert health["actor_model_attempt_count"] == 1
    assert health["world_model_attempt_count"] == 1
    assert health["world_failed_model_attempt_count"] == 1
    assert health["provider_attempt_evidence"] == "model_result_recorded"
    assert health["world_no_op_count"] == 1
    assert health["scene_reappearance_count"] == 1
    assert health["reappeared_npc_count"] == 1
    assert health["lifecycle_reactivation_count"] == 1
    assert health["technical_failure_count"] == 2
    assert health["unattributed_technical_failure_count"] == 1
    assert health["actor_usage"]["status"] == "unknown"
    assert health["actor_usage"]["input_tokens"] is None

    by_ref = {item["npc_ref"]: item for item in health["per_npc"]}
    assert by_ref["npc:lin"]["actor_completed_call_count"] == 2
    assert by_ref["npc:lin"]["world_completed_call_count"] == 1
    assert by_ref["npc:lin"]["technical_failure_count"] == 1
    assert by_ref["npc:lin"]["scene_reappearance_count"] == 1
    assert by_ref["npc:lin"]["lifecycle_reactivation_count"] == 1
    assert by_ref["npc:orphan"]["promotion_closed"] is False


def test_npc_health_uses_not_measured_rates_and_unknown_usage_without_evidence() -> None:
    projection = SimpleNamespace(
        logical_time=NOW,
        committed_world_event_refs=(),
        npcs=(),
        world_occurrences=(),
    )

    health = npc_ecology_health_snapshot(
        projection=projection,
        ledger=_Ledger(()),
        identity_views=(),
    )

    assert health["actor_no_op_rate"] == {
        "status": "not_measured",
        "sample_count": 0,
        "value_bp": None,
    }
    assert health["world_no_op_rate"]["status"] == "not_measured"
    assert health["provider_attempt_evidence"] == "model_result_recorded"
    assert health["world_usage"]["status"] == "unknown"
    assert health["world_usage"]["cost"] is None
