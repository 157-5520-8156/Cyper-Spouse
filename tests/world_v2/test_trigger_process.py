from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2 import ClaimLease, TriggerProcess
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
WORLD_ID = "world-v2-trigger-test"


def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:runtime",
        source="runtime",
        trace_id="trace-trigger",
        causation_id="external-result-1",
        correlation_id="conversation-1",
        idempotency_key=event_id,
        payload=payload,
    )


def process(*, attempt: int, acquired_at: datetime) -> TriggerProcess:
    attempt_id = f"attempt:trigger-1:{attempt}"
    return TriggerProcess(
        trigger_id="trigger-1",
        trigger_ref="result-1",
        process_kind="settlement",
        state="claimed",
        claim_lease=ClaimLease(
            owner_id=f"owner:{attempt_id}",
            attempt_id=attempt_id,
            acquired_at=acquired_at,
            expires_at=acquired_at + timedelta(minutes=2),
        ),
        attempt_ids=tuple(f"attempt:trigger-1:{index}" for index in range(1, attempt + 1)),
    )


def test_only_the_active_lease_owner_can_complete_a_trigger() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    claimed = process(attempt=1, acquired_at=NOW)
    ledger.commit(
        [
            event(
                "event-claim", "TriggerProcessClaimed", {"process": claimed.model_dump(mode="json")}
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    with pytest.raises(ValueError, match="does not own"):
        ledger.commit(
            [
                event(
                    "event-complete-wrong-owner",
                    "TriggerProcessCompleted",
                    {
                        "trigger_id": "trigger-1",
                        "owner_id": "owner:someone-else",
                        "attempt_id": claimed.claim_lease.attempt_id,
                        "completed_at": NOW.isoformat(),
                        "runtime_outcome_ref": "outcome:trigger-1",
                    },
                )
            ],
            expected_world_revision=0,
            expected_deliberation_revision=1,
        )
    assert ledger.project().trigger_processes[0].state == "claimed"


def test_expired_trigger_can_be_reclaimed_with_preserved_attempt_lineage() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    first = process(attempt=1, acquired_at=NOW)
    second = process(attempt=2, acquired_at=NOW + timedelta(minutes=2))
    ledger.commit(
        [event("event-claim", "TriggerProcessClaimed", {"process": first.model_dump(mode="json")})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [
            event(
                "event-reclaim",
                "TriggerProcessReclaimed",
                {"process": second.model_dump(mode="json")},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=1,
    )
    ledger.commit(
        [
            event(
                "event-complete",
                "TriggerProcessCompleted",
                {
                    "trigger_id": "trigger-1",
                    "owner_id": second.claim_lease.owner_id,
                    "attempt_id": second.claim_lease.attempt_id,
                    "completed_at": second.claim_lease.acquired_at.isoformat(),
                    "runtime_outcome_ref": "outcome:trigger-1",
                },
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=2,
    )

    completed = ledger.project().trigger_processes[0]
    assert completed.state == "terminal"
    assert completed.attempt_ids == (
        "attempt:trigger-1:1",
        "attempt:trigger-1:2",
    )
