from __future__ import annotations

from datetime import UTC, datetime

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id="world-v2-ledger-test",
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace-ledger-1",
        causation_id="cause-ledger-1",
        correlation_id="correlation-ledger-1",
        idempotency_key=event_id,
        payload=payload,
    )


def test_audit_event_does_not_invalidate_world_revision_or_projection_hash() -> None:
    ledger = WorldLedger.in_memory(world_id="world-v2-ledger-test")

    observed = ledger.commit(
        [event("event-observation-1", "ObservationRecorded", {"observation_id": "obs-1"})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    projection_before_audit = ledger.project()

    audited = ledger.commit(
        [event("event-proposal-1", "ProposalRecorded", {"proposal_id": "proposal-1"})],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    projection_after_audit = ledger.project()

    assert observed.world_revision == 1
    assert observed.deliberation_revision == 0
    assert audited.world_revision == 1
    assert audited.deliberation_revision == 1
    assert projection_after_audit.semantic_hash == projection_before_audit.semantic_hash
    assert projection_after_audit.observation_refs == ("obs-1",)

    rebuilt = ledger.rebuild()
    assert rebuilt == projection_after_audit


def test_revision_streams_do_not_make_each_other_stale_and_retry_joins() -> None:
    ledger = WorldLedger.in_memory(world_id="world-v2-ledger-test")
    first_observation = event(
        "event-observation-1", "ObservationRecorded", {"observation_id": "obs-1"}
    )
    ledger.commit(
        [first_observation],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [event("event-proposal-1", "ProposalRecorded", {"proposal_id": "proposal-1"})],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )

    world_commit = ledger.commit(
        [event("event-observation-2", "ObservationRecorded", {"observation_id": "obs-2"})],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert world_commit.world_revision == 2
    assert world_commit.deliberation_revision == 1

    retried = ledger.commit(
        [first_observation],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    assert retried.event_ids == ("event-observation-1",)
    assert retried.world_revision == 1
    assert retried.deliberation_revision == 0
    assert retried.ledger_sequence == 1


def test_acceptance_record_advances_the_world_revision() -> None:
    ledger = WorldLedger.in_memory(world_id="world-v2-ledger-test")

    committed = ledger.commit(
        [
            event(
                "event-acceptance-1",
                "AcceptanceRecorded",
                {"acceptance_id": "acceptance-1", "status": "accepted"},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=999,
    )

    assert committed.world_revision == 1
    assert committed.deliberation_revision == 0
