"""NapCat (OneBot v11) adapter CLI.

Receives NapCat HTTP events locally and routes them through the same
CompanionEngine and QQ message coalescer as the official QQ bot adapter.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import time

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from companion_daemon.config import get_settings
from companion_daemon.emotion_reactions import qq_emoji_id
from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.onebot_adapter import (
    OneBotReplyTarget,
    event_token_is_valid,
    parse_onebot_event,
    send_onebot_emoji_like,
)
from companion_daemon.qq_websocket import QQMessageCoalescer
from companion_daemon.process_lock import AlreadyRunningError
from companion_daemon.qq_outbound_owner import (
    QQOutboundOwnerLease,
    qq_outbound_owner_lock_path,
    validate_qq_outbound_configuration,
)
from companion_daemon.runtime import build_companion_engine
from companion_daemon.turn_taking import TurnTakingPolicy

logger = logging.getLogger(__name__)


def create_app(*, adapter: str = "napcat", use_fake_model: bool = False) -> FastAPI:
    settings = get_settings()
    validate_qq_outbound_configuration(
        configured_adapter=settings.qq_adapter,
        launched_adapter=adapter,
    )
    if adapter == "napcat":
        api_url = settings.napcat_api_url
        access_token = settings.napcat_access_token or None
    elif adapter == "onebot":
        api_url = settings.onebot_api_url
        access_token = settings.onebot_access_token or None
    else:
        raise ValueError(f"unsupported OneBot adapter: {adapter}")
    allowed_private_ids = _parse_id_list(settings.napcat_allowed_private_user_ids)

    engine = build_companion_engine(use_fake_model=use_fake_model)

    async def send_reply_image(incoming: IncomingMessage, reply: CompanionReply) -> None:
        image_path = reply.sticker_path or reply.image_path
        if not image_path:
            return
        target = _target_for(incoming, api_url, access_token)
        await target.send_image(Path(image_path))

    async def send_reaction(incoming: IncomingMessage, reply: CompanionReply) -> None:
        emoji_id = qq_emoji_id(reply.suggested_reaction)
        if not emoji_id or not incoming.message_id:
            return
        action_id = engine.begin_reaction_delivery(incoming, reply)
        try:
            result = await send_onebot_emoji_like(
                api_url,
                message_id=incoming.message_id,
                emoji_id=emoji_id,
                access_token=access_token,
            )
        except Exception as exc:
            engine.settle_reaction_delivery(
                action_id,
                status="unknown",
                reason=f"adapter_result_uncertain:{type(exc).__name__}",
            )
            raise
        if result.get("status") == "failed" or int(result.get("retcode") or 0) != 0:
            engine.settle_reaction_delivery(
                action_id,
                status="failed",
                reason=str(result.get("message") or "onebot_reaction_rejected")[:300],
            )
            return
        receipt = f"onebot-reaction:{incoming.message_id}:{emoji_id}"
        engine.settle_reaction_delivery(
            action_id,
            status="delivered",
            external_receipt=receipt,
        )

    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=settings.qq_message_batch_seconds,
        turn_policy=TurnTakingPolicy(short_wait_seconds=settings.qq_message_batch_seconds),
        on_sticker=send_reply_image,
        on_image=send_reply_image,
        on_reaction=send_reaction,
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

    app = FastAPI(title=f"Girl-Agent {adapter.title()} Adapter")

    @app.post("/onebot/event")
    async def onebot_event(
        request: Request,
        authorization: str | None = Header(None),
        x_signature: str | None = Header(None),
    ):
        if not _event_request_is_authorized(
            request,
            access_token,
            authorization=authorization,
            x_signature=x_signature,
            accept_unauthenticated_local=settings.napcat_accept_unauthenticated_local_events,
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        event = await request.json()
        incoming = parse_onebot_event(event)
        if not incoming:
            return {"status": "ignored"}
        if incoming.channel_id and not settings.napcat_allow_group_messages:
            return {"status": "ignored_group"}
        if not incoming.channel_id and not _private_sender_is_allowed(
            incoming.platform_user_id, allowed_private_ids
        ):
            return {"status": "ignored_private"}
        if is_dup(incoming.message_id, incoming.platform_user_id, incoming.text):
            return {"status": "duplicate"}
        target = _target_for(incoming, api_url, access_token)
        key = (
            f"c2c:{incoming.platform_user_id}"
            if not incoming.channel_id
            else f"group:{incoming.channel_id}:{incoming.platform_user_id}"
        )
        await coalescer.add(key, incoming, target)
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "running", "adapter": adapter}

    return app


def _target_for(
    incoming: IncomingMessage, api_url: str, access_token: str | None
) -> OneBotReplyTarget:
    return OneBotReplyTarget(
        api_url=api_url,
        user_id=int(incoming.platform_user_id) if not incoming.channel_id else None,
        group_id=int(incoming.channel_id) if incoming.channel_id else None,
        access_token=access_token,
    )


def _parse_id_list(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _private_sender_is_allowed(user_id: str, allowed_ids: set[str]) -> bool:
    if not allowed_ids:
        return True
    return user_id in allowed_ids


def _event_request_is_authorized(
    request: Request,
    expected_token: str | None,
    *,
    authorization: str | None,
    x_signature: str | None,
    accept_unauthenticated_local: bool,
) -> bool:
    if event_token_is_valid(expected_token, authorization=authorization, x_signature=x_signature):
        return True
    client_host = request.client.host if request.client else None
    return bool(accept_unauthenticated_local and client_host in {"127.0.0.1", "::1"})


def main() -> None:
    _run_cli(default_adapter="napcat")


def onebot_main() -> None:
    _run_cli(default_adapter="onebot")


def _run_cli(*, default_adapter: str) -> None:
    parser = argparse.ArgumentParser(description="Run a OneBot v11 companion adapter.")
    parser.add_argument("--adapter", choices=("napcat", "onebot"), default=default_adapter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--fake", action="store_true", help="Use the local fake model.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    lock_path = qq_outbound_owner_lock_path(settings.database_path)
    try:
        with QQOutboundOwnerLease(lock_path, adapter=args.adapter):
            uvicorn.run(
                create_app(adapter=args.adapter, use_fake_model=args.fake),
                host=args.host,
                port=args.port,
            )
    except AlreadyRunningError as exc:
        raise SystemExit(f"QQ outbound adapter cannot start: {exc}") from exc


if __name__ == "__main__":
    main()
