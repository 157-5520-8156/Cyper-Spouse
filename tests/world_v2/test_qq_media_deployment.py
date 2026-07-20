"""The production media factory fails safe and composes only when complete."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from companion_daemon.config import Settings
from companion_daemon.world_v2.media_authority_provisioning import MediaAuthorityProvisioner
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.qq_c2c_onebot_app import create_qq_c2c_onebot_app
from companion_daemon.world_v2.qq_media_deployment import (
    build_qq_media_preview_deployment,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
WORLD_ID = "world:qq-media-deployment"


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return (f"user:{platform_user_id}", "user:user.1")


class _NoModel:
    async def deliberate(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("factory test does not deliberate")


class _Router:
    async def route(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("factory test does not route")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("factory test does not dispatch")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


async def _provisioned_world(path: Path) -> None:
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=WorldV2TurnApplicationConfig(
            world_id=WORLD_ID,
            companion_actor_ref="agent:companion",
            reply_target="user:user.1",
            action_pump_owner="pump:qq-media-deployment",
        ),
        identities=_Identities(), router=_Router(), main_model=_NoModel(),
        quick_recovery=_NoModel(), transport=_Transport(), now=NOW,
    )
    try:
        await app.tick(
            tick_id="deployment:1", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1), trace_id="trace:deployment",
            causation_id="cause:deployment", correlation_id="correlation:deployment",
            reason="test",
        )
    finally:
        app.close()
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    try:
        MediaAuthorityProvisioner(
            ledger=ledger, signing_key_hex="11" * 32, subject_ref="user:user.1",
        ).ensure()
    finally:
        ledger.close()


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_path": tmp_path / "qq-media-deployment.sqlite",
        "WORLD_V2_MEDIA_PREVIEW_ENABLED": "1",
        "DEEPSEEK_API_KEY": "test-deepseek",
        "OPENAI_API_KEY": "test-openai",
        "NAPCAT_ALLOWED_PRIVATE_USER_IDS": "10001",
        "PRIMARY_USER_ID": "geoff",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_factory_disables_without_the_explicit_switch(tmp_path: Path) -> None:
    settings = _settings(tmp_path, WORLD_V2_MEDIA_PREVIEW_ENABLED="0")
    assert build_qq_media_preview_deployment(settings=settings, world_id=WORLD_ID) is None


def test_factory_disables_without_credentials(tmp_path: Path) -> None:
    assert (
        build_qq_media_preview_deployment(
            settings=_settings(tmp_path, OPENAI_API_KEY=None), world_id=WORLD_ID
        )
        is None
    )
    assert (
        build_qq_media_preview_deployment(
            settings=_settings(tmp_path, DEEPSEEK_API_KEY=None), world_id=WORLD_ID
        )
        is None
    )


def test_factory_disables_until_enforcement_grants_are_provisioned(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert build_qq_media_preview_deployment(settings=settings, world_id=WORLD_ID) is None


@pytest.mark.asyncio
async def test_inspector_hardening_normalizes_list_fields_without_touching_verdicts() -> None:
    import json

    import httpx

    from companion_daemon.world_v2.qq_media_deployment import InspectorHardeningTransport

    content = {
        "passed": False,
        "reason": "subject missing",
        "observed_summary": "a park path",
        "observed_facts": {"environment": "park", "lighting": "golden hour"},
        "deviations": "no subject present",
        "salient_expression_cues": None,
    }

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    transport = InspectorHardeningTransport(
        proxy_url=None, inner=httpx.MockTransport(respond)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post("https://provider.test/chat/completions", json={})
    payload = json.loads(response.json()["choices"][0]["message"]["content"])
    assert payload["observed_facts"] == ["environment: park", "lighting: golden hour"]
    assert payload["deviations"] == ["no subject present"]
    # An explicit JSON null would crash the parser's slice; it becomes [].
    assert payload["salient_expression_cues"] == []
    # Verdict material is never rewritten by deployment hardening.
    assert payload["passed"] is False
    assert payload["reason"] == "subject missing"
    await transport.aclose()


def test_media_observation_surface_is_token_gated_and_read_only(tmp_path: Path) -> None:
    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            database_path=tmp_path / "qq-media-endpoints.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
            DELIVERY_RECONCILIATION_TOKEN="operator-secret",
        ),
        use_fake_model=True,
    )
    with TestClient(app) as client:
        # Missing/wrong token cannot read what she generated or sent.
        assert client.get("/internal/world-v2/media/previews").status_code == 403
        assert (
            client.get(
                "/internal/world-v2/media/previews",
                headers={"X-World-V2-Internal-Token": "wrong"},
            ).status_code
            == 403
        )
        listed = client.get(
            "/internal/world-v2/media/previews",
            headers={"X-World-V2-Internal-Token": "operator-secret"},
        )
        assert listed.status_code == 200
        assert listed.json() == {"previews": []}
        # There is no approval verb anywhere: delivery is the world's own
        # decision, so the old approve/dismiss routes must not exist.
        for verb in ("approve", "dismiss"):
            response = client.post(
                f"/internal/world-v2/media/previews/preview:x/{verb}",
                headers={"X-World-V2-Internal-Token": "operator-secret"},
            )
            assert response.status_code in {404, 405}


def test_media_observation_surface_disabled_without_a_token(tmp_path: Path) -> None:
    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            database_path=tmp_path / "qq-media-endpoints-disabled.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
            DELIVERY_RECONCILIATION_TOKEN=None,
        ),
        use_fake_model=True,
    )
    with TestClient(app) as client:
        response = client.get("/internal/world-v2/media/previews")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_factory_composes_a_complete_preview_deployment_when_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    settings = _settings(tmp_path)
    await _provisioned_world(Path(settings.database_path))
    bundle = build_qq_media_preview_deployment(settings=settings, world_id=WORLD_ID)
    assert bundle is not None
    try:
        deployment = bundle.deployment
        assert deployment.continuation is not None
        assert deployment.acceptance.grant.grant_id == "grant:world-v2:media-planning"
        assert deployment.continuation.render_grant.grant_id == "grant:world-v2:media-render"
        assert (
            deployment.continuation.inspection_grant.grant_id
            == "grant:world-v2:media-inspection"
        )
        # Zero render/inspection reservations keep the lane free of the paid
        # CostProfile requirement while budgets still bootstrap.
        assert deployment.continuation.render_amount_limit == 0
        assert deployment.continuation.inspection_amount_limit == 0
        # World-owned delivery with conservative operational guardrails; the
        # decision authority is a system policy ref, never a human operator.
        assert deployment.auto_delivery is not None
        assert deployment.auto_delivery.delivery_target_ref == "conversation:qq:c2c:10001"
        assert deployment.auto_delivery.recipient_ref == "user:geoff"
        assert deployment.auto_delivery.policy_actor.startswith("system:")
        assert deployment.auto_delivery.max_deliveries_per_day <= 2
        assert bundle.transport.provider == "provider:event-media"
        assert hasattr(bundle.transport, "lookup_execution_result")
    finally:
        bundle.transport.close()
