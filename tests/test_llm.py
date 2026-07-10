from companion_daemon.llm import DeepSeekChatModel


def test_deepseek_thinking_payload_uses_v4_controls_without_temperature() -> None:
    model = DeepSeekChatModel("key", "https://api.deepseek.com", "deepseek-v4-pro")

    payload = model.request_payload([{"role": "user", "content": "hi"}], temperature=0.75)

    assert payload["model"] == "deepseek-v4-pro"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload


def test_deepseek_nonthinking_payload_keeps_temperature() -> None:
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-pro",
        thinking_enabled=False,
    )

    payload = model.request_payload([{"role": "user", "content": "hi"}], temperature=0.55)

    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0.55
