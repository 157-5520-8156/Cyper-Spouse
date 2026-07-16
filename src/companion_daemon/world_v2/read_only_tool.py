"""Source-bound, non-mutating tool request and result authority.

This module deliberately does not expose a general write-tool abstraction.
One accepted request creates exactly one ``read_only_tool`` Action; a terminal
provider result becomes a ``ToolResultAccepted`` projection only when it is
bound to that Action and to an immutable result sidecar descriptor.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Protocol

from pydantic import Field

from .event_identity import domain_idempotency_key
from .schema_core import FrozenModel
from .schemas import (
    Action,
    BudgetReservation,
    CommitResult,
    ExternalObservation,
    ProjectionCursor,
    ReadOnlyToolAuthorizationBinding,
    ReadOnlyToolRequestProjection,
    ToolResultProjection,
    TriggerProcess,
    WorldEvent,
)
from .read_only_tool_authorization import require_read_only_tool_authorization


READ_ONLY_TOOL_CONTRACT_VERSION = "read-only-tool.1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def external_result_trigger_id(*, world_id: str, result_id: str) -> str:
    return "trigger:external-result:" + _digest({"world_id": world_id, "result_id": result_id})


class ReadOnlyToolProposal(FrozenModel):
    """A structured proposal whose source is one committed observation/event."""

    proposal_id: str = Field(min_length=1)
    source_event_ref: str = Field(min_length=1)
    source_world_revision: int = Field(ge=0)
    source_payload_hash: str = Field(min_length=64, max_length=64)
    tool_name: str = Field(min_length=1)
    target: str = Field(min_length=1)
    query_ref: str = Field(min_length=1)
    query_hash: str = Field(min_length=64, max_length=71)
    budget_account_id: str = Field(min_length=1)
    budget_limit: int = Field(ge=0)
    authorization: ReadOnlyToolAuthorizationBinding

    @property
    def request_id(self) -> str:
        return "tool-request:" + _digest(
            {
                "proposal_id": self.proposal_id,
                "source_event_ref": self.source_event_ref,
                "tool_name": self.tool_name,
                "query_hash": self.query_hash,
            }
        )

    @property
    def action_id(self) -> str:
        return "action:read-only-tool:" + _digest({"request_id": self.request_id})


class ToolRequestAcceptedPayload(FrozenModel):
    request: ReadOnlyToolRequestProjection


class ToolResultAcceptedPayload(FrozenModel):
    result: ToolResultProjection


class ReadOnlyToolAcceptanceError(ValueError):
    pass


class ReadOnlyToolAcceptanceRuntime:
    """Accept a source-bound read-only request in one reservation/action UoW."""

    def __init__(self, *, ledger) -> None:
        self._ledger = ledger

    def accept(
        self,
        *,
        proposal: ReadOnlyToolProposal,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        cursor = self._cursor(self._ledger.project())
        source_pair = self._ledger.lookup_event_commit(proposal.source_event_ref)
        if source_pair is None:
            raise ReadOnlyToolAcceptanceError("source_event_missing")
        source_event, source_commit = source_pair
        if (
            source_commit.world_revision != proposal.source_world_revision
            or source_event.payload_hash != proposal.source_payload_hash
            or source_commit.world_revision > cursor.world_revision
        ):
            raise ReadOnlyToolAcceptanceError("source_event_mismatch")
        if any(item.action_id == proposal.action_id for item in self._ledger.project_at(cursor).actions):
            raise ReadOnlyToolAcceptanceError("action_already_exists")
        request = ReadOnlyToolRequestProjection(
            request_id=proposal.request_id,
            action_id=proposal.action_id,
            source_event_ref=proposal.source_event_ref,
            source_world_revision=proposal.source_world_revision,
            source_payload_hash=proposal.source_payload_hash,
            tool_name=proposal.tool_name,
            query_ref=proposal.query_ref,
            query_hash=proposal.query_hash,
            target=proposal.target,
        )
        reservation = BudgetReservation(
            reservation_id="reservation:read-only-tool:" + _digest({"request": request.request_id}),
            account_id=proposal.budget_account_id,
            action_id=proposal.action_id,
            category="tool",
            amount_limit=proposal.budget_limit,
        )
        action = Action(
            schema_version="world-v2.1",
            action_id=proposal.action_id,
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            causation_id=proposal.source_event_ref,
            correlation_id=correlation_id,
            kind="read_only_tool",
            layer="read_only_tool",
            intent_ref=request.request_id,
            actor="agent:companion",
            target=proposal.target,
            payload_ref=proposal.query_ref,
            payload_hash=proposal.query_hash,
            read_only_tool_authorization=proposal.authorization,
            idempotency_key="read-only-tool:" + _digest({"request": request.request_id}),
            budget_reservation_id=reservation.reservation_id,
            state="authorized",
            recovery_policy="result_lookup",
        )
        require_read_only_tool_authorization(
            action=action,
            projection=self._ledger.project_at(cursor),
            logical_time=logical_time,
        )
        definitions = (
            ("ToolRequestAccepted", {"request": request.model_dump(mode="json")}, "request"),
            ("BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, "budget"),
            ("ActionAuthorized", {"action": action.model_dump(mode="json")}, "action"),
        )
        events: list[WorldEvent] = []
        for event_type, payload, suffix in definitions:
            identity = domain_idempotency_key(
                event_type=event_type, world_id=self._ledger.world_id, payload=payload
            )
            if identity is None:
                identity = "read-only-tool:" + _digest(
                    {"event_type": event_type, "request_id": request.request_id, "payload": payload}
                )
            events.append(
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id=f"event:{request.request_id}:{suffix}",
                    world_id=self._ledger.world_id,
                    event_type=event_type,
                    logical_time=logical_time,
                    created_at=created_at,
                    actor=actor,
                    source=source,
                    trace_id=trace_id,
                    causation_id=proposal.source_event_ref,
                    correlation_id=correlation_id,
                    idempotency_key=identity,
                    payload=payload,
                )
            )
        return self._ledger.commit(
            events,
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            commit_id="commit:read-only-tool-acceptance:" + _digest(
                {"cursor": cursor.model_dump(mode="json"), "request": request.request_id}
            ),
        )

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


def accepted_tool_result_events(
    *,
    world_id: str,
    result: ExternalObservation,
    receipt_event: WorldEvent,
    request: ReadOnlyToolRequestProjection,
    accepted_event_ref: str,
) -> tuple[tuple[str, str, dict[str, object]], ...]:
    """Derive the result projection and its deterministic next-turn trigger.

    The helper has no ledger access: settlement supplies the exact accepted
    receipt event and the reducer repeats all source bindings before projecting
    either effect.
    """

    if result.status != "delivered" or result.result_ref is None or result.result_hash is None:
        return ()
    if result.action_id != request.action_id:
        raise ValueError("tool result belongs to another request")
    tool_result = ToolResultProjection(
        result_id="tool-result:" + _digest({"external_result_id": result.result_id}),
        request_id=request.request_id,
        action_id=result.action_id,
        result_ref=result.result_ref,
        result_hash=result.result_hash,
        receipt_event_ref=receipt_event.event_id,
        receipt_event_payload_hash=receipt_event.payload_hash,
        external_result_id=result.result_id,
        accepted_event_ref=accepted_event_ref,
        accepted_at=result.observed_at,
    )
    trigger_id = external_result_trigger_id(world_id=world_id, result_id=tool_result.result_id)
    trigger = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"external-result:{tool_result.result_id}",
        process_kind="external_result_deliberation",
        source_evidence_ref=accepted_event_ref,
        state="open",
    )
    return (
        ("ToolResultAccepted", "tool-result", {"result": tool_result.model_dump(mode="json")}),
        ("TriggerProcessOpened", "result-trigger", {"process": trigger.model_dump(mode="json")}),
    )


class ToolQueryReader(Protocol):
    async def resolve(self, action: Action) -> tuple[str, str, str, str]: ...


class ReadOnlyToolProvider(Protocol):
    provider: str

    async def execute(
        self, *, target: str, tool_name: str, query_ref: str, query_hash: str, body: str, idempotency_key: str
    ) -> tuple[str, str, str, int]: ...


__all__ = [
    "READ_ONLY_TOOL_CONTRACT_VERSION",
    "ReadOnlyToolAcceptanceError",
    "ReadOnlyToolAcceptanceRuntime",
    "ReadOnlyToolProposal",
    "ReadOnlyToolProvider",
    "ToolQueryReader",
    "ToolRequestAcceptedPayload",
    "ToolResultAcceptedPayload",
    "accepted_tool_result_events",
    "external_result_trigger_id",
]
