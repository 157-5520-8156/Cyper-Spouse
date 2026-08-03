"""NOAA/NWS alerts GeoJSON and CAP 1.2 source adapter."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx

from ._source_http import fetch_source_bytes
from .contracts import (
    ExternalSignalPlace,
    ExternalSignalSourceFailure,
    ExternalSignalSourceItem,
    ExternalSignalSourcePage,
    SourceCursor,
)


NWS_ACTIVE_ALERTS_URL = "https://api.weather.gov/alerts/active"
_NWS_HOST = "api.weather.gov"
_UNSAFE_XML_MARKERS = (b"<!doctype", b"<!entity")
_MESSAGE_KINDS = {
    "alert": "public_alert",
    "update": "public_alert_update",
    "cancel": "public_alert_cancel",
    "error": "public_alert_correction",
    "ack": "public_alert_acknowledgement",
}


class NwsAlertsAdapter:
    """Acquire one fixed NWS alert index or alert resource."""

    source_id = "noaa.nws.alerts.us.cap.v1"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        feed_url: str = NWS_ACTIVE_ALERTS_URL,
        user_agent: str = "Girl-Agent-World-Perception/1",
    ) -> None:
        _validate_endpoint(feed_url)
        if not user_agent.strip() or len(user_agent) > 256:
            raise ValueError("NWS User-Agent is invalid")
        self._http_client = http_client
        self._feed_url = feed_url
        self._user_agent = user_agent

    async def fetch(
        self,
        *,
        after: SourceCursor | None,
        observed_at: datetime,
        deadline_at: datetime,
        limit: int,
    ) -> ExternalSignalSourcePage:
        fetched = await fetch_source_bytes(
            http_client=self._http_client,
            url=self._feed_url,
            after=after,
            observed_at=observed_at,
            deadline_at=deadline_at,
            accept="application/geo+json, application/cap+xml",
            extra_headers={"User-Agent": self._user_agent},
        )
        if fetched.not_modified:
            return ExternalSignalSourcePage(
                evidence_media_type=fetched.media_type,
                evidence_bytes=b"",
                next_cursor=fetched.cursor,
                not_modified=True,
            )
        media_type = fetched.media_type.partition(";")[0].strip().casefold()
        if media_type in {"application/cap+xml", "application/xml", "text/xml"}:
            items, rejected, failure_codes = _parse_cap(
                fetched.evidence_bytes, limit=limit, feed_url=self._feed_url
            )
        else:
            items, rejected, failure_codes = _parse_geojson(
                fetched.evidence_bytes, limit=limit, feed_url=self._feed_url
            )
        return ExternalSignalSourcePage(
            evidence_media_type=fetched.media_type,
            evidence_bytes=fetched.evidence_bytes,
            next_cursor=fetched.cursor,
            items=items,
            parser_rejected_item_count=rejected,
            parser_failure_codes=failure_codes,
        )


def _parse_geojson(
    evidence: bytes, *, limit: int, feed_url: str
) -> tuple[tuple[ExternalSignalSourceItem, ...], int, tuple[str, ...]]:
    try:
        payload = json.loads(evidence)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalSignalSourceFailure("nws_geojson_malformed") from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ExternalSignalSourceFailure("nws_geojson_shape_invalid")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ExternalSignalSourceFailure("nws_geojson_shape_invalid")
    items: list[ExternalSignalSourceItem] = []
    rejected = 0
    for feature in features[:limit]:
        try:
            items.append(_geojson_item(feature, feed_url=feed_url))
        except (ArithmeticError, KeyError, TypeError, ValueError):
            rejected += 1
    return (
        tuple(items),
        rejected,
        ("nws_feature_malformed",) if rejected else (),
    )


def _geojson_item(value: Any, *, feed_url: str) -> ExternalSignalSourceItem:
    if not isinstance(value, dict) or value.get("type") != "Feature":
        raise ValueError("NWS feature is invalid")
    properties = value.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("NWS properties are invalid")
    item_id = _required_text(properties.get("id"))
    message_type = _required_text(properties.get("messageType"))
    sent = _source_datetime(properties.get("sent"))
    event = _required_text(properties.get("event"))
    headline = _optional_text(properties.get("headline")) or event
    certainty = _required_text(properties.get("certainty"))
    status = _required_text(properties.get("status"))
    severity = _required_text(properties.get("severity"))
    urgency = _required_text(properties.get("urgency"))
    area = _required_text(properties.get("areaDesc"))
    geometry = value.get("geometry")
    affected_zones = properties.get("affectedZones")
    place_scope = _geojson_place(
        geometry=geometry,
        affected_zones=affected_zones,
        area=area,
        certainty=certainty,
        item_id=item_id,
    )
    canonical_url = _canonical_url(value.get("id"))
    description = _optional_text(properties.get("description"))
    instruction = _optional_text(properties.get("instruction"))
    references = properties.get("references")
    return ExternalSignalSourceItem(
        upstream_item_id=item_id,
        gateway_ref=f"direct-alerts:{feed_url}",
        upstream_publisher_ref="authority:noaa-national-weather-service",
        signal_kind=_signal_kind(message_type),
        headline=headline,
        licensed_summary=_summary(description, instruction),
        canonical_url=canonical_url,
        occurred_at=_optional_datetime(properties.get("onset"))
        or _optional_datetime(properties.get("effective")),
        published_at=sent,
        expires_at=_optional_datetime(properties.get("expires")),
        correction_of_upstream_item_id=_correction_ref(message_type, references),
        entities=_entities(
            status=status,
            message_type=message_type,
            severity=severity,
            urgency=urgency,
            event=event,
        ),
        source_provided_certainty=certainty,
        place_scope=place_scope,
    )


def _parse_cap(
    evidence: bytes, *, limit: int, feed_url: str
) -> tuple[tuple[ExternalSignalSourceItem, ...], int, tuple[str, ...]]:
    if any(marker in evidence.lower() for marker in _UNSAFE_XML_MARKERS):
        raise ExternalSignalSourceFailure("nws_cap_unsafe_declaration")
    try:
        root = ET.fromstring(evidence)
    except ET.ParseError as exc:
        raise ExternalSignalSourceFailure("nws_cap_malformed") from exc
    if _local_name(root.tag) != "alert":
        raise ExternalSignalSourceFailure("nws_cap_shape_invalid")
    if limit <= 0:
        return (), 0, ()
    identifier = _required_text(_child_text(root, "identifier"))
    sender = _required_text(_child_text(root, "sender"))
    sent = _source_datetime(_child_text(root, "sent"))
    status = _required_text(_child_text(root, "status"))
    message_type = _required_text(_child_text(root, "msgType"))
    scope = _required_text(_child_text(root, "scope"))
    info = next((child for child in root if _local_name(child.tag) == "info"), None)
    if info is None:
        raise ExternalSignalSourceFailure("nws_cap_shape_invalid")
    event = _required_text(_child_text(info, "event"))
    certainty = _required_text(_child_text(info, "certainty"))
    severity = _required_text(_child_text(info, "severity"))
    urgency = _required_text(_child_text(info, "urgency"))
    place_scope, rejected_areas = _cap_place(info=info, certainty=certainty)
    item = ExternalSignalSourceItem(
        upstream_item_id=identifier,
        gateway_ref=f"direct-cap:{feed_url}",
        upstream_publisher_ref="authority:noaa-national-weather-service",
        signal_kind=_signal_kind(message_type),
        headline=_optional_text(_child_text(info, "headline")) or event,
        licensed_summary=_summary(
            _optional_text(_child_text(info, "description")),
            _optional_text(_child_text(info, "instruction")),
        ),
        canonical_url=_canonical_url(_child_text(info, "web")),
        occurred_at=_optional_datetime(_child_text(info, "onset"))
        or _optional_datetime(_child_text(info, "effective")),
        published_at=sent,
        expires_at=_optional_datetime(_child_text(info, "expires")),
        correction_of_upstream_item_id=_correction_ref(
            message_type, _child_text(root, "references")
        ),
        entities=_entities(
            status=status,
            message_type=message_type,
            severity=severity,
            urgency=urgency,
            event=event,
            scope=scope,
            sender=sender,
        ),
        source_provided_certainty=certainty,
        place_scope=place_scope,
    )
    return (
        (item,),
        rejected_areas,
        ("nws_area_malformed",) if rejected_areas else (),
    )


def _geojson_place(
    *,
    geometry: object,
    affected_zones: object,
    area: str,
    certainty: str,
    item_id: str,
) -> ExternalSignalPlace:
    if isinstance(geometry, dict):
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        if geometry_type == "Point" and isinstance(coordinates, list) and len(coordinates) >= 2:
            longitude = _coordinate(coordinates[0], minimum=-180, maximum=180)
            latitude = _coordinate(coordinates[1], minimum=-90, maximum=90)
            return ExternalSignalPlace(
                geometry_kind="point",
                source_place_ref=f"nws-alert:{item_id}",
                label=area,
                latitude=latitude,
                longitude=longitude,
                source_provided_certainty=certainty,
            )
        if geometry_type in {"Polygon", "MultiPolygon"} and isinstance(coordinates, list):
            material = json.dumps(geometry, sort_keys=True, separators=(",", ":"), allow_nan=False)
            return ExternalSignalPlace(
                geometry_kind="region_ref",
                source_place_ref="nws-geometry:sha256:"
                + hashlib.sha256(material.encode()).hexdigest(),
                label=area,
                source_provided_certainty=certainty,
            )
    zones = (
        tuple(
            zone
            for zone in affected_zones
            if isinstance(zone, str) and zone.startswith("https://api.weather.gov/zones/")
        )
        if isinstance(affected_zones, list)
        else ()
    )
    material = "\x1f".join(zones) or item_id
    return ExternalSignalPlace(
        geometry_kind="region_ref",
        source_place_ref="nws-area:sha256:" + hashlib.sha256(material.encode()).hexdigest(),
        label=area,
        source_provided_certainty=certainty,
    )


def _cap_place(*, info: ET.Element, certainty: str) -> tuple[ExternalSignalPlace, int]:
    rejected = 0
    for area_element in (child for child in info if _local_name(child.tag) == "area"):
        try:
            area = _required_text(_child_text(area_element, "areaDesc"))
            geometry_parts: list[str] = []
            for child in area_element:
                name = _local_name(child.tag)
                if name == "polygon":
                    polygon = _required_text("".join(child.itertext()))
                    points = polygon.split()
                    if len(points) < 3 or any(len(point.split(",")) != 2 for point in points):
                        raise ValueError("NWS CAP polygon is malformed")
                    geometry_parts.append(f"polygon:{polygon}")
                elif name == "circle":
                    circle = _required_text("".join(child.itertext()))
                    if len(circle.split()) != 2 or len(circle.split()[0].split(",")) != 2:
                        raise ValueError("NWS CAP circle is malformed")
                    geometry_parts.append(f"circle:{circle}")
                elif name == "geocode":
                    value_name = _required_text(_child_text(child, "valueName"))
                    code = _required_text(_child_text(child, "value"))
                    geometry_parts.append(f"geocode:{value_name}:{code}")
            if not geometry_parts:
                raise ValueError("NWS CAP area has no source geometry")
            material = "\x1f".join(geometry_parts)
            return (
                ExternalSignalPlace(
                    geometry_kind="region_ref",
                    source_place_ref="nws-cap-area:sha256:"
                    + hashlib.sha256(material.encode()).hexdigest(),
                    label=area,
                    source_provided_certainty=certainty,
                ),
                rejected,
            )
        except (KeyError, TypeError, ValueError):
            rejected += 1
    raise ExternalSignalSourceFailure("nws_cap_area_missing")


def _correction_ref(message_type: str, references: object) -> str | None:
    if message_type.casefold() not in {"update", "cancel", "error"}:
        return None
    values = references if isinstance(references, list) else [references]
    for value in reversed(values):
        if isinstance(value, dict):
            value = value.get("identifier") or value.get("id") or value.get("@id")
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = value.strip().split()[-1]
        parts = candidate.split(",")
        return parts[1].strip() if len(parts) >= 3 and parts[1].strip() else candidate
    raise ValueError("NWS correcting alert has no source reference")


def _signal_kind(message_type: str) -> str:
    try:
        return _MESSAGE_KINDS[message_type.casefold()]
    except KeyError as exc:
        raise ValueError("NWS CAP message type is unsupported") from exc


def _entities(**values: str) -> tuple[str, ...]:
    return tuple(f"{key}:{value}"[:256] for key, value in values.items() if value)


def _summary(description: str, instruction: str) -> str:
    return "\n\n".join(value for value in (description, instruction) if value)[:8_000]


def _source_datetime(value: object) -> datetime:
    text = _required_text(value)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("NWS timestamp must include an offset")
    return parsed.astimezone(UTC)


def _optional_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    return _source_datetime(value)


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("NWS text field is missing")
    return value.strip()


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coordinate(value: object, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError("NWS coordinate is invalid")
    coordinate = float(value)
    if coordinate < minimum or coordinate > maximum:
        raise ValueError("NWS coordinate is out of range")
    return coordinate


def _canonical_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {_NWS_HOST, "www.weather.gov", "alerts.weather.gov"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value.strip()[:4_096]


def _child_text(element: ET.Element, wanted: str) -> str:
    for child in element:
        if _local_name(child.tag) == wanted.casefold():
            return "".join(child.itertext()).strip()
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _validate_endpoint(value: str) -> None:
    parsed = urlsplit(value)
    path_parts = parsed.path.split("/")
    valid_path = parsed.path == "/alerts/active" or (
        len(path_parts) == 3 and path_parts[:2] == ["", "alerts"] and bool(path_parts[2])
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != _NWS_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not valid_path
    ):
        raise ValueError("NWS feed URL is not in the exact endpoint allowlist")


__all__ = ["NWS_ACTIVE_ALERTS_URL", "NwsAlertsAdapter"]
