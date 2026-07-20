"""Operator provisioning writes a chain the perception resolver accepts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.world_v2.perception_authority_provisioning import (
    PERCEPTION_CONSENT_ID,
    PERCEPTION_PRIVACY_POLICY_ID,
    PERCEPTION_VISION_CAPABILITY_ID,
    PerceptionAuthorityProvisioner,
)
from companion_daemon.world_v2.perception_authorization_resolver import (
    ProjectionPerceptionAuthorizationResolver,
)
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
WORLD_ID = "world:perception-authority"


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return (f"user:{platform_user_id}", "user:user.1")


class _NoModel:
    async def deliberate(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("provisioning test does not deliberate")


class _Router:
    async def route(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("provisioning test does not route")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("provisioning test does not dispatch")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


async def _world_with_clock(path: Path) -> None:
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=WorldV2TurnApplicationConfig(
            world_id=WORLD_ID,
            companion_actor_ref="agent:companion",
            reply_target="user:user.1",
            action_pump_owner="pump:perception-authority",
        ),
        identities=_Identities(), router=_Router(), main_model=_NoModel(),
        quick_recovery=_NoModel(), transport=_Transport(), now=NOW,
    )
    try:
        await app.tick(
            tick_id="perception-authority:1", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1), trace_id="trace:perception-authority",
            causation_id="cause:perception-authority",
            correlation_id="correlation:perception-authority",
            reason="test",
        )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_provisioned_chain_satisfies_the_fail_closed_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    path = tmp_path / "perception-authority.sqlite"
    await _world_with_clock(path)
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    try:
        first = PerceptionAuthorityProvisioner(
            ledger=ledger, signing_key_hex="11" * 32, subject_ref="user:user.1",
        ).ensure()
        assert len(first.committed_event_ids) == 5
        assert first.already_present == ()

        projection = ledger.project()
        binding = ProjectionPerceptionAuthorizationResolver().resolve(
            projection=projection,
            actor_ref="agent:companion",
            subject_ref="user:user.1",
            target="perception:vision",
            logical_time=projection.logical_time,
        )
        assert binding.capability_grant_id == PERCEPTION_VISION_CAPABILITY_ID
        assert binding.consent_id == PERCEPTION_CONSENT_ID
        assert binding.privacy_policy_id == PERCEPTION_PRIVACY_POLICY_ID

        # Vision-only by design: transcription stays fail-closed.
        with pytest.raises(ValueError, match="missing or ambiguous"):
            ProjectionPerceptionAuthorizationResolver().resolve(
                projection=projection,
                actor_ref="agent:companion",
                subject_ref="user:user.1",
                target="perception:transcription",
                logical_time=projection.logical_time,
            )

        # A different actor or subject cannot ride the same chain.
        with pytest.raises(ValueError, match="missing or ambiguous"):
            ProjectionPerceptionAuthorizationResolver().resolve(
                projection=projection,
                actor_ref="agent:someone-else",
                subject_ref="user:user.1",
                target="perception:vision",
                logical_time=projection.logical_time,
            )

        rerun = PerceptionAuthorityProvisioner(
            ledger=ledger, signing_key_hex="11" * 32, subject_ref="user:user.1",
        ).ensure()
        assert rerun.committed_event_ids == ()
        assert len(rerun.already_present) == 5
    finally:
        ledger.close()


def test_unknown_root_key_is_rejected_before_any_write(tmp_path: Path) -> None:
    class _Ledger:
        world_id = WORLD_ID

        def project(self):  # pragma: no cover - never reached
            raise AssertionError("must fail before projecting")

    with pytest.raises(ValueError, match="installed deployment root"):
        PerceptionAuthorityProvisioner(
            ledger=_Ledger(), signing_key_hex="22" * 32, subject_ref="user:user.1",
        )
