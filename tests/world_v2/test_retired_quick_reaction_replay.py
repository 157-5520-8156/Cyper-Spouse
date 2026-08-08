"""Cold-replay compatibility for the retired independent quick-reaction author.

New production has no quick-reaction model, worker, config switch or runtime
hook.  Historical generic V2 events remain valid immutable ledger input.
"""

from datetime import UTC, datetime
import hashlib
import json

from companion_daemon.world_v2.random_authority import RandomDrawRecordedPayload
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_historical_quick_reaction_draw_still_cold_replays_without_live_author() -> None:
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    world_id = "world:historical-quick-reaction"

    def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=world_id,
            event_type=event_type,
            logical_time=now,
            created_at=now,
            actor="system:historical-replay",
            source="world-v2:quick-reaction",
            trace_id="trace:historical-quick-reaction",
            causation_id="event:historical:observation",
            correlation_id="correlation:historical-quick-reaction",
            idempotency_key=f"idempotency:{event_id}",
            payload=payload,
        )

    state = reduce_event(
        ReducerState(),
        event("event:historical:start", "WorldStarted", {}),
    )
    candidates = ("act", "hold")
    draw = RandomDrawRecordedPayload(
        draw_id="draw:historical-quick-reaction",
        attempt_id="quick-reaction:historical-observation",
        candidate_refs=candidates,
        candidate_set_hash=_digest(candidates),
        selected_candidate_ref="hold",
        seed_hash="8" * 64,
        catalog_version="quick-reaction-act-hold.1",
        sampler_version="random-authority.1",
    )

    replayed = reduce_event(
        state,
        event(
            "event:historical:quick-reaction-draw",
            "RandomDrawRecorded",
            draw.model_dump(mode="json"),
        ),
    )

    assert replayed.logical_time == now
