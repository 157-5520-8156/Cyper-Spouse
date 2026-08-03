from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2.external_world_perception.contracts import (
    PerceptionChannelProof,
    SourcePolicyRevision,
    SourceProfile,
)
from companion_daemon.world_v2.external_world_perception.authorized_search import (
    AcceptedWebSearchResultAdapter,
)
from companion_daemon.world_v2.external_world_perception.deployment import (
    build_external_world_perception_deployment,
)
from companion_daemon.world_v2.external_world_perception.production_attention import (
    StaticLiveAttentionChannelPort,
)
from companion_daemon.world_v2.external_world_perception.registry import (
    canonical_source_registry_content_hash,
)
from companion_daemon.world_v2.public_information_authority_provisioning import (
    PublicInformationAuthorityProvisioner,
)
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class Model:
    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del messages, temperature
        return '{"selections":[]}'


class Life:
    async def advance_life_ecology_once(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return object()


class _ProjectionReader:
    def current_projection(self, *, world_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"deployment construction must not read {world_id}")


class _ContentReader:
    def read_exact(self, *, result_ref: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"deployment construction must not read {result_ref}")


def _authorized_search_profile() -> SourceProfile:
    return SourceProfile(
        adapter=AcceptedWebSearchResultAdapter(
            source_id="world.accepted-web-search.v1",
            world_id="world:test",
            projection_reader=_ProjectionReader(),
            content_reader=_ContentReader(),
        ),
        policy=SourcePolicyRevision(
            policy_revision="policy:accepted-search:test:1",
            may_fetch=True,
            may_cache_raw=True,
            may_store_normalized_summary=True,
            may_embed=False,
            may_expose_to_character_model=True,
            may_quote=False,
            may_freeze_durable_snapshot=True,
            maximum_raw_retention_seconds=3_600,
            maximum_signal_retention_seconds=3_600,
            maximum_normalized_retention_seconds=3_600,
        ),
        poll_interval_seconds=30,
        signal_ttl_seconds=3_600,
        raw_retention_seconds=3_600,
        normalized_retention_seconds=3_600,
    )


def _registry(path: Path, *, enabled: bool = True) -> None:
    value = {
        "schema_version": 1,
        "registry_revision": "registry:test:1",
        "uses_user_location": False,
        "sources": [
            {
                "enabled": enabled,
                "adapter_kind": "usgs_geojson",
                "source_id": "usgs.earthquake.global.v1",
                "endpoint": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
                "policy_owner_ref": "operator:test",
                "license_evidence_refs": ["https://www.usgs.gov/data-management/data-licensing"],
                "policy": {
                    "policy_revision": "policy:usgs:test:1",
                    "may_fetch": True,
                    "may_cache_raw": True,
                    "may_store_normalized_summary": True,
                    "may_embed": False,
                    "may_expose_to_character_model": True,
                    "may_quote": False,
                    "may_freeze_durable_snapshot": True,
                    "maximum_raw_retention_seconds": 604800,
                    "maximum_signal_retention_seconds": 604800,
                    "maximum_normalized_retention_seconds": 604800,
                },
                "poll_interval_seconds": 60,
                "signal_ttl_seconds": 3600,
                "raw_retention_seconds": 3600,
                "normalized_retention_seconds": 3600,
                "fetch_deadline_seconds": 10,
                "page_limit": 100,
            }
        ],
    }
    value["content_hash"] = canonical_source_registry_content_hash(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_off_and_missing_registry_are_fail_closed_without_opening_resources(tmp_path: Path) -> None:
    off = build_external_world_perception_deployment(
        settings=Settings(
            _env_file=None,
            DATABASE_PATH=tmp_path / "world.sqlite",
            WORLD_V2_EXTERNAL_PERCEPTION_MODE="off",
        ),
        world_id="world:test",
        actor_ref="agent:companion",
        model=Model(),
        life=Life(),
    )
    missing = build_external_world_perception_deployment(
        settings=Settings(
            _env_file=None,
            DATABASE_PATH=tmp_path / "world.sqlite",
            WORLD_V2_EXTERNAL_PERCEPTION_MODE="live",
        ),
        world_id="world:test",
        actor_ref="agent:companion",
        model=Model(),
        life=Life(),
    )

    assert (off.status, off.reason, off.hub) == ("disabled", "mode_off", None)
    assert (missing.status, missing.reason, missing.hub) == (
        "disabled",
        "registry_not_configured",
        None,
    )


def test_live_requires_an_explicit_source_bound_character_channel(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    _registry(registry)
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=tmp_path / "world.sqlite",
        WORLD_V2_EXTERNAL_PERCEPTION_MODE="live",
        WORLD_V2_EXTERNAL_PERCEPTION_SOURCE_REGISTRY_PATH=registry,
        WORLD_V2_EXTERNAL_PERCEPTION_SIDECAR_PATH=tmp_path / "sidecar.sqlite",
    )

    disabled = build_external_world_perception_deployment(
        settings=settings,
        world_id="world:test",
        actor_ref="agent:companion",
        model=Model(),
        life=Life(),
    )
    channel_port = StaticLiveAttentionChannelPort(
        (
            PerceptionChannelProof(
                channel_ref="channel:public-alert",
                channel_kind="public_alert",
                evidence_refs=("event:channel-capability:1",),
                accessible_source_ids=("usgs.earthquake.global.v1",),
                valid_until=NOW + timedelta(days=1),
            ),
        )
    )
    ready = build_external_world_perception_deployment(
        settings=settings,
        world_id="world:test",
        actor_ref="agent:companion",
        model=Model(),
        life=Life(),
        channel_port=channel_port,
        wall_clock=lambda: NOW,
    )

    assert (disabled.status, disabled.reason, disabled.hub) == (
        "disabled",
        "channel_not_configured",
        None,
    )
    assert ready.status == "ready"
    assert ready.reason == "ready"
    assert ready.hub is not None


@pytest.mark.asyncio
async def test_live_auto_composes_channel_from_root_signed_public_information_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    database = tmp_path / "world.sqlite"
    registry = tmp_path / "registry.json"
    _registry(registry)
    ledger = SQLiteWorldLedger(path=database, world_id="world:test")
    try:
        ledger.commit(
            (
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id="event:world-started:public-info-deployment",
                    event_type="WorldStarted",
                    world_id="world:test",
                    logical_time=NOW,
                    created_at=NOW,
                    actor="system:test",
                    source="test",
                    trace_id="trace:public-info-deployment",
                    causation_id="cause:public-info-deployment",
                    correlation_id="correlation:public-info-deployment",
                    idempotency_key="identity:public-info-deployment",
                    payload={},
                ),
            ),
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
        PublicInformationAuthorityProvisioner(
            ledger=ledger,
            signing_key_hex="11" * 32,
            companion_actor_ref="agent:companion",
            registry_content_hash=json.loads(registry.read_text())["content_hash"],
        ).ensure()
    finally:
        ledger.close()
    deployment = build_external_world_perception_deployment(
        settings=Settings(
            _env_file=None,
            database_path=database,
            WORLD_V2_EXTERNAL_PERCEPTION_MODE="live",
            WORLD_V2_EXTERNAL_PERCEPTION_SOURCE_REGISTRY_PATH=registry,
            WORLD_V2_EXTERNAL_PERCEPTION_SIDECAR_PATH=tmp_path / "sidecar.sqlite",
        ),
        world_id="world:test",
        actor_ref="agent:companion",
        model=Model(),
        life=Life(),
        wall_clock=lambda: NOW,
    )

    assert deployment.status == "ready"
    assert deployment.hub is not None
    await deployment.hub.aclose()


def test_registry_without_enabled_sources_stays_disabled_before_runtime_allocation(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry.json"
    _registry(registry, enabled=False)

    deployment = build_external_world_perception_deployment(
        settings=Settings(
            _env_file=None,
            DATABASE_PATH=tmp_path / "world.sqlite",
            WORLD_V2_EXTERNAL_PERCEPTION_MODE="shadow",
            WORLD_V2_EXTERNAL_PERCEPTION_SOURCE_REGISTRY_PATH=registry,
            WORLD_V2_EXTERNAL_PERCEPTION_SIDECAR_PATH=tmp_path / "sidecar.sqlite",
        ),
        world_id="world:test",
        actor_ref="agent:companion",
        model=Model(),
        life=Life(),
    )

    assert (deployment.status, deployment.reason, deployment.hub) == (
        "disabled",
        "no_enabled_sources",
        None,
    )


@pytest.mark.asyncio
async def test_shadow_can_run_acquisition_without_gaining_v2_authority(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    _registry(registry)
    deployment = build_external_world_perception_deployment(
        settings=Settings(
            _env_file=None,
            DATABASE_PATH=tmp_path / "world.sqlite",
            WORLD_V2_EXTERNAL_PERCEPTION_MODE="shadow",
            WORLD_V2_EXTERNAL_PERCEPTION_SOURCE_REGISTRY_PATH=registry,
            WORLD_V2_EXTERNAL_PERCEPTION_SIDECAR_PATH=tmp_path / "sidecar.sqlite",
        ),
        world_id="world:test",
        actor_ref="agent:companion",
        model=Model(),
        life=Life(),
        wall_clock=lambda: NOW,
    )

    assert deployment.status == "ready"
    assert deployment.hub is not None
    assert deployment.hub.health_snapshot().shadow_attention.state == "disabled"
    await deployment.hub.aclose()


@pytest.mark.asyncio
async def test_settled_authorized_search_can_join_production_hub_without_external_registry(
    tmp_path: Path,
) -> None:
    deployment = build_external_world_perception_deployment(
        settings=Settings(
            _env_file=None,
            DATABASE_PATH=tmp_path / "world.sqlite",
            WORLD_V2_EXTERNAL_PERCEPTION_MODE="shadow",
            WORLD_V2_EXTERNAL_PERCEPTION_SIDECAR_PATH=tmp_path / "sidecar.sqlite",
        ),
        world_id="world:test",
        actor_ref="agent:companion",
        model=Model(),
        life=Life(),
        authorized_search_profile=_authorized_search_profile(),
    )

    assert deployment.status == "ready"
    assert deployment.registry_revision is None
    assert deployment.hub is not None
    await deployment.hub.aclose()
