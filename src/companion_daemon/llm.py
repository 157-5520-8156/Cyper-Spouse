import json
from typing import Protocol

import httpx


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
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort

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
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
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
        return str(payload["choices"][0]["message"]["content"])


class FakeCompanionModel:
    def __init__(self):
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append(messages)
        joined = "\n".join(message["content"] for message in messages)
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
