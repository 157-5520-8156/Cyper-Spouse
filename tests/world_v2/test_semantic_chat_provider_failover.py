import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import FailoverChatModel, OpenAICompatibleChatModel
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)


@pytest.mark.asyncio
async def test_world_v2_composition_installs_configured_openai_fallback() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        OPENAI_API_KEY="openai-test-key",
        OPENAI_PROXY_URL="http://127.0.0.1:7890",
        WORLD_V2_FALLBACK_MODEL="gpt-5.6-luna-test",
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert isinstance(composition.flash_model, FailoverChatModel)
    assert isinstance(composition.flash_model.fallback, OpenAICompatibleChatModel)
    assert composition.flash_model.fallback.model == "gpt-5.6-luna-test"
    assert composition.flash_model.fallback.proxy_url == "http://127.0.0.1:7890"
    await composition.aclose()


def test_world_v2_fallback_model_defaults_to_official_high_volume_tier() -> None:
    settings = Settings(_env_file=None)

    assert settings.world_v2_fallback_model == "gpt-5.6-luna"
