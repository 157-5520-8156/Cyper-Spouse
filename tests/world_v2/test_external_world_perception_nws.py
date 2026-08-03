from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import httpx
import pytest

from companion_daemon.world_v2.external_world_perception.nws import NwsAlertsAdapter


NOW = datetime(2026, 8, 3, 3, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_nws_geojson_maps_update_certainty_and_region_geometry() -> None:
    feed_url = "https://api.weather.gov/alerts/alert-2"
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "id": "https://api.weather.gov/alerts/alert-2",
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[114.0, 22.0], [115.0, 22.0], [114.0, 22.0]]],
                },
                "properties": {
                    "id": "urn:oid:alert-2",
                    "areaDesc": "Test County",
                    "affectedZones": ["https://api.weather.gov/zones/county/TST001"],
                    "references": ["sender@nws.gov,urn:oid:alert-1,2026-08-03T02:00:00+00:00"],
                    "sent": "2026-08-03T02:30:00+00:00",
                    "effective": "2026-08-03T02:30:00+00:00",
                    "onset": "2026-08-03T02:45:00+00:00",
                    "expires": "2026-08-03T05:30:00+00:00",
                    "status": "Actual",
                    "messageType": "Update",
                    "category": "Met",
                    "severity": "Severe",
                    "certainty": "Likely",
                    "urgency": "Immediate",
                    "event": "Flash Flood Warning",
                    "sender": "sender@nws.gov",
                    "senderName": "NWS Test Office",
                    "headline": "Flash Flood Warning issued for Test County",
                    "description": "Flooding is occurring.",
                    "instruction": "Move to higher ground.",
                },
            }
        ],
    }
    evidence = json.dumps(payload, separators=(",", ":")).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "application/geo+json, application/cap+xml"
        assert request.headers["User-Agent"] == "Girl-Agent-World-Perception/1"
        return httpx.Response(
            200, content=evidence, headers={"Content-Type": "application/geo+json"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await NwsAlertsAdapter(http_client=client, feed_url=feed_url).fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=10,
        )

    assert page.parser_rejected_item_count == 0
    assert len(page.items) == 1
    item = page.items[0]
    assert item.upstream_item_id == "urn:oid:alert-2"
    assert item.gateway_ref == f"direct-alerts:{feed_url}"
    assert item.signal_kind == "public_alert_update"
    assert item.correction_of_upstream_item_id == "urn:oid:alert-1"
    assert item.source_provided_certainty == "Likely"
    assert item.occurred_at == datetime(2026, 8, 3, 2, 45, tzinfo=UTC)
    assert item.expires_at == datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
    assert item.place_scope is not None
    assert item.place_scope.geometry_kind == "region_ref"
    assert item.place_scope.source_place_ref.startswith("nws-geometry:sha256:")
    assert item.place_scope.label == "Test County"
    assert "status:Actual" in item.entities
    assert "severity:Severe" in item.entities


@pytest.mark.asyncio
async def test_nws_cap_maps_cancel_and_isolates_a_bad_area_sibling() -> None:
    cap = b"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>urn:oid:alert-3</identifier>
  <sender>sender@nws.gov</sender>
  <sent>2026-08-03T04:00:00+00:00</sent>
  <status>Actual</status><msgType>Cancel</msgType><scope>Public</scope>
  <references>sender@nws.gov,urn:oid:alert-2,2026-08-03T02:30:00+00:00</references>
  <info>
    <category>Met</category><event>Flash Flood Warning</event>
    <urgency>Past</urgency><severity>Severe</severity><certainty>Observed</certainty>
    <effective>2026-08-03T04:00:00+00:00</effective>
    <expires>2026-08-03T04:30:00+00:00</expires>
    <senderName>NWS Test Office</senderName><headline>Warning cancelled</headline>
    <description>Flood waters have receded.</description>
    <area><areaDesc>Test County</areaDesc><polygon>bad polygon</polygon></area>
    <area><areaDesc>Second County</areaDesc><geocode><valueName>UGC</valueName><value>TST002</value></geocode></area>
  </info>
</alert>"""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=cap, headers={"Content-Type": "application/cap+xml"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await NwsAlertsAdapter(http_client=client).fetch(
            after=None,
            observed_at=NOW,
            deadline_at=NOW + timedelta(seconds=5),
            limit=10,
        )

    assert len(page.items) == 1
    item = page.items[0]
    assert item.signal_kind == "public_alert_cancel"
    assert item.correction_of_upstream_item_id == "urn:oid:alert-2"
    assert item.source_provided_certainty == "Observed"
    assert item.place_scope is not None
    assert item.place_scope.label == "Second County"
    assert page.parser_rejected_item_count == 1
    assert page.parser_failure_codes == ("nws_area_malformed",)


@pytest.mark.asyncio
async def test_nws_adapter_rejects_lookalike_hostname_before_transport() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="exact endpoint allowlist"):
            NwsAlertsAdapter(
                http_client=client,
                feed_url="https://api.weather.gov.evil.example/alerts/active",
            )
