"""Exact-cursor authority for a persisted generic DecisionProposal.

The handle issued here is deliberately read authority only.  It proves that a
specific model proposal was recorded at a specific ledger prefix; it grants no
right to turn that proposal into a domain mutation.  Domain compilers consume
the handle and retain their own narrow write capabilities.
"""

from __future__ import annotations

import json

from .ledger import LedgerPort
from .projection import InternalAuthorityReader
from .proposal_audit_schemas import ProposalAuditProjection, ProposalRecordedV2Payload
from .proposal_envelope import DecisionProposal, validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import ProjectionCursor, WorldEvent


class DecisionProposalAuthorityError(ValueError):
    """Stable failure at the generic proposal authority seam."""

    def __init__(self, code: str) -> None:
        self.code = f"decision_proposal_authority.{code}"
        super().__init__(self.code)


class AuditedDecisionProposal(FrozenModel):
    """Read-only value proved by the exact cursor reader."""

    cursor: ProjectionCursor
    audit: ProposalAuditProjection
    proposal: DecisionProposal


class PinnedDecisionProposalHandle:
    """Reader-issued process-local capability; it cannot be copied or serialized."""

    __slots__ = ("__authority", "__issuer")

    def __init__(self, *, authority: AuditedDecisionProposal, issuer: object) -> None:
        self.__authority = authority
        self.__issuer = issuer

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self) -> object:
        raise TypeError("pinned DecisionProposal handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("pinned DecisionProposal handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("pinned DecisionProposal handles cannot be copied")


class DecisionProposalAuthorityReader:
    """Issue exact-cursor handles for persisted generic decision proposals."""

    __slots__ = ("__ledger", "__reader", "__issuer")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self.__ledger = ledger
        self.__reader = InternalAuthorityReader(ledger=ledger)
        self.__issuer = object()

    def pin(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> PinnedDecisionProposalHandle:
        if world_id != self.__ledger.world_id:
            raise DecisionProposalAuthorityError("world_mismatch")
        if not proposal_id:
            raise DecisionProposalAuthorityError("proposal_id_empty")
        audit = self.__reader.proposal_audit_by_id(
            world_id=world_id, cursor=cursor, proposal_id=proposal_id
        )
        if audit is None:
            raise DecisionProposalAuthorityError("proposal_not_persisted")
        if audit.evaluated_world_revision != cursor.world_revision:
            raise DecisionProposalAuthorityError("proposal_stale")
        event = self.__event(audit)
        recorded = ProposalRecordedV2Payload.model_validate_json(event.payload_json)
        if (
            recorded.model_dump(mode="json")
            != audit.model_dump(mode="json", exclude={"event_ref", "event_payload_hash"})
            or event.payload_hash != audit.event_payload_hash
        ):
            raise DecisionProposalAuthorityError("proposal_event_mismatch")
        model_audit = self.__reader.model_result_audit_by_ref(
            world_id=world_id, cursor=cursor, model_result_ref=audit.model_result_ref
        )
        if model_audit is None or (
            model_audit.model_call_id != audit.model_call_id
            or model_audit.attempt_id != audit.attempt_id
            or model_audit.capsule_id != audit.capsule_id
            or model_audit.deliberation_result_id != audit.deliberation_result_id
            or model_audit.attempt_index != model_audit.attempt_count - 1
            or model_audit.proposal_hash != audit.proposal_hash
        ):
            raise DecisionProposalAuthorityError("model_result_mismatch")
        try:
            proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DecisionProposalAuthorityError("proposal_bytes_invalid") from exc
        if not isinstance(proposal, DecisionProposal):
            raise DecisionProposalAuthorityError("proposal_kind_unsupported")
        if (
            proposal.proposal_id != audit.proposal_id
            or proposal.proposal_hash != audit.proposal_hash
            or proposal.trigger_ref != audit.trigger_ref
            or proposal.evaluated_world_revision != cursor.world_revision
        ):
            raise DecisionProposalAuthorityError("proposal_audit_mismatch")
        return PinnedDecisionProposalHandle(
            authority=AuditedDecisionProposal(cursor=cursor, audit=audit, proposal=proposal),
            issuer=self.__issuer,
        )

    def read(self, handle: PinnedDecisionProposalHandle) -> AuditedDecisionProposal:
        if type(handle) is not PinnedDecisionProposalHandle or not handle.issued_by(self.__issuer):
            raise DecisionProposalAuthorityError("proposal_handle_untrusted")
        return object.__getattribute__(handle, "_PinnedDecisionProposalHandle__authority")

    def __event(self, audit: ProposalAuditProjection) -> WorldEvent:
        located = self.__ledger.lookup_event_commit(audit.event_ref)
        if located is None or type(located[0]) is not WorldEvent:
            raise DecisionProposalAuthorityError("proposal_event_missing")
        event = located[0]
        if event.world_id != self.__ledger.world_id or event.event_type != "ProposalRecorded":
            raise DecisionProposalAuthorityError("proposal_event_mismatch")
        return event


__all__ = [
    "AuditedDecisionProposal",
    "DecisionProposalAuthorityError",
    "DecisionProposalAuthorityReader",
    "PinnedDecisionProposalHandle",
]
