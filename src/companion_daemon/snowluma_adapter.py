from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from companion_daemon.models import IncomingMessage, MessageAttachment

logger = logging.getLogger(__name__)


@dataclass
class OneBotReplyTarget:
    """ReplyTarget that sends messages via OneBot v11 HTTP API (SnowLuma)."""

    api_url: str
    user_id: int | None = None
    group_id: int | None = None
    access_token: str | None = None

    async def reply(self, **kwargs: object) -> dict[str, Any]:
        content = str(kwargs.get("content", ""))
        if not content:
            return {}
        payload: dict[str, Any] = {"message": content}
        if self.group_id:
            payload["group_id"] = self.group_id
            endpoint = f"{self.api_url}/send_group_msg"
        elif self.user_id:
            payload["user_id"] = self.user_id
            endpoint = f"{self.api_url}/send_private_msg"
        else:
            return {}
        return await _post(endpoint, payload, self.access_token)


async def send_onebot_image(
    api_url: str,
    *,
    user_id: int | None = None,
    group_id: int | None = None,
    image_path: Path,
    access_token: str | None = None,
) -> dict[str, Any]:
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    segment = [{"type": "image", "data": {"file": f"base64://{b64}"}}]
    payload: dict[str, Any] = {"message": segment}
    if group_id:
        payload["group_id"] = group_id
        endpoint = f"{api_url}/send_group_msg"
    elif user_id:
        payload["user_id"] = user_id
        endpoint = f"{api_url}/send_private_msg"
    else:
        return {}
    return await _post(endpoint, payload, access_token)


def parse_onebot_event(event: dict[str, Any]) -> IncomingMessage | None:
    """Convert an OneBot v11 event dict to an IncomingMessage, or None."""
    if event.get("post_type") != "message":
        return None

    user_id = event.get("user_id")
    if not user_id:
        return None

    raw_message = event.get("raw_message") or ""
    message_id = str(event.get("message_id") or "")
    segments = event.get("message", [])

    text_parts: list[str] = []
    attachments: list[MessageAttachment] = []

    if isinstance(segments, str):
        text_parts.append(segments)
    elif isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type", "")
            sdata = seg.get("data", {})
            if stype == "text":
                text_parts.append(str(sdata.get("text", "")))
            elif stype == "image":
                attachments.append(MessageAttachment(
                    kind="image", url=sdata.get("url"),
                    filename=sdata.get("file"), content_type="image/jpeg",
                ))
            elif stype == "record":
                attachments.append(MessageAttachment(
                    kind="audio", url=sdata.get("url"),
                    filename=sdata.get("file"), content_type="audio/amr",
                ))
            elif stype == "video":
                attachments.append(MessageAttachment(
                    kind="video", url=sdata.get("url"), filename=sdata.get("file"),
                ))
            elif stype == "file":
                attachments.append(MessageAttachment(
                    kind="file", url=sdata.get("url"),
                    filename=sdata.get("file"), size=sdata.get("size"),
                ))

    text = raw_message if raw_message else "".join(text_parts)
    if not text and not attachments:
        return None

    channel_id = str(event.get("group_id") or "") if event.get("message_type") == "group" else None

    return IncomingMessage(
        platform="qq",
        platform_user_id=str(user_id),
        text=text.strip(),
        channel_id=channel_id or None,
        message_id=message_id,
        attachments=attachments,
    )


async def _post(url: str, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    return dict(resp.json())
