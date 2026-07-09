import argparse
import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import random
import time

import botpy
from botpy.message import C2CMessage, GroupMessage

from companion_daemon.engine import CompanionEngine
from companion_daemon.config import get_settings
from companion_daemon.models import CompanionReply, IncomingMessage, MessageAttachment
from companion_daemon.im_timing import between_part_delay_seconds, initial_reply_delay_seconds
from companion_daemon.multimodal import attachment_kind
from companion_daemon.qq_client import QQOfficialClient
from companion_daemon.turn_taking import TurnInput, TurnTakingPolicy
from companion_daemon.runtime import build_companion_engine

logger = logging.getLogger(__name__)


class ReplyTarget:
    async def reply(self, **kwargs) -> object:
        raise NotImplementedError


@dataclass
class QueuedQQMessage:
    incoming: IncomingMessage
    reply_target: ReplyTarget


class QQMessageCoalescer:
    def __init__(
        self,
        engine: CompanionEngine,
        *,
        delay_seconds: float,
        turn_policy: TurnTakingPolicy | None = None,
        on_reply: Callable[[CompanionReply], Awaitable[None]] | None = None,
        on_sticker: Callable[[IncomingMessage, CompanionReply], Awaitable[None]] | None = None,
        on_image: Callable[[IncomingMessage, CompanionReply], Awaitable[None]] | None = None,
        human_timing: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.engine = engine
        self.delay_seconds = delay_seconds
        self.turn_policy = turn_policy or TurnTakingPolicy(short_wait_seconds=delay_seconds)
        self.on_reply = on_reply
        self.on_sticker = on_sticker
        self.on_image = on_image
        self.human_timing = human_timing
        self.sleep = sleep
        self.rng = rng or random.Random()
        self._pending: dict[str, list[QueuedQQMessage]] = defaultdict(list)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def add(self, key: str, incoming: IncomingMessage, reply_target: ReplyTarget) -> None:
        self._pending[key].append(QueuedQQMessage(incoming=incoming, reply_target=reply_target))
        decision = self._decision_for(key)
        existing = self._tasks.get(key)
        if existing and not existing.done():
            existing.cancel()
        self._tasks[key] = asyncio.create_task(self._flush_later(key, decision.wait_seconds))

    def _decision_for(self, key: str):
        queued = self._pending[key]
        latest = queued[-1].incoming.text if queued else ""
        merged = "\n".join(item.incoming.text for item in queued if item.incoming.text.strip())
        return self.turn_policy.decide(
            TurnInput(pending_count=len(queued), latest_text=latest, merged_text=merged)
        )

    async def _flush_later(self, key: str, wait_seconds: float) -> None:
        try:
            await asyncio.sleep(wait_seconds)
            queued = self._pending.pop(key, [])
            if not queued:
                return
            last = queued[-1]
            merged_text = "\n".join(item.incoming.text for item in queued if item.incoming.text.strip())
            attachments = [
                attachment
                for item in queued
                for attachment in item.incoming.attachments
            ]
            merged = last.incoming.model_copy(
                update={"text": merged_text, "attachments": attachments}
            )
            reply = await self.engine.handle_message(merged)
            try:
                if self.human_timing:
                    await self.sleep(initial_reply_delay_seconds(merged, reply, rng=self.rng))
                await _send_reply_parts(
                    last.reply_target,
                    reply.text_parts or [reply.text],
                    sleep=self.sleep,
                    rng=self.rng,
                    human_timing=self.human_timing,
                )
            except Exception:
                logger.exception("failed to send QQ reply")
                return
            if self.on_sticker and reply.sticker_path:
                try:
                    await self.on_sticker(merged, reply)
                except Exception:
                    logger.exception("failed to send QQ sticker reply")
            if self.on_image and reply.image_path:
                try:
                    await self.on_image(merged, reply)
                except Exception:
                    logger.exception("failed to send QQ image reply")
            if self.on_reply:
                await self.on_reply(reply)
        except asyncio.CancelledError:
            return
        finally:
            task = self._tasks.get(key)
            if task is asyncio.current_task():
                self._tasks.pop(key, None)


class CompanionQQClient(botpy.Client):
    def __init__(self, *, use_fake_model: bool = False, **kwargs):
        is_sandbox = bool(kwargs.get("is_sandbox", False))
        super().__init__(**kwargs)
        self.engine = build_companion_engine(use_fake_model=use_fake_model)
        settings = get_settings()
        api_base_url = "https://sandbox.api.sgroup.qq.com" if is_sandbox else "https://api.sgroup.qq.com"
        self.qq_api = (
            QQOfficialClient(settings.qq_bot_app_id, settings.qq_bot_secret, api_base_url=api_base_url)
            if settings.qq_bot_app_id and settings.qq_bot_secret
            else None
        )
        self.coalescer = QQMessageCoalescer(
            self.engine,
            delay_seconds=settings.qq_message_batch_seconds,
            turn_policy=TurnTakingPolicy(short_wait_seconds=settings.qq_message_batch_seconds),
            on_reply=self._log_reply,
            on_sticker=self._send_reply_sticker,
            on_image=self._send_reply_image,
            human_timing=True,
        )

    async def on_ready(self) -> None:
        logger.info("QQ WebSocket client is ready: %s", self.robot.name)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        incoming = IncomingMessage(
            platform="qq",
            platform_user_id=message.author.user_openid,
            text=_clean_content(message.content),
            message_id=message.id,
            attachments=_attachments_from_botpy(message.attachments),
        )
        if not incoming.text and not incoming.attachments:
            return
        await self.coalescer.add(f"c2c:{incoming.platform_user_id}", incoming, message)

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        incoming = IncomingMessage(
            platform="qq",
            platform_user_id=message.author.member_openid,
            channel_id=message.group_openid,
            text=_clean_content(message.content),
            message_id=message.id,
            attachments=_attachments_from_botpy(message.attachments),
        )
        if not incoming.text and not incoming.attachments:
            return
        await self.coalescer.add(
            f"group:{incoming.channel_id}:{incoming.platform_user_id}",
            incoming,
            message,
        )

    async def _log_reply(self, reply: CompanionReply) -> None:
        logger.info("replied to %s; mood=%s", reply.canonical_user_id, reply.mood)

    async def _send_reply_sticker(self, incoming: IncomingMessage, reply: CompanionReply) -> None:
        if reply.sticker_path:
            await self._send_local_image(incoming, Path(reply.sticker_path))

    async def _send_reply_image(self, incoming: IncomingMessage, reply: CompanionReply) -> None:
        if reply.image_path:
            await self._send_local_image(incoming, Path(reply.image_path))

    async def _send_local_image(self, incoming: IncomingMessage, path: Path) -> None:
        if not self.qq_api:
            return
        if incoming.channel_id:
            await self.qq_api.send_group_local_image(
                incoming.channel_id,
                path,
                msg_id=incoming.message_id,
            )
        else:
            await self.qq_api.send_c2c_local_image(
                incoming.platform_user_id,
                path,
                msg_id=incoming.message_id,
            )


def _clean_content(content: str | None) -> str:
    return (content or "").strip()


def _reply_msg_seq() -> int:
    return int(time.time() * 1000) % 1_000_000


async def _send_reply_parts(
    reply_target: ReplyTarget,
    parts: list[str],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
    human_timing: bool = True,
) -> None:
    rng = rng or random.Random()
    for index, part in enumerate(parts):
        if index:
            delay = between_part_delay_seconds(part, rng=rng) if human_timing else min(1.8, 0.45 + len(part) / 45)
            await sleep(delay)
        await reply_target.reply(content=part, msg_seq=_reply_msg_seq())


def _attachments_from_botpy(raw_attachments) -> list[MessageAttachment]:
    attachments: list[MessageAttachment] = []
    for item in raw_attachments or []:
        content_type = getattr(item, "content_type", None)
        filename = getattr(item, "filename", None)
        kind = attachment_kind(content_type, filename)
        attachments.append(
            MessageAttachment(
                kind=kind,
                url=getattr(item, "url", None),
                filename=filename,
                content_type=content_type,
                size=getattr(item, "size", None),
                width=getattr(item, "width", None),
                height=getattr(item, "height", None),
            )
        )
    return attachments


def main() -> None:
    parser = argparse.ArgumentParser(description="Run QQ official WebSocket companion adapter.")
    parser.add_argument("--sandbox", action="store_true", help="Use QQ sandbox environment.")
    parser.add_argument("--fake", action="store_true", help="Use fake model instead of DeepSeek.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.qq_bot_app_id or not settings.qq_bot_secret:
        raise SystemExit("QQ_BOT_APP_ID and QQ_BOT_SECRET are required")

    intents = botpy.Intents(public_messages=True)
    client = CompanionQQClient(
        intents=intents,
        is_sandbox=args.sandbox,
        use_fake_model=args.fake,
    )
    client.run(appid=settings.qq_bot_app_id, secret=settings.qq_bot_secret)


if __name__ == "__main__":
    main()
