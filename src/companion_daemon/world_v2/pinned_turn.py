"""Cursor-pinned Context → Deliberation → Proposal-Audit composition.

This is intentionally the first, non-authorizing WorldRuntime turn vertical.
It turns an already recorded Observation into a trusted Capsule and an audited
model result/proposal at one complete cursor.  Acceptance and Action remain
separate modules; this module never materializes an accepted world effect.
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
from .schemas import Observation, ProjectionCursor, WorldEvent


def _attempt_id(*, trigger_ref: str, cursor: ProjectionCursor) -> str:
    material = json.dumps(
        {
            "contract": "pinned-turn.1",
            "trigger_ref": trigger_ref,
            "cursor": cursor.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"attempt:pinned-turn:{hashlib.sha256(material).hexdigest()}"


class PinnedTurnCompiler:
    """Deep module for one cursor-consistent, audit-only Deliberation attempt."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        deliberation: Deliberation,
        companion_actor_ref: str,
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("Pinned turn companion actor is required")
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._recorder = ProposalAuditRecorder(ledger=ledger)
        self._companion_actor_ref = companion_actor_ref

    async def audit_observation(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
    ) -> ProposalAuditCommit:
        """Compile and audit only if the Observation's exact cursor is current.

        The audit is a deliberation-only commit.  Any world revision change
        between the read and write makes the attempt stale; callers must build
        a fresh turn rather than reusing its Capsule or proposal.
        """

        if observation.world_id != self._ledger.world_id or observation_event.world_id != observation.world_id:
            raise ValueError("Pinned turn observation belongs to another world")
        if observation_event.event_type != "ObservationRecorded":
            raise ValueError("Pinned turn requires an ObservationRecorded event")
        projection = await self._project_at(cursor)
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=observation.observation_id,
        )
        capsule = await self._compile_capsule(query)
        result = await self._deliberation.deliberate(
            capsule,
            attempt_id=_attempt_id(trigger_ref=observation.observation_id, cursor=cursor),
        )
        context = ProposalAuditContext(
            world_id=observation.world_id,
            trigger_ref=observation.observation_id,
            logical_time=projection.logical_time or observation.logical_time,
            created_at=observation.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:pinned-turn",
            trace_id=observation.trace_id,
            causation_id=observation_event.event_id,
            correlation_id=observation.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        try:
            return await self._record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict) as exc:
            current = await self._project()
            if (
                current.world_revision != cursor.world_revision
                or current.ledger_sequence != cursor.ledger_sequence
            ):
                raise ConcurrencyConflict("Pinned turn cursor became stale") from exc
            raise

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _project_at(self, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _compile_capsule(self, query):
        return await asyncio.to_thread(self._capsules.compile_for_deliberation, query)

    async def _record(
        self, result, context: ProposalAuditContext
    ) -> ProposalAuditCommit:
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._recorder.record, result, context)
        return self._recorder.record(result, context)


__all__ = ["PinnedTurnCompiler"]
