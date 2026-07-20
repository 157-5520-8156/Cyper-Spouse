import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re

from companion_daemon.budget import BudgetGate, image_render_estimate
from companion_daemon.companion_turn import CompanionTurn, DispatchAcceptance, TurnBeat
from companion_daemon.config import get_settings
from companion_daemon.image_generation import OpenAIImageGenerator, life_image_prompt
from companion_daemon.life_runtime import advance_life_runtime, runtime_prompt_line
from companion_daemon.qq_delivery import QQDelivery
from companion_daemon.relationship import relationship_status_line
from companion_daemon.runtime import build_companion_engine
from companion_daemon.time import utc_now


LOCAL_INVITATION_RE = re.compile(r"你要不要(?:也)?(?:来|去|一起|过来)[^。！？!?]*[。！？!?]?")

# Keep receipt parsing fixed to the production adapter even when the command's
# delivery class is replaced by a test or deployment-specific transport.
_QQ_RECEIPT_CANDIDATE = QQDelivery.receipt_candidate


class _QQLifeShareTurnTransport:
    """Turn transport for a World-authorized QQ life-share Action.

    A successful request is not proof of a user-visible message.  Only a
    platform identifier makes the segment delivered; a response without one
    remains unknown.  Some OneBot-style adapters do return an explicit failure
    payload, which can safely become a failed Action.
    """

    def __init__(self, delivery: QQDelivery, recipient_id: str) -> None:
        self.delivery = delivery
        self.recipient_id = recipient_id

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        response = await self.delivery.send_text(self.recipient_id, beat.text)
        receipt = _QQ_RECEIPT_CANDIDATE(response)
        if receipt:
            return DispatchAcceptance(status="delivered", external_receipt=receipt)
        if _explicit_qq_failure(response):
            return DispatchAcceptance(
                status="failed",
                reason="qq_adapter_explicit_failure",
            )
        return DispatchAcceptance(
            status="unknown",
            reason="qq_life_share_returned_without_durable_receipt",
        )


def _explicit_qq_failure(response: object) -> bool:
    """Recognize a declared adapter rejection, never infer it from absence."""
    if not isinstance(response, Mapping):
        return False
    status = str(response.get("status") or "").strip().lower()
    return status in {"failed", "failure", "error", "rejected"}


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
    engine = build_companion_engine()
    try:
        return await _run_with_engine(
            engine,
            user_id=user_id,
            send=send,
            sandbox=sandbox,
            generate_image=generate_image,
            image_kind=image_kind,
        )
    finally:
        close = getattr(engine, "aclose", None)
        if callable(close):
            await close()


async def _run_with_engine(
    engine,
    *,
    user_id: str,
    send: bool,
    sandbox: bool,
    generate_image: bool,
    image_kind: str,
) -> bool:
    settings = get_settings()
    if getattr(engine, "world_kernel", None) and getattr(engine, "world_id", None):
        return await _run_world_life_event(engine, user_id=user_id, send=send, sandbox=sandbox)
    if send:
        # The pre-World outbox represents only ``planned``, ``delivered`` and
        # ``failed``.  It has no ExternalObservation/unknown state, so it
        # cannot honestly model a QQ request which returned after the write
        # crossed the network but without a durable platform receipt.  Do not
        # keep a second sender that turns coroutine completion into a fact.
        # Migrate the deployment to a World-backed engine before enabling
        # automatic life-event delivery.
        print("not sent: legacy life-event delivery is retired; migrate to a World-backed engine")
        return False
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
            estimate = image_render_estimate(reference_count=0, attempts=1)
            decision = budget_gate.check(estimate, automatic=True)
            if not decision.allowed:
                print(f"image not generated: {decision.reason}")
            else:
                output = Path("assets/life") / f"life-{event.topic[:24].replace('/', '-')}.png"
                generated = await OpenAIImageGenerator(
                    settings.openai_api_key,
                    base_url=settings.openai_base_url,
                    model=settings.image_model,
                    proxy_url=getattr(settings, "openai_proxy_url", None),
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
    # Legacy dry runs still render the candidate for operator inspection, but
    # never create delivery, memory, or sharing side effects.
    return False


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
        expected_revision=engine.world_kernel.revision(engine.world_id),
        expires_at=(engine._world_logical_now() if hasattr(engine, "_world_logical_now") else datetime.fromisoformat(str(snapshot["clock"]["logical_at"]))) + timedelta(hours=4),
    )
    if not scheduled:
        print("life event not shared: world policy deferred it")
        return False
    outcome = await CompanionTurn(
        engine,
        _QQLifeShareTurnTransport(delivery, recipient_id),
    ).dispatch_scheduled(
        action_id=scheduled.action_id,
        delivery_id=scheduled.delivery_id,
        observed_at=utc_now(),
        idempotency_key=f"life-share:{scheduled.delivery_id}",
    )
    return outcome.terminal_state == "delivered"


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
