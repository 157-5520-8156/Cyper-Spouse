"""NapCat/OneBot HTTP ingress for the World v2 QQ C2C text lane.

The module owns only provider-envelope validation and lifecycle scheduling. It
does not import the legacy engine, conversation turn, or coalescer modules.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.onebot_adapter import event_token_is_valid, parse_onebot_event

from .qq_c2c_host import QQC2CHost, build_qq_c2c_host


logger = logging.getLogger(__name__)


def create_qq_c2c_onebot_app(
    *,
    adapter: str,
    settings: Settings,
    use_fake_model: bool = False,
    scheduler_interval_seconds: float = 15.0,
) -> FastAPI:
    """Create the opt-in v2 OneBot service for exactly one private QQ user.

    ``NAPCAT_ALLOWED_PRIVATE_USER_IDS`` is intentionally required to contain
    one id.  A missing or multi-user allowlist would create ambiguous target
    ownership and must not silently map several relationships into one world.
    """

    if adapter not in {"napcat", "onebot"}:
        raise ValueError(f"unsupported OneBot adapter: {adapter}")
    if scheduler_interval_seconds <= 0:
        raise ValueError("QQ C2C v2 scheduler interval must be positive")
    recipient_ids = tuple(
        item.strip()
        for item in settings.napcat_allowed_private_user_ids.split(",")
        if item.strip()
    )
    if len(recipient_ids) != 1:
        raise ValueError(
            "World v2 QQ C2C requires exactly one NAPCAT_ALLOWED_PRIVATE_USER_IDS entry"
        )
    recipient_id = recipient_ids[0]
    access_token = (
        settings.napcat_access_token if adapter == "napcat" else settings.onebot_access_token
    ) or None
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id=recipient_id,
        model=FakeCompanionModel() if use_fake_model else None,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(
            _scheduler_loop(host, interval_seconds=scheduler_interval_seconds),
            name="world-v2-qq-c2c-scheduler",
        )
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await host.aclose()

    app = FastAPI(title=f"Girl-Agent {adapter.title()} World v2 C2C", lifespan=lifespan)

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
        raw_event = await request.json()
        incoming = parse_onebot_event(raw_event)
        if incoming is None:
            return {"status": "ignored"}
        if incoming.channel_id:
            return {"status": "ignored_group_v2_unsupported"}
        if incoming.platform_user_id != recipient_id:
            return {"status": "ignored_private"}
        if (
            not incoming.message_id
            or not incoming.text
            or incoming.attachments
            or incoming.emoji
            or incoming.sticker_kind
        ):
            return {"status": "ignored_non_text_v2_unsupported"}
        result = await host.inbound_text(
            message_id=str(incoming.message_id),
            recipient_id=recipient_id,
            text=incoming.text,
            observed_at=datetime.now(UTC),
        )
        return {
            "status": result.status,
            "world_action_id": result.action_id,
            "canonical_user_id": result.canonical_user_id,
        }

    @app.get("/health")
    async def health():
        return {
            "status": "running",
            "adapter": adapter,
            "world_v2": True,
            "mode": "c2c-text-only",
        }

    return app


async def _scheduler_loop(host: QQC2CHost, *, interval_seconds: float) -> None:
    """Bounded recovery loop; each pass resumes from the durable v2 clock."""

    while True:
        try:
            await host.scheduler_once(observed_at=datetime.now(UTC))
        except Exception:
            logger.exception("World v2 QQ C2C scheduler pass failed")
        await asyncio.sleep(interval_seconds)


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


__all__ = ["create_qq_c2c_onebot_app"]
