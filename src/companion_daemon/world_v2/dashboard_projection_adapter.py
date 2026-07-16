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
import datetime as dt
import hashlib
import json
import re
from typing import Mapping, Protocol

from .schemas import (
    DashboardPublicProjectionView,
    ProjectionRequest,
    RoomProjectionView,
    WorldProjection,
)


_ROUTE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ROOM_POLICY = "room-public-v1"
_DASHBOARD_PUBLIC_POLICY = "dashboard-public-v1"
_EMPTY_WORLD_OBSERVED_AT = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
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


@dataclass(frozen=True, slots=True)
class DashboardNow:
    """One catalog-backed public present-tense display fact."""

    activity_id: str
    activity_label: str
    availability: str

    def __post_init__(self) -> None:
        if not _ROUTE_IDENTIFIER.fullmatch(self.activity_id):
            raise ValueError("dashboard activity_id must be a safe route identifier")
        if not self.activity_label or len(self.activity_label) > 80:
            raise ValueError("dashboard activity_label must be a short public display label")
        if not _ROUTE_IDENTIFIER.fullmatch(self.availability):
            raise ValueError("dashboard availability must be a safe route identifier")

    def to_payload(self) -> dict[str, str]:
        return {
            "activity_id": self.activity_id,
            "activity_label": self.activity_label,
            "availability": self.availability,
        }


@dataclass(frozen=True, slots=True)
class DashboardAgendaItem:
    """A display catalog projection, never a plan or calendar record."""

    slot_id: str
    starts_at: dt.datetime
    status: str
    activity_id: str
    activity_label: str

    def __post_init__(self) -> None:
        if not _ROUTE_IDENTIFIER.fullmatch(self.slot_id):
            raise ValueError("dashboard agenda slot_id must be a safe route identifier")
        if not _ROUTE_IDENTIFIER.fullmatch(self.status):
            raise ValueError("dashboard agenda status must be a safe route identifier")
        if not _ROUTE_IDENTIFIER.fullmatch(self.activity_id):
            raise ValueError("dashboard agenda activity_id must be a safe route identifier")
        if not self.activity_label or len(self.activity_label) > 80:
            raise ValueError("dashboard agenda label must be a short public display label")
        if self.starts_at.tzinfo is None or self.starts_at.utcoffset() is None:
            raise ValueError("dashboard agenda starts_at must be timezone-aware")

    def to_payload(self) -> dict[str, str]:
        return {
            "slot_id": self.slot_id,
            "starts_at": self.starts_at.isoformat(),
            "status": self.status,
            "activity_id": self.activity_id,
            "activity_label": self.activity_label,
        }


@dataclass(frozen=True, slots=True)
class DashboardNotice:
    """An intentionally content-free public notice."""

    notice_id: str
    kind: str
    label: str

    def __post_init__(self) -> None:
        if not _ROUTE_IDENTIFIER.fullmatch(self.notice_id):
            raise ValueError("dashboard notice_id must be a safe route identifier")
        if not _ROUTE_IDENTIFIER.fullmatch(self.kind):
            raise ValueError("dashboard notice kind must be a safe route identifier")
        if not self.label or len(self.label) > 80:
            raise ValueError("dashboard notice label must be a short public display label")

    def to_payload(self) -> dict[str, str]:
        return {"notice_id": self.notice_id, "kind": self.kind, "label": self.label}


@dataclass(frozen=True, slots=True)
class DashboardFreshness:
    observed_at: dt.datetime
    stale_after_seconds: int = 30

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("dashboard freshness observed_at must be timezone-aware")
        if not 1 <= self.stale_after_seconds <= 300:
            raise ValueError("dashboard stale_after_seconds must be between 1 and 300")

    def to_payload(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "stale_after_seconds": self.stale_after_seconds,
        }


@dataclass(frozen=True, slots=True)
class DashboardPublicProjectionDTO:
    """The browser wire contract for the default public Dashboard read path.

    ``projection_hash`` hashes this DTO's canonical, already-redacted payload;
    it is intentionally not the inner WorldProjection semantic/projection hash.
    """

    schema_version: str
    world_revision: int
    ledger_sequence: int
    projection_hash: str
    room: DashboardSceneRoute
    now: DashboardNow
    agenda: tuple[DashboardAgendaItem, ...]
    notices: tuple[DashboardNotice, ...]
    freshness: DashboardFreshness

    def __post_init__(self) -> None:
        if self.schema_version != "world-v2-dashboard.1":
            raise ValueError("unsupported dashboard public DTO schema version")
        if self.world_revision < 0 or self.ledger_sequence < 0:
            raise ValueError("dashboard cursor values must not be negative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.projection_hash):
            raise ValueError("dashboard projection hash must be a sha256 hex digest")

    def _payload_without_hash(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "cursor": {
                "world_revision": self.world_revision,
                "ledger_sequence": self.ledger_sequence,
            },
            "room": self.room.to_payload(),
            "now": self.now.to_payload(),
            "agenda": [item.to_payload() for item in self.agenda],
            "notices": [item.to_payload() for item in self.notices],
            "freshness": self.freshness.to_payload(),
        }

    def to_payload(self) -> dict[str, object]:
        return {**self._payload_without_hash(), "projection_hash": self.projection_hash}

    @classmethod
    def create(
        cls,
        *,
        world_revision: int,
        ledger_sequence: int,
        room: DashboardSceneRoute,
        now: DashboardNow,
        agenda: tuple[DashboardAgendaItem, ...],
        notices: tuple[DashboardNotice, ...],
        freshness: DashboardFreshness,
    ) -> "DashboardPublicProjectionDTO":
        payload = {
            "schema_version": "world-v2-dashboard.1",
            "cursor": {"world_revision": world_revision, "ledger_sequence": ledger_sequence},
            "room": room.to_payload(),
            "now": now.to_payload(),
            "agenda": [item.to_payload() for item in agenda],
            "notices": [item.to_payload() for item in notices],
            "freshness": freshness.to_payload(),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(
            schema_version="world-v2-dashboard.1",
            world_revision=world_revision,
            ledger_sequence=ledger_sequence,
            projection_hash=digest,
            room=room,
            now=now,
            agenda=agenda,
            notices=notices,
            freshness=freshness,
        )


class DashboardPublicRouteCatalog:
    """Versioned mapping from finite public activity facts to browser labels."""

    def __init__(
        self,
        *,
        room_routes: DashboardRoomRouteCatalog,
        activity_labels: Mapping[str, str],
        unavailable_label: str = "暂不可用",
    ) -> None:
        if not unavailable_label or len(unavailable_label) > 80:
            raise ValueError("dashboard unavailable label must be a short public display label")
        for activity_id, label in activity_labels.items():
            if not _ROUTE_IDENTIFIER.fullmatch(activity_id):
                raise ValueError("dashboard activity label key must be a safe route identifier")
            if not label or len(label) > 80:
                raise ValueError("dashboard activity label must be a short public display label")
        self._room_routes = room_routes
        self._activity_labels = dict(activity_labels)
        self._unavailable_label = unavailable_label

    def route(self, view: DashboardPublicProjectionView) -> DashboardSceneRoute:
        # Reuse the room catalog only for route compilation, not its DTO or
        # authorization policy.  Both derive from the same situation facts.
        return self._room_routes.route(RoomProjectionView(situation=view.situation))

    def now(self, view: DashboardPublicProjectionView, room: DashboardSceneRoute) -> DashboardNow:
        activity = view.situation.activity
        if (
            room.availability == "unavailable"
            or activity is None
            or activity not in self._activity_labels
        ):
            return DashboardNow(
                activity_id="unavailable", activity_label=self._unavailable_label, availability="unavailable"
            )
        return DashboardNow(
            activity_id=activity,
            activity_label=self._activity_labels[activity],
            availability=room.availability,
        )

    def agenda(self, view: DashboardPublicProjectionView) -> tuple[DashboardAgendaItem, ...]:
        items: list[DashboardAgendaItem] = []
        for item in view.agenda:
            label = self._activity_labels.get(item.activity)
            if label is None:
                continue
            seed = f"{item.activity}\x00{item.status}\x00{item.starts_at.isoformat()}"
            slot_id = "agenda-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
            items.append(
                DashboardAgendaItem(
                    slot_id=slot_id,
                    starts_at=item.starts_at,
                    status=item.status,
                    activity_id=item.activity,
                    activity_label=label,
                )
            )
        return tuple(items)

    def notices(self, view: DashboardPublicProjectionView) -> tuple[DashboardNotice, ...]:
        # No current World v2 public notice authority exists.  Keep this
        # closed until a typed, accepted notice projection is added.
        if view.notice_kinds:
            raise PermissionError("dashboard source exposed unsupported public notice kinds")
        return ()


class DashboardPublicProjectionAdapter:
    """Compile one fixed dashboard_public projection into its public DTO."""

    def __init__(self, *, source: RoomProjectionSource, routes: DashboardPublicRouteCatalog) -> None:
        self._source = source
        self._routes = routes

    def capture(self, request: ProjectionRequest) -> DashboardPublicProjectionDTO:
        self._validate_request(request)
        projection = self._source.project(request)
        if projection.world_id != request.world_id:
            raise PermissionError("dashboard projection belongs to another world")
        if (
            projection.viewer_kind != "dashboard_public"
            or projection.redaction_policy != _DASHBOARD_PUBLIC_POLICY
            or not isinstance(projection.view, DashboardPublicProjectionView)
        ):
            raise PermissionError("dashboard source returned an unexpected public projection")
        room = self._routes.route(projection.view)
        return DashboardPublicProjectionDTO.create(
            world_revision=projection.world_revision,
            ledger_sequence=projection.ledger_sequence,
            room=room,
            now=self._routes.now(projection.view, room),
            agenda=self._routes.agenda(projection.view),
            notices=self._routes.notices(projection.view),
            # A newly composed but not-yet-started world has no logical clock.
            # Do not sample wall time on GET (that would make a read non-
            # deterministic); the epoch makes this explicitly stale until an
            # accepted clock/world event establishes authoritative time.
            freshness=DashboardFreshness(
                observed_at=projection.logical_time or _EMPTY_WORLD_OBSERVED_AT
            ),
        )

    @staticmethod
    def _validate_request(request: ProjectionRequest) -> None:
        if request.viewer_kind != "dashboard_public":
            raise PermissionError("dashboard public adapter only accepts dashboard_public requests")
        if request.redaction_policy != _DASHBOARD_PUBLIC_POLICY:
            raise PermissionError("dashboard public adapter requires dashboard-public-v1 redaction")
        if request.permissions or request.include_debug_refs:
            raise PermissionError("dashboard public adapter does not accept elevated projection permissions")


__all__ = [
    "DashboardProjectionAdapter",
    "DashboardPublicProjectionAdapter",
    "DashboardPublicProjectionDTO",
    "DashboardPublicRouteCatalog",
    "DashboardAgendaItem",
    "DashboardFreshness",
    "DashboardNotice",
    "DashboardNow",
    "DashboardRoomProjectionDTO",
    "DashboardRoomRouteCatalog",
    "DashboardSceneRoute",
    "RoomProjectionSource",
]
