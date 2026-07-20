"""Deterministic opener for the ``plan_disruption_appraisal`` inner-feeling lane.

When one of her plans is abandoned (a model chose to abandon it in the
lifecycle, an overdue plan was dropped, or an interruption replaced it), this
module leaves at most one recoverable work opportunity anchored to that
committed ``ActivityAbandoned`` event.  It never interprets the disruption
itself: what losing the plan means (regret, relief, indifference, nothing)
belongs to the model behind the trigger consumer, and every authoritative
value here is derived from committed ledger state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json

from pydantic import Field

from .batch_invariants import plan_disruption_appraisal_trigger_identity
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .schema_core import FrozenModel
from .schemas import TriggerProcess, WorldEvent


PLAN_DISRUPTION_APPRAISAL_TRIGGER_VERSION = "plan-disruption-appraisal-trigger.1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PlanDisruptionOpportunity(FrozenModel):
    """One deterministic, source-bound chance to appraise an abandoned plan."""

    trigger_id: str = Field(min_length=1)
    source_evidence_ref: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    activity_kind: str = Field(min_length=1)
    abandoned_at: datetime
    # True when the plan's window had not yet opened at the abandonment
    # moment: a broken future commitment, not an interrupted current one.
    was_future_plan: bool
    participant_refs: tuple[str, ...] = ()


def plan_disruption_opportunity(projection) -> PlanDisruptionOpportunity | None:
    """Derive the one open-able disruption anchor from committed projection state.

    Only the *latest* committed ``ActivityAbandoned`` event can open: like the
    silence lane's latest-reply anchor, an older abandonment that was never
    appraised before a newer one landed is stale news and never reopens.  The
    join to the abandoned plan is pure over the projection: the plan's
    authority origin names exactly its terminal ``ActivityAbandoned`` event.
    """

    anchor = None
    for ref in projection.committed_world_event_refs:
        if ref.event_type != "ActivityAbandoned":
            continue
        if anchor is None or ref.world_revision > anchor.world_revision:
            anchor = ref
    if anchor is None:
        return None
    trigger_id = plan_disruption_appraisal_trigger_identity(projection.world_id, anchor.event_id)
    # One trigger per abandonment anchor, in any state: a completed appraisal
    # of this same disruption must never reopen on replay or on later passes.
    if any(item.trigger_id == trigger_id for item in projection.trigger_processes):
        return None
    plan = next(
        (
            item
            for item in projection.plans
            if item.status == "abandoned"
            and item.authority_origin is not None
            and item.authority_origin.accepted_event_ref == anchor.event_id
        ),
        None,
    )
    # Legacy or externally replayed abandonments without plan authority stay
    # feeling-less rather than opening a trigger the consumer cannot ground.
    if plan is None:
        return None
    return PlanDisruptionOpportunity(
        trigger_id=trigger_id,
        source_evidence_ref=anchor.event_id,
        plan_id=plan.plan_id,
        activity_kind=plan.activity_kind,
        abandoned_at=anchor.logical_time,
        was_future_plan=(
            plan.scheduled_window is not None
            and plan.scheduled_window.opens_at > anchor.logical_time
        ),
        participant_refs=plan.participant_refs,
    )


class PlanDisruptionAppraisalTriggerOpener:
    """Commit at most one ``TriggerProcessOpened`` per abandoned-plan anchor."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        owner_id: str,
        source: str = "world-v2:plan-disruption-appraisal-trigger-opener",
    ) -> None:
        if not owner_id:
            raise ValueError("plan disruption opener needs an owner")
        self._ledger = ledger
        self._owner_id = owner_id
        self._source = source

    async def open_once(self) -> str | None:
        """Open the current disruption opportunity, returning its trigger id."""

        projection = await self._project()
        opportunity = plan_disruption_opportunity(projection)
        if opportunity is None:
            return None
        located = await self._lookup(opportunity.source_evidence_ref)
        if located is None or located[0].event_type != "ActivityAbandoned":
            raise ValueError("plan disruption anchor authority is unavailable")
        source_event = located[0]
        process = TriggerProcess(
            trigger_id=opportunity.trigger_id,
            trigger_ref=f"plan-disruption:{opportunity.source_evidence_ref}",
            process_kind="plan_disruption_appraisal",
            source_evidence_ref=opportunity.source_evidence_ref,
            state="open",
        )
        payload = {"process": process.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type="TriggerProcessOpened", world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("plan disruption trigger has no domain identity")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:plan-disruption-appraisal:opened:"
            + _digest({"world_id": self._ledger.world_id, "trigger_id": opportunity.trigger_id}),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessOpened",
            # The trigger opens at the durable head, which may be later than
            # the abandonment itself when the opener runs on a delayed pass.
            logical_time=projection.logical_time,
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
            commit_id="commit:plan-disruption-appraisal:opened:" + _digest(opportunity.trigger_id),
        )
        return opportunity.trigger_id

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

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


__all__ = [
    "PLAN_DISRUPTION_APPRAISAL_TRIGGER_VERSION",
    "PlanDisruptionAppraisalTriggerOpener",
    "PlanDisruptionOpportunity",
    "plan_disruption_opportunity",
]
