from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion_daemon.world_v2.dashboard_projection_adapter import (
    DashboardProjectionAdapter,
    DashboardRoomRouteCatalog,
)
from companion_daemon.world_v2.schemas import (
    ProjectionRequest,
    PublicSituationProjection,
    RoomProjectionView,
    WorldProjection,
)


NOW = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
WORLD_ID = "world:dashboard-adapter"


class _Source:
    def __init__(self, projection: WorldProjection) -> None:
        self.projection = projection
        self.requests: list[ProjectionRequest] = []

    def project(self, viewer: ProjectionRequest) -> WorldProjection:
        self.requests.append(viewer)
        return self.projection


def _request(**changes: object) -> ProjectionRequest:
    return ProjectionRequest(
        schema_version="world-v2.1",
        request_id="request:dashboard-room",
        world_id=WORLD_ID,
        viewer_kind="room_renderer",
        viewer_id="room:dashboard",
        permissions=frozenset(),
        trace_id="trace:dashboard-room",
        redaction_policy="room-public-v1",
    ).model_copy(update=changes)


def _projection(*, view: RoomProjectionView) -> WorldProjection:
    return WorldProjection(
        world_id=WORLD_ID,
        world_revision=7,
        ledger_sequence=13,
        viewer_kind="room_renderer",
        redaction_policy="room-public-v1",
        reducer_bundle_version="world-v2-reducers.28",
        projection_hash="b" * 64,
        logical_time=NOW,
        view=view,
    )


def _adapter(source: _Source) -> DashboardProjectionAdapter:
    return DashboardProjectionAdapter(
        source=source,
        routes=DashboardRoomRouteCatalog(
            location_routes={"location:studio": "zhizhi-home"},
            activity_routes={"focused_work": "study"},
        ),
    )


def test_dashboard_adapter_maps_public_room_view_to_fixed_transport_neutral_dto() -> None:
    source = _Source(
        _projection(
            view=RoomProjectionView(
                situation=PublicSituationProjection(
                    location_ref="location:studio",
                    activity="focused_work",
                    activity_phase="active",
                    attention="occupied",
                    visible_status="busy",
                )
            )
        )
    )

    dto = _adapter(source).capture(_request())

    assert dto.to_payload() == {
        "schema_version": "world-v2-dashboard-room.1",
        "cursor": {"world_revision": 7, "ledger_sequence": 13},
        "projection_hash": "b" * 64,
        "route": {
            "scene_id": "zhizhi-home",
            "action_id": "study",
            "availability": "busy",
        },
    }
    assert source.requests == [_request()]


def test_dashboard_adapter_redacts_unknown_and_private_room_values_instead_of_forwarding_refs() -> None:
    secret = "location:private-bedroom:user:geoff"
    source = _Source(
        _projection(
            view=RoomProjectionView(
                situation=PublicSituationProjection(
                    location_ref=secret,
                    activity="private_recovery",
                    visible_status=None,
                )
            )
        )
    )

    payload = _adapter(source).capture(_request()).to_payload()

    assert payload["route"] == {
        "scene_id": "unavailable",
        "action_id": "idle",
        "availability": "unavailable",
    }
    assert secret not in str(payload)
    assert "private_recovery" not in str(payload)


def test_dashboard_adapter_fails_closed_when_a_source_returns_an_unknown_public_status() -> None:
    source = _Source(
        _projection(
            view=RoomProjectionView(
                situation=PublicSituationProjection(
                    location_ref="location:studio",
                    activity="focused_work",
                    visible_status="unexpected_private_hint",
                )
            )
        )
    )

    payload = _adapter(source).capture(_request()).to_payload()

    assert payload["route"] == {
        "scene_id": "unavailable",
        "action_id": "idle",
        "availability": "unavailable",
    }
    assert "unexpected_private_hint" not in str(payload)


@pytest.mark.parametrize(
    "projection_request, message",
    [
        (_request(viewer_kind="dashboard_operator"), "room_renderer"),
        (_request(redaction_policy="operator-default-v1"), "room-public-v1"),
        (_request(permissions=frozenset({"projection:diagnostics"})), "elevated"),
    ],
)
def test_dashboard_adapter_rejects_non_room_or_elevated_requests(
    projection_request: ProjectionRequest, message: str
) -> None:
    source = _Source(_projection(view=RoomProjectionView()))

    with pytest.raises(PermissionError, match=message):
        _adapter(source).capture(projection_request)

    assert source.requests == []


def test_dashboard_adapter_rejects_source_that_substitutes_another_viewer_kind() -> None:
    source = _Source(
        _projection(view=RoomProjectionView()).model_copy(update={"viewer_kind": "evaluator"})
    )

    with pytest.raises(PermissionError, match="non-room projection"):
        _adapter(source).capture(_request())


def test_dashboard_adapter_is_a_read_only_projection_consumer() -> None:
    path = (
        Path(__file__).parents[2]
        / "src/companion_daemon/world_v2/dashboard_projection_adapter.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert imports == {"__future__", "dataclasses", "schemas", "typing"}
    source = path.read_text(encoding="utf-8")
    forbidden = (
        "companion_daemon.engine",
        "companion_daemon.dashboard_ui",
        "companion_daemon.world",
        "WorldRuntime",
        "WorldLedger",
        "SQLiteWorldLedger",
        "life_reducers",
        "_ledger",
    )
    assert not any(token in source for token in forbidden)
