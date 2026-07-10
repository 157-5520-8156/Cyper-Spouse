from typing import Protocol

import httpx

from companion_daemon.llm import ChatModel
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.prompts import reply_prompt
from companion_daemon.reply_postprocess import postprocess_reply_text
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
        self_core_block: str | None = None,
        context_block: str | None = None,
    ) -> str:
        """Return the companion's reply."""


_REWRITE_PROMPT = (
    "下面是一个20岁女大学生在QQ私聊里发的消息。"
    "如果这句话听起来像AI、客服或助手说的，改写得更自然。"
    "保持原意和她的语气。如果已经够自然，原样返回。"
    "只输出最终消息，不加解释。\n\n她的消息：{text}"
)


class PromptedConversationCore:
    """SillyTavern-style prompt core backed by a chat-completions API."""

    def __init__(
        self,
        model: ChatModel,
        companion_system_prompt: str,
        example_messages: list[dict[str, str]] | None = None,
        rewrite_model: ChatModel | None = None,
    ):
        self.model = model
        self.companion_system_prompt = companion_system_prompt
        self.example_messages = example_messages or []
        self.rewrite_model = rewrite_model

    async def reply(
        self,
        message: IncomingMessage,
        mood_state: MoodState,
        recent_lines: list[str],
        platform_context: str | None,
        memory_lines: list[str] | None = None,
        attachment_lines: list[str] | None = None,
        self_core_block: str | None = None,
        context_block: str | None = None,
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
                example_pairs=self.example_messages,
                self_core_block=self_core_block,
                context_block=context_block,
            ),
            temperature=0.75,
        )
        text = postprocess_reply_text(text, recent_lines=recent_lines, user_text=message.text)
        if self.rewrite_model and len(text) >= 5:
            text = await self._rewrite_for_naturalness(text)
        return postprocess_reply_text(text, recent_lines=recent_lines, user_text=message.text)

    async def _rewrite_for_naturalness(self, text: str) -> str:
        try:
            rewritten = await self.rewrite_model.complete(
                [
                    {
                        "role": "user",
                        "content": _REWRITE_PROMPT.format(text=text),
                    }
                ],
                temperature=0.2,
            )
            rewritten = sanitize_chat_text(rewritten)
            if rewritten and len(rewritten) <= len(text) * 3:
                return rewritten
        except Exception:
            pass
        return text


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
        self_core_block: str | None = None,
        context_block: str | None = None,
    ) -> str:
        payload = {
            "systemPrompt": self.companion_system_prompt,
            "userText": message.text,
            "recent": recent_lines,
            "memories": memory_lines or [],
            "attachments": attachment_lines or [],
            "selfCore": self_core_block or "",
            "contextPackage": context_block or "",
            "state": {
                "mood": mood_state.mood,
                "intimacy": mood_state.intimacy,
                "trust": mood_state.trust,
                "attachment": mood_state.attachment,
                "patience": mood_state.patience,
                "security": mood_state.security,
                "curiosity": mood_state.curiosity,
                "initiative": mood_state.initiative,
                "emotional_charge": mood_state.emotional_charge,
                "boundary_level": mood_state.boundary_level,
                "relationship_status": relationship_status_line(mood_state),
                "relationship_stage": mood_state.relationship_stage,
                "relationship_instruction": relationship_instruction(mood_state.relationship_stage),
                "unresolved_emotion": mood_state.unresolved_emotion,
                "last_user_intent": mood_state.last_user_intent,
                "last_interaction_event": mood_state.last_interaction_event,
                "reply_style_hint": mood_state.reply_style_hint,
                "emotion_vector": mood_state.emotion_vector,
                "emotion_baseline": mood_state.emotion_baseline,
                "emotion_affinity": mood_state.emotion_affinity,
                "last_emotion_impact": mood_state.last_emotion_impact,
                "platform_context": platform_context,
            },
        }
        async with httpx.AsyncClient(timeout=60, transport=self.transport, trust_env=False) as client:
            token_response = await client.get(f"{self.base_url}/csrf-token")
            token_response.raise_for_status()
            csrf_token = str(token_response.json().get("token", ""))
            response = await client.post(
                f"{self.base_url}/api/plugins/girl-agent-core/reply",
                json=payload,
                headers={"X-CSRF-Token": csrf_token},
            )
            response.raise_for_status()
        text = sanitize_chat_text(str(response.json().get("text", "")))
        return postprocess_reply_text(text, recent_lines=recent_lines, user_text=message.text)
