"""Source-bound vision/transcription request and immutable result authority.

Perception is deliberately not a generic tool alias.  The accepted request
names the media class and privacy class that may leave the world, while the
provider result stays an opaque descriptor until a separate deliberation lane
chooses what (if anything) to do with it.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field

from .event_identity import domain_idempotency_key
from .schema_core import FrozenModel
from .schemas import (
    Action,
    BudgetReservation,
    CommitResult,
    ExternalObservation,
    PerceptionAuthorizationBinding,
    PerceptionRequestProjection,
    PerceptionResultProjection,
    TriggerProcess,
    WorldEvent,
)
from .perception_authorization import require_perception_authorization


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def perception_result_trigger_id(*, world_id: str, result_id: str) -> str:
    return "trigger:perception-result:" + _digest({"world_id": world_id, "result_id": result_id})


class PerceptionProposal(FrozenModel):
    proposal_id: str = Field(min_length=1)
    source_event_ref: str = Field(min_length=1)
    source_world_revision: int = Field(ge=0)
    source_payload_hash: str = Field(min_length=64, max_length=64)
    analysis_kind: Literal["vision", "transcription"]
    input_ref: str = Field(min_length=1)
    input_hash: str = Field(min_length=64, max_length=71)
    content_privacy_class: Literal["public", "shareable", "personal", "private", "withhold"]
    budget_account_id: str = Field(min_length=1)
    budget_limit: int = Field(ge=0)
    authorization: PerceptionAuthorizationBinding

    @property
    def target(self) -> Literal["perception:vision", "perception:transcription"]:
        return f"perception:{self.analysis_kind}"  # type: ignore[return-value]

    @property
    def request_id(self) -> str:
        return "perception-request:" + _digest(
            {
                "proposal": self.proposal_id,
                "source": self.source_event_ref,
                "kind": self.analysis_kind,
                "input_hash": self.input_hash,
            }
        )

    @property
    def action_id(self) -> str:
        return "action:perception:" + _digest({"request": self.request_id})


class PerceptionRequestAcceptedPayload(FrozenModel):
    request: PerceptionRequestProjection


class PerceptionResultAcceptedPayload(FrozenModel):
    result: PerceptionResultProjection


class PerceptionAcceptanceRuntime:
    """Atomically accept exactly one audited perception request and its budget."""

    def __init__(self, *, ledger) -> None:
        self._ledger = ledger

    def accept(
        self,
        *,
        proposal: PerceptionProposal,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        projection = self._ledger.project()
        source_pair = self._ledger.lookup_event_commit(proposal.source_event_ref)
        if source_pair is None:
            raise ValueError("perception source event is missing")
        source_event, source_commit = source_pair
        if (
            source_commit.world_revision != proposal.source_world_revision
            or source_event.payload_hash != proposal.source_payload_hash
        ):
            raise ValueError("perception source authority does not bind")
        if any(item.action_id == proposal.action_id for item in projection.actions):
            raise ValueError("perception Action already exists")
        request = PerceptionRequestProjection(
            request_id=proposal.request_id,
            action_id=proposal.action_id,
            source_event_ref=proposal.source_event_ref,
            source_world_revision=proposal.source_world_revision,
            source_payload_hash=proposal.source_payload_hash,
            analysis_kind=proposal.analysis_kind,
            target=proposal.target,
            input_ref=proposal.input_ref,
            input_hash=proposal.input_hash,
            content_privacy_class=proposal.content_privacy_class,
        )
        reservation = BudgetReservation(
            reservation_id="reservation:perception:" + _digest({"request": request.request_id}),
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
            kind=proposal.analysis_kind,
            layer="perception_tool",
            intent_ref=request.request_id,
            actor="agent:companion",
            target=proposal.target,
            payload_ref=proposal.input_ref,
            payload_hash=proposal.input_hash,
            perception_authorization=proposal.authorization,
            idempotency_key="perception:" + _digest({"request": request.request_id}),
            budget_reservation_id=reservation.reservation_id,
            state="authorized",
            recovery_policy="result_lookup",
        )
        require_perception_authorization(
            action=action, projection=projection, logical_time=logical_time
        )
        definitions = (
            ("PerceptionRequestAccepted", {"request": request.model_dump(mode="json")}, "request"),
            ("BudgetReserved", {"reservation": reservation.model_dump(mode="json")}, "budget"),
            ("ActionAuthorized", {"action": action.model_dump(mode="json")}, "action"),
        )
        events = tuple(
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
                idempotency_key=domain_idempotency_key(
                    event_type=event_type, world_id=self._ledger.world_id, payload=payload
                )
                or f"perception:{request.request_id}:{suffix}",
                payload=payload,
            )
            for event_type, payload, suffix in definitions
        )
        return self._ledger.commit(
            events,
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            commit_id="commit:perception-accept:" + _digest({"request": request.request_id}),
        )


def accepted_perception_result_events(
    *,
    world_id: str,
    result: ExternalObservation,
    receipt_event: WorldEvent,
    request: PerceptionRequestProjection,
    accepted_event_ref: str,
) -> tuple[tuple[str, str, dict[str, object]], ...]:
    if result.status != "delivered" or result.result_ref is None or result.result_hash is None:
        return ()
    if result.action_id != request.action_id:
        raise ValueError("perception result belongs to another request")
    descriptor = PerceptionResultProjection(
        result_id="perception-result:" + _digest({"external": result.result_id}),
        request_id=request.request_id,
        action_id=result.action_id,
        analysis_kind=request.analysis_kind,
        content_privacy_class=request.content_privacy_class,
        result_ref=result.result_ref,
        result_hash=result.result_hash,
        receipt_event_ref=receipt_event.event_id,
        receipt_event_payload_hash=receipt_event.payload_hash,
        external_result_id=result.result_id,
        accepted_event_ref=accepted_event_ref,
        accepted_at=result.observed_at,
    )
    process = TriggerProcess(
        trigger_id=perception_result_trigger_id(world_id=world_id, result_id=descriptor.result_id),
        trigger_ref=f"perception-result:{descriptor.result_id}",
        process_kind="perception_result_deliberation",
        source_evidence_ref=accepted_event_ref,
        state="open",
    )
    return (
        (
            "PerceptionResultAccepted",
            "perception-result",
            {"result": descriptor.model_dump(mode="json")},
        ),
        ("TriggerProcessOpened", "result-trigger", {"process": process.model_dump(mode="json")}),
    )


__all__ = [
    "PerceptionAcceptanceRuntime",
    "PerceptionProposal",
    "PerceptionRequestAcceptedPayload",
    "PerceptionResultAcceptedPayload",
    "accepted_perception_result_events",
    "perception_result_trigger_id",
]
