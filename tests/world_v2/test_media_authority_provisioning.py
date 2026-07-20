"""Operator provisioning writes a replayable, verifiable enforcement chain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.world_v2.media_authority_provisioning import (
    MEDIA_PLANNING_GRANT_ID,
    MediaAuthorityProvisioner,
)
from companion_daemon.world_v2.media_provider_grants import require_provider_media_grant
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.schemas import Action, ProviderMediaGrantBinding
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
TEST_ROOT_SEED = "11" * 32


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return (f"user:{platform_user_id}", "user:user.1")


class _NoModel:
    async def deliberate(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("provisioning test must not deliberate")


class _Router:
    async def route(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("provisioning test must not route")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("provisioning test must not dispatch")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


async def _clocked_world(path: Path) -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:media-authority",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:media-authority",
    )
    app = build_sqlite_world_v2_turn_application(
        path=path, config=config, identities=_Identities(), router=_Router(),
        main_model=_NoModel(), quick_recovery=_NoModel(), transport=_Transport(), now=NOW,
    )
    try:
        await app.tick(
            tick_id="media-authority:1",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:media-authority",
            causation_id="cause:media-authority",
            correlation_id="correlation:media-authority",
            reason="test",
        )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_provisioner_writes_idempotent_dispatchable_enforcement_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    path = tmp_path / "media-authority.sqlite"
    await _clocked_world(path)
    ledger = SQLiteWorldLedger(path=path, world_id="world:media-authority")
    try:
        first = MediaAuthorityProvisioner(
            ledger=ledger, signing_key_hex=TEST_ROOT_SEED, subject_ref="user:user.1",
        ).ensure()
        rerun = MediaAuthorityProvisioner(
            ledger=ledger, signing_key_hex=TEST_ROOT_SEED, subject_ref="user:user.1",
        ).ensure()
        projection = ledger.project()
    finally:
        ledger.close()

    assert len(first.committed_event_ids) == 13
    assert rerun.committed_event_ids == ()
    assert len(rerun.already_present) == 13
    assert {item.grant_id for item in projection.provider_media_grants} == {
        "grant:world-v2:media-planning",
        "grant:world-v2:media-render",
        "grant:world-v2:media-inspection",
        "grant:world-v2:media-repair",
    }

    # The exact ActionPump enforcement check accepts each provisioned grant.
    for kind, actor, target, grant_id in (
        (
            "media_planning",
            "worker:world-v2:media-selection-acceptance",
            "provider:media-planner",
            MEDIA_PLANNING_GRANT_ID,
        ),
        (
            "media_render",
            "worker:world-v2:media-continuation",
            "provider:media-renderer",
            "grant:world-v2:media-render",
        ),
        (
            "media_inspection",
            "worker:world-v2:media-continuation",
            "provider:media-inspector",
            "grant:world-v2:media-inspection",
        ),
        (
            "media_repair",
            "worker:world-v2:media-continuation",
            "provider:media-renderer",
            "grant:world-v2:media-repair",
        ),
    ):
        action = Action.model_construct(
            schema_version="world-v2.1",
            action_id=f"action:test:{kind}",
            world_id="world:media-authority",
            logical_time=projection.logical_time,
            created_at=projection.logical_time,
            trace_id="trace:test",
            causation_id="cause:test",
            correlation_id="correlation:test",
            kind=kind,
            layer="media_action",
            intent_ref="intent:test",
            actor=actor,
            target=target,
            payload_ref="payload:test",
            payload_hash="hash:test",
            provider_media_grant=ProviderMediaGrantBinding(
                grant_id=grant_id, grant_revision=1
            ),
            idempotency_key=f"key:{kind}",
            budget_reservation_id="reservation:test",
            state="authorized",
            recovery_policy="effect_once",
        )
        grant = require_provider_media_grant(
            action=action, projection=projection, logical_time=projection.logical_time
        )
        assert grant.grant_id == grant_id


def test_provisioner_rejects_a_key_outside_the_installed_root_set(tmp_path: Path) -> None:
    class _Ledger:
        world_id = "world:unused"

    with pytest.raises(ValueError, match="installed deployment root"):
        MediaAuthorityProvisioner(
            ledger=_Ledger(), signing_key_hex="22" * 32, subject_ref="user:user.1",
        )
