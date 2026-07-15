"""Low-priority, source-bound Affect deliberation after Appraisal acceptance."""

from __future__ import annotations

from typing import Literal

from .affect_acceptance_runtime import AffectAcceptanceRuntime
from .affect_proposal_compiler import AffectProposalCompiler
from .errors import ConcurrencyConflict
from .pinned_turn import PinnedTurnCompiler
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


class AffectDeliberationWorkResult(FrozenModel):
    status: Literal["no_proposal", "no_change", "accepted"]
    trigger_event_ref: str
    source_proposal_id: str | None = None
    typed_proposal_id: str | None = None
    deliberation_commit: CommitResult | None = None
    compile_commit: CommitResult | None = None
    acceptance_commit: CommitResult | None = None


class AffectDeliberationWorker:
    """Run one fresh affect turn, then compile and accept it if it proposes one.

    The worker's small interface makes it suitable for an out-of-band queue:
    callers retain only the accepted Appraisal event and its exact cursor.  It
    intentionally does not own a platform adapter or reply action capability.
    """

    def __init__(
        self,
        *,
        ledger,
        pinned_turn: PinnedTurnCompiler,
        compiler: AffectProposalCompiler,
        acceptance: AffectAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:affect-deliberation-worker",
    ) -> None:
        if not actor:
            raise ValueError("affect deliberation worker actor is required")
        if compiler.ledger is not ledger or acceptance.ledger is not ledger:
            raise ValueError("affect worker dependencies must own the same ledger")
        self._ledger = ledger
        self._pinned_turn = pinned_turn
        self._compiler = compiler
        self._acceptance = acceptance
        self._actor = actor
        self._source = source

    async def process(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        appraisal_event: WorldEvent,
    ) -> AffectDeliberationWorkResult:
        if world_id != self._ledger.world_id:
            raise ValueError("affect deliberation world mismatch")
        audited = await self._pinned_turn.audit_appraisal_accepted(
            appraisal_event=appraisal_event,
            cursor=cursor,
        )
        if audited.proposal_id is None:
            return AffectDeliberationWorkResult(
                status="no_proposal",
                trigger_event_ref=appraisal_event.event_id,
                deliberation_commit=audited.result,
            )
        compiled_cursor = audited.cursor
        try:
            compiled = self._compiler.record(
                world_id=world_id,
                cursor=compiled_cursor,
                proposal_id=audited.proposal_id,
            )
        except ConcurrencyConflict:
            raise
        if compiled.status == "no_change":
            return AffectDeliberationWorkResult(
                status="no_change",
                trigger_event_ref=appraisal_event.event_id,
                source_proposal_id=compiled.source_proposal_id,
                deliberation_commit=audited.result,
            )
        if compiled.commit is None or compiled.typed_proposal_id is None:
            raise RuntimeError("affect compiler returned an incomplete candidate result")
        accepted = self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(
                cursor=ProjectionCursor(
                    world_revision=compiled.commit.world_revision,
                    deliberation_revision=compiled.commit.deliberation_revision,
                    ledger_sequence=compiled.commit.ledger_sequence,
                ),
                proposal_id=compiled.typed_proposal_id,
            ),
            actor=self._actor,
            source=self._source,
        )
        return AffectDeliberationWorkResult(
            status="accepted",
            trigger_event_ref=appraisal_event.event_id,
            source_proposal_id=compiled.source_proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
            deliberation_commit=audited.result,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )


__all__ = ["AffectDeliberationWorker", "AffectDeliberationWorkResult"]
