"""Recovery-safe worker for source-bound ``plan_disruption_appraisal`` triggers.

The trigger anchor is the committed ``ActivityAbandoned`` event of one of her
own plans, so this lane reuses the settled-world audit discipline instead of
the user-message pinned turn: no user said anything when her Wednesday meetup
fell through.  The model reads the pinned Context capsule plus one
deterministic advisory naming exactly which plan was dropped (its kind,
window, whether it was still a future commitment, and who was involved) and
may conclude the disruption means nothing at all.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json

from .affect_trigger import affect_deliberation_trigger_events, affect_deliberation_trigger_id
from .relationship_trigger import relationship_deliberation_trigger_events, relationship_deliberation_trigger_id
from .appraisal_proposal_worker import AppraisalProposalWorker
from .context_capsule import (
    ContextCapsuleCompiler,
    InnerAdvisoryCandidate,
    InnerAdvisoryProjection,
)
from .context_resolver import query_from_projection
from .deliberation import Deliberation
from .errors import ConcurrencyConflict, IdempotencyConflict
from .event_identity import domain_idempotency_key
from .interaction_appraisal_trigger_runtime import AppraisalTriggerRunResult
from .ledger import LedgerPort
from .proposal_audit import ProposalAuditCommit, ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import ProposalEvidenceRef
from .schemas import ClaimLease, PlanStateProjection, ProjectionCursor, TriggerProcess, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _attempt_id(*, trigger_ref: str, cursor: ProjectionCursor) -> str:
    material = json.dumps(
        {
            "contract": "plan-disruption-appraisal-turn.1",
            "trigger_ref": trigger_ref,
            "cursor": cursor.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "attempt:plan-disruption-appraisal:" + hashlib.sha256(material).hexdigest()


def _disruption_summary(plan: PlanStateProjection, *, abandoned_event: WorldEvent) -> str:
    """Compress the dropped plan's committed facts into one bounded hint.

    Every value is copied from the pinned projection/event payload; nothing is
    inferred.  The bound is the advisory candidate's 256-character contract.
    """

    parts = [f"Abandoned plan: {plan.activity_kind}"]
    if plan.scheduled_window is not None:
        window = plan.scheduled_window
        parts.append(
            "window "
            + window.opens_at.strftime("%m-%d %H:%M")
            + "~"
            + window.closes_at.strftime("%m-%d %H:%M")
        )
        if window.opens_at > abandoned_event.logical_time:
            parts.append("future commitment, never started")
    if plan.participant_refs:
        parts.append("with " + ", ".join(plan.participant_refs[:3]))
    return ("; ".join(parts))[:256]


class PlanDisruptionAppraisalTurn:
    """Compile Context and record one non-authorizing disruption appraisal audit."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        deliberation: Deliberation,
        companion_actor_ref: str,
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("plan disruption appraisal requires a companion actor")
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._recorder = ProposalAuditRecorder(ledger=ledger)
        self._companion_actor_ref = companion_actor_ref

    async def audit_disruption(
        self, *, abandoned_event: WorldEvent, cursor: ProjectionCursor
    ) -> ProposalAuditCommit:
        if (
            abandoned_event.world_id != self._ledger.world_id
            or abandoned_event.event_type != "ActivityAbandoned"
        ):
            raise ValueError("plan disruption appraisal requires a committed plan abandonment")
        stored = await self._lookup(abandoned_event.event_id)
        if (
            stored is None
            or stored[0] != abandoned_event
            or stored[1].world_revision < 1
            or stored[1].world_revision > cursor.world_revision
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise ValueError("plan disruption source is not pinned committed authority")
        projection = await self._project_at(cursor)
        # The abandoned plan's terminal authority origin names exactly this
        # event, so the join stays pure over the pinned projection.
        plan = next(
            (
                item
                for item in projection.plans
                if item.status == "abandoned"
                and item.authority_origin is not None
                and item.authority_origin.accepted_event_ref == abandoned_event.event_id
            ),
            None,
        )
        if plan is None:
            raise ValueError("plan disruption source does not bind an abandoned plan")
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=abandoned_event.event_id,
        )
        # Terminal plans are absent from the situation's current-activity
        # slices, so the model would otherwise see an abandonment event id
        # with no meaning.  This advisory restores exactly the committed plan
        # facts (kind, window, future/current, participants) as a read-only
        # hint, never as a second plan authority.
        advisory = InnerAdvisoryProjection(
            advisory_id="advisory:plan-disruption:" + _digest(abandoned_event.event_id),
            kind="plan_disruption",
            source_refs=(abandoned_event.event_id,),
            candidate_refs=("plan-disruption:" + plan.plan_id,),
            candidates=(
                InnerAdvisoryCandidate(
                    candidate_ref="plan-disruption:" + plan.plan_id,
                    value=_disruption_summary(plan, abandoned_event=abandoned_event),
                    weight_bp=10_000,
                    confidence_bp=10_000,
                ),
            ),
            confidence_bp=10_000,
            # A short-lived deliberation aid anchored to the pinned durable
            # head, not wall-clock process time.
            expiry=(projection.logical_time or abandoned_event.logical_time) + timedelta(days=1),
            producer_version="plan-disruption-appraisal-turn.1",
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
        result = await self._deliberation.deliberate(
            capsule,
            attempt_id=_attempt_id(trigger_ref=abandoned_event.event_id, cursor=cursor),
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=abandoned_event.event_id,
                    # Her own plan transition is plain committed world
                    # authority; it is neither a settlement nor an observed
                    # user message.
                    evidence_kind="committed_world_event",
                    source_world_revision=next(
                        item.world_revision
                        for item in projection.committed_world_event_refs
                        if item.event_id == abandoned_event.event_id
                    ),
                    immutable_hash="sha256:" + abandoned_event.payload_hash,
                ),
            ),
        )
        context = ProposalAuditContext(
            world_id=abandoned_event.world_id,
            trigger_ref=abandoned_event.event_id,
            logical_time=projection.logical_time or abandoned_event.logical_time,
            created_at=abandoned_event.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:plan-disruption-appraisal-turn",
            trace_id=abandoned_event.trace_id,
            causation_id=abandoned_event.event_id,
            correlation_id=abandoned_event.correlation_id,
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
            raise ConcurrencyConflict("plan disruption appraisal cursor became stale") from cause


class PlanDisruptionAppraisalTriggerRuntime:
    """Consume one plan-disruption trigger without fabricating a message observation."""

    def __init__(
        self,
        *,
        ledger,
        turn: PlanDisruptionAppraisalTurn,
        worker: AppraisalProposalWorker,
        owner_id: str,
        affect_owner_id: str | None = None,
        relationship_owner_id: str | None = None,
        lease_seconds: int = 120,
        source: str = "world-v2:plan-disruption-appraisal-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("plan disruption trigger runtime needs owner and positive lease")
        if worker.ledger is not ledger:
            raise ValueError("plan disruption worker must own the exact ledger")
        if affect_owner_id is not None and not affect_owner_id:
            raise ValueError("affect owner must not be empty")
        if relationship_owner_id is not None and not relationship_owner_id:
            raise ValueError("relationship owner must not be empty")
        self._ledger = ledger
        self._turn = turn
        self._worker = worker
        self._owner_id = owner_id
        self._affect_owner_id = affect_owner_id
        self._relationship_owner_id = relationship_owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> AppraisalTriggerRunResult:
        projection = await self._project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "plan_disruption_appraisal" and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return AppraisalTriggerRunResult(trigger_id="", status="idle")
        abandonment = await self._abandonment(process, self._cursor(projection))
        active = await self._claim_or_reclaim(
            process=process, source_event=abandonment, projection=projection
        )
        if active is None:
            return AppraisalTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")

        current = await self._project()
        cursor = self._cursor(current)
        # A decision proposal is actionable only at the exact world revision
        # it evaluated.  An audit stranded by an interleaved commit stays as
        # inert history; this pass deliberates freshly at the current cursor
        # instead of feeding the authority reader a proposal it must reject.
        audit = next(
            (
                item
                for item in current.proposal_audits
                if item.proposal_kind == "decision"
                and item.trigger_ref == abandonment.event_id
                and item.evaluated_world_revision == cursor.world_revision
            ),
            None,
        )
        if audit is None:
            audited = await self._turn.audit_disruption(
                abandoned_event=abandonment, cursor=cursor
            )
            if audited.proposal_id is None:
                await self._complete(
                    process=active,
                    source_event=abandonment,
                    cursor=audited.cursor,
                    outcome_ref=f"outcome:{active.trigger_id}:no-proposal",
                )
                return AppraisalTriggerRunResult(
                    trigger_id=active.trigger_id, status="processed", work_status="no_proposal"
                )
            audit = next(
                (
                    item
                    for item in (await self._project()).proposal_audits
                    if item.proposal_id == audited.proposal_id and item.proposal_kind == "decision"
                ),
                None,
            )
            if audit is None:
                raise RuntimeError("plan disruption audit was not durably recorded")
            work_cursor = audited.cursor
        else:
            stored = await self._lookup(audit.event_ref)
            if stored is None:
                raise RuntimeError("plan disruption audit event is unavailable")
            work_cursor = self._cursor_from_commit(stored[1])
            if work_cursor != cursor:
                # Mirror the silence lane's recovery: unrelated commits between
                # the durable audit and this pass must not wedge the world.
                # The compile re-pins at the current head and its own commit
                # CAS still rejects genuinely lost races.
                work_cursor = cursor

        if self._ledger.blocks_event_loop:
            work = await asyncio.to_thread(
                self._worker.process,
                world_id=self._ledger.world_id,
                cursor=work_cursor,
                proposal_id=audit.proposal_id,
            )
        else:
            work = self._worker.process(
                world_id=self._ledger.world_id,
                cursor=work_cursor,
                proposal_id=audit.proposal_id,
            )
        if work.status == "no_change":
            await self._complete(
                process=active,
                source_event=abandonment,
                cursor=self._cursor(await self._project()),
                outcome_ref=f"outcome:{active.trigger_id}:no-change",
            )
            return AppraisalTriggerRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )
        if work.acceptance_commit is None:
            raise RuntimeError("accepted plan disruption appraisal has no acceptance commit")
        await self._open_affect_trigger(acceptance_event_ids=work.acceptance_commit.event_ids)
        return AppraisalTriggerRunResult(
            trigger_id=active.trigger_id, status="processed", work_status="accepted"
        )

    async def _abandonment(self, process: TriggerProcess, cursor: ProjectionCursor) -> WorldEvent:
        if process.source_evidence_ref is None:
            raise ValueError("plan disruption trigger has no abandonment source")
        stored = await self._lookup(process.source_evidence_ref)
        if stored is None or stored[0].event_type != "ActivityAbandoned":
            raise ValueError("plan disruption abandonment authority is unavailable")
        event, commit = stored
        if (
            commit.world_revision > cursor.world_revision
            or process.trigger_ref != f"plan-disruption:{process.source_evidence_ref}"
        ):
            raise ValueError("plan disruption trigger does not bind its abandonment")
        return event

    async def _claim_or_reclaim(self, *, process, source_event, projection):
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:plan-disruption-appraisal:" + _digest(
            {"trigger_id": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": ClaimLease(
                    owner_id=self._owner_id,
                    attempt_id=attempt_id,
                    acquired_at=at,
                    expires_at=at + timedelta(seconds=self._lease_seconds),
                ),
                "attempt_ids": (*process.attempt_ids, attempt_id),
            }
        )
        event_type = "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        payload = {"process": claimed.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("plan disruption claim identity is missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:plan-disruption-appraisal:trigger:"
            + event_type.lower()
            + ":"
            + _digest([process.trigger_id, attempt_id]),
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        await self._commit(
            (event,),
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id="commit:plan-disruption-appraisal:claim:"
            + _digest([process.trigger_id, attempt_id]),
        )
        return claimed

    async def _complete(self, *, process, source_event, cursor, outcome_ref: str) -> None:
        if process.claim_lease is None:
            raise ValueError("plan disruption completion requires a claimed process")
        projection = await self._project_at(cursor)
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("plan disruption lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:plan-disruption-appraisal:trigger:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessCompleted",
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key="world-v2:plan-disruption-appraisal-trigger:completion:"
            + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
            payload=payload,
        )
        await self._commit_at_cursor(
            (event,),
            cursor=cursor,
            commit_id="commit:plan-disruption-appraisal:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]),
        )

    async def _open_affect_trigger(self, *, acceptance_event_ids: tuple[str, ...]) -> None:
        if self._affect_owner_id is None and self._relationship_owner_id is None:
            return
        appraisal_event = next(
            (
                located[0]
                for event_id in acceptance_event_ids
                if (located := self._ledger.lookup_event_commit(event_id)) is not None
                and located[0].event_type == "AppraisalAccepted"
            ),
            None,
        )
        if appraisal_event is None:
            raise RuntimeError("accepted plan disruption appraisal has no durable mutation event")
        projection = await self._project()
        events = []
        affect_id = affect_deliberation_trigger_id(
            world_id=self._ledger.world_id, appraisal_event_id=appraisal_event.event_id
        )
        if self._affect_owner_id is not None and not any(
            item.trigger_id == affect_id for item in projection.trigger_processes
        ):
            events.extend(
                affect_deliberation_trigger_events(
                    appraisal_event=appraisal_event, owner_id=self._affect_owner_id
                )
            )
        relationship_id = relationship_deliberation_trigger_id(
            world_id=self._ledger.world_id, appraisal_event_id=appraisal_event.event_id
        )
        if self._relationship_owner_id is not None and not any(
            item.trigger_id == relationship_id for item in projection.trigger_processes
        ):
            events.extend(
                relationship_deliberation_trigger_events(
                    appraisal_event=appraisal_event, owner_id=self._relationship_owner_id
                )
            )
        if not events:
            return
        await self._commit(
            tuple(events),
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id=f"commit:plan-disruption-appraisal:open-downstream:{affect_id}:{relationship_id}",
        )

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

    async def _commit(self, events, *, world_revision, deliberation_revision, commit_id):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit,
                events,
                expected_world_revision=world_revision,
                expected_deliberation_revision=deliberation_revision,
                commit_id=commit_id,
            )
        return self._ledger.commit(
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
            commit_id=commit_id,
        )

    async def _commit_at_cursor(self, events, *, cursor, commit_id):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit_at_cursor, events, expected_cursor=cursor, commit_id=commit_id
            )
        return self._ledger.commit_at_cursor(events, expected_cursor=cursor, commit_id=commit_id)

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )

    @staticmethod
    def _cursor_from_commit(commit) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=commit.world_revision,
            deliberation_revision=commit.deliberation_revision,
            ledger_sequence=commit.ledger_sequence,
        )


__all__ = ["PlanDisruptionAppraisalTriggerRuntime", "PlanDisruptionAppraisalTurn"]
