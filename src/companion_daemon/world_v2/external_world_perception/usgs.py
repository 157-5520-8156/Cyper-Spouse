"""USGS Earthquake Hazards Program GeoJSON source adapter."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

import httpx

from ._source_http import fetch_source_bytes
from .contracts import (
    ExternalSignalPlace,
    ExternalSignalSourceFailure,
    ExternalSignalSourceItem,
    ExternalSignalSourcePage,
    SourceCursor,
)


USGS_ALL_HOUR_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
USGS_ALL_DAY_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
_ALLOWED_FEED_URLS = frozenset({USGS_ALL_HOUR_URL, USGS_ALL_DAY_URL})


class UsgsEarthquakeGeoJsonAdapter:
    """Acquire one fixed USGS summary feed without assigning World truth."""

    source_id = "usgs.earthquake.global.v1"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        feed_url: str = USGS_ALL_HOUR_URL,
    ) -> None:
        if feed_url not in _ALLOWED_FEED_URLS:
            raise ValueError("USGS feed URL is not in the exact endpoint allowlist")
        self._http_client = http_client
        self._feed_url = feed_url

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
            accept="application/geo+json",
        )
        if fetched.not_modified:
            return ExternalSignalSourcePage(
                evidence_media_type=fetched.media_type,
                evidence_bytes=b"",
                next_cursor=fetched.cursor,
                not_modified=True,
            )
        evidence = fetched.evidence_bytes
        try:
            payload = json.loads(evidence)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExternalSignalSourceFailure("usgs_geojson_malformed") from exc
        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
            raise ExternalSignalSourceFailure("usgs_geojson_shape_invalid")
        features = payload.get("features")
        if not isinstance(features, list):
            raise ExternalSignalSourceFailure("usgs_geojson_shape_invalid")
        items: list[ExternalSignalSourceItem] = []
        rejected = 0
        for feature in features[:limit]:
            try:
                items.append(_source_item(feature, feed_url=self._feed_url))
            except (ArithmeticError, KeyError, TypeError, ValueError):
                rejected += 1
        return ExternalSignalSourcePage(
            evidence_media_type=fetched.media_type,
            evidence_bytes=evidence,
            next_cursor=fetched.cursor,
            items=tuple(items),
            parser_rejected_item_count=rejected,
            parser_failure_codes=("usgs_feature_malformed",) if rejected else (),
        )


def _source_item(value: Any, *, feed_url: str) -> ExternalSignalSourceItem:
    if not isinstance(value, dict) or value.get("type") != "Feature":
        raise ValueError("USGS feature is invalid")
    item_id = _required_text(value.get("id"))
    properties = value.get("properties")
    geometry = value.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        raise ValueError("USGS feature properties are invalid")
    place = _required_text(properties.get("place"))
    magnitude = properties.get("mag")
    if not isinstance(magnitude, int | float) or isinstance(magnitude, bool):
        raise ValueError("USGS magnitude is invalid")
    status = _required_text(properties.get("status"))
    coordinates = geometry.get("coordinates")
    if geometry.get("type") != "Point" or not isinstance(coordinates, list):
        raise ValueError("USGS point geometry is invalid")
    if len(coordinates) < 2:
        raise ValueError("USGS point geometry is incomplete")
    longitude = _coordinate(coordinates[0], minimum=-180, maximum=180)
    latitude = _coordinate(coordinates[1], minimum=-90, maximum=90)
    occurred_at = _epoch_milliseconds(properties.get("time"))
    updated_at = _epoch_milliseconds(properties.get("updated"))
    canonical_url = properties.get("url")
    if not isinstance(canonical_url, str) or not canonical_url.startswith(
        "https://earthquake.usgs.gov/"
    ):
        canonical_url = None
    event_type = _required_text(properties.get("type"))
    return ExternalSignalSourceItem(
        upstream_item_id=item_id,
        gateway_ref=f"direct-json:{feed_url}",
        upstream_publisher_ref="authority:usgs-earthquake-hazards-program",
        signal_kind=event_type,
        headline=f"M {magnitude:g} - {place}",
        canonical_url=canonical_url,
        occurred_at=occurred_at,
        published_at=occurred_at,
        updated_at=updated_at,
        entities=(event_type, place),
        source_provided_certainty=status,
        place_scope=ExternalSignalPlace(
            geometry_kind="point",
            source_place_ref=f"usgs:event:{item_id}",
            label=place,
            latitude=latitude,
            longitude=longitude,
            source_provided_certainty=status,
        ),
    )


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("USGS text field is missing")
    return value.strip()


def _coordinate(value: object, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError("USGS coordinate is invalid")
    coordinate = float(value)
    if coordinate < minimum or coordinate > maximum:
        raise ValueError("USGS coordinate is out of range")
    return coordinate


def _epoch_milliseconds(value: object) -> datetime:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError("USGS timestamp is invalid")
    return datetime.fromtimestamp(float(value) / 1_000, tz=UTC)


__all__ = [
    "USGS_ALL_DAY_URL",
    "USGS_ALL_HOUR_URL",
    "UsgsEarthquakeGeoJsonAdapter",
]
