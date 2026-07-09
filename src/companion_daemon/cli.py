import argparse
import asyncio

from companion_daemon.models import IncomingMessage
from companion_daemon.runtime import build_companion_engine


async def run_simulation(text: str, fake: bool) -> None:
    engine = build_companion_engine(use_fake_model=fake)
    reply = await engine.handle_message(
        IncomingMessage(platform="simulator", platform_user_id="geoff", text=text)
    )
    print(f"[mood={reply.mood}] {reply.text}")
    decision = await engine.proactive_tick(reply.canonical_user_id)
    print(f"[private] {decision.private_thought}")
    if decision.should_send:
        print(f"[proactive:{decision.platform}] {decision.message}")
        if decision.sticker_path:
            print(f"[sticker] {decision.sticker_path}")
        if decision.image_path:
            print(f"[image] {decision.image_path}")
    else:
        print("[proactive] no message")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a companion chat turn.")
    parser.add_argument("text", help="Incoming user text")
    parser.add_argument("--fake", action="store_true", help="Do not call DeepSeek")
    args = parser.parse_args()
    asyncio.run(run_simulation(args.text, args.fake))
