from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import httpx
import pytest

from companion_daemon.world_v2.external_world_perception.contracts import (
    ExternalSignalSourceFailure,
    MAX_EVIDENCE_BYTES,
    SourceCursor,
)
from companion_daemon.world_v2.external_world_perception.usgs import (
    USGS_ALL_DAY_URL,
    UsgsEarthquakeGeoJsonAdapter,
)


NOW = datetime(2026, 8, 3, 2, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_usgs_adapter_normalizes_one_source_revision_and_records_http_validator() -> None:
    requests: list[httpx.Request] = []
    payload = {
        "type": "FeatureCollection",
        "metadata": {"generated": 1_775_354_400_000, "count": 1},
        "features": [
            {
                "type": "Feature",
                "id": "us7000abcd",
                "properties": {
                    "mag": 5.2,
                    "place": "10 km S of Testville",
                    "time": 1_775_354_100_000,
                    "updated": 1_775_354_220_000,
                    "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000abcd",
                    "status": "reviewed",
                    "type": "earthquake",
                },
                "geometry": {"type": "Point", "coordinates": [114.1, 22.3, 12.4]},
            }
        ],
    }
    evidence = json.dumps(payload, separators=(",", ":")).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=evidence,
            headers={"Content-Type": "application/geo+json", "ETag": '"feed-v1"'},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await UsgsEarthquakeGeoJsonAdapter(http_client=client).fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=10,
        )

    assert requests[0].url == httpx.URL(
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
    )
    assert page.evidence_bytes == evidence
    assert json.loads(page.next_cursor.opaque_value)["etag"] == '"feed-v1"'
    assert page.parser_rejected_item_count == 0
    assert len(page.items) == 1
    item = page.items[0]
    assert item.upstream_item_id == "us7000abcd"
    assert item.signal_kind == "earthquake"
    assert item.headline == "M 5.2 - 10 km S of Testville"
    assert item.occurred_at == datetime(2026, 4, 5, 1, 55, tzinfo=UTC)
    assert item.updated_at == datetime(2026, 4, 5, 1, 57, tzinfo=UTC)
    assert item.source_provided_certainty == "reviewed"
    assert item.place_scope is not None
    assert item.place_scope.model_dump() == {
        "geometry_kind": "point",
        "source_place_ref": "usgs:event:us7000abcd",
        "label": "10 km S of Testville",
        "latitude": 22.3,
        "longitude": 114.1,
        "radius_meters": None,
        "source_provided_certainty": "reviewed",
    }


@pytest.mark.asyncio
async def test_usgs_adapter_uses_etag_and_represents_304_without_evidence() -> None:
    requests: list[httpx.Request] = []
    cursor = SourceCursor(
        opaque_value=json.dumps(
            {"etag": '"feed-v1"', "last_modified": "Sun, 03 Aug 2026 02:00:00 GMT"}
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await UsgsEarthquakeGeoJsonAdapter(http_client=client).fetch(
            after=cursor,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=10,
        )

    assert requests[0].headers["If-None-Match"] == '"feed-v1"'
    assert requests[0].headers["If-Modified-Since"] == "Sun, 03 Aug 2026 02:00:00 GMT"
    assert page.not_modified is True
    assert page.evidence_bytes == b""
    assert page.items == ()
    assert page.next_cursor == cursor


@pytest.mark.asyncio
async def test_usgs_adapter_preserves_429_retry_after_as_technical_failure() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "37"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalSignalSourceFailure) as caught:
            await UsgsEarthquakeGeoJsonAdapter(http_client=client).fetch(
                after=None,
                observed_at=NOW,
                deadline_at=NOW + timedelta(seconds=5),
                limit=10,
            )

    assert caught.value.failure_code == "source_rate_limited"
    assert caught.value.retry_after_seconds == 37


@pytest.mark.asyncio
async def test_usgs_adapter_rejects_redirects_without_following_location() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/feed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalSignalSourceFailure) as caught:
            await UsgsEarthquakeGeoJsonAdapter(http_client=client).fetch(
                after=None,
                observed_at=NOW,
                deadline_at=NOW + timedelta(seconds=5),
                limit=10,
            )

    assert caught.value.failure_code == "source_redirect_rejected"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_usgs_adapter_stops_when_source_response_exceeds_evidence_limit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (MAX_EVIDENCE_BYTES + 1),
            headers={"Content-Type": "application/geo+json"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalSignalSourceFailure) as caught:
            await UsgsEarthquakeGeoJsonAdapter(http_client=client).fetch(
                after=None,
                observed_at=NOW,
                deadline_at=NOW + timedelta(seconds=5),
                limit=10,
            )

    assert caught.value.failure_code == "source_response_too_large"


@pytest.mark.asyncio
async def test_usgs_adapter_isolates_one_bad_feature_from_its_valid_sibling() -> None:
    valid = {
        "type": "Feature",
        "id": "us7000good",
        "properties": {
            "mag": 3.4,
            "place": "Valid sibling",
            "time": 1_700_000_000_000,
            "updated": 1_700_000_060_000,
            "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000good",
            "status": "automatic",
            "type": "earthquake",
        },
        "geometry": {"type": "Point", "coordinates": [120.0, 30.0, 5.0]},
    }
    invalid = json.loads(json.dumps(valid))
    invalid["id"] = "us7000bad"
    invalid["properties"]["time"] = 10**1_000
    payload = json.dumps(
        {"type": "FeatureCollection", "metadata": {}, "features": [invalid, valid]}
    ).encode()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await UsgsEarthquakeGeoJsonAdapter(http_client=client).fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=10,
        )

    assert tuple(item.upstream_item_id for item in page.items) == ("us7000good",)
    assert page.parser_rejected_item_count == 1
    assert page.parser_failure_codes == ("usgs_feature_malformed",)


@pytest.mark.asyncio
async def test_usgs_item_gateway_ref_binds_the_configured_feed() -> None:
    payload = {
        "type": "FeatureCollection",
        "metadata": {},
        "features": [
            {
                "type": "Feature",
                "id": "us7000day",
                "properties": {
                    "mag": 2.1,
                    "place": "Day feed",
                    "time": 1_700_000_000_000,
                    "updated": 1_700_000_060_000,
                    "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000day",
                    "status": "reviewed",
                    "type": "earthquake",
                },
                "geometry": {"type": "Point", "coordinates": [120.0, 30.0, 5.0]},
            }
        ],
    }

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await UsgsEarthquakeGeoJsonAdapter(
            http_client=client, feed_url=USGS_ALL_DAY_URL
        ).fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=10,
        )

    assert page.items[0].gateway_ref == f"direct-json:{USGS_ALL_DAY_URL}"
