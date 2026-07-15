"""Read-only adapter from immutable Media v2 sidecar to ActionPump payloads."""

from __future__ import annotations

from .media_v2 import ImmutableMediaPayloadStore
from .platform_action_executor import ResolvedActionPayload
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


__all__ = ["MediaSidecarPayloadReader"]
