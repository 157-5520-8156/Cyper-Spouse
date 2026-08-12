from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "run_world_v2_media_qq_acceptance.py"
)


def _load_script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("media_qq_acceptance_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


media_qq_acceptance = _load_script()


def _authorized_args(recipient: str = "10001"):  # type: ignore[no-untyped-def]
    return media_qq_acceptance._parse_args(
        [
            "--recipient",
            recipient,
            "--consent-recipient",
            recipient,
            "--confirm-send",
            f"SEND_ONE_MEDIA_TO_QQ_{recipient}",
        ]
    )


def _settings(
    *, recipient: str = "10001", allowed: str | None = None, adapter: str = "napcat"
):
    return SimpleNamespace(
        qq_adapter=adapter,
        napcat_allowed_private_user_ids=allowed if allowed is not None else recipient,
        deepseek_api_key="deepseek-test-key",
        openai_api_key="openai-test-key",
    )


def test_default_refusal_happens_before_scratch_allocation() -> None:
    scratch_called = False

    def fail_if_called() -> Path:
        nonlocal scratch_called
        scratch_called = True
        raise AssertionError("unauthorized harness allocated scratch state")

    exit_code = asyncio.run(
        media_qq_acceptance.main(
            argv=[],
            environ={},
            scratch_factory=fail_if_called,
        )
    )

    assert exit_code == 2
    assert scratch_called is False


@pytest.mark.parametrize(
    ("argv", "environ", "reason_code"),
    (
        ([], {}, "real_qq_acceptance_switch_disabled"),
        (
            [
                "--recipient",
                "10001",
                "--consent-recipient",
                "10002",
                "--confirm-send",
                "SEND_ONE_MEDIA_TO_QQ_10001",
            ],
            {"WORLD_V2_REAL_MEDIA_QQ_ACCEPTANCE": "1"},
            "recipient_consent_mismatch",
        ),
        (
            [
                "--recipient",
                "10001",
                "--consent-recipient",
                "10001",
                "--confirm-send",
                "send it",
            ],
            {"WORLD_V2_REAL_MEDIA_QQ_ACCEPTANCE": "1"},
            "single_send_confirmation_mismatch",
        ),
    ),
)
def test_cli_authorization_fails_closed(
    argv: list[str], environ: dict[str, str], reason_code: str
) -> None:
    decision = media_qq_acceptance._authorize_cli(
        media_qq_acceptance._parse_args(argv), environ=environ
    )

    assert decision.authorized is False
    assert reason_code in decision.reason_codes


def test_settings_authorization_requires_exact_single_allowlisted_recipient() -> None:
    args = _authorized_args()
    cli = media_qq_acceptance._authorize_cli(
        args, environ={"WORLD_V2_REAL_MEDIA_QQ_ACCEPTANCE": "1"}
    )
    assert cli.authorized is True

    exact = media_qq_acceptance._authorize_settings(args, settings=_settings())
    extra = media_qq_acceptance._authorize_settings(
        args, settings=_settings(allowed="10001,10002")
    )
    official = media_qq_acceptance._authorize_settings(
        args, settings=_settings(adapter="official")
    )

    assert exact.authorized is True
    assert extra.authorized is False
    assert "configured_recipient_scope_mismatch" in extra.reason_codes
    assert official.authorized is False
    assert "qq_adapter_not_receipt_capable" in official.reason_codes


def test_cli_authorization_rejects_noncanonical_recipient_bytes() -> None:
    args = media_qq_acceptance._parse_args(
        [
            "--recipient",
            " 10001 ",
            "--consent-recipient",
            "10001",
            "--confirm-send",
            "SEND_ONE_MEDIA_TO_QQ_10001",
        ]
    )

    decision = media_qq_acceptance._authorize_cli(
        args,
        environ={"WORLD_V2_REAL_MEDIA_QQ_ACCEPTANCE": "1"},
    )

    assert decision.authorized is False
    assert "recipient_not_one_numeric_private_qq_id" in decision.reason_codes


def test_scratch_root_and_paths_are_private_and_bounded(tmp_path: Path) -> None:
    root = media_qq_acceptance._new_scratch_root(base_dir=tmp_path)
    database_path = media_qq_acceptance._scratch_path(root, "world/media.sqlite")

    assert root.parent == tmp_path
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert database_path == root / "world" / "media.sqlite"
    with pytest.raises(ValueError, match="outside"):
        media_qq_acceptance._require_scratch_path(tmp_path / "outside.sqlite", root)


def test_delivery_app_replaces_current_dataclass_config_coordinates(
    tmp_path: Path,
) -> None:
    from companion_daemon.world_v2.production_turn_application import (
        WorldV2TurnApplicationConfig,
    )

    captured: dict[str, object] = {}
    auto_delivery = object()
    original = WorldV2TurnApplicationConfig(
        world_id="world:media-preview-acceptance",
        companion_actor_ref="agent:companion",
        reply_target="conversation:old",
        action_pump_owner="pump:old",
    )

    class _Preview:
        class _Router:
            pass

        @staticmethod
        def _config(*, media_bundle: object) -> WorldV2TurnApplicationConfig:
            assert media_bundle is bundle
            return original

        @staticmethod
        def _new_character_interior(role_model: object) -> object:
            return role_model

        @staticmethod
        def build_sqlite_world_v2_turn_application(**kwargs: object) -> object:
            captured.update(kwargs)
            return SimpleNamespace(config=kwargs["config"])

    bundle = SimpleNamespace(
        deployment=SimpleNamespace(
            auto_delivery=auto_delivery,
            planner=object(),
        ),
        transport=object(),
    )

    app = media_qq_acceptance._build_delivery_app(
        preview=_Preview(),
        bundle=bundle,
        settings=object(),
        database_path=tmp_path / "media.sqlite",
        recipient_id="10001",
        role_model=object(),
        delivery=SimpleNamespace(),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert original.reply_target == "conversation:old"
    assert original.action_pump_owner == "pump:old"
    assert app.config.reply_target == "conversation:qq:c2c:10001"
    assert app.config.action_pump_owner == "pump:media-qq-acceptance"
    assert app.config.media_auto_delivery is auto_delivery
    assert captured["config"] is app.config


@pytest.mark.asyncio
async def test_single_image_delivery_rejects_wrong_target_non_media_and_second_send(
    tmp_path: Path,
) -> None:
    class _Delegate:
        def __init__(self) -> None:
            self.images: list[tuple[str, Path]] = []
            self.lookups: list[tuple[str, str]] = []

        async def send_image_message(
            self, recipient_id: str, *, image_path: Path
        ) -> dict[str, object]:
            self.images.append((recipient_id, image_path))
            return {"status": "ok", "data": {"message_id": "9001"}}

        async def get_message(
            self, recipient_id: str, *, message_id: str
        ) -> dict[str, object]:
            self.lookups.append((recipient_id, message_id))
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": message_id},
            }

    image = tmp_path / "one.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\none")
    delegate = _Delegate()
    delivery = media_qq_acceptance._SingleImageDelivery(
        delegate=delegate,
        recipient_id="10001",
    )

    with pytest.raises(RuntimeError, match="recipient"):
        await delivery.send_image_message("10002", image_path=image)
    with pytest.raises(RuntimeError, match="non-media"):
        await delivery.send_text("10001", "must not send")

    response = await delivery.send_image_message("10001", image_path=image)
    lookup = await delivery.get_message("10001", message_id="9001")

    assert response["status"] == "ok"
    assert lookup["data"]["message_id"] == "9001"
    assert delegate.images == [("10001", image)]
    assert delegate.lookups == [("10001", "9001")]
    with pytest.raises(RuntimeError, match="single-send"):
        await delivery.send_image_message("10001", image_path=image)
    assert len(delegate.images) == 1


def test_report_never_claims_production_qualification_or_discloses_qq_number(
    tmp_path: Path,
) -> None:
    report = media_qq_acceptance._base_report(
        scratch_root=tmp_path,
        recipient_id="10001",
    )
    encoded = json.dumps(report, sort_keys=True)

    assert report["production_qualified"] is False
    assert report["qualification_complete"] is False
    assert report["manual_only"] is True
    assert report["send_policy"]["max_image_sends"] == 1
    assert report["consent"]["explicit_recipient_match"] is True
    assert report["consent"]["durable_world_consent_event_for_delivery"] is False
    assert report["upstream_scope"]["normal_conversation_candidate_generation_qualified"] is False
    assert "10001" not in encoded
