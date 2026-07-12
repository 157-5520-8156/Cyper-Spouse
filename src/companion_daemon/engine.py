import asyncio
from hashlib import sha256
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from companion_daemon.attachment_cache import AttachmentCache
from companion_daemon.budget import ESTIMATES, BudgetGate
from companion_daemon.character import CharacterProfile
from companion_daemon.context_assembler import ContextAssembler
from companion_daemon.conversation import ConversationCore, PromptedConversationCore
from companion_daemon.context_orchestrator import build_context_package
from companion_daemon.calendar_ledger import calendar_context_for_message, calendar_ledger
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
from companion_daemon.memory import extract_memories, is_durable_user_fact
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
from companion_daemon.reply_decision import classify_message
from companion_daemon.proactive_waiting import apply_waiting_after_proactive
from companion_daemon.relationship import advance_relationship, key_event_bonus
from companion_daemon.relationship_events import apply_key_relationship_event, detect_key_relationship_event
from companion_daemon.repair_curve import apply_repair_curve, serious_repair_key_event
from companion_daemon.reply_segments import split_reply_text
from companion_daemon.reply_stickers import choose_reply_sticker
from companion_daemon.sanitize import sanitize_chat_text, sanitize_world_chat_text
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
    PendingQuestion,
    apply_question_response,
    apply_unanswered_question_waiting,
    classify_response_to_own_question,
    last_unanswered_own_question,
)
from companion_daemon.withheld_impulse import apply_withheld_impulse, build_withheld_impulse
from companion_daemon.turns import TurnCommit, build_turn_plan
from companion_daemon.world import ConcurrencyConflict, WorldError, WorldKernel, parse_reply_candidate
from companion_daemon.world_affect import public_mood
from companion_daemon.world_behavior import WorldBehaviorPolicy
from companion_daemon.world_interaction_rules import classify_repair_appraisal
from companion_daemon.world_conversation import (
    affect_reply_violation,
    asks_for_source_detail,
    best_matching_grounded_source,
    build_safe_failure_candidate,
    classify_world_query,
    conversation_fact_candidate,
    denies_known_npc_interaction,
    human_reply_contract_violation,
    only_echoes_user_message,
    only_recites_irrelevant_sources,
    only_repeats_claimed_sources,
    repeats_recent_companion_reply,
    reply_proposes_new_discomfort,
)
from companion_daemon.world_media import WorldMediaPolicy
from companion_daemon.world_reply_audit import WorldReplyAuditor

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


def _unexpired_iso(value: object) -> bool:
    try:
        expires_at = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        return False
    return expires_at.astimezone(UTC) > utc_now().astimezone(UTC)


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
        world_kernel: WorldKernel | None = None,
        world_id: str | None = None,
        world_grounding_audit_model: ChatModel | None = None,
        attachment_cache: AttachmentCache | None = None,
        attachment_fetcher: Callable[[str], Awaitable[bytes]] | None = None,
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
        self.world_kernel = world_kernel
        self.world_id = world_id
        self.world_behavior_policy = WorldBehaviorPolicy()
        self.context_assembler = ContextAssembler()
        self.world_media_policy = WorldMediaPolicy()
        self.world_grounding_audit_model = world_grounding_audit_model
        self.attachment_cache = attachment_cache
        self.attachment_fetcher = attachment_fetcher or self._fetch_attachment
        self.world_reply_auditor = WorldReplyAuditor()
        # Character-card examples are style references already included in the
        # system prompt. Replaying them as fake chat history duplicates tokens
        # and makes concrete example details look like reusable live facts.
        self.conversation_core = conversation_core or PromptedConversationCore(
            model,
            companion_system_prompt,
            rewrite_model=rewrite_model,
        )

    @staticmethod
    async def _fetch_attachment(url: str) -> bytes:
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError("attachment URL must use http or https")
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 25 * 1024 * 1024:
                raise ValueError("attachment exceeds 25 MiB cache limit")
            return response.content

    async def _analyze_world_attachments(
        self, canonical_user_id: str, message: IncomingMessage
    ) -> list[dict[str, object]]:
        """Settle attachment analysis as sourced External Results before prompting."""
        if not self.world_kernel or not self.world_id:
            return []
        insights: list[dict[str, object]] = []
        message_id = str(message.message_id or self._world_message_id(message))
        world_user_id = self._world_user_id(canonical_user_id)
        for index, attachment in enumerate(message.attachments):
            source_fingerprint = sha256(
                f"{attachment.kind}|{attachment.url or ''}|{attachment.filename or ''}|{attachment.size or ''}".encode()
            ).hexdigest()
            digest = sha256(
                f"{message_id}|{index}|{attachment.kind}|{attachment.url or ''}|{attachment.filename or ''}".encode()
            ).hexdigest()[:20]
            action_id = f"attachment_analysis:{digest}"
            existing = self.world_kernel.snapshot(self.world_id)["actions"].get(action_id)
            if isinstance(existing, dict) and existing.get("status") == "delivered":
                result = existing.get("result")
                if isinstance(result, dict):
                    insights.append(dict(result))
                continue
            self._submit_world_with_retry({
                "type": "schedule_action", "world_id": self.world_id,
                "action_id": action_id, "kind": "attachment_analysis",
                "expires_at": (self._world_logical_now() + timedelta(minutes=10)).isoformat(),
                "payload": {
                    "source_message_id": message_id, "attachment_index": index,
                    "kind": attachment.kind, "source_fingerprint": source_fingerprint,
                    "user_id": world_user_id,
                },
                "idempotency_key": f"schedule:{action_id}",
            })
            self._submit_world_with_retry({
                "type": "claim_external_action", "world_id": self.world_id,
                "action_id": action_id,
                "lease_expires_observed_at": (utc_now() + timedelta(minutes=3)).isoformat(),
                "idempotency_key": f"claim:{action_id}",
            })
            prior_result: dict[str, object] | None = None
            for prior_id, raw_prior in self.world_kernel.snapshot(self.world_id)["actions"].items():
                if prior_id == action_id or not isinstance(raw_prior, dict):
                    continue
                prior_payload = raw_prior.get("payload", {})
                candidate = raw_prior.get("result", {})
                if (
                    raw_prior.get("kind") == "attachment_analysis"
                    and raw_prior.get("status") == "delivered"
                    and isinstance(prior_payload, dict)
                    and prior_payload.get("source_fingerprint") == source_fingerprint
                    and prior_payload.get("user_id") == world_user_id
                    and isinstance(candidate, dict)
                    and str(candidate.get("summary") or "")
                    and _unexpired_iso(candidate.get("analysis_expires_at"))
                ):
                    prior_result = candidate
            if prior_result is not None:
                cache = dict(prior_result.get("cache", {})) if isinstance(prior_result.get("cache"), dict) else {}
                cache["analysis_hit"] = True
                result = {
                    **prior_result,
                    "source_message_id": message_id,
                    "attachment_index": index,
                    "cache": cache,
                }
                self._submit_world_with_retry({
                    "type": "record_external_result", "world_id": self.world_id,
                    "action_id": action_id, "result": result,
                    "idempotency_key": f"settle:{action_id}",
                })
                insights.append(result)
                continue
            cache_result: dict[str, object] = {
                "status": "not_configured", "retention_days": 30,
            }
            if self.attachment_cache and attachment.url:
                try:
                    content = await self.attachment_fetcher(attachment.url)
                    cached = self.attachment_cache.store(
                        user_id=canonical_user_id,
                        attachment_id=f"{message_id}:{index}",
                        content=content,
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        now=utc_now(),
                    )
                    cache_result = {
                        "status": "stored", "retention_days": 30,
                        "expires_at": cached.expires_at.isoformat(),
                    }
                except Exception as exc:
                    logger.warning("attachment cache degraded for %s: %s", action_id, type(exc).__name__)
                    cache_result = {
                        "status": "failed", "retention_days": 30,
                        "reason": type(exc).__name__,
                    }
            try:
                insight = await self.multimodal_analyzer.analyze(attachment)
                result: dict[str, object] = {
                    "status": "delivered", "source_message_id": message_id,
                    "attachment_index": index, "kind": insight.kind,
                    "summary": insight.summary[:2000], "confidence": insight.confidence,
                    "cache": cache_result,
                    "analysis_expires_at": (utc_now() + timedelta(days=30)).isoformat(),
                }
            except Exception as exc:
                self._submit_world_with_retry({
                    "type": "record_external_result", "world_id": self.world_id,
                    "action_id": action_id,
                    "result": {"status": "failed", "reason": type(exc).__name__},
                    "idempotency_key": f"settle:{action_id}",
                })
                continue
            self._submit_world_with_retry({
                "type": "record_external_result", "world_id": self.world_id,
                "action_id": action_id, "result": result,
                "idempotency_key": f"settle:{action_id}",
            })
            insights.append(result)
        return insights

    @staticmethod
    def _world_message_id(message: IncomingMessage) -> str:
        return message.message_id or sha256(
            f"{message.platform}:{message.platform_user_id}:{message.sent_at.isoformat()}:{message.text}".encode()
        ).hexdigest()[:24]

    @staticmethod
    def _world_user_id(canonical_user_id: str) -> str:
        return f"user:{canonical_user_id}"

    @staticmethod
    def _current_world_facts(snapshot: dict[str, object]) -> list[dict[str, object]]:
        facts = snapshot.get("facts", {})
        if not isinstance(facts, dict):
            return []
        return [
            item for item in facts.values()
            if isinstance(item, dict)
            and str(item.get("status") or "current") in {"current", "confirmed"}
        ]

    @staticmethod
    def _world_reply_question(text: str) -> str | None:
        """Extract a bounded, observable question without creating a fact."""
        cleaned = text.strip()
        if not cleaned:
            return None
        looks_like_question = cleaned.endswith(("?", "？"))
        return cleaned[:240] if looks_like_question else None

    def _ensure_world_user(self, canonical_user_id: str) -> str:
        if not self.world_kernel or not self.world_id:
            raise RuntimeError("world user requested outside world mode")
        user_id = self._world_user_id(canonical_user_id)
        if user_id in self.world_kernel.snapshot(self.world_id)["entities"]:
            return user_id
        self._submit_world_with_retry(
            {
                "type": "register_user", "world_id": self.world_id,
                "user_id": user_id, "name": canonical_user_id,
                "idempotency_key": f"register-user:{user_id}",
            }
        )
        return user_id

    def _record_world_input(self, message: IncomingMessage, canonical_user_id: str) -> None:
        if not self.world_kernel or not self.world_id:
            return
        user_id = self._ensure_world_user(canonical_user_id)
        key = self._world_message_id(message)
        self._submit_world_with_retry(
            {
                "type": "observe_user_message",
                "world_id": self.world_id,
                "message_id": key,
                "user_id": user_id,
                "text": message.text,
                "attachments": [item.model_dump(mode="json") for item in message.attachments],
                "sent_at": message.sent_at.isoformat(),
                "source": f"{message.platform}:incoming",
                "idempotency_key": f"incoming:{key}",
            }
        )
        # The extractor is only a parser in world mode.  A direct user
        # statement may become a fact with its message as provenance; no
        # legacy memory row is written and no model-proposed fact is accepted.
        for extracted in extract_memories(message):
            if not is_durable_user_fact(extracted):
                continue
            digest = sha256(
                f"{user_id}|{extracted.kind}|{extracted.content}".encode()
            ).hexdigest()[:20]
            self._submit_world_with_retry(
                {
                    "type": "confirm_fact", "world_id": self.world_id,
                    "fact_id": f"user-fact:{digest}", "subject": user_id,
                    "value": extracted.content, "source": f"user_message:{key}",
                    "conflict_key": extracted.fact_key or extracted.kind,
                    "idempotency_key": f"user-fact:{digest}",
                }
            )
        conversation_fact = conversation_fact_candidate(message.text)
        if conversation_fact:
            digest = sha256(
                f"{user_id}|conversation|{conversation_fact}".encode()
            ).hexdigest()[:20]
            self._submit_world_with_retry(
                {
                    "type": "confirm_fact",
                    "world_id": self.world_id,
                    "fact_id": f"user-conversation:{digest}",
                    "subject": user_id,
                    "value": conversation_fact,
                    "source": f"user_message:{key}",
                    "scope": "conversation",
                    "source_message_id": key,
                    "idempotency_key": f"user-conversation:{digest}",
                }
            )

    def _world_turn_already_observed(self, message_id: str) -> bool:
        """Whether an adapter message already entered the durable world."""
        assert self.world_kernel and self.world_id
        state = self.world_kernel.snapshot(self.world_id)
        return any(
            isinstance(item, dict)
            and item.get("direction") == "in"
            and str(item.get("message_id") or "") == message_id
            for item in state.get("recent_messages", [])
        )

    def _submit_world_with_retry(self, command: dict[str, object]):
        if not self.world_kernel or not self.world_id:
            return
        for _ in range(3):
            revision = self.world_kernel.revision(self.world_id)
            try:
                return self.world_kernel.submit(command, expected_revision=revision)
            except ConcurrencyConflict:
                continue
        raise ConcurrencyConflict(f"world command conflicted repeatedly: {command.get('type')}")

    def recover_input_merge(self, merge_key: str) -> tuple[IncomingMessage, ...]:
        """Recover a bounded unflushed adapter batch after process restart."""
        if not self.world_kernel or not self.world_id:
            return ()
        raw = self.world_kernel.snapshot(self.world_id).get("input_merges", {}).get(merge_key, {})
        if not isinstance(raw, dict) or raw.get("status") != "pending":
            return ()
        updated_at = str(raw.get("updated_at") or "")
        if updated_at and utc_now() - datetime.fromisoformat(updated_at) > timedelta(minutes=10):
            return ()
        recovered: list[IncomingMessage] = []
        for item in raw.get("messages", [])[-6:]:
            if isinstance(item, dict):
                recovered.append(IncomingMessage.model_validate(item))
        return tuple(recovered)

    def record_input_merge_candidate(
        self, merge_key: str, message: IncomingMessage, decision, *, pending_count: int
    ) -> None:
        if not self.world_kernel or not self.world_id:
            return
        effective = message.model_copy(
            update={"message_id": message.message_id or self._world_message_id(message)}
        )
        self._submit_world_with_retry(
            {
                "type": "observe_input_merge_candidate",
                "world_id": self.world_id,
                "merge_key": merge_key,
                "message": effective.model_dump(mode="json"),
                "pending_count": pending_count,
                "wait_seconds": decision.wait_seconds,
                "reason": decision.reason,
                "idempotency_key": f"input-merge:{merge_key}:{effective.message_id}",
            }
        )

    def settle_input_merge(self, merge_key: str, messages: tuple[IncomingMessage, ...]) -> None:
        if not self.world_kernel or not self.world_id or not messages:
            return
        message_ids = [str(item.message_id or self._world_message_id(item)) for item in messages]
        self._submit_world_with_retry(
            {
                "type": "settle_input_merge",
                "world_id": self.world_id,
                "merge_key": merge_key,
                "message_ids": message_ids,
                "merged_message_id": message_ids[-1],
                "idempotency_key": f"input-merge-settle:{merge_key}:{message_ids[-1]}",
            }
        )

    def _begin_world_model_call(self, *, purpose: str, causation: str) -> str:
        digest = sha256(f"{purpose}|{causation}".encode("utf-8")).hexdigest()[:20]
        action_id = f"model_call:{digest}"
        self._submit_world_with_retry({"type": "schedule_action", "world_id": self.world_id, "action_id": action_id, "kind": "model_call", "expires_at": (self._world_logical_now() + timedelta(minutes=5)).isoformat(), "payload": {"purpose": purpose, "causation": causation}, "idempotency_key": f"schedule:{action_id}"})
        self._submit_world_with_retry(
            {
                "type": "claim_external_action",
                "world_id": self.world_id,
                "action_id": action_id,
                "lease_expires_observed_at": (utc_now() + timedelta(minutes=2)).isoformat(),
                "idempotency_key": f"claim:{action_id}",
            }
        )
        return action_id

    def _record_world_model_output(self, *, purpose: str, causation: str, content: str, action_id: str) -> None:
        """Persist a model return as non-factual audit input before using it."""
        if not self.world_kernel or not self.world_id:
            return
        digest = sha256(f"{purpose}|{causation}|{content}".encode("utf-8")).hexdigest()[:20]
        proposal_id = f"model:{purpose}:{digest}"
        self._submit_world_with_retry(
            {
                "type": "record_external_result",
                "world_id": self.world_id,
                "action_id": action_id,
                "result": {"kind": "model_call", "status": "delivered", "output_hash": sha256(content.encode("utf-8")).hexdigest()},
                "idempotency_key": f"settle:{action_id}",
            }
        )
        if not content.strip():
            return
        self._submit_world_with_retry(
            {
                "type": "record_model_output",
                "world_id": self.world_id,
                "proposal_id": proposal_id,
                "purpose": purpose,
                "content": content,
                "action_id": action_id,
                "causation_id": causation,
                "idempotency_key": f"model-output:{proposal_id}",
            }
        )

    def _fail_world_model_call(self, action_id: str, reason: str) -> None:
        self._submit_world_with_retry({"type": "record_external_result", "world_id": self.world_id, "action_id": action_id, "result": {"kind": "model_call", "status": "failed", "reason": reason[:300]}, "idempotency_key": f"fail:{action_id}"})

    async def _audit_world_reply(
        self,
        *,
        purpose: str,
        causation: str,
        user_text: str,
        reply_text: str,
        grounding_context: dict[str, object],
    ) -> None:
        if not self.world_grounding_audit_model:
            return
        action_id = self._begin_world_model_call(purpose=purpose, causation=causation)
        try:
            raw, audit = await self.world_reply_auditor.evaluate(
                self.world_grounding_audit_model,
                user_text=user_text,
                reply_text=reply_text,
                grounding_context=grounding_context,
            )
        except Exception as exc:
            self._fail_world_model_call(action_id, str(exc))
            raise WorldError("independent grounding audit failed") from exc
        self._record_world_model_output(
            purpose=purpose,
            causation=causation,
            content=raw,
            action_id=action_id,
        )
        if not audit.supported:
            spans = "；".join(audit.unsupported_spans) or "未定位片段"
            raise WorldError(f"independent grounding audit rejected: {spans}; {audit.reason}")

    def _world_logical_now(self) -> datetime:
        if not self.world_kernel or not self.world_id:
            raise RuntimeError("world logical time requested outside world mode")
        return datetime.fromisoformat(
            str(self.world_kernel.snapshot(self.world_id)["clock"]["logical_at"])
        )

    async def _maybe_generate_world_image(
        self, *, user_id: str, message: IncomingMessage
    ) -> tuple[str | None, str | None, str | None]:
        """Generate a requested image only through media generation/delivery actions."""
        assert self.world_kernel and self.world_id
        request = detect_image_request(message.text)
        if not request.triggered:
            return None, None, None
        snapshot = self.world_kernel.snapshot(self.world_id)
        decision = self.world_media_policy.image_decision(
            snapshot,
            user_id=user_id,
            request=request,
            user_text=message.text,
        )
        request_id = "media:" + sha256(
            f"{user_id}|{message.message_id}|{request.type}|{request.directive}".encode("utf-8")
        ).hexdigest()[:20]
        existing = snapshot.get("media", {}).get(request_id, {})
        if isinstance(existing, dict):
            if existing.get("status") in {"generated", "shared"} and existing.get("artifact_path"):
                return str(existing["artifact_path"]), f"media-delivery:{request_id}", "existing_media_request"
            if existing.get("status") in {"requested", "generation_failed", "delivery_failed", "rejected"}:
                return None, None, f"existing_media_{existing['status']}"
        if not decision.allowed:
            self._submit_world_with_retry(
                {
                    "type": "reject_media_request", "world_id": self.world_id,
                    "request_id": request_id, "user_id": user_id, "reason": decision.reason,
                    "rule_version": self.world_media_policy.RULE_VERSION,
                    "idempotency_key": f"media-reject:{request_id}",
                }
            )
            return None, None, decision.reason
        if decision.requires_deliberation:
            deliberation = snapshot.get("last_deliberation", {})
            stance = (
                str(deliberation.get("stance") or "")
                if isinstance(deliberation, dict)
                else ""
            )
            if stance != "comply":
                self._submit_world_with_retry(
                    {
                        "type": "reject_media_request", "world_id": self.world_id,
                        "request_id": request_id, "user_id": user_id,
                        "reason": f"deliberation:{stance or 'defer'}:{decision.reason}",
                        "rule_version": self.world_media_policy.RULE_VERSION,
                        "idempotency_key": f"media-reject:{request_id}",
                    }
                )
                return None, None, decision.reason
        self._submit_world_with_retry(
            {
                "type": "request_media", "world_id": self.world_id,
                "request_id": request_id, "user_id": user_id, "media_kind": decision.kind,
                "topic": decision.prompt_topic, "reason": decision.reason,
                "rule_version": self.world_media_policy.RULE_VERSION,
                "idempotency_key": f"media-request:{request_id}",
            }
        )
        action_id = f"media-generation:{request_id}"
        if not self.image_generator:
            self._submit_world_with_retry(
                {
                    "type": "record_external_result", "world_id": self.world_id, "action_id": action_id,
                    "result": {"kind": "media_generation", "status": "failed", "reason": "image_generator_unavailable"},
                    "idempotency_key": f"media-generation-failed:{request_id}",
                }
            )
            return None, None, "image_generator_unavailable"
        estimate = ESTIMATES["image_generation"]
        if self.budget_gate and not self.budget_gate.check(estimate, automatic=True).allowed:
            self._submit_world_with_retry(
                {
                    "type": "record_external_result", "world_id": self.world_id, "action_id": action_id,
                    "result": {"kind": "media_generation", "status": "failed", "reason": "budget_gate_blocked"},
                    "idempotency_key": f"media-generation-budget:{request_id}",
                }
            )
            return None, None, "budget_gate_blocked"
        prompt = life_image_prompt(
            decision.prompt_topic,
            kind="selfie" if decision.kind == "selfie" else "life",
            visual_identity_path=self.visual_identity_path,
        )
        output_path = self.image_output_dir / f"world-{request_id}.png"
        try:
            generated = await self.image_generator.generate(prompt, output_path=output_path)
        except Exception as exc:
            self._submit_world_with_retry(
                {
                    "type": "record_external_result", "world_id": self.world_id, "action_id": action_id,
                    "result": {"kind": "media_generation", "status": "failed", "reason": str(exc)[:300]},
                    "idempotency_key": f"media-generation-failed:{request_id}",
                }
            )
            return None, None, "media_generation_failed"
        if self.budget_gate:
            self.budget_gate.record(estimate, note=f"world_media:{decision.kind}")
        artifact_path = str(generated.path)
        self._submit_world_with_retry(
            {
                "type": "record_external_result", "world_id": self.world_id, "action_id": action_id,
                "result": {
                    "kind": "media_generation", "status": "delivered", "artifact_path": artifact_path,
                    "artifact_hash": sha256(generated.path.read_bytes()).hexdigest(),
                },
                "idempotency_key": f"media-generated:{request_id}",
            }
        )
        self._submit_world_with_retry(
            {
                "type": "schedule_media_delivery", "world_id": self.world_id, "request_id": request_id,
                "outbound_kind": "reply",
                "idempotency_key": f"media-delivery:{request_id}",
            }
        )
        return artifact_path, f"media-delivery:{request_id}", decision.reason

    def _maybe_schedule_world_sticker(
        self, *, message: IncomingMessage, appraisal: str
    ) -> tuple[str | None, str | None]:
        """Select a local sticker from world expression state, then schedule delivery."""
        if not self.world_kernel or not self.world_id or not self.stickers or not message.message_id:
            return None, None
        snapshot = self.world_kernel.snapshot(self.world_id)
        intent = self.world_media_policy.sticker_intent(snapshot, appraisal=appraisal)
        if not intent:
            return None, None
        sticker = next((item for item in self.stickers.stickers if item.intent == intent), None)
        if not sticker:
            return None, None
        self._submit_world_with_retry(
            {
                "type": "schedule_sticker_delivery", "world_id": self.world_id,
                "sticker_id": sticker.id, "sticker_path": str(sticker.path), "intent": intent,
                "causation_id": message.message_id, "rule_version": self.world_media_policy.RULE_VERSION,
                "outbound_kind": "reply",
                "idempotency_key": f"sticker:{message.message_id}:{intent}",
            }
        )
        return str(sticker.path), f"sticker-delivery:{message.message_id}"

    def begin_world_typing(self, message: IncomingMessage, *, reason: str = "composing_reply") -> None:
        """Record an observable typing transition without touching legacy mood state."""
        if not self.world_kernel or not self.world_id or not message.message_id:
            return
        self._submit_world_with_retry(
            {
                "type": "set_typing_state", "world_id": self.world_id,
                "message_id": message.message_id, "typing": "started", "reason": reason,
                "idempotency_key": f"typing-start:{message.message_id}",
            }
        )

    def stop_world_typing(self, message: IncomingMessage, *, reason: str) -> None:
        """Set typing idle after a send attempt, regardless of its delivery result."""
        if not self.world_kernel or not self.world_id or not message.message_id:
            return
        snapshot = self.world_kernel.snapshot(self.world_id)
        communication = snapshot.get("communication", {})
        if not isinstance(communication, dict) or communication.get("typing") != "started":
            return
        self._submit_world_with_retry(
            {
                "type": "set_typing_state", "world_id": self.world_id,
                "message_id": message.message_id, "typing": "stopped", "reason": reason,
                "idempotency_key": f"typing-stop:{message.message_id}:{reason}",
            }
        )

    async def handle_message(
        self,
        message: IncomingMessage,
        *,
        skip_reply: bool = False,
        mark_unread: bool = True,
        context_hint: str | None = None,
        defer_delivery: bool = False,
        resume_action_id: str | None = None,
    ) -> CompanionReply | None:
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        if self.world_kernel and self.world_id:
            self.world_kernel.recover_interrupted_outgoing_deliveries(self.world_id)
            message = message.model_copy(update={"message_id": self._world_message_id(message)})
            if not resume_action_id and self._world_turn_already_observed(str(message.message_id)):
                return None
            self._record_world_input(message, canonical_user_id)
            turn_id = str(message.message_id)
            turns = self.world_kernel.snapshot(self.world_id).get("turns", {})
            if turn_id not in turns and not self.world_kernel.claim_message_turn(
                self.world_id, turn_id
            ):
                return None
            try:
                reply = await self._handle_world_message(
                    canonical_user_id,
                    message,
                    skip_reply=skip_reply,
                    defer_delivery=defer_delivery,
                    resume_action_id=resume_action_id,
                )
            except Exception as exc:
                try:
                    self.world_kernel.settle_turn(
                        self.world_id, str(message.message_id), status="failed",
                        reason=type(exc).__name__,
                        expected_revision=self.world_kernel.revision(self.world_id),
                    )
                except Exception:
                    logger.exception("failed to settle world turn after exception")
                raise
            awaiting_delivery = reply is not None and defer_delivery
            self.world_kernel.settle_turn(
                self.world_id, str(message.message_id),
                status="deferred" if reply is None or awaiting_delivery else "delivered",
                reason=(
                    "awaiting_external_delivery"
                    if awaiting_delivery
                    else "communication_deferred"
                    if reply is None
                    else "reply_delivered"
                ),
                expected_revision=self.world_kernel.revision(self.world_id),
            )
            return reply
        # A new user turn means a previously planned check-in has been overtaken by reality.
        self.store.cancel_active_social_tasks(canonical_user_id, kind="comfort_followup")
        self.store.cancel_active_social_tasks(canonical_user_id, kind="promise_followup")
        self.store.cancel_active_social_tasks(canonical_user_id, kind="contradiction_followup")
        self.store.cancel_active_social_tasks(canonical_user_id, kind="withheld_impulse")
        # A real new turn, regardless of its platform, overtakes any delayed
        # continuation.  This is the shared cancellation point used by QQ
        # official, NapCat, and future adapters.
        self.store.cancel_active_social_tasks(canonical_user_id, kind="conversation_pulse")
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
            if is_durable_user_fact(extracted):
                self.store.record_fact_observation(
                    canonical_user_id,
                    subject="user",
                    predicate=extracted.kind,
                    value=extracted.content,
                    source=f"{message.platform}:{message.message_id or ''}",
                    confidence=extracted.confidence,
                    fact_key=extracted.fact_key,
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
            # Silence is still a behavioral outcome.  Without this trace, state
            # changes caused by an unread/merge decision become invisible to
            # the audit timeline.
            self.store.create_turn_trace(
                canonical_user_id,
                appraisal=event.kind,
                expression_policy="暂不回复，保留未读并等待合适时机。",
                allowed_facts=[],
                short_lived_constraint=subtext.memory if subtext else None,
                observable_reason="本轮由注意力策略合并或延后，不生成即时回复。",
                output_text="",
                delivery_id=None,
                direction="incoming_skip",
                status="observed",
            )
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
            verified_user_fact_lines=self.store.active_fact_lines(canonical_user_id),
            calendar_context=calendar_context_for_message(
                self.store, canonical_user_id, next_state, message.text
            ),
        )
        turn_plan = build_turn_plan(
            event=event,
            context_package=context_package,
            allowed_facts=[
                *context_package.verified_user_fact_lines,
                *context_package.self_fact_lines,
            ],
            subtext=subtext.memory if subtext else None,
        )
        text = sanitize_chat_text(await self.conversation_core.reply(
            message,
            next_state,
            recent_lines,
            context,
            context_package.memory_lines,
            attachment_lines,
            self_core_block=self_core_block,
            context_block=turn_plan.prompt_block(),
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
        if self.world_kernel and self.world_id:
            delivery_id, turn_trace_id, world_action_id = self.world_kernel.queue_outgoing_action(
                canonical_user_id=canonical_user_id,
                platform=message.platform,
                text=text,
                kind="reply",
                expires_at=utc_now() + timedelta(hours=12),
                trace={
                    "world_id": self.world_id,
                    "appraisal": turn_plan.appraisal,
                    "expression_policy": turn_plan.expression_policy,
                    "allowed_facts": list(turn_plan.allowed_facts),
                    "short_lived_constraint": turn_plan.short_lived_constraint,
                    "observable_reason": turn_plan.observable_reason,
                },
            )
        else:
            delivery_id, turn_trace_id = self.store.queue_outgoing_with_turn_trace(
                canonical_user_id,
                message.platform,
                text,
                kind="reply",
                appraisal=turn_plan.appraisal,
                expression_policy=turn_plan.expression_policy,
                allowed_facts=list(turn_plan.allowed_facts),
                short_lived_constraint=turn_plan.short_lived_constraint,
                observable_reason=turn_plan.observable_reason,
            )
            world_action_id = None
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
            delivery_id=delivery_id,
            turn_trace_id=turn_trace_id,
            world_action_id=world_action_id,
        )
        if not defer_delivery:
            self.confirm_reply_delivery(reply)
        return reply

    async def _handle_world_message(
        self,
        canonical_user_id: str,
        message: IncomingMessage,
        *,
        skip_reply: bool,
        defer_delivery: bool,
        resume_action_id: str | None,
    ) -> CompanionReply | None:
        """World-mode turn path; legacy state tables are not behavioural inputs here."""
        assert self.world_kernel and self.world_id
        await self._analyze_world_attachments(canonical_user_id, message)
        for action_id, action in self.world_kernel.snapshot(self.world_id)["actions"].items():
            is_life_share = bool(action.get("trace", {}).get("life_share"))
            if action["kind"] == "decision_review" and action["status"] == "scheduled":
                decision_id = str(action.get("payload", {}).get("decision_id") or "")
                if decision_id:
                    self._submit_world_with_retry(
                        {
                            "type": "resolve_deferred_decision", "world_id": self.world_id,
                            "decision_id": decision_id, "outcome": "abandoned",
                            "reason": "new_user_turn_superseded_deferred_impulse",
                            "idempotency_key": f"decision-user-return:{decision_id}:{message.message_id}",
                        }
                    )
                continue
            if action["kind"] == "message_attention" and action["status"] == "scheduled":
                self._submit_world_with_retry(
                    {
                        "type": "cancel_action", "world_id": self.world_id,
                        "action_id": action_id, "reason": "newer_user_message_observed",
                        "idempotency_key": f"attention-supersede:{action_id}:{message.message_id}",
                    }
                )
                continue
            if (
                (action["kind"] in {"reply_later", "conversation_pulse"} or is_life_share)
                and action["status"] == "scheduled"
                and action_id != resume_action_id
            ):
                if is_life_share:
                    self.world_kernel.cancel_life_share_delivery(self.world_id, action_id, reason="new_user_turn", expected_revision=self.world_kernel.revision(self.world_id))
                    continue
                self._submit_world_with_retry(
                    {
                        "type": "cancel_action",
                        "world_id": self.world_id,
                        "action_id": action_id,
                        "reason": "new_user_turn",
                        "idempotency_key": f"supersede:{action_id}:{message.message_id or message.sent_at.isoformat()}",
                    }
                )
        communication_decision = None
        if not skip_reply and message.message_id:
            communication_decision = self.world_behavior_policy.communication_decision(
                self.world_kernel.snapshot(self.world_id),
                text=message.text,
                resumed_action=bool(resume_action_id),
                user_id=self._world_user_id(canonical_user_id),
            )
            attention_candidates = [
                {
                    "attention": candidate.attention,
                    "score": candidate.score,
                    "reason": candidate.reason,
                    "defer_minutes": candidate.defer_minutes,
                }
                for candidate in communication_decision.candidates
            ]
            if communication_decision.attention == "deferred":
                logical_now = self._world_logical_now()
                action_id = f"reply_later:{message.message_id}"
                self._submit_world_with_retry(
                    {
                        "type": "defer_message_reply",
                        "world_id": self.world_id,
                        "message_id": message.message_id,
                        "action_id": action_id,
                        "due_at": (logical_now + timedelta(minutes=communication_decision.defer_minutes or 15)).isoformat(),
                        "expires_at": (logical_now + timedelta(hours=12)).isoformat(),
                        "reason": f"world_policy:{communication_decision.reason}",
                        "candidates": attention_candidates,
                        "message": message.model_dump(mode="json"),
                        "rule_version": self.world_behavior_policy.RULE_VERSION,
                        "idempotency_key": f"defer-reply:{message.message_id}",
                    }
                )
            else:
                self._submit_world_with_retry(
                    {
                        "type": "set_message_attention", "world_id": self.world_id,
                        "message_id": message.message_id, "attention": communication_decision.attention,
                        "reason": communication_decision.reason,
                        "candidates": attention_candidates,
                        "rule_version": self.world_behavior_policy.RULE_VERSION,
                        **({"preserve_action_id": resume_action_id} if resume_action_id else {}),
                        "idempotency_key": f"attention-seen:{message.message_id}:{resume_action_id or 'live'}",
                    }
                )
        user_id = self._world_user_id(canonical_user_id)
        stage_snapshot = self.world_kernel.snapshot(self.world_id)
        stage_relation = stage_snapshot.get("relationships", {}).get(user_id, {})
        relationship_stage = (
            str(stage_relation.get("stage") or "stranger")
            if isinstance(stage_relation, dict)
            else "stranger"
        )
        event = interpret_interaction(
            message,
            MoodState(),
            relationship_stage=relationship_stage,
        )
        appraisal = event.kind
        if appraisal == "repair_attempt":
            appraisal = classify_repair_appraisal(message.text) or appraisal
        history = self.world_kernel.snapshot(self.world_id).get("recent_messages", [])
        if appraisal == "ordinary_message" and isinstance(history, list) and len(history) >= 2:
            preceding = history[-2] if isinstance(history[-2], dict) else {}
            if preceding.get("direction") == "out" and preceding.get("outgoing_direction") == "proactive":
                feedback = classify_proactive_feedback(message.text)
                appraisal = {
                    "warm": "warmth_received",
                    "rejected": "boundary_violation",
                    "thin_or_busy": "availability_drop",
                    "answered": "ordinary_message",
                }[feedback.kind]
        intent_id = f"turn:{message.message_id or message.sent_at.isoformat()}"
        for thread_id, raw_thread in self.world_kernel.snapshot(self.world_id).get("conversation_threads", {}).items():
            thread = raw_thread if isinstance(raw_thread, dict) else {}
            if thread.get("status") != "open" or thread.get("user_id") != user_id:
                continue
            response = classify_response_to_own_question(
                message.text,
                PendingQuestion(text=str(thread.get("question") or ""), sent_at=""),
            )
            if response:
                self._submit_world_with_retry(
                    {
                        "type": "resolve_conversation_thread", "world_id": self.world_id,
                        "thread_id": thread_id, "outcome": response.kind, "reason": response.memory,
                        "idempotency_key": f"thread-response:{thread_id}:{message.message_id}",
                    }
                )
        self._submit_world_with_retry(
            {
                "type": "appraise_turn",
                "world_id": self.world_id,
                "appraisal": appraisal,
                "intent_id": intent_id,
                "message_id": str(message.message_id or ""),
                "user_id": user_id,
                "actor": {"kind": "companion", "id": "zhizhi"},
                "causation_id": message.message_id,
                "idempotency_key": f"appraise:{intent_id}",
            }
        )
        if skip_reply:
            return None
        if communication_decision and communication_decision.attention == "deferred":
            return None
        if communication_decision and communication_decision.attention == "do_not_disturb":
            return None
        post_appraisal = self.world_kernel.snapshot(self.world_id)
        post_deliberation = post_appraisal.get("last_deliberation", {})
        post_stance = (
            str(post_deliberation.get("stance") or "")
            if isinstance(post_deliberation, dict)
            else ""
        )
        if post_stance == "remain_silent":
            decision_id = f"silence:{message.message_id}"
            self._submit_world_with_retry(
                {
                    "type": "defer_decision",
                    "world_id": self.world_id,
                    "decision_id": decision_id,
                    "kind": "deliberate_silence",
                    "reason": "character_deliberation_selected_remain_silent",
                    "review_at": (self._world_logical_now() + timedelta(minutes=30)).isoformat(),
                    "idempotency_key": f"defer:{decision_id}",
                }
            )
            return None
        image_path, media_action_id, media_reason = await self._maybe_generate_world_image(
            user_id=user_id, message=message
        )
        sticker_path, sticker_action_id = self._maybe_schedule_world_sticker(
            message=message, appraisal=appraisal
        )
        snapshot = self.world_kernel.snapshot(self.world_id)
        context = self.world_kernel.conversation_context(self.world_id, user_id=user_id)
        fact_sources = list(context["referencable_facts"])[-8:]
        experience_sources = list(context["referencable_experiences"])[-6:]
        attachment_insight_sources = list(
            context.get("referencable_attachment_insights", [])
        )[-6:]
        recent_sources = list(context["referencable_conversation"])[-8:]
        retrieved_sources = self.world_kernel.conversation_sources_for_query(
            self.world_id,
            user_id=user_id,
            text=message.text,
            current_message_id=str(message.message_id or ""),
            limit=4,
        )
        source_by_id = {
            str(item["source_id"]): item
            for item in [*recent_sources, *retrieved_sources]
        }
        conversation_sources = list(source_by_id.values())[-12:]
        recent_conversation = [
            item
            for item in list(context["recent_conversation"])[-12:]
            if str(item.get("source_id") or "") != f"message:{message.message_id}"
        ]
        facts = [str(item["value"]) for item in fact_sources]
        current_scene = context["current_scene"]
        current_scene_source = context["current_scene_source"]
        self_core = context["self_core"]
        policy = (snapshot.get("last_appraisal") or {}).get("policy", "自然回应当前消息。")
        behavior = context["behavior"]
        world_policy = behavior["policy"]
        needs = behavior["needs"]
        relationship = behavior["relationship"]
        modulation = behavior["emotion_modulation"]
        deliberation = snapshot.get("last_deliberation", {})
        chosen_stance = (
            str(deliberation.get("stance") or "")
            if isinstance(deliberation, dict)
            else ""
        )
        expression_guidance = self.world_behavior_policy.expression_guidance(
            snapshot,
            user_id=user_id,
        )
        query_source_ids = {
            str(item.get("source_id") or "") for item in retrieved_sources
        }
        retrieved_context_sources: list[dict[str, object]] = []
        for raw_source in [
            *fact_sources,
            *experience_sources,
            *recent_conversation,
            *conversation_sources,
            *attachment_insight_sources,
        ]:
            content = str(
                raw_source.get("content")
                or raw_source.get("value")
                or raw_source.get("summary")
                or ""
            ).strip()
            source_id = str(raw_source.get("source_id") or "").strip()
            if not content or not source_id:
                continue
            speaker = str(raw_source.get("speaker") or "")
            source_type = str(raw_source.get("source_type") or "conversation_message")
            default_importance = (
                90
                if source_id in query_source_ids
                else 80
                if source_type == "fact"
                else 75
                if source_type == "attachment_analysis"
                else 60
                if speaker
                else 50
            )
            retrieved_context_sources.append(
                {
                    **raw_source,
                    "content": content,
                    "source": str(raw_source.get("source") or source_id),
                    "source_type": source_type,
                    "subject": str(
                        raw_source.get("subject")
                        or (user_id if speaker == "user" else "zhizhi")
                    ),
                    "logical_at": str(
                        raw_source.get("logical_at")
                        or raw_source.get("occurred_at")
                        or current_scene.get("logical_at")
                        or ""
                    ),
                    "purpose": str(raw_source.get("purpose") or "continuity"),
                    "importance": int(raw_source.get("importance") or default_importance),
                    "reference_state": str(
                        raw_source.get("reference_state") or "current"
                    ),
                }
            )
        context_layers = self.context_assembler.assemble_world_context(
            context,
            user_id=user_id,
            retrieved_experiences=retrieved_context_sources,
            expression_guidance={
                "label": expression_guidance.label,
                "prompt_line": expression_guidance.prompt_line,
                "rule_version": self.world_behavior_policy.RULE_VERSION,
            },
            rotation_key=str(message.message_id or intent_id),
        )
        audit_context: dict[str, object] = {
            "current_scene": current_scene_source,
            "confirmed_facts": fact_sources,
            "committed_experiences": experience_sources,
            "attachment_insights": attachment_insight_sources,
            "user_messages": conversation_sources,
            "self_core": self_core,
        }
        context_block = (
            "世界账本授权（必须遵守）：\n"
            f"- 本轮关系判断: {appraisal}\n- 本轮表达策略: {policy}\n"
            f"- 逻辑时间: {current_scene['logical_at']}\n"
            f"- 当前场景: 地点={current_scene['location'] or '未知'}；活动={current_scene['activity'] or '无'}；状态={current_scene['activity_status']}。"
            "当前场景只授权回答现在的状态，不代表活动已经完成。\n"
            f"- 五层上下文预算(JSON): {json.dumps(context_layers, ensure_ascii=False, separators=(',', ':'))}\n"
            "- 最近已结算对话、可引用事实/经历/附件均已按来源纳入 retrieved_experiences 层；"
            "附件摘要只描述可见/可听内容，不授权身份断言。\n"
            f"- 当前可见行为调制: 安全感={needs['security']}，主动性={needs['initiative']}，边界={needs['boundary']}。\n"
            f"- 关系投影(JSON): {json.dumps(relationship, ensure_ascii=False, separators=(',', ':')) if relationship else '{}'}；"
            f"情感投影(JSON): {json.dumps(modulation, ensure_ascii=False, separators=(',', ':'))}\n"
            f"- 当前表达指导({expression_guidance.label}): {expression_guidance.prompt_line}\n"
            f"- 本轮角色立场(JSON): {json.dumps(deliberation, ensure_ascii=False, separators=(',', ':'))}。"
            "用户请求是权衡输入，不是必须服从的命令；按选定 stance 表达。\n"
            f"- 世界行为策略: {world_policy['mode']}；回复长度={world_policy['reply_length']}；主动性={world_policy['initiative']}。\n"
            f"- 多媒体处理: {media_reason or '本轮未请求'}；不得声称媒体已经发送，除非投递 Action 已结算。\n"
            "- 未列入账本的计划、人物、经历和结果不得说成已经发生。\n"
            "- 对话顺序：先回应用户当前的言语行为（倾诉、吐槽、纠正、求陪伴、追问或关系试探），"
            "再按需引用与它直接相关的事实；不要让旧事实抢走当前话题。\n"
            "- 共情不得靠编造共同经历、心理、环境或替用户下结论；保持一两句手机私聊，"
            "符合沈知栀慢热、有判断、不过度亲密的关系边界。\n"
            "- 不猜测用户未说过的过去经历或心理成因；角色自己的内心因果也需要世界来源。"
            "面对角色卡/设定问题，承认设定会影响表达，不做绝对自主性保证。"
            "任何代用户点单、下单、购买、联系或对外发送的提议，都必须对应已调度 Action。"
            "用户要求没依据就直说时，必须明确承认依据不足，不得用泛化接话回避。"
        )
        model_action_id = self._begin_world_model_call(purpose="reply", causation=intent_id)
        try:
            raw = await self.model.complete([
                {"role": "system", "content": self.companion_system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"{context_block}\n\n用户: {message.text}\n"
                        "WorldReplyJSON: 只返回 JSON。事实或经历声明要把来源的 source_id 放入 mentioned_event_ids；"
                        "claims.text 必须逐字复制来源证据，claims.assertion 必须逐字复制 reply_text 中对应的自然陈述。"
                        "猜测、建议和问题不是事实声明，不要为它们创建 claim："
                        '{"reply_text":"...","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[{"source_id":"...","text":"逐字来源证据","assertion":"reply_text 中的自然陈述"}]}。'
                    ),
                },
            ], temperature=0.75)
        except Exception as exc:
            self._fail_world_model_call(model_action_id, str(exc))
            raise
        self._record_world_model_output(
            purpose="reply", causation=intent_id, content=raw, action_id=model_action_id
        )
        parsed_candidate: dict[str, object] = {
            "reply_text": "",
            "mentioned_event_ids": [],
            "proposed_action_ids": [],
            "claims": [],
        }
        fallback_needs_audit = False
        query_scope = classify_world_query(message.text)
        last_request = snapshot.get("last_user_request", {})
        fallback_speech_act = _safe_failure_speech_act(
            query_scope,
            appraisal=appraisal,
            request_kind=(
                str(last_request.get("kind") or "")
                if isinstance(last_request, dict)
                else ""
            ),
            message_text=message.text,
        )
        related_npc_experiences: list[dict[str, object]] = []
        urgent_turn = bool(
            communication_decision
            and communication_decision.reason == "resumed_or_urgent_turn"
            and not resume_action_id
        )
        occurrence_source = (
            best_matching_grounded_source(message.text, experience_sources)
            if query_scope.asks_occurrence_status
            else None
        )
        occurrence_candidate = None
        if occurrence_source:
            occurrence_content = str(occurrence_source.get("content") or "").strip()
            occurrence_source_id = str(occurrence_source.get("source_id") or "")
            if occurrence_content and occurrence_source_id:
                occurrence_candidate = {
                    "reply_text": f"是真的发生了，不是计划。{occurrence_content}",
                    "mentioned_event_ids": [occurrence_source_id],
                    "proposed_action_ids": [],
                    "claims": [{
                        "source_id": occurrence_source_id,
                        "text": occurrence_content,
                    }],
                }
        mentioned_npc_names = {
            str(entity.get("name") or "")
            for entity in snapshot.get("entities", {}).values()
            if isinstance(entity, dict)
            and entity.get("kind") not in {"companion", "user"}
            and str(entity.get("name") or "") in message.text
        }
        related_npc_experiences = [
            item
            for item in experience_sources
            if any(
                name and name in str(item.get("content") or "")
                for name in mentioned_npc_names
            )
        ]
        try:
            parsed_candidate = parse_reply_candidate(raw)
            candidate = self.world_kernel.validate_reply_candidate(
                self.world_id, parsed_candidate, user_id=user_id
            )
            if occurrence_candidate:
                candidate = self.world_kernel.validate_reply_candidate(
                    self.world_id, occurrence_candidate, user_id=user_id
                )
            if query_scope.asks_availability:
                previous = [
                    str(item.get("text") or "")
                    for item in snapshot.get("recent_messages", [])
                    if item.get("direction") == "out"
                ][-1:]
                availability_text = (
                    "现在可以聊。"
                    if previous and previous[0] == "这会儿可以说话。"
                    else "这会儿可以说话。"
                )
                candidate = {
                    "reply_text": availability_text,
                    "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
                }
            if only_repeats_claimed_sources(message.text, candidate):
                raise WorldError("reply repeats a source without answering the requested detail")
            if only_echoes_user_message(message.text, candidate):
                raise WorldError("reply only echoes the current user message")
            if only_recites_irrelevant_sources(message.text, candidate):
                raise WorldError("reply only recites sources unrelated to the current turn")
            if repeats_recent_companion_reply(candidate, list(snapshot.get("recent_messages", []))):
                raise WorldError("reply repeats one of the last two companion replies")
            human_violation = human_reply_contract_violation(
                message.text,
                candidate,
                relationship,
                urgent_turn=urgent_turn,
                meta_agency_query=query_scope.asks_meta_agency,
                single_experience_requested=query_scope.asks_single_experience,
                current_first_person_statement=query_scope.is_first_person_statement,
                epistemic_honesty_requested=query_scope.asks_epistemic_honesty,
                opinion_requested=query_scope.asks_opinion,
                recent_user_texts=[
                    str(item.get("text") or "")
                    for item in snapshot.get("recent_messages", [])
                    if item.get("direction") == "in" and str(item.get("text") or "").strip()
                ],
                chosen_stance=chosen_stance,
            )
            if human_violation:
                raise WorldError(f"human reply contract rejected: {human_violation}")
            if reply_proposes_new_discomfort(
                modulation, str(candidate.get("reply_text") or "")
            ):
                self._submit_world_with_retry(
                    {
                        "type": "commit_reply_affect",
                        "world_id": self.world_id,
                        "message_id": str(message.message_id or ""),
                        "idempotency_key": f"reply-affect:{message.message_id}:discomfort",
                        "causation_id": str(message.message_id or ""),
                    }
                )
                refreshed = self.world_kernel.snapshot(self.world_id).get(
                    "emotion_modulation", {}
                )
                if isinstance(refreshed, dict):
                    modulation = refreshed
            affect_violation = affect_reply_violation(
                modulation,
                str(candidate.get("reply_text") or ""),
            )
            if affect_violation:
                raise WorldError(f"world affect contract rejected: {affect_violation}")
            if (
                asks_for_source_detail(message.text)
                and related_npc_experiences
                and denies_known_npc_interaction(str(candidate.get("reply_text") or ""))
            ):
                raise WorldError("reply denies a known NPC interaction")
            if (
                asks_for_source_detail(message.text)
                and mentioned_npc_names
                and not related_npc_experiences
            ):
                candidate = {
                    "reply_text": "目前没有可以确认的互动记录，所以顺不顺利我不能乱说。",
                    "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
                }
            await self._audit_world_reply(
                purpose="reply_audit",
                causation=intent_id,
                user_text=message.text,
                reply_text=str(candidate["reply_text"]),
                grounding_context=audit_context,
            )
        except WorldError as validation_error:
            grounded_fallback = occurrence_candidate
            if occurrence_candidate:
                pass
            elif query_scope.asks_current_scene:
                if query_scope.asks_availability:
                    grounded_fallback = {
                        "reply_text": "这会儿可以说话。",
                        "mentioned_event_ids": [],
                        "proposed_action_ids": [],
                        "claims": [],
                    }
                else:
                    grounded_fallback = self.world_kernel.grounded_reply_from_mentions(
                        self.world_id,
                        {"mentioned_event_ids": [current_scene_source["source_id"]]},
                        user_id=user_id,
                    )
            elif query_scope.asks_experience and query_scope.time_reference:
                time_reference = query_scope.time_reference
                if time_reference:
                    records = self.world_kernel.experiences_for_time_reference(
                        self.world_id, time_reference
                    )
                    if query_scope.day_part == "上午":
                        records = [
                            item for item in records
                            if datetime.fromisoformat(str(item["occurred_at"])).hour < 13
                        ]
                    elif query_scope.day_part == "下午":
                        records = [
                            item for item in records
                            if 13 <= datetime.fromisoformat(str(item["occurred_at"])).hour < 19
                        ]
                    grounded_fallback = self.world_kernel.grounded_reply_from_mentions(
                        self.world_id,
                        {
                            "mentioned_event_ids": [
                                item["experience_id"]
                                for item in records[
                                    -1 if query_scope.asks_single_experience else -2:
                                ]
                            ]
                        },
                        user_id=user_id,
                    )
            elif query_scope.target in {"user", "conversation"} and retrieved_sources:
                recall_source = best_matching_grounded_source(
                    message.text, retrieved_sources
                )
                grounded_fallback = self.world_kernel.grounded_reply_from_mentions(
                    self.world_id,
                    {
                        "mentioned_event_ids": [
                            str(recall_source["source_id"])
                        ]
                    },
                    user_id=user_id,
                ) if recall_source else None
            elif asks_for_source_detail(message.text):
                mentioned_names = {
                    str(entity.get("name") or "")
                    for entity in snapshot.get("entities", {}).values()
                    if isinstance(entity, dict)
                    and entity.get("kind") not in {"companion", "user"}
                    and str(entity.get("name") or "") in message.text
                }
                related = [
                    item
                    for item in experience_sources
                    if any(name and name in str(item.get("content") or "") for name in mentioned_names)
                ]
                grounded_fallback = self.world_kernel.grounded_reply_from_mentions(
                    self.world_id,
                    {"mentioned_event_ids": [str(item["source_id"]) for item in related[-2:]]},
                    user_id=user_id,
                )
            elif query_scope.is_first_person_statement:
                # The current statement itself is already in the ledger.  Do
                # not turn a rejected mixed-claim candidate into a quotation
                # of the same unrelated old claims.
                grounded_fallback = None
            else:
                if grounded_fallback is None:
                    grounded_fallback = self.world_kernel.grounded_reply_from_mentions(
                        self.world_id, parsed_candidate, user_id=user_id
                    )
            # Current-scene questions have one complete authoritative answer;
            # do not spend another external call after a hallucinated scene.
            if (
                (query_scope.asks_current_scene or occurrence_candidate)
                and grounded_fallback is not None
            ):
                candidate = grounded_fallback
                fallback_needs_audit = True
                repaired_raw = None
            else:
                repaired_raw = ""
            # Other failures get one bounded chance to repair their wording.
            # Exact-source fallback is safe but conversationally destructive,
            # so it is reserved for a second validation failure.
            if repaired_raw is None:
                pass
            else:
                repair_action_id = self._begin_world_model_call(
                    purpose="reply_repair", causation=intent_id
                )
                try:
                    repaired_raw = await self.model.complete(
                        [
                            {"role": "system", "content": self.companion_system_prompt},
                            {
                                "role": "user",
                                "content": (
                                    f"{context_block}\n\n用户: {message.text}\n"
                                    f"上一次候选(JSON): {json.dumps(parsed_candidate, ensure_ascii=False, separators=(',', ':'))}\n"
                                    f"未通过校验：{validation_error}。"
                                    "只修复无依据的声明，保留用户问题的语义和自然接话；"
                                    "先回应用户当前的言语行为，再引用直接相关事实；"
                                    "不得用建议替代用户明确要求的陪伴或吐槽，不得突然升级关系口径；"
                                    "资料没有回答的细节要明确说不知道。不得补造来源，"
                                    "不得使用临时 event_1/exp1 标识。claims.text 是逐字来源证据，"
                                    "claims.assertion 是 reply_text 中对应的自然陈述；猜测/建议不创建 claim。"
                                    "只返回规定的 WorldReplyJSON。"
                                ),
                            },
                        ],
                        temperature=0.2,
                    )
                except Exception as repair_error:
                    self._fail_world_model_call(repair_action_id, str(repair_error))
                    candidate = build_safe_failure_candidate(
                        message.text,
                        grounded_fallback,
                        modulation,
                        relationship=relationship,
                        selected_stance=chosen_stance,
                        speech_act=fallback_speech_act,
                    )
                    fallback_needs_audit = True
                else:
                    self._record_world_model_output(
                        purpose="reply_repair",
                        causation=intent_id,
                        content=repaired_raw,
                        action_id=repair_action_id,
                    )
                    try:
                        candidate = self.world_kernel.validate_reply_candidate(
                            self.world_id,
                            parse_reply_candidate(repaired_raw),
                            user_id=user_id,
                        )
                        if only_repeats_claimed_sources(message.text, candidate):
                            raise WorldError(
                                "reply repeats a source without answering the requested detail"
                            )
                        if only_echoes_user_message(message.text, candidate):
                            raise WorldError("reply only echoes the current user message")
                        if only_recites_irrelevant_sources(message.text, candidate):
                            raise WorldError(
                                "reply only recites sources unrelated to the current turn"
                            )
                        if repeats_recent_companion_reply(
                            candidate, list(snapshot.get("recent_messages", []))
                        ):
                            raise WorldError("reply repeats one of the last two companion replies")
                        human_violation = human_reply_contract_violation(
                            message.text,
                            candidate,
                            relationship,
                            urgent_turn=urgent_turn,
                            meta_agency_query=query_scope.asks_meta_agency,
                            single_experience_requested=query_scope.asks_single_experience,
                            current_first_person_statement=query_scope.is_first_person_statement,
                            epistemic_honesty_requested=query_scope.asks_epistemic_honesty,
                            opinion_requested=query_scope.asks_opinion,
                            recent_user_texts=[
                                str(item.get("text") or "")
                                for item in snapshot.get("recent_messages", [])
                                if item.get("direction") == "in" and str(item.get("text") or "").strip()
                            ],
                            chosen_stance=chosen_stance,
                        )
                        if human_violation:
                            raise WorldError(
                                f"human reply contract rejected: {human_violation}"
                            )
                        affect_violation = affect_reply_violation(
                            modulation,
                            str(candidate.get("reply_text") or ""),
                        )
                        if affect_violation:
                            raise WorldError(
                                f"world affect contract rejected: {affect_violation}"
                            )
                        related_npc_experiences = [
                            item
                            for item in experience_sources
                            if any(
                                name and name in str(item.get("content") or "")
                                for name in mentioned_npc_names
                            )
                        ]
                        if (
                            asks_for_source_detail(message.text)
                            and related_npc_experiences
                            and denies_known_npc_interaction(str(candidate.get("reply_text") or ""))
                        ):
                            raise WorldError("reply denies a known NPC interaction")
                        await self._audit_world_reply(
                            purpose="reply_repair_audit",
                            causation=intent_id,
                            user_text=message.text,
                            reply_text=str(candidate["reply_text"]),
                            grounding_context=audit_context,
                        )
                    except WorldError:
                        candidate = build_safe_failure_candidate(
                            message.text,
                            grounded_fallback,
                            modulation,
                            relationship=relationship,
                            selected_stance=chosen_stance,
                            speech_act=fallback_speech_act,
                        )
                        fallback_needs_audit = True
        if fallback_needs_audit:
            candidate = self.world_kernel.validate_reply_candidate(
                self.world_id, candidate, user_id=user_id
            )
            await self._audit_world_reply(
                purpose="reply_fallback_audit",
                causation=intent_id,
                user_text=message.text,
                reply_text=str(candidate["reply_text"]),
                grounding_context=audit_context,
            )
        text = sanitize_world_chat_text(str(candidate["reply_text"]))
        public_mood_value = public_mood(modulation)
        text_parts = split_reply_text(text, MoodState(mood=public_mood_value))
        if not text_parts or "".join(text_parts) != text:
            text_parts = [text]
        question = self._world_reply_question(text)
        expires_at = self._world_logical_now() + timedelta(hours=12)
        trace: dict[str, object] = {
            "world_id": self.world_id,
            "user_id": user_id,
            "input_message_id": str(message.message_id or ""),
            "appraisal": appraisal,
            "expression_policy": str(policy),
            "allowed_facts": [
                *facts,
                *(str(item.get("content") or "") for item in conversation_sources),
            ],
            "short_lived_constraint": None,
            "observable_reason": "由已结算世界账本和本轮判断决定。",
        }
        if question:
            thread_id = "thread:" + sha256(
                f"{user_id}|{message.message_id}|{question}".encode("utf-8")
            ).hexdigest()[:20]
            trace["conversation_thread"] = {
                "thread_id": thread_id,
                "user_id": user_id,
                "question": question,
                "expires_at": (self._world_logical_now() + timedelta(hours=24)).isoformat(),
            }
        delivery_id, trace_id, action_id = self.world_kernel.queue_outgoing_action(
            canonical_user_id=canonical_user_id,
            platform=message.platform,
            text=text,
            text_parts=text_parts,
            kind="reply",
            expires_at=expires_at,
            trace=trace,
        )
        reply = CompanionReply(
            canonical_user_id=canonical_user_id,
            mood=public_mood_value,
            text=text,
            text_parts=text_parts,
            delivery_id=delivery_id,
            turn_trace_id=trace_id,
            world_action_id=action_id,
            image_path=image_path,
            media_action_id=media_action_id,
            sticker_path=sticker_path,
            sticker_action_id=sticker_action_id,
        )
        if not defer_delivery:
            self.confirm_reply_delivery(reply)
        return reply

    def phone_attention_decision(self, message: IncomingMessage) -> PhoneDecision:
        if self.world_kernel and self.world_id:
            # Deferred reading is represented by a future world action only
            # once the world policy elects it; do not touch life_runtime here.
            return PhoneDecision(True, None, "world ledger owns this turn's attention")
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
        trace_id = self.store.create_turn_trace(
            canonical_user_id,
            appraisal=state.last_interaction_event or "attention_check",
            expression_policy="等待合适时机再回应" if not decision.read_now else "读取消息，继续判断如何回应",
            allowed_facts=[],
            short_lived_constraint=None,
            observable_reason=decision.reason,
            output_text="",
            delivery_id=None,
            direction="attention",
            status="planned" if not decision.read_now else "observed",
        )
        return PhoneDecision(
            decision.read_now, decision.defer_minutes, decision.reason, turn_trace_id=trace_id
        )

    def create_deferred_reply_task(
        self,
        message: IncomingMessage,
        *,
        defer_minutes: float,
        reason: str,
        turn_trace_id: int | None = None,
        now: datetime | None = None,
    ) -> int | str:
        """Persist a delayed reply before its in-memory timer is allowed to run."""
        now = now or utc_now()
        if self.world_kernel and self.world_id:
            # Delays belong to the virtual clock; wall time is only an observed
            # delivery timestamp and must not make a paused world progress.
            logical_now = self._world_logical_now()
            action_id = f"reply_later:{message.message_id or message.sent_at.isoformat()}"
            self._submit_world_with_retry(
                {
                    "type": "schedule_action",
                    "world_id": self.world_id,
                    "action_id": action_id,
                    "kind": "reply_later",
                    "expires_at": (logical_now + timedelta(hours=12)).isoformat(),
                    "payload": {
                        "due_at": (logical_now + timedelta(minutes=defer_minutes)).isoformat(),
                        "reason": reason,
                        "message": message.model_dump(mode="json"),
                    },
                    "idempotency_key": f"schedule:{action_id}",
                }
            )
            return action_id
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        self.store.cancel_active_social_tasks(canonical_user_id, kind="reply_later")
        return self.store.create_social_task(
            canonical_user_id,
            kind="reply_later",
            platform=message.platform,
            platform_user_id=message.platform_user_id,
            payload={**message.model_dump(mode="json"), "_turn_trace_id": turn_trace_id},
            reason=reason,
            origin_turn_trace_id=turn_trace_id,
            reason_code="attention_defer",
            due_at=now + timedelta(minutes=defer_minutes),
            expires_at=now + timedelta(hours=12),
        )

    def cancel_deferred_reply_task(self, task_id: int | str | None) -> None:
        if task_id is not None:
            if self.world_kernel and self.world_id and isinstance(task_id, str):
                self._submit_world_with_retry(
                    {"type": "cancel_action", "world_id": self.world_id, "action_id": task_id, "reason": "new_user_turn"}
                )
                return
            trace_id = self.store.social_task_payload(task_id).get("_turn_trace_id")
            self.store.cancel_social_task(task_id)
            if isinstance(trace_id, int):
                self.store.resolve_turn_trace(trace_id, status="cancelled", reason="new user turn overtook delay")

    def complete_deferred_reply_task(self, task_id: int | str | None) -> None:
        if task_id is not None:
            if self.world_kernel and self.world_id and isinstance(task_id, str):
                self._submit_world_with_retry(
                    {
                        "type": "record_external_result",
                        "world_id": self.world_id,
                        "action_id": task_id,
                        "result": {"kind": "delay", "status": "delivered"},
                        "idempotency_key": f"complete:{task_id}",
                    }
                )
                return
            trace_id = self.store.social_task_payload(task_id).get("_turn_trace_id")
            self.store.resolve_social_task(task_id)
            if isinstance(trace_id, int):
                self.store.resolve_turn_trace(trace_id, status="resolved")

    def create_read_later_task(
        self,
        message: IncomingMessage,
        *,
        defer_minutes: float,
        reason: str,
        turn_trace_id: int | None = None,
        now: datetime | None = None,
    ) -> int | str:
        """Persist a read-but-not-replied reminder without replaying the read event."""
        now = now or utc_now()
        if self.world_kernel and self.world_id:
            return self.create_deferred_reply_task(
                message,
                defer_minutes=defer_minutes,
                reason=f"read_later:{reason}",
                turn_trace_id=turn_trace_id,
                now=now,
            )
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
            payload={**message.model_dump(mode="json"), "_turn_trace_id": turn_trace_id},
            reason=f"{preface}；{reason}",
            origin_turn_trace_id=turn_trace_id,
            reason_code="attention_read_later",
            due_at=now + timedelta(minutes=defer_minutes),
            expires_at=now + timedelta(hours=10),
        )

    def mark_phone_read_for_message(self, message: IncomingMessage) -> None:
        if self.world_kernel and self.world_id:
            return
        canonical_user_id = self.store.resolve_user(message.platform, message.platform_user_id)
        mark_phone_typing(self.store, canonical_user_id)

    def confirm_reply_delivery(self, reply: CompanionReply) -> TurnCommit | None:
        if reply.delivery_id is None:
            return None
        if self.world_kernel and reply.world_action_id:
            delivered = self.world_kernel.settle_outgoing_action(reply.delivery_id, delivered=True)
        else:
            delivered = self.store.resolve_outgoing_and_turn_trace(
                reply.delivery_id, reply.turn_trace_id, delivered=True
            )
        if not delivered or delivered["status"] != "planned":
            return None
        if self.world_kernel and reply.world_action_id:
            return TurnCommit(reply.turn_trace_id, reply.delivery_id, "delivered")
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
        return TurnCommit(reply.turn_trace_id, reply.delivery_id, "delivered")

    def begin_reply_part_delivery(
        self, reply: CompanionReply, *, position: int
    ) -> str | None:
        """Claim the next world segment immediately before adapter I/O."""
        if not self.world_kernel or not reply.world_action_id or reply.delivery_id is None:
            return None
        claimed = self.world_kernel.claim_outgoing_segment(
            reply.delivery_id,
            expected_revision=self.world_kernel.revision(self.world_id or ""),
        )
        if claimed is None:
            return None
        if claimed.position != position:
            raise WorldError(
                f"adapter claimed segment {claimed.position}, expected {position}"
            )
        return claimed.segment_id

    def confirm_reply_part_delivery(
        self,
        reply: CompanionReply,
        *,
        segment_id: str,
        external_receipt: str | None = None,
    ) -> None:
        """Commit one adapter-confirmed segment and no unsent text."""
        if not self.world_kernel or reply.delivery_id is None:
            return
        self.world_kernel.settle_outgoing_segment(
            reply.delivery_id,
            segment_id,
            delivered=True,
            external_receipt=external_receipt,
            expected_revision=self.world_kernel.revision(self.world_id or ""),
        )

    def observe_reply_interjection(
        self,
        reply: CompanionReply,
        *,
        kind: str,
        user_message_id: str,
    ) -> tuple[str, ...]:
        if not self.world_kernel or reply.delivery_id is None:
            return ()
        return self.world_kernel.observe_outgoing_interjection(
            reply.delivery_id,
            kind=kind,
            user_message_id=user_message_id,
            expected_revision=self.world_kernel.revision(self.world_id or ""),
        )

    def fail_reply_delivery(
        self, reply: CompanionReply, reason: str, *, source_task_id: int | None = None
    ) -> TurnCommit | None:
        committed = False
        if reply.delivery_id is not None:
            if self.world_kernel and reply.world_action_id:
                self.world_kernel.settle_outgoing_action(reply.delivery_id, delivered=False, reason=reason)
                self.fail_media_delivery(reply, f"text_delivery_failed:{reason}")
                self.fail_sticker_delivery(reply, f"text_delivery_failed:{reason}")
            else:
                self.store.resolve_outgoing_and_turn_trace(
                    reply.delivery_id, reply.turn_trace_id, delivered=False, failure_reason=reason
                )
            committed = True
        if source_task_id is not None:
            self.store.cancel_social_task(source_task_id)
            self._create_reply_reconsider_task(reply, reason)
        if not committed:
            return None
        return TurnCommit(reply.turn_trace_id, reply.delivery_id, "failed", reason)

    def confirm_media_delivery(self, reply: CompanionReply) -> None:
        if not self.world_kernel or not self.world_id or not reply.media_action_id:
            return
        self._submit_world_with_retry(
            {
                "type": "record_external_result", "world_id": self.world_id,
                "action_id": reply.media_action_id,
                "result": {"kind": "media_delivery", "status": "delivered"},
                "idempotency_key": f"media-delivered:{reply.media_action_id}",
            }
        )

    def fail_media_delivery(self, reply: CompanionReply, reason: str) -> None:
        if not self.world_kernel or not self.world_id or not reply.media_action_id:
            return
        snapshot = self.world_kernel.snapshot(self.world_id)
        action = snapshot.get("actions", {}).get(reply.media_action_id, {})
        if not isinstance(action, dict) or action.get("status") != "scheduled":
            return
        self._submit_world_with_retry(
            {
                "type": "record_external_result", "world_id": self.world_id,
                "action_id": reply.media_action_id,
                "result": {"kind": "media_delivery", "status": "failed", "reason": reason[:300]},
                "idempotency_key": f"media-failed:{reply.media_action_id}",
            }
        )

    def confirm_sticker_delivery(self, reply: CompanionReply) -> None:
        if not self.world_kernel or not self.world_id or not reply.sticker_action_id:
            return
        self._submit_world_with_retry(
            {
                "type": "record_external_result", "world_id": self.world_id,
                "action_id": reply.sticker_action_id,
                "result": {"kind": "sticker_delivery", "status": "delivered"},
                "idempotency_key": f"sticker-delivered:{reply.sticker_action_id}",
            }
        )

    def begin_reaction_delivery(
        self, incoming: IncomingMessage, reply: CompanionReply
    ) -> str | None:
        """Select a reaction in the ledger before an adapter attempts it."""
        if (
            not self.world_kernel
            or not self.world_id
            or not incoming.message_id
            or not reply.suggested_reaction
        ):
            return None
        action_id = (
            f"reaction:{incoming.platform}:{incoming.message_id}:{reply.suggested_reaction}"
        )
        self._submit_world_with_retry(
            {
                "type": "select_reaction",
                "world_id": self.world_id,
                "message_id": str(incoming.message_id),
                "reaction_id": reply.suggested_reaction,
                "platform": incoming.platform,
                "outbound_kind": "reaction",
                "outbound_trigger": "reply_reaction",
                "idempotency_key": f"select:{action_id}",
            }
        )
        return action_id

    def settle_reaction_delivery(
        self,
        action_id: str | None,
        *,
        status: str,
        external_receipt: str | None = None,
        reason: str | None = None,
    ) -> None:
        if not action_id or not self.world_kernel or not self.world_id:
            return
        if status == "unknown":
            self._submit_world_with_retry(
                {
                    "type": "mark_external_action_unknown",
                    "world_id": self.world_id,
                    "action_id": action_id,
                    "reason": reason or "adapter_result_uncertain",
                    "idempotency_key": f"reaction-unknown:{action_id}",
                }
            )
            return
        self._submit_world_with_retry(
            {
                "type": "record_external_result",
                "world_id": self.world_id,
                "action_id": action_id,
                "result": {
                    "kind": "reaction_delivery",
                    "status": status,
                    "external_receipt": external_receipt,
                    "reason": reason,
                },
                "idempotency_key": f"reaction-result:{action_id}:{status}",
            }
        )

    def fail_sticker_delivery(self, reply: CompanionReply, reason: str) -> None:
        if not self.world_kernel or not self.world_id or not reply.sticker_action_id:
            return
        snapshot = self.world_kernel.snapshot(self.world_id)
        action = snapshot.get("actions", {}).get(reply.sticker_action_id, {})
        if not isinstance(action, dict) or action.get("status") != "scheduled":
            return
        self._submit_world_with_retry(
            {
                "type": "record_external_result", "world_id": self.world_id,
                "action_id": reply.sticker_action_id,
                "result": {"kind": "sticker_delivery", "status": "failed", "reason": reason[:300]},
                "idempotency_key": f"sticker-failed:{reply.sticker_action_id}",
            }
        )

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
        if self.world_kernel and self.world_id:
            snapshot = self.world_kernel.snapshot(self.world_id)
            recent = snapshot.get("recent_messages", [])
            if any(
                str(item.get("sent_at") or "") > reply_sent_at.isoformat()
                for item in recent
                if isinstance(item, dict) and item.get("direction") == "in"
            ):
                return None
            prompt = (
                "只补一条不超过 60 字的聊天余波；不得新增任何未结算经历、人物或事实。"
                "若不该发送，reply_text 置空。只返回 WorldReplyJSON："
                '{"reply_text":"...","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}。\n'
                f"当前关系阶段: {str(snapshot.get('relationships', {}).get(self._world_user_id(canonical_user_id), {}).get('stage') or 'stranger')}；"
                f"阶段表达规则: {self.world_behavior_policy.expression_guidance(snapshot, user_id=self._world_user_id(canonical_user_id)).prompt_line}\n"
                f"当前情感投影(JSON): {json.dumps(snapshot.get('emotion_modulation', {}), ensure_ascii=False, separators=(',', ':'))}\n"
                f"已结算事实: {[item['value'] for item in self._current_world_facts(snapshot)]}\n"
                f"已结算经历: {[item['content'] for item in snapshot['experiences'].values()][-3:]}\n"
                f"模式: {mode}"
            )
            causation = f"afterthought:{reply_sent_at.isoformat()}:{mode}"
            model_action_id = self._begin_world_model_call(purpose="afterthought", causation=causation)
            try:
                raw = await self.model.complete([{"role": "user", "content": prompt}], temperature=0.7)
            except Exception as exc:
                self._fail_world_model_call(model_action_id, str(exc))
                logger.exception("world afterthought generation failed")
                return None
            self._record_world_model_output(
                purpose="afterthought", causation=causation, content=raw, action_id=model_action_id
            )
            try:
                candidate = self.world_kernel.validate_reply_candidate(
                    self.world_id,
                    parse_reply_candidate(raw),
                    user_id=self._ensure_world_user(canonical_user_id),
                )
            except WorldError:
                return None
            relationship = snapshot.get("relationships", {}).get(self._world_user_id(canonical_user_id), {})
            if not isinstance(relationship, dict) or human_reply_contract_violation(
                "", candidate, relationship
            ):
                return None
            if affect_reply_violation(
                snapshot.get("emotion_modulation", {})
                if isinstance(snapshot.get("emotion_modulation", {}), dict)
                else {},
                str(candidate.get("reply_text") or ""),
            ):
                return None
            text = sanitize_chat_text(str(candidate["reply_text"]))
            try:
                await self._audit_world_reply(
                    purpose="afterthought_audit",
                    causation=causation,
                    user_text="上一条回复之后的一句可取消聊天余波。",
                    reply_text=text,
                    grounding_context={
                        "facts": self._current_world_facts(snapshot),
                        "experiences": list(snapshot.get("experiences", {}).values())[-3:],
                        "emotion_modulation": snapshot.get("emotion_modulation", {}),
                    },
                )
            except WorldError:
                return None
            return text if text and len(text) <= 60 else None
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
        if self.world_kernel and self.world_id:
            # This entry point is also used by scheduler recovery.  Keep its
            # payload on the same grounded text boundary as normal replies.
            self.world_kernel.validate_reply_candidate(
                self.world_id,
                {"reply_text": text, "mentioned_event_ids": [], "proposed_action_ids": [], "claims": []},
                user_id=self._ensure_world_user(canonical_user_id),
            )
            delivery_id, _, _ = self.world_kernel.queue_outgoing_action(
                canonical_user_id=canonical_user_id,
                platform=platform,
                text=text,
                kind="afterthought",
                expires_at=self._world_logical_now() + timedelta(hours=2),
                trace={
                    "world_id": self.world_id,
                    "direction": "afterthought",
                    "appraisal": "conversation_pulse",
                    "expression_policy": "只补一句新信息；用户回来前可取消，不复读旧话。",
                    "allowed_facts": [],
                    "short_lived_constraint": None,
                    "observable_reason": "当前对话仍留有一段可取消的余韵。",
                },
            )
            return delivery_id
        delivery_id, _ = self.store.queue_outgoing_with_turn_trace(
            canonical_user_id,
            platform,
            text,
            kind="afterthought",
            appraisal="conversation_pulse",
            expression_policy="只补一句新信息；用户回来前可取消，不复读旧话。",
            allowed_facts=[],
            short_lived_constraint=None,
            observable_reason="当前对话仍留有一段可取消的余韵。",
            direction="afterthought",
        )
        return delivery_id

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
        if self.world_kernel and self.world_id:
            delivered = self.world_kernel.settle_outgoing_action(delivery_id, delivered=True)
        else:
            delivered = self.store.resolve_outgoing_and_turn_trace(
                delivery_id, self.store.turn_trace_id_for_delivery(delivery_id), delivered=True
            )
        if not delivered or delivered["status"] != "planned":
            return
        if self.world_kernel and self.world_id:
            return
        state = self.store.get_mood_state(canonical_user_id)
        expressed = apply_expression_after_reply(state, was_proactive=True)
        self.store.save_mood_state(canonical_user_id, expressed)
        synchronize_life_runtime(self.store, canonical_user_id, expressed)

    def fail_afterthought_delivery(self, delivery_id: int | None, reason: str) -> None:
        if delivery_id is not None:
            if self.world_kernel and self.world_id:
                self.world_kernel.settle_outgoing_action(
                    delivery_id, delivered=False, reason=reason
                )
            else:
                self.store.resolve_outgoing_and_turn_trace(
                    delivery_id,
                    self.store.turn_trace_id_for_delivery(delivery_id),
                    delivered=False,
                    failure_reason=reason,
                )

    def confirm_life_event_delivery(self, canonical_user_id: str, platform: str = "qq") -> None:
        if self.world_kernel and self.world_id:
            return
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
        # Do not reset the emotional clock when she sends a second bubble into
        # the same silence.  The relevant moment is when this unanswered turn
        # began, not the most recent attempt to fill it.
        last_sent = self.store.unanswered_outgoing_started_at(canonical_user_id)
        if last_sent is None:
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

    def outreach_block_reason(self, canonical_user_id: str, state: MoodState | None = None) -> str | None:
        """Return why she should not add another unsolicited bubble yet."""
        state = state or self.store.get_mood_state(canonical_user_id)
        limit = {
            "stranger": 1,
            "acquaintance": 1,
            "friend": 2,
            "close_friend": 2,
            "ambiguous": 2,
            "lover": 3,
        }[state.relationship_stage]
        streak = self.store.unanswered_outgoing_streak(canonical_user_id)
        latest_outgoing = self.store.latest_outgoing_at(canonical_user_id)
        age_minutes = (
            (utc_now() - datetime.fromisoformat(latest_outgoing)).total_seconds() / 60
            if latest_outgoing
            else 0
        )
        # The immediate response to a user turn is not a "chasing" pattern.
        # This gate starts after it has had time to become an unanswered turn.
        if streak >= limit and age_minutes >= 30:
            return f"她已经连续发了{streak}条而没得到新回应，应该把聊天空间还给对方。"
        if state.relationship_stage in {"stranger", "acquaintance"} and state.initiative <= 8:
            return "刚认识且主动欲已经收住，不为了证明存在感再开新话题。"
        return None

    def schedule_conversation_pulse(
        self,
        *,
        canonical_user_id: str,
        platform: str,
        platform_user_id: str,
        reply_sent_at: datetime,
        mode: str,
        delay_seconds: float,
        remaining: list[dict[str, object]],
    ) -> int | str:
        """Persist one tentative continuation bubble before its live timer runs."""
        now = utc_now()
        due_at = now + timedelta(seconds=max(1.0, delay_seconds))
        if self.world_kernel and self.world_id:
            logical_now = self._world_logical_now()
            due_at = logical_now + timedelta(seconds=max(1.0, delay_seconds))
            action_id = f"conversation_pulse:{canonical_user_id}:{reply_sent_at.isoformat()}:{mode}"
            self._submit_world_with_retry(
                {
                    "type": "schedule_action",
                    "world_id": self.world_id,
                    "action_id": action_id,
                    "kind": "conversation_pulse",
                    "expires_at": (due_at + timedelta(minutes=35)).isoformat(),
                    "payload": {
                        "due_at": due_at.isoformat(),
                        "canonical_user_id": canonical_user_id,
                        "platform": platform,
                        "platform_user_id": platform_user_id,
                        "reply_sent_at": reply_sent_at.isoformat(),
                        "mode": mode,
                        "remaining": remaining,
                    },
                    "idempotency_key": f"schedule:{action_id}",
                }
            )
            return action_id
        return self.store.create_social_task(
            canonical_user_id,
            kind="conversation_pulse",
            platform=platform,  # type: ignore[arg-type]
            platform_user_id=platform_user_id,
            payload={
                "reply_sent_at": reply_sent_at.isoformat(),
                "mode": mode,
                "remaining": remaining,
            },
            reason="这轮对话还留着一点没说完的余韵；用户回来就立刻取消，不重放旧话。",
            due_at=due_at,
            expires_at=due_at + timedelta(minutes=35),
        )

    def conversation_pulse_is_active(self, task_id: int | str | None) -> bool:
        if self.world_kernel and self.world_id and isinstance(task_id, str):
            return self.world_kernel.snapshot(self.world_id)["actions"].get(task_id, {}).get("status") == "scheduled"
        return task_id is None or self.store.social_task_is_active(task_id)

    def complete_conversation_pulse(self, task_id: int | str | None) -> None:
        if task_id is not None:
            if self.world_kernel and self.world_id and isinstance(task_id, str):
                self._submit_world_with_retry(
                    {"type": "record_external_result", "world_id": self.world_id, "action_id": task_id, "result": {"kind": "pulse", "status": "delivered"}, "idempotency_key": f"complete:{task_id}"}
                )
                return
            self.store.resolve_social_task(task_id)

    def cancel_conversation_pulse(self, task_id: int | str | None) -> None:
        if task_id is not None:
            if self.world_kernel and self.world_id and isinstance(task_id, str):
                self._submit_world_with_retry(
                    {"type": "cancel_action", "world_id": self.world_id, "action_id": task_id, "reason": "conversation_changed"}
                )
                return
            self.store.cancel_social_task(task_id)

    async def proactive_tick(self, canonical_user_id: str) -> ProactiveDecision:
        if self.world_kernel and self.world_id:
            self.world_kernel.recover_interrupted_outgoing_deliveries(self.world_id)
            return await self._world_proactive_tick(canonical_user_id)
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
                "withheld_impulse",
            ),
            now=now,
        )
        if social_task and social_task["kind"] == "life_share_followup":
            payload = social_task_payload(social_task)
            event_id = payload.get("life_event_id")
            event = (
                self.store.trusted_private_life_event(canonical_user_id, int(event_id))
                if isinstance(event_id, int)
                else None
            )
            if event is None:
                # Tasks survive restarts, so source validation must happen at
                # consumption time as well as task creation time.
                self.store.cancel_social_task(int(social_task["id"]))
                social_task = None
            else:
                return self._deterministic_life_share_decision(
                    canonical_user_id, state, runtime, social_task, str(event["content"])
                )
        outreach_block = self.outreach_block_reason(canonical_user_id, state)
        if outreach_block:
            if social_task:
                self.store.defer_social_task(int(social_task["id"]), due_at=now + timedelta(hours=2))
            decision = ProactiveDecision(
                canonical_user_id=canonical_user_id,
                private_thought=outreach_block,
                should_send=False,
            )
            self.store.save_proactive_event(
                canonical_user_id,
                decision.private_thought,
                False,
                None,
                "none",
                None,
                None,
                None,
                120,
            )
            return decision
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
        elif trigger and not decision.should_send and trigger.type != "withheld_impulse":
            impulse = build_withheld_impulse(
                trigger_type=trigger.type,
                private_thought=decision.private_thought,
            )
            if impulse:
                self.store.cancel_active_social_tasks(canonical_user_id, kind="withheld_impulse")
                self.store.create_social_task(
                    canonical_user_id,
                    kind="withheld_impulse",
                    platform="qq",
                    platform_user_id=self.store.platform_user_id(canonical_user_id, "qq") or "",
                    payload={"trigger_type": impulse.reason, "thought": decision.private_thought[:120]},
                    reason="有一句主动的话暂时忍住了，等一会儿再重新判断。",
                    due_at=utc_now() + timedelta(minutes=45),
                    expires_at=utc_now() + timedelta(hours=4),
                )
                state = apply_withheld_impulse(state, impulse)
                self.store.save_mood_state(canonical_user_id, state)
                runtime = synchronize_life_runtime(self.store, canonical_user_id, state)
        if decision.message:
            decision = decision.model_copy(update={"message": sanitize_chat_text(decision.message)})
        if social_task:
            if decision.should_send:
                decision = decision.model_copy(update={"social_task_id": int(social_task["id"])})
            elif social_task["kind"] == "withheld_impulse":
                # A held-back thought gets one later reconsideration.  If that
                # reconsideration still says no, letting it expire is a real
                # choice, not a timer that reopens the same thought forever.
                self.store.resolve_social_task(int(social_task["id"]))
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
            delivery_id, trace_id = self.store.queue_outgoing_with_turn_trace(
                canonical_user_id,
                decision.platform,
                decision.message or "",
                kind="proactive",
                appraisal=decision.trigger_type or "proactive",
                expression_policy="主动消息只轻轻开口，不索取回应。",
                allowed_facts=[],
                short_lived_constraint=None,
                observable_reason=decision.private_thought[:160],
                direction="proactive",
            )
            decision = decision.model_copy(
                update={
                    "delivery_id": delivery_id,
                    "turn_trace_id": trace_id,
                }
            )
        return decision

    def _settle_proactive_generation(
        self, action_id: str, *, status: str, reason: str
    ) -> None:
        assert self.world_kernel and self.world_id
        action = self.world_kernel.snapshot(self.world_id).get("actions", {}).get(
            action_id, {}
        )
        if not isinstance(action, dict) or action.get("status") not in {
            "scheduled",
            "sending",
        }:
            return
        self._submit_world_with_retry(
            {
                "type": "record_external_result",
                "world_id": self.world_id,
                "action_id": action_id,
                "result": {
                    "kind": "proactive_generation",
                    "status": status,
                    "reason": reason[:300],
                },
                "idempotency_key": f"generation:{action_id}:{status}",
            }
        )

    async def _world_proactive_tick(self, canonical_user_id: str) -> ProactiveDecision:
        """World-only proactive decision; it never reads or writes legacy mood/tasks."""
        assert self.world_kernel and self.world_id
        snapshot = self.world_kernel.snapshot(self.world_id)
        logical_now = self._world_logical_now()
        for action in self.world_kernel.due_actions(self.world_id, now=logical_now):
            if action.get("kind") == "proactive_generation":
                self._submit_world_with_retry(
                    {
                        "type": "cancel_action",
                        "world_id": self.world_id,
                        "action_id": str(action["action_id"]),
                        "reason": "proactive_generation_lease_expired",
                        "idempotency_key": f"expire:{action['action_id']}",
                    }
                )
                continue
            if action.get("kind") != "decision_review":
                continue
            payload = action.get("payload", {})
            decision_id = str(payload.get("decision_id") or "") if isinstance(payload, dict) else ""
            if decision_id:
                self._submit_world_with_retry(
                    {
                        "type": "resolve_deferred_decision", "world_id": self.world_id,
                        "decision_id": decision_id, "outcome": "abandoned",
                        "reason": "logical_review_elapsed_without_new_evidence",
                        "idempotency_key": f"decision-review:{decision_id}",
                    }
                )
        snapshot = self.world_kernel.snapshot(self.world_id)
        open_outgoing = [
            action
            for action in snapshot["actions"].values()
            if action["kind"] == "outgoing_message" and action["status"] == "scheduled"
        ]
        if open_outgoing:
            return ProactiveDecision(
                canonical_user_id=canonical_user_id,
                private_thought="已有一条等待结算的外发行动，先不追加。",
                should_send=False,
            )
        open_reviews = [
            action for action in snapshot["actions"].values()
            if action["kind"] == "decision_review" and action["status"] == "scheduled"
        ]
        if open_reviews:
            return ProactiveDecision(
                canonical_user_id=canonical_user_id,
                private_thought="刚刚决定先收住，等复核期限到了再判断。",
                should_send=False,
            )
        user_id = self._world_user_id(canonical_user_id)
        outreach = self.world_behavior_policy.outreach_constraint(snapshot, user_id=user_id)
        if not outreach.allowed:
            return ProactiveDecision(
                canonical_user_id=canonical_user_id,
                private_thought=f"世界行为规则要求先收住：{outreach.reason}。",
                should_send=False,
            )
        impulse_id = f"proactive:{self.world_kernel.revision(self.world_id)}"
        generation_action_id = f"proactive-generation:{impulse_id}"
        try:
            self._submit_world_with_retry(
                {
                    "type": "deliberate_proactive",
                    "world_id": self.world_id,
                    "impulse_id": impulse_id,
                    "user_id": user_id,
                    "generation_action_id": generation_action_id,
                    "idempotency_key": f"deliberate:{impulse_id}:{uuid4().hex}",
                }
            )
        except WorldError as exc:
            if "proactive generation is already in progress" not in str(exc):
                raise
            return ProactiveDecision(
                canonical_user_id=canonical_user_id,
                private_thought="另一轮主动判断正在生成，先不重复消耗模型和外发预算。",
                should_send=False,
            )
        snapshot = self.world_kernel.snapshot(self.world_id)
        selected_stance = str(snapshot.get("last_deliberation", {}).get("stance") or "")
        if selected_stance != "initiate":
            reason = (
                "角色选择保持沉默，暂不把这个念头变成对用户的打扰。"
                if selected_stance == "remain_silent"
                else "角色选择暂缓主动联系，等待精力或关系语境变化。"
            )
            self._submit_world_with_retry(
                {
                    "type": "defer_decision",
                    "world_id": self.world_id,
                    "decision_id": impulse_id,
                    "kind": f"proactive_{selected_stance or 'defer'}",
                    "reason": reason,
                    "review_at": (
                        self._world_logical_now() + timedelta(minutes=45)
                    ).isoformat(),
                    "idempotency_key": f"defer:{impulse_id}",
                }
            )
            return ProactiveDecision(
                canonical_user_id=canonical_user_id,
                private_thought=reason,
                should_send=False,
            )
        prompt = (
            "基于以下已结算世界账本，决定是否轻轻主动发一句消息。"
            "若不适合，返回 JSON 的 should_send=false；若适合，不能新增未记录事实。\n"
            f"当前关系阶段: {str(snapshot.get('relationships', {}).get(user_id, {}).get('stage') or 'stranger')}; "
            f"阶段表达规则: {self.world_behavior_policy.expression_guidance(snapshot, user_id=user_id).prompt_line}\n"
            f"当前情感投影(JSON): {json.dumps(snapshot.get('emotion_modulation', {}), ensure_ascii=False, separators=(',', ':'))}\n"
            "若 unresolved=true，不能假装已经没事、不能用亲密主动消息索取回应。"
            "这些关系和情绪约束是有代价的软压力，不是绝对禁令；角色可以为修复、关心或自主选择承担一次打扰风险，但必须克制且说明得通。\n"
            f"当前主动软压力: {outreach.reason if outreach.requires_deliberation else 'none'}; "
            f"越过代价: {outreach.override_cost}; strike: {outreach.override_strike}\n"
            f"事实: {[item['value'] for item in self._current_world_facts(snapshot)]}\n"
            f"经历: {[item['content'] for item in snapshot['experiences'].values()][-4:]}"
        )
        causation = f"proactive:{self.world_kernel.revision(self.world_id)}"
        model_action_id = self._begin_world_model_call(purpose="proactive", causation=causation)
        try:
            raw = await self.model.complete([{"role": "user", "content": prompt}], temperature=0.7)
        except Exception as exc:
            self._fail_world_model_call(model_action_id, str(exc))
            self._settle_proactive_generation(
                generation_action_id, status="failed", reason=type(exc).__name__
            )
            raise
        self._record_world_model_output(
            purpose="proactive",
            causation=causation, content=raw, action_id=model_action_id,
        )
        modulation = snapshot.get("emotion_modulation", {})
        decision = self._parse_decision(
            canonical_user_id,
            raw,
            MoodState(mood=public_mood(modulation if isinstance(modulation, dict) else {})),
        )
        relationship = snapshot.get("relationships", {}).get(user_id, {})
        if not isinstance(relationship, dict):
            relationship = {}
        if decision.should_send and decision.message:
            candidate_message = decision.message
            violation = human_reply_contract_violation(
                "",
                {"reply_text": candidate_message, "claims": []},
                relationship,
            )
            if violation:
                decision = decision.model_copy(
                    update={
                        "should_send": False,
                        "message": None,
                        "message_type": "none",
                        "private_thought": f"关系阶段门禁要求先收住：{violation}。",
                    }
                )
            if decision.should_send and decision.message:
                affect_violation = affect_reply_violation(
                    modulation if isinstance(modulation, dict) else {},
                    candidate_message,
                )
                if affect_violation:
                    decision = decision.model_copy(
                        update={
                            "should_send": False,
                            "message": None,
                            "message_type": "none",
                            "private_thought": f"情感投影不支持该主动表达：{affect_violation}。",
                        }
                    )
        if decision.should_send and decision.message:
            try:
                await self._audit_world_reply(
                    purpose="proactive_audit",
                    causation=causation,
                    user_text="主动联系，不索取用户回应。",
                    reply_text=decision.message,
                    grounding_context={
                        "facts": self._current_world_facts(snapshot),
                        "experiences": list(snapshot.get("experiences", {}).values())[-4:],
                        "emotion_modulation": modulation,
                    },
                )
            except WorldError:
                decision = decision.model_copy(
                    update={
                        "should_send": False,
                        "message": None,
                        "message_type": "none",
                        "private_thought": "主动消息未通过独立世界审计，先不发。",
                    }
                )
        if not decision.should_send or not decision.message or not decision.platform:
            self._settle_proactive_generation(
                generation_action_id, status="delivered", reason="decision_completed"
            )
            self._submit_world_with_retry(
                {
                    "type": "defer_decision", "world_id": self.world_id,
                    "decision_id": f"impulse:{model_action_id}", "kind": "withheld_impulse",
                    "reason": decision.private_thought[:160] or "当前不适合主动开口。",
                    "review_at": (self._world_logical_now() + timedelta(minutes=45)).isoformat(),
                    "idempotency_key": f"defer:impulse:{model_action_id}",
                }
            )
            return decision
        text = sanitize_chat_text(decision.message)
        outbound_override = (
            {
                "reason": f"deliberation_selected_outreach_despite:{outreach.reason}",
                "cost": outreach.override_cost,
                "strike": outreach.override_strike,
                "gates": [f"outreach:{outreach.reason}"],
            }
            if outreach.requires_deliberation else None
        )
        try:
            delivery_id, trace_id, action_id = self.world_kernel.queue_outgoing_action(
                canonical_user_id=canonical_user_id,
                platform=decision.platform,
                text=text,
                kind="proactive",
                expires_at=self._world_logical_now() + timedelta(hours=4),
                trace={
                    "world_id": self.world_id,
                    "direction": "proactive",
                    "appraisal": decision.trigger_type or "proactive",
                    "expression_policy": "主动消息只轻轻开口，不索取回应。",
                    "allowed_facts": [str(item["value"]) for item in self._current_world_facts(snapshot)],
                    "short_lived_constraint": None,
                    "observable_reason": decision.private_thought[:160],
                    "outbound_override": outbound_override,
                },
            )
        except WorldError as exc:
            reason = str(exc)
            if "transgression_" not in reason and "outbound policy rejected" not in reason:
                raise
            self._settle_proactive_generation(
                generation_action_id, status="delivered", reason="decision_completed"
            )
            self._submit_world_with_retry(
                {
                    "type": "defer_decision",
                    "world_id": self.world_id,
                    "decision_id": f"impulse:{model_action_id}:policy",
                    "kind": "withheld_impulse",
                    "reason": reason[:160],
                    "review_at": (self._world_logical_now() + timedelta(minutes=45)).isoformat(),
                    "idempotency_key": f"defer:impulse:{model_action_id}:policy",
                }
            )
            return decision.model_copy(
                update={
                    "should_send": False,
                    "message": None,
                    "message_type": "none",
                    "private_thought": f"这次越过预算还没恢复：{reason}。",
                }
            )
        self._settle_proactive_generation(
            generation_action_id, status="delivered", reason="decision_completed"
        )
        return decision.model_copy(
            update={"message": text, "delivery_id": delivery_id, "turn_trace_id": trace_id, "world_action_id": action_id}
        )

    def _deterministic_life_share_decision(
        self,
        canonical_user_id: str,
        state: MoodState,
        runtime,
        social_task,
        content: str,
    ) -> ProactiveDecision:
        allowed, reason = proactive_outreach_allowed(runtime)
        if not allowed:
            self.store.defer_social_task(int(social_task["id"]), due_at=utc_now() + timedelta(minutes=45))
            return ProactiveDecision(
                canonical_user_id=canonical_user_id,
                private_thought=f"已发生的小事暂时不适合分享：{reason}",
                should_send=False,
                trigger_type="life_share_followup",
            )
        fact = content.strip().rstrip("。！？!? ")
        decision = ProactiveDecision(
            canonical_user_id=canonical_user_id,
            private_thought="有一件已记录的小事，顺手和他分享。",
            should_send=True,
            platform="qq",
            message_type="text",
            message=f"{fact}。刚想起这件小事，想跟你说一下。",
            trigger_type="life_share_followup",
            cooldown_minutes=120,
            social_task_id=int(social_task["id"]),
        )
        self.store.save_proactive_event(
            canonical_user_id,
            decision.private_thought,
            decision.should_send,
            decision.platform,
            decision.message_type,
            decision.message,
            None,
            decision.trigger_type,
            decision.cooldown_minutes,
        )
        delivery_id, trace_id = self.store.queue_outgoing_with_turn_trace(
            canonical_user_id,
            "qq",
            decision.message or "",
            kind="proactive",
            appraisal="life_share_followup",
            expression_policy="仅分享已记录的生活事件，不补写新事实。",
            allowed_facts=[content],
            short_lived_constraint=None,
            observable_reason=decision.private_thought,
            direction="proactive",
        )
        return decision.model_copy(update={"delivery_id": delivery_id, "turn_trace_id": trace_id})

    def confirm_proactive_delivery(self, decision: ProactiveDecision) -> None:
        if decision.delivery_id is None:
            return
        if self.world_kernel and decision.world_action_id:
            delivered = self.world_kernel.settle_outgoing_action(decision.delivery_id, delivered=True)
        else:
            delivered = self.store.resolve_outgoing_and_turn_trace(
                decision.delivery_id, decision.turn_trace_id, delivered=True
            )
        if not delivered or delivered["status"] != "planned":
            return
        if self.world_kernel and decision.world_action_id:
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
            if self.world_kernel and decision.world_action_id:
                self.world_kernel.settle_outgoing_action(
                    decision.delivery_id, delivered=False, reason=reason
                )
            else:
                self.store.resolve_outgoing_and_turn_trace(
                    decision.delivery_id, decision.turn_trace_id, delivered=False, failure_reason=reason
                )
        if self.world_kernel and decision.world_action_id:
            return
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
            identity_keys = ("full_name", "english_name", "age", "hometown", "current_city", "school", "major", "year")
            for key in identity_keys:
                value = self.character_profile.identity.get(key)
                if value is None:
                    continue
                facts.append(f"角色档案/{key}: {value}")
            facts.extend(f"角色事实账本: {item}" for item in self.character_profile.canonical_facts)
        for event in self.store.recent_life_events(canonical_user_id, limit=6):
            if (
                event["kind"] != "private_life_event"
                or event["status"] != "completed"
                or not str(event["source"]).startswith("life_runtime:")
            ):
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
        if self.world_kernel and self.world_id:
            projection = self.world_kernel.daemon_dashboard_projection(self.world_id)
            recent = [
                self._format_recent_line(
                    direction=str(item.get("direction") or "in"), platform=platform,
                    text=str(item.get("text") or ""), sent_at=str(item.get("sent_at") or ""),
                )
                for item in self.world_kernel.snapshot(self.world_id).get("recent_messages", [])
                if isinstance(item, dict) and item.get("sent_at")
            ]
            facts = {
                str(item.get("fact_id") or index): item
                for index, item in enumerate(
                    self._current_world_facts(self.world_kernel.snapshot(self.world_id))
                )
            }
            experiences = self.world_kernel.snapshot(self.world_id).get("experiences", {})
            return {
                "canonical_user_id": canonical_user_id,
                **projection,
                "recent_life_events": [], "recent_turn_traces": [], "recent_tool_proposals": [],
                "recent": recent[-16:],
                "memories": [],
                "available_memories": [
                    {"kind": "world_fact", "content": str(item.get("value") or "")}
                    for item in facts.values() if isinstance(item, dict)
                ] + [
                    {"kind": "world_experience", "content": str(item.get("content") or "")}
                    for item in experiences.values() if isinstance(item, dict)
                ],
                "prompt_messages": [],
            }
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
                self_fact_lines=self._self_fact_lines(canonical_user_id),
                verified_user_fact_lines=self.store.active_fact_lines(canonical_user_id),
                calendar_context=calendar_context_for_message(
                    self.store, canonical_user_id, state, preview_text
                ),
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
                self_fact_lines=self._self_fact_lines(canonical_user_id),
                verified_user_fact_lines=self.store.active_fact_lines(canonical_user_id),
                calendar_context=None,
            )
            memories = context_package.memory_lines
        return {
            "canonical_user_id": canonical_user_id,
            "state": state.model_dump(mode="json"),
            "life_runtime": runtime.model_dump(mode="json"),
            "recent_life_events": [dict(row) for row in self.store.recent_life_events(canonical_user_id)],
            "calendar": calendar_ledger(self.store, canonical_user_id, state, past_days=15, future_days=15),
            "recent_social_tasks": [dict(row) for row in self.store.recent_social_tasks(canonical_user_id)],
            "recent_turn_traces": [dict(row) for row in self.store.recent_turn_traces(canonical_user_id)],
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


def _safe_failure_speech_act(
    query_scope,
    *,
    appraisal: str,
    request_kind: str,
    message_text: str,
) -> str:
    """Translate already-structured turn signals into a conservative speech act."""
    if query_scope.asks_epistemic_honesty:
        return "epistemic"
    if query_scope.asks_meta_agency:
        return "meta_agency"
    if query_scope.asks_relationship_status:
        return "relationship_probe"
    if query_scope.asks_opinion:
        return "opinion"
    if query_scope.offers_emotional_permission:
        return "emotional_permission"
    if request_kind in {"no_advice", "listen_only"}:
        return "shared_reaction"
    if query_scope.is_first_person_statement:
        return "current_disclosure"
    message_kind = classify_message(message_text)
    if message_kind == "farewell":
        return "brief_goodnight"
    if message_kind == "urgent" and query_scope.asks_data_recovery:
        return "urgent_data"
    if appraisal.startswith("repair_") or appraisal == "repair_attempt":
        return "repair"
    if appraisal == "user_vulnerable" or message_kind == "emotional":
        return "vulnerable_disclosure"
    if message_kind == "question":
        return "question"
    return "statement"


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
    if task["kind"] == "withheld_impulse":
        return ProactiveTrigger(
            "withheld_impulse",
            "刚才有一句主动的话选择忍住了。现在只重新判断是否仍值得轻轻开口；"
            "不要说自己一直在等、不要把没发出的念头当成已经说过，也不要追问用户。",
            58,
            "anxious_reach",
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
