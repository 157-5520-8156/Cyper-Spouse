"""Durable terminal settlement for one audited typed change with no legal effect."""

from __future__ import annotations

import json
from typing import Literal

from .audited_change_terminal import (
    RELATIONSHIP_COMMITMENT_TERMINAL_REASON,
    audited_change_terminal_event_id,
    audited_change_terminal_payload,
    audited_change_terminal_proposal_id,
    terminal_relationship_commitment_payload,
    validate_audited_change_terminal_payload,
)
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .proposal_audit_schemas import ProposalAuditProjection
from .proposal_envelope import (
    DecisionProposal,
    TypedChange,
    validate_proposal_envelope,
)
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


class AuditedChangeTerminalSettlement(FrozenModel):
    """One derived typed-change proposal closed without authorizing a World effect."""

    status: Literal["rejected", "stale"]
    reason_code: str
    source_change_id: str
    derived_proposal_id: str
    commit: CommitResult


def _require_exact_source_change(
    *,
    audit: ProposalAuditProjection,
    change: TypedChange,
) -> None:
    terminal_relationship_commitment_payload(change)
    try:
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("audited change terminal source proposal is invalid") from exc
    if not isinstance(proposal, DecisionProposal):
        raise ValueError("audited change terminal source is not a decision proposal")
    matches = tuple(item for item in proposal.proposed_changes if item == change)
    if matches != (change,):
        raise ValueError("audited change terminal source change is not exact")


def find_terminal_audited_change(
    *,
    ledger: LedgerPort,
    audit: ProposalAuditProjection,
    change: TypedChange,
) -> AuditedChangeTerminalSettlement | None:
    """Return the one installed marker for this exact change, if it exists."""

    _require_exact_source_change(audit=audit, change=change)
    event_id = audited_change_terminal_event_id(audit=audit, change=change)
    located = ledger.lookup_event_commit(event_id)
    if located is None:
        return None
    event, commit = located
    payload = event.payload()
    source = ledger.lookup_event_commit(audit.event_ref)
    if source is None:
        raise ValueError("audited change terminal source event is missing")
    source_event, source_commit = source
    expected_idempotency_key = domain_idempotency_key(
        event_type="AdvisoryAcceptanceRejected",
        world_id=ledger.world_id,
        payload=payload,
    )
    if (
        event.world_id != ledger.world_id
        or event.event_type != "AdvisoryAcceptanceRejected"
        or event.event_id != event_id
        or event.event_id not in commit.event_ids
        or event.causation_id != audit.event_ref
        or expected_idempotency_key is None
        or event.idempotency_key != expected_idempotency_key
        or source_event.world_id != ledger.world_id
        or source_event.event_id != audit.event_ref
        or source_event.event_type != "ProposalRecorded"
        or source_event.payload_hash != audit.event_payload_hash
        or source_event.event_id not in source_commit.event_ids
        or source_commit.world_revision > commit.world_revision
        or source_commit.deliberation_revision >= commit.deliberation_revision
        or source_commit.ledger_sequence >= commit.ledger_sequence
    ):
        raise ValueError("audited change terminal marker is not exact")
    resolved = validate_audited_change_terminal_payload(
        payload=payload,
        audit=audit,
        current_world_revision=commit.world_revision,
    )
    if resolved != change:
        raise ValueError("audited change terminal marker changed source authority")
    return AuditedChangeTerminalSettlement(
        status=payload["stage"],
        reason_code=payload["reason_code"],
        source_change_id=change.change_id,
        derived_proposal_id=payload["proposal_id"],
        commit=commit,
    )


def settle_terminal_audited_change(
    *,
    ledger: LedgerPort,
    audit: ProposalAuditProjection,
    change: TypedChange,
    current_cursor: ProjectionCursor,
    actor: str,
    source: str,
) -> AuditedChangeTerminalSettlement:
    """Append an effect-free terminal for one exact audited typed change.

    The derived proposal identity excludes sibling changes from the same role
    output.  This is only a lifecycle terminal: it cannot authorize a mutation,
    infer a replacement character choice, or close another semantic lane.
    """

    if not actor or not source:
        raise ValueError("audited change terminal coordinates are invalid")
    _require_exact_source_change(audit=audit, change=change)
    projection = ledger.project_at(current_cursor)
    matches = tuple(
        item
        for item in projection.proposal_audits
        if item.proposal_id == audit.proposal_id
        and item.event_ref == audit.event_ref
        and item.event_payload_hash == audit.event_payload_hash
    )
    if matches != (audit,):
        raise ValueError("audited change terminal source is not exact")
    if audit.evaluated_world_revision > current_cursor.world_revision:
        raise ValueError("audited change terminal evaluates a future World revision")
    existing = find_terminal_audited_change(
        ledger=ledger,
        audit=audit,
        change=change,
    )
    if existing is not None:
        return existing
    status: Literal["rejected", "stale"] = (
        "stale"
        if audit.evaluated_world_revision < current_cursor.world_revision
        else "rejected"
    )
    located = ledger.lookup_event_commit(audit.event_ref)
    if located is None:
        raise ValueError("audited change terminal source event is missing")
    proposal_event, proposal_commit = located
    if (
        proposal_event.event_type != "ProposalRecorded"
        or proposal_event.payload_hash != audit.event_payload_hash
        or proposal_event.event_id not in proposal_commit.event_ids
        or proposal_commit.world_revision > current_cursor.world_revision
        or proposal_commit.deliberation_revision
        > current_cursor.deliberation_revision
        or proposal_commit.ledger_sequence > current_cursor.ledger_sequence
    ):
        raise ValueError("audited change terminal source event is not pinned")

    payload = audited_change_terminal_payload(
        audit=audit,
        change=change,
        status=status,
    )
    idempotency_key = domain_idempotency_key(
        event_type="AdvisoryAcceptanceRejected",
        world_id=ledger.world_id,
        payload=payload,
    )
    if idempotency_key is None:
        raise RuntimeError("audited change terminal has no installed identity")
    event_id = audited_change_terminal_event_id(audit=audit, change=change)
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=ledger.world_id,
        event_type="AdvisoryAcceptanceRejected",
        logical_time=projection.logical_time or proposal_event.logical_time,
        created_at=proposal_event.created_at,
        actor=actor,
        source=source,
        trace_id=proposal_event.trace_id,
        causation_id=proposal_event.event_id,
        correlation_id=proposal_event.correlation_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    commit = ledger.commit_at_cursor(
        (event,),
        expected_cursor=current_cursor,
        commit_id="commit:audited-typed-change-terminal:"
        + audited_change_terminal_proposal_id(audit=audit, change=change).rsplit(
            ":", 1
        )[-1],
    )
    return AuditedChangeTerminalSettlement(
        status=status,
        reason_code=RELATIONSHIP_COMMITMENT_TERMINAL_REASON,
        source_change_id=change.change_id,
        derived_proposal_id=payload["proposal_id"],
        commit=commit,
    )


__all__ = [
    "AuditedChangeTerminalSettlement",
    "find_terminal_audited_change",
    "settle_terminal_audited_change",
]
