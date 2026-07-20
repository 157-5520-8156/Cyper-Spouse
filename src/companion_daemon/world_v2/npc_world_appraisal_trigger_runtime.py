"""Recovery-safe worker for source-bound ``npc_world_appraisal`` triggers."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json

from .affect_trigger import affect_deliberation_trigger_events, affect_deliberation_trigger_id
from .relationship_trigger import relationship_deliberation_trigger_events, relationship_deliberation_trigger_id
from .appraisal_proposal_worker import AppraisalProposalWorker
from .event_identity import domain_idempotency_key
from .interaction_appraisal_trigger_runtime import AppraisalTriggerRunResult
from .schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent
from .settled_world_appraisal_turn import SettledWorldAppraisalTurn


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class NpcWorldAppraisalTriggerRuntime:
    """Consume one settlement trigger without fabricating a message observation."""

    def __init__(
        self,
        *,
        ledger,
        turn: SettledWorldAppraisalTurn,
        worker: AppraisalProposalWorker,
        owner_id: str,
        affect_owner_id: str | None = None,
        relationship_owner_id: str | None = None,
        lease_seconds: int = 120,
        source: str = "world-v2:npc-world-appraisal-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("NPC appraisal trigger runtime needs owner and positive lease")
        if worker.ledger is not ledger:
            raise ValueError("NPC appraisal worker must own the exact ledger")
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
                if item.process_kind == "npc_world_appraisal" and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return AppraisalTriggerRunResult(trigger_id="", status="idle")
        settlement = await self._settlement(process, self._cursor(projection))
        active = await self._claim_or_reclaim(
            process=process, source_event=settlement, projection=projection
        )
        if active is None:
            return AppraisalTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")

        current = await self._project()
        cursor = self._cursor(current)
        # A decision proposal is actionable only at the exact world revision
        # it evaluated (the Pinned Turn contract).  An audit left behind by a
        # crashed or interleaved pass is therefore reusable only when the
        # world has not moved since; otherwise it stays as inert history and
        # this pass deliberates freshly at the current cursor.  Reusing it
        # anyway wedged the scheduler: the authority reader rejects the stale
        # proposal on every retry while the failed pass also stops the clock.
        audit = next(
            (
                item
                for item in current.proposal_audits
                if item.proposal_kind == "decision"
                and item.trigger_ref == settlement.event_id
                and item.evaluated_world_revision == cursor.world_revision
            ),
            None,
        )
        if audit is None:
            audited = await self._turn.audit_settlement(settlement_event=settlement, cursor=cursor)
            if audited.proposal_id is None:
                await self._complete(
                    process=active,
                    source_event=settlement,
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
                raise RuntimeError("NPC world appraisal audit was not durably recorded")
            work_cursor = audited.cursor
        else:
            stored = await self._lookup(audit.event_ref)
            if stored is None:
                raise RuntimeError("NPC world appraisal audit event is unavailable")
            work_cursor = self._cursor_from_commit(stored[1])
            if work_cursor != cursor:
                # The audit and its settlement are immutable, but unrelated
                # commits (an outbound receipt, another background lane) can
                # land between the audit and this recovery pass.  Treating
                # that as a hard failure wedged the world permanently: the
                # failed pass prevented the clock from advancing, so the
                # cursor could never catch up.  Re-pin the compile at the
                # current head instead — the compiler re-projects there and
                # its own commit CAS still rejects genuinely lost races.
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
                source_event=settlement,
                cursor=self._cursor(await self._project()),
                outcome_ref=f"outcome:{active.trigger_id}:no-change",
            )
            return AppraisalTriggerRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )
        if work.acceptance_commit is None:
            raise RuntimeError("accepted NPC appraisal has no acceptance commit")
        await self._open_affect_trigger(acceptance_event_ids=work.acceptance_commit.event_ids)
        return AppraisalTriggerRunResult(
            trigger_id=active.trigger_id, status="processed", work_status="accepted"
        )

    async def _settlement(self, process: TriggerProcess, cursor: ProjectionCursor) -> WorldEvent:
        if process.source_evidence_ref is None:
            raise ValueError("NPC world appraisal trigger has no settlement source")
        stored = await self._lookup(process.source_evidence_ref)
        if stored is None or stored[0].event_type != "WorldOccurrenceSettled":
            raise ValueError("NPC world appraisal settlement authority is unavailable")
        event, commit = stored
        if commit.world_revision > cursor.world_revision or process.trigger_ref != process.trigger_id:
            raise ValueError("NPC world appraisal trigger does not bind its settlement")
        return event

    async def _claim_or_reclaim(self, *, process, source_event, projection):
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:npc-world-appraisal:" + _digest(
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
            raise ValueError("NPC appraisal claim identity is missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:npc-world-appraisal:trigger:"
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
            commit_id="commit:npc-world-appraisal:claim:" + _digest([process.trigger_id, attempt_id]),
        )
        return claimed

    async def _complete(self, *, process, source_event, cursor, outcome_ref: str) -> None:
        if process.claim_lease is None:
            raise ValueError("NPC appraisal completion requires a claimed process")
        projection = await self._project_at(cursor)
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("NPC appraisal lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:npc-world-appraisal:trigger:completed:"
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
            idempotency_key="world-v2:npc-world-appraisal-trigger:completion:"
            + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
            payload=payload,
        )
        await self._commit_at_cursor(
            (event,),
            cursor=cursor,
            commit_id="commit:npc-world-appraisal:completed:"
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
            raise RuntimeError("accepted NPC appraisal has no durable mutation event")
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
            commit_id=f"commit:npc-world-appraisal:open-downstream:{affect_id}:{relationship_id}",
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


__all__ = ["NpcWorldAppraisalTriggerRuntime"]
