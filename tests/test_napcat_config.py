from pathlib import Path

import pytest
from pydantic import ValidationError

import companion_daemon.napcat_cli as napcat_cli
from companion_daemon.config import Settings
from companion_daemon.napcat_cli import _parse_id_list
from companion_daemon.qq_outbound_owner import QQOutboundConfigurationError


def test_napcat_settings_use_new_names() -> None:
    settings = Settings(NAPCAT_API_URL="http://127.0.0.1:3000", NAPCAT_ACCESS_TOKEN="secret")
    assert settings.napcat_api_url == "http://127.0.0.1:3000"
    assert settings.napcat_access_token == "secret"


def test_settings_reject_unknown_qq_adapter_before_any_process_starts() -> None:
    with pytest.raises(ValidationError, match="QQ_ADAPTER"):
        Settings(QQ_ADAPTER="auto")


def test_production_settings_use_fast_expression_interface() -> None:
    settings = Settings(_env_file=None)

    assert settings.world_v2_expression_episode_mode == "stream"
    with pytest.raises(ValidationError, match="WORLD_V2_EXPRESSION_EPISODE_MODE"):
        Settings(_env_file=None, WORLD_V2_EXPRESSION_EPISODE_MODE="on")


def test_external_world_perception_is_fail_closed_until_a_source_registry_is_supplied() -> None:
    settings = Settings(_env_file=None)

    assert settings.world_v2_external_perception_mode == "off"
    assert settings.world_v2_external_perception_source_registry_path is None
    assert settings.world_v2_external_perception_user_location_enabled is False
    assert settings.world_v2_external_perception_sidecar_path == Path(
        "data/external-world-perception.sqlite"
    )

    configured = Settings(
        _env_file=None,
        WORLD_V2_EXTERNAL_PERCEPTION_MODE="shadow",
        WORLD_V2_EXTERNAL_PERCEPTION_SOURCE_REGISTRY_PATH="configs/perception-sources.json",
    )
    assert configured.world_v2_external_perception_mode == "shadow"
    assert configured.world_v2_external_perception_source_registry_path == Path(
        "configs/perception-sources.json"
    )

    with pytest.raises(ValidationError, match="WORLD_V2_EXTERNAL_PERCEPTION_MODE"):
        Settings(_env_file=None, WORLD_V2_EXTERNAL_PERCEPTION_MODE="enabled")
    with pytest.raises(
        ValidationError,
        match="WORLD_V2_EXTERNAL_PERCEPTION_USER_LOCATION_ENABLED",
    ):
        Settings(
            _env_file=None,
            WORLD_V2_EXTERNAL_PERCEPTION_USER_LOCATION_ENABLED=True,
        )


def test_text_endpoint_request_does_not_make_local_appraisal_mandatory() -> None:
    settings = Settings(
        _env_file=None,
        WORLD_V2_TEXT_ENDPOINT_ENABLED=True,
        LOCAL_APPRAISAL_ENABLED=False,
    )

    assert settings.world_v2_text_endpoint_enabled is True
    assert settings.local_appraisal_enabled is False


def test_napcat_settings_accept_legacy_snowluma_names() -> None:
    settings = Settings(SNOWLUMA_API_URL="http://127.0.0.1:5700", SNOWLUMA_ACCESS_TOKEN="legacy")
    assert settings.onebot_api_url == "http://127.0.0.1:5700"
    assert settings.onebot_access_token == "legacy"


def test_napcat_and_generic_onebot_have_separate_settings() -> None:
    settings = Settings(
        NAPCAT_API_URL="http://127.0.0.1:3000",
        ONEBOT_API_URL="http://127.0.0.1:5700",
        ONEBOT_PROACTIVE_USER_ID="123456789",
    )
    assert settings.napcat_api_url == "http://127.0.0.1:3000"
    assert settings.onebot_api_url == "http://127.0.0.1:5700"
    assert settings.onebot_proactive_user_id == "123456789"


def test_napcat_group_messages_are_opt_in() -> None:
    assert Settings().napcat_allow_group_messages is False
    assert Settings(NAPCAT_ALLOW_GROUP_MESSAGES="true").napcat_allow_group_messages is True


def test_napcat_private_allowlist_parses_comma_separated_ids() -> None:
    settings = Settings(NAPCAT_ALLOWED_PRIVATE_USER_IDS="123, 456")
    assert _parse_id_list(settings.napcat_allowed_private_user_ids) == {"123", "456"}
    assert _parse_id_list("") == set()


def test_napcat_process_refuses_to_start_when_another_qq_adapter_is_configured(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        napcat_cli,
        "get_settings",
        lambda: Settings(QQ_ADAPTER="official"),
    )

    with pytest.raises(QQOutboundConfigurationError, match="only the configured adapter"):
        napcat_cli.create_app(adapter="napcat", use_fake_model=True)


def test_archive_lane_selection_fails_fast_with_a_removal_error(monkeypatch) -> None:
    monkeypatch.setattr(
        napcat_cli,
        "get_settings",
        lambda: Settings(QQ_ADAPTER="napcat", NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001"),
    )

    with pytest.raises(RuntimeError, match="archived QQ Engine/coalescer lane was removed"):
        napcat_cli.create_app(adapter="napcat", use_fake_model=True, world_v2_c2c=False)


def test_napcat_run_script_defaults_to_hotter_batch_window() -> None:
    script = Path("scripts/run_napcat_adapter.sh").read_text()

    assert "QQ_MESSAGE_BATCH_SECONDS:=0.8" in script
