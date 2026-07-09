import argparse
import asyncio
from datetime import datetime
import random

from companion_daemon.config import get_settings
from companion_daemon.life_event import run as run_life_event
from companion_daemon.proactive_cli import run as run_once
from companion_daemon.relationship import life_event_probability, proactive_cooldown_minutes
from companion_daemon.runtime import build_companion_engine


def _minutes_since(iso_timestamp: str | None) -> float | None:
    if not iso_timestamp:
        return None
    then = datetime.fromisoformat(iso_timestamp)
    now = datetime.now(then.tzinfo)
    return (now - then).total_seconds() / 60


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
        users = engine.store.canonical_users() or ["geoff"]
        for user_id in users:
            state = engine.store.get_mood_state(user_id)
            cooldown_minutes = proactive_cooldown_minutes(
                state,
                settings.proactive_min_cooldown_minutes,
            )
            last_sent = engine.store.last_proactive_delivery(user_id, "qq")
            elapsed = _minutes_since(last_sent)
            if elapsed is not None and elapsed < cooldown_minutes:
                print(f"skip {user_id}: proactive cooldown {elapsed:.1f}m/{cooldown_minutes}m")
            else:
                await run_once(user_id, send=send, sandbox=sandbox)

            if not life_events:
                continue
            life_last_sent = engine.store.last_proactive_delivery(user_id, "qq:life_event")
            life_elapsed = _minutes_since(life_last_sent)
            life_cooldown = max(cooldown_minutes * 2, 120)
            if life_elapsed is not None and life_elapsed < life_cooldown:
                print(f"skip {user_id}: life-event cooldown {life_elapsed:.1f}m/{life_cooldown}m")
                continue
            probability = life_event_probability(state)
            if random.random() > probability:
                print(f"skip {user_id}: life-event probability {probability:.2f}")
                continue
            await run_life_event(
                user_id=user_id,
                send=send,
                sandbox=sandbox,
                generate_image=generate_life_images,
                image_kind=life_image_kind,
            )
        if once:
            return
        await asyncio.sleep(settings.proactive_interval_seconds)


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
