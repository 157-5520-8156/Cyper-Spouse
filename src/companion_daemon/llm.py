import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from time import monotonic
from typing import Protocol, TypeVar

import httpx

from companion_daemon.model_call_policy import ProviderCircuitState


_T = TypeVar("_T")


_MODEL_CALL_PURPOSE: ContextVar[str] = ContextVar(
    "model_call_purpose", default="unclassified"
)
_MODEL_CALL_META: ContextVar[dict[str, object]] = ContextVar(
    "model_call_meta", default={}
)
_MODEL_CALL_STATE: ContextVar["ModelCallScopeState | None"] = ContextVar(
    "model_call_state", default=None
)


@contextmanager
def model_turn_scope(
    *, world_id: str = "", turn_id: str = "", cadence: str = ""
) -> Iterator[None]:
    token = _MODEL_CALL_META.set(
        {
            **_MODEL_CALL_META.get(),
            "world_id": world_id,
            "turn_id": turn_id,
            "cadence": cadence,
        }
    )
    try:
        yield
    finally:
        _MODEL_CALL_META.reset(token)


@contextmanager
def model_call_scope(
    purpose: str,
    *,
    action_id: str = "",
    attempt: int = 1,
    budget_reservation_id: str = "",
) -> Iterator["ModelCallScopeState"]:
    # Background helpers may add a more specific purpose scope around an
    # already-reserved provider boundary.  Preserve its evidence instead of
    # creating an inner state that would be discarded before the reservation
    # is finalized.
    inherited_state = _MODEL_CALL_STATE.get()
    state = inherited_state or ModelCallScopeState()
    token = _MODEL_CALL_PURPOSE.set(purpose)
    state_token = None if inherited_state is not None else _MODEL_CALL_STATE.set(state)
    meta_token = _MODEL_CALL_META.set(
        {
            **_MODEL_CALL_META.get(),
            "action_id": action_id,
            "attempt": max(1, int(attempt)),
            "budget_reservation_id": budget_reservation_id,
        }
    )
    try:
        yield state
    finally:
        _MODEL_CALL_META.reset(meta_token)
        if state_token is not None:
            _MODEL_CALL_STATE.reset(state_token)
        _MODEL_CALL_PURPOSE.reset(token)


@dataclass(frozen=True)
class ModelCallUsage:
    purpose: str
    model: str
    status: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    total_tokens: int = 0
    error: str = ""
    world_id: str = ""
    turn_id: str = ""
    action_id: str = ""
    cadence: str = ""
    attempt: int = 1
    budget_reservation_id: str = ""
    # The effective provider request mode, not an inferred token heuristic.
    # Flash can legitimately run with or without thinking, so model name alone
    # cannot support a truthful latency/cost baseline.
    thinking_enabled: bool = False
    reasoning_effort: str = ""
    # ``unknown`` means the provider may have accepted or charged the call.
    # Only a concrete local/provider rejection may use ``not_billed``.
    billing_state: str = "unknown"


@dataclass
class ModelCallScopeState:
    """Provider-boundary facts retained while the call scope remains active."""

    request_emitted: bool = False
    usage_persisted: bool | None = None


def _mark_model_request_emitted() -> None:
    state = _MODEL_CALL_STATE.get()
    if state is not None:
        state.request_emitted = True


class ModelCircuitOpenError(ConnectionError):
    """Raised immediately while a model provider circuit is open."""


def _is_provider_outage(exc: Exception) -> bool:
    if isinstance(exc, ModelCircuitOpenError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 408 or status == 429 or status >= 500
    return isinstance(exc, (ConnectionError, httpx.TransportError))


class ProviderCircuitBreaker:
    """Bound repeated provider stalls while allowing a timed recovery probe."""

    def __init__(
        self,
        *,
        failure_threshold: int = 2,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    def before_call(self) -> None:
        if self._opened_at is None:
            return
        if self.clock() - self._opened_at < self.cooldown_seconds:
            raise ModelCircuitOpenError("model provider circuit is open")
        if self._probe_in_flight:
            raise ModelCircuitOpenError("model provider recovery probe is in flight")
        self._probe_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        self._probe_in_flight = False
        if self._failures >= self.failure_threshold:
            self._opened_at = self.clock()

    def release_probe(self) -> None:
        """Release a half-open lease without treating caller cancellation as failure."""
        self._probe_in_flight = False

    def snapshot(self) -> ProviderCircuitState:
        if self._opened_at is None:
            return ProviderCircuitState.closed()
        if self.clock() - self._opened_at < self.cooldown_seconds:
            return ProviderCircuitState.open()
        return ProviderCircuitState.half_open()


async def complete_with_timeout(
    awaitable: Awaitable[_T],
    *,
    timeout_seconds: float,
    cancellation_grace_seconds: float = 0.1,
) -> _T:
    """Bound one model operation while preserving why its task was cancelled."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait(
            (task,), timeout=max(0.0, float(timeout_seconds))
        )
    except asyncio.CancelledError:
        await _cancel_with_grace(
            task,
            reason="caller_cancelled",
            grace_seconds=cancellation_grace_seconds,
        )
        raise
    if task in done:
        return task.result()
    await _cancel_with_grace(
        task,
        reason="provider_timeout",
        grace_seconds=cancellation_grace_seconds,
    )
    raise TimeoutError(f"model call exceeded {timeout_seconds:g}s")


async def _cancel_with_grace(
    task: asyncio.Future[object], *, reason: str, grace_seconds: float
) -> None:
    task.cancel(reason)
    done, _pending = await asyncio.wait(
        (task,), timeout=max(0.0, float(grace_seconds))
    )
    if task in done:
        _consume_task_result(task)
    else:
        task.add_done_callback(_consume_task_result)


def _consume_task_result(task: asyncio.Future[object]) -> None:
    try:
        task.result()
    except BaseException:
        # A detached, already-cancelled child must not produce an unhandled-task
        # warning or replace the caller's cancellation/timeout outcome.
        pass


class ChatModel(Protocol):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        """Return assistant text for chat messages."""


class DeepSeekChatModel:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        thinking_enabled: bool = True,
        reasoning_effort: str = "high",
        transport: httpx.AsyncBaseTransport | None = None,
        usage_observer: Callable[[ModelCallUsage], None] | None = None,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort
        self.transport = transport
        self.usage_observer = usage_observer
        self.circuit_breaker = circuit_breaker
        self.client = client or httpx.AsyncClient(
            timeout=45,
            trust_env=False,
            transport=transport,
        )

    def request_payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
        }
        if self.thinking_enabled:
            # DeepSeek V4 ignores temperature in thinking mode. Leaving it out
            # makes the mode choice explicit and avoids false tuning knobs.
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = temperature
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        return await self._complete(messages, temperature=temperature, json_object=False)

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        """Request one JSON object without changing the generic ChatModel API."""
        return await self._complete(messages, temperature=temperature, json_object=True)

    async def _complete(
        self, messages: list[dict[str, str]], *, temperature: float, json_object: bool
    ) -> str:
        started = monotonic()
        purpose = _MODEL_CALL_PURPOSE.get()
        call_meta = _MODEL_CALL_META.get()
        try:
            if self.circuit_breaker is not None:
                self.circuit_breaker.before_call()
            _mark_model_request_emitted()
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=self.request_payload(
                    messages, temperature=temperature, json_object=json_object
                ),
            )
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices:
                raise ValueError("model response choices must be a non-empty list")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise ValueError("model response content must be a non-empty string")
        except asyncio.CancelledError as exc:
            cancellation_kind = str(exc.args[0]) if exc.args else "caller_cancelled"
            provider_timeout = cancellation_kind == "provider_timeout"
            if self.circuit_breaker is not None:
                if provider_timeout:
                    self.circuit_breaker.record_failure()
                else:
                    self.circuit_breaker.release_probe()
            self._report_usage(
                ModelCallUsage(
                    purpose=purpose,
                    model=self.model,
                    status="failed",
                    latency_ms=max(0, int((monotonic() - started) * 1000)),
                    error="provider_timeout" if provider_timeout else "caller_cancelled",
                    world_id=str(call_meta.get("world_id") or ""),
                    turn_id=str(call_meta.get("turn_id") or ""),
                    action_id=str(call_meta.get("action_id") or ""),
                    cadence=str(call_meta.get("cadence") or ""),
                    attempt=max(1, int(call_meta.get("attempt") or 1)),
                    budget_reservation_id=str(
                        call_meta.get("budget_reservation_id") or ""
                    ),
                    thinking_enabled=self.thinking_enabled,
                    reasoning_effort=self.reasoning_effort,
                    billing_state="unknown",
                )
            )
            raise
        except Exception as exc:
            provider_outage = _is_provider_outage(exc)
            if self.circuit_breaker is not None and provider_outage:
                self.circuit_breaker.record_failure()
            if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
                error = f"schema_error:{exc}"
            elif provider_outage:
                error = f"provider_error:{exc}"
            elif isinstance(exc, httpx.HTTPStatusError):
                error = f"provider_rejection:{exc}"
            else:
                error = f"unexpected_error:{exc}"
            billing_state = (
                "not_billed"
                if isinstance(exc, ModelCircuitOpenError)
                or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code not in {408, 429}
                    and exc.response.status_code < 500
                )
                else "unknown"
            )
            self._report_usage(
                ModelCallUsage(
                    purpose=purpose,
                    model=self.model,
                    status="failed",
                    latency_ms=max(0, int((monotonic() - started) * 1000)),
                    error=error[:500],
                    world_id=str(call_meta.get("world_id") or ""),
                    turn_id=str(call_meta.get("turn_id") or ""),
                    action_id=str(call_meta.get("action_id") or ""),
                    cadence=str(call_meta.get("cadence") or ""),
                    attempt=max(1, int(call_meta.get("attempt") or 1)),
                    budget_reservation_id=str(
                        call_meta.get("budget_reservation_id") or ""
                    ),
                    thinking_enabled=self.thinking_enabled,
                    reasoning_effort=self.reasoning_effort,
                    billing_state=billing_state,
                )
            )
            raise
        if self.circuit_breaker is not None:
            self.circuit_breaker.record_success()
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("completion_tokens_details")
        details = details if isinstance(details, dict) else {}
        self._report_usage(
            ModelCallUsage(
                purpose=purpose,
                model=self.model,
                status="succeeded",
                latency_ms=max(0, int((monotonic() - started) * 1000)),
                prompt_tokens=_usage_int(usage, "prompt_tokens"),
                completion_tokens=_usage_int(usage, "completion_tokens"),
                reasoning_tokens=_usage_int(details, "reasoning_tokens"),
                cache_hit_tokens=_usage_int(usage, "prompt_cache_hit_tokens"),
                cache_miss_tokens=_usage_int(usage, "prompt_cache_miss_tokens"),
                total_tokens=_usage_int(usage, "total_tokens"),
                world_id=str(call_meta.get("world_id") or ""),
                turn_id=str(call_meta.get("turn_id") or ""),
                action_id=str(call_meta.get("action_id") or ""),
                cadence=str(call_meta.get("cadence") or ""),
                attempt=max(1, int(call_meta.get("attempt") or 1)),
                budget_reservation_id=str(call_meta.get("budget_reservation_id") or ""),
                thinking_enabled=self.thinking_enabled,
                reasoning_effort=self.reasoning_effort,
                billing_state="known",
            )
        )
        return content

    async def aclose(self) -> None:
        await self.client.aclose()

    def _report_usage(self, usage: ModelCallUsage) -> None:
        state = _MODEL_CALL_STATE.get()
        if self.usage_observer is None:
            if state is not None:
                state.usage_persisted = False
            return
        try:
            self.usage_observer(usage)
            if state is not None:
                state.usage_persisted = True
        except Exception:
            # Observability must never turn a successful model response into a
            # failed companion turn.
            if state is not None:
                state.usage_persisted = False
            return


def _usage_int(source: dict[str, object], key: str) -> int:
    value = source.get(key, 0)
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


class FakeCompanionModel:
    def __init__(self):
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append(messages)
        joined = "\n".join(message["content"] for message in messages)
        if "严格的虚拟世界事实审计器" in joined:
            return json.dumps(
                {"supported": True, "unsupported_spans": [], "reason": "fake audit pass"},
                ensure_ascii=False,
            )
        if "聊天余波" in joined and "WorldReplyJSON" in joined:
            return json.dumps(
                {
                    "reply_text": "想再补一句。",
                    "mentioned_event_ids": [],
                    "proposed_action_ids": [],
                },
                ensure_ascii=False,
            )
        if "WorldReplyJSON" in joined:
            return json.dumps(
                {
                    "reply_text": "刚看到，我在。",
                    "mentioned_event_ids": [],
                    "proposed_action_ids": [],
                },
                ensure_ascii=False,
            )
        if "Return strict JSON" in joined:
            return json.dumps(
                {
                    "private_thought": "他刚刚隔了一会儿才回来，我有点想贴近一点，但不想显得太黏。",
                    "should_send": True,
                    "platform": "qq",
                    "message_type": "text",
                    "message": "你回来了呀。我刚刚有一点点在等你。",
                    "sticker_category": None,
                    "cooldown_minutes": 45,
                },
                ensure_ascii=False,
            )
        return "刚刚是不是忙完了？我在呢。"
