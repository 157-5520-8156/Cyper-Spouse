"""Durably close one persisted typed proposal from an older World epoch."""

from __future__ import annotations

import hashlib
import json

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
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


def settle_stale_typed_proposal(
    *,
    ledger: LedgerPort,
    proposal_event: WorldEvent,
    proposal_id: str,
    evaluated_world_revision: int,
    current_cursor: ProjectionCursor,
    actor: str,
    source: str,
) -> CommitResult:
    """Append the installed generic stale terminal for one exact proposal."""

    if proposal_event.world_id != ledger.world_id:
        raise ValueError("stale proposal event belongs to another World")
    if evaluated_world_revision >= current_cursor.world_revision:
        raise ValueError("stale proposal settlement requires an older World epoch")
    projection = ledger.project_at(current_cursor)
    if any(item.proposal_id == proposal_id for item in projection.acceptance_decisions):
        raise ValueError("stale proposal already has a terminal decision")
    acceptance_id = f"acceptance:typed-proposal-stale:{_digest(proposal_id)}"
    payload = {
        "acceptance_id": acceptance_id,
        "status": "stale",
        "proposal_id": proposal_id,
        "evaluated_world_revision": evaluated_world_revision,
    }
    identity = domain_idempotency_key(
        event_type="AcceptanceRecorded",
        world_id=ledger.world_id,
        payload=payload,
    )
    if identity is None:
        raise RuntimeError("stale proposal settlement has no installed identity")
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:typed-proposal-stale:{_digest([ledger.world_id, proposal_id])}",
        world_id=ledger.world_id,
        event_type="AcceptanceRecorded",
        logical_time=projection.logical_time or proposal_event.logical_time,
        created_at=proposal_event.created_at,
        actor=actor,
        source=source,
        trace_id=proposal_event.trace_id,
        causation_id=proposal_event.event_id,
        correlation_id=proposal_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )
    return ledger.commit_at_cursor(
        (event,),
        expected_cursor=current_cursor,
        commit_id=f"commit:typed-proposal-stale:{_digest(event.event_id)}",
    )


__all__ = ["settle_stale_typed_proposal"]
