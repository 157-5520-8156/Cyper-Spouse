"""Provider-free recovery of delivered, role-authored relationship commitments.

The durable DecisionProposal is the only commitment author.  This worker waits
until the same proposal's expression has a terminal delivered receipt, then
invokes the deterministic compiler and the atomic commitment acceptance
vertical.  It never calls a model and never classifies message text.
"""

from __future__ import annotations

import json
from typing import Literal

from .audited_proposal_settlement import settle_terminal_audited_proposal
from .ledger import LedgerPort
from .proposal_envelope import DecisionProposal, validate_proposal_envelope
from .relationship_commitment_acceptance_runtime import (
    RelationshipCommitmentAcceptanceRuntime,
)
from .relationship_proposal_compiler import (
    RelationshipProposalCompiler,
    RelationshipProposalCompilerError,
)
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor
from .stale_proposal_settlement import settle_stale_typed_proposal


class RelationshipCommitmentWorkerError(ValueError):
    """Stable technical or authority failure at background settlement."""

    def __init__(self, code: str) -> None:
        self.code = f"relationship_commitment_worker.{code}"
        super().__init__(self.code)


class RelationshipCommitmentWorkResult(FrozenModel):
    status: Literal["accepted", "rejected", "stale"] = "accepted"
    source_proposal_id: str
    acceptance_commit: CommitResult
    typed_proposal_id: str | None = None
    compile_commit: CommitResult | None = None
    reason_code: str | None = None


class RelationshipCommitmentWorker:
    """Settle at most one delivered commitment from immutable ledger authority."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        compiler: RelationshipProposalCompiler,
        acceptance: RelationshipCommitmentAcceptanceRuntime,
        actor: str,
        source: str = "world-v2:relationship-commitment-worker",
    ) -> None:
        if not actor or not source:
            raise ValueError("relationship commitment worker needs actor and source")
        if compiler.ledger is not ledger or acceptance.ledger is not ledger:
            raise ValueError(
                "relationship commitment worker dependencies must own the same ledger"
            )
        self._ledger = ledger
        self._compiler = compiler
        self._acceptance = acceptance
        self._actor = actor
        self._source = source

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    async def drain_one(self) -> RelationshipCommitmentWorkResult | None:
        """Accept one exact delivered commitment, or remain idle."""

        projection = self._ledger.project()
        current_cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        terminal_source_proposals = {
            item.proposal_id for item in projection.acceptance_decisions
        }
        for audit in projection.proposal_audits:
            if audit.proposal_kind != "decision":
                continue
            try:
                proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RelationshipCommitmentWorkerError(
                    "source_proposal_invalid"
                ) from exc
            if not isinstance(proposal, DecisionProposal):
                continue
            if audit.proposal_id in terminal_source_proposals:
                continue
            changes = tuple(
                change
                for change in proposal.proposed_changes
                if change.kind == "relationship_commitment"
            )
            if not changes:
                continue
            if len(changes) != 1 or changes[0].transition != "commit":
                raise RelationshipCommitmentWorkerError(
                    "commitment_change_invalid"
                )
            located = self._ledger.lookup_event_commit(audit.event_ref)
            if (
                located is None
                or located[0].event_type != "ProposalRecorded"
                or located[0].payload_hash != audit.event_payload_hash
            ):
                raise RelationshipCommitmentWorkerError(
                    "source_proposal_event_missing"
                )
            audit_commit = located[1]
            audit_cursor = ProjectionCursor(
                world_revision=audit_commit.world_revision,
                deliberation_revision=audit_commit.deliberation_revision,
                ledger_sequence=audit_commit.ledger_sequence,
            )
            stale = self._stale_candidate(
                projection=projection,
                audit=audit,
                change=changes[0],
                current_cursor=current_cursor,
            )
            if stale is not None:
                candidate, proposal_event, proposal_commit = stale
                settled = settle_stale_typed_proposal(
                    ledger=self._ledger,
                    proposal_event=proposal_event,
                    proposal_id=candidate.proposal_id,
                    evaluated_world_revision=candidate.evaluated_world_revision,
                    current_cursor=current_cursor,
                    actor=self._actor,
                    source=self._source,
                )
                return RelationshipCommitmentWorkResult(
                    status="stale",
                    source_proposal_id=proposal.proposal_id,
                    typed_proposal_id=candidate.proposal_id,
                    compile_commit=proposal_commit,
                    acceptance_commit=settled,
                )
            if (
                self._compiler.accepted_commitment_descendant(
                    world_id=self._ledger.world_id,
                    audit_cursor=audit_cursor,
                    current_cursor=current_cursor,
                    proposal_id=proposal.proposal_id,
                )
                is not None
            ):
                continue
            if not self._has_terminal_delivered_expression(
                projection=projection,
                proposal_id=proposal.proposal_id,
            ):
                continue
            try:
                compiled = self._compiler.record_commitment_rebased(
                    world_id=self._ledger.world_id,
                    audit_cursor=audit_cursor,
                    current_cursor=current_cursor,
                    proposal_id=proposal.proposal_id,
                )
            except RelationshipProposalCompilerError as exc:
                if exc.code not in {
                    "relationship_proposal_compiler."
                    "commitment_stage_transition_not_installed",
                }:
                    raise
                terminal = settle_terminal_audited_proposal(
                    ledger=self._ledger,
                    audit=audit,
                    current_cursor=current_cursor,
                    reason_code=exc.code,
                    actor=self._actor,
                    source=self._source,
                )
                return RelationshipCommitmentWorkResult(
                    status=terminal.status,
                    source_proposal_id=proposal.proposal_id,
                    acceptance_commit=terminal.commit,
                    reason_code=terminal.reason_code,
                )
            if (
                compiled.status != "candidate_recorded"
                or compiled.typed_proposal_id is None
                or compiled.commit is None
                or compiled.acceptance_cursor is None
            ):
                raise RelationshipCommitmentWorkerError(
                    "compiled_candidate_incomplete"
                )
            accepted = self._acceptance.accept_runtime_owned(
                handle=self._acceptance.pin_proposal(
                    cursor=compiled.acceptance_cursor,
                    proposal_id=compiled.typed_proposal_id,
                ),
                actor=self._actor,
                source=self._source,
            )
            return RelationshipCommitmentWorkResult(
                source_proposal_id=proposal.proposal_id,
                typed_proposal_id=compiled.typed_proposal_id,
                compile_commit=compiled.commit,
                acceptance_commit=accepted,
            )
        return None

    def _stale_candidate(self, *, projection, audit, change, current_cursor):
        decided = {item.proposal_id for item in projection.acceptance_decisions}
        for candidate in projection.relationship_proposals:
            binding = candidate.source_audit
            if (
                candidate.proposal_id in decided
                or candidate.transition_kind != "commitment"
                or candidate.evaluated_world_revision
                >= current_cursor.world_revision
                or binding is None
                or binding.proposal_event_ref != audit.event_ref
                or binding.proposal_event_payload_hash != audit.event_payload_hash
                or binding.model_result_ref != audit.model_result_ref
                or binding.capsule_id != audit.capsule_id
                or binding.change_id != change.change_id
                or binding.change_payload_hash != change.payload.payload_hash
                or candidate.recorded_event_ref is None
            ):
                continue
            located = self._ledger.lookup_event_commit(candidate.recorded_event_ref)
            if (
                located is None
                or located[0].event_type != "ProposalRecorded"
                or located[0].payload_hash != candidate.recorded_event_payload_hash
            ):
                raise RelationshipCommitmentWorkerError(
                    "stale_candidate_event_missing"
                )
            return candidate, located[0], located[1]
        return None

    @staticmethod
    def _has_terminal_delivered_expression(*, projection, proposal_id: str) -> bool:
        plans = tuple(
            item
            for item in projection.expression_plans
            if item.proposal_id == proposal_id and item.state == "completed"
        )
        for plan in plans:
            beats = tuple(
                item
                for item in projection.expression_beats
                if item.proposal_id == proposal_id
                and item.plan_id == plan.plan_id
                and item.acceptance_id == plan.acceptance_id
                and item.state == "settled"
                and item.action_id is not None
            )
            for beat in beats:
                actions = tuple(
                    item
                    for item in projection.actions
                    if item.action_id == beat.action_id
                    and item.expression_plan_id == plan.plan_id
                    and item.expression_beat_id == beat.beat_id
                    and item.state == "delivered"
                )
                for action in actions:
                    if any(
                        receipt.action_id == action.action_id
                        and receipt.receipt_kind == "terminal"
                        and receipt.observed_state == "delivered"
                        and receipt.is_terminal
                        for receipt in projection.execution_receipts
                    ):
                        return True
        return False


__all__ = [
    "RelationshipCommitmentWorker",
    "RelationshipCommitmentWorkerError",
    "RelationshipCommitmentWorkResult",
]
