import argparse
import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import random
import re
import time

import botpy
from botpy.message import C2CMessage, GroupMessage

from companion_daemon.engine import CompanionEngine
from companion_daemon.companion_turn import (
    CompanionTurn,
    DispatchAcceptance,
    ExternalObservation,
    ResponseBudget,
    SettlementOutcome,
    TurnBeat,
    TurnEnvelope,
    TurnOptions,
    TurnPresentation,
    TurnPresenter,
    TurnTransport,
)
from companion_daemon.config import get_settings
from companion_daemon.models import CompanionReply, IncomingMessage, MessageAttachment
from companion_daemon.im_timing import between_part_delay_seconds, initial_reply_delay_seconds
from companion_daemon.multimodal import attachment_kind
from companion_daemon.process_lock import AlreadyRunningError
from companion_daemon.qq_client import QQOfficialClient
from companion_daemon.qq_delivery import QQDelivery
from companion_daemon.qq_outbound_owner import (
    QQOutboundOwnerLease,
    qq_outbound_owner_lock_path,
    validate_qq_outbound_configuration,
)
from companion_daemon.reply_decision import (
    ReplyAction,
    ReplyDecision,
    classify_message,
    decide_reply,
    is_urgent_interrupt,
)
from companion_daemon.time import utc_now
from companion_daemon.turn_taking import TurnInput, TurnTakingPolicy
from companion_daemon.runtime import build_companion_engine
from companion_daemon.world import WorldError, WorldKernel

logger = logging.getLogger(__name__)

DEFERRED_CONTEXT_HINT = (
    "回复时机提示: 你隔了一段时间才回复这条消息。"
    "可以自然地带一点“刚在忙”或“刚看到”的感觉，但不要每次都解释，也不要道歉。"
)

GHOST_CONTEXT_HINT = (
    "回复时机提示: 她刚才看到了这条消息，但当时心情不好，故意没有马上回。"
    "现在过了一会儿才决定接话，语气可以延续那份情绪的余温，"
    "不要假装刚看到，也不要突然热情。"
)


class ReplyTarget:
    async def reply(self, **kwargs) -> object:
        raise NotImplementedError


class QQTurnTransport(TurnTransport):
    """Official-QQ text adapter: a receipt is required before delivery is true."""

    def __init__(
        self, reply_target: ReplyTarget, *, on_dispatch_started: Callable[[], None] | None = None
    ) -> None:
        self.reply_target = reply_target
        self.on_dispatch_started = on_dispatch_started

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        if self.on_dispatch_started:
            self.on_dispatch_started()
        response = await self.reply_target.reply(content=beat.text, msg_seq=_reply_msg_seq())
        receipt = QQDelivery.receipt_candidate(response)
        if not receipt:
            return DispatchAcceptance(
                status="unknown",
                reason="qq_reply_returned_without_durable_receipt",
            )
        return DispatchAcceptance(status="delivered", external_receipt=receipt)


class QQTurnPresenter(TurnPresenter):
    """Keeps non-text expression effects ordered around the text Action."""

    def __init__(
        self,
        engine: CompanionEngine,
        *,
        on_reply: Callable[[CompanionReply], Awaitable[None]] | None,
        on_sticker: Callable[[IncomingMessage, CompanionReply], Awaitable[object | None]] | None,
        on_image: Callable[[IncomingMessage, CompanionReply], Awaitable[object | None]] | None,
        on_reaction: Callable[
            [IncomingMessage, CompanionReply], Awaitable[DispatchAcceptance | None]
        ] | None,
        after_delivered: Callable[[CompanionReply], Awaitable[None]] | None,
        after_terminal: Callable[[], None],
        settle_external: Callable[[ExternalObservation], Awaitable[SettlementOutcome]] | None = None,
    ) -> None:
        self.engine = engine
        self.on_reply = on_reply
        self.on_sticker = on_sticker
        self.on_image = on_image
        self.on_reaction = on_reaction
        self.after_delivered = after_delivered
        self.after_terminal = after_terminal
        self.settle_external = settle_external

    @staticmethod
    def _reply(presentation: TurnPresentation) -> CompanionReply:
        return CompanionReply(
            canonical_user_id=presentation.canonical_user_id,
            mood="calm",
            text="",
            sticker_path=presentation.sticker_path,
            image_path=presentation.image_path,
            suggested_reaction=presentation.suggested_reaction,
            world_action_id=presentation.action_id,
            media_action_id=presentation.media_action_id,
            sticker_action_id=presentation.sticker_action_id,
        )

    async def before_text(self, presentation: TurnPresentation) -> None:
        if self.on_reaction is None or not presentation.suggested_reaction:
            return
        try:
            reply = self._reply(presentation)
            if self.settle_external is None:
                await self.on_reaction(presentation.incoming, reply)
                return
            action_id = self.engine.begin_reaction_delivery(presentation.incoming, reply)
            if not action_id:
                return
            acceptance = await self.on_reaction(presentation.incoming, reply)
            await self._settle_dispatch_acceptance(action_id, acceptance, effect="reaction")
        except Exception as exc:
            logger.exception("failed to send QQ reaction before text")
            if "action_id" in locals():
                await self._settle_expression(
                    action_id,
                    status="unknown",
                    reason=f"qq_reaction_exception:{type(exc).__name__}",
                )

    async def after_text(self, presentation: TurnPresentation, terminal_state: str) -> None:
        reply = self._reply(presentation)
        try:
            if terminal_state != "delivered":
                await self._settle_expression(
                    presentation.media_action_id,
                    status="cancelled",
                    reason=f"text_delivery_{terminal_state}",
                )
                await self._settle_expression(
                    presentation.sticker_action_id,
                    status="cancelled",
                    reason=f"text_delivery_{terminal_state}",
                )
                return
            if self.on_sticker and reply.sticker_path:
                try:
                    response = await self.on_sticker(presentation.incoming, reply)
                    await self._settle_expression_response(
                        presentation.sticker_action_id, response, effect="sticker"
                    )
                except Exception as exc:
                    logger.exception("failed to send QQ sticker reply")
                    await self._settle_expression(
                        presentation.sticker_action_id,
                        status="unknown",
                        reason=f"qq_sticker_exception:{type(exc).__name__}",
                    )
            elif reply.sticker_path:
                await self._settle_expression(
                    presentation.sticker_action_id,
                    status="failed",
                    reason="sticker_adapter_unavailable",
                )
            if self.on_image and reply.image_path:
                try:
                    response = await self.on_image(presentation.incoming, reply)
                    await self._settle_expression_response(
                        presentation.media_action_id, response, effect="image"
                    )
                except Exception as exc:
                    logger.exception("failed to send QQ image reply")
                    await self._settle_expression(
                        presentation.media_action_id,
                        status="unknown",
                        reason=f"qq_image_exception:{type(exc).__name__}",
                    )
            elif reply.image_path:
                await self._settle_expression(
                    presentation.media_action_id,
                    status="failed",
                    reason="image_adapter_unavailable",
                )
            if self.on_reply:
                await self.on_reply(reply)
            if self.after_delivered:
                await self.after_delivered(reply)
        finally:
            self.after_terminal()

    async def _settle_expression_response(
        self, action_id: str | None, response: object | None, *, effect: str
    ) -> None:
        receipt = QQDelivery.receipt_candidate(response)
        if receipt:
            await self._settle_expression(
                action_id,
                status="delivered",
                external_receipt=receipt,
            )
            return
        await self._settle_expression(
            action_id,
            status="unknown",
            reason=f"qq_{effect}_returned_without_durable_receipt",
        )

    async def _settle_dispatch_acceptance(
        self,
        action_id: str,
        acceptance: DispatchAcceptance | None,
        *,
        effect: str,
    ) -> None:
        if acceptance is None or acceptance.status in {"accepted", "unknown"}:
            await self._settle_expression(
                action_id,
                status="unknown",
                reason=(
                    acceptance.reason
                    if acceptance is not None and acceptance.reason
                    else f"qq_{effect}_returned_without_durable_receipt"
                ),
            )
            return
        await self._settle_expression(
            action_id,
            status=acceptance.status,
            external_receipt=acceptance.external_receipt,
            reason=acceptance.reason,
        )

    async def _settle_expression(
        self,
        action_id: str | None,
        *,
        status: str,
        external_receipt: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Record a non-text result through the owning CompanionTurn.

        ``None`` means no expression action was planned.  The small legacy
        fallback keeps non-World test doubles usable, while production World
        paths never call the old Engine convenience methods from this adapter.
        """
        if not action_id:
            return
        if self.settle_external is None:
            return
        observed_at = utc_now()
        if status == "unknown":
            await self.settle_external(
                ExternalObservation(
                    action_id=action_id,
                    kind="timeout",
                    observed_at=observed_at,
                    idempotency_key=f"qq-expression-unknown:{action_id}:{reason or 'unknown'}",
                    payload={"reason": reason or "adapter_result_uncertain"},
                    world_id=self.engine.world_id,
                )
            )
            return
        await self.settle_external(
            ExternalObservation(
                action_id=action_id,
                kind="media_result",
                observed_at=observed_at,
                idempotency_key=(
                    f"qq-expression:{status}:{action_id}:"
                    f"{external_receipt or reason or 'terminal'}"
                ),
                status=status,  # type: ignore[arg-type]
                payload={
                    "status": status,
                    "external_receipt": external_receipt,
                    "reason": reason,
                },
                world_id=self.engine.world_id,
            )
        )


class MissingDeliveryEvidenceError(RuntimeError):
    """The adapter call returned, but supplied no durable delivery evidence."""


def _mark_claimed_reply_segment_unknown(
    engine: object,
    reply: CompanionReply,
    *,
    segment_id: str,
    reason: str,
) -> bool:
    marker = getattr(engine, "mark_reply_part_unknown", None)
    if callable(marker):
        marker(reply, segment_id=segment_id, reason=reason)
        return True
    kernel = getattr(engine, "world_kernel", None)
    world_id = getattr(engine, "world_id", None)
    if kernel is None or world_id is None or reply.delivery_id is None:
        return False
    kernel.mark_outgoing_segment_unknown(
        reply.delivery_id,
        segment_id,
        reason=reason,
        expected_revision=kernel.revision(world_id),
    )
    return True


@dataclass
class QueuedQQMessage:
    incoming: IncomingMessage
    reply_target: ReplyTarget


@dataclass
class DeferredReply:
    merged: IncomingMessage
    reply_target: ReplyTarget
    task_id: int | str | None = None


@dataclass
class ActiveSend:
    incoming: IncomingMessage
    reply_target: ReplyTarget
    reply: CompanionReply | None = None
    cancel_before_next_part: bool = False
    interruption_message_id: str | None = None
    text_dispatch_started: bool = False


@dataclass(frozen=True)
class AfterthoughtPlan:
    mode: str
    delay_seconds: float
    probability: float


@dataclass(frozen=True)
class TurnRuntimeObservation:
    key: str
    outcome: str
    elapsed_seconds: float
    failure_type: str | None = None
    failure_reason: str | None = None


class QQMessageCoalescer:
    def __init__(
        self,
        engine: CompanionEngine,
        *,
        delay_seconds: float,
        turn_policy: TurnTakingPolicy | None = None,
        on_reply: Callable[[CompanionReply], Awaitable[None]] | None = None,
        on_sticker: Callable[[IncomingMessage, CompanionReply], Awaitable[object | None]] | None = None,
        on_image: Callable[[IncomingMessage, CompanionReply], Awaitable[object | None]] | None = None,
        on_reaction: Callable[
            [IncomingMessage, CompanionReply], Awaitable[DispatchAcceptance | None]
        ] | None = None,
        human_timing: bool = False,
        enable_reply_decision: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        on_turn_observation: Callable[[TurnRuntimeObservation], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        response_timeout_seconds: float | None = None,
    ):
        self.engine = engine
        self.delay_seconds = delay_seconds
        self.turn_policy = turn_policy or TurnTakingPolicy(short_wait_seconds=delay_seconds)
        self.on_reply = on_reply
        self.on_sticker = on_sticker
        self.on_image = on_image
        self.on_reaction = on_reaction
        self.human_timing = human_timing
        self.enable_reply_decision = enable_reply_decision
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.on_turn_observation = on_turn_observation
        self.monotonic = monotonic
        self.response_timeout_seconds = response_timeout_seconds
        self._pending: dict[str, list[QueuedQQMessage]] = defaultdict(list)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._deferred: dict[str, DeferredReply] = {}
        self._deferred_tasks: dict[str, asyncio.Task[None]] = {}
        self._afterthought_tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._active_sends: dict[str, ActiveSend] = {}
        self._active_turns: dict[str, CompanionTurn] = {}
        self._frozen_turn_contexts: dict[str, object] = {}
        set_media_delivery_handler = getattr(self.engine, "set_media_delivery_handler", None)
        if callable(set_media_delivery_handler):
            set_media_delivery_handler(self._deliver_background_media)

    async def _deliver_background_media(self, incoming: IncomingMessage, image_path: Path) -> bool:
        """Send an image after its text reply, outside the reply-generation latency path."""
        if self.on_image is None:
            return False
        canonical_user_id = self.engine.store.resolve_user(
            incoming.platform, incoming.platform_user_id
        )
        reply = CompanionReply(
            canonical_user_id=canonical_user_id,
            mood="calm",
            text="",
            image_path=str(image_path),
        )
        await self.on_image(incoming, reply)
        return True

    async def add(self, key: str, incoming: IncomingMessage, reply_target: ReplyTarget) -> None:
        self._cancel_afterthought(key)
        if key in self._active_sends:
            handled = await self._handle_mid_reply_interruption(key, incoming, reply_target)
            if handled:
                return
        if not self._pending[key] and hasattr(self.engine, "recover_input_merge"):
            recovered = self.engine.recover_input_merge(key)
            current_id = str(incoming.message_id or "")
            for prior in recovered:
                if current_id and str(prior.message_id or "") == current_id:
                    continue
                self._pending[key].append(
                    QueuedQQMessage(incoming=prior, reply_target=reply_target)
                )
        if len(self._pending[key]) >= 6:
            existing = self._tasks.get(key)
            if existing and not existing.done():
                existing.cancel()
            await self._flush_later(key, 0.0)
        if key in self._deferred:
            self._cancel_deferred(key)
            deferred = self._deferred.pop(key, None)
            if deferred:
                self._pending[key].append(
                    QueuedQQMessage(incoming=deferred.merged, reply_target=deferred.reply_target)
                )
            if self.enable_reply_decision and is_urgent_interrupt(incoming.text):
                self._pending[key].append(
                    QueuedQQMessage(incoming=incoming, reply_target=reply_target)
                )
                existing = self._tasks.get(key)
                if existing and not existing.done():
                    existing.cancel()
                self._tasks[key] = asyncio.create_task(self._flush_later(key, 0.2))
                return

        self._pending[key].append(QueuedQQMessage(incoming=incoming, reply_target=reply_target))
        decision = self._decision_for(key)
        if hasattr(self.engine, "record_input_merge_candidate"):
            self.engine.record_input_merge_candidate(
                key, incoming, decision, pending_count=len(self._pending[key])
            )
        existing = self._tasks.get(key)
        if existing and not existing.done():
            existing.cancel()
        self._tasks[key] = asyncio.create_task(self._flush_later(key, decision.wait_seconds))

    async def _handle_mid_reply_interruption(
        self,
        key: str,
        incoming: IncomingMessage,
        reply_target: ReplyTarget,
    ) -> bool:
        active = self._active_sends[key]
        decision = classify_mid_reply_interruption(incoming.text)
        active_turn = self._active_turns.get(key)
        if active_turn is not None:
            if not active.text_dispatch_started:
                try:
                    envelope = TurnEnvelope.from_message(
                        incoming,
                        idempotency_key=(
                            f"{incoming.platform}:{incoming.platform_user_id}:{incoming.message_id}"
                        ),
                    )
                    await active_turn.interrupt(envelope, kind="substantive")
                except Exception:
                    logger.exception("failed to cancel pre-dispatch QQ v2 Action for %s", key)
                if not self._pending[key]:
                    self._pending[key].append(
                        QueuedQQMessage(incoming=active.incoming, reply_target=active.reply_target)
                    )
                self._pending[key].append(
                    QueuedQQMessage(incoming=incoming, reply_target=reply_target)
                )
                if hasattr(self.engine, "record_input_merge_candidate"):
                    self.engine.record_input_merge_candidate(
                        key,
                        incoming,
                        self._decision_for(key),
                        pending_count=len(self._pending[key]),
                    )
                existing = self._tasks.get(key)
                if existing and not existing.done():
                    existing.cancel()
                self._tasks[key] = asyncio.create_task(self._flush_later(key, self.delay_seconds))
                logger.info("superseded un-dispatched QQ v2 turn for %s", key)
                return True
            if decision == "backchannel":
                await self._record_without_reply(incoming, mark_unread=False)
                logger.info("kept sending after QQ v2 backchannel interruption for %s", key)
                return True
            if decision == "takeover":
                try:
                    envelope = TurnEnvelope.from_message(
                        incoming,
                        idempotency_key=(
                            f"{incoming.platform}:{incoming.platform_user_id}:{incoming.message_id}"
                        ),
                    )
                    await active_turn.interrupt(envelope, kind="substantive")
                except Exception:
                    logger.exception("failed to interrupt QQ v2 outgoing Action for %s", key)
                self._pending[key].append(
                    QueuedQQMessage(incoming=incoming, reply_target=reply_target)
                )
                existing = self._tasks.get(key)
                if existing and not existing.done():
                    existing.cancel()
                self._tasks[key] = asyncio.create_task(self._flush_later(key, self.delay_seconds))
                logger.info("interrupted QQ v2 remaining beats for %s", key)
                return True
        if decision == "backchannel":
            if active.reply and hasattr(self.engine, "observe_reply_interjection"):
                self.engine.observe_reply_interjection(
                    active.reply,
                    kind="backchannel",
                    user_message_id=str(incoming.message_id or incoming.sent_at.isoformat()),
                )
            await self._record_without_reply(incoming, mark_unread=False)
            logger.info("kept sending after backchannel interruption for %s", key)
            return True
        if decision == "takeover":
            active.cancel_before_next_part = True
            active.interruption_message_id = str(
                incoming.message_id or incoming.sent_at.isoformat()
            )
            if active.reply and hasattr(self.engine, "observe_reply_interjection"):
                self.engine.observe_reply_interjection(
                    active.reply,
                    kind="substantive",
                    user_message_id=active.interruption_message_id,
                )
            self._pending[key].append(QueuedQQMessage(incoming=incoming, reply_target=reply_target))
            existing = self._tasks.get(key)
            if existing and not existing.done():
                existing.cancel()
            self._tasks[key] = asyncio.create_task(self._flush_later(key, self.delay_seconds))
            logger.info("stopping remaining reply parts after user takeover for %s", key)
            return True
        return False

    async def _record_without_reply(self, incoming: IncomingMessage, *, mark_unread: bool) -> None:
        try:
            await self.engine.handle_message(incoming, skip_reply=True, mark_unread=mark_unread)
        except TypeError:
            await self.engine.handle_message(incoming)

    def _decision_for(self, key: str):
        queued = self._pending[key]
        latest = queued[-1].incoming.text if queued else ""
        merged = "\n".join(item.incoming.text for item in queued if item.incoming.text.strip())
        cadence = None
        if queued and hasattr(self.engine, "freeze_turn_context"):
            context = self.engine.freeze_turn_context(queued[-1].incoming)
            self._frozen_turn_contexts[key] = context
            cadence = context.cadence
        elif queued and hasattr(self.engine, "conversation_cadence"):
            cadence = self.engine.conversation_cadence(queued[-1].incoming)
        return self.turn_policy.decide(
            TurnInput(pending_count=len(queued), latest_text=latest, merged_text=merged),
            cadence=cadence,
        )

    async def _flush_later(self, key: str, wait_seconds: float) -> None:
        started_at = self.monotonic()
        try:
            await self.sleep(wait_seconds)
            queued = self._pending.pop(key, [])
            if not queued:
                return
            last = queued[-1]
            merged_text = "\n".join(
                item.incoming.text for item in queued if item.incoming.text.strip()
            )
            attachments = [
                attachment for item in queued for attachment in item.incoming.attachments
            ]
            merged = last.incoming.model_copy(
                update={
                    "text": merged_text,
                    "attachments": attachments,
                    "emoji": [emoji for item in queued for emoji in item.incoming.emoji][:16],
                    "source_message_ids": [
                        str(item.incoming.message_id or item.incoming.sent_at.isoformat())
                        for item in queued
                    ],
                }
            )

            if hasattr(self.engine, "phone_attention_decision"):
                attention = self.engine.phone_attention_decision(merged)
                if not attention.read_now and attention.defer_minutes:
                    task_id = self._persist_deferred_reply(
                        merged, attention.defer_minutes, attention.reason, attention.turn_trace_id
                    )
                    self._deferred[key] = DeferredReply(
                        merged=merged, reply_target=last.reply_target, task_id=task_id
                    )
                    self._deferred_tasks[key] = asyncio.create_task(
                        self._fire_deferred_after(
                            key,
                            attention.defer_minutes,
                            ReplyDecision(
                                ReplyAction.DEFER,
                                defer_minutes=attention.defer_minutes,
                                reason=attention.reason,
                                mark_unread=True,
                            ),
                        )
                    )
                    logger.info(
                        "left message unread for %s by %.1f min (%s)",
                        key,
                        attention.defer_minutes,
                        attention.reason,
                    )
                    self._settle_input_merge(key, queued)
                    return

            world_mode = isinstance(
                getattr(self.engine, "world_kernel", None), WorldKernel
            ) and bool(getattr(self.engine, "world_id", None))
            if self.enable_reply_decision and not world_mode:
                has_unread = False
                mood_state = None
                try:
                    canonical_user_id = self.engine.store.resolve_user(
                        merged.platform, merged.platform_user_id
                    )
                    mood_state = self.engine.store.get_mood_state(canonical_user_id)
                    has_unread = mood_state.has_unread
                    recent_context_open = _recent_context_open(
                        self.engine.store.recent_messages(canonical_user_id, limit=6)
                    )
                except Exception:
                    logger.exception("failed to load reply decision state")
                    recent_context_open = False
                action = decide_reply(
                    merged_text,
                    state=mood_state,
                    has_pending_reply=key in self._deferred,
                    has_unread=has_unread,
                    recent_context_open=recent_context_open,
                    has_attachments=bool(merged.attachments),
                    rng=self.rng,
                )
                if action.action == ReplyAction.SKIP:
                    await self.engine.handle_message(
                        merged, skip_reply=True, mark_unread=action.mark_unread
                    )
                    self._settle_input_merge(key, queued)
                    return
                if action.action == ReplyAction.DEFER and action.defer_minutes:
                    # At this point phone attention already said "read now", so this
                    # is the read-but-sidetracked case rather than an unread defer.
                    task_id = self._persist_read_later(
                        merged,
                        action.defer_minutes,
                        action.reason,
                        attention.turn_trace_id if "attention" in locals() else None,
                    )
                    self._deferred[key] = DeferredReply(
                        merged=merged, reply_target=last.reply_target, task_id=task_id
                    )
                    self._deferred_tasks[key] = asyncio.create_task(
                        self._fire_deferred_after(key, action.defer_minutes, action)
                    )
                    logger.info(
                        "deferred reply for %s by %.1f min (%s)",
                        key,
                        action.defer_minutes,
                        action.reason,
                    )
                    self._settle_input_merge(key, queued)
                    return

            frozen_context = self._frozen_turn_contexts.pop(key, None)
            response_budget = self.response_timeout_seconds
            if response_budget is None:
                heat = str(getattr(getattr(frozen_context, "cadence", None), "heat", "cold"))
                response_budget = {"hot": 5.0, "warm": 8.0, "cold": 12.0}.get(heat, 12.0)
            # The deadline is user-perceived turn time, not a fresh budget
            # granted after coalescing has already consumed several seconds.
            generation_budget = response_budget - (self.monotonic() - started_at)
            if world_mode and generation_budget <= 0:
                expired_turn = TurnEnvelope.from_message(
                    merged,
                    idempotency_key=(
                        f"{merged.platform}:{merged.platform_user_id}:{merged.message_id}"
                    ),
                )
                await CompanionTurn(
                    self.engine,
                    QQTurnTransport(last.reply_target),
                    sleep=self.sleep,
                ).observe_expired(expired_turn)
                self._settle_input_merge(key, queued)
                self._observe_turn(
                    TurnRuntimeObservation(
                        key=key,
                        outcome="response_budget_expired_before_dispatch",
                        elapsed_seconds=max(0.0, self.monotonic() - started_at),
                        failure_type="TimeoutError",
                        failure_reason="user_perceived_budget_exhausted",
                    )
                )
                return
            generation_budget = max(0.05, generation_budget)
            v2_budget = ResponseBudget(
                first_visible_by_ms=max(50, int(generation_budget * 1_000)),
                complete_by_ms=max(50, int(generation_budget * 1_000)),
            )
            if world_mode:
                delivered = await self._generate_and_send(
                    merged,
                    last.reply_target,
                    key=key,
                    turn_context=frozen_context,
                    budget=v2_budget,
                )
            else:
                async with asyncio.timeout(generation_budget):
                    delivered = await self._generate_and_send(
                        merged,
                        last.reply_target,
                        key=key,
                        turn_context=frozen_context,
                    )
            self._settle_input_merge(key, queued)
            self._observe_turn(
                TurnRuntimeObservation(
                    key=key,
                    outcome="reply_delivered" if delivered else "no_reply_delivered",
                    elapsed_seconds=max(0.0, self.monotonic() - started_at),
                )
            )
        except (WorldError, TimeoutError) as exc:
            if "world_mode" in locals() and world_mode:
                logger.exception("CompanionTurn failed for %s without adapter fallback", key)
                if "queued" in locals():
                    self._settle_input_merge(key, queued)
                self._observe_turn(
                    TurnRuntimeObservation(
                        key=key,
                        outcome="companion_turn_failed",
                        elapsed_seconds=max(0.0, self.monotonic() - started_at),
                        failure_type=type(exc).__name__,
                        failure_reason=str(exc)[:240],
                    )
                )
                return
            logger.exception("world reply failed for %s; delivering bounded adapter fallback", key)
            delivered = False
            if "last" in locals() and "merged" in locals():
                try:
                    fallback_text = _safe_live_failure_reply(merged.text)
                    prepared_reply = None
                    if hasattr(self.engine, "prepare_adapter_failure_reply"):
                        prepared_reply = self.engine.prepare_adapter_failure_reply(
                            merged,
                            fallback_text,
                            failure_reason=str(exc),
                        )
                    segment_id = None
                    if prepared_reply is not None and hasattr(
                        self.engine, "begin_reply_part_delivery"
                    ):
                        segment_id = self.engine.begin_reply_part_delivery(
                            prepared_reply, position=0
                        )
                    response = await last.reply_target.reply(
                        content=fallback_text, msg_seq=_reply_msg_seq()
                    )
                    if segment_id and hasattr(self.engine, "confirm_reply_part_delivery"):
                        receipt = QQDelivery.receipt_candidate(response)
                        if not receipt:
                            raise MissingDeliveryEvidenceError(
                                "QQ fallback returned without durable delivery evidence"
                            )
                        self.engine.confirm_reply_part_delivery(
                            prepared_reply,
                            segment_id=segment_id,
                            external_receipt=receipt,
                        )
                    if prepared_reply is not None and hasattr(
                        self.engine, "confirm_reply_delivery"
                    ):
                        self.engine.confirm_reply_delivery(prepared_reply)
                    delivered = True
                except Exception as fallback_error:
                    logger.exception("failed to deliver bounded adapter fallback for %s", key)
                    if (
                        "prepared_reply" in locals()
                        and prepared_reply is not None
                        and hasattr(self.engine, "fail_reply_delivery")
                    ):
                        self.engine.fail_reply_delivery(
                            prepared_reply, f"QQ adapter fallback failed: {fallback_error}"
                        )
                if "queued" in locals():
                    self._settle_input_merge(key, queued)
            self._observe_turn(
                TurnRuntimeObservation(
                    key=key,
                    outcome=(
                        ("deadline_fallback_delivered" if delivered else "deadline_fallback_failed")
                        if isinstance(exc, TimeoutError)
                        else (
                            "world_error_fallback_delivered"
                            if delivered
                            else "world_error_fallback_failed"
                        )
                    ),
                    elapsed_seconds=max(0.0, self.monotonic() - started_at),
                    failure_type=type(exc).__name__,
                    failure_reason=str(exc)[:240],
                )
            )
        except asyncio.CancelledError:
            if "merged" in locals() and hasattr(self.engine, "settle_cancelled_world_turn"):
                try:
                    cancelled_message = merged
                    if key in self._active_turns:
                        cancelled_message = TurnEnvelope.from_message(
                            merged,
                            idempotency_key=(
                                f"{merged.platform}:{merged.platform_user_id}:{merged.message_id}"
                            ),
                        ).message
                    self.engine.settle_cancelled_world_turn(cancelled_message)
                except Exception:
                    logger.exception("failed to settle cancelled world turn for %s", key)
            return
        finally:
            self._frozen_turn_contexts.pop(key, None)
            task = self._tasks.get(key)
            if task is asyncio.current_task():
                self._tasks.pop(key, None)

    def _observe_turn(self, observation: TurnRuntimeObservation) -> None:
        logger.info(
            "QQ turn outcome=%s key=%s elapsed_seconds=%.3f failure_type=%s",
            observation.outcome,
            observation.key,
            observation.elapsed_seconds,
            observation.failure_type or "none",
        )
        if self.on_turn_observation is None:
            return
        try:
            self.on_turn_observation(observation)
        except Exception:
            logger.exception("QQ turn observer failed for %s", observation.key)

    def _settle_input_merge(self, key: str, queued: list[QueuedQQMessage]) -> None:
        if hasattr(self.engine, "settle_input_merge"):
            self.engine.settle_input_merge(key, tuple(item.incoming for item in queued))

    async def _fire_deferred_after(self, key: str, minutes: float, decision: ReplyDecision) -> None:
        try:
            await self.sleep(minutes * 60)
            deferred = self._deferred.pop(key, None)
            self._deferred_tasks.pop(key, None)
            if not deferred:
                return
            logger.info("firing deferred reply for %s", key)
            context_hint = (
                GHOST_CONTEXT_HINT
                if decision.reason == "emotional_ghost"
                else DEFERRED_CONTEXT_HINT
            )
            delivered = await self._generate_and_send(
                deferred.merged, deferred.reply_target, key=key, context_hint=context_hint
            )
            if delivered and hasattr(self.engine, "complete_deferred_reply_task"):
                self.engine.complete_deferred_reply_task(deferred.task_id)
        except asyncio.CancelledError:
            return

    def _cancel_deferred(self, key: str) -> None:
        task = self._deferred_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
        deferred = self._deferred.get(key)
        if deferred and hasattr(self.engine, "cancel_deferred_reply_task"):
            self.engine.cancel_deferred_reply_task(deferred.task_id)

    def _persist_deferred_reply(
        self,
        message: IncomingMessage,
        minutes: float,
        reason: str,
        turn_trace_id: int | None = None,
    ) -> int | None:
        if not hasattr(self.engine, "create_deferred_reply_task"):
            return None
        try:
            return self.engine.create_deferred_reply_task(
                message, defer_minutes=minutes, reason=reason, turn_trace_id=turn_trace_id
            )
        except TypeError:
            return self.engine.create_deferred_reply_task(
                message, defer_minutes=minutes, reason=reason
            )
        except Exception:
            logger.exception("failed to persist deferred reply; keeping in-memory fallback")
            return None

    def _persist_read_later(
        self,
        message: IncomingMessage,
        minutes: float,
        reason: str,
        turn_trace_id: int | None = None,
    ) -> int | None:
        if not hasattr(self.engine, "create_read_later_task"):
            return self._persist_deferred_reply(message, minutes, reason, turn_trace_id)
        try:
            return self.engine.create_read_later_task(
                message, defer_minutes=minutes, reason=reason, turn_trace_id=turn_trace_id
            )
        except TypeError:
            return self.engine.create_read_later_task(message, defer_minutes=minutes, reason=reason)
        except Exception:
            logger.exception("failed to persist read-later task; keeping in-memory fallback")
            return None

    async def _generate_and_send_v2(
        self,
        merged: IncomingMessage,
        reply_target: ReplyTarget,
        *,
        key: str,
        context_hint: str | None,
        turn_context: object | None,
        budget: ResponseBudget | None,
    ) -> bool:
        """World-mode text path: CompanionTurn owns every text Action transition."""
        response_seconds = self.response_timeout_seconds or 12.0
        resolved_budget = budget or ResponseBudget(
            first_visible_by_ms=max(50, int(response_seconds * 1_000)),
            complete_by_ms=max(50, int(response_seconds * 1_000)),
        )

        def terminal() -> None:
            self._active_sends.pop(key, None)
            self._active_turns.pop(key, None)

        async def delivered(reply: CompanionReply) -> None:
            self._schedule_afterthought(key, merged, reply_target, utc_now())

        presenter = QQTurnPresenter(
            self.engine,
            on_reply=self.on_reply,
            on_sticker=self.on_sticker,
            on_image=self.on_image,
            on_reaction=self.on_reaction,
            after_delivered=delivered,
            after_terminal=terminal,
        )
        active_send = ActiveSend(incoming=merged, reply_target=reply_target)
        self._active_sends[key] = active_send
        runtime = CompanionTurn(
            self.engine,
            QQTurnTransport(
                reply_target,
                on_dispatch_started=lambda: setattr(active_send, "text_dispatch_started", True),
            ),
            sleep=self.sleep,
            presenter=presenter,
        )
        presenter.settle_external = runtime.settle
        self._active_turns[key] = runtime
        await runtime.start()
        try:
            outcome = await runtime.respond(
                TurnEnvelope.from_message(
                    merged,
                    idempotency_key=(
                        f"{merged.platform}:{merged.platform_user_id}:{merged.message_id}"
                    ),
                ),
                budget=resolved_budget,
                options=TurnOptions(context_hint=context_hint, turn_context=turn_context),
            )
        except Exception:
            terminal()
            raise
        if outcome.visible_status in {"failed", "unknown"}:
            terminal()
            return False
        return True

    async def _generate_and_send(
        self,
        merged: IncomingMessage,
        reply_target: ReplyTarget,
        *,
        key: str = "",
        context_hint: str | None = None,
        turn_context: object | None = None,
        budget: ResponseBudget | None = None,
    ) -> bool:
        if hasattr(self.engine, "mark_phone_read_for_message"):
            self.engine.mark_phone_read_for_message(merged)
        has_world_runtime = bool(
            getattr(self.engine, "world_kernel", None) and getattr(self.engine, "world_id", None)
        )
        world_v2 = (
            isinstance(getattr(self.engine, "world_kernel", None), WorldKernel)
            and has_world_runtime
        )
        if world_v2:
            return await self._generate_and_send_v2(
                merged,
                reply_target,
                key=key,
                context_hint=context_hint,
                turn_context=turn_context,
                budget=budget,
            )
        world_mode = has_world_runtime
        typing_started = has_world_runtime
        if not has_world_runtime and hasattr(self.engine, "begin_world_typing"):
            try:
                self.engine.begin_world_typing(merged)
                typing_started = True
            except Exception:
                logger.exception("failed to record early world typing state")
        kwargs: dict[str, object] = {}
        if context_hint:
            kwargs["context_hint"] = context_hint
        kwargs["defer_delivery"] = True
        if turn_context is not None:
            kwargs["turn_context"] = turn_context
        try:
            try:
                reply = await self.engine.handle_message(merged, **kwargs)
            except TypeError:
                # Lightweight test/dummy engines may not expose delivery staging.
                kwargs.pop("defer_delivery", None)
                reply = await self.engine.handle_message(merged, **kwargs)
        except asyncio.CancelledError:
            if typing_started and hasattr(self.engine, "stop_world_typing"):
                self.engine.stop_world_typing(merged, reason="reply_generation_cancelled")
            raise
        except Exception:
            if typing_started and hasattr(self.engine, "stop_world_typing"):
                self.engine.stop_world_typing(merged, reason="reply_generation_failed")
            raise
        if reply is None:
            if typing_started and hasattr(self.engine, "stop_world_typing"):
                self.engine.stop_world_typing(merged, reason="no_reply_selected")
            return False
        if self.on_reaction and reply.suggested_reaction:
            # She reacts to the message as she reads it, before typing a reply.
            try:
                await self.on_reaction(merged, reply)
            except Exception:
                logger.exception("failed to send QQ emoji reaction")
        timing_state = None
        if (
            self.human_timing
            and not world_mode
            and hasattr(self.engine, "store")
            and hasattr(self.engine.store, "get_mood_state")
        ):
            try:
                timing_state = self.engine.store.get_mood_state(reply.canonical_user_id)
            except Exception:
                logger.exception("failed to load state for reply timing")
        try:
            # In world mode the old stochastic timing model is not allowed to
            # make a hidden behavioural decision.  A future delayed send must
            # first be scheduled as a world action; an immediately dispatched
            # reply has no adapter-side delay to simulate.
            if self.human_timing and not world_mode:
                await self.sleep(
                    initial_reply_delay_seconds(merged, reply, state=timing_state, rng=self.rng)
                )
            self._active_sends[key] = ActiveSend(
                incoming=merged, reply_target=reply_target, reply=reply
            )
            claimed_segments: dict[int, str] = {}
            settled_segments: set[str] = set()

            def before_part(index: int, _part: str) -> None:
                if hasattr(self.engine, "begin_reply_part_delivery"):
                    segment_id = self.engine.begin_reply_part_delivery(reply, position=index)
                    if segment_id:
                        claimed_segments[index] = segment_id

            def after_part(index: int, _part: str, response: object) -> None:
                segment_id = claimed_segments.get(index)
                if segment_id and hasattr(self.engine, "confirm_reply_part_delivery"):
                    external_receipt = QQDelivery.receipt_candidate(response)
                    if not external_receipt:
                        raise MissingDeliveryEvidenceError(
                            "QQ reply returned without a platform message id or queryable receipt"
                        )
                    self.engine.confirm_reply_part_delivery(
                        reply,
                        segment_id=segment_id,
                        external_receipt=external_receipt,
                    )
                    settled_segments.add(segment_id)

            sent_completely = await _send_reply_parts(
                reply_target,
                reply.text_parts or [reply.text],
                part_delays_ms=reply.part_delays_ms,
                sleep=self.sleep,
                rng=self.rng,
                human_timing=self.human_timing,
                should_continue=lambda: (
                    not self._active_sends.get(
                        key, ActiveSend(merged, reply_target)
                    ).cancel_before_next_part
                ),
                before_part=before_part,
                after_part=after_part,
            )
            if not sent_completely:
                active_send = self._active_sends.get(key)
                cancelled = ()
                if (
                    active_send
                    and active_send.interruption_message_id
                    and hasattr(self.engine, "observe_reply_interjection")
                ):
                    cancelled = self.engine.observe_reply_interjection(
                        reply,
                        kind="substantive",
                        user_message_id=active_send.interruption_message_id,
                    )
                if not cancelled and hasattr(self.engine, "fail_reply_delivery"):
                    self.engine.fail_reply_delivery(
                        reply, "QQ reply interrupted before all parts were sent"
                    )
                return False
        except Exception:
            logger.exception("failed to send QQ reply")
            uncertain_reason = "QQ adapter call ended without durable delivery evidence"
            uncertain_segments = (
                [
                    segment_id
                    for segment_id in claimed_segments.values()
                    if segment_id not in settled_segments
                ]
                if "claimed_segments" in locals()
                else []
            )
            for segment_id in uncertain_segments:
                try:
                    _mark_claimed_reply_segment_unknown(
                        self.engine,
                        reply,
                        segment_id=segment_id,
                        reason=uncertain_reason,
                    )
                except Exception:
                    logger.exception("failed to persist uncertain QQ segment")
            if not uncertain_segments and hasattr(self.engine, "fail_reply_delivery"):
                self.engine.fail_reply_delivery(reply, "QQ text delivery failed")
            return False
        finally:
            if typing_started and hasattr(self.engine, "stop_world_typing"):
                self.engine.stop_world_typing(
                    merged,
                    reason="reply_sent"
                    if "sent_completely" in locals() and sent_completely
                    else "reply_send_stopped",
                )
            self._active_sends.pop(key, None)
        if hasattr(self.engine, "confirm_reply_delivery"):
            self.engine.confirm_reply_delivery(reply)
        if self.on_sticker and reply.sticker_path:
            try:
                await self.on_sticker(merged, reply)
                if hasattr(self.engine, "confirm_sticker_delivery"):
                    self.engine.confirm_sticker_delivery(reply)
            except Exception:
                logger.exception("failed to send QQ sticker reply")
                if hasattr(self.engine, "fail_sticker_delivery"):
                    self.engine.fail_sticker_delivery(reply, "QQ sticker delivery failed")
        elif reply.sticker_path and hasattr(self.engine, "fail_sticker_delivery"):
            self.engine.fail_sticker_delivery(reply, "sticker adapter unavailable")
        if self.on_image and reply.image_path:
            try:
                await self.on_image(merged, reply)
                if hasattr(self.engine, "confirm_media_delivery"):
                    self.engine.confirm_media_delivery(reply)
            except Exception:
                logger.exception("failed to send QQ image reply")
                if hasattr(self.engine, "fail_media_delivery"):
                    self.engine.fail_media_delivery(reply, "QQ image delivery failed")
        elif reply.image_path and hasattr(self.engine, "fail_media_delivery"):
            self.engine.fail_media_delivery(reply, "image adapter unavailable")
        if self.on_reply:
            await self.on_reply(reply)
        self._schedule_afterthought(key, merged, reply_target, utc_now())
        return True

    def _schedule_afterthought(
        self,
        key: str,
        merged: IncomingMessage,
        reply_target: ReplyTarget,
        reply_sent_at: datetime,
    ) -> None:
        if not self.human_timing:
            return
        try:
            canonical_user_id = self.engine.store.resolve_user(
                merged.platform, merged.platform_user_id
            )
            state = self.engine.store.get_mood_state(canonical_user_id)
            if state.mood in {"hurt", "guarded"} or state.boundary_level >= 20:
                logger.info(
                    "did not schedule afterthought for %s because she is keeping a boundary", key
                )
                return
        except (AttributeError, KeyError):
            # Lightweight test doubles and adapters without a mood store do not
            # participate in the optional afterthought feature.
            pass
        plans = _afterthought_plans(merged.text, self.rng)
        selected: list[AfterthoughtPlan] = []
        for plan in plans:
            if self.rng.random() <= plan.probability:
                selected.append(plan)
            # A small thread can breathe twice, but never turns into the agent
            # replying to itself for the rest of the evening.
            if len(selected) == 2:
                break
        if not selected:
            logger.info("no afterthought planned for %s", key)
            return
        first = selected[0]
        remaining = _pulse_remaining(selected[1:], first.delay_seconds)
        pulse_task_id = self._persist_conversation_pulse(
            merged,
            reply_sent_at,
            mode=first.mode,
            delay_seconds=first.delay_seconds,
            remaining=remaining,
        )
        self._afterthought_tasks[key] = [
            asyncio.create_task(
                self._fire_afterthought_episode(
                    key, selected, merged, reply_target, reply_sent_at, pulse_task_id=pulse_task_id
                )
            )
        ]
        logger.info(
            "scheduled afterthought episode for %s modes=%s",
            key,
            ",".join(plan.mode for plan in selected),
        )

    def _persist_conversation_pulse(
        self,
        merged: IncomingMessage,
        reply_sent_at: datetime,
        *,
        mode: str,
        delay_seconds: float,
        remaining: list[dict[str, object]],
    ) -> int | None:
        if not hasattr(self.engine, "schedule_conversation_pulse"):
            return None
        try:
            canonical_user_id = self.engine.store.resolve_user(
                merged.platform, merged.platform_user_id
            )
            return self.engine.schedule_conversation_pulse(
                canonical_user_id=canonical_user_id,
                platform=merged.platform,
                platform_user_id=merged.platform_user_id,
                reply_sent_at=reply_sent_at,
                mode=mode,
                delay_seconds=delay_seconds,
                remaining=remaining,
            )
        except Exception:
            logger.exception("failed to persist conversation pulse; retaining live timer")
            return None

    async def _fire_afterthought_episode(
        self,
        key: str,
        plans: list[AfterthoughtPlan],
        merged: IncomingMessage,
        reply_target: ReplyTarget,
        reply_sent_at: datetime,
        *,
        pulse_task_id: int | None = None,
    ) -> None:
        """Run a bounded, cancellable continuation episode.

        Each later thought is contingent on the earlier one actually being sent.
        Any new user turn cancels this task, so a continuation cannot race a
        resumed conversation or a platform switch.
        """
        elapsed = 0.0
        sent_texts: list[str] = []
        for index, plan in enumerate(plans):
            remaining_delay = max(0.0, plan.delay_seconds - elapsed)
            if index:
                pulse_task_id = self._persist_conversation_pulse(
                    merged,
                    reply_sent_at,
                    mode=plan.mode,
                    delay_seconds=remaining_delay,
                    remaining=_pulse_remaining(plans[index + 1 :], plan.delay_seconds),
                )
            sent = await self._fire_afterthought(
                key,
                plan,
                merged,
                reply_target,
                reply_sent_at,
                delay_seconds=remaining_delay,
                avoid_texts=sent_texts,
                pulse_task_id=pulse_task_id,
            )
            # If a generation was withheld, there is no conversational thread
            # to extend.  Cancellation is also represented by task completion.
            if not sent or key not in self._afterthought_tasks:
                return
            if hasattr(self.engine, "complete_conversation_pulse"):
                self.engine.complete_conversation_pulse(pulse_task_id)
            sent_texts.append(sent)
            elapsed = plan.delay_seconds

    async def _fire_afterthought(
        self,
        key: str,
        plan: AfterthoughtPlan,
        merged: IncomingMessage,
        reply_target: ReplyTarget,
        reply_sent_at: datetime,
        *,
        delay_seconds: float | None = None,
        avoid_texts: list[str] | None = None,
        pulse_task_id: int | None = None,
    ) -> str | None:
        try:
            await self.sleep(plan.delay_seconds if delay_seconds is None else delay_seconds)
            if hasattr(
                self.engine, "conversation_pulse_is_active"
            ) and not self.engine.conversation_pulse_is_active(pulse_task_id):
                logger.info(
                    "afterthought pulse %s was cancelled by newer user activity", pulse_task_id
                )
                return None
            canonical_user_id = self.engine.store.resolve_user(
                merged.platform, merged.platform_user_id
            )
            text = await self.engine.generate_afterthought(
                canonical_user_id,
                reply_sent_at,
                mode=plan.mode,
            )
            if not text:
                logger.info("afterthought withheld for %s mode=%s", key, plan.mode)
                self._afterthought_tasks.pop(key, None)
                if hasattr(self.engine, "cancel_conversation_pulse"):
                    self.engine.cancel_conversation_pulse(pulse_task_id)
                return None
            if any(_afterthought_texts_overlap(text, earlier) for earlier in avoid_texts or []):
                logger.info("afterthought withheld for %s because it repeats this episode", key)
                return None
            logger.info("sending afterthought for %s mode=%s", key, plan.mode)
            await self.sleep(self.rng.uniform(1.5, 4.0))
            delivery_id = None
            if hasattr(self.engine, "queue_afterthought_delivery"):
                delivery_id = self.engine.queue_afterthought_delivery(
                    canonical_user_id,
                    merged.platform,
                    text,
                )
            await reply_target.reply(content=text, msg_seq=_reply_msg_seq())
            if hasattr(self.engine, "confirm_afterthought_delivery"):
                self.engine.confirm_afterthought_delivery(
                    canonical_user_id,
                    merged.platform,
                    text,
                    delivery_id=delivery_id,
                )
            return text
        except asyncio.CancelledError:
            if "delivery_id" in locals() and hasattr(self.engine, "fail_afterthought_delivery"):
                self.engine.fail_afterthought_delivery(
                    delivery_id,
                    "QQ afterthought cancelled by newer user activity",
                )
            if hasattr(self.engine, "cancel_conversation_pulse"):
                self.engine.cancel_conversation_pulse(pulse_task_id)
            return None
        except Exception:
            logger.exception("afterthought failed")
            if "delivery_id" in locals() and hasattr(
                self.engine,
                "fail_afterthought_delivery",
            ):
                self.engine.fail_afterthought_delivery(
                    delivery_id,
                    "QQ afterthought delivery failed",
                )
            return None

    def _cancel_afterthought(self, key: str) -> None:
        tasks = self._afterthought_tasks.pop(key, [])
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            logger.info("cancelled pending afterthought for %s due to new user activity", key)


def _afterthought_texts_overlap(candidate: str, earlier: str) -> bool:
    """Keep one continuation episode from paraphrasing itself."""

    def normalize(text: str) -> str:
        return re.sub(r"[\s，。！？!?、~～]+", "", text).lower()

    left, right = normalize(candidate), normalize(earlier)
    if not left or not right:
        return False
    return (
        left in right
        or right in left
        or len(set(left) & set(right)) / max(len(set(left)), 1) >= 0.82
    )


def _pulse_remaining(
    plans: list[AfterthoughtPlan], previous_delay: float
) -> list[dict[str, object]]:
    """Serialize later stages as delays relative to the stage before them."""
    remaining: list[dict[str, object]] = []
    elapsed = previous_delay
    for plan in plans:
        remaining.append(
            {"mode": plan.mode, "delay_seconds": max(1.0, plan.delay_seconds - elapsed)}
        )
        elapsed = plan.delay_seconds
    return remaining


def _afterthought_plans(text: str, rng: random.Random) -> list[AfterthoughtPlan]:
    """Create restrained continuation opportunities from the user's actual turn."""
    message_type = classify_message(text)
    compact = re.sub(r"\s+", "", text)
    explicit_farewell = any(
        token in compact for token in ("晚安", "睡了", "先睡", "拜拜", "回头聊", "明天聊")
    )
    if (
        message_type in {"urgent", "farewell", "withdrawal", "thinking", "reaction_pause"}
        or explicit_farewell
    ):
        return []
    if message_type in {"story", "emotional", "nonverbal_share"} or len(text.strip()) >= 35:
        return [
            AfterthoughtPlan("quick_continue", rng.uniform(12, 30), 0.26),
            AfterthoughtPlan("topic_drift", rng.uniform(75, 180), 0.14),
            AfterthoughtPlan("silence_react", rng.uniform(240, 600), 0.08),
        ]
    # A normal short turn has already received its answer. Scheduling an extra
    # message here makes it too easy for her to appear to answer herself.
    return []


class CompanionQQClient(botpy.Client):
    def __init__(self, *, use_fake_model: bool = False, **kwargs):
        is_sandbox = bool(kwargs.get("is_sandbox", False))
        super().__init__(**kwargs)
        self.engine = build_companion_engine(use_fake_model=use_fake_model)
        settings = get_settings()
        api_base_url = (
            "https://sandbox.api.sgroup.qq.com" if is_sandbox else "https://api.sgroup.qq.com"
        )
        self.qq_api = (
            QQOfficialClient(
                settings.qq_bot_app_id, settings.qq_bot_secret, api_base_url=api_base_url
            )
            if settings.qq_bot_app_id and settings.qq_bot_secret
            else None
        )
        self.coalescer = QQMessageCoalescer(
            self.engine,
            delay_seconds=settings.qq_message_batch_seconds,
            turn_policy=TurnTakingPolicy(short_wait_seconds=settings.qq_message_batch_seconds),
            on_reply=self._log_reply,
            on_sticker=self._send_reply_sticker,
            on_image=self._send_reply_image,
            on_reaction=self._reject_unsupported_reply_reaction,
            human_timing=True,
            enable_reply_decision=settings.enable_reply_decision,
        )
        self._seen_message_ids: set[str] = set()
        self._recent_text_keys: dict[str, float] = {}

    def _is_duplicate(self, message_id: str | None, user_id: str, text: str) -> bool:
        import time as _time

        now = _time.time()
        text_key = f"{user_id}:{text[:80]}"
        recent = self._recent_text_keys.get(text_key, 0)
        # QQ can redeliver one user turn with a new event id after a reconnect.
        # Only use this narrow content window when there *is* an id, so a human
        # consciously sending the same short text a few seconds later still works.
        text_window = 1.5 if message_id else 5.0
        if now - recent < text_window:
            logger.info("skipped near-simultaneous duplicate message: user=%s", user_id)
            return True
        if message_id:
            mid = str(message_id)
            if mid in self._seen_message_ids:
                logger.info("skipped duplicate message by id: %s", mid)
                return True
            self._seen_message_ids.add(mid)
        self._recent_text_keys[text_key] = now
        if len(self._recent_text_keys) > 200:
            cutoff = now - 10.0
            self._recent_text_keys = {k: v for k, v in self._recent_text_keys.items() if v > cutoff}
        if len(self._seen_message_ids) > 500:
            self._seen_message_ids = set(sorted(self._seen_message_ids)[-300:])
        return False

    async def on_ready(self) -> None:
        logger.info("QQ WebSocket client is ready: %s", self.robot.name)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        user_id = message.author.user_openid
        text = _clean_content(message.content)
        if self._is_duplicate(message.id, user_id, text):
            return
        incoming = IncomingMessage(
            platform="qq",
            platform_user_id=user_id,
            text=text,
            message_id=message.id,
            attachments=_attachments_from_botpy(message.attachments),
        )
        if not incoming.text and not incoming.attachments:
            return
        await self.coalescer.add(f"c2c:{incoming.platform_user_id}", incoming, message)

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        user_id = message.author.member_openid
        text = _clean_content(message.content)
        if self._is_duplicate(message.id, user_id, text):
            return
        incoming = IncomingMessage(
            platform="qq",
            platform_user_id=user_id,
            channel_id=message.group_openid,
            text=text,
            message_id=message.id,
            attachments=_attachments_from_botpy(message.attachments),
        )
        if not incoming.text and not incoming.attachments:
            return
        await self.coalescer.add(
            f"group:{incoming.channel_id}:{incoming.platform_user_id}",
            incoming,
            message,
        )

    async def _log_reply(self, reply: CompanionReply) -> None:
        logger.info("replied to %s; mood=%s", reply.canonical_user_id, reply.mood)

    async def _send_reply_sticker(
        self, incoming: IncomingMessage, reply: CompanionReply
    ) -> object | None:
        if reply.sticker_path:
            return await self._send_local_image(incoming, Path(reply.sticker_path))
        return None

    async def _send_reply_image(self, incoming: IncomingMessage, reply: CompanionReply) -> object | None:
        if reply.image_path:
            return await self._send_local_image(incoming, Path(reply.image_path))
        return None

    async def _reject_unsupported_reply_reaction(
        self, incoming: IncomingMessage, reply: CompanionReply
    ) -> DispatchAcceptance:
        """Report the platform capability; the turn owns Action settlement."""
        return DispatchAcceptance(
            status="failed", reason="official_qq_reaction_unsupported"
        )

    async def _send_local_image(self, incoming: IncomingMessage, path: Path) -> object | None:
        if not self.qq_api:
            return None
        if incoming.channel_id:
            return await self.qq_api.send_group_local_image(
                incoming.channel_id,
                path,
                msg_id=incoming.message_id,
            )
        else:
            return await self.qq_api.send_c2c_local_image(
                incoming.platform_user_id,
                path,
                msg_id=incoming.message_id,
            )


def _clean_content(content: str | None) -> str:
    return (content or "").strip()


def _recent_context_open(rows) -> bool:
    recent_text = "\n".join(str(row["text"]) for row in rows[-4:] if row["direction"] == "in")
    return any(
        token in recent_text
        for token in (
            "考试",
            "毛概",
            "复习",
            "背",
            "累",
            "闷",
            "难过",
            "老师",
            "下雨",
            "伞",
            "离谱",
            "不知道怎么说",
        )
    )


def _reply_msg_seq() -> int:
    return int(time.time() * 1000) % 1_000_000


def _safe_live_failure_reply(text: str) -> str:
    """Return a bounded, current-turn reply when the world pipeline cannot settle.

    It deliberately makes no world or memory claims.  A small set of pragmatic
    cues keeps the last-resort message from sounding unrelated to what the user
    just said.
    """
    compact = re.sub(r"\s+", "", text or "")
    disappointment_cues = (
        "算了",
        "不说了",
        "没意思",
        "敷衍",
        "不想听",
        "你继续",
        "当我没说",
    )
    if any(cue in compact for cue in disappointment_cues):
        return "我听出来了，你对我刚才的回应有点失望。是我没接好，不该继续追着问。"
    if compact.endswith(("?", "？")) or any(
        compact.startswith(token) for token in ("为什么", "怎么", "什么", "你觉得", "是不是")
    ):
        return "我看到你在认真问我这件事。刚才回复这边出了点问题，我先不拿一句敷衍话糊弄你。"
    return "我看到你刚才说的了。回复这边出了点问题，但不是故意忽略你这句。"


async def _send_reply_parts(
    reply_target: ReplyTarget,
    parts: list[str],
    *,
    part_delays_ms: list[int] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
    human_timing: bool = True,
    should_continue: Callable[[], bool] | None = None,
    before_part: Callable[[int, str], None] | None = None,
    after_part: Callable[[int, str, object], None] | None = None,
) -> bool:
    rng = rng or random.Random()
    planned_delays = part_delays_ms or [0] * len(parts)
    if len(planned_delays) != len(parts):
        planned_delays = [0] * len(parts)
    for index, part in enumerate(parts):
        if should_continue and not should_continue():
            return False
        if index:
            delay = (
                planned_delays[index] / 1000
                if planned_delays[index] > 0
                else (
                    between_part_delay_seconds(part, rng=rng)
                    if human_timing
                    else min(1.8, 0.45 + len(part) / 45)
                )
            )
            await sleep(delay)
            if should_continue and not should_continue():
                return False
        if before_part:
            before_part(index, part)
        response = await reply_target.reply(content=part, msg_seq=_reply_msg_seq())
        if after_part:
            after_part(index, part, response)
    return True


def classify_mid_reply_interruption(text: str) -> str:
    stripped = re.sub(r"\s+", "", text.strip())
    if not stripped:
        return "ignore_empty"
    if any(
        token in stripped
        for token in ("等下", "等等", "打断一下", "不是这个意思", "我不是说", "先别", "你等会")
    ):
        return "takeover"
    if _looks_like_backchannel(stripped):
        return "backchannel"
    msg_type = classify_message(text)
    if msg_type in {"urgent", "question", "emotional", "story", "thinking", "withdrawal"}:
        return "takeover"
    if len(stripped) >= 10:
        return "takeover"
    return "backchannel"


def _looks_like_backchannel(text: str) -> bool:
    if re.fullmatch(
        r"(嗯+|哦+|噢+|喔+|啊+|哈+|哈哈+|对+|是+|确实|真的|草|笑死|好+|行+|可以+)[。！!～~]*", text
    ):
        return True
    return text in {"对吧", "是吧", "懂了", "原来如此", "有道理"}


def _attachments_from_botpy(raw_attachments) -> list[MessageAttachment]:
    attachments: list[MessageAttachment] = []
    for item in raw_attachments or []:
        content_type = getattr(item, "content_type", None)
        filename = getattr(item, "filename", None)
        kind = attachment_kind(content_type, filename)
        attachments.append(
            MessageAttachment(
                kind=kind,
                url=getattr(item, "url", None),
                filename=filename,
                content_type=content_type,
                size=getattr(item, "size", None),
                width=getattr(item, "width", None),
                height=getattr(item, "height", None),
            )
        )
    return attachments


def main() -> None:
    parser = argparse.ArgumentParser(description="Run QQ official WebSocket companion adapter.")
    parser.add_argument("--sandbox", action="store_true", help="Use QQ sandbox environment.")
    parser.add_argument("--fake", action="store_true", help="Use fake model instead of DeepSeek.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    validate_qq_outbound_configuration(
        configured_adapter=settings.qq_adapter,
        launched_adapter="official",
    )
    if not settings.qq_bot_app_id or not settings.qq_bot_secret:
        raise SystemExit("QQ_BOT_APP_ID and QQ_BOT_SECRET are required")

    intents = botpy.Intents(public_messages=True)
    lock_path = qq_outbound_owner_lock_path(settings.database_path)
    try:
        with QQOutboundOwnerLease(lock_path, adapter="official"):
            client = CompanionQQClient(
                intents=intents,
                is_sandbox=args.sandbox,
                use_fake_model=args.fake,
            )
            try:
                client.run(appid=settings.qq_bot_app_id, secret=settings.qq_bot_secret)
            finally:
                asyncio.run(client.engine.aclose())
    except AlreadyRunningError as exc:
        raise SystemExit(f"QQ outbound adapter cannot start: {exc}") from exc


if __name__ == "__main__":
    main()
