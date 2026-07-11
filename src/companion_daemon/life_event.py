import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re

from companion_daemon.budget import ESTIMATES, BudgetGate
from companion_daemon.config import get_settings
from companion_daemon.image_generation import OpenAIImageGenerator, life_image_prompt
from companion_daemon.life_runtime import advance_life_runtime, runtime_prompt_line
from companion_daemon.qq_delivery import QQDelivery
from companion_daemon.relationship import relationship_status_line
from companion_daemon.social_followups import cancel_life_share_followup_for_event
from companion_daemon.runtime import build_companion_engine
from companion_daemon.stickers import load_stickers


logger = logging.getLogger(__name__)
LOCAL_INVITATION_RE = re.compile(r"你要不要(?:也)?(?:来|去|一起|过来)[^。！？!?]*[。！？!?]?")


@dataclass(frozen=True)
class LifeEvent:
    topic: str
    messages: list[str]
    sticker_category: str | None = None
    memory_mode: str = "planned_today"


class LifeEventGenerator:
    """Render an already-recorded world event without inventing a new one.

    The private-life runtime is the authority for what happens in the companion's
    fictional world.  This boundary deliberately does *not* ask a language model
    to fill in an event from a broad activity such as "studying": that was the
    route by which plausible prose became a false ledger entry.
    """

    def __init__(self, model):
        # Kept for constructor compatibility with callers/tests while the model
        # is no longer allowed to author factual event content.
        self.model = model

    async def generate(
        self,
        *,
        mood: str,
        relationship_stage: str,
        relationship_status: str,
        life_context: str | None = None,
        lived_event: str | None = None,
    ) -> LifeEvent | None:
        if not lived_event:
            return None
        fact = lived_event.strip().rstrip("。！？!? ")
        if not fact:
            return None
        # The event wording comes directly from the deterministic runtime
        # ledger.  The short trailing clause is an expression of intent, not a
        # new claim about place, people, time, or outcome.
        return LifeEvent(
            topic=fact[:24],
            messages=[f"{fact}。刚想起这件小事，想跟你说一下。"],
            memory_mode="ledger_event",
        )


def parse_life_event(raw: str) -> LifeEvent:
    import json

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return LifeEvent(topic="随手分享", messages=[raw.strip()[:300]], sticker_category="comfort")
    messages = [_clean_life_event_message(str(item)) for item in data.get("messages", []) if str(item).strip()]
    messages = [message for message in messages if message]
    if not messages:
        messages = ["我刚刚遇到一件小事，突然有点想跟你说。"]
    memory_mode = str(data.get("memory_mode") or "planned_today").strip()
    if memory_mode not in {"planned_today", "spontaneous_recall"}:
        memory_mode = "planned_today"
    return LifeEvent(
        topic=str(data.get("topic") or "随手分享"),
        messages=messages[:4],
        sticker_category=data.get("sticker_category"),
        memory_mode=memory_mode,
    )


def _clean_life_event_message(message: str) -> str:
    cleaned = message.strip()
    cleaned = LOCAL_INVITATION_RE.sub("下次拍给你看。", cleaned)
    return cleaned[:300].strip()


def _life_event_memory_content(event: LifeEvent) -> str:
    return f"{event.topic}: {' / '.join(event.messages)}"


def _private_life_event_source(event: LifeEvent) -> str:
    return f"life_event:{event.memory_mode}"


def _private_life_event_confidence(event: LifeEvent) -> float:
    return 0.78 if event.memory_mode == "planned_today" else 0.68


async def run(
    *,
    user_id: str,
    send: bool,
    sandbox: bool,
    generate_image: bool,
    image_kind: str,
) -> bool:
    settings = get_settings()
    engine = build_companion_engine()
    if getattr(engine, "world_kernel", None) and getattr(engine, "world_id", None):
        return await _run_world_life_event(engine, user_id=user_id, send=send, sandbox=sandbox)
    state = engine.store.get_mood_state(user_id)
    outreach_block = (
        engine.outreach_block_reason(user_id, state)
        if hasattr(engine, "outreach_block_reason")
        else None
    )
    if outreach_block:
        print(f"life event not shared: {outreach_block}")
        return False
    runtime = advance_life_runtime(engine.store, user_id, state)
    unshared_events = engine.store.unshared_private_life_events(user_id, limit=1)
    selected_event = unshared_events[0] if unshared_events else None
    if selected_event is None:
        print("life event not shared: no completed private event in the life ledger")
        return False
    budget_gate = BudgetGate(
        engine.store,
        monthly_budget_cny=settings.monthly_budget_cny,
        daily_budget_cny=settings.daily_budget_cny,
        soft_daily_budget_cny=settings.soft_daily_budget_cny,
        monthly_image_limit=settings.monthly_image_limit,
        monthly_vision_limit=settings.monthly_vision_limit,
        monthly_audio_limit=settings.monthly_audio_limit,
    )
    event = await LifeEventGenerator(model=None).generate(
        mood=state.mood,
        relationship_stage=state.relationship_stage,
        relationship_status=relationship_status_line(state),
        life_context=runtime_prompt_line(runtime),
        lived_event=str(selected_event["content"]),
    )
    if event is None:
        print("life event not shared: selected ledger event was empty")
        return False
    print(f"topic: {event.topic}")
    for index, message in enumerate(event.messages, start=1):
        print(f"{index}. {message}")
    print(f"sticker_category: {event.sticker_category or ''}")
    generated_path = None
    if generate_image:
        if not settings.openai_api_key:
            print("image not generated: OPENAI_API_KEY is missing")
        else:
            estimate = ESTIMATES["image_generation"]
            decision = budget_gate.check(estimate, automatic=True)
            if not decision.allowed:
                print(f"image not generated: {decision.reason}")
            else:
                output = Path("assets/life") / f"life-{event.topic[:24].replace('/', '-')}.png"
                generated = await OpenAIImageGenerator(
                    settings.openai_api_key,
                    base_url=settings.openai_base_url,
                    model=settings.image_model,
                ).generate(
                    life_image_prompt(
                        event.topic,
                        kind=image_kind,
                        visual_identity_path=settings.visual_identity_path,
                    ),
                    output_path=output,
                )
                budget_gate.record(estimate, note=f"life_event:{image_kind}:{event.topic}")
                generated_path = generated.path
                print(f"generated image: {generated_path}")
    if not send:
        return False

    delivery = QQDelivery(settings, sandbox=sandbox)
    recipient_id = delivery.proactive_recipient_id() or engine.store.platform_user_id(user_id, "qq")
    if not recipient_id:
        print("not sent: no outbound QQ recipient configured")
        return False
    shared_event_id = int(selected_event["id"])
    delivered_messages: list[str] = []
    for message in event.messages:
        delivery_id, trace_id = engine.store.queue_outgoing_with_turn_trace(
            user_id,
            "qq",
            message,
            kind="life_event",
            appraisal="life_event_share",
            expression_policy="只分享已登记的生活事件，不补写新的经历。",
            allowed_facts=[event.topic],
            short_lived_constraint=None,
            observable_reason="生活事件已在运行时账本中发生，现决定分享。",
            direction="life_event",
        )
        try:
            await delivery.send_text(recipient_id, message)
        except Exception as exc:
            logger.exception("life event text send failed")
            print(f"life event not fully sent: {exc}")
            engine.store.resolve_outgoing_and_turn_trace(
                delivery_id, trace_id, delivered=False, failure_reason=str(exc)
            )
            engine.store.upsert_memory(
                user_id,
                kind="life_event_send_failed",
                content=f"{event.topic}: {message[:120]}",
                source="life_event",
                confidence=0.2,
            )
            if delivered_messages:
                # She already said part of it out loud, so the event counts as
                # shared: without this she would re-share the same story later.
                _record_shared_life_event(
                    engine,
                    user_id,
                    event,
                    shared_event_id,
                    delivered_messages,
                )
            return False
        engine.store.resolve_outgoing_and_turn_trace(delivery_id, trace_id, delivered=True)
        delivered_messages.append(message)

    if generated_path:
        try:
            await delivery.send_image(recipient_id, generated_path)
            print(f"sent generated image: {generated_path}")
        except Exception as exc:
            logger.exception("life event image send failed")
            print(f"generated image not sent: {exc}")

    if event.sticker_category and not generated_path:
        catalog = load_stickers(str(settings.stickers_path))
        sticker = next((item for item in catalog.stickers if item.category == event.sticker_category), None)
        if sticker:
            try:
                await delivery.send_image(recipient_id, Path(sticker.path))
                print(f"sent sticker: {sticker.path}")
            except Exception as exc:
                logger.exception("life event sticker send failed")
                print(f"sticker not sent: {exc}")
    _record_shared_life_event(engine, user_id, event, shared_event_id, event.messages)
    return True


async def _run_world_life_event(engine, *, user_id: str, send: bool, sandbox: bool) -> bool:
    """Share one committed-but-private world experience without legacy life tables."""
    snapshot = engine.world_kernel.snapshot(engine.world_id)
    if not send:
        return False
    settings = get_settings()
    delivery = QQDelivery(settings, sandbox=sandbox)
    recipient_id = delivery.proactive_recipient_id() or engine.store.platform_user_id(user_id, "qq")
    if not recipient_id:
        print("not sent: no outbound QQ recipient configured")
        return False
    scheduled = engine.world_kernel.schedule_life_share_delivery(
        world_id=engine.world_id, canonical_user_id=user_id, platform="qq",
        expires_at=(engine._world_logical_now() if hasattr(engine, "_world_logical_now") else datetime.fromisoformat(str(snapshot["clock"]["logical_at"]))) + timedelta(hours=4),
    )
    if not scheduled:
        print("life event not shared: world policy deferred it")
        return False
    if not engine.world_kernel.begin_outgoing_action(scheduled.delivery_id):
        return False
    try:
        await delivery.send_text(recipient_id, scheduled.text)
    except Exception as exc:
        engine.world_kernel.settle_outgoing_action(scheduled.delivery_id, delivered=False, reason=str(exc))
        return False
    engine.world_kernel.settle_outgoing_action(scheduled.delivery_id, delivered=True)
    return True


def _record_shared_life_event(
    engine,
    user_id: str,
    event: LifeEvent,
    shared_event_id: int | None,
    delivered_messages: list[str],
) -> None:
    """Close the sharing loop over whatever actually reached the user."""
    if hasattr(engine, "confirm_life_event_delivery"):
        engine.confirm_life_event_delivery(user_id)
    else:
        engine.store.record_proactive_delivery(user_id, "qq:life_event")
    if shared_event_id is not None:
        engine.store.mark_life_event_shared(shared_event_id)
        cancel_life_share_followup_for_event(engine.store, user_id, shared_event_id)
    engine.store.upsert_memory(
        user_id,
        kind="life_event",
        content=f"{event.topic}: {' / '.join(delivered_messages)}",
        source=f"life_event:shared:{shared_event_id}" if shared_event_id else "life_event",
        confidence=0.82,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and optionally send a life-event share.")
    parser.add_argument("--user", default="geoff")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--generate-image", action="store_true")
    parser.add_argument("--image-kind", default="life", choices=["life", "selfie", "food"])
    args = parser.parse_args()
    asyncio.run(
        run(
            user_id=args.user,
            send=args.send,
            sandbox=args.sandbox,
            generate_image=args.generate_image,
            image_kind=args.image_kind,
        )
    )
