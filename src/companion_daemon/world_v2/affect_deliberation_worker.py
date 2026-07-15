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

    @property
    def ledger(self):
        """Ledger identity for composition-root validation."""

        return self._ledger

    async def process(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        appraisal_event: WorldEvent,
    ) -> AffectDeliberationWorkResult:
        if world_id != self._ledger.world_id:
            raise ValueError("affect deliberation world mismatch")
        projection = self._ledger.project_at(cursor)
        reusable = next(
            (
                audit
                for audit in projection.proposal_audits
                if audit.proposal_kind == "decision"
                and audit.trigger_ref == appraisal_event.event_id
                and audit.evaluated_world_revision == cursor.world_revision
            ),
            None,
        )
        audited = None
        source_audit_event_ref = None
        if reusable is None:
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
            source_proposal_id = audited.proposal_id
            compiled_cursor = audited.cursor
        else:
            # A prior attempt may have persisted the expensive generic audit and
            # crashed before compiling/accepting it.  Reuse that exact audit;
            # recovery must never turn an already-paid model call into another
            # model call merely because the process restarted.
            source_proposal_id = reusable.proposal_id
            source_audit_event_ref = reusable.event_ref
            compiled_cursor = cursor
        pending = next(
            (
                proposal
                for proposal in projection.affect_proposals
                if proposal.source_audit is not None
                and proposal.source_audit.proposal_event_ref == source_audit_event_ref
            ),
            None,
        )
        if pending is not None:
            accepted = self._accept_pending(cursor=cursor, proposal_id=pending.proposal_id)
            return AffectDeliberationWorkResult(
                status="accepted",
                trigger_event_ref=appraisal_event.event_id,
                source_proposal_id=source_proposal_id,
                typed_proposal_id=pending.proposal_id,
                deliberation_commit=audited.result if audited is not None else None,
                acceptance_commit=accepted,
            )
        try:
            compiled = self._compiler.record(
                world_id=world_id,
                cursor=compiled_cursor,
                proposal_id=source_proposal_id,
            )
        except ConcurrencyConflict:
            raise
        if compiled.status == "no_change":
            return AffectDeliberationWorkResult(
                status="no_change",
                trigger_event_ref=appraisal_event.event_id,
                source_proposal_id=compiled.source_proposal_id,
                deliberation_commit=audited.result if audited is not None else None,
            )
        if compiled.commit is None or compiled.typed_proposal_id is None:
            raise RuntimeError("affect compiler returned an incomplete candidate result")
        accepted = self._accept_pending(
            cursor=ProjectionCursor(
                world_revision=compiled.commit.world_revision,
                deliberation_revision=compiled.commit.deliberation_revision,
                ledger_sequence=compiled.commit.ledger_sequence,
            ),
            proposal_id=compiled.typed_proposal_id,
        )
        return AffectDeliberationWorkResult(
            status="accepted",
            trigger_event_ref=appraisal_event.event_id,
            source_proposal_id=compiled.source_proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
            deliberation_commit=audited.result if audited is not None else None,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )

    def _accept_pending(self, *, cursor: ProjectionCursor, proposal_id: str) -> CommitResult:
        return self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(cursor=cursor, proposal_id=proposal_id),
            actor=self._actor,
            source=self._source,
        )


__all__ = ["AffectDeliberationWorker", "AffectDeliberationWorkResult"]
