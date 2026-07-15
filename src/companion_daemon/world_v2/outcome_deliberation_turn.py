"""Pinned, non-message deliberation over one observed active world outcome."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass

from .context_capsule import ContextCapsuleCompiler, InnerAdvisoryCandidate, InnerAdvisoryProjection
from .context_resolver import query_from_projection
from .deliberation import Deliberation
from .errors import ConcurrencyConflict, IdempotencyConflict
from .ledger import LedgerPort
from .outcome_candidate_reader import OutcomeCandidateReader
from .proposal_audit import ProposalAuditCommit, ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import ProposalEvidenceRef
from .schemas import OutcomeObservationProjection, ProjectionCursor, WorldEvent


def _attempt_id(*, trigger_ref: str, cursor: ProjectionCursor) -> str:
    material = json.dumps(
        {"contract": "outcome-deliberation-turn.1", "trigger_ref": trigger_ref,
         "cursor": cursor.model_dump(mode="json")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return "attempt:outcome-deliberation:" + hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class OutcomeDeliberationAudit:
    """Either one durable generic Decision audit or an explicit no-model disposition."""

    commit: ProposalAuditCommit | None
    disposition: str | None = None


class OutcomeDeliberationTurn:
    """Expose only verified candidate excerpts as a source-bound advisory matrix.

    Outcome observations are not messages.  Candidate prose is read from the
    immutable sidecar and added as an advisory overlay bound to the committed
    observation that names the active occurrence.  If no candidate can be read
    this lane returns an explicit fail-closed disposition and makes no model
    call, rather than asking a model to invent a result from opaque references.
    """

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        deliberation: Deliberation,
        candidate_reader: OutcomeCandidateReader,
        companion_actor_ref: str,
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("outcome deliberation requires a companion actor")
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._candidates = candidate_reader
        self._recorder = ProposalAuditRecorder(ledger=ledger)
        self._companion_actor_ref = companion_actor_ref

    async def audit_observation(
        self, *, observation_event: WorldEvent, cursor: ProjectionCursor
    ) -> OutcomeDeliberationAudit:
        if observation_event.world_id != self._ledger.world_id or observation_event.event_type != "OutcomeObservationRecorded":
            raise ValueError("outcome deliberation requires a committed outcome observation")
        stored = await self._lookup(observation_event.event_id)
        if stored is None or stored[0] != observation_event or stored[1].world_revision > cursor.world_revision or stored[1].ledger_sequence > cursor.ledger_sequence:
            raise ValueError("outcome observation is not pinned committed authority")
        observation = OutcomeObservationProjection.model_validate_json(
            json.dumps(observation_event.payload().get("observation"))
        )
        projection = await self._project_at(cursor)
        occurrence = next((item for item in projection.world_occurrences if item.occurrence_id == observation.occurrence_id), None)
        if occurrence is None or occurrence.status != "active" or observation.observation_id not in occurrence.observation_refs:
            raise ValueError("outcome observation no longer belongs to an active occurrence")
        readable = self._candidates.read(occurrence=occurrence, viewer_privacy_ceiling="private")
        if not readable.candidates:
            return OutcomeDeliberationAudit(commit=None, disposition="candidate_content_unavailable")
        advisory = InnerAdvisoryProjection(
            advisory_id="advisory:outcome-candidates:" + occurrence.occurrence_id,
            kind="outcome_candidate_matrix",
            source_refs=(observation_event.event_id,),
            candidate_refs=tuple(item.candidate_result_ref for item in readable.candidates),
            candidates=tuple(
                InnerAdvisoryCandidate(
                    candidate_ref=item.candidate_result_ref,
                    value=item.text[:256],
                    weight_bp=10_000,
                    confidence_bp=observation.confidence_bp,
                ) for item in readable.candidates
            ),
            confidence_bp=observation.confidence_bp,
            expiry=occurrence.time_window.closes_at,
            producer_version="outcome-candidate-sidecar.1",
        )
        query = query_from_projection(projection, actor_ref=self._companion_actor_ref, trigger_ref=observation_event.event_id)
        try:
            capsule = await asyncio.to_thread(
                self._capsules.compile_for_deliberation_with_advisories, query, (advisory,)
            )
        except ValueError as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        result = await self._deliberation.deliberate(
            capsule,
            attempt_id=_attempt_id(trigger_ref=observation_event.event_id, cursor=cursor),
            trigger_evidence=(ProposalEvidenceRef(
                ref_id=observation_event.event_id,
                evidence_kind="committed_world_event",
                source_world_revision=stored[1].world_revision,
                immutable_hash="sha256:" + observation_event.payload_hash,
            ),),
        )
        context = ProposalAuditContext(
            world_id=observation_event.world_id, trigger_ref=observation_event.event_id,
            logical_time=projection.logical_time or observation_event.logical_time,
            created_at=observation_event.created_at, actor=self._companion_actor_ref,
            source="world-runtime:outcome-deliberation-turn", trace_id=observation_event.trace_id,
            causation_id=observation_event.event_id, correlation_id=observation_event.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        try:
            commit = await self._record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict) as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        return OutcomeDeliberationAudit(commit=commit)

    async def _record(self, result, context):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._recorder.record, result, context)
        return self._recorder.record(result, context)

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _project_at(self, cursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _lookup(self, event_id):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _raise_if_stale(self, cursor, cause: Exception) -> None:
        current = await self._project()
        if (current.world_revision, current.deliberation_revision, current.ledger_sequence) != (cursor.world_revision, cursor.deliberation_revision, cursor.ledger_sequence):
            raise ConcurrencyConflict("outcome deliberation cursor became stale") from cause


__all__ = ["OutcomeDeliberationAudit", "OutcomeDeliberationTurn"]
