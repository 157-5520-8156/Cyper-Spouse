from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.dashboard_projection_adapter import (
    DashboardPublicProjectionAdapter,
    DashboardPublicRouteCatalog,
    DashboardRoomRouteCatalog,
)
from companion_daemon.world_v2.schemas import (
    DashboardPublicProjectionView,
    ProjectionRequest,
    PublicAgendaProjection,
    PublicSituationProjection,
    WorldProjection,
)


NOW = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
WORLD_ID = "world:dashboard-public"


class _Source:
    def __init__(self, projection: WorldProjection) -> None:
        self.projection = projection
        self.requests: list[ProjectionRequest] = []

    def project(self, request: ProjectionRequest) -> WorldProjection:
        self.requests.append(request)
        return self.projection


def _request(**changes: object) -> ProjectionRequest:
    return ProjectionRequest(
        schema_version="world-v2.1",
        request_id="request:dashboard-public",
        world_id=WORLD_ID,
        viewer_kind="dashboard_public",
        viewer_id="dashboard:public",
        permissions=frozenset(),
        trace_id="trace:dashboard-public",
        redaction_policy="dashboard-public-v1",
    ).model_copy(update=changes)


def _projection(view: DashboardPublicProjectionView) -> WorldProjection:
    return WorldProjection(
        world_id=WORLD_ID,
        world_revision=7,
        ledger_sequence=13,
        viewer_kind="dashboard_public",
        redaction_policy="dashboard-public-v1",
        reducer_bundle_version="world-v2-reducers.28",
        projection_hash="b" * 64,
        logical_time=NOW,
        view=view,
    )


def _adapter(source: _Source) -> DashboardPublicProjectionAdapter:
    return DashboardPublicProjectionAdapter(
        source=source,
        routes=DashboardPublicRouteCatalog(
            room_routes=DashboardRoomRouteCatalog(
                location_routes={"location:studio": "zhizhi-home"},
                activity_routes={"focused_work": "study", "relax": "relax"},
            ),
            activity_labels={"focused_work": "在看资料", "relax": "放松一下"},
        ),
    )


def test_public_adapter_compiles_one_cursor_whitelisted_dashboard_dto() -> None:
    source = _Source(
        _projection(
            DashboardPublicProjectionView(
                situation=PublicSituationProjection(
                    location_ref="location:studio",
                    activity="focused_work",
                    visible_status="busy",
                ),
                agenda=(
                    PublicAgendaProjection(
                        activity="relax", status="scheduled", starts_at=NOW
                    ),
                ),
            )
        )
    )

    dto = _adapter(source).capture(_request())
    payload = dto.to_payload()

    assert payload["schema_version"] == "world-v2-dashboard.1"
    assert payload["cursor"] == {"world_revision": 7, "ledger_sequence": 13}
    assert payload["room"] == {
        "scene_id": "zhizhi-home",
        "action_id": "study",
        "availability": "busy",
    }
    assert payload["now"] == {
        "activity_id": "focused_work",
        "activity_label": "在看资料",
        "availability": "busy",
    }
    assert payload["agenda"] == [
        {
            "slot_id": payload["agenda"][0]["slot_id"],
            "starts_at": NOW.isoformat(),
            "status": "scheduled",
            "activity_id": "relax",
            "activity_label": "放松一下",
        }
    ]
    assert payload["agenda"][0]["slot_id"].startswith("agenda-")
    assert payload["notices"] == []
    assert payload["freshness"] == {"observed_at": NOW.isoformat(), "stale_after_seconds": 30}
    assert len(payload["projection_hash"]) == 64
    assert source.requests == [_request()]


def test_public_adapter_omits_unknown_agenda_and_never_forwards_private_route_values() -> None:
    secret = "location:private-bedroom:user:geoff"
    source = _Source(
        _projection(
            DashboardPublicProjectionView(
                situation=PublicSituationProjection(
                    location_ref=secret,
                    activity="private_recovery",
                    visible_status="busy",
                ),
                agenda=(
                    PublicAgendaProjection(
                        activity="private_recovery", status="scheduled", starts_at=NOW
                    ),
                ),
            )
        )
    )

    payload = _adapter(source).capture(_request()).to_payload()

    assert payload["room"]["availability"] == "unavailable"
    assert payload["now"] == {
        "activity_id": "unavailable",
        "activity_label": "暂不可用",
        "availability": "unavailable",
    }
    assert payload["agenda"] == []
    assert secret not in str(payload)
    assert "private_recovery" not in str(payload)


@pytest.mark.parametrize(
    ("projection_request", "message"),
    [
        (_request(viewer_kind="room_renderer"), "dashboard_public"),
        (_request(redaction_policy="room-public-v1"), "dashboard-public-v1"),
        (_request(permissions=frozenset({"projection:diagnostics"})), "elevated"),
    ],
)
def test_public_adapter_rejects_nonfixed_or_elevated_capabilities(
    projection_request: ProjectionRequest, message: str
) -> None:
    source = _Source(_projection(DashboardPublicProjectionView()))

    with pytest.raises(PermissionError, match=message):
        _adapter(source).capture(projection_request)

    assert source.requests == []


def test_public_adapter_rejects_untyped_notices_and_source_view_substitution() -> None:
    source = _Source(
        _projection(DashboardPublicProjectionView(notice_kinds=("pending_commitment",)))
    )
    with pytest.raises(PermissionError, match="unsupported public notice"):
        _adapter(source).capture(_request())

    source.projection = source.projection.model_copy(update={"viewer_kind": "room_renderer"})
    with pytest.raises(PermissionError, match="unexpected public projection"):
        _adapter(source).capture(_request())
