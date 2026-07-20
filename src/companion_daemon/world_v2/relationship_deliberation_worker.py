"""Source-bound worker for the signal-before-adjustment relationship stage."""

from __future__ import annotations

import json
from typing import Literal

from .errors import ConcurrencyConflict
from .pinned_turn import PinnedTurnCompiler
from .proposal_envelope import DecisionProposal, validate_proposal_envelope
from .relationship_acceptance_runtime import RelationshipAcceptanceRuntime
from .relationship_proposal_compiler import RelationshipProposalCompiler
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


class RelationshipDeliberationWorkResult(FrozenModel):
    status: Literal["no_proposal", "no_change", "accepted"]
    trigger_event_ref: str
    source_proposal_id: str | None = None
    typed_proposal_id: str | None = None
    deliberation_commit: CommitResult | None = None
    compile_commit: CommitResult | None = None
    acceptance_commit: CommitResult | None = None


class RelationshipDeliberationWorker:
    """Audit, compile and accept one signal; never adjusts relationship variables."""

    def __init__(
        self,
        *,
        ledger,
        pinned_turn: PinnedTurnCompiler,
        compiler: RelationshipProposalCompiler,
        acceptance: RelationshipAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:relationship-deliberation-worker",
    ) -> None:
        if not actor:
            raise ValueError("relationship deliberation worker actor is required")
        if compiler.ledger is not ledger or acceptance.ledger is not ledger:
            raise ValueError("relationship worker dependencies must own the same ledger")
        self._ledger = ledger
        self._pinned_turn = pinned_turn
        self._compiler = compiler
        self._acceptance = acceptance
        self._actor = actor
        self._source = source

    @property
    def ledger(self):
        return self._ledger

    async def process(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        appraisal_event: WorldEvent,
    ) -> RelationshipDeliberationWorkResult:
        if world_id != self._ledger.world_id:
            raise ValueError("relationship deliberation world mismatch")
        projection = self._ledger.project_at(cursor)
        reusable = self._reusable_audit(projection=projection, appraisal_event=appraisal_event)
        audited = None
        source_audit_event_ref = None
        if reusable is None:
            audited = await self._pinned_turn.audit_appraisal_accepted(
                appraisal_event=appraisal_event,
                cursor=cursor,
                attempt_namespace="relationship",
            )
            if audited.proposal_id is None:
                return RelationshipDeliberationWorkResult(
                    status="no_proposal",
                    trigger_event_ref=appraisal_event.event_id,
                    deliberation_commit=audited.result,
                )
            source_proposal_id = audited.proposal_id
            compiled_cursor = audited.cursor
        else:
            source_proposal_id = reusable.proposal_id
            source_audit_event_ref = reusable.event_ref
            compiled_cursor = cursor
        pending = next(
            (
                proposal
                for proposal in projection.relationship_proposals
                if proposal.source_audit is not None
                and proposal.source_audit.proposal_event_ref == source_audit_event_ref
            ),
            None,
        )
        if pending is not None:
            accepted = self._accept_pending(cursor=cursor, proposal_id=pending.proposal_id)
            return RelationshipDeliberationWorkResult(
                status="accepted",
                trigger_event_ref=appraisal_event.event_id,
                source_proposal_id=source_proposal_id,
                typed_proposal_id=pending.proposal_id,
                deliberation_commit=audited.result if audited is not None else None,
                acceptance_commit=accepted,
            )
        try:
            compiled = self._compiler.record(
                world_id=world_id, cursor=compiled_cursor, proposal_id=source_proposal_id
            )
        except ConcurrencyConflict:
            raise
        if compiled.status == "no_change":
            return RelationshipDeliberationWorkResult(
                status="no_change",
                trigger_event_ref=appraisal_event.event_id,
                source_proposal_id=compiled.source_proposal_id,
                deliberation_commit=audited.result if audited is not None else None,
            )
        if compiled.commit is None or compiled.typed_proposal_id is None:
            raise RuntimeError("relationship compiler returned an incomplete candidate result")
        accepted = self._accept_pending(
            cursor=ProjectionCursor(
                world_revision=compiled.commit.world_revision,
                deliberation_revision=compiled.commit.deliberation_revision,
                ledger_sequence=compiled.commit.ledger_sequence,
            ),
            proposal_id=compiled.typed_proposal_id,
        )
        return RelationshipDeliberationWorkResult(
            status="accepted",
            trigger_event_ref=appraisal_event.event_id,
            source_proposal_id=compiled.source_proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
            deliberation_commit=audited.result if audited is not None else None,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )

    @staticmethod
    def _reusable_audit(*, projection, appraisal_event: WorldEvent):
        for audit in projection.proposal_audits:
            if (
                audit.proposal_kind != "decision"
                or audit.trigger_ref != appraisal_event.event_id
                or audit.evaluated_world_revision != projection.world_revision
            ):
                continue
            try:
                proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(proposal, DecisionProposal) and any(
                change.kind == "relationship_signal" for change in proposal.proposed_changes
            ):
                return audit
        return None

    def _accept_pending(self, *, cursor: ProjectionCursor, proposal_id: str) -> CommitResult:
        return self._acceptance.accept_runtime_owned(
            handle=self._acceptance.pin_proposal(cursor=cursor, proposal_id=proposal_id),
            actor=self._actor,
            source=self._source,
        )


__all__ = ["RelationshipDeliberationWorker", "RelationshipDeliberationWorkResult"]
