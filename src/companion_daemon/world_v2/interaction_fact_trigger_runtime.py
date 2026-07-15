"""Recovery-safe background acceptance for source-bound user Facts.

The visible reply lane records only an ``interaction_fact`` opportunity.  This
worker later rereads the exact committed message, asks a constrained model for
one candidate, records its immutable audit, and drives the existing Fact-v2
acceptance authority.  It never lets a model write a Fact or choose evidence.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from typing import Literal

from .event_identity import domain_idempotency_key
from .fact_accepted_contracts import rehydrate_fact_commit_intent_v2_json
from .fact_draft_adapter import FactObservationProposalAdapter
from .fact_proposal_audit_v2 import build_fact_commit_proposal_recorded_event_v2
from .fact_reducers import INSTALLED_FACT_PREDICATE_CARDINALITY
from .fact_v2_acceptance_envelope_authority import FactV2AcceptanceEnvelopeRequestV2
from .fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from .ledger import ObservationEventLocator
from .schema_core import FrozenModel
from .schemas import ClaimLease, Observation, ProjectionCursor, TriggerProcess, WorldEvent
from .sealed_fact_commit_adapter_v2 import FactCommitPolicyResolutionV2
from .sqlite_ledger import SQLiteWorldLedger


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FactTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal["no_change", "accepted"] | None = None


class InteractionFactTriggerRuntime:
    """Drain one claimed-or-open ``interaction_fact`` trigger."""

    def __init__(
        self,
        *,
        ledger: SQLiteWorldLedger,
        acceptance: FactV2AcceptanceRuntime,
        adapter: FactObservationProposalAdapter,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:interaction-fact-trigger-runtime",
    ) -> None:
        if type(ledger) is not SQLiteWorldLedger or acceptance.ledger is not ledger:
            raise ValueError("Fact trigger must use the acceptance runtime's exact SQLite ledger")
        if not owner_id or lease_seconds <= 0:
            raise ValueError("Fact trigger runtime needs an owner and positive lease")
        self._ledger = ledger
        self._acceptance = acceptance
        self._adapter = adapter
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> FactTriggerRunResult:
        projection = await self._project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "interaction_fact" and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return FactTriggerRunResult(trigger_id="", status="idle")
        source_event, observation = await self._source_observation(process, self._cursor(projection))
        active = await self._claim_or_reclaim(
            process=process, source_event=source_event, projection=projection
        )
        if active is None:
            return FactTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")

        before = await self._project()
        cursor = self._cursor(before)
        source_commit = await self._lookup_event_commit(source_event.event_id)
        if source_commit is None:
            raise ValueError("interaction fact source event is no longer available")
        source_world_revision = source_commit[1].world_revision
        try:
            proposal = await self._adapter.propose(
                observation=observation,
                observation_event=source_event,
                source_world_revision=source_world_revision,
                evaluated_world_revision=cursor.world_revision,
            )
        except ValueError:
            # A malformed or overreaching model draft has no durable meaning.
            # Mark the source opportunity consumed without producing either a
            # Fact or a scripted user-visible repair; future messages create
            # their own independently source-bound opportunities.
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:invalid-draft",
            )
            return FactTriggerRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )
        if proposal is None:
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:no-change",
            )
            return FactTriggerRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )

        audit_event = build_fact_commit_proposal_recorded_event_v2(
            proposal=proposal,
            world_id=self._ledger.world_id,
            logical_time=source_event.logical_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
        )
        audit_commit = await self._commit_at_cursor(
            (audit_event,),
            cursor=cursor,
            commit_id="commit:interaction-fact:audit:" + _digest(proposal.proposal_id),
        )
        audit_cursor = self._cursor_from_commit(audit_commit)
        handle = self._acceptance.pin_proposal(cursor=audit_cursor, proposal_id=proposal.proposal_id)
        intent = rehydrate_fact_commit_intent_v2_json(proposal.proposed_changes[0].payload.canonical_json)
        sources = self._acceptance.resolve_sources(
            cursor=audit_cursor,
            intent=intent,
            locators=(
                ObservationEventLocator.for_message(
                    world_id=self._ledger.world_id,
                    observation_id=observation.observation_id,
                    source=observation.source,
                    source_event_id=observation.source_event_id,
                ),
            ),
        )
        prepared = self._acceptance.prepare(
            proposal_handle=handle,
            change_id=proposal.proposed_changes[0].change_id,
            policy=FactCommitPolicyResolutionV2(
                cardinality=INSTALLED_FACT_PREDICATE_CARDINALITY[intent.predicate_code],
                policy_refs=("policy:fact-commit.2",),
            ),
        )
        identity = _digest(
            {"proposal_id": proposal.proposal_id, "trigger_id": active.trigger_id}
        )
        accepted = self._acceptance.accept(
            request=FactV2AcceptanceEnvelopeRequestV2(
                acceptance_id=f"acceptance:interaction-fact:{identity}",
                acceptance_event_id=f"event:interaction-fact:accepted:{identity}",
                acceptance_causation_id=self._acceptance.proposal_audit_event_ref(
                    proposal_handle=handle
                ),
                cursor=audit_cursor,
                world_id=self._ledger.world_id,
                logical_time=source_event.logical_time,
                created_at=source_event.created_at,
                actor=self._owner_id,
                source=self._source,
                trace_id=source_event.trace_id,
                correlation_id=source_event.correlation_id,
            ),
            proposal_handle=handle,
            prepared=prepared,
            sources=sources,
        )
        await self._complete(
            process=active,
            source_event=source_event,
            cursor=self._cursor_from_commit(accepted),
            outcome_ref=f"outcome:{active.trigger_id}:accepted:{proposal.proposal_id}",
        )
        return FactTriggerRunResult(
            trigger_id=active.trigger_id, status="processed", work_status="accepted"
        )

    async def _source_observation(
        self, process: TriggerProcess, cursor: ProjectionCursor
    ) -> tuple[WorldEvent, Observation]:
        observation_id = process.source_evidence_ref
        if observation_id is None:
            raise ValueError("interaction fact trigger has no source observation")
        projection = await self._project_at(cursor)
        reference = next(
            (item for item in projection.message_observations if item.observation_id == observation_id),
            None,
        )
        if reference is None or not reference.source or not reference.source_event_id:
            raise ValueError("interaction fact source observation is unavailable")
        locator = ObservationEventLocator.for_message(
            world_id=self._ledger.world_id,
            observation_id=observation_id,
            source=reference.source,
            source_event_id=reference.source_event_id,
        )
        events = await self._observation_events_at((locator,), cursor=cursor)
        if len(events) != 1 or events[0].event.event_type != "ObservationRecorded":
            raise ValueError("interaction fact source proof is incomplete")
        event = events[0].event
        observation = Observation.model_validate_json(event.payload_json)
        if (
            observation.observation_id != observation_id
            or event.world_id != self._ledger.world_id
            or process.trigger_ref != f"fact:{observation_id}"
        ):
            raise ValueError("interaction fact source proof does not bind its trigger")
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
        attempt_id = "attempt:interaction-fact:" + _digest(
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
            raise ValueError("interaction fact claim has no domain identity")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:interaction-fact:{event_type.lower()}:{_digest([process.trigger_id, attempt_id])}",
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
            commit_id=f"commit:interaction-fact:claim:{_digest([process.trigger_id, attempt_id])}",
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
            raise ValueError("interaction fact completion requires a claimed process")
        projection = await self._project_at(cursor)
        at = max(projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at)
        if at > process.claim_lease.expires_at:
            raise ValueError("interaction fact lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:interaction-fact:completed:"
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
            idempotency_key="world-v2:interaction-fact:completion:"
            + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
            payload=payload,
        )
        await self._commit_at_cursor(
            (event,),
            cursor=cursor,
            commit_id="commit:interaction-fact:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]),
        )

    async def _project(self):
        return await asyncio.to_thread(self._ledger.project)

    async def _project_at(self, cursor: ProjectionCursor):
        return await asyncio.to_thread(self._ledger.project_at, cursor)

    async def _observation_events_at(self, locators, *, cursor: ProjectionCursor):
        return await asyncio.to_thread(self._ledger.observation_events_at, locators, cursor=cursor)

    async def _lookup_event_commit(self, event_id: str):
        return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)

    async def _commit(self, events, *, world_revision: int, deliberation_revision: int, commit_id: str):
        return await asyncio.to_thread(
            self._ledger.commit,
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
            commit_id=commit_id,
        )

    async def _commit_at_cursor(self, events, *, cursor: ProjectionCursor, commit_id: str):
        return await asyncio.to_thread(
            self._ledger.commit_at_cursor, events, expected_cursor=cursor, commit_id=commit_id
        )

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


__all__ = ["FactTriggerRunResult", "InteractionFactTriggerRuntime"]
