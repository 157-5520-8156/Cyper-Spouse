import argparse
import asyncio
from hashlib import sha256

from companion_daemon.companion_turn import CompanionTurn, ResponseBudget, TurnEnvelope
from companion_daemon.models import IncomingMessage
from companion_daemon.runtime import build_companion_engine
from companion_daemon.turn_transports import CaptureTurnTransport


async def run_simulation(text: str, fake: bool) -> None:
    engine = build_companion_engine(use_fake_model=fake)
    try:
        message = IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id=f"simulation:{sha256(text.encode()).hexdigest()[:20]}",
            text=text,
        )
        transport = CaptureTurnTransport(receipt_namespace="simulator")
        turn = CompanionTurn(engine, transport)
        outcome = await turn.respond(
            TurnEnvelope.from_message(
                message,
                idempotency_key=(
                    f"{message.platform}:{message.platform_user_id}:{message.message_id}"
                ),
            ),
            budget=ResponseBudget(first_visible_by_ms=8_000, complete_by_ms=12_000),
        )
        await turn.wait_for_delivery_continuations()
        if not transport.text:
            print("[reply] no immediate reply")
            return
        print(f"[reply:{outcome.visible_status}] {transport.text}")
        decision = await engine.proactive_tick("geoff")
        print(f"[private] {decision.private_thought}")
        if decision.should_send:
            print(f"[proactive:{decision.platform}] {decision.message}")
            if decision.sticker_path:
                print(f"[sticker] {decision.sticker_path}")
            if decision.image_path:
                print(f"[image] {decision.image_path}")
        else:
            print("[proactive] no message")
    finally:
        await engine.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a companion chat turn.")
    parser.add_argument("text", help="Incoming user text")
    parser.add_argument("--fake", action="store_true", help="Do not call DeepSeek")
    args = parser.parse_args()
    asyncio.run(run_simulation(args.text, args.fake))
