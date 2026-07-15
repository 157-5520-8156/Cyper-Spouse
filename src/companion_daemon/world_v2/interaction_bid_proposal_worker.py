"""Closed compile/accept work unit for an audited delivered-media bid."""

from __future__ import annotations

from typing import Literal

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .interaction_bid_acceptance_runtime import InteractionBidAcceptanceRuntime
from .interaction_bid_proposal_compiler import InteractionBidProposalCompiler
from .media_thread_acceptance_runtime import MediaDeliveryThreadAcceptanceRuntime
from .media_thread_proposal_compiler import MediaDeliveryThreadProposalCompiler
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor


class InteractionBidProposalWorkResult(FrozenModel):
    status: Literal["no_change", "accepted"]
    source_proposal_id: str
    typed_proposal_id: str | None = None
    compile_commit: CommitResult | None = None
    acceptance_commit: CommitResult | None = None


class InteractionBidProposalWorker:
    """Compile and accept exactly one delivery-bound bid, or durably do nothing."""

    def __init__(
        self,
        *,
        compiler: InteractionBidProposalCompiler,
        acceptance: InteractionBidAcceptanceRuntime,
        media_thread_compiler: MediaDeliveryThreadProposalCompiler | None = None,
        media_thread_acceptance: MediaDeliveryThreadAcceptanceRuntime | None = None,
        actor: str,
        source: str = "world-v2:interaction-bid-proposal-worker",
    ) -> None:
        if not actor:
            raise ValueError("interaction bid proposal worker actor is required")
        if compiler.ledger is not acceptance.ledger:
            raise ValueError("interaction bid compiler and acceptance must own the same ledger")
        if (media_thread_compiler is None) != (media_thread_acceptance is None):
            raise ValueError("media thread compiler and acceptance must be configured together")
        if media_thread_compiler is not None and media_thread_compiler.ledger is not compiler.ledger:
            raise ValueError("media thread compiler must own the same ledger")
        if media_thread_acceptance is not None and media_thread_acceptance.ledger is not compiler.ledger:
            raise ValueError("media thread acceptance must own the same ledger")
        self._compiler = compiler
        self._acceptance = acceptance
        self._media_thread_compiler = media_thread_compiler
        self._media_thread_acceptance = media_thread_acceptance
        self._authority = DecisionProposalAuthorityReader(ledger=compiler.ledger)
        self._actor = actor
        self._source = source

    @property
    def ledger(self):
        return self._compiler.ledger

    def process(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> InteractionBidProposalWorkResult:
        authority = self._authority.read(
            self._authority.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        if not authority.proposal.proposed_changes:
            return InteractionBidProposalWorkResult(
                status="no_change", source_proposal_id=proposal_id
            )
        projection = self.ledger.project_at(cursor)
        change = authority.proposal.proposed_changes[0]
        if change.kind == "media_delivery_thread_transition":
            return self._process_media_thread(
                world_id=world_id,
                cursor=cursor,
                proposal_id=proposal_id,
                projection=projection,
            )
        if change.kind != "interaction_bid_transition":
            raise ValueError("interaction proposal worker received unsupported change")
        existing = next(
            (item for item in projection.interaction_bid_proposals if item.decision_proposal_id == proposal_id),
            None,
        )
        if existing is not None:
            accepted = self._accept(cursor=cursor, proposal_id=existing.interaction_bid_proposal_id)
            return InteractionBidProposalWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                typed_proposal_id=existing.interaction_bid_proposal_id,
                acceptance_commit=accepted,
            )
        compiled = self._compiler.record(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        compiled_cursor = ProjectionCursor(
            world_revision=compiled.commit.world_revision,
            deliberation_revision=compiled.commit.deliberation_revision,
            ledger_sequence=compiled.commit.ledger_sequence,
        )
        accepted = self._accept(cursor=compiled_cursor, proposal_id=compiled.typed_proposal_id)
        return InteractionBidProposalWorkResult(
            status="accepted",
            source_proposal_id=compiled.source_proposal_id,
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

    def _process_media_thread(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str, projection
    ) -> InteractionBidProposalWorkResult:
        compiler = self._media_thread_compiler
        acceptance = self._media_thread_acceptance
        if compiler is None or acceptance is None:
            # The grammar only makes this shape reachable when production
            # composition installs the specialized authority chain.  Keeping
            # the worker closed here protects partial/manual composition too.
            raise ValueError("media delivery thread authority is not installed")
        existing = next(
            (item for item in projection.media_thread_proposals if item.decision_proposal_id == proposal_id),
            None,
        )
        if existing is not None:
            accepted = acceptance.accept_runtime_owned(
                handle=acceptance.pin_proposal(
                    cursor=cursor, proposal_id=existing.media_thread_proposal_id
                ),
                actor=self._actor,
                source=self._source,
            )
            return InteractionBidProposalWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                typed_proposal_id=existing.media_thread_proposal_id,
                acceptance_commit=accepted,
            )
        compiled = compiler.record(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        compiled_cursor = ProjectionCursor(
            world_revision=compiled.commit.world_revision,
            deliberation_revision=compiled.commit.deliberation_revision,
            ledger_sequence=compiled.commit.ledger_sequence,
        )
        accepted = acceptance.accept_runtime_owned(
            handle=acceptance.pin_proposal(
                cursor=compiled_cursor, proposal_id=compiled.typed_proposal_id
            ),
            actor=self._actor,
            source=self._source,
        )
        return InteractionBidProposalWorkResult(
            status="accepted",
            source_proposal_id=compiled.source_proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
            compile_commit=compiled.commit,
            acceptance_commit=accepted,
        )


__all__ = ["InteractionBidProposalWorker", "InteractionBidProposalWorkResult"]
