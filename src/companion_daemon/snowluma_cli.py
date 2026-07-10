"""SnowLuma (OneBot v11) adapter CLI.

Starts a FastAPI server that receives OneBot v11 events from SnowLuma
via HTTP webhook, feeds them into the existing QQMessageCoalescer +
CompanionEngine, and sends replies via SnowLuma's HTTP API.

Usage:
    uv run companion-snowluma

Configure in .env:
    SNOWLUMA_API_URL=http://127.0.0.1:5700
    SNOWLUMA_ACCESS_TOKEN=optional_token
"""
from __future__ import annotations

import argparse
import logging
import time

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from companion_daemon.config import get_settings
from companion_daemon.qq_websocket import QQMessageCoalescer
from companion_daemon.runtime import build_companion_engine
from companion_daemon.snowluma_adapter import OneBotReplyTarget, parse_onebot_event
from companion_daemon.turn_taking import TurnTakingPolicy

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    api_url = settings.snowluma_api_url
    access_token = settings.snowluma_access_token or None

    engine = build_companion_engine(use_fake_model=False)
    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=settings.qq_message_batch_seconds,
        turn_policy=TurnTakingPolicy(short_wait_seconds=settings.qq_message_batch_seconds),
        human_timing=True,
        enable_reply_decision=settings.enable_reply_decision,
    )

    seen_ids: set[str] = set()
    recent_text: dict[str, float] = {}

    def is_dup(msg_id: str | None, user_id: str, text: str) -> bool:
        now = time.time()
        if msg_id and msg_id in seen_ids:
            return True
        if msg_id:
            seen_ids.add(msg_id)
            if len(seen_ids) > 500:
                seen_ids.clear()
                seen_ids.add(msg_id)
        key = f"{user_id}:{text[:80]}"
        if now - recent_text.get(key, 0) < 5.0:
            return True
        recent_text[key] = now
        return False

    app = FastAPI(title="Girl-Agent SnowLuma Adapter")

    @app.post("/onebot/event")
    async def onebot_event(request: Request, x_signature: str | None = Header(None)):
        event = await request.json()
        if access_token and x_signature != access_token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        incoming = parse_onebot_event(event)
        if not incoming:
            return {"status": "ignored"}

        if is_dup(incoming.message_id, incoming.platform_user_id, incoming.text):
            return {"status": "duplicate"}

        target = OneBotReplyTarget(
            api_url=api_url,
            user_id=int(incoming.platform_user_id) if not incoming.channel_id else None,
            group_id=int(incoming.channel_id) if incoming.channel_id else None,
            access_token=access_token,
        )

        key = f"c2c:{incoming.platform_user_id}" if not incoming.channel_id else f"group:{incoming.channel_id}:{incoming.platform_user_id}"
        await coalescer.add(key, incoming, target)
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "running", "adapter": "snowluma"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SnowLuma (OneBot v11) companion adapter.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fake", action="store_true", help="Use fake model.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
