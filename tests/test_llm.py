import httpx
import pytest

from companion_daemon.llm import DeepSeekChatModel, model_call_scope


def test_deepseek_thinking_payload_uses_v4_controls_without_temperature() -> None:
    model = DeepSeekChatModel("key", "https://api.deepseek.com", "deepseek-v4-flash")

    payload = model.request_payload([{"role": "user", "content": "hi"}], temperature=0.75)

    assert payload["model"] == "deepseek-v4-flash"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload


def test_deepseek_nonthinking_payload_keeps_temperature() -> None:
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
    )

    payload = model.request_payload([{"role": "user", "content": "hi"}], temperature=0.55)

    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0.55


@pytest.mark.asyncio
async def test_deepseek_completion_reports_real_usage_with_call_purpose() -> None:
    captured = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "你好。"}}],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 18,
                    "total_tokens": 138,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 40,
                    "completion_tokens_details": {"reasoning_tokens": 7},
                },
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
        usage_observer=captured.append,
    )

    with model_call_scope("reply_audit"):
        text = await model.complete([{"role": "user", "content": "你好"}])

    assert text == "你好。"
    assert len(captured) == 1
    usage = captured[0]
    assert usage.purpose == "reply_audit"
    assert usage.model == "deepseek-v4-flash"
    assert usage.status == "succeeded"
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 18
    assert usage.reasoning_tokens == 7
    assert usage.cache_hit_tokens == 80
    assert usage.cache_miss_tokens == 40
    assert usage.total_tokens == 138
    assert usage.latency_ms >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, 123, {"text": "hi"}, ""])
async def test_malformed_success_response_content_is_recorded_as_failed(
    content: object,
) -> None:
    captured = []
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "usage": {"total_tokens": 12},
                },
            )
        ),
        usage_observer=captured.append,
    )

    with pytest.raises(ValueError), model_call_scope("reply"):
        await model.complete([{"role": "user", "content": "hi"}])

    assert len(captured) == 1
    assert captured[0].status == "failed"
    assert captured[0].purpose == "reply"


@pytest.mark.asyncio
async def test_missing_choices_is_recorded_as_failed() -> None:
    captured = []
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": []})
        ),
        usage_observer=captured.append,
    )

    with pytest.raises(ValueError), model_call_scope("reply"):
        await model.complete([{"role": "user", "content": "hi"}])

    assert [item.status for item in captured] == ["failed"]
