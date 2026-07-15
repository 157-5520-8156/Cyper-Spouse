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
from .expression_payload_store import ImmutableExpressionPayloadStore, expression_payload_hash
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

    def __init__(
        self, *, ledger: LedgerPort, expression_payload_store: ImmutableExpressionPayloadStore | None = None
    ) -> None:
        self._reader: _ProjectionReader = ledger
        self._expression_payload_store = expression_payload_store

    async def resolve(self, action: Action) -> ResolvedActionPayload:
        if action.world_id != self._reader.world_id:
            raise ValueError("authorized payload reader belongs to another world")
        projection = await self._project()
        generic = tuple(
            (manifest, beat)
            for manifest in projection.expression_plan_manifests
            for beat in manifest.beats
            if beat.action.action_id == action.action_id
        )
        if generic:
            if len(generic) != 1:
                raise ValueError("Action has no unique expression-plan authorization manifest")
            return self._resolve_generic(action=action, projection=projection, manifest=generic[0][0], beat=generic[0][1])
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

    def _resolve_generic(self, *, action: Action, projection: LedgerProjection, manifest: object, beat: object) -> ResolvedActionPayload:
        # Keep this intentionally structural: projection values are frozen
        # pydantic models, but narrowing through their immutable fields avoids
        # giving a platform adapter any ledger mutation capability.
        if (
            action.intent_ref != f"{manifest.proposal_id}:{beat.intent_id}"
            or action.payload_ref != beat.payload_ref
            or action.payload_hash != beat.payload_hash
            or action.expression_plan_id != manifest.plan_id
            or action.expression_beat_id != beat.beat_id
            or action != beat.action
        ):
            raise ValueError("Action does not exactly bind its expression-plan authorization manifest")
        if beat.storage_kind == "inline_text":
            payloads = tuple(item for item in projection.stored_message_payloads if (
                item.acceptance_id == manifest.acceptance_id and item.proposal_id == manifest.proposal_id
                and item.payload_ref == beat.payload_ref and item.payload_hash == beat.payload_hash
            ))
            if len(payloads) != 1 or payloads[0].text != beat.text:
                raise ValueError("expression authorization has no unique stored message payload")
            return ResolvedActionPayload(payload_ref=beat.payload_ref, payload_hash=beat.payload_hash,
                                         content_type=beat.content_type, body=payloads[0].text)
        descriptors = tuple(item for item in projection.expression_payload_descriptors if (
            item.acceptance_id == manifest.acceptance_id and item.proposal_id == manifest.proposal_id
            and item.payload_ref == beat.payload_ref and item.payload_hash == beat.payload_hash
            and item.content_type == beat.content_type and item.privacy_class == beat.privacy_class
            and item.payload_kind == beat.sidecar_kind
        ))
        if len(descriptors) != 1 or self._expression_payload_store is None:
            raise ValueError("expression sidecar descriptor or store is unavailable")
        record = self._expression_payload_store.read_exact(payload_ref=beat.payload_ref)
        if record is None or (
            record.payload_hash != beat.payload_hash
            or record.content_type != beat.content_type
            or record.privacy_class != beat.privacy_class
            or record.payload_kind != beat.sidecar_kind
            or expression_payload_hash(record.encoded_payload) != record.payload_hash
        ):
            raise ValueError("expression sidecar payload proof failed")
        return ResolvedActionPayload(payload_ref=record.payload_ref, payload_hash=record.payload_hash,
                                     content_type=record.content_type, body=record.encoded_payload)

    async def _project(self) -> LedgerProjection:
        if self._reader.blocks_event_loop:
            return await asyncio.to_thread(self._reader.project)
        return self._reader.project()


__all__ = ["LedgerAuthorizedPayloadReader"]
