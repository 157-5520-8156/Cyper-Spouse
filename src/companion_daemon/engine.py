import json
from datetime import datetime
from pathlib import Path

from companion_daemon.budget import ESTIMATES, BudgetGate
from companion_daemon.character import CharacterProfile
from companion_daemon.conversation import ConversationCore, PromptedConversationCore
from companion_daemon.db import CompanionStore
from companion_daemon.emotion_state import interpret_interaction
from companion_daemon.emotion_reactions import select_character_reaction
from companion_daemon.image_agency import decide_image_agency, image_agency_prompt_line
from companion_daemon.image_generation import OpenAIImageGenerator, life_image_prompt
from companion_daemon.image_prompt_builder import ChatImageMessage, build_image_prompt
from companion_daemon.image_requests import detect_image_request
from companion_daemon.life_continuity import build_life_continuity
from companion_daemon.llm import ChatModel
from companion_daemon.memory import extract_memories, memory_lines
from companion_daemon.models import (
    CompanionReply,
    IncomingMessage,
    MessageAttachment,
    MoodState,
    ProactiveDecision,
)
from companion_daemon.human_rhythm import apply_expression_after_reply
from companion_daemon.mood import (
    platform_context,
    update_mood_for_attachment_insight,
    update_mood_for_message,
)
from companion_daemon.inner_subtext import infer_inner_subtext
from companion_daemon.multimodal import summarize_attachments
from companion_daemon.multimodal_analysis import AttachmentInsight, MultimodalAnalyzer
from companion_daemon.personality_drift import apply_personality_drift, personality_drift_line
from companion_daemon.prompts import proactive_prompt
from companion_daemon.proactive_feedback import apply_proactive_feedback, classify_proactive_feedback
from companion_daemon.proactive_triggers import evaluate_proactive_trigger
from companion_daemon.proactive_waiting import apply_waiting_after_proactive
from companion_daemon.relationship import advance_relationship, key_event_bonus
from companion_daemon.relationship_events import apply_key_relationship_event, detect_key_relationship_event
from companion_daemon.repair_curve import apply_repair_curve
from companion_daemon.reply_segments import split_reply_text
from companion_daemon.reply_stickers import choose_reply_sticker
from companion_daemon.sanitize import sanitize_chat_text
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
        self.conversation_core = conversation_core or PromptedConversationCore(
            model,
            companion_system_prompt,
            example_messages=(
                character_profile.example_messages if character_profile else None
            ),
            rewrite_model=rewrite_model,
        )

    async def handle_message(
        self,
        message: IncomingMessage,
        *,
        skip_reply: bool = False,
        mark_unread: bool = True,
        context_hint: str | None = None,
    ) -> CompanionReply | None:
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        previous_state = self.store.get_mood_state(canonical_user_id)
        recent_dicts_before = self._recent_dicts(canonical_user_id, limit=16)
        pending_own_question = last_unanswered_own_question(recent_dicts_before)
        context = platform_context(previous_state, message)
        event = interpret_interaction(message, previous_state)
        next_state = update_mood_for_message(previous_state, message)
        key_event = detect_key_relationship_event(message)
        next_state = apply_key_relationship_event(next_state, key_event)
        next_state = apply_repair_curve(next_state, message_text=message.text)
        next_state = apply_personality_drift(next_state)
        proactive_feedback = None
        if self._is_reply_to_recent_proactive(canonical_user_id, message.platform):
            proactive_feedback = classify_proactive_feedback(message.text)
            next_state = apply_proactive_feedback(next_state, proactive_feedback)
        question_response = classify_response_to_own_question(message.text, pending_own_question)
        next_state = apply_question_response(next_state, question_response)

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
        if context_hint:
            attachment_lines.append(context_hint)
        recent_lines = self._recent_lines(canonical_user_id)
        tone_inertia = build_tone_inertia(next_state, recent_lines)
        attachment_lines.append(tone_inertia.prompt_line)
        attachment_lines.append(personality_drift_line(next_state))
        life_continuity = build_life_continuity(
            next_state,
            previous_content=self._latest_life_continuity(canonical_user_id),
        )
        attachment_lines.append(life_continuity.prompt_line)
        self.store.upsert_memory(
            canonical_user_id,
            kind="life_continuity",
            content=life_continuity.content,
            source=f"{message.platform}:{message.message_id or 'turn'}",
            confidence=0.72,
        )
        if key_event:
            attachment_lines.append(key_event.prompt_line)
            self.store.upsert_memory(
                canonical_user_id,
                kind="key_relationship_event",
                content=key_event.memory,
                source=f"{message.platform}:{message.message_id or 'turn'}",
                confidence=0.86,
            )
        subtext = infer_inner_subtext(next_state)
        if subtext:
            attachment_lines.append(subtext.prompt_line)
            self.store.upsert_memory(
                canonical_user_id,
                kind="inner_subtext",
                content=subtext.memory,
                source=f"{message.platform}:{message.message_id or 'turn'}",
                confidence=0.74,
            )
        if proactive_feedback:
            attachment_lines.append(proactive_feedback.prompt_line)
            self.store.upsert_memory(
                canonical_user_id,
                kind="proactive_response",
                content=proactive_feedback.memory_content,
                source=f"{message.platform}:{message.message_id or 'turn'}",
                confidence=0.82,
            )
        if question_response:
            attachment_lines.append(question_response.prompt_line)
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

        if skip_reply:
            return None

        text = await self.conversation_core.reply(
            message,
            next_state,
            recent_lines,
            context,
            memory_lines(self.store.memories(canonical_user_id)),
            attachment_lines,
        )
        self.store.save_outgoing(canonical_user_id, message.platform, text)
        expressed_state = apply_expression_after_reply(
            next_state,
            was_proactive=False,
            sent_image=bool(generated_image_path),
        )
        text_parts = split_reply_text(text, expressed_state)
        self.store.save_mood_state(canonical_user_id, expressed_state)
        self.store.upsert_memory(
            canonical_user_id,
            kind="tone_inertia",
            content=f"last_outgoing_tone={classify_outgoing_tone(text, expressed_state)}",
            source=f"{message.platform}:outgoing",
            confidence=0.65,
        )
        suggested_reaction = select_character_reaction(message.text, next_state)
        sticker = choose_reply_sticker(
            self.stickers,
            next_state,
            message,
            suggested_reaction=suggested_reaction.reaction_id if suggested_reaction else None,
        )
        if generated_image_path:
            sticker = None
        return CompanionReply(
            canonical_user_id=canonical_user_id,
            mood=expressed_state.mood,
            text=text,
            text_parts=text_parts,
            platform_context=context,
            sticker_path=str(sticker.path) if sticker else None,
            image_path=str(generated_image_path) if generated_image_path else None,
            suggested_reaction=(
                suggested_reaction.reaction_id if suggested_reaction and suggested_reaction.probability >= 0.25 else None
            ),
        )

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

    async def proactive_tick(self, canonical_user_id: str) -> ProactiveDecision:
        state = self.store.get_mood_state(canonical_user_id)
        last_sent = self.store.last_proactive_delivery(canonical_user_id, "qq")
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
        recent_lines = self._recent_lines(canonical_user_id)
        recent_rows = self._recent_dicts(canonical_user_id, limit=16)
        trigger = evaluate_proactive_trigger(
            state=state,
            recent_messages=recent_rows,
            trigger_history=self.store.recent_proactive_trigger_history(canonical_user_id),
            now=utc_now(),
        )
        raw = await self.model.complete(
            proactive_prompt(state, recent_lines, self.companion_system_prompt, trigger),
            temperature=0.7,
        )
        decision = self._parse_decision(canonical_user_id, raw, state)
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
        if decision.message:
            decision = decision.model_copy(update={"message": sanitize_chat_text(decision.message)})
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
        if decision.should_send and decision.platform and decision.message:
            self.store.save_outgoing(canonical_user_id, decision.platform, decision.message)
        if decision.should_send:
            expressed_state = apply_expression_after_reply(
                state,
                was_proactive=True,
                sent_image=bool(decision.image_path),
            )
            self.store.save_mood_state(canonical_user_id, expressed_state)
        return decision

    def _recent_lines(self, canonical_user_id: str) -> list[str]:
        lines = []
        for row in self.store.recent_messages(canonical_user_id):
            who = "你" if row["direction"] == "in" else "她"
            lines.append(f"[{row['platform']}] {who}: {row['text']}")
        return lines

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

    def _latest_life_continuity(self, canonical_user_id: str) -> str | None:
        row = self.store.latest_memory(canonical_user_id, kind="life_continuity")
        return str(row["content"]) if row else None

    def _is_reply_to_recent_proactive(self, canonical_user_id: str, platform: str) -> bool:
        last_sent = self.store.last_proactive_delivery(canonical_user_id, platform)
        if not last_sent:
            return False
        sent_at = datetime.fromisoformat(last_sent)
        if (utc_now() - sent_at).total_seconds() > 12 * 60 * 60:
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
