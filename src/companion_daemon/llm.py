from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from time import monotonic
from typing import Protocol

import httpx


_MODEL_CALL_PURPOSE: ContextVar[str] = ContextVar(
    "model_call_purpose", default="unclassified"
)


@contextmanager
def model_call_scope(purpose: str) -> Iterator[None]:
    token = _MODEL_CALL_PURPOSE.set(purpose)
    try:
        yield
    finally:
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
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort
        self.transport = transport
        self.usage_observer = usage_observer

    def request_payload(self, messages: list[dict[str, str]], *, temperature: float) -> dict[str, object]:
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
        return payload

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        started = monotonic()
        purpose = _MODEL_CALL_PURPOSE.get()
        try:
            async with httpx.AsyncClient(
                timeout=45,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self.request_payload(messages, temperature=temperature),
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
        except Exception as exc:
            self._report_usage(
                ModelCallUsage(
                    purpose=purpose,
                    model=self.model,
                    status="failed",
                    latency_ms=max(0, int((monotonic() - started) * 1000)),
                    error=str(exc)[:500],
                )
            )
            raise
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
            )
        )
        return content

    def _report_usage(self, usage: ModelCallUsage) -> None:
        if self.usage_observer is None:
            return
        try:
            self.usage_observer(usage)
        except Exception:
            # Observability must never turn a successful model response into a
            # failed companion turn.
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
