"""Production source authority and Context readers for external evidence.

Character-authored attention belongs to ``CharacterInterior``.  This module
only freezes trusted Context slices and proves which external channels the
character may inspect; it never calls a model or authors an attention choice.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Protocol

from ..context_capsule import ContextCapsule, ContextCapsuleCompiler
from ..context_resolver import query_from_projection
from ..ledger import LedgerPort
from ..schemas import LedgerProjection, ProjectionCursor
from .contracts import (
    CharacterAttentionContext,
    LiveCharacterAttentionContext,
    PerceptionChannelProof,
    SourceBoundAttentionContextItem,
)


_CONTEXT_SLICE_GROUPS = {
    "situation": ("current_situation", "world_life"),
    "relevant_context": (
        "relationship_slice",
        "appraisals",
        "open_threads",
        "recent_experiences",
    ),
}

_REGISTRY_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
PUBLIC_INFORMATION_CAPABILITY_ID_PREFIX = "capability:world-v2:public-information-read:"


def public_information_capability_id(registry_content_hash: str) -> str:
    """Bind source-list authority to the registry's complete semantic hash."""

    if not _REGISTRY_HASH.fullmatch(registry_content_hash):
        raise ValueError("public information capability requires a registry sha256")
    return PUBLIC_INFORMATION_CAPABILITY_ID_PREFIX + registry_content_hash.removeprefix("sha256:")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class LiveAttentionChannelPort(Protocol):
    """Read-only authorization seam; a source registry may implement it later."""

    async def available_channels(
        self,
        *,
        world_id: str,
        actor_ref: str,
        cursor: ProjectionCursor,
        capsule: ContextCapsule,
        observed_at: datetime,
    ) -> tuple[PerceptionChannelProof, ...]: ...


class StaticLiveAttentionChannelPort:
    """Immutable candidate adapter for tests and prevalidated operator wiring.

    The production Context port still binds every returned evidence ref to the
    exact pinned World projection; this adapter cannot mint authority merely
    by returning a string.
    """

    def __init__(self, channels: tuple[PerceptionChannelProof, ...]) -> None:
        self._channels = tuple(
            PerceptionChannelProof.model_validate(item.model_dump(mode="python"), strict=True)
            for item in channels
        )

    async def available_channels(
        self,
        *,
        world_id: str,
        actor_ref: str,
        cursor: ProjectionCursor,
        capsule: ContextCapsule,
        observed_at: datetime,
    ) -> tuple[PerceptionChannelProof, ...]:
        del world_id, actor_ref, cursor, capsule
        del observed_at
        return self._channels


class LedgerPublicInformationChannelPort:
    """Resolve one public-information channel from an enforcement capability.

    The capability authorizes read-only public web access; the immutable
    deployment registry independently narrows that broad permission to the
    exact source ids exposed by this channel.  Neither side proves that the
    character noticed or believed any item.
    """

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        accessible_source_ids: tuple[str, ...],
        registry_content_hash: str,
    ) -> None:
        source_ids = tuple(sorted(set(accessible_source_ids)))
        if not source_ids or len(source_ids) != len(accessible_source_ids):
            raise ValueError("public information channel source ids must be nonempty and unique")
        self._ledger = ledger
        self._accessible_source_ids = source_ids
        self._registry_content_hash = registry_content_hash
        self._capability_id = public_information_capability_id(registry_content_hash)

    def authority_is_available(self, *, actor_ref: str) -> bool:
        projection = self._ledger.project()
        if projection.logical_time is None:
            return False
        return (
            self._active_grant(
                projection=projection,
                actor_ref=actor_ref,
                logical_time=projection.logical_time,
            )
            is not None
        )

    async def available_channels(
        self,
        *,
        world_id: str,
        actor_ref: str,
        cursor: ProjectionCursor,
        capsule: ContextCapsule,
        observed_at: datetime,
    ) -> tuple[PerceptionChannelProof, ...]:
        del capsule
        if world_id != self._ledger.world_id:
            raise ValueError("public information channel requested another world")
        projection = self._ledger.project()
        current = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        if current != cursor:
            raise ValueError("public information authority changed after Context was pinned")
        grant = self._active_grant(
            projection=projection,
            actor_ref=actor_ref,
            logical_time=projection.logical_time,
        )
        if grant is None:
            return ()
        values = grant.values
        return (
            PerceptionChannelProof(
                channel_ref=(
                    "channel:public-information:"
                    + self._registry_content_hash.removeprefix("sha256:")
                ),
                channel_kind="public_information_feed",
                evidence_refs=(grant.origin.event_ref,),
                accessible_source_ids=self._accessible_source_ids,
                valid_until=values.expires_at or datetime.max.replace(tzinfo=UTC),
            ),
        )

    def _active_grant(
        self, *, projection: object, actor_ref: str, logical_time: datetime
    ) -> object | None:
        matching = tuple(
            grant
            for grant in projection.capability_grants  # type: ignore[attr-defined]
            if grant.grant_id == self._capability_id
        )
        if len(matching) > 1:
            raise ValueError("public information capability identity is ambiguous")
        if not matching:
            return None
        grant = matching[0]
        values = grant.values
        if (
            values.state != "active"
            or values.capability_kind != "public_information_read"
            or values.actor_ref != actor_ref
            or tuple(values.target_scope_refs) != ("channel:public_information",)
            or "constraint:read-only" not in values.constraint_refs
            or not grant.origin.enforcement_eligible
            or values.valid_from > logical_time
            or (values.expires_at is not None and values.expires_at <= logical_time)
        ):
            return None
        return grant


class CapsuleBackedLiveAttentionContextPort:
    """Freeze role-safe Context slices at one complete read-only ledger cursor.

    User dialogue, user Facts, MemoryCandidates, and private impressions are
    deliberately not accessed here.  In particular, this attention lane has
    no route to a user's inferred/current location unless a future explicit
    authorization seam adds a source-bound role-facing Context item.
    """

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        channel_port: LiveAttentionChannelPort,
    ) -> None:
        self._ledger = ledger
        self._capsule_compiler = capsule_compiler
        self._channel_port = channel_port

    async def freeze_attention_context(
        self,
        *,
        world_id: str,
        actor_ref: str,
        observed_at: datetime,
    ) -> LiveCharacterAttentionContext:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("live attention observation time must be timezone-aware")
        if world_id != self._ledger.world_id:
            raise ValueError("live attention Context requested another world")
        projection, capsule = await self._compile_at_current_cursor(
            actor_ref=actor_ref,
            observed_at=observed_at,
        )
        if projection.logical_time is None:
            raise ValueError("live attention requires established World logical time")
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        channels = await self._channel_port.available_channels(
            world_id=world_id,
            actor_ref=actor_ref,
            cursor=cursor,
            capsule=capsule,
            observed_at=observed_at,
        )
        if any(item.valid_until <= projection.logical_time for item in channels):
            raise ValueError("live attention Context contains an expired channel")
        committed_event_refs = {item.event_id for item in projection.committed_world_event_refs}
        for channel in channels:
            missing = tuple(ref for ref in channel.evidence_refs if ref not in committed_event_refs)
            if missing:
                raise ValueError(
                    "live attention channel evidence is absent from the pinned World cursor"
                )
        situation = self._items(capsule, "situation")
        relevant_context = self._items(capsule, "relevant_context")
        return LiveCharacterAttentionContext(
            world_id=world_id,
            actor_ref=actor_ref,
            pinned_world_cursor=cursor,
            world_logical_time=projection.logical_time,
            situation=situation,
            relevant_context=relevant_context,
            available_channels=channels,
        )

    async def _compile_at_current_cursor(
        self, *, actor_ref: str, observed_at: datetime
    ) -> tuple[LedgerProjection, ContextCapsule]:
        def compile_pinned() -> tuple[LedgerProjection, ContextCapsule]:
            projection = self._ledger.project()
            cursor = ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            )
            trigger_ref = "external-attention-context:" + _digest(
                {
                    "cursor": cursor.model_dump(mode="json"),
                    "actor_ref": actor_ref,
                    "observed_at": observed_at.isoformat(),
                }
            )
            capsule = self._capsule_compiler.compile(
                query_from_projection(
                    projection,
                    actor_ref=actor_ref,
                    trigger_ref=trigger_ref,
                )
            )
            return projection, capsule

        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(compile_pinned)
        return compile_pinned()

    @staticmethod
    def _items(capsule: ContextCapsule, group: str) -> tuple[SourceBoundAttentionContextItem, ...]:
        compiled: list[SourceBoundAttentionContextItem] = []
        for slice_name in _CONTEXT_SLICE_GROUPS[group]:
            capsule_slice = getattr(capsule, slice_name)
            for item in capsule_slice.items:
                refs = tuple(dict.fromkeys(binding.ref for binding in item.source_bindings))
                if not refs or any(len(ref) > 256 for ref in refs):
                    raise ValueError("live attention Context has an unusable source binding")
                compiled.append(
                    SourceBoundAttentionContextItem(
                        context_ref=f"context:{slice_name}:{item.item_ref}",
                        context_kind=slice_name,
                        text=item.payload_json,
                        source_refs=refs,
                    )
                )
        return tuple(compiled)


class CapsuleBackedShadowAttentionContextPort:
    """Shadow view of the same production Context without ledger authority."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        channel_port: LiveAttentionChannelPort,
    ) -> None:
        self._live_reader = CapsuleBackedLiveAttentionContextPort(
            ledger=ledger,
            capsule_compiler=capsule_compiler,
            channel_port=channel_port,
        )

    async def freeze_attention_context(
        self,
        *,
        world_id: str,
        actor_ref: str,
        observed_at: datetime,
    ) -> CharacterAttentionContext:
        live = await self._live_reader.freeze_attention_context(
            world_id=world_id,
            actor_ref=actor_ref,
            observed_at=observed_at,
        )
        return CharacterAttentionContext(
            world_id=live.world_id,
            actor_ref=live.actor_ref,
            pinned_world_cursor="projection-cursor:"
            + _canonical(live.pinned_world_cursor.model_dump(mode="json")),
            world_logical_time=live.world_logical_time,
            situation=live.situation,
            relevant_context=live.relevant_context,
            available_channels=live.available_channels,
        )


__all__ = [
    "CapsuleBackedLiveAttentionContextPort",
    "CapsuleBackedShadowAttentionContextPort",
    "LedgerPublicInformationChannelPort",
    "LiveAttentionChannelPort",
    "PUBLIC_INFORMATION_CAPABILITY_ID_PREFIX",
    "StaticLiveAttentionChannelPort",
    "public_information_capability_id",
]
