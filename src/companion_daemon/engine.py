import json

from companion_daemon.conversation import ConversationCore, PromptedConversationCore
from companion_daemon.db import CompanionStore
from companion_daemon.emotion_state import interpret_interaction
from companion_daemon.llm import ChatModel
from companion_daemon.memory import extract_memories, memory_lines
from companion_daemon.models import (
    CompanionReply,
    IncomingMessage,
    MessageAttachment,
    MoodState,
    ProactiveDecision,
)
from companion_daemon.mood import (
    platform_context,
    update_mood_for_attachment_insight,
    update_mood_for_message,
)
from companion_daemon.multimodal import summarize_attachments
from companion_daemon.multimodal_analysis import AttachmentInsight, MultimodalAnalyzer
from companion_daemon.prompts import proactive_prompt
from companion_daemon.relationship import advance_relationship
from companion_daemon.sanitize import sanitize_chat_text
from companion_daemon.stickers import StickerCatalog
from companion_daemon.time import utc_now


class CompanionEngine:
    def __init__(
        self,
        store: CompanionStore,
        model: ChatModel,
        companion_system_prompt: str,
        stickers: StickerCatalog | None = None,
        multimodal_analyzer: MultimodalAnalyzer | None = None,
        conversation_core: ConversationCore | None = None,
    ):
        self.store = store
        self.model = model
        self.companion_system_prompt = companion_system_prompt
        self.stickers = stickers
        self.multimodal_analyzer = multimodal_analyzer or MultimodalAnalyzer()
        self.conversation_core = conversation_core or PromptedConversationCore(
            model,
            companion_system_prompt,
        )

    async def handle_message(self, message: IncomingMessage) -> CompanionReply:
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        previous_state = self.store.get_mood_state(canonical_user_id)
        context = platform_context(previous_state, message)
        event = interpret_interaction(message, previous_state)
        next_state = update_mood_for_message(previous_state, message)

        self.store.save_incoming(canonical_user_id, message)
        self.store.record_interaction_event(
            canonical_user_id,
            event_kind=event.kind,
            user_intent=event.user_intent,
            intensity=event.intensity,
            private_note=event.private_note,
            platform=message.platform,
            message_id=message.message_id,
        )
        self.store.upsert_memory(
            canonical_user_id,
            kind="interaction_pattern",
            content=f"{event.kind}: {event.private_note}",
            source=f"{message.platform}:{message.message_id or ''}",
            confidence=min(0.9, 0.45 + (event.intensity * 0.1)),
        )
        for extracted in extract_memories(message):
            self.store.upsert_memory(
                canonical_user_id,
                kind=extracted.kind,
                content=extracted.content,
                source=f"{message.platform}:{message.message_id or ''}",
                confidence=extracted.confidence,
            )
        attachment_lines = summarize_attachments(message.attachments)
        for attachment in message.attachments:
            source = self._attachment_source(message, attachment)
            cached = self.store.memory_by_source(
                canonical_user_id,
                kind=f"{attachment.kind}_insight",
                source=source,
            )
            if cached:
                insight = AttachmentInsight(
                    attachment.kind,
                    str(cached["content"]),
                    float(cached["confidence"]),
                )
            else:
                insight = await self.multimodal_analyzer.analyze(attachment)
                self.store.upsert_memory(
                    canonical_user_id,
                    kind=f"{insight.kind}_insight",
                    content=insight.summary,
                    source=source,
                    confidence=insight.confidence,
                )
            attachment_lines.append(f"分析: {insight.summary}")
            next_state = update_mood_for_attachment_insight(next_state, insight)

        next_state = advance_relationship(
            next_state,
            user_message_count=self.store.incoming_message_count(canonical_user_id),
        )
        self.store.save_mood_state(canonical_user_id, next_state)

        recent_lines = self._recent_lines(canonical_user_id)
        text = await self.conversation_core.reply(
            message,
            next_state,
            recent_lines,
            context,
            memory_lines(self.store.memories(canonical_user_id)),
            attachment_lines,
        )
        self.store.save_outgoing(canonical_user_id, message.platform, text)
        return CompanionReply(
            canonical_user_id=canonical_user_id,
            mood=next_state.mood,
            text=text,
            platform_context=context,
        )

    async def proactive_tick(self, canonical_user_id: str) -> ProactiveDecision:
        state = self.store.get_mood_state(canonical_user_id)
        recent_lines = self._recent_lines(canonical_user_id)
        raw = await self.model.complete(
            proactive_prompt(state, recent_lines, self.companion_system_prompt),
            temperature=0.7,
        )
        decision = self._parse_decision(canonical_user_id, raw, state)
        if decision.message:
            decision = decision.model_copy(update={"message": sanitize_chat_text(decision.message)})
        decision = self._attach_sticker(decision, state)
        self.store.save_proactive_event(
            canonical_user_id,
            decision.private_thought,
            decision.should_send,
            decision.platform,
            decision.message_type,
            decision.message,
            decision.sticker_category,
            decision.cooldown_minutes,
        )
        if decision.should_send and decision.platform and decision.message:
            self.store.save_outgoing(canonical_user_id, decision.platform, decision.message)
        return decision

    def _recent_lines(self, canonical_user_id: str) -> list[str]:
        lines = []
        for row in self.store.recent_messages(canonical_user_id):
            who = "你" if row["direction"] == "in" else "她"
            lines.append(f"[{row['platform']}] {who}: {row['text']}")
        return lines

    def _attachment_source(self, message: IncomingMessage, attachment: MessageAttachment) -> str:
        if attachment.url:
            return f"attachment:{attachment.url}"
        return f"{message.platform}:{message.message_id or ''}:{attachment.kind}:{attachment.filename or ''}"

    def _parse_decision(
        self, canonical_user_id: str, raw: str, state: MoodState
    ) -> ProactiveDecision:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {
                "private_thought": raw[:400] or "没有形成清晰想法。",
                "should_send": False,
                "platform": state.last_platform,
                "message_type": "none",
                "message": None,
                "sticker_category": None,
                "cooldown_minutes": 30,
            }
        data.setdefault("canonical_user_id", canonical_user_id)
        data.setdefault("private_thought", "只是短暂想了一下你。")
        data.setdefault("should_send", False)
        data.setdefault("platform", state.last_platform)
        data.setdefault("message_type", "none")
        data.setdefault("message", None)
        data.setdefault("sticker_category", None)
        data.setdefault("sticker_path", None)
        data.setdefault("cooldown_minutes", 30)
        return ProactiveDecision(**data)

    def _attach_sticker(
        self,
        decision: ProactiveDecision,
        state: MoodState,
    ) -> ProactiveDecision:
        if not self.stickers or decision.message_type not in {"sticker", "text_sticker"}:
            return decision
        sticker = self.stickers.choose(state.mood)
        if not sticker:
            return decision
        return decision.model_copy(
            update={
                "sticker_category": decision.sticker_category or sticker.category,
                "sticker_path": str(sticker.path),
            }
        )


def seed_user(store: CompanionStore, canonical_user_id: str = "geoff") -> None:
    store.map_account("simulator", "geoff", canonical_user_id)
    store.save_mood_state(canonical_user_id, MoodState(updated_at=utc_now()))
