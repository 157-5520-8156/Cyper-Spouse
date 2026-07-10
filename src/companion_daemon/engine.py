import asyncio
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from companion_daemon.budget import ESTIMATES, BudgetGate
from companion_daemon.character import CharacterProfile
from companion_daemon.conversation import ConversationCore, PromptedConversationCore
from companion_daemon.context_orchestrator import build_context_package
from companion_daemon.db import CompanionStore
from companion_daemon.emotion_state import interpret_interaction
from companion_daemon.emotion_personality import mbti_temperament_note
from companion_daemon.emotion_reactions import select_character_reaction
from companion_daemon.memory_consolidation import (
    build_self_core,
    load_self_core,
    should_consolidate,
    consolidate_memories,
)
from companion_daemon.image_agency import decide_image_agency, image_agency_prompt_line
from companion_daemon.image_generation import OpenAIImageGenerator, life_image_prompt
from companion_daemon.image_prompt_builder import ChatImageMessage, build_image_prompt
from companion_daemon.image_requests import detect_image_request
from companion_daemon.impression import apply_repeated_interaction_drift, apply_user_impression
from companion_daemon.life_continuity import build_life_continuity
from companion_daemon.life_runtime import (
    PhoneDecision,
    advance_life_runtime,
    apply_user_event_to_life_runtime,
    decide_phone_attention,
    mark_phone_idle,
    mark_phone_typing,
    proactive_outreach_allowed,
    synchronize_life_runtime,
    runtime_prompt_line,
)
from companion_daemon.llm import ChatModel
from companion_daemon.memory import extract_memories
from companion_daemon.models import (
    CompanionReply,
    IncomingMessage,
    MessageAttachment,
    MoodState,
    ProactiveDecision,
)
from companion_daemon.human_rhythm import apply_expression_after_reply, human_rhythm_snapshot
from companion_daemon.mood import (
    platform_context,
    update_mood_for_attachment_insight,
    update_mood_for_message,
)
from companion_daemon.inner_subtext import infer_inner_subtext
from companion_daemon.multimodal import summarize_attachments
from companion_daemon.multimodal_analysis import AttachmentInsight, MultimodalAnalyzer
from companion_daemon.personality_drift import apply_personality_drift
from companion_daemon.prompts import proactive_prompt, reply_prompt
from companion_daemon.proactive_feedback import apply_proactive_feedback, classify_proactive_feedback
from companion_daemon.proactive_triggers import ProactiveTrigger, evaluate_proactive_trigger
from companion_daemon.proactive_waiting import apply_waiting_after_proactive
from companion_daemon.relationship import advance_relationship, key_event_bonus
from companion_daemon.relationship_events import apply_key_relationship_event, detect_key_relationship_event
from companion_daemon.repair_curve import apply_repair_curve, serious_repair_key_event
from companion_daemon.reply_segments import split_reply_text
from companion_daemon.reply_stickers import choose_reply_sticker
from companion_daemon.sanitize import sanitize_chat_text
from companion_daemon.social_followups import (
    create_contradiction_followup,
    detect_mild_contradiction,
    reconcile_unshared_life_share_tasks,
    social_task_payload,
)
from companion_daemon.stickers import StickerCatalog
from companion_daemon.tone_inertia import build_tone_inertia, classify_outgoing_tone
from companion_daemon.time import utc_now
from companion_daemon.tool_requests import detect_tool_request, tool_prompt_line
from companion_daemon.unanswered_question import (
    apply_question_response,
    apply_unanswered_question_waiting,
    classify_response_to_own_question,
    last_unanswered_own_question,
)
from companion_daemon.withheld_impulse import apply_withheld_impulse, build_withheld_impulse

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Asia/Shanghai")

_DEEP_NIGHT_AFTERTHOUGHT_MOODS = {"miss_you", "worried", "affectionate", "sad", "anxious"}
_DEEP_NIGHT_RECENT_ALLOWED_TOKENS = (
    "累",
    "难过",
    "心里",
    "闷",
    "睡不着",
    "想你",
    "在吗",
    "怎么",
    "为什么",
    "？",
    "?",
    "离谱",
    "刚刚",
)


class CompanionEngine:
    def __init__(
        self,
        store: CompanionStore,
        model: ChatModel,
        companion_system_prompt: str,
        stickers: StickerCatalog | None = None,
        multimodal_analyzer: MultimodalAnalyzer | None = None,
        conversation_core: ConversationCore | None = None,
        character_profile: CharacterProfile | None = None,
        image_generator: OpenAIImageGenerator | None = None,
        budget_gate: BudgetGate | None = None,
        visual_identity_path: Path | None = Path("configs/visual_identity.yaml"),
        image_output_dir: Path = Path("assets/life"),
        rewrite_model: ChatModel | None = None,
    ):
        self.store = store
        self.model = model
        self.companion_system_prompt = companion_system_prompt
        self.stickers = stickers
        self.multimodal_analyzer = multimodal_analyzer or MultimodalAnalyzer()
        self.character_profile = character_profile
        self.image_generator = image_generator
        self.budget_gate = budget_gate
        self.visual_identity_path = visual_identity_path
        self.image_output_dir = image_output_dir
        # Character-card examples are style references already included in the
        # system prompt. Replaying them as fake chat history duplicates tokens
        # and makes concrete example details look like reusable live facts.
        self.conversation_core = conversation_core or PromptedConversationCore(
            model,
            companion_system_prompt,
            rewrite_model=rewrite_model,
        )

    async def handle_message(
        self,
        message: IncomingMessage,
        *,
        skip_reply: bool = False,
        mark_unread: bool = True,
        context_hint: str | None = None,
        defer_delivery: bool = False,
    ) -> CompanionReply | None:
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        # A new user turn means a previously planned check-in has been overtaken by reality.
        self.store.cancel_active_social_tasks(canonical_user_id, kind="comfort_followup")
        self.store.cancel_active_social_tasks(canonical_user_id, kind="promise_followup")
        self.store.cancel_active_social_tasks(canonical_user_id, kind="contradiction_followup")
        previous_state = self.store.get_mood_state(canonical_user_id)
        runtime = advance_life_runtime(self.store, canonical_user_id, previous_state)
        recent_dicts_before = self._recent_dicts(canonical_user_id, limit=16)
        recent_lines_before = self._format_recent_dicts(recent_dicts_before)
        pending_own_question = last_unanswered_own_question(recent_dicts_before)
        context = platform_context(previous_state, message)
        event = interpret_interaction(message, previous_state)
        next_state = update_mood_for_message(previous_state, message, event=event)
        key_event = detect_key_relationship_event(message)
        next_state = apply_key_relationship_event(next_state, key_event)
        next_state = apply_repair_curve(next_state, message_text=message.text)
        repair_key_event = serious_repair_key_event(next_state, message.text)
        key_event_for_memory = key_event or repair_key_event
        next_state = apply_personality_drift(next_state)
        proactive_feedback = None
        if self._is_reply_to_recent_proactive(canonical_user_id, message.platform):
            proactive_feedback = classify_proactive_feedback(message.text)
            next_state = apply_proactive_feedback(next_state, proactive_feedback)
            feedback_event = {
                "warm": "warmth_received",
                "rejected": "boundary_violation",
                "thin_or_busy": "proactive_thin_or_busy",
                "answered": "proactive_answered",
            }.get(proactive_feedback.kind, "ordinary_message")
            next_state = apply_user_impression(next_state, event_kind=feedback_event)
        question_response = classify_response_to_own_question(message.text, pending_own_question)
        next_state = apply_question_response(next_state, question_response)
        next_state = apply_user_impression(
            next_state,
            event_kind=event.kind,
            question_response=question_response.kind if question_response else None,
        )
        runtime = apply_user_event_to_life_runtime(
            self.store,
            canonical_user_id,
            event_kind=event.kind,
            message=message,
            state=next_state,
        )
        runtime = synchronize_life_runtime(self.store, canonical_user_id, next_state)

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
        next_state = apply_repeated_interaction_drift(
            next_state,
            [dict(row) for row in self.store.recent_interaction_events(canonical_user_id, limit=8)],
        )
        if event.kind == "user_vulnerable":
            self._create_comfort_followup(canonical_user_id, message)
        self._create_promise_followup(canonical_user_id, message)
        contradiction_hint = None
        contradiction = detect_mild_contradiction(
            message.text,
            runtime,
            recent_her_lines=[
                line.split("她:", 1)[-1]
                for line in recent_lines_before
                if "她:" in line
            ],
        )
        if contradiction:
            contradiction_hint = (
                f"用户注意到前后说法不一致：{contradiction}。"
                "不要防御性辩解，可以轻描淡写地圆过去或承认记混了。"
            )
            create_contradiction_followup(
                self.store,
                canonical_user_id,
                platform=message.platform,
                platform_user_id=message.platform_user_id,
                note=contradiction,
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
        if context_hint:
            attachment_lines.append(context_hint)
        if contradiction_hint:
            attachment_lines.append(contradiction_hint)
        if key_event_for_memory:
            attachment_lines.append(key_event_for_memory.prompt_line)
        recent_lines = recent_lines_before
        tone_inertia = build_tone_inertia(
            next_state,
            recent_lines,
            last_outgoing_tone=self._last_outgoing_tone(canonical_user_id),
        )
        life_continuity = build_life_continuity(
            next_state,
            previous_content=self._latest_life_continuity(canonical_user_id),
        )
        self.store.upsert_memory(
            canonical_user_id,
            kind="life_continuity",
            content=life_continuity.content,
            source=f"{message.platform}:{message.message_id or 'turn'}",
            confidence=0.72,
        )
        if key_event_for_memory:
            self.store.upsert_memory(
                canonical_user_id,
                kind="key_relationship_event",
                content=key_event_for_memory.memory,
                source=f"{message.platform}:{message.message_id or 'turn'}",
                confidence=0.86,
            )
        subtext = infer_inner_subtext(next_state)
        if subtext:
            self.store.upsert_memory(
                canonical_user_id,
                kind="inner_subtext",
                content=subtext.memory,
                source=f"{message.platform}:{message.message_id or 'turn'}",
                confidence=0.74,
            )
        if proactive_feedback:
            self.store.upsert_memory(
                canonical_user_id,
                kind="proactive_response",
                content=proactive_feedback.memory_content,
                source=f"{message.platform}:{message.message_id or 'turn'}",
                confidence=0.82,
            )
        if question_response:
            self.store.upsert_memory(
                canonical_user_id,
                kind=f"own_question_{question_response.kind}",
                content=question_response.memory,
                source=f"{message.platform}:{message.message_id or 'turn'}",
                confidence=0.78,
            )
        image_request = detect_image_request(
            message.text,
            [
                row["text"]
                for row in self.store.recent_messages(canonical_user_id, limit=6)
                if row["direction"] == "out"
            ],
        )
        if image_request.triggered:
            image_agency = decide_image_agency(image_request, next_state, message.text)
            attachment_lines.append(
                "图片请求: 用户可能在请求图片/自拍；"
                f"类型={image_request.type}；指向={image_request.directive or '未指定'}；"
                f"风格={image_request.style_tags or '默认'}。"
            )
            attachment_lines.append(image_agency_prompt_line(image_agency))
            self.store.upsert_memory(
                canonical_user_id,
                kind="image_request",
                content=f"{image_request.type}: {image_request.directive or message.text}",
                source=f"{message.platform}:{message.message_id or ''}",
                confidence=image_request.confidence,
            )
            if not image_agency.allow_generation:
                self.store.upsert_memory(
                    canonical_user_id,
                    kind=image_agency.kind,
                    content=f"{image_agency.reason}: {image_request.directive or message.text}",
                    source=f"{message.platform}:{message.message_id or ''}",
                    confidence=0.82,
                )
        else:
            image_agency = None
        tool_request = detect_tool_request(message.text)
        if tool_request:
            attachment_lines.append(tool_prompt_line(tool_request))
            self.store.record_tool_proposal(
                canonical_user_id,
                kind=tool_request.kind,
                risk=tool_request.risk,
                summary=tool_request.summary,
            )
        generated_image_path = await self._maybe_generate_requested_image(
            canonical_user_id,
            message,
            image_request.triggered and bool(image_agency and image_agency.allow_generation),
        )
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
            if insight.kind == "image" and _user_claims_image_is_self(message.text):
                self.store.upsert_memory(
                    canonical_user_id,
                    kind="user_visual_anchor",
                    content=(
                        "用户明确说这张图是自己/自拍；可见线索："
                        f"{insight.summary}"
                    ),
                    source=source,
                    confidence=min(0.88, max(0.62, insight.confidence)),
                )
                attachment_lines.append("视觉身份: 用户明确说这张图是自己；以后只能作为弱线索，不要凭图擅自认人。")
            next_state = update_mood_for_attachment_insight(next_state, insight)

        next_state = advance_relationship(
            next_state,
            user_message_count=self.store.incoming_message_count(canonical_user_id),
            key_event_score=key_event_bonus(
                [
                    str(row["content"])
                    for row in self.store.memories(canonical_user_id, limit=20)
                    if row["kind"] == "key_relationship_event"
                ]
            ),
        )
        next_state = next_state.model_copy(update={"has_unread": mark_unread if skip_reply else False})
        self.store.save_mood_state(canonical_user_id, next_state)
        runtime = synchronize_life_runtime(self.store, canonical_user_id, next_state)

        if skip_reply:
            return None

        self_core_block = self._self_core_block(canonical_user_id)

        context_package = build_context_package(
            message,
            next_state,
            recent_dicts_before,
            self.store.memories(canonical_user_id, limit=200),
            continuity_hint=f"{life_continuity.prompt_line} {tone_inertia.memory}",
            subtext_hint=subtext.prompt_line if subtext else None,
            life_context_override=runtime_prompt_line(runtime),
            self_fact_lines=self._self_fact_lines(canonical_user_id),
        )
        text = sanitize_chat_text(await self.conversation_core.reply(
            message,
            next_state,
            recent_lines,
            context,
            context_package.memory_lines,
            attachment_lines,
            self_core_block=self_core_block,
            context_block=context_package.prompt_block(),
        ))
        text_parts = split_reply_text(text, next_state)
        suggested_reaction = select_character_reaction(message.text, next_state)
        sticker = choose_reply_sticker(
            self.stickers,
            next_state,
            message,
            suggested_reaction=suggested_reaction.reaction_id if suggested_reaction else None,
        )
        if generated_image_path:
            sticker = None
        asyncio.create_task(self._maybe_consolidate(canonical_user_id, next_state))
        reply = CompanionReply(
            canonical_user_id=canonical_user_id,
            mood=next_state.mood,
            text=text,
            text_parts=text_parts,
            platform_context=context,
            sticker_path=str(sticker.path) if sticker else None,
            image_path=str(generated_image_path) if generated_image_path else None,
            suggested_reaction=(
                suggested_reaction.reaction_id if suggested_reaction and suggested_reaction.probability >= 0.25 else None
            ),
            delivery_id=self.store.queue_outgoing(
                canonical_user_id,
                message.platform,
                text,
                kind="reply",
            ),
        )
        if not defer_delivery:
            self.confirm_reply_delivery(reply)
        return reply

    def phone_attention_decision(self, message: IncomingMessage) -> PhoneDecision:
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        state = self.store.get_mood_state(canonical_user_id)
        decision = decide_phone_attention(
            self.store,
            canonical_user_id,
            message,
            state,
        )
        if not decision.read_now and not state.has_unread:
            unread_state = state.model_copy(update={"has_unread": True})
            self.store.save_mood_state(canonical_user_id, unread_state)
            synchronize_life_runtime(self.store, canonical_user_id, unread_state)
        return decision

    def create_deferred_reply_task(
        self,
        message: IncomingMessage,
        *,
        defer_minutes: float,
        reason: str,
        now: datetime | None = None,
    ) -> int:
        """Persist a delayed reply before its in-memory timer is allowed to run."""
        now = now or utc_now()
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        self.store.cancel_active_social_tasks(canonical_user_id, kind="reply_later")
        return self.store.create_social_task(
            canonical_user_id,
            kind="reply_later",
            platform=message.platform,
            platform_user_id=message.platform_user_id,
            payload=message.model_dump(mode="json"),
            reason=reason,
            due_at=now + timedelta(minutes=defer_minutes),
            expires_at=now + timedelta(hours=12),
        )

    def cancel_deferred_reply_task(self, task_id: int | None) -> None:
        if task_id is not None:
            self.store.cancel_social_task(task_id)

    def complete_deferred_reply_task(self, task_id: int | None) -> None:
        if task_id is not None:
            self.store.resolve_social_task(task_id)

    def create_read_later_task(
        self,
        message: IncomingMessage,
        *,
        defer_minutes: float,
        reason: str,
        now: datetime | None = None,
    ) -> int:
        """Persist a read-but-not-replied reminder without replaying the read event."""
        now = now or utc_now()
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        self.store.cancel_active_social_tasks(canonical_user_id, kind="reply_later")
        preface = (
            "读到了但当时不想回"
            if reason.startswith("emotional_ghost")
            else "读到了但被手头的事岔开"
        )
        return self.store.create_social_task(
            canonical_user_id,
            kind="reply_later",
            platform=message.platform,
            platform_user_id=message.platform_user_id,
            payload=message.model_dump(mode="json"),
            reason=f"{preface}；{reason}",
            due_at=now + timedelta(minutes=defer_minutes),
            expires_at=now + timedelta(hours=10),
        )

    def mark_phone_read_for_message(self, message: IncomingMessage) -> None:
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        mark_phone_typing(self.store, canonical_user_id)

    def confirm_reply_delivery(self, reply: CompanionReply) -> None:
        if reply.delivery_id is None:
            return
        delivered = self.store.mark_outgoing_delivered(reply.delivery_id)
        if not delivered or delivered["status"] != "planned":
            return
        state = self.store.get_mood_state(reply.canonical_user_id)
        expressed = apply_expression_after_reply(
            state,
            was_proactive=False,
            sent_image=bool(reply.image_path),
        )
        self.store.save_mood_state(reply.canonical_user_id, expressed)
        synchronize_life_runtime(self.store, reply.canonical_user_id, expressed)
        mark_phone_idle(self.store, reply.canonical_user_id)
        self.store.upsert_memory(
            reply.canonical_user_id,
            kind="tone_inertia",
            content=f"last_outgoing_tone={classify_outgoing_tone(reply.text, expressed)}",
            source=f"{delivered['platform']}:outgoing",
            confidence=0.65,
        )

    def fail_reply_delivery(self, reply: CompanionReply, reason: str, *, source_task_id: int | None = None) -> None:
        if reply.delivery_id is not None:
            self.store.mark_outgoing_failed(reply.delivery_id, reason)
        if source_task_id is not None:
            self.store.cancel_social_task(source_task_id)
            self._create_reply_reconsider_task(reply, reason)

    def _create_reply_reconsider_task(self, reply: CompanionReply, reason: str) -> int | None:
        if reply.delivery_id is None:
            return None
        failed = self.store.outbox_message(reply.delivery_id)
        if not failed:
            return None
        now = utc_now()
        return self.store.create_social_task(
            reply.canonical_user_id,
            kind="reply_reconsider",
            platform=str(failed["platform"]),
            platform_user_id="",
            payload={
                "failed_delivery_id": reply.delivery_id,
                "failed_text": reply.text[:240],
                "failure_reason": reason[:240],
            },
            reason="刚才那句没发出去，稍后重新判断要不要自然补一句，而不是重放原消息",
            due_at=now + timedelta(minutes=18),
            expires_at=now + timedelta(hours=6),
        )

    async def _maybe_consolidate(self, canonical_user_id: str, state: MoodState) -> None:
        try:
            if not should_consolidate(self.store, canonical_user_id):
                return
            estimate = ESTIMATES["memory_maintenance"]
            if self.budget_gate:
                decision = self.budget_gate.check(estimate, automatic=True)
                if not decision.allowed:
                    self.store.upsert_memory(
                        canonical_user_id,
                        kind="memory_maintenance_blocked",
                        content=decision.reason,
                        source="budget_gate",
                        confidence=0.9,
                    )
                    return
            logger.info("triggering memory consolidation for %s", canonical_user_id)
            await consolidate_memories(self.store, self.model, canonical_user_id)
            await build_self_core(self.store, self.model, canonical_user_id, state)
            if self.budget_gate:
                self.budget_gate.record(estimate, note="memory_consolidation:self_core")
        except Exception:
            logger.exception("background consolidation failed")

    async def generate_afterthought(
        self,
        canonical_user_id: str,
        reply_sent_at: datetime,
        *,
        mode: str = "quick_continue",
    ) -> str | None:
        """Generate a short follow-up message after a reply, if conditions are right.

        Returns the afterthought text, or None if no afterthought should be sent.
        """
        state = self.store.get_mood_state(canonical_user_id)
        if state.mood in {"guarded", "hurt"}:
            return None
        if state.boundary_level >= 35:
            return None
        rhythm = human_rhythm_snapshot(state)
        recent_rows = self._recent_dicts(canonical_user_id, limit=8)
        if rhythm.phase == "deep_night" and not _deep_night_afterthought_allowed(state, recent_rows):
            return None
        new_count = self.store.message_count_since(
            canonical_user_id, direction="in", since_iso=reply_sent_at.isoformat()
        )
        if new_count > 0:
            return None
        estimate = ESTIMATES["afterthought"]
        if self.budget_gate:
            decision = self.budget_gate.check(estimate, automatic=True)
            if not decision.allowed:
                self.store.upsert_memory(
                    canonical_user_id,
                    kind="afterthought_blocked",
                    content=decision.reason,
                    source="budget_gate",
                    confidence=0.85,
                )
                return None
        recent_lines = self._recent_lines(canonical_user_id)
        prompt = afterthought_prompt(mode, recent_lines[-8:])
        try:
            raw = await self.model.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.7,
            )
        except Exception:
            logger.exception("afterthought generation failed")
            return None
        text = sanitize_chat_text(raw)
        if not text or len(text) > 60 or _afterthought_repeats_recent(text, recent_lines):
            return None
        if self.budget_gate:
            self.budget_gate.record(estimate, note=f"qq_afterthought:{mode}")
        return text

    def queue_afterthought_delivery(self, canonical_user_id: str, platform: str, text: str) -> int:
        return self.store.queue_outgoing(canonical_user_id, platform, text, kind="afterthought")

    def confirm_afterthought_delivery(
        self,
        canonical_user_id: str,
        platform: str,
        text: str,
        *,
        delivery_id: int | None = None,
    ) -> None:
        if delivery_id is None:
            delivery_id = self.queue_afterthought_delivery(canonical_user_id, platform, text)
        delivered = self.store.mark_outgoing_delivered(delivery_id)
        if not delivered or delivered["status"] != "planned":
            return
        state = self.store.get_mood_state(canonical_user_id)
        expressed = apply_expression_after_reply(state, was_proactive=True)
        self.store.save_mood_state(canonical_user_id, expressed)
        synchronize_life_runtime(self.store, canonical_user_id, expressed)

    def fail_afterthought_delivery(self, delivery_id: int | None, reason: str) -> None:
        if delivery_id is not None:
            self.store.mark_outgoing_failed(delivery_id, reason)

    def confirm_life_event_delivery(self, canonical_user_id: str, platform: str = "qq") -> None:
        self.store.record_proactive_delivery(canonical_user_id, f"{platform}:life_event")
        state = self.store.get_mood_state(canonical_user_id)
        expressed = apply_expression_after_reply(state, was_proactive=True)
        self.store.save_mood_state(canonical_user_id, expressed)
        synchronize_life_runtime(self.store, canonical_user_id, expressed)


    async def _maybe_generate_requested_image(
        self,
        canonical_user_id: str,
        message: IncomingMessage,
        image_requested: bool,
    ) -> Path | None:
        if not image_requested or not self.image_generator or not self.character_profile:
            return None
        estimate = ESTIMATES["image_generation"]
        if self.budget_gate:
            decision = self.budget_gate.check(estimate, automatic=True)
            if not decision.allowed:
                self.store.upsert_memory(
                    canonical_user_id,
                    kind="image_request_blocked",
                    content=f"{decision.reason}: {message.text[:80]}",
                    source=f"{message.platform}:{message.message_id or ''}",
                    confidence=0.8,
                )
                return None
        payload = build_image_prompt(
            message.text,
            character=self.character_profile,
            recent_messages=[
                ChatImageMessage(text=str(row["text"]), is_user=row["direction"] == "in")
                for row in self.store.recent_messages(canonical_user_id, limit=8)
            ],
            visual_identity_path=self.visual_identity_path,
        )
        output_path = self.image_output_dir / f"reply-{canonical_user_id}-{int(utc_now().timestamp())}.png"
        generated = await self.image_generator.generate(payload.prompt, output_path=output_path)
        if self.budget_gate:
            self.budget_gate.record(estimate, note=f"chat_image:{payload.mode}:{payload.directive[:40]}")
        self.store.upsert_memory(
            canonical_user_id,
            kind="generated_image",
            content=f"{payload.mode}: {payload.directive}",
            source=str(generated.path),
            confidence=0.8,
        )
        return generated.path

    def refresh_waiting_state(self, canonical_user_id: str) -> MoodState:
        """Advance waiting/unanswered-question psychology outside a full proactive tick.

        The scheduler calls this on every pass, so time keeps flowing through her
        state even while the proactive cooldown is skipping decisions.
        """
        state = self.store.get_mood_state(canonical_user_id)
        last_sent = self.store.last_initiated_delivery(canonical_user_id, "qq")
        state = apply_waiting_after_proactive(
            state,
            last_sent_iso=last_sent,
            incoming_since=(
                self.store.message_count_since(canonical_user_id, direction="in", since_iso=last_sent)
                if last_sent
                else 0
            ),
        )
        pending_question = last_unanswered_own_question(self._recent_dicts(canonical_user_id, limit=16))
        state = apply_unanswered_question_waiting(state, pending_question)
        self.store.save_mood_state(canonical_user_id, state)
        synchronize_life_runtime(self.store, canonical_user_id, state)
        return state

    async def proactive_tick(self, canonical_user_id: str) -> ProactiveDecision:
        now = utc_now()
        state = self.refresh_waiting_state(canonical_user_id)
        reconcile_unshared_life_share_tasks(self.store, canonical_user_id)
        runtime = advance_life_runtime(self.store, canonical_user_id, state)
        recent_lines = self._recent_lines(canonical_user_id)
        recent_rows = self._recent_dicts(canonical_user_id, limit=16)
        social_task = self.store.next_due_social_task(
            canonical_user_id,
            kinds=(
                "comfort_followup",
                "promise_followup",
                "reply_reconsider",
                "life_share_followup",
                "contradiction_followup",
            ),
            now=now,
        )
        trigger = _social_task_trigger(social_task) or evaluate_proactive_trigger(
            state=state,
            recent_messages=recent_rows,
            trigger_history=self.store.recent_proactive_trigger_history(canonical_user_id),
            now=now,
        )
        estimate = ESTIMATES["proactive_decision"]
        if self.budget_gate:
            budget_decision = self.budget_gate.check(estimate, automatic=True)
            if not budget_decision.allowed:
                decision = ProactiveDecision(
                    canonical_user_id=canonical_user_id,
                    private_thought=f"预算阀门阻止主动决策：{budget_decision.reason}",
                    should_send=False,
                    trigger_type=trigger.type if trigger else None,
                )
                self.store.save_proactive_event(
                    canonical_user_id,
                    decision.private_thought,
                    decision.should_send,
                    decision.platform,
                    decision.message_type,
                    decision.message,
                    decision.sticker_category,
                    decision.trigger_type,
                    decision.cooldown_minutes,
                )
                if social_task:
                    self.store.defer_social_task(int(social_task["id"]), due_at=now + timedelta(minutes=45))
                return decision
        raw = await self.model.complete(
            proactive_prompt(
                state,
                recent_lines,
                self.companion_system_prompt,
                trigger,
                life_runtime_context=runtime_prompt_line(runtime),
            ),
            temperature=0.7,
        )
        if self.budget_gate:
            self.budget_gate.record(estimate, note=f"proactive_decision:{trigger.type if trigger else 'none'}")
        decision = self._parse_decision(canonical_user_id, raw, state)
        allowed, activity_reason = proactive_outreach_allowed(runtime)
        if decision.should_send and not allowed:
            decision = decision.model_copy(
                update={
                    "should_send": False,
                    "message_type": "none",
                    "message": None,
                    "sticker_path": None,
                    "image_path": None,
                    "private_thought": f"{decision.private_thought}（{activity_reason}）",
                }
            )
        if trigger and decision.should_send:
            decision = decision.model_copy(update={"trigger_type": trigger.type})
        elif trigger and not decision.should_send:
            impulse = build_withheld_impulse(
                trigger_type=trigger.type,
                private_thought=decision.private_thought,
            )
            if impulse:
                self.store.upsert_memory(
                    canonical_user_id,
                    kind="withheld_proactive_impulse",
                    content=impulse.memory_content,
                    source="proactive_tick",
                    confidence=0.76,
                )
                state = apply_withheld_impulse(state, impulse)
                self.store.save_mood_state(canonical_user_id, state)
                runtime = synchronize_life_runtime(self.store, canonical_user_id, state)
        if decision.message:
            decision = decision.model_copy(update={"message": sanitize_chat_text(decision.message)})
        if social_task:
            if decision.should_send:
                decision = decision.model_copy(update={"social_task_id": int(social_task["id"])})
            else:
                self.store.defer_social_task(int(social_task["id"]), due_at=now + timedelta(minutes=45))
        decision = self._attach_sticker(decision, state)
        decision = await self._attach_proactive_image(canonical_user_id, decision, state)
        self.store.save_proactive_event(
            canonical_user_id,
            decision.private_thought,
            decision.should_send,
            decision.platform,
            decision.message_type,
            decision.message,
            decision.sticker_category,
            decision.trigger_type,
            decision.cooldown_minutes,
        )
        if decision.should_send and decision.platform:
            decision = decision.model_copy(
                update={
                    "delivery_id": self.store.queue_outgoing(
                        canonical_user_id,
                        decision.platform,
                        decision.message or "",
                        kind="proactive",
                    )
                }
            )
        return decision

    def confirm_proactive_delivery(self, decision: ProactiveDecision) -> None:
        if decision.delivery_id is None:
            return
        delivered = self.store.mark_outgoing_delivered(decision.delivery_id)
        if not delivered or delivered["status"] != "planned":
            return
        self.store.record_proactive_delivery(decision.canonical_user_id, str(delivered["platform"]))
        state = self.store.get_mood_state(decision.canonical_user_id)
        expressed = apply_expression_after_reply(
            state,
            was_proactive=True,
            sent_image=bool(decision.image_path),
        )
        self.store.save_mood_state(decision.canonical_user_id, expressed)
        synchronize_life_runtime(self.store, decision.canonical_user_id, expressed)
        if decision.social_task_id is not None:
            payload = self.store.social_task_payload(decision.social_task_id)
            self.store.resolve_social_task(decision.social_task_id)
            if decision.trigger_type == "life_share_followup":
                event_id = payload.get("life_event_id")
                if event_id is not None:
                    self.store.mark_life_event_shared(int(event_id))

    def fail_proactive_delivery(self, decision: ProactiveDecision, reason: str) -> None:
        if decision.delivery_id is not None:
            self.store.mark_outgoing_failed(decision.delivery_id, reason)
        if decision.social_task_id is not None:
            self.store.defer_social_task(decision.social_task_id, due_at=utc_now() + timedelta(minutes=20))
        elif decision.message:
            # Mirror the failed-reply path: keep the failed outbox as fact and let a
            # later decision judge whether reaching out again still feels natural.
            now = utc_now()
            self.store.create_social_task(
                decision.canonical_user_id,
                kind="reply_reconsider",
                platform=decision.platform or "qq",
                platform_user_id="",
                payload={
                    "failed_delivery_id": decision.delivery_id,
                    "failed_text": decision.message[:240],
                    "failure_reason": reason[:240],
                },
                reason="刚才那句主动消息没发出去，稍后重新判断还想不想开口，而不是重放原话",
                due_at=now + timedelta(minutes=25),
                expires_at=now + timedelta(hours=6),
            )

    def _create_comfort_followup(self, canonical_user_id: str, message: IncomingMessage) -> int:
        now = utc_now()
        return self.store.create_social_task(
            canonical_user_id,
            kind="comfort_followup",
            platform=message.platform,
            platform_user_id=message.platform_user_id,
            payload={"message_id": message.message_id or "", "event": "user_vulnerable"},
            reason="刚听见你状态不好，晚一点仍会想确认你有没有缓过来",
            due_at=now + timedelta(minutes=75),
            expires_at=now + timedelta(hours=10),
        )

    def _create_promise_followup(self, canonical_user_id: str, message: IncomingMessage) -> int | None:
        delay = _promise_followup_delay(message.text)
        if delay is None:
            return None
        now = utc_now()
        return self.store.create_social_task(
            canonical_user_id,
            kind="promise_followup",
            platform=message.platform,
            platform_user_id=message.platform_user_id,
            payload={"message_id": message.message_id or "", "promise": message.text[:160]},
            reason="他留了一个晚点会说的后续，她先记着但不追着问",
            due_at=now + delay,
            expires_at=now + delay + timedelta(hours=18),
        )

    def _recent_lines(self, canonical_user_id: str) -> list[str]:
        return self._format_recent_rows(self.store.recent_messages(canonical_user_id))

    def _format_recent_rows(self, rows) -> list[str]:
        return [
            self._format_recent_line(
                direction=str(row["direction"]),
                platform=str(row["platform"]),
                text=str(row["text"]),
                sent_at=str(row["sent_at"]),
            )
            for row in rows
        ]

    def _format_recent_dicts(self, rows: list[dict[str, str]]) -> list[str]:
        return [
            self._format_recent_line(
                direction=row["direction"],
                platform=row["platform"],
                text=row["text"],
                sent_at=row["sent_at"],
            )
            for row in rows
        ]

    def _format_recent_line(self, *, direction: str, platform: str, text: str, sent_at: str) -> str:
        who = "你" if direction == "in" else "她"
        if direction == "out":
            text = sanitize_chat_text(text)
        time_hint = relative_chat_time_hint(sent_at)
        return f"[{platform}][{time_hint}] {who}: {text}"

    def _recent_dicts(self, canonical_user_id: str, limit: int = 16) -> list[dict[str, str]]:
        return [
            {
                "direction": str(row["direction"]),
                "platform": str(row["platform"]),
                "text": str(row["text"]),
                "sent_at": str(row["sent_at"]),
            }
            for row in self.store.recent_messages(canonical_user_id, limit=limit)
        ]

    def _attachment_source(self, message: IncomingMessage, attachment: MessageAttachment) -> str:
        if attachment.url:
            return f"attachment:{attachment.url}"
        return f"{message.platform}:{message.message_id or ''}:{attachment.kind}:{attachment.filename or ''}"

    def _self_core_block(self, canonical_user_id: str) -> str | None:
        """Self-core prompt block plus the character's MBTI temperament anchor."""
        core = load_self_core(self.store, canonical_user_id)
        block = core.to_prompt_block() if core else None
        temperament = (
            mbti_temperament_note(self.character_profile) if self.character_profile else None
        )
        if temperament:
            block = f"{block}\n{temperament}" if block else temperament
        return block

    def _self_fact_lines(self, canonical_user_id: str) -> list[str]:
        """Return the small, source-owned ledger for claims about 知栀 herself."""
        facts: list[str] = []
        if self.character_profile:
            for key, value in self.character_profile.identity.items():
                facts.append(f"角色档案/{key}: {value}")
            if self.character_profile.background:
                facts.append(f"角色档案/成长背景: {self.character_profile.background.strip()}")
            facts.extend(
                f"角色档案/日常: {item}" for item in self.character_profile.daily_life[:3]
            )
        for event in self.store.recent_life_events(canonical_user_id, limit=6):
            if event["kind"] != "private_life_event" or event["status"] != "completed":
                continue
            facts.append(f"已发生生活事件: {event['content']}")
        return facts[:12]

    def _latest_life_continuity(self, canonical_user_id: str) -> str | None:
        row = self.store.latest_memory(canonical_user_id, kind="life_continuity")
        return str(row["content"]) if row else None

    def _last_outgoing_tone(self, canonical_user_id: str) -> str | None:
        row = self.store.latest_memory(canonical_user_id, kind="tone_inertia")
        if not row:
            return None
        content = str(row["content"])
        if content.startswith("last_outgoing_tone="):
            return content.split("=", 1)[1].strip() or None
        return None

    def debug_snapshot(
        self,
        canonical_user_id: str,
        *,
        preview_text: str = "",
        platform: str = "qq",
    ) -> dict[str, object]:
        """Return daemon-owned context for local inspection without sending a reply."""
        state = self.store.get_mood_state(canonical_user_id)
        runtime = advance_life_runtime(self.store, canonical_user_id, state)
        recent_rows = self._recent_dicts(canonical_user_id, limit=16)
        recent_lines = self._format_recent_dicts(recent_rows)
        memory_rows = self.store.memories(canonical_user_id, limit=200)
        continuity_hint = build_tone_inertia(state, recent_lines).memory
        memories = []
        self_core_block = self._self_core_block(canonical_user_id) or ""
        prompt_messages: list[dict[str, str]] = []
        if preview_text.strip():
            preview_message = IncomingMessage(
                platform=platform,  # type: ignore[arg-type]
                platform_user_id=canonical_user_id,
                text=preview_text,
            )
            context_package = build_context_package(
                preview_message,
                state,
                recent_rows,
                memory_rows,
                continuity_hint=continuity_hint,
                life_context_override=runtime_prompt_line(runtime),
            )
            memories = context_package.memory_lines
            prompt_messages = reply_prompt(
                preview_message,
                state,
                recent_lines,
                None,
                self.companion_system_prompt,
                memories,
                [
                    "调试预览: 未执行状态更新、附件分析、生活连续性写入或真实发送。",
                ],
                self_core_block=self_core_block,
                context_block=context_package.prompt_block(),
            )
        else:
            context_package = build_context_package(
                IncomingMessage(
                    platform=platform,  # type: ignore[arg-type]
                    platform_user_id=canonical_user_id,
                    text="",
                ),
                state,
                recent_rows,
                memory_rows,
                continuity_hint=continuity_hint,
                life_context_override=runtime_prompt_line(runtime),
            )
            memories = context_package.memory_lines
        return {
            "canonical_user_id": canonical_user_id,
            "state": state.model_dump(mode="json"),
            "life_runtime": runtime.model_dump(mode="json"),
            "recent_life_events": [dict(row) for row in self.store.recent_life_events(canonical_user_id)],
            "recent_social_tasks": [dict(row) for row in self.store.recent_social_tasks(canonical_user_id)],
            "recent_tool_proposals": [dict(row) for row in self.store.recent_tool_proposals(canonical_user_id)],
            "dashboard": _dashboard_view(
                state,
                runtime,
                [dict(row) for row in self.store.upcoming_life_plan_items(canonical_user_id, now=utc_now())],
                [dict(row) for row in self.store.recent_social_tasks(canonical_user_id)],
            ),
            "recent": recent_lines,
            "memories": memories,
            "available_memories": [
                {
                    "kind": str(row["kind"]),
                    "content": str(row["content"]),
                    "confidence": float(row["confidence"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in memory_rows
            ],
            "self_core": self_core_block,
            "context_package": asdict(context_package),
            "preview_prompt": prompt_messages,
        }

    def _is_reply_to_recent_proactive(self, canonical_user_id: str, platform: str) -> bool:
        last_sent = self.store.last_initiated_delivery(canonical_user_id, platform)
        if not last_sent:
            return False
        sent_at = datetime.fromisoformat(last_sent)
        if (utc_now() - sent_at).total_seconds() > 72 * 60 * 60:
            return False
        return self.store.message_count_since(
            canonical_user_id,
            direction="in",
            since_iso=last_sent,
        ) == 0

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
        data.setdefault("image_path", None)
        data.setdefault("trigger_type", None)
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

    async def _attach_proactive_image(
        self,
        canonical_user_id: str,
        decision: ProactiveDecision,
        state: MoodState,
    ) -> ProactiveDecision:
        if not decision.should_send or decision.message_type not in {"image", "text_image"}:
            return decision
        if not self.image_generator or not self.character_profile:
            return decision.model_copy(update={"message_type": "text" if decision.message else "none"})
        if state.relationship_stage not in {"friend", "close_friend", "ambiguous", "lover"}:
            return decision.model_copy(update={"message_type": "text" if decision.message else "none"})
        if state.mood in {"guarded", "hurt"} or state.boundary_level >= 35:
            return decision.model_copy(update={"message_type": "text" if decision.message else "none"})

        estimate = ESTIMATES["image_generation"]
        if self.budget_gate:
            budget_decision = self.budget_gate.check(estimate, automatic=True)
            if not budget_decision.allowed:
                self.store.upsert_memory(
                    canonical_user_id,
                    kind="proactive_image_blocked",
                    content=f"{budget_decision.reason}: {decision.message or decision.private_thought[:80]}",
                    source="proactive_tick",
                    confidence=0.8,
                )
                return decision.model_copy(update={"message_type": "text" if decision.message else "none"})

        kind = "selfie" if state.relationship_stage in {"close_friend", "ambiguous", "lover"} and state.trust >= 55 else "life"
        topic = decision.message or decision.private_thought
        output_path = self.image_output_dir / f"proactive-{canonical_user_id}-{int(utc_now().timestamp())}.png"
        generated = await self.image_generator.generate(
            life_image_prompt(topic, kind=kind, visual_identity_path=self.visual_identity_path),
            output_path=output_path,
        )
        if self.budget_gate:
            self.budget_gate.record(estimate, note=f"proactive_image:{kind}:{topic[:40]}")
        self.store.upsert_memory(
            canonical_user_id,
            kind="generated_image",
            content=f"proactive_{kind}: {topic[:120]}",
            source=str(generated.path),
            confidence=0.82,
        )
        return decision.model_copy(update={"image_path": str(generated.path)})


def _deep_night_afterthought_allowed(state: MoodState, recent_rows: list[dict[str, str]]) -> bool:
    if state.mood in _DEEP_NIGHT_AFTERTHOUGHT_MOODS:
        return True
    recent_text = "\n".join(row.get("text", "") for row in recent_rows[-6:])
    return any(token in recent_text for token in _DEEP_NIGHT_RECENT_ALLOWED_TOKENS)


def _user_claims_image_is_self(text: str) -> bool:
    compact = text.replace(" ", "")
    return any(
        token in compact
        for token in (
            "这是我",
            "是我",
            "我的照片",
            "我照片",
            "我的自拍",
            "我自拍",
            "自拍",
            "本人",
            "我本人",
        )
    )


def afterthought_prompt(mode: str, recent_lines: list[str]) -> str:
    instructions = {
        "quick_continue": (
            "你刚发完上一条，几秒后发现自己还有半句没说完。"
            "补一句很短的自然延续，可以是解释、补充、轻轻改口或顺手接话。"
            "不要重复上一条，不要问新问题。"
        ),
        "topic_drift": (
            "过了一小会儿，刚才的话题在脑子里拐了个小弯。"
            "发一条像真人碎碎念的短消息，可以继续刚才话题、补一个小感受、或突然想到旁枝。"
            "不要像总结，不要像客服，不要强行提问。"
        ),
        "silence_react": (
            "你发完后用户暂时没有回。你注意到了这个空白，但不要控诉或催。"
            "可以轻轻疑惑一下、自己收住、转成一句小念头，或假装刚才那句只是随口补充。"
            "只发一条短消息。"
        ),
    }
    instruction = instructions.get(mode, instructions["quick_continue"])
    return (
        f"{instruction}\n"
        "你是在 QQ/微信私聊里打字。只输出消息内容，不加解释，不写动作旁白。\n"
        "最近聊天中，'你:'只代表用户，'她:'只代表知栀。你只能续写知栀已经发出的意思，"
        "不能假装用户在这之后又说了一句，更不能替用户补一句再回答它。\n"
        "不得反转、接受、否认或评价一个用户尚未说出的立场；例如不能凭空写'我信你'、'那好吧'、"
        "'你想多了'。没有真正的补充就返回空字符串。\n"
        "最多 45 个字；优先陈述，不要连续追问。不得换词复述她上一条已经说过的事实或结论。\n\n"
        f"最近聊天：\n{chr(10).join(recent_lines)}\n"
    )


def _afterthought_repeats_recent(text: str, recent_lines: list[str]) -> bool:
    compact = re.sub(r"[^\w\u4e00-\u9fff]", "", text).lower()
    if len(compact) < 7:
        return False
    recent_outgoing = [line.split("她:", 1)[-1] for line in recent_lines if "她:" in line]
    for earlier in recent_outgoing[-2:]:
        earlier_compact = re.sub(r"[^\w\u4e00-\u9fff]", "", earlier).lower()
        if (
            len(earlier_compact) >= 7
            and compact[:2] == earlier_compact[:2]
            and "刚" in compact
            and "刚" in earlier_compact
        ):
            return True
        if len(earlier_compact) >= 7 and (
            compact in earlier_compact or earlier_compact in compact
        ):
            return True
        if _character_bigram_overlap(compact, earlier_compact) >= 0.58:
            return True
    return False


def _character_bigram_overlap(left: str, right: str) -> float:
    if len(left) < 2 or len(right) < 2:
        return 0.0
    left_pairs = {left[index : index + 2] for index in range(len(left) - 1)}
    right_pairs = {right[index : index + 2] for index in range(len(right) - 1)}
    return len(left_pairs & right_pairs) / max(1, min(len(left_pairs), len(right_pairs)))


def relative_chat_time_hint(sent_at_iso: str, *, now: datetime | None = None) -> str:
    """Human-scale local recency for prompt context."""
    now_local = _to_local(now or utc_now())
    sent_local = _to_local(_parse_datetime(sent_at_iso))
    delta = now_local - sent_local
    if delta.total_seconds() < 0:
        return "刚刚"
    minutes = delta.total_seconds() / 60
    if minutes <= 10:
        return "刚刚"
    if minutes <= 60:
        return "刚才"
    if now_local.date() == sent_local.date():
        if sent_local.hour < 6:
            return "今天凌晨"
        if sent_local.hour < 12:
            return "今天上午"
        if sent_local.hour < 18:
            return "今天下午"
        return "今天晚上"
    if (now_local.date() - sent_local.date()).days == 1:
        if sent_local.hour < 6:
            return "昨晚"
        if sent_local.hour < 12:
            return "昨天上午"
        if sent_local.hour < 18:
            return "昨天下午"
        return "昨晚"
    return "更早"


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ)


def seed_user(
    store: CompanionStore,
    canonical_user_id: str = "geoff",
    initial_state: MoodState | None = None,
) -> None:
    store.map_account("simulator", "geoff", canonical_user_id)
    if not store.has_mood_state(canonical_user_id):
        store.save_mood_state(
            canonical_user_id,
            (initial_state or MoodState()).model_copy(update={"updated_at": utc_now()}),
        )


def _dashboard_view(
    state: MoodState,
    runtime,
    upcoming: list[dict[str, object]],
    social_tasks: list[dict[str, object]],
) -> dict[str, object]:
    """A readable projection for the local visual home; no hidden prompt is exposed."""
    phone_labels = {
        "away": "手机放在一边",
        "notified": "收到了提醒",
        "glanced": "刚瞄到消息",
        "reading": "正在看消息",
        "typing": "正在组织回复",
        "do_not_disturb": "先不看手机",
    }
    mood_labels = {
        "calm": "平静",
        "happy": "心情不错",
        "sulking": "有点别扭",
        "miss_you": "有点想你",
        "worried": "有点挂心",
        "jealous_soft": "小小吃醋",
        "sleepy": "有点困",
        "guarded": "在收着",
        "hurt": "有点受伤",
        "affectionate": "很亲近",
        "curious": "有点好奇",
    }
    reasons = [f"现在在{runtime.activity}", phone_labels.get(runtime.phone_attention, "手机状态未知")]
    if runtime.user_event_effect:
        reasons.append(runtime.user_event_effect)
    if runtime.state_effect:
        reasons.append(runtime.state_effect)
    active_tasks = [task for task in social_tasks if task.get("status") in {"pending", "claimed"}]
    task_labels = {
        "comfort_followup": "还在挂念你之前状态不太好",
        "promise_followup": "记着你说晚点还会说",
        "life_share_followup": "有件小事还没自然说出口",
        "contradiction_followup": "前后说法有点对不上，心里还卡着",
        "reply_reconsider": "有一句想补的话还没发出去",
        "reply_later": "读到了但被手头的事岔开",
    }
    for task in active_tasks[:3]:
        label = task_labels.get(str(task.get("kind")), str(task.get("reason") or "有件小事挂着"))
        reasons.append(label)
    return {
        "mood_label": mood_labels.get(state.mood, state.mood),
        "phone_label": phone_labels.get(runtime.phone_attention, runtime.phone_attention),
        "attention": runtime.attention_demand,
        "activity": runtime.activity,
        "reasons": reasons,
        "next_plan": upcoming,
        "active_task_count": len(active_tasks),
        "relationship_stage": state.relationship_stage,
        # This is deliberately a small, declarative contract. The local visual
        # client may animate it, but it must not infer a new activity from mood
        # or make a planned activity look like a lived fact.
        "scene": _scene_projection(state, runtime, has_open_task=bool(active_tasks)),
    }


def _scene_projection(state: MoodState, runtime, *, has_open_task: bool) -> dict[str, str | bool]:
    """Project daemon-owned life state into a deterministic visual action.

    ``location`` selects a walkable scene anchor; ``action`` selects a pose
    once she arrives. Phone state wins over the base activity because it is the
    most immediate observable change. This is visual-only and never writes back
    into the life ledger.
    """
    activity_scene = {
        "class": ("desk", "study"),
        "study": ("desk", "study"),
        "meal": ("kitchen", "eat"),
        "walk": ("entry", "walk_out"),
        "errand": ("entry", "walk_out"),
        "friends": ("sofa", "social"),
        "unwind": ("sofa", "relax"),
        "routine": ("vanity", "tidy"),
        "sleep": ("bed", "sleep"),
        "quiet": ("window", "gaze"),
        "morning": ("vanity", "tidy"),
        "between": ("rug", "idle"),
    }
    location, action = activity_scene.get(runtime.activity_kind, ("rug", "idle"))
    phone_overrides = {
        "notified": "notice_phone",
        "glanced": "glance_phone",
        "reading": "read_phone",
        "typing": "type_phone",
        "do_not_disturb": "withdraw",
    }
    action = phone_overrides.get(runtime.phone_attention, action)
    mood_expression = {
        "happy": "smile",
        "affectionate": "smile",
        "curious": "spark",
        "miss_you": "soft",
        "worried": "worry",
        "sleepy": "sleepy",
        "sulking": "pout",
        "jealous_soft": "pout",
        "guarded": "guarded",
        "hurt": "hurt",
    }.get(state.mood, "neutral")
    time_of_day = "night" if runtime.activity_kind in {"sleep", "quiet"} else "day"
    return {
        "location": location,
        "action": action,
        "expression": mood_expression,
        "time_of_day": time_of_day,
        "has_notification": runtime.phone_attention in {"notified", "glanced"},
        "has_open_task": has_open_task,
        "activity_kind": runtime.activity_kind,
        "phone_attention": runtime.phone_attention,
    }


def _social_task_trigger(task) -> ProactiveTrigger | None:
    if not task:
        return None
    if task["kind"] == "comfort_followup":
        return ProactiveTrigger(
            "comfort_followup",
            "用户之前状态不太好。现在适合低压力地补一句关心，先确认他有没有缓过来；不要说你在执行提醒，也不要连环追问。",
            92,
            "anxious_reach",
        )
    if task["kind"] == "promise_followup":
        return ProactiveTrigger(
            "promise_followup",
            "用户之前留了一个晚点会说的后续。可以顺着原话题轻轻接近或分享一件自己的小事，不要说“你还没告诉我”、不要催问。",
            74,
            "anxious_reach",
        )
    if task["kind"] == "reply_reconsider":
        return ProactiveTrigger(
            "reply_reconsider",
            "刚才有一句本来想发但投递失败了。现在只重新判断是否还需要自然补一句；不要解释网络失败，不要复读原话，不要把失败消息当成已经说过。",
            70,
            "anxious_reach",
        )
    if task["kind"] == "life_share_followup":
        payload = social_task_payload(task)
        snippet = str(payload.get("content") or "一件今天的小事")[:100]
        return ProactiveTrigger(
            "life_share_followup",
            (
                f"你今天早些时候发生过这件小事但还没自然地跟他说：{snippet}。"
                "可以低压力地分享或联想，不要像汇报任务，不要编造与这件事矛盾的新细节。"
            ),
            66,
            "life_share",
        )
    if task["kind"] == "contradiction_followup":
        payload = social_task_payload(task)
        note = str(payload.get("note") or "前后说法有点对不上")[:120]
        return ProactiveTrigger(
            "contradiction_followup",
            (
                f"他之前注意到说法不一致：{note}。"
                "如果还合适，可以轻描淡写地圆过去或承认记混了；不要质问用户，不要重提争吵。"
            ),
            64,
            "anxious_reach",
        )
    return None


def _promise_followup_delay(text: str) -> timedelta | None:
    normalized = text.replace(" ", "")
    if not any(token in normalized for token in ("告诉你", "跟你说", "跟你讲", "说给你听")):
        return None
    if "明天" in normalized:
        return timedelta(hours=12)
    if any(token in normalized for token in ("晚点", "一会儿", "等会", "等下")):
        return timedelta(hours=3)
    if "回头" in normalized or "之后" in normalized:
        return timedelta(hours=6)
    return None
