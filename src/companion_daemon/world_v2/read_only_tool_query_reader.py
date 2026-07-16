"""Read only query bytes frozen in a persisted tool-request proposal audit."""

from __future__ import annotations

import hashlib

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .ledger import LedgerPort
from .proposal_envelope import DecisionProposal
from .read_only_tool_proposal_compiler import tool_query_ref
from .read_only_tool_authorization import TOOL_NAME_BY_TARGET
from .schemas import Action, ProjectionCursor


class AuditedReadOnlyToolQueryReader:
    """Resolve a query only by rehydrating the audit that authorized its Action.

    The reader owns no write method and does not accept caller-provided query
    text.  Ambiguous/missing audited candidates fail before any provider call.
    """

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    async def resolve(self, action: Action) -> tuple[str, str, str, str]:
        if action.world_id != self._ledger.world_id:
            raise ValueError("tool query reader belongs to another world")
        projection = self._ledger.project()
        request = next(
            (item for item in projection.read_only_tool_requests if item.action_id == action.action_id),
            None,
        )
        if request is None or request.query_ref != action.payload_ref or request.query_hash != action.payload_hash:
            raise ValueError("tool Action has no exact accepted request")
        candidates: list[tuple[str, str]] = []
        reader = DecisionProposalAuthorityReader(ledger=self._ledger)
        for audit in projection.proposal_audits:
            if audit.proposal_kind != "decision":
                continue
            recorded = self._ledger.lookup_event_commit(audit.event_ref)
            if recorded is None:
                continue
            try:
                authority = reader.read(
                    reader.pin(
                        world_id=self._ledger.world_id,
                        cursor=ProjectionCursor(
                            world_revision=recorded[1].world_revision,
                            deliberation_revision=recorded[1].deliberation_revision,
                            ledger_sequence=recorded[1].ledger_sequence,
                        ),
                        proposal_id=audit.proposal_id,
                    )
                )
            except ValueError:
                continue
            proposal = authority.proposal
            if not isinstance(proposal, DecisionProposal):
                continue
            for change in proposal.proposed_changes:
                if change.kind != "read_only_tool_request" or change.transition != "request":
                    continue
                raw = change.payload.value()
                query = str(raw["query"])
                digest = "sha256:" + hashlib.sha256(query.encode()).hexdigest()
                if (
                    tool_query_ref(proposal_id=proposal.proposal_id, change_id=change.change_id)
                    == action.payload_ref
                    and digest == action.payload_hash
                    and str(raw["tool_name"]) == request.tool_name
                    and request.tool_name == TOOL_NAME_BY_TARGET.get(action.target)
                    and str(raw["target"]) == action.target
                ):
                    candidates.append((str(raw["tool_name"]), query))
        if len(candidates) != 1:
            raise ValueError("tool Action has no unique immutable query audit")
        tool_name, query = candidates[0]
        return tool_name, action.payload_ref, action.payload_hash, query


__all__ = ["AuditedReadOnlyToolQueryReader"]
