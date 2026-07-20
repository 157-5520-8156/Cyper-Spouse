"""Worker that turns one accepted signal into one accepted slow adjustment."""

from __future__ import annotations

import json
from typing import Literal

from .relationship_adjustment_acceptance_runtime import (
    RelationshipAdjustmentAcceptanceRuntime,
)
from .relationship_adjustment_compiler import RelationshipAdjustmentCompiler
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


class RelationshipAdjustmentWorkResult(FrozenModel):
    status: Literal["accepted", "no_change"]
    signal_event_ref: str
    typed_proposal_id: str | None = None
    compile_commit: CommitResult | None = None
    acceptance_commit: CommitResult | None = None


class RelationshipAdjustmentWorker:
    """Idempotent worker for the deterministic post-signal vertical."""

    def __init__(
        self,
        *,
        ledger,
        compiler: RelationshipAdjustmentCompiler,
        acceptance: RelationshipAdjustmentAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:relationship-adjustment-worker",
    ) -> None:
        if not actor:
            raise ValueError("relationship adjustment worker actor is required")
        if compiler.ledger is not ledger or acceptance.ledger is not ledger:
            raise ValueError("relationship adjustment worker dependencies must own the same ledger")
        self._ledger = ledger
        self._compiler = compiler
        self._acceptance = acceptance
        self._actor = actor
        self._source = source

    @property
    def ledger(self):
        return self._ledger

    async def process(
        self, *, world_id: str, cursor: ProjectionCursor, signal_event: WorldEvent
    ) -> RelationshipAdjustmentWorkResult:
        if world_id != self._ledger.world_id or signal_event.world_id != world_id:
            raise ValueError("relationship adjustment world mismatch")
        if signal_event.event_type != "RelationshipSignalAccepted":
            raise ValueError("relationship adjustment requires an accepted signal event")
        projection = self._ledger.project_at(cursor)
        signal = next(
            (
                item
                for item in projection.relationship_signals
                if item.origin.accepted_event_ref == signal_event.event_id
            ),
            None,
        )
        if signal is None:
            raise ValueError("relationship adjustment signal projection is unavailable")
        pending = self._pending_proposal(
            projection=projection, signal_id=signal.signal_id
        )
        if pending is not None:
            accepted = self._accept(cursor=cursor, proposal_id=pending.proposal_id)
            return RelationshipAdjustmentWorkResult(
                status="accepted",
                signal_event_ref=signal_event.event_id,
                typed_proposal_id=pending.proposal_id,
                acceptance_commit=accepted,
            )
        compiled = self._compiler.record(
            world_id=world_id, cursor=cursor, signal_id=signal.signal_id
        )
        if compiled.status == "no_change":
            return RelationshipAdjustmentWorkResult(
                status="no_change", signal_event_ref=signal_event.event_id
            )
        if compiled.commit is None or compiled.typed_proposal_id is None:
            raise RuntimeError("relationship adjustment compiler returned incomplete candidate")
        accepted = self._accept(
            cursor=ProjectionCursor(
                world_revision=compiled.commit.world_revision,
                deliberation_revision=compiled.commit.deliberation_revision,
                ledger_sequence=compiled.commit.ledger_sequence,
            ),
            proposal_id=compiled.typed_proposal_id,
        )
        return RelationshipAdjustmentWorkResult(
            status="accepted",
            signal_event_ref=signal_event.event_id,
            typed_proposal_id=compiled.typed_proposal_id,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )

    def _accept(self, *, cursor: ProjectionCursor, proposal_id: str) -> CommitResult:
        return self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(cursor=cursor, proposal_id=proposal_id),
            actor=self._actor,
            source=self._source,
        )

    @staticmethod
    def _pending_proposal(*, projection, signal_id: str):
        """Locate an exact typed adjustment, never a JSON substring match."""

        for proposal in projection.relationship_proposals:
            if proposal.transition_kind != "adjust":
                continue
            try:
                payload = json.loads(proposal.proposed_mutation.payload_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("signal_refs") == [signal_id]:
                return proposal
        return None


__all__ = ["RelationshipAdjustmentWorker", "RelationshipAdjustmentWorkResult"]
