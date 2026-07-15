"""Read-only dashboard DTOs compiled exclusively from a Room projection.

The dashboard is a viewer, not a second state owner.  In particular this
adapter deliberately cannot see a ledger, an internal snapshot, an operator
projection, or the legacy dashboard state.  It accepts an already-authorized
``room_renderer`` request and maps the small public Room view onto stable UI
route identifiers.  Public authority refs are never forwarded to a browser as
an accidental rendering contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Protocol

from .schemas import ProjectionRequest, RoomProjectionView, WorldProjection


_ROUTE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ROOM_POLICY = "room-public-v1"
_PUBLIC_AVAILABILITY = frozenset(
    {"available", "busy", "do_not_disturb", "recovering", "active", "paused", "scheduled"}
)


class RoomProjectionSource(Protocol):
    """The sole query capability required by the dashboard adapter."""

    def project(self, viewer: ProjectionRequest) -> WorldProjection: ...


@dataclass(frozen=True, slots=True)
class DashboardSceneRoute:
    """A renderer-safe route, intentionally not a serialized world fact."""

    scene_id: str
    action_id: str
    availability: str

    def __post_init__(self) -> None:
        for name, value in (
            ("scene_id", self.scene_id),
            ("action_id", self.action_id),
            ("availability", self.availability),
        ):
            if not _ROUTE_IDENTIFIER.fullmatch(value):
                raise ValueError(f"dashboard {name} must be a safe route identifier")

    def to_payload(self) -> dict[str, str]:
        return {
            "scene_id": self.scene_id,
            "action_id": self.action_id,
            "availability": self.availability,
        }


@dataclass(frozen=True, slots=True)
class DashboardRoomProjectionDTO:
    """Minimal cacheable room input for dashboard/Godot-style viewers.

    The cursor and projection hash let a transport cache and compare snapshots
    without seeing the internal semantic hash.  There is deliberately no
    affect, participant, media-preview, private location, or raw activity ref
    in this type.
    """

    schema_version: str
    world_revision: int
    ledger_sequence: int
    projection_hash: str
    route: DashboardSceneRoute

    def __post_init__(self) -> None:
        if self.schema_version != "world-v2-dashboard-room.1":
            raise ValueError("unsupported dashboard room DTO schema version")
        if self.world_revision < 0 or self.ledger_sequence < 0:
            raise ValueError("dashboard cursor values must not be negative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.projection_hash):
            raise ValueError("dashboard projection hash must be a sha256 hex digest")

    def to_payload(self) -> dict[str, object]:
        """Return the exact HTTP/WebSocket-independent viewer wire shape."""

        return {
            "schema_version": self.schema_version,
            "cursor": {
                "world_revision": self.world_revision,
                "ledger_sequence": self.ledger_sequence,
            },
            "projection_hash": self.projection_hash,
            "route": self.route.to_payload(),
        }


class DashboardRoomRouteCatalog:
    """Maps explicit public world labels to predeclared renderer routes.

    Missing routes are normal during migration.  They become the fixed
    ``unavailable``/``idle`` route instead of leaking an authority ref or
    inventing a scene.  The catalog is composition data, not a world writer.
    """

    def __init__(
        self,
        *,
        location_routes: Mapping[str, str] = {},
        activity_routes: Mapping[str, str] = {},
        unavailable_scene_id: str = "unavailable",
        idle_action_id: str = "idle",
    ) -> None:
        self._validate_mapping("location_routes", location_routes)
        self._validate_mapping("activity_routes", activity_routes)
        self._location_routes = dict(location_routes)
        self._activity_routes = dict(activity_routes)
        self._unavailable_scene_id = unavailable_scene_id
        self._idle_action_id = idle_action_id
        DashboardSceneRoute(
            scene_id=unavailable_scene_id,
            action_id=idle_action_id,
            availability="unavailable",
        )

    def route(self, view: RoomProjectionView) -> DashboardSceneRoute:
        situation = view.situation
        scene_id = (
            self._location_routes.get(situation.location_ref)
            if situation.location_ref is not None
            else None
        )
        action_id = (
            self._activity_routes.get(situation.activity)
            if situation.activity is not None
            else None
        )
        if scene_id is None:
            return DashboardSceneRoute(
                scene_id=self._unavailable_scene_id,
                action_id=self._idle_action_id,
                availability="unavailable",
            )
        if situation.visible_status not in _PUBLIC_AVAILABILITY:
            return DashboardSceneRoute(
                scene_id=self._unavailable_scene_id,
                action_id=self._idle_action_id,
                availability="unavailable",
            )
        return DashboardSceneRoute(
            scene_id=scene_id,
            action_id=action_id or self._idle_action_id,
            availability=situation.visible_status,
        )

    @staticmethod
    def _validate_mapping(name: str, routes: Mapping[str, str]) -> None:
        for authority_ref, route in routes.items():
            if not authority_ref:
                raise ValueError(f"dashboard {name} must not contain an empty authority ref")
            if not _ROUTE_IDENTIFIER.fullmatch(route):
                raise ValueError(f"dashboard {name} route must be a safe route identifier")


class DashboardProjectionAdapter:
    """Compile an authorized public room projection into a dashboard DTO."""

    def __init__(
        self,
        *,
        source: RoomProjectionSource,
        routes: DashboardRoomRouteCatalog,
    ) -> None:
        self._source = source
        self._routes = routes

    def capture(self, request: ProjectionRequest) -> DashboardRoomProjectionDTO:
        """Read one public room snapshot without any transport dependency."""

        self._validate_request(request)
        projection = self._source.project(request)
        if projection.world_id != request.world_id:
            raise PermissionError("dashboard projection belongs to another world")
        if projection.viewer_kind != "room_renderer" or projection.redaction_policy != _ROOM_POLICY:
            raise PermissionError("dashboard source returned a non-room projection")
        if not isinstance(projection.view, RoomProjectionView):
            raise PermissionError("dashboard source returned an unexpected projection view")
        return DashboardRoomProjectionDTO(
            schema_version="world-v2-dashboard-room.1",
            world_revision=projection.world_revision,
            ledger_sequence=projection.ledger_sequence,
            projection_hash=projection.projection_hash,
            route=self._routes.route(projection.view),
        )

    @staticmethod
    def _validate_request(request: ProjectionRequest) -> None:
        if request.viewer_kind != "room_renderer":
            raise PermissionError("dashboard adapter only accepts room_renderer requests")
        if request.redaction_policy != _ROOM_POLICY:
            raise PermissionError("dashboard adapter requires room-public-v1 redaction")
        if request.permissions or request.include_debug_refs:
            raise PermissionError("dashboard adapter does not accept elevated projection permissions")


__all__ = [
    "DashboardProjectionAdapter",
    "DashboardRoomProjectionDTO",
    "DashboardRoomRouteCatalog",
    "DashboardSceneRoute",
    "RoomProjectionSource",
]
