"""Cold replay for immutable events from the retired LifeAuthor runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

from companion_daemon.world_v2.legacy_life_author_events import (
    LifeAuthorDecisionRecordedPayload,
    LifeAvailabilitySnapshotRecordedPayload,
)
from companion_daemon.world_v2.random_authority import RandomDrawRecordedPayload
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_historical_life_author_events_cold_replay_without_live_author() -> None:
    started_at = datetime(2026, 7, 1, 11, 50, tzinfo=UTC)
    now = started_at + timedelta(minutes=10)
    world_id = "world:historical-life-author"

    def event(
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        *,
        at: datetime,
        causation_id: str,
    ) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=world_id,
            event_type=event_type,
            logical_time=at,
            created_at=at,
            actor="system:historical-replay",
            source="world-v2:life-author",
            trace_id="trace:historical-life-author",
            causation_id=causation_id,
            correlation_id="correlation:historical-life-author",
            idempotency_key=f"idempotency:{event_id}",
            payload=payload,
        )

    state = reduce_event(
        ReducerState(),
        event(
            "event:historical:start",
            "WorldStarted",
            {},
            at=started_at,
            causation_id="cause:historical:start",
        ),
    )
    wake = event(
        "event:historical:clock",
        "ClockAdvanced",
        {
            "logical_time_from": started_at.isoformat(),
            "logical_time_to": now.isoformat(),
        },
        at=now,
        causation_id="event:historical:start",
    )
    state = reduce_event(state, wake)

    candidate_token = "a" * 64
    draw_payload = RandomDrawRecordedPayload(
        draw_id="draw:historical-life-author",
        attempt_id="attempt:historical-life-author",
        candidate_refs=(candidate_token,),
        candidate_set_hash=_digest((candidate_token,)),
        selected_candidate_ref=candidate_token,
        seed_hash="b" * 64,
        catalog_version="life-seed.legacy",
        sampler_version="random-authority.1",
    )
    draw = event(
        "event:historical:draw",
        "RandomDrawRecorded",
        draw_payload.model_dump(mode="json"),
        at=now,
        causation_id=wake.event_id,
    )
    state = reduce_event(state, draw)

    decision_payload = LifeAuthorDecisionRecordedPayload(
        decision_id="decision:historical-life-author",
        attempt_id="attempt:historical-life-author",
        wake_event_ref=wake.event_id,
        wake_event_payload_hash=wake.payload_hash,
        wake_world_revision=2,
        draw_event_ref=draw.event_id,
        draw_event_payload_hash=draw.payload_hash,
        draw_world_revision=3,
        candidate_token=candidate_token,
        catalog_version="life-seed.legacy",
        catalog_hash="c" * 64,
        decision="no_op",
        model="historical-life-author-model",
        raw_output_hash="d" * 64,
    )
    state = reduce_event(
        state,
        event(
            "event:historical:decision",
            "LifeAuthorDecisionRecorded",
            decision_payload.model_dump(mode="json"),
            at=now,
            causation_id=draw.event_id,
        ),
    )

    availability_payload = LifeAvailabilitySnapshotRecordedPayload(
        snapshot_id="snapshot:historical-life-author",
        wake_event_ref=wake.event_id,
        wake_event_payload_hash=wake.payload_hash,
        wake_world_revision=2,
        candidate_token=candidate_token,
        catalog_version="life-seed.legacy",
        catalog_hash="c" * 64,
        owner_actor_ref="actor:companion",
        availability_scope="current_presence",
        participant_refs=(),
        availability_hash="e" * 64,
    )
    replayed = reduce_event(
        state,
        event(
            "event:historical:availability",
            "LifeAvailabilitySnapshotRecorded",
            availability_payload.model_dump(mode="json"),
            at=now,
            causation_id=wake.event_id,
        ),
    )

    assert replayed.logical_time == now
    assert tuple(item.event_type for item in replayed.committed_world_event_refs) == (
        "WorldStarted",
        "ClockAdvanced",
        "RandomDrawRecorded",
        "LifeAvailabilitySnapshotRecorded",
    )
