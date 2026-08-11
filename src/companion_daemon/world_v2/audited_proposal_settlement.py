"""Durable terminal settlement for an audited proposal with no legal effect."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from .acceptance_manifest import (
    AcceptanceManifestV2,
    canonical_acceptance_manifest_hash,
    derive_acceptance_manifest_proposal_v2,
)
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .proposal_audit_schemas import ProposalAuditProjection
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class AuditedProposalTerminalSettlement(FrozenModel):
    """One source proposal closed without authorizing a World effect."""

    status: Literal["rejected", "stale"]
    reason_code: str
    commit: CommitResult


def settle_terminal_audited_proposal(
    *,
    ledger: LedgerPort,
    audit: ProposalAuditProjection,
    current_cursor: ProjectionCursor,
    reason_code: str,
    actor: str,
    source: str,
) -> AuditedProposalTerminalSettlement:
    """Append the installed effect-free manifest for one exact ProposalAudit.

    This is only a lifecycle terminal. It cannot authorize a mutation, infer a
    replacement character choice, or turn an undelivered expression into one.
    """

    if not reason_code or len(reason_code) > 128 or not actor or not source:
        raise ValueError("audited proposal terminal coordinates are invalid")
    projection = ledger.project_at(current_cursor)
    matches = tuple(
        item
        for item in projection.proposal_audits
        if item.proposal_id == audit.proposal_id
        and item.event_ref == audit.event_ref
        and item.event_payload_hash == audit.event_payload_hash
    )
    if matches != (audit,):
        raise ValueError("audited proposal terminal source is not exact")
    if any(
        item.proposal_id == audit.proposal_id
        for item in projection.acceptance_decisions
    ):
        raise ValueError("audited proposal already has a terminal decision")
    if audit.evaluated_world_revision > current_cursor.world_revision:
        raise ValueError("audited proposal evaluates a future World revision")
    status: Literal["rejected", "stale"] = (
        "stale"
        if audit.evaluated_world_revision < current_cursor.world_revision
        else "rejected"
    )
    located = ledger.lookup_event_commit(audit.event_ref)
    if located is None:
        raise ValueError("audited proposal source event is missing")
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
        raise ValueError("audited proposal source event is not pinned")

    binding = derive_acceptance_manifest_proposal_v2(
        proposal_json=audit.proposal_json,
        proposal_event_ref=audit.event_ref,
        proposal_event_payload_hash=audit.event_payload_hash,
    )
    identity_material = {
        "contract": "audited-proposal-terminal.1",
        "world_id": ledger.world_id,
        "proposal_event_ref": audit.event_ref,
        "proposal_event_payload_hash": audit.event_payload_hash,
        "status": status,
        "reason_code": reason_code,
    }
    digest = _digest(identity_material)
    acceptance_id = (
        f"acceptance:semantic-source-terminal:{reason_code}:{digest}"
    )
    raw: dict[str, object] = {
        "manifest_version": "acceptance-manifest.2",
        "acceptance_id": acceptance_id,
        "status": status,
        "evaluated_world_revision": audit.evaluated_world_revision,
        "proposals": (binding.model_dump(mode="json"),),
        "authorized_effects": (),
    }
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    # The canonical JSON dump represents nested tuple fields as arrays; the
    # manifest model reconstitutes those immutable tuples before validation.
    manifest = AcceptanceManifestV2.model_validate(raw)
    payload = manifest.model_dump(mode="json")
    idempotency_key = domain_idempotency_key(
        event_type="AcceptanceRecorded",
        world_id=ledger.world_id,
        payload=payload,
    )
    if idempotency_key is None:
        raise RuntimeError("audited proposal terminal has no installed identity")
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:semantic-source-terminal:{digest}",
        world_id=ledger.world_id,
        event_type="AcceptanceRecorded",
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
        commit_id=f"commit:semantic-source-terminal:{digest}",
    )
    return AuditedProposalTerminalSettlement(
        status=status,
        reason_code=reason_code,
        commit=commit,
    )


__all__ = [
    "AuditedProposalTerminalSettlement",
    "settle_terminal_audited_proposal",
]
