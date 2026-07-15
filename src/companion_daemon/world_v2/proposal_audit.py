"""Deep write seam for the independent Phase-4A audit transaction."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pydantic import Field

from .deliberation import DeliberationResult, ModelResultAudit, ModelRoute
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .proposal_audit_schemas import (
    ModelResultRecordedPayload,
    ProposalRecordedV2Payload,
    canonical_json,
    model_audit_json,
    sha256,
)
from .proposal_envelope import validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import CommitResult, ProjectionCursor, WorldEvent


class ProposalAuditContext(FrozenModel):
    world_id: str = Field(min_length=1)
    trigger_ref: str = Field(min_length=1, max_length=512)
    logical_time: datetime
    created_at: datetime
    actor: str = Field(min_length=1)
    source: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_commit_world_revision: int = Field(ge=0)
    expected_deliberation_revision: int = Field(ge=0)


class ProposalAuditCommit(FrozenModel):
    result: CommitResult
    model_result_ref: str
    proposal_id: str | None

    @property
    def world_revision(self) -> int:
        return self.result.world_revision

    @property
    def deliberation_revision(self) -> int:
        return self.result.deliberation_revision

    @property
    def event_ids(self) -> tuple[str, ...]:
        return self.result.event_ids

    @property
    def cursor(self) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=self.result.world_revision,
            deliberation_revision=self.result.deliberation_revision,
            ledger_sequence=self.result.ledger_sequence,
        )


class ProposalAuditRecorder:
    """Persist model and Proposal audit together without invoking Acceptance B."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    def record(
        self, result: DeliberationResult, context: ProposalAuditContext
    ) -> ProposalAuditCommit:
        events = self.build_events(result, context)
        validated = _strict_result(result)
        proposal = (
            validate_proposal_envelope(validated.proposal)
            if validated.proposal is not None
            else None
        )
        lineage = tuple(
            part
            for audit in validated.attempt_audits
            for part in (audit.model_call_id, audit.model_result_ref)
        )
        commit_id = _identity(
            "proposal-audit-commit",
            context.world_id,
            *lineage,
            proposal.proposal_id if proposal is not None else "no-proposal",
        )
        committed = self._ledger.commit(
            events,
            expected_world_revision=context.expected_commit_world_revision,
            expected_deliberation_revision=context.expected_deliberation_revision,
            commit_id=commit_id,
        )
        return ProposalAuditCommit(
            result=committed,
            model_result_ref=validated.audit.model_result_ref,
            proposal_id=proposal.proposal_id if proposal is not None else None,
        )

    def build_events(
        self, result: DeliberationResult, context: ProposalAuditContext
    ) -> tuple[WorldEvent, ...]:
        if context.world_id != self._ledger.world_id:
            raise ValueError("proposal audit belongs to another world")
        result = _strict_result(result)
        proposal = (
            validate_proposal_envelope(result.proposal)
            if result.proposal is not None
            else None
        )
        if proposal is not None and (
            proposal.trigger_ref != context.trigger_ref
            or proposal.evaluated_world_revision != context.evaluated_world_revision
        ):
            raise ValueError("proposal audit lineage does not match its commit context")
        evaluated_world_revision = (
            proposal.evaluated_world_revision
            if proposal is not None
            else context.evaluated_world_revision
        )

        model_events: list[WorldEvent] = []
        previous_cause = context.causation_id
        for index, audit in enumerate(result.attempt_audits):
            audit_json = model_audit_json(audit)  # type: ignore[arg-type]
            model_payload = ModelResultRecordedPayload(
                audit_contract=(
                    "model-result-audit.2" if audit.usage is not None else "model-result-audit.1"
                ),
                model_result_ref=audit.model_result_ref,
                deliberation_result_id=result.result_id,
                proposal_hash=proposal.proposal_hash if proposal is not None else None,
                model_call_id=audit.model_call_id,
                attempt_id=audit.attempt_id,
                capsule_id=result.capsule_id,
                trigger_ref=context.trigger_ref,
                evaluated_world_revision=evaluated_world_revision,
                attempt_index=index,
                attempt_count=len(result.attempt_audits),
                audit_json=audit_json,
                audit_hash=sha256(audit_json),
            )
            model_event = _event(
                context,
                event_type="ModelResultRecorded",
                identity=(audit.model_call_id, audit.model_result_ref),
                payload=model_payload.model_dump(mode="json"),
                causation_id=previous_cause,
            )
            model_events.append(model_event)
            previous_cause = model_event.event_id
        if proposal is None:
            return tuple(model_events)
        proposal_json = canonical_json(proposal.model_dump(mode="json"))
        proposal_payload = ProposalRecordedV2Payload(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            model_result_ref=result.audit.model_result_ref,
            deliberation_result_id=result.result_id,
            model_call_id=result.audit.model_call_id,
            attempt_id=result.audit.attempt_id,
            capsule_id=result.capsule_id,
            trigger_ref=context.trigger_ref,
            evaluated_world_revision=proposal.evaluated_world_revision,
            proposal_json=proposal_json,
            proposal_hash=proposal.proposal_hash,
        )
        proposal_event = _event(
            context,
            event_type="ProposalRecorded",
            identity=(context.trigger_ref, proposal.proposal_id),
            payload=proposal_payload.model_dump(mode="json"),
            causation_id=previous_cause,
        )
        return (*model_events, proposal_event)


def _strict_result(value: DeliberationResult) -> DeliberationResult:
    """Bound hostile constructed objects before any recursive serialization."""

    try:
        raw_attempts = value.attempt_audits
        if not isinstance(raw_attempts, tuple) or not 1 <= len(raw_attempts) <= 2:
            raise ValueError("model attempt audit count is out of bounds")
        proposal = (
            validate_proposal_envelope(value.proposal)
            if value.proposal is not None
            else None
        )
        audits = tuple(_strict_audit(audit) for audit in raw_attempts)
        final = _strict_audit(value.audit)
        return DeliberationResult(
            result_id=value.result_id,
            capsule_id=value.capsule_id,
            proposal=proposal,
            audit=final,
            attempt_audits=audits,
        )
    except Exception as exc:
        raise ValueError("deliberation result failed strict revalidation") from exc


def _strict_audit(value: ModelResultAudit) -> ModelResultAudit:
    route = ModelRoute(
        tier=value.route.tier,
        reason_code=value.route.reason_code,
        router_version=value.route.router_version,
    )
    return ModelResultAudit(
        model_call_id=value.model_call_id,
        model_result_ref=value.model_result_ref,
        attempt_id=value.attempt_id,
        route=route,
        model_id=value.model_id,
        model_version=value.model_version,
        request_hash=value.request_hash,
        response_hash=value.response_hash,
        status=value.status,
        failure_code=value.failure_code,
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        usage=value.usage,
    )


def _identity(label: str, *parts: str) -> str:
    encoded = json.dumps([label, *parts], ensure_ascii=False, separators=(",", ":")).encode()
    return f"{label}:{hashlib.sha256(encoded).hexdigest()}"


def _event(
    context: ProposalAuditContext,
    *,
    event_type: str,
    identity: tuple[str, ...],
    payload: dict[str, object],
    causation_id: str | None = None,
) -> WorldEvent:
    event_id = _identity(f"event:{event_type}", context.world_id, *identity)
    idempotency_key = domain_idempotency_key(
        event_type=event_type, world_id=context.world_id, payload=payload
    )
    if idempotency_key is None:
        raise ValueError(f"{event_type} has no installed domain identity")
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=context.world_id,
        event_type=event_type,
        logical_time=context.logical_time,
        created_at=context.created_at,
        actor=context.actor,
        source=context.source,
        trace_id=context.trace_id,
        causation_id=causation_id or context.causation_id,
        correlation_id=context.correlation_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )


__all__ = ["ProposalAuditCommit", "ProposalAuditContext", "ProposalAuditRecorder"]
