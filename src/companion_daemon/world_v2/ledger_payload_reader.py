"""Read-only resolver for payloads that were durably authorized by World v2.

The platform executor deliberately has no ledger access.  This adapter is the
narrow capability it needs: it can resolve one text payload only after tying
the Action to the immutable minimal-reply acceptance manifest and the stored
payload projection.  It cannot commit, settle, or create any external Action.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Protocol

from .ledger import LedgerPort
from .platform_action_executor import ResolvedActionPayload
from .schemas import Action, LedgerProjection


class _ProjectionReader(Protocol):
    @property
    def world_id(self) -> str: ...

    @property
    def blocks_event_loop(self) -> bool: ...

    def project(self) -> LedgerProjection: ...


class LedgerAuthorizedPayloadReader:
    """Resolve exactly one accepted text payload from a read-only projection.

    ``LedgerPort`` is accepted only at construction so composition roots can
    use their existing durable ledger.  The stored value is held as the much
    narrower projection-reader protocol and this class exposes no write
    methods.  A missing, ambiguous, or mismatched manifest is a hard failure;
    a platform adapter must never guess a payload from an Action alone.
    """

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._reader: _ProjectionReader = ledger

    async def resolve(self, action: Action) -> ResolvedActionPayload:
        if action.world_id != self._reader.world_id:
            raise ValueError("authorized payload reader belongs to another world")
        projection = await self._project()
        manifests = tuple(
            item for item in projection.minimal_reply_manifests if item.action_id == action.action_id
        )
        if len(manifests) != 1:
            raise ValueError("Action has no unique minimal-reply authorization manifest")
        manifest = manifests[0]
        if (
            action.kind != "reply"
            or action.layer != "external_action"
            or action.intent_ref != f"{manifest.proposal_id}:{manifest.intent_id}"
            or action.payload_ref != manifest.message_payload_ref
            or action.payload_hash != manifest.message_payload_hash
        ):
            raise ValueError("Action does not exactly bind its authorization manifest")
        payloads = tuple(
            item
            for item in projection.stored_message_payloads
            if item.acceptance_id == manifest.acceptance_id
            and item.proposal_id == manifest.proposal_id
            and item.payload_ref == manifest.message_payload_ref
            and item.payload_hash == manifest.message_payload_hash
        )
        if len(payloads) != 1:
            raise ValueError("authorization manifest has no unique stored message payload")
        payload = payloads[0]
        actual_hash = "sha256:" + hashlib.sha256(payload.text.encode("utf-8")).hexdigest()
        if actual_hash != payload.payload_hash:
            raise ValueError("stored message payload hash does not bind its text")
        return ResolvedActionPayload(
            payload_ref=payload.payload_ref,
            payload_hash=payload.payload_hash,
            content_type=payload.content_type,
            body=payload.text,
        )

    async def _project(self) -> LedgerProjection:
        if self._reader.blocks_event_loop:
            return await asyncio.to_thread(self._reader.project)
        return self._reader.project()


__all__ = ["LedgerAuthorizedPayloadReader"]
