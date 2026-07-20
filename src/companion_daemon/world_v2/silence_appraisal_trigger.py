"""Deterministic opener for the ``silence_appraisal`` inner-feeling lane.

After the companion's visible reply is delivered and the user stays quiet,
this module leaves at most one recoverable work opportunity anchored to that
reply's committed execution receipt.  It never interprets the silence itself:
what the quiet means (missing them, unease, indifference, nothing) belongs to
the model behind the trigger consumer, and every authoritative value here is
derived from committed ledger state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json

from pydantic import Field

from .batch_invariants import silence_appraisal_trigger_identity
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .schema_core import FrozenModel
from .schemas import TriggerProcess, WorldEvent


SILENCE_APPRAISAL_TRIGGER_VERSION = "silence-appraisal-trigger.1"

# The silence anchor must be one of her own visible conversational messages.
# Media, tool, and perception receipts are provider plumbing, not something a
# person would sit with while waiting for an answer.
_VISIBLE_MESSAGE_ACTION_KINDS = frozenset({"reply", "followup", "proactive_message"})
_ANSWERABLE_RECEIPT_STATES = frozenset({"provider_accepted", "delivered"})


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class SilenceOpportunity(FrozenModel):
    """One deterministic, source-bound chance to appraise an unanswered reply."""

    trigger_id: str = Field(min_length=1)
    source_evidence_ref: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    action_kind: str = Field(min_length=1)
    anchored_at: datetime
    idle_seconds: int = Field(ge=0)


def silence_appraisal_opportunity(
    projection, *, idle_seconds_threshold: int
) -> SilenceOpportunity | None:
    """Derive the one open-able silence anchor from committed projection state.

    Returns ``None`` unless all of the following hold: the companion's latest
    visible message receipt is acknowledged or delivered, no newer user message
    observation exists, the durable clock has moved past the idle threshold,
    and no silence trigger was ever opened for this anchor.
    """

    if idle_seconds_threshold <= 0:
        return None
    logical_time = projection.logical_time
    if logical_time is None:
        return None
    receipt_refs = tuple(
        item
        for item in projection.committed_world_event_refs
        if item.event_type == "ExecutionReceiptRecorded"
    )
    # Each ``ExecutionReceiptRecorded`` reduction appends exactly one receipt,
    # so the committed refs of that type align positionally with the receipt
    # projection.  This join stays pure over the projection: no ledger lookup
    # or payload re-hash is needed to bind a receipt to its committed event.
    if len(receipt_refs) != len(projection.execution_receipts):
        raise ValueError("execution receipt projection does not align with committed refs")
    action_kinds = {item.action_id: item.kind for item in projection.actions}
    anchor = None
    for ref, receipt in zip(receipt_refs, projection.execution_receipts, strict=True):
        if (
            receipt.observed_state not in _ANSWERABLE_RECEIPT_STATES
            or action_kinds.get(receipt.action_id) not in _VISIBLE_MESSAGE_ACTION_KINDS
        ):
            continue
        if anchor is None or ref.world_revision > anchor[0].world_revision:
            anchor = (ref, receipt)
    if anchor is None:
        return None
    anchor_ref, anchor_receipt = anchor
    # The user answering (any newer message observation) closes the silence:
    # what follows is an ordinary interaction appraisal, never this lane.
    if any(
        item.world_revision > anchor_ref.world_revision
        for item in projection.message_observations
    ):
        return None
    idle_seconds = int((logical_time - anchor_ref.logical_time).total_seconds())
    if idle_seconds < idle_seconds_threshold:
        return None
    trigger_id = silence_appraisal_trigger_identity(projection.world_id, anchor_ref.event_id)
    # One trigger per silence anchor, in any state: a completed appraisal of
    # this same quiet stretch must never reopen on replay or on later passes.
    if any(item.trigger_id == trigger_id for item in projection.trigger_processes):
        return None
    return SilenceOpportunity(
        trigger_id=trigger_id,
        source_evidence_ref=anchor_ref.event_id,
        receipt_id=anchor_receipt.receipt_id,
        action_id=anchor_receipt.action_id,
        action_kind=action_kinds[anchor_receipt.action_id],
        anchored_at=anchor_ref.logical_time,
        idle_seconds=idle_seconds,
    )


class SilenceAppraisalTriggerOpener:
    """Commit at most one ``TriggerProcessOpened`` per eligible silence anchor."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        owner_id: str,
        idle_seconds_threshold: int,
        source: str = "world-v2:silence-appraisal-trigger-opener",
    ) -> None:
        if not owner_id or idle_seconds_threshold <= 0:
            raise ValueError("silence appraisal opener needs owner and positive idle threshold")
        self._ledger = ledger
        self._owner_id = owner_id
        self._idle_seconds_threshold = idle_seconds_threshold
        self._source = source

    async def open_once(self) -> str | None:
        """Open the current silence opportunity, returning its trigger id."""

        projection = await self._project()
        opportunity = silence_appraisal_opportunity(
            projection, idle_seconds_threshold=self._idle_seconds_threshold
        )
        if opportunity is None:
            return None
        located = await self._lookup(opportunity.source_evidence_ref)
        if located is None or located[0].event_type != "ExecutionReceiptRecorded":
            raise ValueError("silence appraisal anchor authority is unavailable")
        source_event = located[0]
        process = TriggerProcess(
            trigger_id=opportunity.trigger_id,
            trigger_ref=f"silence:{opportunity.source_evidence_ref}",
            process_kind="silence_appraisal",
            source_evidence_ref=opportunity.source_evidence_ref,
            state="open",
        )
        payload = {"process": process.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type="TriggerProcessOpened", world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("silence appraisal trigger has no domain identity")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:silence-appraisal:opened:"
            + _digest({"world_id": self._ledger.world_id, "trigger_id": opportunity.trigger_id}),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessOpened",
            # The trigger opens when the durable clock crossed the threshold,
            # not retroactively at the anchor's own delivery instant.
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
            commit_id="commit:silence-appraisal:opened:" + _digest(opportunity.trigger_id),
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
    "SILENCE_APPRAISAL_TRIGGER_VERSION",
    "SilenceAppraisalTriggerOpener",
    "SilenceOpportunity",
    "silence_appraisal_opportunity",
]
