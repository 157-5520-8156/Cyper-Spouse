from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import companion_daemon.app as app_module
from companion_daemon.config import Settings
from companion_daemon.world_v2.http_capture_host import HttpV2CaptureHost
from companion_daemon.world_v2.qq_c2c_host import QQC2CHost
from companion_daemon.world_v2.semantic_chat_composition import (
    unavailable_life_source_authority_health,
)


EXPECTED_UNAVAILABLE_LIFE_SOURCE_AUTHORITY_HEALTH = {
    "status": "unavailable",
    "warning": True,
    "warning_reasons": ["life_source_authority.composition_unavailable"],
    "runtime_isolated": False,
    "runtime_isolation": "unavailable",
    "reviewer_model": None,
    "contracts": {
        "life-development-source-closure-review.1": {
            "schema_installed": False,
            "parser_fail_closed": True,
            "release_qualified": False,
        },
        "life-development-novel-origin-review.2": {
            "schema_installed": False,
            "parser_fail_closed": True,
            "release_qualified": False,
        },
    },
    "last_transport_winner": None,
    "route_suppression": {},
    "transport_runtime": None,
}


def test_unavailable_life_source_authority_health_is_stable_and_fresh() -> None:
    first = unavailable_life_source_authority_health()

    assert first == EXPECTED_UNAVAILABLE_LIFE_SOURCE_AUTHORITY_HEALTH

    first["warning_reasons"] = []
    assert (
        unavailable_life_source_authority_health()
        == EXPECTED_UNAVAILABLE_LIFE_SOURCE_AUTHORITY_HEALTH
    )


def test_http_and_qq_hosts_expose_the_same_unavailable_life_health() -> None:
    platform = SimpleNamespace(close=lambda: None)
    http_host = HttpV2CaptureHost(
        host=platform,  # type: ignore[arg-type]
        transport=object(),  # type: ignore[arg-type]
        primary_user_id="geoff",
    )
    qq_host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
    )

    assert http_host.life_source_authority_health() == (
        EXPECTED_UNAVAILABLE_LIFE_SOURCE_AUTHORITY_HEALTH
    )
    assert qq_host.life_source_authority_health() == (
        EXPECTED_UNAVAILABLE_LIFE_SOURCE_AUTHORITY_HEALTH
    )


def test_top_level_health_uses_the_same_legacy_capture_fallback(
    tmp_path: Path,
) -> None:
    class _LegacyCapture:
        def proactive_source_authority_health(self) -> dict[str, object]:
            return {"status": "ready"}

        async def aclose(self) -> None:
            return None

    configured = app_module.create_http_asgi_app(
        settings=Settings(database_path=tmp_path / "life-health.sqlite")
    )
    configured.state.http_v2_capture = _LegacyCapture()

    with TestClient(configured) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["life_source_authority"] == (
        EXPECTED_UNAVAILABLE_LIFE_SOURCE_AUTHORITY_HEALTH
    )
