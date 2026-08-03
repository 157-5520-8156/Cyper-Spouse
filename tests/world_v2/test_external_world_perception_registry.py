from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.external_world_perception.registry import (
    ExternalPerceptionSourceRegistry,
    build_production_source_profiles,
    canonical_source_registry_content_hash,
    load_external_perception_source_registry,
)
from companion_daemon.world_v2.external_world_perception.rss import (
    RssAtomSourceAdapter,
    RssHubPullAdapter,
)
from companion_daemon.world_v2.external_world_perception.nws import NwsAlertsAdapter
from companion_daemon.world_v2.external_world_perception.usgs import (
    UsgsEarthquakeGeoJsonAdapter,
)


def _policy(
    revision: str,
    *,
    expose: bool = True,
    freeze: bool = True,
) -> dict[str, object]:
    return {
        "policy_revision": revision,
        "may_fetch": True,
        "may_cache_raw": True,
        "may_store_normalized_summary": True,
        "may_embed": True,
        "may_expose_to_character_model": expose,
        "may_quote": False,
        "may_freeze_durable_snapshot": freeze,
        "maximum_raw_retention_seconds": 2_592_000,
        "maximum_signal_retention_seconds": 7_776_000,
        "maximum_normalized_retention_seconds": 7_776_000,
    }


def _source(
    *,
    adapter_kind: str = "usgs_geojson",
    endpoint: str = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"),
    source_id: str = "usgs.earthquake.global.v1",
    policy: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "enabled": True,
        "adapter_kind": adapter_kind,
        "source_id": source_id,
        "endpoint": endpoint,
        "policy_owner_ref": "operator:external-perception-reviewer",
        "license_evidence_refs": ["https://www.usgs.gov/data-management/data-licensing"],
        "policy": policy or _policy("usgs-public-domain-review-2026-08-03"),
        "poll_interval_seconds": 60,
        "signal_ttl_seconds": 7_776_000,
        "raw_retention_seconds": 2_592_000,
        "normalized_retention_seconds": 7_776_000,
        "fetch_deadline_seconds": 20,
        "page_limit": 200,
    }


def _registry_value(*, sources: list[dict[str, object]]) -> dict[str, object]:
    material: dict[str, object] = {
        "schema_version": 1,
        "registry_revision": "external-perception-sources-2026-08-03.1",
        "uses_user_location": False,
        "sources": sources,
    }
    return {
        **material,
        "content_hash": canonical_source_registry_content_hash(material),
    }


def _rsshub_source(
    *,
    source_id: str = "cn.weibo.search.hot.v1",
    route: str = "/weibo/search/hot",
    allowed_item_hosts: list[str] | None = None,
) -> dict[str, object]:
    source = _source(
        adapter_kind="rsshub",
        endpoint="http://127.0.0.1:1200",
        source_id=source_id,
        policy=_policy(f"{source_id}-public-feed-review-2026-08-03"),
    )
    source.update(
        {
            "route": route,
            "signal_kind": "public_discourse_signal",
            "upstream_publisher_ref": "publisher:weibo",
            "allowed_item_hosts": allowed_item_hosts or ["s.weibo.com", "weibo.com"],
            "license_evidence_refs": [
                "https://github.com/DIYgod/RSSHub/tree/master/lib/routes/weibo"
            ],
            "poll_interval_seconds": 600,
            "page_limit": 12,
        }
    )
    return source


def _publisher_rss_source() -> dict[str, object]:
    source = _source(
        adapter_kind="rss_atom",
        endpoint="https://www.xinhuanet.com/edu/news_edu.xml",
        source_id="cn.news.xinhua.edu.v1",
        policy=_policy("xinhua-publisher-rss-review-2026-08-03"),
    )
    source.update(
        {
            "signal_kind": "publisher_news_signal",
            "upstream_publisher_ref": "publisher:xinhua",
            "allowed_item_hosts": ["www.news.cn", "www.xinhuanet.com"],
            "license_evidence_refs": ["https://www.news.cn/linktous.htm"],
            "poll_interval_seconds": 600,
            "page_limit": 5,
        }
    )
    return source


def _write_registry(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_off_mode_is_disabled_without_reading_registry(tmp_path: Path) -> None:
    malformed = tmp_path / "registry.json"
    malformed.write_text("not json", encoding="utf-8")
    result = build_production_source_profiles(
        deployment_mode="off",
        registry_path=malformed,
        http_client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "disabled"
    assert result.reason == "mode_off"
    assert result.source_profiles == ()


@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_non_off_mode_without_registry_is_fail_closed(mode: str) -> None:
    result = build_production_source_profiles(
        deployment_mode=mode,  # type: ignore[arg-type]
        registry_path=None,
        http_client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "disabled"
    assert result.reason == "registry_not_configured"
    assert result.source_profiles == ()


def test_shadow_registry_builds_only_explicit_usgs_and_nws_adapters(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    nws = _source(
        adapter_kind="nws_alerts",
        endpoint="https://api.weather.gov/alerts/active",
        source_id="noaa.nws.alerts.us.cap.v1",
        policy=_policy("nws-public-domain-review-2026-08-03", expose=False, freeze=False),
    )
    nws["license_evidence_refs"] = ["https://www.weather.gov/disclaimer"]
    _write_registry(registry_path, _registry_value(sources=[_source(), nws]))

    result = build_production_source_profiles(
        deployment_mode="shadow",
        registry_path=registry_path,
        http_client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "ready"
    assert result.reason == "ready"
    assert result.registry_revision == "external-perception-sources-2026-08-03.1"
    assert result.registry_content_hash.startswith("sha256:")
    assert [type(item.adapter) for item in result.source_profiles] == [
        UsgsEarthquakeGeoJsonAdapter,
        NwsAlertsAdapter,
    ]
    assert [item.adapter.source_id for item in result.source_profiles] == [
        "usgs.earthquake.global.v1",
        "noaa.nws.alerts.us.cap.v1",
    ]


def test_shadow_registry_builds_exact_allowlisted_local_rsshub_route(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, _registry_value(sources=[_rsshub_source()]))

    result = build_production_source_profiles(
        deployment_mode="shadow",
        registry_path=registry_path,
        http_client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "ready"
    assert len(result.source_profiles) == 1
    adapter = result.source_profiles[0].adapter
    assert isinstance(adapter, RssHubPullAdapter)
    assert adapter.source_id == "cn.weibo.search.hot.v1"


def test_registry_builds_publisher_native_rss_without_rsshub(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, _registry_value(sources=[_publisher_rss_source()]))

    result = build_production_source_profiles(
        deployment_mode="live",
        registry_path=registry_path,
        http_client=object(),  # type: ignore[arg-type]
    )

    adapter = result.source_profiles[0].adapter
    assert isinstance(adapter, RssAtomSourceAdapter)
    assert not isinstance(adapter, RssHubPullAdapter)
    assert adapter.source_id == "cn.news.xinhua.edu.v1"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"endpoint": "https://rsshub.example.com"}, "loopback"),
        ({"route": "https://evil.example/feed"}, "fixed local path"),
        ({"route": "/weibo/search/hot#fragment"}, "fixed local path"),
        ({"allowed_item_hosts": []}, "at least 1 item"),
    ],
)
def test_registry_rejects_nonlocal_or_open_ended_rsshub_authority(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    registry_path = tmp_path / "registry.json"
    source = _rsshub_source()
    source.update(mutation)
    _write_registry(registry_path, _registry_value(sources=[source]))

    with pytest.raises((ValueError, ValidationError), match=message):
        build_production_source_profiles(
            deployment_mode="shadow",
            registry_path=registry_path,
            http_client=object(),  # type: ignore[arg-type]
        )


def test_registry_allows_undated_trend_observations_to_cross_with_observation_time(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    source = _rsshub_source()
    source.update(
        {
            "signal_kind": "platform_trend_observation",
            "allow_undated_items": True,
        }
    )
    _write_registry(registry_path, _registry_value(sources=[source]))

    result = build_production_source_profiles(
        deployment_mode="live",
        registry_path=registry_path,
        http_client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "ready"
    assert len(result.source_profiles) == 1


def test_registry_is_hash_bound_and_frozen(tmp_path: Path) -> None:
    value = _registry_value(sources=[_source()])
    registry = ExternalPerceptionSourceRegistry.model_validate_json(json.dumps(value))
    with pytest.raises(ValidationError):
        registry.registry_revision = "changed"  # type: ignore[misc]

    value["sources"][0]["poll_interval_seconds"] = 61  # type: ignore[index]
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, value)
    with pytest.raises(ValueError, match="registry content hash mismatch"):
        build_production_source_profiles(
            deployment_mode="shadow",
            registry_path=registry_path,
            http_client=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("expose", "freeze"),
    [(False, True), (True, False)],
)
def test_live_requires_model_exposure_and_durable_snapshot_rights_to_match(
    tmp_path: Path,
    expose: bool,
    freeze: bool,
) -> None:
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        _registry_value(
            sources=[_source(policy=_policy("review-1", expose=expose, freeze=freeze))]
        ),
    )
    with pytest.raises(ValueError, match="live source policy"):
        build_production_source_profiles(
            deployment_mode="live",
            registry_path=registry_path,
            http_client=object(),  # type: ignore[arg-type]
        )


def test_live_allows_a_fetch_only_source_that_cannot_enter_character_attention(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        _registry_value(
            sources=[_source(policy=_policy("fetch-only-1", expose=False, freeze=False))]
        ),
    )

    result = build_production_source_profiles(
        deployment_mode="live",
        registry_path=registry_path,
        http_client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "ready"
    assert len(result.source_profiles) == 1
    assert not result.source_profiles[0].policy.may_expose_to_character_model
    assert not result.source_profiles[0].policy.may_freeze_durable_snapshot


@pytest.mark.parametrize(
    "mutation",
    [
        {"policy_owner_ref": ""},
        {"license_evidence_refs": []},
        {"endpoint": "https://example.com/earthquakes.geojson"},
    ],
)
def test_registry_rejects_missing_audit_evidence_or_non_allowlisted_endpoint(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    registry_path = tmp_path / "registry.json"
    source = _source()
    source.update(mutation)
    _write_registry(registry_path, _registry_value(sources=[source]))
    with pytest.raises((ValueError, ValidationError)):
        build_production_source_profiles(
            deployment_mode="shadow",
            registry_path=registry_path,
            http_client=object(),  # type: ignore[arg-type]
        )


def test_registry_forbids_user_location_and_duplicate_source_ids(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    value = _registry_value(sources=[_source(), _source()])
    value["uses_user_location"] = True
    value["content_hash"] = canonical_source_registry_content_hash(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    _write_registry(registry_path, value)
    with pytest.raises((ValueError, ValidationError)):
        build_production_source_profiles(
            deployment_mode="shadow",
            registry_path=registry_path,
            http_client=object(),  # type: ignore[arg-type]
        )


def test_registry_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        '{"schema_version":1,"schema_version":1,"registry_revision":"r",'
        '"content_hash":"sha256:' + "0" * 64 + '","uses_user_location":false,"sources":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON keys"):
        build_production_source_profiles(
            deployment_mode="shadow",
            registry_path=registry_path,
            http_client=object(),  # type: ignore[arg-type]
        )


def test_registry_with_no_enabled_sources_remains_disabled(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    source = _source()
    source["enabled"] = False
    _write_registry(registry_path, _registry_value(sources=[source]))

    result = build_production_source_profiles(
        deployment_mode="live",
        registry_path=registry_path,
        http_client=object(),  # type: ignore[arg-type]
    )

    assert result.status == "disabled"
    assert result.reason == "no_enabled_sources"
    assert result.source_profiles == ()


def test_checked_in_production_registry_is_domestic_and_bounded() -> None:
    path = Path(__file__).parents[2] / "configs" / "external-perception-sources.cn.json"

    registry = load_external_perception_source_registry(path)
    enabled = tuple(item for item in registry.sources if item.enabled)

    social = tuple(item for item in registry.sources if item.source_id.startswith("cn.social."))
    publisher = tuple(item for item in enabled if item.adapter_kind == "rss_atom")

    assert len(enabled) == 11
    assert len(publisher) == 1
    assert len(social) == 10
    assert all(item.enabled for item in social)
    assert all(item.source_id.startswith("cn.") for item in enabled)
    assert all(item.adapter_kind in {"rss_atom", "rsshub"} for item in enabled)
    assert sum(item.page_limit for item in publisher) <= 3
    assert sum(item.page_limit for item in social) <= 200
    assert all(item.page_limit <= 20 for item in social)
    assert all(not item.policy.may_store_normalized_summary for item in publisher)
    assert all(item.policy.may_store_normalized_summary for item in social)
    assert all(not item.policy.may_quote for item in enabled)
