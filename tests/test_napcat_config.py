import pytest
from pydantic import ValidationError

import companion_daemon.napcat_cli as napcat_cli
from companion_daemon.config import Settings
from companion_daemon.napcat_cli import _parse_id_list, _private_sender_is_allowed
from companion_daemon.qq_outbound_owner import QQOutboundConfigurationError


def test_napcat_settings_use_new_names() -> None:
    settings = Settings(NAPCAT_API_URL="http://127.0.0.1:3000", NAPCAT_ACCESS_TOKEN="secret")
    assert settings.napcat_api_url == "http://127.0.0.1:3000"
    assert settings.napcat_access_token == "secret"


def test_settings_reject_unknown_qq_adapter_before_any_process_starts() -> None:
    with pytest.raises(ValidationError, match="QQ_ADAPTER"):
        Settings(QQ_ADAPTER="auto")


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


def test_napcat_private_message_allowlist() -> None:
    settings = Settings(NAPCAT_ALLOWED_PRIVATE_USER_IDS="123, 456")
    allowed_ids = _parse_id_list(settings.napcat_allowed_private_user_ids)
    assert _private_sender_is_allowed("123", allowed_ids)
    assert _private_sender_is_allowed("456", allowed_ids)
    assert not _private_sender_is_allowed("789", allowed_ids)
    assert _private_sender_is_allowed("789", set())


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
