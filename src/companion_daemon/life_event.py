import argparse
import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re

from companion_daemon.budget import ESTIMATES, BudgetGate
from companion_daemon.config import get_settings
from companion_daemon.image_generation import OpenAIImageGenerator, life_image_prompt
from companion_daemon.life_runtime import advance_life_runtime, runtime_prompt_line
from companion_daemon.llm import DeepSeekChatModel, FakeCompanionModel
from companion_daemon.qq_delivery import QQDelivery
from companion_daemon.relationship import relationship_instruction, relationship_status_line
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
    def __init__(self, model):
        self.model = model

    async def generate(
        self,
        *,
        mood: str,
        relationship_stage: str,
        relationship_status: str,
        life_context: str | None = None,
        lived_event: str | None = None,
    ) -> LifeEvent:
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是沈知栀，一个真实感很强的中国女大学生。"
                    "生成一件今天刚发生的小事，用 QQ 私聊连续发 2-4 条消息分享。"
                    "不要写舞台动作，不要说这是编的。"
                    "亲疏程度必须符合当前关系，不要跳过关系阶段。"
                    "她在上海上学，用户在成都；可以分享自己的本地生活，但不要邀请用户立刻去她身边的店、学校或活动。"
                    "具体经历要像事后可写进生活连续性账本的小事，不要编造和用户共同经历过的线下事件。"
                    "生活记忆模式 memory_mode 可选 planned_today 或 spontaneous_recall："
                    "planned_today 表示今天按生活节奏实际发生的小事；"
                    "spontaneous_recall 表示刚突然想起的微小生活碎片，比如午饭吃到怪东西、路上听见一句话。"
                    "输出严格 JSON: topic, messages, sticker_category, memory_mode。"
                    "sticker_category 可选 happy, sulk, miss_you, jealous_soft, angry_soft, sleepy, comfort, teasing。"
                    "若给出已发生的私有事件，只能围绕它自然地分享或联想，不能添加与该事件矛盾的新事实。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"当前心情: {mood}\n"
                    f"{relationship_status}\n"
                    f"关系阶段说明: {relationship_instruction(relationship_stage)}\n"
                    f"已经发生的当前生活上下文: {life_context or '无'}\n"
                    f"优先分享的已发生私有事件: {lived_event or '无；可生成一件当前活动中刚发生的微小事件'}\n"
                    "只能从这段上下文自然延伸，不能忽略当前时间凭空写另一段日程。"
                ),
            },
        ]
        raw = await self.model.complete(prompt, temperature=0.9)
        return parse_life_event(raw)


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
    model = (
        DeepSeekChatModel(
            settings.deepseek_api_key,
            settings.deepseek_base_url,
            settings.deepseek_model,
            thinking_enabled=settings.deepseek_thinking_enabled,
            reasoning_effort=settings.deepseek_reasoning_effort,
        )
        if settings.deepseek_api_key
        else FakeCompanionModel()
    )
    budget_gate = BudgetGate(
        engine.store,
        monthly_budget_cny=settings.monthly_budget_cny,
        daily_budget_cny=settings.daily_budget_cny,
        soft_daily_budget_cny=settings.soft_daily_budget_cny,
        monthly_image_limit=settings.monthly_image_limit,
        monthly_vision_limit=settings.monthly_vision_limit,
        monthly_audio_limit=settings.monthly_audio_limit,
    )
    event_estimate = ESTIMATES["life_event"]
    event_budget = budget_gate.check(event_estimate, automatic=True)
    if not event_budget.allowed:
        print(f"life event not generated: {event_budget.reason}")
        return False
    event = await LifeEventGenerator(model).generate(
        mood=state.mood,
        relationship_stage=state.relationship_stage,
        relationship_status=relationship_status_line(state),
        life_context=runtime_prompt_line(runtime),
        lived_event=str(selected_event["content"]) if selected_event else None,
    )
    budget_gate.record(event_estimate, note=f"life_event:{event.topic[:40]}")
    generated_event_id: int | None = None
    if send and selected_event is None:
        occurred_at = runtime.updated_at
        engine.store.upsert_memory(
            user_id,
            kind="private_life_event",
            content=_life_event_memory_content(event),
            source=_private_life_event_source(event),
            confidence=_private_life_event_confidence(event),
        )
        # This is an internal occurrence ledger, not a message. It is written before
        # delivery so a failed share remains a private experience rather than a lie.
        generated_event_id = engine.store.record_life_event(
            user_id,
            kind="private_life_event",
            content=_life_event_memory_content(event),
            started_at=occurred_at,
            ends_at=occurred_at,
            status="completed",
            source=_private_life_event_source(event),
        )
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
    shared_event_id = int(selected_event["id"]) if selected_event is not None else generated_event_id
    delivered_messages: list[str] = []
    for message in event.messages:
        delivery_id = engine.store.queue_outgoing(user_id, "qq", message, kind="life_event")
        try:
            await delivery.send_text(recipient_id, message)
        except Exception as exc:
            logger.exception("life event text send failed")
            print(f"life event not fully sent: {exc}")
            engine.store.mark_outgoing_failed(delivery_id, str(exc))
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
        engine.store.mark_outgoing_delivered(delivery_id)
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
