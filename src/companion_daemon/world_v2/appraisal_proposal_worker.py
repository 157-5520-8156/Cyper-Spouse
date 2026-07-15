"""One background work unit for a persisted generic Appraisal decision.

Scheduling is intentionally outside this module.  A scheduler only needs to
persist or retry ``(world_id, cursor, proposal_id)`` and call ``process``; the
worker owns the compile/accept ordering and never has a reply-generation port.
"""

from __future__ import annotations

from typing import Literal

from .appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from .appraisal_proposal_compiler import AppraisalProposalCompiler
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor


class AppraisalProposalWorkResult(FrozenModel):
    status: Literal["no_change", "accepted"]
    source_proposal_id: str
    typed_proposal_id: str | None = None
    compile_commit: CommitResult | None = None
    acceptance_commit: CommitResult | None = None


class AppraisalProposalWorker:
    """Compile then accept one generic decision without exposing intermediate authority.

    A caller cannot substitute a typed proposal, event sequence, or timestamp:
    the compiler derives the candidate from an audited cursor and the existing
    acceptance runtime derives all accepted events from its opaque handle.
    """

    def __init__(
        self,
        *,
        compiler: AppraisalProposalCompiler,
        acceptance: AppraisalAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:appraisal-proposal-worker",
    ) -> None:
        if not actor:
            raise ValueError("appraisal proposal worker actor is required")
        if compiler.ledger is not acceptance.ledger:
            raise ValueError("compiler and acceptance must own the same ledger")
        self._compiler = compiler
        self._acceptance = acceptance
        self._actor = actor
        self._source = source

    def process(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> AppraisalProposalWorkResult:
        compiled = self._compiler.record(
            world_id=world_id,
            cursor=cursor,
            proposal_id=proposal_id,
        )
        if compiled.status == "no_change":
            return AppraisalProposalWorkResult(
                status="no_change",
                source_proposal_id=compiled.source_proposal_id,
            )
        if compiled.commit is None or compiled.typed_proposal_id is None:
            raise RuntimeError("appraisal compiler returned an incomplete candidate result")
        compiled_cursor = ProjectionCursor(
            world_revision=compiled.commit.world_revision,
            deliberation_revision=compiled.commit.deliberation_revision,
            ledger_sequence=compiled.commit.ledger_sequence,
        )
        accepted = self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(
                cursor=compiled_cursor,
                proposal_id=compiled.typed_proposal_id,
            ),
            actor=self._actor,
            source=self._source,
        )
        return AppraisalProposalWorkResult(
            status="accepted",
            source_proposal_id=compiled.source_proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )


__all__ = ["AppraisalProposalWorker", "AppraisalProposalWorkResult"]
