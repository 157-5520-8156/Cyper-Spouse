from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.event_media_planner_adapter import (
    SQLiteEventMediaPlanningResultStore,
)
from companion_daemon.world_v2.media_v2 import (
    MediaNotRenderable,
    MediaPlan,
    MediaPlanningResult,
    StoredMediaPayload,
    canonical_media_json,
    media_payload_hash,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
REQUEST_ID = "media-plan-request:durable-store"


def _plan_result() -> MediaPlanningResult:
    body = canonical_media_json({"version": "event-media-plan-v5", "plan_id": "media-plan:durable"})
    payload = StoredMediaPayload(
        payload_ref="sidecar:media-plan:durable",
        payload_hash=media_payload_hash(body),
        content_type="application/vnd.world-v2.media-plan+json",
        body=body,
    )
    return MediaPlanningResult(
        plan=MediaPlan(
            plan_id="media-plan:durable",
            planning_request_id=REQUEST_ID,
            opportunity_id="opportunity:durable",
            event_snapshot_hash="sha256:" + "a" * 64,
            family="life_share",
            planner_version="event-media-planner-adapter.p0",
            schema_version="event-media-plan-v5",
            plan_payload_ref=payload.payload_ref,
            plan_payload_hash=payload.payload_hash,
            frozen_at=NOW,
        ),
        plan_payload=payload,
    )


def _not_renderable_result(
    *, request_id: str = REQUEST_ID, reason_code: str = "not_renderable"
) -> MediaPlanningResult:
    return MediaPlanningResult(
        not_renderable=MediaNotRenderable(
            opportunity_id="opportunity:durable",
            planning_request_id=request_id,
            event_snapshot_hash="sha256:" + "a" * 64,
            reason_code=reason_code,
            planner_version="event-media-planner-adapter.p0",
        )
    )


@pytest.mark.asyncio
async def test_sqlite_event_media_planning_result_store_round_trips_terminal_results_after_reopen(
    tmp_path,
) -> None:
    path = str(tmp_path / "event-media-results.sqlite")
    planned = _plan_result()
    not_renderable_request = "media-plan-request:not-renderable"
    not_renderable = _not_renderable_result(request_id=not_renderable_request)
    writer = SQLiteEventMediaPlanningResultStore(path=path, world_id="world:one")
    await writer.put_if_absent(planning_request_id=REQUEST_ID, result=planned)
    await writer.put_if_absent(planning_request_id=not_renderable_request, result=not_renderable)
    writer.close()

    reader = SQLiteEventMediaPlanningResultStore(path=path, world_id="world:one")
    try:
        assert await reader.lookup(planning_request_id=REQUEST_ID) == planned
        assert await reader.lookup(planning_request_id=not_renderable_request) == not_renderable
    finally:
        reader.close()


@pytest.mark.asyncio
async def test_sqlite_event_media_planning_result_store_is_world_scoped_and_immutable(tmp_path) -> None:
    path = str(tmp_path / "event-media-results.sqlite")
    first = SQLiteEventMediaPlanningResultStore(path=path, world_id="world:one")
    second = SQLiteEventMediaPlanningResultStore(path=path, world_id="world:two")
    planned = _plan_result()
    try:
        await first.put_if_absent(planning_request_id=REQUEST_ID, result=planned)
        await first.put_if_absent(planning_request_id=REQUEST_ID, result=planned)
        assert await second.lookup(planning_request_id=REQUEST_ID) is None

        with pytest.raises(ValueError, match="different immutable terminal result"):
            await first.put_if_absent(
                planning_request_id=REQUEST_ID,
                result=_not_renderable_result(reason_code="other_terminal_result"),
            )
    finally:
        first.close()
        second.close()
