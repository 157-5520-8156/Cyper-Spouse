"""Read-only adapter from immutable Media v2 sidecar to ActionPump payloads."""

from __future__ import annotations

from .media_v2 import ImmutableMediaPayloadStore
from .platform_action_executor import AuthorizedPayloadReader, ResolvedActionPayload
from .schemas import Action


class MediaSidecarPayloadReader:
    def __init__(self, *, store: ImmutableMediaPayloadStore) -> None:
        self._store = store

    async def resolve(self, action: Action) -> ResolvedActionPayload:
        record = self._store.read_exact(payload_ref=action.payload_ref)
        if record is None or record.payload_hash != action.payload_hash:
            raise ValueError("Media v2 Action payload sidecar is missing or does not bind Action")
        return ResolvedActionPayload(
            payload_ref=record.payload_ref,
            payload_hash=record.payload_hash,
            content_type=record.content_type,
            body=record.body,
        )


class PlatformAndMediaPayloadReader:
    """Route only delivery bytes to the immutable media-sidecar capability."""

    def __init__(self, *, platform: AuthorizedPayloadReader, media: MediaSidecarPayloadReader) -> None:
        self._platform = platform
        self._media = media

    async def resolve(self, action: Action) -> ResolvedActionPayload:
        if action.kind == "media_delivery":
            return await self._media.resolve(action)
        return await self._platform.resolve(action)


__all__ = ["MediaSidecarPayloadReader", "PlatformAndMediaPayloadReader"]
