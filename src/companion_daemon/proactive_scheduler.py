import argparse
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import logging
import random

from companion_daemon.config import get_settings
from companion_daemon.life_event import run as run_life_event
from companion_daemon.models import IncomingMessage
from companion_daemon.im_timing import between_part_delay_seconds
from companion_daemon.proactive_cli import run as run_once
from companion_daemon.qq_delivery import QQDelivery
from companion_daemon.relationship import life_event_probability, proactive_cooldown_minutes
from companion_daemon.runtime import build_companion_engine
from companion_daemon.life_runtime import maybe_apply_planned_life_result, synchronize_life_runtime
from companion_daemon.world import ConcurrencyConflict
from companion_daemon.world_clock import WorldClockDriver

# The model may voice how long she wants to hold back, but the daemon caps how
# much of that wish it honors so a bad decision cannot silence her for a day.
MAX_MODEL_COOLDOWN_MINUTES = 240


logger = logging.getLogger(__name__)

DEFERRED_RECOVERY_CONTEXT_HINT = (
    "回复时机提示: 这条消息在她忙完后才重新看到。"
    "自然接住即可，不要解释系统或承诺过的等待。"
)


def _minutes_since(iso_timestamp: str | None) -> float | None:
    if not iso_timestamp:
        return None
    then = datetime.fromisoformat(iso_timestamp)
    now = datetime.now(then.tzinfo)
    return (now - then).total_seconds() / 60


def _jittered_cooldown_minutes(
    *,
    user_id: str,
    base_minutes: int,
    state_key: str,
    last_sent: str | None,
) -> int:
    if not last_sent:
        return base_minutes
    ratio = _stable_ratio(user_id, state_key, last_sent)
    multiplier = 0.86 + (ratio * 0.42)
    if any(token in state_key for token in ("hurt", "guarded", "sulking")):
        multiplier = max(1.0, multiplier)
    return max(12, min(420, round(base_minutes * multiplier)))


def _next_sleep_seconds(base_seconds: float, rng: random.Random | None = None) -> float:
    rng = rng or random
    return max(30.0, base_seconds * rng.uniform(0.65, 1.35))


def _has_due_social_task(store, user_id: str) -> bool:
    if not hasattr(store, "next_due_social_task"):
        return False
    task = store.next_due_social_task(
        user_id,
        kinds=(
            "comfort_followup",
            "promise_followup",
            "reply_reconsider",
            "life_share_followup",
            "contradiction_followup",
        ),
        now=datetime.now().astimezone(),
    )
    return task is not None


def _model_cooldown_block(store, user_id: str) -> tuple[float, int] | None:
    """Honor the cooldown the last proactive decision asked for, within a cap.

    Applies to withheld decisions too: "我现在不想发，过两小时再看" should stop
    the scheduler from re-asking the model every pass.
    """
    if not hasattr(store, "last_proactive_event"):
        return None
    last_event = store.last_proactive_event(user_id)
    if last_event is None:
        return None
    requested = min(int(last_event["cooldown_minutes"] or 0), MAX_MODEL_COOLDOWN_MINUTES)
    if requested <= 0:
        return None
    elapsed = _minutes_since(str(last_event["created_at"]))
    if elapsed is not None and elapsed < requested:
        return elapsed, requested
    return None


def _stable_ratio(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(0xFFFFFFFFFFFF)


async def recover_overdue_deferred_replies(
    engine,
    *,
    send: bool,
    sandbox: bool,
    now: datetime | None = None,
) -> int:
    """Recover delayed replies after an adapter restart.

    The two-minute grace period leaves the live coalescer's precise timer as the
    normal path. Recovery only handles work that demonstrably outlived that timer.
    A failed recovery is closed after its failed outbox record is retained: replaying
    the same incoming turn would otherwise create duplicate conversation history.
    """
    if not send or not hasattr(engine.store, "claim_due_social_tasks"):
        return 0
    now = now or datetime.now().astimezone()
    overdue = now - timedelta(minutes=2)
    rows = engine.store.claim_due_social_tasks(kind="reply_later", now=overdue)
    if not rows:
        return 0
    delivery = QQDelivery(get_settings(), sandbox=sandbox)
    recovered = 0
    for row in rows:
        task_id = int(row["id"])
        reply = None
        try:
            message = IncomingMessage.model_validate(json.loads(row["payload_json"]))
            reply = await engine.handle_message(
                message,
                context_hint=DEFERRED_RECOVERY_CONTEXT_HINT,
                defer_delivery=True,
            )
            if reply is None:
                engine.complete_deferred_reply_task(task_id)
                continue
            parts = reply.text_parts or [reply.text]
            for index, part in enumerate(parts):
                if index:
                    # A recovered deferred reply has no live coalescer, but it
                    # still needs the same interruption-sized gaps as normal QQ
                    # delivery.  Sending it as a burst made restart recovery
                    # visibly unlike every other path.
                    await asyncio.sleep(between_part_delay_seconds(part))
                await delivery.send_text(str(row["platform_user_id"]), part)
            engine.confirm_reply_delivery(reply)
            engine.complete_deferred_reply_task(task_id)
            recovered += 1
            logger.info("recovered overdue deferred reply task %s", task_id)
        except Exception:
            logger.exception("failed to recover deferred reply task %s", task_id)
            if reply is not None:
                engine.fail_reply_delivery(
                    reply,
                    "deferred reply recovery delivery failed",
                    source_task_id=task_id,
                )
            else:
                # No outbox was created, so there is nothing to reconsider. Close
                # the stale task to avoid replaying the same incoming turn forever.
                engine.complete_deferred_reply_task(task_id)
    return recovered


async def recover_world_due_replies(
    engine,
    *,
    send: bool,
    sandbox: bool,
    now: datetime | None = None,
) -> int:
    """Recover `reply_later` actions directly from the world event ledger."""
    if not send or not getattr(engine, "world_kernel", None) or not getattr(engine, "world_id", None):
        return 0
    logical_now = now or datetime.fromisoformat(
        str(engine.world_kernel.snapshot(engine.world_id)["clock"]["logical_at"])
    )
    actions = [
        action
        for action in engine.world_kernel.due_actions(engine.world_id, now=logical_now)
        if action["kind"] == "reply_later"
    ]
    if not actions:
        return 0
    delivery = QQDelivery(get_settings(), sandbox=sandbox)
    recovered = 0
    for action in actions:
        action_id = str(action["action_id"])
        payload = action.get("payload") or {}
        raw_message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(raw_message, dict):
            engine.cancel_deferred_reply_task(action_id)
            continue
        reply = None
        try:
            message = IncomingMessage.model_validate(raw_message)
            reply = await engine.handle_message(
                message,
                context_hint=DEFERRED_RECOVERY_CONTEXT_HINT,
                defer_delivery=True,
                resume_action_id=action_id,
            )
            if reply is None:
                engine.complete_deferred_reply_task(action_id)
                continue
            recipient_id = str(message.platform_user_id)
            for index, part in enumerate(reply.text_parts or [reply.text]):
                if index:
                    await asyncio.sleep(between_part_delay_seconds(part))
                await delivery.send_text(recipient_id, part)
            engine.confirm_reply_delivery(reply)
            engine.complete_deferred_reply_task(action_id)
            recovered += 1
        except Exception:
            logger.exception("failed to recover world delayed reply %s", action_id)
            if reply is not None:
                engine.fail_reply_delivery(reply, "world deferred reply recovery delivery failed")
            engine.cancel_deferred_reply_task(action_id)
    return recovered


def recover_interrupted_world_life_shares(engine) -> int:
    """Resolve ambiguous proactive sends conservatively after a process restart."""
    if not getattr(engine, "world_kernel", None) or not getattr(engine, "world_id", None):
        return 0
    return engine.world_kernel.recover_interrupted_life_share_deliveries(engine.world_id)


async def recover_overdue_conversation_pulses(
    engine,
    *,
    send: bool,
    sandbox: bool,
    now: datetime | None = None,
) -> int:
    """Deliver continuation stages that outlived an adapter process.

    The live QQ coalescer owns normal sub-minute timing.  This only claims a
    stage after a grace period, and every incoming turn cancels its task through
    ``CompanionEngine.handle_message`` before it can be claimed.
    """
    if not send or not hasattr(engine.store, "claim_due_social_tasks"):
        return 0
    now = now or datetime.now().astimezone()
    rows = engine.store.claim_due_social_tasks(
        kind="conversation_pulse", now=now - timedelta(minutes=2)
    )
    if not rows:
        return 0
    delivery = QQDelivery(get_settings(), sandbox=sandbox)
    recovered = 0
    for row in rows:
        task_id = int(row["id"])
        payload = json.loads(row["payload_json"])
        delivery_id = None
        try:
            reply_sent_at = datetime.fromisoformat(str(payload["reply_sent_at"]))
            mode = str(payload.get("mode") or "quick_continue")
            text = await engine.generate_afterthought(
                str(row["canonical_user_id"]), reply_sent_at, mode=mode
            )
            if not text:
                engine.cancel_conversation_pulse(task_id)
                continue
            delivery_id = engine.queue_afterthought_delivery(
                str(row["canonical_user_id"]), str(row["platform"]), text
            )
            await delivery.send_text(str(row["platform_user_id"]), text)
            engine.confirm_afterthought_delivery(
                str(row["canonical_user_id"]),
                str(row["platform"]),
                text,
                delivery_id=delivery_id,
            )
            engine.complete_conversation_pulse(task_id)
            remaining = payload.get("remaining") or []
            if remaining and hasattr(engine, "schedule_conversation_pulse"):
                next_stage = remaining[0]
                engine.schedule_conversation_pulse(
                    canonical_user_id=str(row["canonical_user_id"]),
                    platform=str(row["platform"]),
                    platform_user_id=str(row["platform_user_id"]),
                    reply_sent_at=reply_sent_at,
                    mode=str(next_stage.get("mode") or "topic_drift"),
                    delay_seconds=float(next_stage.get("delay_seconds") or 60),
                    remaining=list(remaining[1:]),
                )
            recovered += 1
            logger.info("recovered conversation pulse %s", task_id)
        except Exception:
            logger.exception("failed to recover conversation pulse %s", task_id)
            if delivery_id is not None:
                engine.fail_afterthought_delivery(delivery_id, "conversation pulse recovery delivery failed")
            engine.cancel_conversation_pulse(task_id)
    return recovered


async def recover_world_due_conversation_pulses(
    engine,
    *,
    send: bool,
    sandbox: bool,
    now: datetime | None = None,
) -> int:
    """Settle due continuation bubbles from the world ledger after a restart."""
    if not send or not getattr(engine, "world_kernel", None) or not getattr(engine, "world_id", None):
        return 0
    snapshot = engine.world_kernel.snapshot(engine.world_id)
    logical_now = now or datetime.fromisoformat(str(snapshot["clock"]["logical_at"]))
    due = [item for item in engine.world_kernel.due_actions(engine.world_id, now=logical_now) if item["kind"] == "conversation_pulse"]
    if not due:
        return 0
    delivery = QQDelivery(get_settings(), sandbox=sandbox)
    recovered = 0
    for item in due:
        action_id = str(item["action_id"])
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            engine.cancel_conversation_pulse(action_id)
            continue
        delivery_id = None
        try:
            canonical_user_id = str(payload.get("canonical_user_id") or "")
            platform = str(payload["platform"])
            platform_user_id = str(payload["platform_user_id"])
            reply_sent_at = datetime.fromisoformat(str(payload["reply_sent_at"]))
            mode = str(payload.get("mode") or "quick_continue")
            if not canonical_user_id:
                # Compatibility for actions created before canonical_user_id
                # became explicit world data.
                canonical_user_id = action_id.removeprefix("conversation_pulse:").split(":", 1)[0]
            text = await engine.generate_afterthought(canonical_user_id, reply_sent_at, mode=mode)
            if not text:
                engine.cancel_conversation_pulse(action_id)
                continue
            delivery_id = engine.queue_afterthought_delivery(canonical_user_id, platform, text)
            await delivery.send_text(platform_user_id, text)
            engine.confirm_afterthought_delivery(canonical_user_id, platform, text, delivery_id=delivery_id)
            engine.complete_conversation_pulse(action_id)
            remaining = payload.get("remaining") or []
            if remaining:
                next_stage = remaining[0]
                if isinstance(next_stage, dict):
                    engine.schedule_conversation_pulse(
                        canonical_user_id=canonical_user_id,
                        platform=platform,
                        platform_user_id=platform_user_id,
                        reply_sent_at=reply_sent_at,
                        mode=str(next_stage.get("mode") or "topic_drift"),
                        delay_seconds=float(next_stage.get("delay_seconds") or 60),
                        remaining=list(remaining[1:]),
                    )
            recovered += 1
        except Exception:
            logger.exception("failed to recover world conversation pulse %s", action_id)
            if delivery_id is not None:
                engine.fail_afterthought_delivery(delivery_id, "world conversation pulse recovery delivery failed")
            engine.cancel_conversation_pulse(action_id)
    return recovered


async def scheduler_loop(
    *,
    send: bool,
    sandbox: bool,
    once: bool,
    life_events: bool,
    generate_life_images: bool,
    life_image_kind: str,
) -> None:
    settings = get_settings()
    while True:
        engine = build_companion_engine()
        if getattr(engine, "world_kernel", None):
            try:
                WorldClockDriver(engine.world_kernel).tick(
                    engine.world_id,
                    observed_now=datetime.now().astimezone(),
                    expected_revision=engine.world_kernel.revision(engine.world_id),
                )
            except ConcurrencyConflict:
                # Another adapter advanced the world between the read and the
                # tick.  The next scheduler pass rehydrates from that revision.
                logger.info("world clock tick lost an optimistic race; retrying next pass")
            recover_interrupted_world_life_shares(engine)
            await recover_world_due_replies(engine, send=send, sandbox=sandbox)
            await recover_world_due_conversation_pulses(engine, send=send, sandbox=sandbox)
        else:
            await recover_overdue_deferred_replies(engine, send=send, sandbox=sandbox)
            await recover_overdue_conversation_pulses(engine, send=send, sandbox=sandbox)
        users = engine.store.canonical_users() or ["geoff"]
        for user_id in users:
            if getattr(engine, "world_kernel", None):
                # World mode owns time and outstanding actions in its ledger;
                # running legacy waiting/life projections here would recreate a
                # second behavioural history.
                await run_once(user_id, send=send, sandbox=sandbox)
                continue
            # Time keeps flowing through her waiting psychology even when the
            # cooldown below skips the actual proactive decision.
            if hasattr(engine, "refresh_waiting_state"):
                state = engine.refresh_waiting_state(user_id)
            else:
                state = engine.store.get_mood_state(user_id)
            if hasattr(engine.store, "get_life_runtime"):
                synchronize_life_runtime(engine.store, user_id, state)
                applied = maybe_apply_planned_life_result(engine.store, user_id, state)
                if applied and applied.user_event_effect:
                    print(f"life result for {user_id}: {applied.user_event_effect}", flush=True)
            base_cooldown_minutes = proactive_cooldown_minutes(
                state,
                settings.proactive_min_cooldown_minutes,
            )
            last_sent = engine.store.last_proactive_delivery(user_id, "qq")
            cooldown_minutes = _jittered_cooldown_minutes(
                user_id=user_id,
                base_minutes=base_cooldown_minutes,
                state_key=f"{state.relationship_stage}:{state.mood}",
                last_sent=last_sent,
            )
            elapsed = _minutes_since(last_sent)
            model_block = _model_cooldown_block(engine.store, user_id)
            if model_block is not None and _has_due_social_task(engine.store, user_id):
                # A due social commitment (comfort/promise/reconsider) outranks the
                # model's own wish to stay quiet; the tick will pick it up as trigger.
                model_block = None
            if elapsed is not None and elapsed < cooldown_minutes:
                print(
                    f"skip {user_id}: proactive cooldown {elapsed:.1f}m/{cooldown_minutes}m "
                    f"(base {base_cooldown_minutes}m)",
                    flush=True,
                )
            elif model_block is not None:
                print(
                    f"skip {user_id}: decision cooldown {model_block[0]:.1f}m/{model_block[1]}m",
                    flush=True,
                )
            else:
                await run_once(user_id, send=send, sandbox=sandbox)

            if not life_events:
                continue
            life_last_sent = engine.store.last_proactive_delivery(user_id, "qq:life_event")
            life_elapsed = _minutes_since(life_last_sent)
            life_cooldown = max(cooldown_minutes * 2, 120)
            if life_elapsed is not None and life_elapsed < life_cooldown:
                print(f"skip {user_id}: life-event cooldown {life_elapsed:.1f}m/{life_cooldown}m", flush=True)
                continue
            probability = life_event_probability(state)
            if random.random() > probability:
                print(f"skip {user_id}: life-event probability {probability:.2f}", flush=True)
                continue
            try:
                await run_life_event(
                    user_id=user_id,
                    send=send,
                    sandbox=sandbox,
                    generate_image=generate_life_images,
                    image_kind=life_image_kind,
                )
            except Exception as exc:
                logger.exception("life-event scheduler step failed")
                print(f"life-event failed for {user_id}: {exc}", flush=True)
        if once:
            return
        await asyncio.sleep(_next_sleep_seconds(settings.proactive_interval_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run proactive companion scheduler.")
    parser.add_argument("--send", action="store_true", help="Actually send allowed proactive messages.")
    parser.add_argument("--sandbox", action="store_true", help="Use QQ sandbox API.")
    parser.add_argument("--once", action="store_true", help="Run one scheduler pass.")
    parser.add_argument("--life-events", action="store_true", help="Occasionally share life events.")
    parser.add_argument("--generate-life-images", action="store_true", help="Attach generated images.")
    parser.add_argument("--life-image-kind", default="life", choices=["life", "selfie", "food"])
    args = parser.parse_args()
    asyncio.run(
        scheduler_loop(
            send=args.send,
            sandbox=args.sandbox,
            once=args.once,
            life_events=args.life_events,
            generate_life_images=args.generate_life_images,
            life_image_kind=args.life_image_kind,
        )
    )
