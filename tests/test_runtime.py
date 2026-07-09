from companion_daemon.runtime import build_companion_engine


def test_runtime_builds_engine_with_fake_model() -> None:
    engine = build_companion_engine(use_fake_model=True)

    assert engine.companion_system_prompt
