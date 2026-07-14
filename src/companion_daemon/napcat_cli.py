"""NapCat (OneBot v11) adapter CLI.

Receives NapCat HTTP events locally and routes them through the same
CompanionEngine and QQ message coalescer as the official QQ bot adapter.
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime
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
from companion_daemon.companion_turn import DispatchAcceptance
from companion_daemon.companion_turn import CompanionTurn, ResponseBudget, ScheduledTurnFrame
from companion_daemon.qq_websocket import QQMessageCoalescer, QQTurnTransport
from companion_daemon.qq_delivery import QQDelivery
from companion_daemon.qq_runtime_observations import QQTurnObservationJSONLExporter
from companion_daemon.process_lock import AlreadyRunningError
from companion_daemon.qq_outbound_owner import (
    QQOutboundOwnerLease,
    qq_outbound_owner_lock_path,
    validate_qq_outbound_configuration,
)
from companion_daemon.runtime import build_companion_engine
from companion_daemon.time import utc_now
from companion_daemon.turn_taking import TurnTakingPolicy
from companion_daemon.world import ConcurrencyConflict
from companion_daemon.world_clock import WorldClockDriver

logger = logging.getLogger(__name__)

NAPCAT_SCHEDULED_BUDGET = ResponseBudget(first_visible_by_ms=12_000, complete_by_ms=15_000)


def onebot_image_dispatch_acceptance(result: object | None) -> DispatchAcceptance:
    """Classify a OneBot image-send response without inventing delivery evidence.

    OneBot/NapCat's synchronous response is usable as a delivery receipt only
    when it contains the created message's identifier.  A successful-looking
    envelope without one remains uncertain: it might represent a partial
    adapter failure or an API variant we do not yet understand.
    """
    if isinstance(result, Mapping):
        status = str(result.get("status") or "").strip().lower()
        retcode = result.get("retcode")
        try:
            rejected = retcode is not None and int(str(retcode)) != 0
        except (TypeError, ValueError):
            rejected = False
        if status == "failed" or rejected:
            reason = str(
                result.get("message")
                or result.get("wording")
                or "onebot_image_rejected"
            )[:300]
            return DispatchAcceptance(status="failed", reason=reason)

    receipt = QQDelivery.receipt_candidate(result)
    if receipt:
        return DispatchAcceptance(status="delivered", external_receipt=receipt)
    return DispatchAcceptance(
        status="unknown",
        reason="onebot_image_returned_without_durable_receipt",
    )


def onebot_reaction_dispatch_acceptance(result: object | None) -> DispatchAcceptance:
    """Classify a OneBot reaction result without treating HTTP success as delivery.

    ``set_msg_emoji_like`` commonly returns only an acknowledgement.  The
    request's incoming message id and emoji id describe what we *asked* the
    adapter to do; neither is a platform-issued receipt proving that the
    reaction was applied.  Only an identifier returned by OneBot itself may
    close the reaction Action as delivered.
    """
    if isinstance(result, Mapping):
        status = str(result.get("status") or "").strip().lower()
        retcode = result.get("retcode")
        try:
            rejected = retcode is not None and int(str(retcode)) != 0
        except (TypeError, ValueError):
            rejected = False
        if status == "failed" or rejected:
            reason = str(
                result.get("message")
                or result.get("wording")
                or "onebot_reaction_rejected"
            )[:300]
            return DispatchAcceptance(status="failed", reason=reason)

    receipt = QQDelivery.receipt_candidate(result)
    if receipt:
        return DispatchAcceptance(status="delivered", external_receipt=receipt)
    return DispatchAcceptance(
        status="unknown",
        reason="onebot_reaction_returned_without_durable_receipt",
    )


async def send_onebot_image_with_acceptance(
    target: OneBotReplyTarget, image_path: Path
) -> DispatchAcceptance:
    """Send one image and preserve the adapter's real receipt semantics."""
    try:
        return onebot_image_dispatch_acceptance(await target.send_image(image_path))
    except Exception as exc:
        return DispatchAcceptance(
            status="unknown",
            reason=f"onebot_image_exception:{type(exc).__name__}",
        )


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

    async def send_reply_image(
        incoming: IncomingMessage, reply: CompanionReply
    ) -> DispatchAcceptance:
        image_path = reply.sticker_path or reply.image_path
        if not image_path:
            return DispatchAcceptance(status="failed", reason="onebot_image_path_missing")
        target = _target_for(incoming, api_url, access_token)
        return await send_onebot_image_with_acceptance(target, Path(image_path))

    async def send_reaction(
        incoming: IncomingMessage, reply: CompanionReply
    ) -> DispatchAcceptance | None:
        emoji_id = qq_emoji_id(reply.suggested_reaction)
        if not emoji_id or not incoming.message_id:
            return None
        try:
            result = await send_onebot_emoji_like(
                api_url,
                message_id=incoming.message_id,
                emoji_id=emoji_id,
                access_token=access_token,
            )
        except Exception as exc:
            return DispatchAcceptance(
                status="unknown",
                reason=f"adapter_result_uncertain:{type(exc).__name__}",
            )
        return onebot_reaction_dispatch_acceptance(result)

    coalescer = QQMessageCoalescer(
        engine,
        delay_seconds=settings.qq_message_batch_seconds,
        turn_policy=TurnTakingPolicy(
            short_wait_seconds=settings.qq_message_batch_seconds,
            long_wait_seconds=max(1.2, min(2.0, settings.qq_message_batch_seconds * 2.0)),
            long_burst_seconds=6.0,
            longform_start_seconds=4.0,
        ),
        on_sticker=send_reply_image,
        on_image=send_reply_image,
        on_reaction=send_reaction,
        human_timing=True,
        enable_reply_decision=settings.enable_reply_decision,
        on_turn_observation=(
            QQTurnObservationJSONLExporter(settings.qq_turn_observation_path)
            if settings.qq_turn_observation_path is not None
            else None
        ),
        runtime_adapter=adapter,
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        recovery_task = asyncio.create_task(
            _scheduled_recovery_loop(engine, api_url=api_url, access_token=access_token)
        )
        try:
            yield
        finally:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
            await engine.aclose()

    app = FastAPI(title=f"Girl-Agent {adapter.title()} Adapter", lifespan=lifespan)

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


async def _scheduled_recovery_loop(
    engine,
    *,
    api_url: str,
    access_token: str | None,
    interval_seconds: float = 15.0,
) -> None:
    """Recover World-scheduled delayed replies through the OneBot/NapCat channel."""
    while True:
        try:
            await _recover_due_world_scheduled_actions(
                engine, api_url=api_url, access_token=access_token
            )
        except Exception:
            logger.exception("NapCat scheduled recovery pass failed")
        await asyncio.sleep(interval_seconds)


async def _recover_due_world_scheduled_actions(
    engine,
    *,
    api_url: str,
    access_token: str | None,
) -> int:
    world = getattr(engine, "world_kernel", None)
    world_id = getattr(engine, "world_id", None)
    if world is None or not world_id:
        return 0
    try:
        WorldClockDriver(world).tick(
            world_id,
            observed_now=utc_now(),
            expected_revision=world.revision(world_id),
        )
    except ConcurrencyConflict:
        return 0
    snapshot = world.snapshot(world_id)
    logical_now = snapshot.get("clock", {}).get("logical_at")
    if not logical_now:
        return 0

    due = [
        item
        for item in world.due_actions(world_id, now=datetime.fromisoformat(str(logical_now)))
        if item.get("kind") in {"reply_later", "conversation_pulse"}
    ]
    recovered = 0
    for action in due:
        kind = str(action.get("kind") or "")
        action_id = str(action.get("action_id") or "")
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        try:
            if kind == "reply_later":
                raw_message = payload.get("message") if isinstance(payload, dict) else None
                if not isinstance(raw_message, dict):
                    engine.cancel_deferred_reply_task(action_id)
                    continue
                from companion_daemon.models import IncomingMessage

                message = IncomingMessage.model_validate(raw_message)
                target = _target_for(message, api_url, access_token)
                frame = ScheduledTurnFrame(
                    source_action_id=action_id,
                    canonical_user_id=engine.store.resolve_user(
                        message.platform, message.platform_user_id
                    ),
                    platform=message.platform,
                    platform_user_id=message.platform_user_id,
                    observed_at=utc_now(),
                    idempotency_key=f"napcat-world-deferred:{action_id}",
                    kind="reply_later",
                    message=message,
                    frozen_cadence="cold",
                )
                outcome = await CompanionTurn(
                    engine, QQTurnTransport(target)
                ).resume_scheduled_reply(
                    frame,
                    budget=NAPCAT_SCHEDULED_BUDGET,
                    context_hint="刚才读到了但被手头的事岔开，现在补回来。",
                )
            else:
                platform_user_id = str(payload.get("platform_user_id") or "")
                if not platform_user_id:
                    engine.cancel_conversation_pulse(action_id)
                    continue
                target = OneBotReplyTarget(
                    api_url=api_url,
                    user_id=int(platform_user_id),
                    access_token=access_token,
                )

                frame = ScheduledTurnFrame(
                    source_action_id=action_id,
                    canonical_user_id=str(payload.get("canonical_user_id") or "geoff"),
                    platform=str(payload.get("platform") or "qq"),
                    platform_user_id=platform_user_id,
                    observed_at=utc_now(),
                    idempotency_key=f"napcat-world-pulse:{action_id}",
                    kind="conversation_pulse",
                    reply_sent_at=datetime.fromisoformat(str(payload["reply_sent_at"])),
                    mode=str(payload.get("mode") or "quick_continue"),
                    frozen_cadence="cold",
                )
                outcome = await CompanionTurn(
                    engine, QQTurnTransport(target)
                ).deliver_conversation_pulse(frame, budget=NAPCAT_SCHEDULED_BUDGET)
            if outcome.visible_status == "delivered":
                recovered += 1
        except Exception:
            logger.exception("failed to recover NapCat scheduled action %s", action_id)
    return recovered


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
