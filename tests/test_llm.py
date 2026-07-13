import asyncio

import httpx
import pytest

from companion_daemon.llm import (
    DeepSeekChatModel,
    ModelCircuitOpenError,
    ProviderCircuitBreaker,
    complete_with_timeout,
    model_call_scope,
    model_turn_scope,
)


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


def test_deepseek_json_payload_requests_one_object() -> None:
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
    )

    payload = model.request_payload(
        [{"role": "user", "content": "hi"}],
        temperature=0.55,
        json_object=True,
    )

    assert payload["response_format"] == {"type": "json_object"}


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
async def test_model_usage_is_correlated_to_the_frozen_world_turn() -> None:
    captured = []
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        ),
        usage_observer=captured.append,
    )

    with model_turn_scope(
        world_id="world-1", turn_id="turn-1", cadence="hot"
    ), model_call_scope("reply", action_id="model-call-1", attempt=2):
        await model.complete([{"role": "user", "content": "hi"}])

    usage = captured[0]
    assert usage.world_id == "world-1"
    assert usage.turn_id == "turn-1"
    assert usage.action_id == "model-call-1"
    assert usage.cadence == "hot"
    assert usage.attempt == 2


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


@pytest.mark.asyncio
async def test_provider_circuit_breaker_skips_repeated_wait_and_recovers() -> None:
    now = [0.0]
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ReadTimeout("provider stalled")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "恢复了"}}]}
        )

    breaker = ProviderCircuitBreaker(
        failure_threshold=1,
        cooldown_seconds=30,
        clock=lambda: now[0],
    )
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(handler),
        circuit_breaker=breaker,
    )

    with pytest.raises(httpx.ReadTimeout):
        await model.complete([{"role": "user", "content": "第一次"}])
    with pytest.raises(ModelCircuitOpenError):
        await model.complete([{"role": "user", "content": "第二次"}])
    assert requests == 1

    now[0] = 31.0
    assert await model.complete([{"role": "user", "content": "探测"}]) == "恢复了"
    assert requests == 2


@pytest.mark.asyncio
async def test_deepseek_model_reuses_injected_http_client_until_closed() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": f"reply-{requests}"}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        client=client,
    )

    assert await model.complete([{"role": "user", "content": "one"}]) == "reply-1"
    assert await model.complete([{"role": "user", "content": "two"}]) == "reply-2"
    assert requests == 2

    await model.aclose()
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_model_timeout_opens_provider_circuit_but_caller_cancellation_does_not() -> None:
    blocker = asyncio.Event()
    captured = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        await blocker.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    breaker = ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(handler),
        usage_observer=captured.append,
        circuit_breaker=breaker,
    )

    with pytest.raises(TimeoutError):
        await complete_with_timeout(
            model.complete([{"role": "user", "content": "timeout"}]),
            timeout_seconds=0.01,
        )
    assert breaker.snapshot().status == "open"
    assert captured[-1].error == "provider_timeout"

    caller_breaker = ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=0)
    caller_breaker.record_failure()
    caller_model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(handler),
        usage_observer=captured.append,
        circuit_breaker=caller_breaker,
    )
    task = asyncio.create_task(
        complete_with_timeout(
            caller_model.complete([{"role": "user", "content": "cancel"}]),
            timeout_seconds=30,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert caller_breaker.snapshot().status == "half_open"
    assert captured[-1].error == "caller_cancelled"
    blocker.set()
    assert await caller_model.complete([{"role": "user", "content": "probe"}]) == "ok"
    assert caller_breaker.snapshot().status == "closed"

    await model.aclose()
    await caller_model.aclose()


def test_provider_circuit_snapshot_exposes_open_and_half_open_policy_states() -> None:
    now = [0.0]
    breaker = ProviderCircuitBreaker(
        failure_threshold=1,
        cooldown_seconds=30,
        clock=lambda: now[0],
    )

    breaker.record_failure()
    assert breaker.snapshot().status == "open"
    now[0] = 31.0
    assert breaker.snapshot().status == "half_open"


@pytest.mark.asyncio
async def test_schema_and_client_rejections_do_not_trip_provider_outage_circuit() -> None:
    breaker = ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    malformed = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": []})
        ),
        circuit_breaker=breaker,
    )

    with pytest.raises(ValueError):
        await malformed.complete([{"role": "user", "content": "schema"}])
    assert breaker.snapshot().status == "closed"
    await malformed.aclose()

    rejected = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"error": "bad request"})
        ),
        circuit_breaker=breaker,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await rejected.complete([{"role": "user", "content": "bad request"}])
    assert breaker.snapshot().status == "closed"
    await rejected.aclose()


@pytest.mark.asyncio
async def test_provider_server_error_trips_outage_circuit() -> None:
    breaker = ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=30)
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"error": "unavailable"})
        ),
        circuit_breaker=breaker,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await model.complete([{"role": "user", "content": "outage"}])
    assert breaker.snapshot().status == "open"
    await model.aclose()


@pytest.mark.asyncio
async def test_timeout_remains_hard_when_child_ignores_cancellation() -> None:
    release = asyncio.Event()

    async def stubborn() -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return "late"

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await complete_with_timeout(
            stubborn(), timeout_seconds=0.01, cancellation_grace_seconds=0.01
        )
    assert asyncio.get_running_loop().time() - started < 0.2
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_swallowed_by_stubborn_child() -> None:
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    wrapper = asyncio.create_task(
        complete_with_timeout(
            stubborn(), timeout_seconds=30, cancellation_grace_seconds=0.01
        )
    )
    await asyncio.sleep(0)
    wrapper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(wrapper, timeout=0.2)
    release.set()
    await asyncio.sleep(0)
