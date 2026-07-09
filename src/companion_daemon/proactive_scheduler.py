import argparse
import asyncio
from datetime import datetime
import hashlib
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


def _stable_ratio(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(0xFFFFFFFFFFFF)


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
            if elapsed is not None and elapsed < cooldown_minutes:
                print(
                    f"skip {user_id}: proactive cooldown {elapsed:.1f}m/{cooldown_minutes}m "
                    f"(base {base_cooldown_minutes}m)",
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
            await run_life_event(
                user_id=user_id,
                send=send,
                sandbox=sandbox,
                generate_image=generate_life_images,
                image_kind=life_image_kind,
            )
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
