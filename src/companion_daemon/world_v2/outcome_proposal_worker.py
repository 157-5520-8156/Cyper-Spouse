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
    compile_commit: CommitResult
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
        compiled = self._compiler.record(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        compiled_cursor = ProjectionCursor(
            world_revision=compiled.commit.world_revision,
            deliberation_revision=compiled.commit.deliberation_revision,
            ledger_sequence=compiled.commit.ledger_sequence,
        )
        accepted = self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(
                cursor=compiled_cursor, proposal_id=compiled.typed_proposal_id
            ),
            actor=self._actor,
            source=self._source,
        )
        return OutcomeProposalWorkResult(
            status="accepted",
            source_proposal_id=compiled.source_proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )


__all__ = ["OutcomeProposalWorker", "OutcomeProposalWorkResult"]
