"""Replay compatibility for the retired independent afterthought lane."""

from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import ClaimLease, TriggerProcess, WorldEvent


def test_historical_afterthought_trigger_events_still_replay_to_terminal() -> None:
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    lease = ClaimLease(
        owner_id="worker:retired-afterthought",
        attempt_id="attempt:historical-afterthought",
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    opened = TriggerProcess(
        trigger_id="trigger:historical-afterthought",
        trigger_ref="afterthought:historical-receipt",
        process_kind="afterthought_author",
        source_evidence_ref="receipt:historical",
        state="open",
    )
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": lease,
            "attempt_ids": (lease.attempt_id,),
        }
    )

    def historical_event(
        event_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id="world:historical-afterthought",
            event_type=event_type,
            logical_time=now,
            created_at=now,
            actor="system:historical-replay",
            source="world-v2:afterthought-author",
            trace_id="trace:historical-afterthought",
            causation_id="receipt:historical",
            correlation_id="correlation:historical-afterthought",
            idempotency_key=f"idempotency:{event_id}",
            payload=payload,
        )

    state = ReducerState()
    state = reduce_event(
        state,
        historical_event(
            "event:historical-afterthought:opened",
            "TriggerProcessOpened",
            {"process": opened.model_dump(mode="json")},
        ),
    )
    state = reduce_event(
        state,
        historical_event(
            "event:historical-afterthought:claimed",
            "TriggerProcessClaimed",
            {"process": claimed.model_dump(mode="json")},
        ),
    )
    state = reduce_event(
        state,
        historical_event(
            "event:historical-afterthought:completed",
            "TriggerProcessCompleted",
            {
                "trigger_id": claimed.trigger_id,
                "owner_id": lease.owner_id,
                "attempt_id": lease.attempt_id,
                "completed_at": now.isoformat(),
                "runtime_outcome_ref": "afterthought:historical:silent",
            },
        ),
    )

    assert state.trigger_processes == (
        claimed.model_copy(
            update={
                "state": "terminal",
                "runtime_outcome_ref": "afterthought:historical:silent",
            }
        ),
    )
