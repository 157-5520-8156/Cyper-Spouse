"""Deep write seam for the independent Phase-4A audit transaction."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pydantic import Field

from .deliberation import (
    AuthoredCandidateInvocationAudit,
    DeliberationResult,
    ModelResultAudit,
    ModelRoute,
    ProviderSubcallAudit,
)
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
    expected_ledger_sequence: int = Field(ge=0)


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
        if proposal is not None:
            # Wording and proposed effects are meaningful only against the
            # complete snapshot the role model saw. A world-only Clock/Action
            # append must therefore invalidate the candidate just as surely as
            # another deliberation append.
            committed = self._ledger.commit_at_cursor(
                events,
                expected_cursor=ProjectionCursor(
                    world_revision=context.expected_commit_world_revision,
                    deliberation_revision=context.expected_deliberation_revision,
                    ledger_sequence=context.expected_ledger_sequence,
                ),
                commit_id=commit_id,
            )
        else:
            # A content-free terminal audit carries no wording or proposed
            # effect. PinnedTurn separately revalidates its exact durable
            # attempt before rebasing this technical result at the current head.
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
        authored_candidates: list[
            tuple[ModelResultAudit, AuthoredCandidateInvocationAudit]
        ] = []
        provider_subcalls: list[tuple[ModelResultAudit, ProviderSubcallAudit]] = []
        previous_cause = context.causation_id
        for index, audit in enumerate(result.attempt_audits):
            audit_json = model_audit_json(audit)  # type: ignore[arg-type]
            model_payload = ModelResultRecordedPayload(
                audit_contract=(
                    "model-result-audit.5"
                    if audit.presented_prefetch_traces
                    else "model-result-audit.4"
                    if audit.recall_trace is not None
                    or audit.prefetch_trace is not None
                    else "model-result-audit.3"
                    if audit.slot is not None
                    else "model-result-audit.2"
                    if audit.usage is not None
                    else "model-result-audit.1"
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
            authored_candidates.extend(
                (audit, candidate)
                for candidate in audit.authored_candidate_audits
            )
            provider_subcalls.extend(
                (audit, subcall) for subcall in audit.provider_subcall_audits
            )
        # Keep the authored main/recovery attempts as the first complete
        # deliberation group. Earlier author candidates and reviewer
        # invocations are adjacent independent single-attempt groups and
        # therefore cannot alter primary/recovery attempt semantics.
        for parent_audit, candidate in authored_candidates:
            candidate_audit = authored_candidate_model_audit(
                candidate,
                attempt_id=parent_audit.attempt_id,
            )
            candidate_audit_json = model_audit_json(candidate_audit)  # type: ignore[arg-type]
            candidate_result_id = "deliberation:" + sha256(
                canonical_json(
                    {
                        "capsule_id": result.capsule_id,
                        "proposal_hash": None,
                        "attempt_audits": [json.loads(candidate_audit_json)],
                    }
                )
            )
            candidate_payload = ModelResultRecordedPayload(
                audit_contract="model-result-audit.3",
                model_result_ref=candidate_audit.model_result_ref,
                deliberation_result_id=candidate_result_id,
                proposal_hash=None,
                model_call_id=candidate_audit.model_call_id,
                attempt_id=candidate_audit.attempt_id,
                capsule_id=result.capsule_id,
                trigger_ref=context.trigger_ref,
                evaluated_world_revision=evaluated_world_revision,
                attempt_index=0,
                attempt_count=1,
                audit_json=candidate_audit_json,
                audit_hash=sha256(candidate_audit_json),
            )
            candidate_event = _event(
                context,
                event_type="ModelResultRecorded",
                identity=(
                    candidate_audit.model_call_id,
                    candidate_audit.model_result_ref,
                ),
                payload=candidate_payload.model_dump(mode="json"),
                causation_id=previous_cause,
            )
            model_events.append(candidate_event)
            previous_cause = candidate_event.event_id
        for parent_audit, subcall in provider_subcalls:
            provider_audit = provider_subcall_model_audit(
                subcall,
                attempt_id=parent_audit.attempt_id,
            )
            provider_audit_json = model_audit_json(provider_audit)  # type: ignore[arg-type]
            provider_result_id = "deliberation:" + sha256(
                canonical_json(
                    {
                        "capsule_id": result.capsule_id,
                        "proposal_hash": None,
                        "attempt_audits": [json.loads(provider_audit_json)],
                    }
                )
            )
            provider_payload = ModelResultRecordedPayload(
                audit_contract="model-result-audit.3",
                model_result_ref=provider_audit.model_result_ref,
                deliberation_result_id=provider_result_id,
                proposal_hash=None,
                model_call_id=provider_audit.model_call_id,
                parent_model_call_id=provider_audit.parent_model_call_id,
                attempt_id=provider_audit.attempt_id,
                capsule_id=result.capsule_id,
                trigger_ref=context.trigger_ref,
                evaluated_world_revision=evaluated_world_revision,
                attempt_index=0,
                attempt_count=1,
                audit_json=provider_audit_json,
                audit_hash=sha256(provider_audit_json),
            )
            provider_event = _event(
                context,
                event_type="ModelResultRecorded",
                identity=(
                    provider_audit.model_call_id,
                    provider_audit.model_result_ref,
                ),
                payload=provider_payload.model_dump(mode="json"),
                causation_id=previous_cause,
            )
            model_events.append(provider_event)
            previous_cause = provider_event.event_id
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
    raw_candidates = value.authored_candidate_audits
    raw_subcalls = value.provider_subcall_audits
    if (
        not isinstance(raw_candidates, tuple)
        or len(raw_candidates) > 8
        or not isinstance(raw_subcalls, tuple)
        or len(raw_subcalls) > 16
    ):
        raise ValueError("nested provider audit count is out of bounds")
    candidates = tuple(
        AuthoredCandidateInvocationAudit(
            purpose=item.purpose,
            model_call_id=item.model_call_id,
            request_hash=item.request_hash,
            response_hash=item.response_hash,
            model_id=item.model_id,
            model_version=item.model_version,
            outcome=item.outcome,
            usage=item.usage,
        )
        for item in raw_candidates
    )
    subcalls = tuple(
        ProviderSubcallAudit(
            purpose=item.purpose,
            parent_model_call_id=item.parent_model_call_id,
            model_call_id=item.model_call_id,
            request_hash=item.request_hash,
            model_id=item.model_id,
            model_version=item.model_version,
            lane=item.lane,
            outcome=item.outcome,
            failure_code=item.failure_code,
            response_hash=item.response_hash,
            usage=item.usage,
        )
        for item in raw_subcalls
    )
    route = ModelRoute(
        tier=value.route.tier,
        reason_code=value.route.reason_code,
        router_version=value.route.router_version,
    )
    audit = ModelResultAudit(
        model_call_id=value.model_call_id,
        parent_model_call_id=value.parent_model_call_id,
        model_result_ref=value.model_result_ref,
        attempt_id=value.attempt_id,
        route=route,
        model_id=value.model_id,
        model_version=value.model_version,
        attempted_model_id=value.attempted_model_id,
        attempted_model_version=value.attempted_model_version,
        request_hash=value.request_hash,
        response_hash=value.response_hash,
        status=value.status,
        failure_code=value.failure_code,
        slot=value.slot,
        outcome=value.outcome,
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        usage=value.usage,
        recall_trace=value.recall_trace,
        prefetch_trace=value.prefetch_trace,
        presented_prefetch_traces=value.presented_prefetch_traces,
        provider_subcall_audits=subcalls,
        authored_candidate_audits=candidates,
    )
    if audit.parent_model_call_id is not None:
        raise ValueError("authored model attempt cannot claim a parent model call")
    candidate_ids = tuple(
        candidate.model_call_id for candidate in audit.authored_candidate_audits
    )
    allowed_parents = {audit.model_call_id, *candidate_ids}
    all_call_ids = (
        audit.model_call_id,
        *candidate_ids,
        *(subcall.model_call_id for subcall in audit.provider_subcall_audits),
    )
    if len(all_call_ids) != len(set(all_call_ids)):
        raise ValueError("nested provider invocation identities are not unique")
    if any(
        subcall.parent_model_call_id not in allowed_parents
        or subcall.parent_model_call_id == subcall.model_call_id
        for subcall in audit.provider_subcall_audits
    ):
        raise ValueError(
            "provider subcall parent has no batch-persisted author attempt"
        )
    return audit


def authored_candidate_model_audit(
    value: AuthoredCandidateInvocationAudit,
    *,
    attempt_id: str,
) -> ModelResultAudit:
    """Expand one non-final author invocation into an immutable audit record."""

    corrective = value.purpose not in {
        "primary_initial",
        "quick_recovery_initial",
        "provisional_initial",
    }
    unresolved = value.outcome == "validation_unresolved"
    model_result_ref = "model-result:" + sha256(
        canonical_json(
            {
                "model_call_id": value.model_call_id,
                "response_hash": value.response_hash,
            }
        )
    )
    return ModelResultAudit(
        model_call_id=value.model_call_id,
        model_result_ref=model_result_ref,
        attempt_id=attempt_id,
        route=ModelRoute(
            tier="flash",
            reason_code=(
                f"author_candidate.{value.purpose}.{value.outcome}"[:128]
            ),
            router_version="authored-candidate-audit.1",
        ),
        model_id=value.model_id,
        model_version=value.model_version,
        request_hash=value.request_hash,
        response_hash=value.response_hash,
        status="candidate_returned" if unresolved else "main_invalid",
        failure_code=(
            None
            if unresolved
            else "corrective_invalid"
            if corrective
            else "primary_invalid"
        ),
        slot="corrective" if corrective else "primary",
        outcome="returned" if unresolved else "invalid",
        input_tokens=value.usage.input_tokens if value.usage is not None else None,
        output_tokens=value.usage.output_tokens if value.usage is not None else None,
        usage=value.usage,
    )


def provider_subcall_model_audit(
    value: ProviderSubcallAudit,
    *,
    attempt_id: str,
) -> ModelResultAudit:
    """Expand one nested provider identity into an immutable audit record."""

    succeeded = value.outcome == "winner"
    response_hash = value.response_hash if succeeded else None
    model_result_ref = "model-result:" + sha256(
        canonical_json(
            {
                "model_call_id": value.model_call_id,
                "response_hash": response_hash,
            }
        )
    )
    return ModelResultAudit(
        model_call_id=value.model_call_id,
        parent_model_call_id=value.parent_model_call_id,
        model_result_ref=model_result_ref,
        attempt_id=attempt_id,
        route=ModelRoute(
            tier="flash",
            reason_code=f"validation.{value.purpose}"[:128],
            router_version="provider-subcall-audit.1",
        ),
        model_id=value.model_id if succeeded else None,
        model_version=value.model_version if succeeded else None,
        attempted_model_id=None if succeeded else value.model_id,
        attempted_model_version=None if succeeded else value.model_version,
        request_hash=value.request_hash,
        response_hash=response_hash,
        status=(
            "proposal_validated"
            if succeeded
            else "main_timeout"
            if value.outcome == "timeout"
            else "main_exception"
        ),
        failure_code=(
            None
            if succeeded
            else value.failure_code
            or (
                "source_review_timeout"
                if value.outcome == "timeout"
                else "source_review_exception"
            )
        ),
        slot=(
            "primary"
            if value.lane in {"primary", "direct"}
            else "backup"
        ),
        outcome=value.outcome,
        input_tokens=value.usage.input_tokens if value.usage is not None else None,
        output_tokens=value.usage.output_tokens if value.usage is not None else None,
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


__all__ = [
    "ProposalAuditCommit",
    "ProposalAuditContext",
    "ProposalAuditRecorder",
    "provider_subcall_model_audit",
]
