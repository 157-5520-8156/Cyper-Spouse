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
    """Reply target for a local OneBot v11 HTTP API, including NapCat."""

    api_url: str
    user_id: int | None = None
    group_id: int | None = None
    access_token: str | None = None

    async def reply(self, **kwargs: object) -> dict[str, Any]:
        content = str(kwargs.get("content", ""))
        if not content:
            return {}
        return await self._send_message(content)

    async def send_image(self, image_path: Path) -> dict[str, Any]:
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        return await self._send_message([
            {"type": "image", "data": {"file": f"base64://{b64}"}}
        ])

    async def react_with_emoji(self, message_id: str, emoji_id: str) -> dict[str, Any]:
        """Attach a QQ emoji reaction to a received message (NapCat set_msg_emoji_like)."""
        if not message_id or not emoji_id:
            return {}
        endpoint = f"{self.api_url.rstrip('/')}/set_msg_emoji_like"
        payload: dict[str, Any] = {
            "message_id": message_id,
            "emoji_id": emoji_id,
            "set": True,
        }
        return await _post(endpoint, payload, self.access_token)

    async def send_face(self, face_id: str) -> dict[str, Any]:
        """Send one standard OneBot ``face`` segment as a visible sticker beat."""

        if not face_id or not face_id.isdigit():
            raise ValueError("OneBot face id must be numeric")
        return await self._send_message(
            [{"type": "face", "data": {"id": face_id}}]
        )

    async def get_msg(self, message_id: str) -> dict[str, Any]:
        """Fetch one previously sent/received message by its OneBot message id.

        NapCat and most OneBot v11 implementations expose ``get_msg``.  A
        successful response proves the platform durably persisted the message,
        which is the strongest delivery evidence this API family offers.
        """

        if not message_id:
            raise ValueError("OneBot get_msg requires a message id")
        endpoint = f"{self.api_url.rstrip('/')}/get_msg"
        return await _post(endpoint, {"message_id": message_id}, self.access_token)

    async def set_input_status(self, *, event_type: int) -> dict[str, Any]:
        """Set NapCat's private-chat input state for this target.

        ``set_input_status`` is a NapCat extension rather than portable
        OneBot.  Callers expose it only from the NapCat deployment profile.
        """

        if self.user_id is None:
            raise ValueError("input status requires a private OneBot target")
        if event_type not in {0, 1}:
            raise ValueError("input status event type must be 0 or 1")
        endpoint = f"{self.api_url.rstrip('/')}/set_input_status"
        return await _post(
            endpoint,
            {"user_id": str(self.user_id), "event_type": event_type},
            self.access_token,
        )

    async def _send_message(self, message: str | list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {"message": message}
        if self.group_id:
            payload["group_id"] = self.group_id
            endpoint = f"{self.api_url.rstrip('/')}/send_group_msg"
        elif self.user_id:
            payload["user_id"] = self.user_id
            endpoint = f"{self.api_url.rstrip('/')}/send_private_msg"
        else:
            return {}
        return await _post(endpoint, payload, self.access_token)


async def get_onebot_friend_msg_history(
    api_url: str,
    *,
    user_id: str,
    count: int = 30,
    access_token: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch the most recent private-chat history for one peer.

    ``get_friend_msg_history`` is a NapCat extension (also implemented by
    several other OneBot v11 forks).  ``message_seq`` 0 asks for the newest
    messages.  Callers own filtering and idempotency; this helper only
    returns the provider's raw message objects, newest last.
    """

    if not user_id:
        raise ValueError("friend message history requires a peer user id")
    if not 1 <= count <= 200:
        raise ValueError("friend message history count must be between 1 and 200")
    endpoint = f"{api_url.rstrip('/')}/get_friend_msg_history"
    response = await _post(
        endpoint,
        {"user_id": user_id, "message_seq": 0, "count": count, "reverseOrder": False},
        access_token,
    )
    data = response.get("data")
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict)]


async def get_onebot_image(
    api_url: str,
    *,
    file: str,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Resolve one received image segment through the OneBot ``get_image`` API.

    NapCat (and most OneBot v11 forks) return a ``data`` object that may
    carry a re-signed download ``url``, a provider-local ``file`` path, or a
    ``base64`` body depending on the implementation.  The caller owns
    choosing among them; this helper only performs the provider call.
    """

    if not file:
        raise ValueError("OneBot get_image requires the segment file identifier")
    endpoint = f"{api_url.rstrip('/')}/get_image"
    response = await _post(endpoint, {"file": file}, access_token)
    data = response.get("data")
    return data if isinstance(data, dict) else {}


async def send_onebot_emoji_like(
    api_url: str,
    *,
    message_id: str,
    emoji_id: str,
    access_token: str | None = None,
) -> dict[str, Any]:
    target = OneBotReplyTarget(api_url=api_url, access_token=access_token)
    return await target.react_with_emoji(message_id, emoji_id)


async def send_onebot_image(
    api_url: str,
    *,
    user_id: int | None = None,
    group_id: int | None = None,
    image_path: Path,
    access_token: str | None = None,
) -> dict[str, Any]:
    target = OneBotReplyTarget(
        api_url=api_url,
        user_id=user_id,
        group_id=group_id,
        access_token=access_token,
    )
    return await target.send_image(image_path)


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
    emoji: list[str] = []
    sticker_kind: str | None = None
    reply_target: str | None = None

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
            elif stype == "face":
                face_id = str(sdata.get("id") or "").strip()
                if face_id:
                    emoji.append(f"qq-face:{face_id}")
            elif stype in {"mface", "market_face"}:
                sticker_kind = str(
                    sdata.get("summary") or sdata.get("emoji_id") or "qq-market-face"
                )[:80]
            elif stype == "reply":
                reply_target = str(sdata.get("id") or "").strip()[:160] or None
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
    if not text and not attachments and not emoji and not sticker_kind:
        return None

    channel_id = str(event.get("group_id") or "") if event.get("message_type") == "group" else None

    return IncomingMessage(
        platform="qq",
        platform_user_id=str(user_id),
        text=text.strip(),
        channel_id=channel_id or None,
        message_id=message_id,
        attachments=attachments,
        emoji=emoji[:16],
        sticker_kind=sticker_kind,
        reply_target=reply_target,
    )


def event_token_is_valid(
    expected_token: str | None,
    *,
    authorization: str | None,
    x_signature: str | None,
) -> bool:
    """Accept common OneBot token header styles."""
    if not expected_token:
        return True
    accepted_authorizations = {
        expected_token,
        f"Bearer {expected_token}",
        f"Token {expected_token}",
    }
    return authorization in accepted_authorizations or x_signature == expected_token


async def _post(url: str, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    return dict(resp.json())
