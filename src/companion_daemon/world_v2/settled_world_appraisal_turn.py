"""Pinned audit-only deliberation for a settled NPC/world occurrence.

This is intentionally separate from ``PinnedTurnCompiler``'s user-message
path.  A settlement is already immutable world authority; treating it as a
synthetic user message would invent an actor, text, and channel, and would
break the distinction between a life event and a conversation turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from .context_capsule import ContextCapsuleCompiler
from .context_resolver import query_from_projection
from .deliberation import Deliberation
from .errors import ConcurrencyConflict, IdempotencyConflict
from .ledger import LedgerPort
from .proposal_audit import ProposalAuditCommit, ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import ProposalEvidenceRef
from .schemas import ProjectionCursor, WorldEvent


def _attempt_id(*, trigger_ref: str, cursor: ProjectionCursor) -> str:
    material = json.dumps(
        {
            "contract": "settled-world-appraisal-turn.1",
            "trigger_ref": trigger_ref,
            "cursor": cursor.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "attempt:settled-world-appraisal:" + hashlib.sha256(material).hexdigest()


class SettledWorldAppraisalTurn:
    """Compile Context and record one non-authorizing settlement appraisal audit."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        deliberation: Deliberation,
        companion_actor_ref: str,
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("settled-world appraisal requires a companion actor")
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._recorder = ProposalAuditRecorder(ledger=ledger)
        self._companion_actor_ref = companion_actor_ref

    async def audit_settlement(
        self, *, settlement_event: WorldEvent, cursor: ProjectionCursor
    ) -> ProposalAuditCommit:
        if (
            settlement_event.world_id != self._ledger.world_id
            or settlement_event.event_type != "WorldOccurrenceSettled"
        ):
            raise ValueError("settled-world appraisal requires a committed occurrence settlement")
        stored = await self._lookup(settlement_event.event_id)
        if (
            stored is None
            or stored[0] != settlement_event
            or stored[1].world_revision < 1
            or stored[1].world_revision > cursor.world_revision
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise ValueError("settled-world appraisal source is not pinned committed authority")
        projection = await self._project_at(cursor)
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=settlement_event.event_id,
        )
        try:
            capsule = await asyncio.to_thread(self._capsules.compile_for_deliberation, query)
        except ValueError as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        result = await self._deliberation.deliberate(
            capsule,
            attempt_id=_attempt_id(trigger_ref=settlement_event.event_id, cursor=cursor),
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=settlement_event.event_id,
                    evidence_kind="settled_world_event",
                    # A batch cursor is terminal; the event's exact revision
                    # is supplied by the Context/trigger authority below.
                    source_world_revision=next(
                        item.world_revision
                        for item in projection.committed_world_event_refs
                        if item.event_id == settlement_event.event_id
                    ),
                    immutable_hash="sha256:" + settlement_event.payload_hash,
                ),
            ),
        )
        context = ProposalAuditContext(
            world_id=settlement_event.world_id,
            trigger_ref=settlement_event.event_id,
            logical_time=projection.logical_time or settlement_event.logical_time,
            created_at=settlement_event.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:settled-world-appraisal-turn",
            trace_id=settlement_event.trace_id,
            causation_id=settlement_event.event_id,
            correlation_id=settlement_event.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        try:
            if self._ledger.blocks_event_loop:
                return await asyncio.to_thread(self._recorder.record, result, context)
            return self._recorder.record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict) as exc:
            await self._raise_if_stale(cursor, exc)
            raise

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _project_at(self, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _lookup(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _raise_if_stale(self, cursor: ProjectionCursor, cause: Exception) -> None:
        current = await self._project()
        if (
            current.world_revision != cursor.world_revision
            or current.deliberation_revision != cursor.deliberation_revision
            or current.ledger_sequence != cursor.ledger_sequence
        ):
            raise ConcurrencyConflict("settled-world appraisal cursor became stale") from cause


__all__ = ["SettledWorldAppraisalTurn"]
