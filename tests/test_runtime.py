from companion_daemon.config import Settings
from companion_daemon.runtime import build_companion_engine


def test_runtime_builds_engine_with_fake_model() -> None:
    engine = build_companion_engine(use_fake_model=True)

    assert engine.companion_system_prompt


def test_daemon_prompt_core_is_default_without_env() -> None:
    settings = Settings(_env_file=None)

    assert settings.conversation_core == "prompt"
    assert settings.deepseek_model == "deepseek-v4-pro"
    assert settings.deepseek_thinking_enabled is True
