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


_REWRITE_PROMPT = """下面是一位20岁女大学生在 QQ 私聊里准备发出的消息。
把它改得自然、短、像真人聊天；如果已自然可原样返回。

同时核对下方的事实账本：不要保留或新增没有来源的具体经历、地点、人物、物品或结果；
不要把知栀的经历说成用户的，也不要反过来。最近聊天只用于语气与话题延续，不是事实凭据。
没把握时删掉该细节或改成不确定的感受。
如果需要解释回复间隔或失联感，只能使用账本里的当前生活状态；没有记录就直接承认让对方等到了，
不要为了圆场虚构“刚在做什么”、突然离开或要去做什么，也不要擅自许诺以后绝不会发生。

事实账本：
{evidence}

只输出最终消息，不加解释。

她的消息：{text}"""


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
            text = await self._rewrite_for_naturalness(text, context_block or "无额外事实账本")
        return postprocess_reply_text(text, recent_lines=recent_lines, user_text=message.text)

    async def _rewrite_for_naturalness(self, text: str, evidence: str) -> str:
        try:
            rewritten = await self.rewrite_model.complete(
                [
                    {
                        "role": "user",
                        "content": _REWRITE_PROMPT.format(text=text, evidence=evidence),
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
