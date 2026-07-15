"""Pinned, source-bound deliberation after a delivered Media v2 artifact.

The lane deliberately receives a compact description of the *delivery claim*,
not a free-form image prompt or mutable provider result.  It may choose one
private InteractionBid or make a durable no-change decision; it cannot send a
message or alter the delivered artifact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from .context_capsule import (
    ContextCapsuleCompiler,
    InnerAdvisoryCandidate,
    InnerAdvisoryProjection,
)
from .context_resolver import query_from_projection
from .deliberation import Deliberation
from .errors import ConcurrencyConflict, IdempotencyConflict
from .ledger import LedgerPort
from .media_v2 import MediaDeliveryShared, MediaDeliverySharedPayload
from .proposal_audit import ProposalAuditCommit, ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import ProposalEvidenceRef
from .schemas import ProjectionCursor, WorldEvent


def _attempt_id(*, trigger_ref: str, cursor: ProjectionCursor) -> str:
    material = json.dumps(
        {
            "contract": "interaction-bid-deliberation-turn.1",
            "trigger_ref": trigger_ref,
            "cursor": cursor.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "attempt:interaction-bid:" + hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class InteractionBidDeliberationAudit:
    commit: ProposalAuditCommit


class InteractionBidDeliberationTurn:
    """Audit one bounded social-continuation decision for a delivery event."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        deliberation: Deliberation,
        companion_actor_ref: str,
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("interaction bid deliberation requires a companion actor")
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._recorder = ProposalAuditRecorder(ledger=ledger)
        self._companion_actor_ref = companion_actor_ref

    async def audit_delivery(
        self, *, delivery_event: WorldEvent, cursor: ProjectionCursor
    ) -> InteractionBidDeliberationAudit:
        delivery = await self._delivery_at_cursor(delivery_event=delivery_event, cursor=cursor)
        projection = await self._project_at(cursor)
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=delivery_event.event_id,
        )
        # The advisory names the immutable social fact available to this lane.
        # It intentionally excludes image bytes, prompt text, and all provider
        # metadata that was not committed by the delivery lifecycle.
        advisory = InnerAdvisoryProjection(
            advisory_id="advisory:media-delivery:" + delivery.delivery_id,
            kind="delivered_media_interaction",
            source_refs=(delivery_event.event_id,),
            candidate_refs=("delivery:" + delivery.delivery_id,),
            candidates=(
                InnerAdvisoryCandidate(
                    candidate_ref="delivery:" + delivery.delivery_id,
                    value=(
                        "A verified media share reached "
                        f"{delivery.recipient_ref}; decide whether a low-pressure private bid is warranted."
                    ),
                    weight_bp=10_000,
                    confidence_bp=10_000,
                ),
            ),
            confidence_bp=10_000,
            # The delivery itself is immutable, but this advisory is a
            # short-lived deliberation aid.  Anchor expiry to the pinned
            # logical head (which can be later than the provider receipt after
            # recovery), not wall-clock process time.
            expiry=(projection.logical_time or delivery_event.logical_time) + timedelta(days=1),
            producer_version="media-delivery-interaction.1",
        )
        try:
            capsule = await asyncio.to_thread(
                self._capsules.compile_for_deliberation_with_advisories,
                query,
                (advisory,),
            )
        except ValueError as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        stored = await self._lookup(delivery_event.event_id)
        assert stored is not None
        result = await self._deliberation.deliberate(
            capsule,
            attempt_id=_attempt_id(trigger_ref=delivery_event.event_id, cursor=cursor),
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=delivery_event.event_id,
                    evidence_kind="committed_world_event",
                    source_world_revision=stored[1].world_revision,
                    immutable_hash="sha256:" + delivery_event.payload_hash,
                ),
            ),
        )
        context = ProposalAuditContext(
            world_id=delivery_event.world_id,
            trigger_ref=delivery_event.event_id,
            logical_time=projection.logical_time or delivery_event.logical_time,
            created_at=delivery_event.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:interaction-bid-deliberation-turn",
            trace_id=delivery_event.trace_id,
            causation_id=delivery_event.event_id,
            correlation_id=delivery_event.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        try:
            commit = await self._record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict) as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        return InteractionBidDeliberationAudit(commit=commit)

    async def _delivery_at_cursor(
        self, *, delivery_event: WorldEvent, cursor: ProjectionCursor
    ) -> MediaDeliveryShared:
        if delivery_event.world_id != self._ledger.world_id or delivery_event.event_type != "MediaDeliveryShared":
            raise ValueError("interaction bid deliberation requires committed MediaDeliveryShared")
        stored = await self._lookup(delivery_event.event_id)
        if (
            stored is None
            or stored[0] != delivery_event
            or stored[1].world_revision > cursor.world_revision
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise ValueError("media delivery is not pinned committed authority")
        delivery = MediaDeliverySharedPayload.model_validate_json(delivery_event.payload_json).delivery
        projection = await self._project_at(cursor)
        if not any(item.delivery_id == delivery.delivery_id for item in projection.media_deliveries):
            raise ValueError("media delivery is absent from the pinned projection")
        return delivery

    async def _record(self, result, context):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._recorder.record, result, context)
        return self._recorder.record(result, context)

    async def _project(self):
        return await asyncio.to_thread(self._ledger.project) if self._ledger.blocks_event_loop else self._ledger.project()

    async def _project_at(self, cursor):
        return await asyncio.to_thread(self._ledger.project_at, cursor) if self._ledger.blocks_event_loop else self._ledger.project_at(cursor)

    async def _lookup(self, event_id):
        return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id) if self._ledger.blocks_event_loop else self._ledger.lookup_event_commit(event_id)

    async def _raise_if_stale(self, cursor, cause: Exception) -> None:
        current = await self._project()
        if (current.world_revision, current.deliberation_revision, current.ledger_sequence) != (
            cursor.world_revision,
            cursor.deliberation_revision,
            cursor.ledger_sequence,
        ):
            raise ConcurrencyConflict("interaction bid deliberation cursor became stale") from cause


__all__ = ["InteractionBidDeliberationAudit", "InteractionBidDeliberationTurn"]
