import asyncio
import json
import multiprocessing
from pathlib import Path

import httpx
import pytest

from companion_daemon.llm import (
    DeepSeekChatModel,
    FailoverChatModel,
    ModelCapacityBusyError,
    ModelCircuitOpenError,
    OpenAICompatibleChatModel,
    ProviderCapacityGate,
    ProviderCircuitBreaker,
    complete_with_timeout,
    model_call_scope,
    model_request_emission_scope,
    model_turn_scope,
)
from companion_daemon.world_v2.deliberation import ModelUsageProvenance


def _claim_stale_capacity_marker_in_process(
    marker_path: str,
    token: str,
    barrier: object,
    results: object,
) -> None:
    gate = ProviderCapacityGate(
        wall_clock=lambda: 1_000.0,
        marker_path=Path(marker_path),
    )
    barrier.wait()  # type: ignore[attr-defined]
    status = gate._claim_marker(token)  # noqa: SLF001
    results.put((token, status))  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 429, 500, 503])
async def test_provider_failure_falls_back_and_preserves_provider_attribution(
    status: int,
) -> None:
    captured = []
    primary = DeepSeekChatModel(
        "deepseek-key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, json={"error": "unavailable"})
        ),
        usage_observer=captured.append,
    )
    fallback = OpenAICompatibleChatModel(
        "openai-key",
        "https://api.openai.com/v1",
        "gpt-5.6-luna",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "备用回复"}}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
                },
            )
        ),
        usage_observer=captured.append,
    )
    model = FailoverChatModel(primary=primary, fallback=fallback)

    assert await model.complete([{"role": "user", "content": "你好"}]) == "备用回复"
    assert [(item.provider, item.model, item.status) for item in captured] == [
        ("deepseek", "deepseek-v4-flash", "failed"),
        ("openai", "gpt-5.6-luna", "succeeded"),
    ]
    assert model.last_provider == "openai"
    assert model.last_model == "gpt-5.6-luna"
    assert model.last_attempt_used_fallback is True
    await model.aclose()


@pytest.mark.asyncio
async def test_metered_failover_returns_usage_from_the_provider_that_answered() -> None:
    primary = DeepSeekChatModel(
        "deepseek-key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"error": "unavailable"})
        ),
    )
    fallback = OpenAICompatibleChatModel(
        "openai-key",
        "https://api.openai.com/v1",
        "gpt-5.6-luna",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "chatcmpl-openai-fallback-1",
                    "choices": [{"message": {"content": "备用回复"}}],
                    "usage": {
                        "prompt_tokens": 9,
                        "completion_tokens": 3,
                        "total_tokens": 12,
                    },
                },
            )
        ),
    )
    model = FailoverChatModel(primary=primary, fallback=fallback)

    text, usage_raw = await model.complete_with_usage(
        [{"role": "user", "content": "你好"}]
    )
    usage = ModelUsageProvenance.model_validate(usage_raw)

    assert text == "备用回复"
    assert usage.provider == "openai"
    assert usage.input_tokens == 9
    assert usage.output_tokens == 3
    assert model.last_provider == "openai"
    assert model.last_model == "gpt-5.6-luna"
    assert model.last_attempt_used_fallback is True
    await model.aclose()


@pytest.mark.asyncio
async def test_network_failure_falls_back_but_content_validation_failure_does_not() -> None:
    fallback_calls = 0

    def fallback_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal fallback_calls
        fallback_calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    fallback = OpenAICompatibleChatModel(
        "openai-key",
        "https://api.openai.com/v1",
        "gpt-5.6-luna",
        transport=httpx.MockTransport(fallback_handler),
    )
    offline = DeepSeekChatModel(
        "deepseek-key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(httpx.ConnectError("offline"))
        ),
    )
    assert await FailoverChatModel(primary=offline, fallback=fallback).complete(
        [{"role": "user", "content": "network"}]
    ) == "ok"

    malformed = DeepSeekChatModel(
        "deepseek-key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": []})
        ),
    )
    malformed_route = FailoverChatModel(primary=malformed, fallback=fallback)
    with pytest.raises(ValueError, match="choices"):
        await malformed_route.complete(
            [{"role": "user", "content": "schema"}]
        )
    assert fallback_calls == 1
    assert malformed_route.last_attempt_used_fallback is False
    await offline.aclose()
    await malformed.aclose()
    await fallback.aclose()


def test_openai_compatible_payload_does_not_send_deepseek_controls() -> None:
    model = OpenAICompatibleChatModel(
        "openai-key", "https://api.openai.com/v1", "gpt-5.6-luna"
    )

    payload = model.request_payload(
        [{"role": "user", "content": "hi"}], temperature=0.7, json_object=True
    )

    assert payload == {
        "model": "gpt-5.6-luna",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "none",
        "max_completion_tokens": 900,
        "response_format": {"type": "json_object"},
    }


@pytest.mark.asyncio
async def test_openai_compatible_metered_completion_reports_openai_provenance() -> None:
    model = OpenAICompatibleChatModel(
        "openai-key",
        "https://api.openai.com/v1",
        "gpt-5.6-luna",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "chatcmpl-openai-1",
                    "choices": [{"message": {"content": '{"ok":true}'}}],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 2,
                        "total_tokens": 13,
                    },
                },
            )
        ),
    )

    _text, usage_raw = await model.complete_json_with_usage(
        [{"role": "user", "content": "JSON"}]
    )
    usage = ModelUsageProvenance.model_validate(usage_raw)

    assert usage.provider == "openai"
    assert usage.route_class == "chat"
    assert usage.input_tokens == 11
    assert usage.output_tokens == 2
    assert usage.thinking_tokens == 0
    await model.aclose()


@pytest.mark.asyncio
async def test_deepseek_json_stream_exposes_deltas_and_one_metered_result() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = b"".join(
            (
                b'data: {"id":"stream-1","choices":[{"delta":{"content":"{\\\"a\\\":"}}]}\n\n',
                b'data: {"id":"stream-1","choices":[{"delta":{"content":"1}"}}]}\n\n',
                b'data: {"id":"stream-1","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n',
                b"data: [DONE]\n\n",
            )
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    model = DeepSeekChatModel(
        "deepseek-key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    deltas: list[str] = []

    text, usage_raw = await model.complete_json_stream_with_usage(
        [{"role": "user", "content": "JSON"}], on_text_delta=deltas.append
    )
    usage = ModelUsageProvenance.model_validate(usage_raw)

    assert text == '{"a":1}'
    assert deltas == ['{"a":', "1}"]
    assert requests[0]["stream"] is True
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert usage.provider == "deepseek"
    assert usage.input_tokens == 7
    assert usage.output_tokens == 3
    await model.aclose()


@pytest.mark.asyncio
async def test_deepseek_json_stream_keeps_completed_text_when_usage_is_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"id":"stream-2","choices":[{"delta":{"content":"{}"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    model = DeepSeekChatModel(
        "deepseek-key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        text, usage_raw = await model.complete_json_stream_with_usage(
            [{"role": "user", "content": "JSON"}]
        )
    finally:
        await model.aclose()

    assert text == "{}"
    assert usage_raw is None


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
@pytest.mark.parametrize(
    "tool_calls,error",
    [
        (
            [
                {
                    "type": "function",
                    "function": {"name": "wrong_contract", "arguments": "{}"},
                }
            ],
            "unexpected tool identity",
        ),
        (
            [
                {
                    "type": "function",
                    "function": {"name": "combined_cognition", "arguments": "{}"},
                },
                {
                    "type": "function",
                    "function": {"name": "combined_cognition", "arguments": "{}"},
                },
            ],
            "exactly one tool call",
        ),
    ],
)
async def test_forced_tool_completion_rejects_wrong_or_multiple_tool_calls(
    tool_calls: list[dict[str, object]],
    error: str,
) -> None:
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [{"message": {"tool_calls": tool_calls}}]},
            )
        ),
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "combined_cognition",
                "parameters": {"type": "object"},
            },
        }
    ]
    choice = {"type": "function", "function": {"name": "combined_cognition"}}

    try:
        with pytest.raises(ValueError, match=error):
            await model.complete_json_with_usage(
                [{"role": "user", "content": "choose"}],
                tools=tools,
                tool_choice=choice,
            )
    finally:
        await model.aclose()


@pytest.mark.asyncio
async def test_forced_tool_stream_validates_identity_and_exposes_argument_deltas() -> None:
    body = b"".join(
        (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"combined_cognition","arguments":"{\\"a\\":"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}\n\n',
            b"data: [DONE]\n\n",
        )
    )
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )
        ),
    )
    deltas: list[str] = []
    tools = [
        {
            "type": "function",
            "function": {
                "name": "combined_cognition",
                "parameters": {"type": "object"},
            },
        }
    ]
    choice = {"type": "function", "function": {"name": "combined_cognition"}}

    try:
        text, _usage = await model.complete_json_stream_with_usage(
            [{"role": "user", "content": "choose"}],
            on_text_delta=deltas.append,
            tools=tools,
            tool_choice=choice,
        )
    finally:
        await model.aclose()

    assert text == '{"a":1}'
    assert deltas == ['{"a":', "1}"]


@pytest.mark.asyncio
async def test_request_emission_marker_runs_after_payload_build_at_transport_boundary() -> None:
    events: list[str] = []

    class _ObservedPayloadModel(DeepSeekChatModel):
        def request_payload(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            events.append("payload_built")
            return super().request_payload(*args, **kwargs)

    def handler(_request: httpx.Request) -> httpx.Response:
        events.append("transport_entered")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    model = _ObservedPayloadModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )

    with model_request_emission_scope(
        provider_call_id="model-call:test-emission",
        entry_marker=lambda _call_id: events.append("request_emitted"),
        completion_marker=lambda _call_id: events.append("request_completed"),
    ):
        assert await model.complete([{"role": "user", "content": "hi"}]) == "ok"

    assert events == [
        "payload_built",
        "request_emitted",
        "transport_entered",
        "request_completed",
    ]
    await model.aclose()


@pytest.mark.asyncio
async def test_local_capacity_rejection_does_not_fabricate_transport_entry() -> None:
    gate = ProviderCapacityGate()
    lease = gate.acquire()
    marks: list[str] = []
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        capacity_gate=gate,
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("capacity rejection must precede transport")
        ),
    )

    try:
        with model_request_emission_scope(
            provider_call_id="model-call:test-capacity",
            entry_marker=lambda _call_id: marks.append("request_emitted"),
            completion_marker=lambda _call_id: marks.append("request_completed"),
        ):
            with pytest.raises(ModelCapacityBusyError):
                await model.complete([{"role": "user", "content": "hi"}])
    finally:
        gate.release(lease)
        await model.aclose()

    assert marks == []


@pytest.mark.asyncio
async def test_failover_transport_requests_receive_distinct_closed_span_identities() -> None:
    events: list[tuple[str, str]] = []
    primary = DeepSeekChatModel(
        "deepseek-key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"error": "unavailable"})
        ),
    )
    fallback = OpenAICompatibleChatModel(
        "openai-key",
        "https://api.openai.com/v1",
        "gpt-5.6-luna",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "fallback"}}]},
            )
        ),
    )
    route = FailoverChatModel(primary=primary, fallback=fallback)

    try:
        with model_request_emission_scope(
            provider_call_id="model-call:failover",
            entry_marker=lambda call_id: events.append(("entry", call_id)),
            completion_marker=lambda call_id: events.append(("completion", call_id)),
        ):
            assert await route.complete([{"role": "user", "content": "hi"}]) == "fallback"
    finally:
        await route.aclose()

    entries = [call_id for kind, call_id in events if kind == "entry"]
    completions = [call_id for kind, call_id in events if kind == "completion"]
    assert len(entries) == 2
    assert len(set(entries)) == 2
    assert completions == entries


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
    assert usage.thinking_enabled is False
    assert usage.reasoning_effort == ""
    assert usage.latency_ms >= 0


@pytest.mark.asyncio
async def test_deepseek_metered_completion_returns_provider_bound_usage() -> None:
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "chatcmpl-deepseek-1",
                    "choices": [{"message": {"content": "你好。"}}],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 18,
                        "total_tokens": 138,
                        "completion_tokens_details": {"reasoning_tokens": 7},
                    },
                },
            )
        ),
    )

    text, usage_raw = await model.complete_with_usage(
        [{"role": "user", "content": "你好"}]
    )
    usage = ModelUsageProvenance.model_validate(usage_raw)

    assert text == "你好。"
    assert usage.route_class == "chat"
    assert usage.input_tokens == 120
    assert usage.output_tokens == 18
    assert usage.thinking_tokens == 7
    assert usage.token_provenance == "provider_reported"
    assert usage.transport == "provider_api"
    assert usage.provider == "deepseek"
    assert usage.provider_usage_ref.startswith("provider-usage:deepseek:")
    await model.aclose()


@pytest.mark.asyncio
async def test_metered_completion_rejects_missing_provider_token_counts() -> None:
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "chatcmpl-without-usage",
                    "choices": [{"message": {"content": "你好。"}}],
                },
            )
        ),
    )

    with pytest.raises(ValueError, match="prompt_tokens"):
        await model.complete_with_usage([{"role": "user", "content": "你好"}])

    await model.aclose()


@pytest.mark.asyncio
async def test_metered_json_completion_preserves_the_json_request_mode() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-json-1",
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )

    text, usage_raw = await model.complete_json_with_usage(
        [{"role": "user", "content": "JSON"}]
    )

    assert text == '{"ok":true}'
    assert captured_payload["response_format"] == {"type": "json_object"}
    assert ModelUsageProvenance.model_validate(usage_raw).input_tokens == 8
    await model.aclose()


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
    ), model_call_scope(
        "reply",
        action_id="model-call-1",
        attempt=2,
        budget_reservation_id="reservation-1",
    ):
        await model.complete([{"role": "user", "content": "hi"}])

    usage = captured[0]
    assert usage.world_id == "world-1"
    assert usage.turn_id == "turn-1"
    assert usage.action_id == "model-call-1"
    assert usage.cadence == "hot"
    assert usage.attempt == 2
    assert usage.budget_reservation_id == "reservation-1"
    assert usage.thinking_enabled is True
    assert usage.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_emitted_request_reports_unpersisted_usage_to_its_call_scope() -> None:
    """A DB/observer outage cannot make an emitted provider request look free."""

    def unavailable_observer(_usage: object) -> None:
        raise OSError("usage database unavailable")

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        ),
        usage_observer=unavailable_observer,
    )

    with model_call_scope("reply", budget_reservation_id="reservation-1") as scope:
        assert await model.complete([{"role": "user", "content": "hi"}]) == "ok"

    assert scope.request_emitted is True
    assert scope.usage_persisted is False


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
async def test_shared_single_worker_capacity_rejects_a_second_call_without_queueing() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "本地结果"}}]},
        )

    capacity = ProviderCapacityGate(cooldown_seconds=30)
    transport = httpx.MockTransport(handler)
    first = OpenAICompatibleChatModel(
        "local",
        "http://127.0.0.1:8188/v1",
        "qwen-local",
        transport=transport,
        capacity_gate=capacity,
    )
    second = OpenAICompatibleChatModel(
        "local",
        "http://127.0.0.1:8188/v1",
        "qwen-local",
        transport=transport,
        capacity_gate=capacity,
    )

    in_flight = asyncio.create_task(
        first.complete([{"role": "user", "content": "first"}])
    )
    await entered.wait()
    with pytest.raises(ModelCapacityBusyError, match="in flight"):
        await second.complete([{"role": "user", "content": "must not queue"}])

    snapshot = capacity.snapshot()
    assert snapshot.status == "active"
    assert snapshot.admitted_calls == 1
    assert snapshot.rejected_active_calls == 1
    assert requests == 1

    release.set()
    assert await in_flight == "本地结果"
    assert capacity.snapshot().status == "idle"
    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
async def test_cancelled_local_call_arms_conservative_cooldown_and_busy_marker(
    tmp_path,
) -> None:
    monotonic_now = [0.0]
    wall_now = [1_000.0]
    entered = asyncio.Event()
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "恢复"}}]},
        )

    marker = tmp_path / "local-provider.capacity"
    capacity = ProviderCapacityGate(
        cooldown_seconds=120,
        active_lease_seconds=300,
        clock=lambda: monotonic_now[0],
        wall_clock=lambda: wall_now[0],
        marker_path=marker,
    )
    model = OpenAICompatibleChatModel(
        "local",
        "http://127.0.0.1:8188/v1",
        "qwen-local",
        transport=httpx.MockTransport(handler),
        capacity_gate=capacity,
    )

    timed = asyncio.create_task(
        model.complete([{"role": "user", "content": "slow"}])
    )
    await entered.wait()
    timed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await timed

    snapshot = capacity.snapshot()
    assert snapshot.status == "cooldown"
    assert snapshot.ambiguous_cancellations == 1
    assert marker.is_dir()
    with pytest.raises(ModelCapacityBusyError, match="cooldown"):
        await model.complete([{"role": "user", "content": "do not queue"}])
    assert calls == 1

    monotonic_now[0] = 121.0
    wall_now[0] = 1_121.0
    assert await model.complete([{"role": "user", "content": "recovery probe"}]) == "恢复"
    assert calls == 2
    assert capacity.snapshot().status == "idle"
    assert not marker.exists()
    await model.aclose()


def test_capacity_gate_observes_the_watchdog_cross_process_lease(tmp_path) -> None:
    marker = tmp_path / "local-provider.capacity"
    marker.mkdir()
    (marker / "state").write_text(
        "1300.000000\nwatchdog:test:1\nactive\n",
        encoding="utf-8",
    )
    capacity = ProviderCapacityGate(
        cooldown_seconds=120,
        active_lease_seconds=300,
        wall_clock=lambda: 1_000.0,
        marker_path=marker,
    )

    with pytest.raises(ModelCapacityBusyError, match="externally busy"):
        capacity.acquire()

    snapshot = capacity.snapshot()
    assert snapshot.status == "external_busy"
    assert snapshot.rejected_external_calls == 1
    assert snapshot.last_rejection_reason == "active"


def test_capacity_marker_write_failure_cleans_the_new_marker_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "local-provider.capacity"
    original_write_text = Path.write_text

    def fail_capacity_state_write(path: Path, *args: object, **kwargs: object) -> int:
        if path.parent == marker and path.name.startswith(".state."):
            original_write_text(path, "partial", encoding="utf-8")
            raise OSError("simulated capacity state write failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_capacity_state_write)
    capacity = ProviderCapacityGate(marker_path=marker)

    with pytest.raises(ModelCapacityBusyError, match="marker_unavailable"):
        capacity.acquire()

    assert not marker.exists()
    assert capacity.snapshot().status == "idle"


@pytest.mark.parametrize(
    "partial_state",
    (None, "not-a-deadline\npartial-owner\n"),
    ids=("empty-marker", "partial-state"),
)
def test_capacity_gate_reclaims_unreadable_marker_only_after_the_active_lease(
    tmp_path: Path,
    partial_state: str | None,
) -> None:
    marker = tmp_path / "local-provider.capacity"
    marker.mkdir()
    if partial_state is not None:
        (marker / "state").write_text(partial_state, encoding="utf-8")
    marker_mtime = marker.stat().st_mtime
    wall_now = [marker_mtime + 299.0]
    capacity = ProviderCapacityGate(
        cooldown_seconds=120,
        active_lease_seconds=300,
        wall_clock=lambda: wall_now[0],
        marker_path=marker,
    )

    with pytest.raises(ModelCapacityBusyError, match="marker_unreadable"):
        capacity.acquire()
    assert marker.is_dir()

    wall_now[0] = marker_mtime + 301.0
    token = capacity.acquire()

    assert (marker / "state").read_text(encoding="utf-8").splitlines()[1] == token
    capacity.release(token)
    assert not marker.exists()


def test_capacity_health_reports_an_unreadable_marker_as_degraded(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "local-provider.capacity"
    marker.mkdir()
    wall_now = marker.stat().st_mtime + 1.0
    capacity = ProviderCapacityGate(
        active_lease_seconds=300,
        wall_clock=lambda: wall_now,
        marker_path=marker,
    )

    snapshot = capacity.snapshot()
    health = capacity.health_snapshot()

    assert snapshot.status == "degraded"
    assert snapshot.last_rejection_reason == "marker_unreadable"
    assert health["status"] == "degraded"
    assert health["last_rejection_reason"] == "marker_unreadable"


def test_capacity_marker_clear_requires_the_observed_owner(tmp_path) -> None:
    marker = tmp_path / "local-provider.capacity"
    marker.mkdir()
    (marker / "state").write_text(
        "900.000000\nstale-owner\nactive\n",
        encoding="utf-8",
    )
    stale_reader = ProviderCapacityGate(
        wall_clock=lambda: 1_000.0,
        marker_path=marker,
    )

    stale_reader._clear_marker(expected_token="different-owner")  # noqa: SLF001
    assert marker.is_dir()
    assert (marker / "state").read_text(encoding="utf-8").splitlines()[1] == "stale-owner"

    stale_reader._clear_marker(expected_token="stale-owner")  # noqa: SLF001
    assert not marker.exists()


def test_only_one_process_can_atomically_take_over_the_same_stale_capacity_lease(
    tmp_path,
) -> None:
    marker = tmp_path / "local-provider.capacity"
    marker.mkdir()
    (marker / "state").write_text(
        "900.000000\nstale-owner\nactive\n",
        encoding="utf-8",
    )
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    tokens = ("daemon:contender:a", "daemon:contender:b")
    processes = tuple(
        context.Process(
            target=_claim_stale_capacity_marker_in_process,
            args=(str(marker), token, barrier, results),
        )
        for token in tokens
    )

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    outcomes = tuple(results.get(timeout=1) for _ in processes)
    winners = [token for token, status in outcomes if status is None]
    losers = [status for _token, status in outcomes if status is not None]
    assert len(winners) == 1
    assert losers == ["active"]
    assert (marker / "state").read_text(encoding="utf-8").splitlines()[1] == winners[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError),
)
async def test_ambiguous_transport_failure_keeps_capacity_in_cooldown(
    error_type: type[httpx.TransportError],
) -> None:
    monotonic_now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("ambiguous provider transport", request=request)

    capacity = ProviderCapacityGate(
        cooldown_seconds=120,
        active_lease_seconds=300,
        clock=lambda: monotonic_now[0],
    )
    model = OpenAICompatibleChatModel(
        "local",
        "http://127.0.0.1:8188/v1",
        "qwen-local",
        transport=httpx.MockTransport(handler),
        capacity_gate=capacity,
    )

    with pytest.raises(error_type):
        await model.complete([{"role": "user", "content": "ambiguous"}])

    snapshot = capacity.snapshot()
    assert snapshot.status == "cooldown"
    assert snapshot.ambiguous_cancellations == 1
    with pytest.raises(ModelCapacityBusyError, match="cooldown"):
        await model.complete([{"role": "user", "content": "must not overlap"}])
    await model.aclose()


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
