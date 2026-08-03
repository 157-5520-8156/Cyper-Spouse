from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.external_world_perception import (
    PerceptionAdvanceResult,
)
from companion_daemon.world_v2.qq_c2c_host import QQC2CHost


NOW = datetime(2026, 8, 3, 5, 0, tzinfo=UTC)


class _PerceptionHub:
    def __init__(self, result: PerceptionAdvanceResult) -> None:
        self.result = result
        self.observed_at: list[datetime] = []
        self.closed = False

    async def advance_once(self, *, observed_at: datetime) -> PerceptionAdvanceResult:
        self.observed_at.append(observed_at)
        return self.result

    def health_snapshot(self) -> dict[str, object]:
        return {"state": "healthy", "mode": "shadow"}

    async def aclose(self) -> None:
        self.closed = True


class _FailingPerceptionHub(_PerceptionHub):
    async def advance_once(self, *, observed_at: datetime) -> PerceptionAdvanceResult:
        self.observed_at.append(observed_at)
        raise RuntimeError("sidecar unavailable")

    def health_snapshot(self) -> dict[str, object]:
        raise RuntimeError("sidecar unavailable")


class _PlatformHost:
    def __init__(self) -> None:
        self.background_calls = 0
        self.scheduled_background_budget: int | None = None

    async def current_logical_time(self) -> datetime:
        return NOW

    async def tick(self, _tick: object) -> SimpleNamespace:
        return SimpleNamespace(status="observed_only", authorized_action_ids=())

    async def drain_background_once(self) -> None:
        self.background_calls += 1
        return None

    async def drain_scheduled_work(self, **kwargs: object) -> SimpleNamespace:
        self.scheduled_background_budget = int(kwargs["max_background_units"])
        return SimpleNamespace(action_statuses=(), background_statuses=())

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_scheduler_gives_one_background_unit_to_external_perception() -> None:
    perception = _PerceptionHub(PerceptionAdvanceResult(status="progressed", progressed_units=1))
    platform = _PlatformHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        external_world_perception_hub=perception,
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=600,
    )
    try:
        result = await host.scheduler_once(
            observed_at=NOW + timedelta(seconds=30),
            max_action_units=0,
            max_background_units=1,
        )
    finally:
        await host.aclose()

    assert perception.observed_at == [NOW + timedelta(seconds=30)]
    assert result.background_statuses == ("external-perception:progressed",)
    assert platform.background_calls == 0
    assert platform.scheduled_background_budget == 0
    assert perception.closed is True


@pytest.mark.asyncio
async def test_zero_background_budget_does_not_advance_external_perception() -> None:
    perception = _PerceptionHub(PerceptionAdvanceResult(status="idle", progressed_units=0))
    platform = _PlatformHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        external_world_perception_hub=perception,
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=600,
    )
    try:
        await host.scheduler_once(
            observed_at=NOW + timedelta(seconds=30),
            max_action_units=0,
            max_background_units=0,
        )
        health = host.external_world_perception_health()
    finally:
        await host.aclose()

    assert perception.observed_at == []
    assert health == {"enabled": True, "state": "healthy", "mode": "shadow"}


def test_unconfigured_external_perception_health_is_explicitly_disabled() -> None:
    host = QQC2CHost(
        host=_PlatformHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_now=lambda: NOW,
    )

    assert host.external_world_perception_health() == {
        "enabled": False,
        "state": "disabled",
        "reason": "not_configured",
    }


def test_external_perception_health_includes_static_registry_coverage() -> None:
    registry_health = {
        "registry_revision": "registry:test:1",
        "registry_content_hash": "sha256:" + "a" * 64,
        "registered_source_count": 2,
        "enabled_source_count": 1,
        "coverage_states": [
            {
                "source_id": "cn.social.weibo.trends.tophub.v1",
                "route_registered": True,
                "acquisition_state": "disabled_unlicensed",
                "character_visibility": False,
                "reason_code": "usage_rights_not_approved",
            }
        ],
    }
    host = QQC2CHost(
        host=_PlatformHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        external_world_perception_registry_health=registry_health,
        ingress_now=lambda: NOW,
    )

    assert host.external_world_perception_health() == {
        "enabled": False,
        "state": "disabled",
        "reason": "not_configured",
        "registry": registry_health,
    }


@pytest.mark.asyncio
async def test_external_perception_failure_does_not_block_other_scheduler_work() -> None:
    perception = _FailingPerceptionHub(PerceptionAdvanceResult(status="idle", progressed_units=0))
    platform = _PlatformHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        external_world_perception_hub=perception,
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=600,
    )
    try:
        result = await host.scheduler_once(
            observed_at=NOW + timedelta(seconds=30),
            max_action_units=0,
            max_background_units=1,
        )
        health = host.external_world_perception_health()
    finally:
        await host.aclose()

    assert result.background_statuses == ("external-perception:technical-failure",)
    assert platform.scheduled_background_budget == 0
    assert health == {
        "enabled": True,
        "state": "degraded",
        "warning_reasons": ["health_read_failed"],
    }


def test_failed_hub_health_does_not_hide_static_registry_coverage() -> None:
    registry_health = {
        "registry_revision": "registry:test:1",
        "coverage_states": [
            {
                "source_id": "cn.social.xiaohongshu.general.v1",
                "acquisition_state": "unsupported",
                "character_visibility": False,
            }
        ],
    }
    host = QQC2CHost(
        host=_PlatformHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        external_world_perception_hub=_FailingPerceptionHub(
            PerceptionAdvanceResult(status="idle", progressed_units=0)
        ),
        external_world_perception_registry_health=registry_health,
        ingress_now=lambda: NOW,
    )

    assert host.external_world_perception_health() == {
        "enabled": True,
        "state": "degraded",
        "warning_reasons": ["health_read_failed"],
        "registry": registry_health,
    }
