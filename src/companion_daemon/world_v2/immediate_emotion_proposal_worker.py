"""Accept one audited, combined Appraisal/Affect decision without another model call."""

from __future__ import annotations

import logging
import time
from typing import Literal

from .affect_acceptance_runtime import AffectAcceptanceRuntime
from .affect_proposal_compiler import AffectProposalCompiler
from .appraisal_proposal_worker import AppraisalProposalWorkResult, AppraisalProposalWorker
from .decision_proposal_authority import DecisionProposalAuthorityReader
from .errors import ConcurrencyConflict
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor


_LOG = logging.getLogger(__name__)


class ImmediateEmotionProposalWorkResult(FrozenModel):
    status: Literal["no_change", "appraisal_only", "accepted"]
    source_proposal_id: str
    appraisal: AppraisalProposalWorkResult
    affect_skip_reason: str | None = None
    requires_fresh_affect_consideration: bool = False
    typed_affect_proposal_id: str | None = None
    affect_compile_commit: CommitResult | None = None
    affect_acceptance_commit: CommitResult | None = None


class ImmediateEmotionConcurrencyConflict(ConcurrencyConflict):
    """A cursor race with enough phase identity for safe recovery.

    Appraisal contention means no Appraisal mutation was accepted and the
    role must reconsider against a fresh pinned World Context. Affect
    contention happens only after the source Appraisal is authoritative, so
    recovery must reuse the same audited decision rather than create a second
    Appraisal for one Observation.
    """

    def __init__(self, *, stage: Literal["appraisal", "affect"]) -> None:
        super().__init__(f"immediate emotion {stage} cursor became stale")
        self.stage = stage


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
        self._decision_reader = DecisionProposalAuthorityReader(ledger=ledger)
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
        current_cursor: ProjectionCursor | None = None,
    ) -> ImmediateEmotionProposalWorkResult:
        if world_id != self._ledger.world_id:
            raise ValueError("immediate emotion worker world mismatch")
        started = time.perf_counter()
        appraisal = self._accepted_appraisal(
            world_id=world_id,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal_id,
        )
        try:
            if appraisal is None:
                appraisal = (
                    self._appraisal.process_rebased(
                        world_id=world_id,
                        audit_cursor=audit_cursor,
                        current_cursor=current_cursor,
                        proposal_id=proposal_id,
                    )
                    if current_cursor is not None and current_cursor != audit_cursor
                    else self._appraisal.process(
                        world_id=world_id,
                        cursor=audit_cursor,
                        proposal_id=proposal_id,
                    )
                )
        except ConcurrencyConflict as exc:
            raise ImmediateEmotionConcurrencyConflict(stage="appraisal") from exc
        appraisal_ms = (time.perf_counter() - started) * 1000
        head = self._ledger.project()
        current_cursor = ProjectionCursor(
            world_revision=head.world_revision,
            deliberation_revision=head.deliberation_revision,
            ledger_sequence=head.ledger_sequence,
        )
        try:
            affect = self._affect_compiler.record_rebased(
                world_id=world_id,
                audit_cursor=audit_cursor,
                current_cursor=current_cursor,
                proposal_id=proposal_id,
            )
        except ConcurrencyConflict as exc:
            raise ImmediateEmotionConcurrencyConflict(stage="affect") from exc
        _LOG.warning(
            "immediate emotion worker phases proposal=%s appraisal_ms=%.1f affect_ms=%.1f affect_status=%s",
            proposal_id,
            appraisal_ms,
            (time.perf_counter() - started) * 1000 - appraisal_ms,
            affect.status,
        )
        if affect.status == "no_change":
            reconsider = (
                affect.skip_reason
                == "affect_proposal_compiler.target_lower_bound_changed_after_pin"
            )
            return ImmediateEmotionProposalWorkResult(
                status="appraisal_only" if appraisal.status == "accepted" else "no_change",
                source_proposal_id=proposal_id,
                appraisal=appraisal,
                affect_skip_reason=affect.skip_reason,
                requires_fresh_affect_consideration=reconsider,
            )
        if (
            affect.commit is None
            or affect.acceptance_cursor is None
            or affect.typed_proposal_id is None
        ):
            raise RuntimeError("rebased affect compiler returned an incomplete candidate")
        try:
            accepted = self._affect_acceptance.accept_runtime_owned(
                handle=self._affect_acceptance.pin_proposal(
                    cursor=affect.acceptance_cursor,
                    proposal_id=affect.typed_proposal_id,
                ),
                actor=self._actor,
                source=self._source,
            )
        except ConcurrencyConflict as exc:
            raise ImmediateEmotionConcurrencyConflict(stage="affect") from exc
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

    def _accepted_appraisal(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor | None,
        proposal_id: str,
    ) -> AppraisalProposalWorkResult | None:
        """Rejoin Appraisal after its atomic batch terminalized the source trigger."""

        authority = self._decision_reader.read(
            self._decision_reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        appraisal_changes = tuple(
            item
            for item in authority.proposal.proposed_changes
            if item.kind == "appraisal_transition"
        )
        if not appraisal_changes:
            return None
        if len(appraisal_changes) != 1:
            raise ValueError("immediate emotion Appraisal change is ambiguous")
        projection = (
            self._ledger.project_at(current_cursor)
            if current_cursor is not None
            else self._ledger.project()
        )
        accepted = tuple(
            item
            for item in projection.acceptance_decisions
            if item.status == "accepted"
            and item.manifest_version == "appraisal-acceptance.1"
            and item.accepted_change_id == appraisal_changes[0].change_id
            and item.acceptance_event_ref is not None
        )
        if not accepted:
            return None
        if len(accepted) != 1:
            raise ValueError("immediate emotion accepted Appraisal is ambiguous")
        accepted_appraisals = tuple(
            item
            for item in projection.appraisals
            if item.origin.change_id == appraisal_changes[0].change_id
        )
        if len(accepted_appraisals) != 1:
            raise ValueError("immediate emotion accepted Appraisal state is ambiguous")
        located = self._ledger.lookup_event_commit(
            accepted_appraisals[0].origin.accepted_event_ref
        )
        if located is None or located[0].event_type != "AppraisalAccepted":
            raise ValueError("immediate emotion accepted Appraisal event is missing")
        return AppraisalProposalWorkResult(
            status="accepted",
            source_proposal_id=proposal_id,
            typed_proposal_id=accepted[0].proposal_id,
            acceptance_commit=located[1],
        )


__all__ = [
    "ImmediateEmotionConcurrencyConflict",
    "ImmediateEmotionProposalWorker",
    "ImmediateEmotionProposalWorkResult",
]
