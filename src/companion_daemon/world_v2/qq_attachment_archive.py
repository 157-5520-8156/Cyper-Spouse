"""Deployment-local inbound QQ attachment bytes for optional perception.

The ingress lane deliberately normalizes provider attachments into opaque,
URL-free refs (:func:`qq_ingress_policy.onebot_attachment_ref`) so that no
provider URL can reach a World Event.  Perception, however, needs the exact
bytes behind one accepted ref.  This module owns that boundary:

* :class:`QQAttachmentArchive` is a content store keyed by the opaque ref.
  Bytes live as plain files under a deployment directory; the store persists
  no URL, filename, or provider metadata anywhere.  It also implements the
  perception vertical's :class:`PerceptionInputSource` contract by exposing
  each archived image as one canonical ``data:`` URL string whose hash binds
  the accepted Action to the exact bytes a later dispatch will send.
* :class:`QQOneBotAttachmentArchiver` runs at the adapter boundary, before
  ingress normalization strips the provider envelope.  It pulls image bytes
  through the transient segment URL or the OneBot ``get_image`` API and
  archives them idempotently.  Every failure degrades to "no bytes to
  perceive"; ingress and replies never depend on this hook.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
from typing import Any, Literal, Mapping

import httpx

from .perception_input_source import PerceptionInputDescriptor
from .qq_ingress_policy import onebot_attachment_ref
from .schemas import Action


_LOG = logging.getLogger(__name__)

# Bounded by the ingress fragment contract (refs are at most 512 chars) and
# by the archive's own discipline: one ref maps to exactly one flat file.
_REF_PATTERN = re.compile(r"^[A-Za-z0-9:._-]{1,512}$")

# Image formats the vision provider route accepts as inline data URLs.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def sniff_image_media_type(data: bytes) -> str | None:
    """Identify supported image bytes by signature; never trust filenames."""

    for signature, media_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class QQAttachmentArchive:
    """Idempotent, URL-free local content store keyed by opaque ingress refs.

    ``describe`` and ``resolve`` implement :class:`PerceptionInputSource`:
    the canonical perception body of an archived image is one ``data:`` URL
    string, so the accepted ``input_hash`` binds the exact bytes and media
    type that the vision transport will later submit.  The store survives
    process restarts trivially because a ref is a deterministic file name.
    """

    def __init__(
        self, root: Path, *, max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("attachment archive size bound must be positive")
        self._root = root
        self._max_bytes = max_bytes

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, attachment_ref: str) -> Path:
        if not _REF_PATTERN.match(attachment_ref):
            raise ValueError("attachment ref is not a bounded opaque token")
        return self._root / attachment_ref.replace(":", "_")

    def has(self, attachment_ref: str) -> bool:
        try:
            return self._path(attachment_ref).is_file()
        except ValueError:
            return False

    def read(self, attachment_ref: str) -> bytes | None:
        path = self._path(attachment_ref)
        if not path.is_file():
            return None
        return path.read_bytes()

    def store(self, attachment_ref: str, data: bytes) -> bool:
        """Archive one attachment's bytes; returns False when already present."""

        if not data:
            raise ValueError("attachment archive rejects empty content")
        if len(data) > self._max_bytes:
            raise ValueError("attachment exceeds the archive size bound")
        path = self._path(attachment_ref)
        if path.is_file():
            return False
        self._root.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(path.name + f".tmp-{os.getpid()}")
        scratch.write_bytes(data)
        try:
            scratch.replace(path)
        except OSError:
            scratch.unlink(missing_ok=True)
            raise
        return True

    # -- PerceptionInputSource -------------------------------------------------

    def _canonical_body(self, attachment_ref: str) -> str:
        data = self.read(attachment_ref)
        if data is None:
            raise ValueError("attachment bytes are not archived for this ref")
        media_type = sniff_image_media_type(data)
        if media_type is None:
            raise ValueError("archived attachment is not a supported image format")
        return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"

    def describe(
        self, *, attachment_ref: str, analysis_kind: Literal["vision", "transcription"]
    ) -> PerceptionInputDescriptor:
        if analysis_kind != "vision":
            raise ValueError("the QQ attachment archive only serves vision perception")
        body = self._canonical_body(attachment_ref)
        return PerceptionInputDescriptor(
            attachment_ref=attachment_ref,
            analysis_kind=analysis_kind,
            content_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        )

    async def resolve(self, action: Action) -> tuple[str, str, str]:
        body = self._canonical_body(action.payload_ref)
        digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        return action.payload_ref, digest, body


@dataclass(frozen=True, slots=True)
class QQAttachmentArchiveReport:
    """Process-local evidence of one boundary archiving pass."""

    considered: int = 0
    archived: int = 0
    already_present: int = 0
    failed: int = 0


class QQOneBotAttachmentArchiver:
    """Pull inbound image bytes at the adapter boundary, keyed by opaque ref.

    The provider URL (or ``get_image`` response) exists only inside this
    call; nothing here is a World authority and every failure is swallowed
    into the report so ingress can never block or fail on archiving.
    """

    def __init__(
        self,
        *,
        archive: QQAttachmentArchive,
        api_url: str,
        access_token: str | None = None,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_url:
            raise ValueError("attachment archiver requires the OneBot API URL")
        self._archive = archive
        self._api_url = api_url
        self._access_token = access_token
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def archive(self) -> QQAttachmentArchive:
        return self._archive

    @staticmethod
    def image_segments(event: Mapping[str, Any]) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """Extract (opaque ref, provider segment data) pairs for image segments."""

        if event.get("post_type") != "message" or event.get("message_type") == "group":
            return ()
        segments = event.get("message")
        if not isinstance(segments, list):
            return ()
        found: list[tuple[str, Mapping[str, Any]]] = []
        for segment in segments:
            if not isinstance(segment, Mapping) or str(segment.get("type") or "") != "image":
                continue
            data = segment.get("data")
            data = data if isinstance(data, Mapping) else {}
            found.append((onebot_attachment_ref("image", data), data))
        return tuple(found)

    async def archive_from_event(self, event: Mapping[str, Any]) -> QQAttachmentArchiveReport:
        considered = archived = present = failed = 0
        for attachment_ref, data in self.image_segments(event):
            considered += 1
            if self._archive.has(attachment_ref):
                present += 1
                continue
            try:
                content = await self._fetch(data)
                if self._archive.store(attachment_ref, content):
                    archived += 1
                else:
                    present += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - degrade to "no bytes to perceive"
                failed += 1
                _LOG.warning(
                    "QQ attachment archive failed ref=%s error=%s",
                    attachment_ref,
                    type(exc).__name__,
                )
        return QQAttachmentArchiveReport(
            considered=considered,
            archived=archived,
            already_present=present,
            failed=failed,
        )

    async def _fetch(self, data: Mapping[str, Any]) -> bytes:
        url = str(data.get("url") or "")
        if url.startswith(("http://", "https://")):
            return await self._download(url)
        return await self._fetch_via_get_image(str(data.get("file") or ""))

    async def _fetch_via_get_image(self, file_id: str) -> bytes:
        from companion_daemon.onebot_adapter import get_onebot_image

        if not file_id:
            raise ValueError("image segment carries neither URL nor file identifier")
        resolved = await get_onebot_image(
            self._api_url, file=file_id, access_token=self._access_token
        )
        encoded = resolved.get("base64")
        if isinstance(encoded, str) and encoded:
            return base64.b64decode(encoded, validate=False)
        url = str(resolved.get("url") or "")
        if url.startswith(("http://", "https://")):
            return await self._download(url)
        local = str(resolved.get("file") or "")
        if local.startswith("file://"):
            local = local[len("file://") :]
        path = Path(local)
        if local and path.is_file():
            return path.read_bytes()
        raise ValueError("OneBot get_image returned no readable content")

    async def _download(self, url: str) -> bytes:
        async with httpx.AsyncClient(
            timeout=self._timeout, trust_env=False, transport=self._transport
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                collected = bytearray()
                async for chunk in response.aiter_bytes():
                    collected.extend(chunk)
                    if len(collected) > DEFAULT_MAX_ATTACHMENT_BYTES:
                        raise ValueError("attachment download exceeds the size bound")
                return bytes(collected)


__all__ = [
    "DEFAULT_MAX_ATTACHMENT_BYTES",
    "QQAttachmentArchive",
    "QQAttachmentArchiveReport",
    "QQOneBotAttachmentArchiver",
    "sniff_image_media_type",
]
