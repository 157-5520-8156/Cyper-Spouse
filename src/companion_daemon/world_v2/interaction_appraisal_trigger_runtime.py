"""Recovery-safe background executor for interaction-appraisal triggers.

Visible reply generation only records an ``interaction_appraisal`` opportunity.
This runtime later consumes its proof-backed source observation, audits one
bounded decision, and asks the existing typed appraisal worker to accept it.
It never owns a reply Action or an unbounded ledger reader.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import logging
import time
from typing import Literal

from .affect_trigger import affect_deliberation_trigger_events, affect_deliberation_trigger_id
from .relationship_trigger import relationship_deliberation_trigger_events, relationship_deliberation_trigger_id
from .appraisal_proposal_worker import AppraisalProposalWorker
from .immediate_emotion_proposal_worker import ImmediateEmotionProposalWorker
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort, ObservationEventLocator
from .pinned_turn import PinnedTurnCompiler
from .schema_core import FrozenModel
from .schemas import ClaimLease, Observation, ProjectionCursor, TriggerProcess, WorldEvent


_LOG = logging.getLogger(__name__)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class AppraisalTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal[
        "no_proposal", "no_change", "accepted", "advisory_validation_rejected"
    ] | None = None


class InteractionAppraisalTriggerRuntime:
    """Drain one durable interaction appraisal without delaying a reply turn."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        pinned_turn: PinnedTurnCompiler,
        worker: AppraisalProposalWorker,
        owner_id: str,
        affect_owner_id: str | None = None,
        relationship_owner_id: str | None = None,
        immediate_emotion_worker: ImmediateEmotionProposalWorker | None = None,
        lease_seconds: int = 120,
        source: str = "world-v2:interaction-appraisal-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("interaction appraisal trigger runtime needs owner and positive lease")
        if worker.ledger is not ledger:
            raise ValueError("interaction appraisal worker must own the exact ledger")
        if affect_owner_id is not None and not affect_owner_id:
            raise ValueError("affect owner must not be empty")
        if relationship_owner_id is not None and not relationship_owner_id:
            raise ValueError("relationship owner must not be empty")
        if (
            immediate_emotion_worker is not None
            and immediate_emotion_worker.ledger is not ledger
        ):
            raise ValueError("immediate emotion worker must own the exact ledger")
        self._ledger = ledger
        self._pinned_turn = pinned_turn
        self._worker = worker
        self._owner_id = owner_id
        self._affect_owner_id = affect_owner_id
        self._relationship_owner_id = relationship_owner_id
        self._immediate_emotion_worker = immediate_emotion_worker
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> AppraisalTriggerRunResult:
        return await self._run_one(observation_id=None)

    async def run_observation(self, observation_id: str) -> AppraisalTriggerRunResult:
        """Process the exact new interaction before its visible reply is deliberated.

        The durable trigger remains the authority and recovery seam.  Selecting it by
        source observation prevents an older queued interaction from delaying or
        contaminating the current same-turn emotion pass.
        """

        if not observation_id:
            raise ValueError("same-turn appraisal requires an observation id")
        return await self._run_one(observation_id=observation_id)

    async def _run_one(self, *, observation_id: str | None) -> AppraisalTriggerRunResult:
        started = time.perf_counter()
        projection = await self._project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "interaction_appraisal" and item.state != "terminal"
                and (observation_id is None or item.source_evidence_ref == observation_id)
            ),
            None,
        )
        if process is None:
            return AppraisalTriggerRunResult(trigger_id="", status="idle")
        source_event, observation = await self._source_observation(process, self._cursor(projection))
        active = await self._claim_or_reclaim(
            process=process, source_event=source_event, projection=projection
        )
        if active is None:
            return AppraisalTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")

        current = await self._project()
        current_cursor = self._cursor(current)
        audit = self._existing_decision_audit(current, source_event=source_event)
        if audit is None:
            audited = await self._pinned_turn.audit_observation(
                observation=observation,
                observation_event=source_event,
                cursor=current_cursor,
            )
            _LOG.warning(
                "interaction appraisal phases trace=%s audit_ms=%.1f",
                source_event.trace_id,
                (time.perf_counter() - started) * 1000,
            )
            if audited.proposal_id is None:
                await self._complete(
                    process=active,
                    source_event=source_event,
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
                raise RuntimeError("interaction appraisal audit was not durably recorded")
            work_cursor = audited.cursor
        else:
            stored = await self._lookup_event_commit(audit.event_ref)
            if stored is None:
                raise RuntimeError("interaction appraisal audit event is unavailable")
            work_cursor = self._cursor_from_commit(stored[1])
            if work_cursor != current_cursor and self._immediate_emotion_worker is None:
                # A decision audit is cursor-bound.  Do not compile it against a
                # newer world snapshot; a later drain can create a fresh bounded
                # audit from the same immutable source message.
                raise RuntimeError("interaction appraisal audit cursor is stale")

        if self._immediate_emotion_worker is not None:
            operation = self._immediate_emotion_worker.process
            kwargs = {
                "world_id": self._ledger.world_id,
                "audit_cursor": work_cursor,
                "proposal_id": audit.proposal_id,
            }
        else:
            operation = self._worker.process
            kwargs = {
                "world_id": self._ledger.world_id,
                "cursor": work_cursor,
                "proposal_id": audit.proposal_id,
            }
        try:
            result = (
                await asyncio.to_thread(operation, **kwargs)
                if self._ledger.blocks_event_loop
                else operation(**kwargs)
            )
            _LOG.warning(
                "interaction appraisal phases trace=%s worker_ms=%.1f",
                source_event.trace_id,
                (time.perf_counter() - started) * 1000,
            )
        except ValueError as exc:
            # Appraisal/Affect are advisory state.  Their typed compiler and
            # Acceptance still fail closed (the accepted batch is atomic), but
            # a rejected advisory must not discard the independently audited
            # Expression from the same model result.  Close the durable trigger
            # with a stable failure fingerprint so recovery does not retry an
            # invalid candidate forever, then let the visible lane continue.
            failure_fingerprint = _digest(
                {"exception_type": type(exc).__name__, "message": str(exc)[:240]}
            )
            await self._record_advisory_rejection(
                proposal_id=audit.proposal_id,
                source_event_ref=audit.event_ref,
                source_event=source_event,
                cursor=self._cursor(await self._project()),
                failure_fingerprint=failure_fingerprint,
            )
            return AppraisalTriggerRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="advisory_validation_rejected",
            )
        work = result.appraisal if self._immediate_emotion_worker is not None else result
        if work.status == "no_change":
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=self._cursor(await self._project()),
                outcome_ref=f"outcome:{active.trigger_id}:no-change",
            )
            return AppraisalTriggerRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )
        if work.acceptance_commit is None:
            raise RuntimeError("accepted interaction appraisal has no acceptance commit")
        await self._open_affect_trigger(
            acceptance_event_ids=work.acceptance_commit.event_ids,
            include_affect=self._immediate_emotion_worker is None,
        )
        return AppraisalTriggerRunResult(
            trigger_id=active.trigger_id, status="processed", work_status="accepted"
        )

    async def _source_observation(
        self, process: TriggerProcess, cursor: ProjectionCursor
    ) -> tuple[WorldEvent, Observation]:
        observation_id = process.source_evidence_ref
        if observation_id is None:
            raise ValueError("interaction appraisal trigger has no source observation")
        projection = await self._project_at(cursor)
        reference = next(
            (item for item in projection.message_observations if item.observation_id == observation_id),
            None,
        )
        if reference is None or not reference.source or not reference.source_event_id:
            raise ValueError("interaction appraisal source observation is unavailable")
        locator = ObservationEventLocator.for_message(
            world_id=self._ledger.world_id,
            observation_id=observation_id,
            source=reference.source,
            source_event_id=reference.source_event_id,
        )
        events = await self._observation_events_at((locator,), cursor=cursor)
        if len(events) != 1 or events[0].event.event_type != "ObservationRecorded":
            raise ValueError("interaction appraisal source proof is incomplete")
        event = events[0].event
        observation = Observation.model_validate_json(event.payload_json)
        if (
            observation.observation_id != observation_id
            or event.world_id != self._ledger.world_id
            or process.trigger_ref != f"interaction:{observation_id}"
        ):
            raise ValueError("interaction appraisal source proof does not bind its trigger")
        return event, observation

    async def _claim_or_reclaim(
        self, *, process: TriggerProcess, source_event: WorldEvent, projection
    ) -> TriggerProcess | None:
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:interaction-appraisal:" + _digest(
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
            raise ValueError("interaction appraisal claim identity is missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:interaction-appraisal:trigger:{event_type.lower()}:{_digest([process.trigger_id, attempt_id])}",
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
            commit_id=f"commit:interaction-appraisal:claim:{_digest([process.trigger_id, attempt_id])}",
        )
        return claimed

    async def _complete(
        self,
        *,
        process: TriggerProcess,
        source_event: WorldEvent,
        cursor: ProjectionCursor,
        outcome_ref: str,
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("interaction appraisal completion requires a claimed process")
        projection = await self._project_at(cursor)
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("interaction appraisal lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        identity = "world-v2:interaction-appraisal-trigger:completion:" + _digest(
            [self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]
        )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:interaction-appraisal:trigger:completed:"
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
            idempotency_key=identity,
            payload=payload,
        )
        await self._commit_at_cursor(
            (event,),
            cursor=cursor,
            commit_id="commit:interaction-appraisal:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]),
        )

    async def _record_advisory_rejection(
        self,
        *,
        proposal_id: str,
        source_event_ref: str,
        source_event: WorldEvent,
        cursor: ProjectionCursor,
        failure_fingerprint: str,
    ) -> None:
        """Persist why advisory Acceptance produced no authoritative mutation."""

        payload = {
            "proposal_id": proposal_id,
            "source_event_ref": source_event_ref,
            "advisory_kind": "appraisal_affect",
            "stage": "immediate_emotion_acceptance",
            "reason_code": "advisory_validation_rejected",
            "failure_fingerprint": failure_fingerprint,
        }
        identity = domain_idempotency_key(
            event_type="AdvisoryAcceptanceRejected",
            world_id=self._ledger.world_id,
            payload=payload,
        )
        if identity is None:
            raise ValueError("advisory rejection audit identity is missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:advisory-acceptance-rejected:"
            + _digest([proposal_id, "immediate_emotion_acceptance", failure_fingerprint]),
            world_id=self._ledger.world_id,
            event_type="AdvisoryAcceptanceRejected",
            logical_time=source_event.logical_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event_ref,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        await self._commit_at_cursor(
            (event,),
            cursor=cursor,
            commit_id="commit:advisory-acceptance-rejected:"
            + _digest([proposal_id, failure_fingerprint]),
        )

    async def _open_affect_trigger(
        self, *, acceptance_event_ids: tuple[str, ...], include_affect: bool = True
    ) -> None:
        if (not include_affect or self._affect_owner_id is None) and self._relationship_owner_id is None:
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
            raise RuntimeError("accepted interaction appraisal has no durable mutation event")
        projection = await self._project()
        events = []
        affect_id = affect_deliberation_trigger_id(
            world_id=self._ledger.world_id, appraisal_event_id=appraisal_event.event_id
        )
        if include_affect and self._affect_owner_id is not None and not any(
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
            commit_id=f"commit:interaction-appraisal:open-downstream:{affect_id}:{relationship_id}",
        )

    @staticmethod
    def _existing_decision_audit(projection, *, source_event: WorldEvent):
        return next(
            (
                item
                for item in projection.proposal_audits
                if item.proposal_kind == "decision"
                and item.trigger_ref == source_event.event_id
                # The visible-expression draft intentionally shares the same
                # immutable source Observation.  It is not an appraisal audit
                # and may have been committed at an earlier cursor, so never
                # join it merely because both proposals are DecisionProposal.
                and (
                    item.proposal_id.startswith("proposal:appraisal-draft:")
                    or item.proposal_id.startswith("proposal:interaction-appraisal:")
                )
            ),
            None,
        )

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _project_at(self, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _lookup_event_commit(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _observation_events_at(self, locators, *, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.observation_events_at, locators, cursor=cursor)
        return self._ledger.observation_events_at(locators, cursor=cursor)

    async def _commit(self, events, *, world_revision: int, deliberation_revision: int, commit_id: str):
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

    async def _commit_at_cursor(self, events, *, cursor: ProjectionCursor, commit_id: str):
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


__all__ = ["AppraisalTriggerRunResult", "InteractionAppraisalTriggerRuntime"]
