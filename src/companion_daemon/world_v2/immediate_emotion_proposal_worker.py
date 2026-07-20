"""Accept one audited, combined Appraisal/Affect decision without another model call."""

from __future__ import annotations

import logging
import time
from typing import Literal

from .affect_acceptance_runtime import AffectAcceptanceRuntime
from .affect_proposal_compiler import AffectProposalCompiler
from .appraisal_proposal_worker import AppraisalProposalWorkResult, AppraisalProposalWorker
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor


_LOG = logging.getLogger(__name__)


class ImmediateEmotionProposalWorkResult(FrozenModel):
    status: Literal["no_change", "appraisal_only", "accepted"]
    source_proposal_id: str
    appraisal: AppraisalProposalWorkResult
    affect_skip_reason: str | None = None
    typed_affect_proposal_id: str | None = None
    affect_compile_commit: CommitResult | None = None
    affect_acceptance_commit: CommitResult | None = None


class ImmediateEmotionProposalWorker:
    """Rebase one proposal's Affect only after its Appraisal is authoritative.

    The worker has no model port. ``audit_cursor`` always identifies the one
    persisted DecisionProposal; Appraisal acceptance may advance World
    revision, after which the compiler records the Affect candidate against the
    new head while retaining the original proposal audit binding.
    """

    def __init__(
        self,
        *,
        appraisal_worker: AppraisalProposalWorker,
        affect_compiler: AffectProposalCompiler,
        affect_acceptance: AffectAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:immediate-emotion-proposal-worker",
    ) -> None:
        if not actor:
            raise ValueError("immediate emotion worker actor is required")
        ledger = appraisal_worker.ledger
        if affect_compiler.ledger is not ledger or affect_acceptance.ledger is not ledger:
            raise ValueError("immediate emotion worker dependencies must own the same ledger")
        self._ledger = ledger
        self._appraisal = appraisal_worker
        self._affect_compiler = affect_compiler
        self._affect_acceptance = affect_acceptance
        self._actor = actor
        self._source = source

    @property
    def ledger(self):
        return self._ledger

    def process(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        proposal_id: str,
    ) -> ImmediateEmotionProposalWorkResult:
        if world_id != self._ledger.world_id:
            raise ValueError("immediate emotion worker world mismatch")
        started = time.perf_counter()
        appraisal = self._appraisal.process(
            world_id=world_id,
            cursor=audit_cursor,
            proposal_id=proposal_id,
        )
        appraisal_ms = (time.perf_counter() - started) * 1000
        head = self._ledger.project()
        current_cursor = ProjectionCursor(
            world_revision=head.world_revision,
            deliberation_revision=head.deliberation_revision,
            ledger_sequence=head.ledger_sequence,
        )
        affect = self._affect_compiler.record_rebased(
            world_id=world_id,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal_id,
        )
        _LOG.warning(
            "immediate emotion worker phases proposal=%s appraisal_ms=%.1f affect_ms=%.1f affect_status=%s",
            proposal_id,
            appraisal_ms,
            (time.perf_counter() - started) * 1000 - appraisal_ms,
            affect.status,
        )
        if affect.status == "no_change":
            return ImmediateEmotionProposalWorkResult(
                status="appraisal_only" if appraisal.status == "accepted" else "no_change",
                source_proposal_id=proposal_id,
                appraisal=appraisal,
                affect_skip_reason=affect.skip_reason,
            )
        if affect.commit is None or affect.typed_proposal_id is None:
            raise RuntimeError("rebased affect compiler returned an incomplete candidate")
        affect_cursor = ProjectionCursor(
            world_revision=affect.commit.world_revision,
            deliberation_revision=affect.commit.deliberation_revision,
            ledger_sequence=affect.commit.ledger_sequence,
        )
        accepted = self._affect_acceptance.accept_runtime_owned(
            handle=self._affect_acceptance.pin_proposal(
                cursor=affect_cursor,
                proposal_id=affect.typed_proposal_id,
            ),
            actor=self._actor,
            source=self._source,
        )
        _LOG.warning(
            "immediate emotion worker complete proposal=%s total_ms=%.1f acceptance_events=%d",
            proposal_id,
            (time.perf_counter() - started) * 1000,
            len(accepted.event_ids),
        )
        return ImmediateEmotionProposalWorkResult(
            status="accepted",
            source_proposal_id=proposal_id,
            appraisal=appraisal,
            typed_affect_proposal_id=affect.typed_proposal_id,
            affect_compile_commit=affect.commit,
            affect_acceptance_commit=accepted,
        )


__all__ = ["ImmediateEmotionProposalWorker", "ImmediateEmotionProposalWorkResult"]
