from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.errors import IdempotencyConflict
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.schemas import WorldEvent


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    logical_time: datetime = NOW,
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id="world-v2-ledger-test",
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id="world-v2-ledger-test",
        event_type=event_type,
        logical_time=logical_time,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace-ledger-1",
        causation_id="cause-ledger-1",
        correlation_id="correlation-ledger-1",
        idempotency_key=identity or event_id,
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
    ledger.commit(
        [
            event(
                "event-proposal-1",
                "ProposalRecorded",
                {"proposal_id": "proposal-1", "evaluated_world_revision": 0},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    committed = ledger.commit(
        [
            event(
                "event-acceptance-1",
                "AcceptanceRecorded",
                {
                    "acceptance_id": "acceptance-1",
                    "status": "rejected",
                    "proposal_id": "proposal-1",
                    "evaluated_world_revision": 0,
                },
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=1,
    )

    assert committed.world_revision == 1
    assert committed.deliberation_revision == 1


def test_current_commits_cannot_forge_migration_only_acceptance_audits() -> None:
    ledger = WorldLedger.in_memory(world_id="world-v2-ledger-test")

    with pytest.raises(ValueError, match="migration-only"):
        ledger.commit(
            [
                event(
                    "event-legacy-acceptance",
                    "LegacyAcceptanceAuditRecorded",
                    {"status": "rejected", "acceptance_id": "legacy:acceptance"},
                )
            ],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )

    assert ledger.project().world_revision == 0


def test_multi_event_unit_of_work_retry_joins_the_original_result() -> None:
    ledger = WorldLedger.in_memory(world_id="world-v2-ledger-test")
    events = [
        event("event-observation-1", "ObservationRecorded", {"observation_id": "obs-1"}),
        event("event-observation-2", "ObservationRecorded", {"observation_id": "obs-2"}),
    ]

    first = ledger.commit(
        events,
        commit_id="commit-trigger-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    retried = ledger.commit(
        events,
        commit_id="commit-trigger-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    assert retried == first
    assert first.event_ids == ("event-observation-1", "event-observation-2")
    assert ledger.project().world_revision == 2


def test_commit_rejects_reused_commit_or_in_batch_identity_atomically() -> None:
    ledger = WorldLedger.in_memory(world_id="world-v2-ledger-test")
    original = event("event-observation-1", "ObservationRecorded", {"observation_id": "obs-1"})
    ledger.commit(
        [original],
        commit_id="commit-trigger-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    with pytest.raises(IdempotencyConflict, match="commit_id"):
        ledger.commit(
            [event("event-observation-2", "ObservationRecorded", {"observation_id": "obs-2"})],
            commit_id="commit-trigger-1",
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )

    duplicate_key = original.model_copy(update={"event_id": "event-other"})
    with pytest.raises(IdempotencyConflict):
        ledger.commit(
            [
                event("event-observation-2", "ObservationRecorded", {"observation_id": "obs-2"}),
                duplicate_key,
            ],
            commit_id="commit-trigger-2",
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )

    assert ledger.project().world_revision == 1


def test_late_observation_does_not_move_logical_time_backwards() -> None:
    ledger = WorldLedger.in_memory(world_id="world-v2-ledger-test")
    later = event(
        "event-clock",
        "ClockAdvanced",
        {"logical_time_from": NOW.isoformat(), "logical_time_to": NOW.replace(hour=13).isoformat()},
    )
    ledger.commit([later], expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        [event(
            "event-late",
            "ObservationRecorded",
            {"observation_id": "obs-late"},
            logical_time=NOW.replace(hour=13),
        )],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )

    assert ledger.project().logical_time == NOW.replace(hour=13)


def test_clock_from_must_match_the_current_logical_time() -> None:
    ledger = WorldLedger.in_memory(world_id="world-v2-ledger-test")
    ledger.commit(
        [event("event-observation", "ObservationRecorded", {"observation_id": "obs-1"})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    mismatched = event(
        "event-clock",
        "ClockAdvanced",
        {
            "logical_time_from": NOW.replace(hour=11).isoformat(),
            "logical_time_to": NOW.replace(hour=13).isoformat(),
        },
    )

    with pytest.raises(ValueError, match="logical_time_from"):
        ledger.commit([mismatched], expected_world_revision=1, expected_deliberation_revision=0)
    assert ledger.project().world_revision == 1
