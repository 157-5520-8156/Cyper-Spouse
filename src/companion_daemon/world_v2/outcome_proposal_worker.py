"""One closed background work unit for an audited Outcome decision."""

from __future__ import annotations

from typing import Literal

from .outcome_acceptance_runtime import OutcomeAcceptanceRuntime
from .outcome_proposal_compiler import OutcomeProposalCompiler
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor


class OutcomeProposalWorkResult(FrozenModel):
    status: Literal["accepted"]
    source_proposal_id: str
    typed_proposal_id: str
    compile_commit: CommitResult | None = None
    acceptance_commit: CommitResult


class OutcomeProposalWorker:
    """Compile then atomically accept one source-bound Outcome proposal.

    The worker deliberately has no way to accept an arbitrary typed proposal:
    the compiler re-checks the claimed trigger and sidecar candidate first, and
    the acceptance lane only accepts its own opaque pinned handle.
    """

    def __init__(
        self,
        *,
        compiler: OutcomeProposalCompiler,
        acceptance: OutcomeAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:outcome-proposal-worker",
    ) -> None:
        if not actor:
            raise ValueError("outcome proposal worker actor is required")
        if compiler.ledger is not acceptance.ledger:
            raise ValueError("outcome compiler and acceptance must own the same ledger")
        self._compiler = compiler
        self._acceptance = acceptance
        self._actor = actor
        self._source = source

    @property
    def ledger(self):
        return self._compiler.ledger

    def process(self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str) -> OutcomeProposalWorkResult:
        """Finish one audit, reusing a crash-persisted typed proposal if present.

        Compilation and acceptance are intentionally separate commits.  A
        process crash in that gap must not make the generic audit stale merely
        because the compiler's *deliberation* commit advanced the cursor.  The
        persisted typed proposal is the durable hand-off: it is safe to pin at
        the newer cursor and atomically accept, without recalling the model or
        attempting to compile a second candidate.
        """
        projection = self.ledger.project_at(cursor)
        existing = next(
            (
                item
                for item in projection.outcome_proposals
                if item.decision_proposal_id == proposal_id
            ),
            None,
        )
        if existing is not None:
            accepted = self._accept_pending(cursor=cursor, proposal_id=existing.outcome_proposal_id)
            return OutcomeProposalWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                typed_proposal_id=existing.outcome_proposal_id,
                # There is no new compiler commit on this recovery attempt;
                # its prior durable event remains the authority.
                compile_commit=None,
                acceptance_commit=accepted,
            )
        compiled = self._compiler.record(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        compiled_cursor = ProjectionCursor(
            world_revision=compiled.commit.world_revision,
            deliberation_revision=compiled.commit.deliberation_revision,
            ledger_sequence=compiled.commit.ledger_sequence,
        )
        accepted = self._accept_pending(
            cursor=compiled_cursor, proposal_id=compiled.typed_proposal_id
        )
        return OutcomeProposalWorkResult(
            status="accepted",
            source_proposal_id=compiled.source_proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )

    def _accept_pending(self, *, cursor: ProjectionCursor, proposal_id: str) -> CommitResult:
        return self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(cursor=cursor, proposal_id=proposal_id),
            actor=self._actor,
            source=self._source,
        )


__all__ = ["OutcomeProposalWorker", "OutcomeProposalWorkResult"]
