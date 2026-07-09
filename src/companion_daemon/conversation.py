from typing import Protocol

import httpx

from companion_daemon.llm import ChatModel
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.prompts import reply_prompt
from companion_daemon.relationship import relationship_instruction, relationship_status_line
from companion_daemon.sanitize import sanitize_chat_text


class ConversationCore(Protocol):
    async def reply(
        self,
        message: IncomingMessage,
        mood_state: MoodState,
        recent_lines: list[str],
        platform_context: str | None,
        memory_lines: list[str] | None = None,
        attachment_lines: list[str] | None = None,
    ) -> str:
        """Return the companion's reply."""


class PromptedConversationCore:
    """SillyTavern-style prompt core backed by a chat-completions API."""

    def __init__(self, model: ChatModel, companion_system_prompt: str):
        self.model = model
        self.companion_system_prompt = companion_system_prompt

    async def reply(
        self,
        message: IncomingMessage,
        mood_state: MoodState,
        recent_lines: list[str],
        platform_context: str | None,
        memory_lines: list[str] | None = None,
        attachment_lines: list[str] | None = None,
    ) -> str:
        text = await self.model.complete(
            reply_prompt(
                message,
                mood_state,
                recent_lines,
                platform_context,
                self.companion_system_prompt,
                memory_lines,
                attachment_lines,
            ),
            temperature=0.85,
        )
        return sanitize_chat_text(text)


class SillyTavernConversationCore:
    """Conversation core hosted by a SillyTavern server plugin."""

    def __init__(
        self,
        base_url: str,
        companion_system_prompt: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.companion_system_prompt = companion_system_prompt
        self.transport = transport

    async def reply(
        self,
        message: IncomingMessage,
        mood_state: MoodState,
        recent_lines: list[str],
        platform_context: str | None,
        memory_lines: list[str] | None = None,
        attachment_lines: list[str] | None = None,
    ) -> str:
        payload = {
            "systemPrompt": self.companion_system_prompt,
            "userText": message.text,
            "recent": recent_lines,
            "memories": memory_lines or [],
            "attachments": attachment_lines or [],
            "state": {
                "mood": mood_state.mood,
                "intimacy": mood_state.intimacy,
                "trust": mood_state.trust,
                "attachment": mood_state.attachment,
                "relationship_status": relationship_status_line(mood_state),
                "relationship_stage": mood_state.relationship_stage,
                "relationship_instruction": relationship_instruction(mood_state.relationship_stage),
                "unresolved_emotion": mood_state.unresolved_emotion,
                "platform_context": platform_context,
            },
        }
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
            token_response = await client.get(f"{self.base_url}/csrf-token")
            token_response.raise_for_status()
            csrf_token = str(token_response.json().get("token", ""))
            response = await client.post(
                f"{self.base_url}/api/plugins/girl-agent-core/reply",
                json=payload,
                headers={"X-CSRF-Token": csrf_token},
            )
            response.raise_for_status()
        return sanitize_chat_text(str(response.json().get("text", "")))
