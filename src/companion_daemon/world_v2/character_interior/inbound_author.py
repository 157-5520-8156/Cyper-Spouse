"""The sole private character author for an ordinary inbound Inner Turn.

The author performs one provider round trip and returns one merged
``ModelOutput`` carrying Expression, Appraisal, and optional immediate Affect.
Its wire materializers are inaccessible implementation details. Production can
only invoke it through ``InboundTurnFaculty`` and ``CharacterInterior.consider``;
there is no expression/appraisal composition surface or legacy fallback route.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from hashlib import sha256
import json
import logging
from typing import Any

from companion_daemon.llm import (
    model_request_emission_scope,
)

from ..affect_target_bounds import (
    AffectTargetBelowMinimumError,
    target_reselection_instruction,
)
from ..companion_identity import (
    CompanionIdentityFrame,
    companion_identity_source_refs,
)
from ..model_completion import ChatCompletionModel
from ..source_closure_lane import SourceClosureReselectionLane
from .inbound_appraisal_wire import (
    _active_affect_heads,
    _appraisal_draft_messages,
    _proposal_from_draft as materialize_appraisal_draft,
)
from .inbound_tool_contract import InboundToolContracts
from .inbound_wire import (
    _ExpressionDraftWire,
    _ProviderSubcallAuditCapture,
    RecallChoiceValidationError,
    _RoutedExpressionDraftWire,
    ValidationReselectionResult,
    _ExpressionRecoveryContextStore,
    _ProviderInvocationIdentity,
    _parse_json_object,
    _life_authority_availability_from_messages,
    _split_expression_episode_disposition,
    _provider_invocation_identity,
    _proposal_from_model_text as materialize_expression_draft,
    _source_closure_reselection_envelope,
    _stream_unit_identity,
    _trace_source_reselection_materialization_failure,
    _combine_usage,
    _expression_tool_reselection_kwargs,
    parse_character_recall_request,
    claim_repair_instruction,
    complete_bounded_validation_reselection,
    is_authored_expression_draft_shape_violation,
    is_private_turn_state_violation,
    is_recall_choice_violation,
    private_turn_state_reselection_instruction,
    recall_choice_reselection_instruction,
    review_expression_with_candidate_external_coverage,
    shape_repair_instruction,
    source_closure_violation,
)
from ..deliberation import (
    ModelInput,
    ModelOutput,
    ModelUsageProvenance,
    PhysicalProviderInvocationAudit,
    RecoveryCandidateFailure,
    ValidationTechnicalFailure,
    begin_validation_reselection_recovery,
    claim_secondary_provider_slot,
    expression_episode_provider_slots_active,
    fit_pre_provider_wait_timeout,
    fit_secondary_call_timeout,
    has_provider_slot_coordinator,
    mark_first_role_provider_completion,
    mark_first_role_provider_entry,
)
from ..expression_draft import (
    ExpressionDraftCapabilities,
    SourceRefAliasTable,
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
    build_source_ref_alias_table,
    is_world_claim_violation as _is_world_claim_violation,
    request_requires_response_expectation_assessment,
    validate_expression_private_turn_state,
    world_claim_source_ref_aliases_by_scope,
)
from ..isolated_source_closure_trace import (
    SourceClosureTraceStage,
    emit_source_closure_trace,
)
from ..model_facing_context import compact_chat_model_facing_context
from ..production_reliability_metrics import (
    record_claim_repair,
    record_failsafe,
    record_shape_repair,
    record_source_closure_reselection,
)
from ..private_turn_state import PrivateTurnState
from ..proposal_envelope import (
    DecisionProposal,
    MinimalProposal,
    ProposalEvidenceRef,
    validate_proposal_envelope,
)
from ..recall_index import RecallCursor
from ..recall_runtime import (
    PREFETCH_FIRST_PASS_JOIN_SECONDS,
    PresentedPrefetchTrace,
    RecallCoordinator,
    TrustedRecallTrace,
    append_presented_prefetch,
    augment_model_content_with_recall,
    mark_recall_budget_consumed,
    model_content_allows_recall,
    perform_character_recall,
    perform_character_recall_with_prefetch,
    recall_followup_evidence_json,
    verify_trusted_recall_trace,
)
from ..structured_expression_reselection_model import (
    expression_reselection_output_contract,
    normalize_realtime_expression_reselection_output,
)
from .author_identity import character_semantic_author_identity


_MAX_PENDING_DRAFTS = 64
_CONTEXTUAL_FAILSAFE_TIMEOUT_SECONDS = 3.0
_CONTEXTUAL_FAILSAFE_VERSION = "contextual-failure-recovery.1"
# One corrective completion for a claim-bookkeeping near-miss.  A repaired
# genuine reply a few seconds late reads far more human than an instant
# canned acknowledgement, but the wait stays bounded.
_CLAIM_REPAIR_TIMEOUT_SECONDS = 8.0
_APPRAISAL_COMMON_FIELDS = frozenset(
    {
        "appraise",
        "affect",
        "brief_rationale",
        "behavior_tendency",
        "stance",
        "display_strategy",
        "confidence",
        "relationship_signal",
    }
)
_APPRAISAL_EVENT_FIELDS = frozenset({"meanings", "attribution", "severity"})
_APPRAISAL_AFFECT_FIELDS = {
    "open": frozenset({"components"}),
    "update": frozenset({"episode_id", "components"}),
    "resolve": frozenset({"episode_id", "resolution_summary"}),
    "supersede": frozenset({"episode_id", "components"}),
}
logger = logging.getLogger(__name__)




class _InboundRecallRequested(RuntimeError):
    """One valid role-authored request for CharacterInterior-owned Recall.

    This is a private control transfer between the paired wire
    materializer and the ``inbound_turn`` Faculty.  It carries no recalled
    bytes and performs no retrieval.  CharacterInterior remains the only
    component allowed to execute Recall and to present the resulting sources
    to the same role on its bounded follow-up.
    """

    def __init__(
        self,
        *,
        query: str,
        model_id: str,
        model_version: str,
        model_call_id: str,
        request_hash: str,
        response_hash: str,
        usage: ModelUsageProvenance | None,
        private_turn_state: PrivateTurnState | None,
    ) -> None:
        super().__init__("character interior recall requested")
        self.query = query
        self.model_id = model_id
        self.model_version = model_version
        self.model_call_id = model_call_id
        self.request_hash = request_hash
        self.response_hash = response_hash
        self.usage = usage
        self.private_turn_state = private_turn_state


def _trace_source_closure_rejection(
    *,
    stage: SourceClosureTraceStage,
    raw: str,
    review: Any,
) -> None:
    """Expose only the rejected visible surface to an explicit audit scope."""

    emit_source_closure_trace(
        stage=stage,
        raw_candidate=raw,
        ci=tuple(review.unsupported_claim_indexes),
        v=tuple(review.visible_text_failures),
        p=tuple(review.private_turn_state_failures),
        visible_findings=tuple(review.visible_findings),
        discourse_resolved_visible_finding_indexes=tuple(
            review.discourse_resolved_visible_finding_indexes
        ),
    )


def _cache_key(request: ModelInput) -> tuple[str, ...]:
    """Locate transient paired state for one immutable inbound Observation.

    This key deliberately locates an episode across Appraisal acceptance so
    Recall provenance can be carried forward.  It is never sufficient reuse
    authority: every cached value also freezes the complete origin call,
    cursor, Capsule/Context request hash, and provider identity, which callers
    must compare before any bytes or conversation are reused.
    """

    trigger = request.trigger_message
    if trigger is None:
        raise ValueError("single-call inbound cognition requires a verified current message")
    return (request.trigger_ref, trigger.observation_ref, trigger.event_payload_hash)


def _model_input_request_hash(request: ModelInput) -> str:
    """Match Deliberation's canonical request hash without changing its API."""

    canonical = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(canonical.encode()).hexdigest()


def _failed_cache_key(request: ModelInput) -> tuple[str, ...]:
    """Bind failed bytes/conversations to one complete Pinned Turn identity."""

    return (
        *_cache_key(request),
        request.call_id,
        str(request.evaluated_world_revision),
        str(request.evaluated_deliberation_revision),
        str(request.evaluated_ledger_sequence),
        request.capsule_id,
        _model_input_request_hash(request),
    )


def _provider_runtime_resource_ids(provider: object | None) -> frozenset[int]:
    """Identify mutable provider resources that an observer must not share."""

    if provider is None:
        return frozenset()
    resources: set[int] = set()
    visited: set[int] = set()

    def visit(candidate: object | None) -> None:
        if candidate is None or id(candidate) in visited:
            return
        visited.add(id(candidate))
        for attribute in ("client", "capacity_gate", "circuit_breaker"):
            value = getattr(candidate, attribute, None)
            if value is not None:
                resources.add(id(value))
        visit(getattr(candidate, "primary", None))
        visit(getattr(candidate, "fallback", None))

    visit(provider)
    return frozenset(resources)


class _PendingExpression:
    __slots__ = (
        "raw",
        "model_id",
        "route_tier",
        "usage",
        "private_state_context_json",
        "source_ref_aliases",
        "origin_call_id",
        "origin_request_hash",
        "origin_world_revision",
        "origin_deliberation_revision",
        "origin_ledger_sequence",
        "winning_model_call_id",
        "winning_request_hash",
        "author_provider",
        "corrective_spent",
        "episode_disposition",
        "recall_trace",
        "prefetch_trace",
        "presented_prefetch_traces",
    )

    def __init__(
        self,
        *,
        raw: str,
        model_id: str,
        route_tier: str,
        usage: ModelUsageProvenance | None,
        private_state_context_json: str,
        source_ref_aliases: SourceRefAliasTable,
        origin_call_id: str,
        origin_request_hash: str,
        origin_world_revision: int,
        origin_deliberation_revision: int,
        origin_ledger_sequence: int,
        winning_model_call_id: str,
        winning_request_hash: str,
        author_provider: ChatCompletionModel,
        corrective_spent: bool,
        episode_disposition: str | None = None,
        recall_trace: TrustedRecallTrace | None = None,
        prefetch_trace: TrustedRecallTrace | None = None,
        presented_prefetch_traces: tuple[PresentedPrefetchTrace, ...] = (),
    ) -> None:
        self.raw = raw
        self.model_id = model_id
        self.route_tier = route_tier
        self.usage = usage
        self.private_state_context_json = private_state_context_json
        self.source_ref_aliases = source_ref_aliases
        self.origin_call_id = origin_call_id
        self.origin_request_hash = origin_request_hash
        self.origin_world_revision = origin_world_revision
        self.origin_deliberation_revision = origin_deliberation_revision
        self.origin_ledger_sequence = origin_ledger_sequence
        self.winning_model_call_id = winning_model_call_id
        self.winning_request_hash = winning_request_hash
        self.author_provider = author_provider
        self.corrective_spent = corrective_spent
        self.episode_disposition = episode_disposition
        self.recall_trace = recall_trace
        self.prefetch_trace = prefetch_trace
        self.presented_prefetch_traces = presented_prefetch_traces


class _CombinedInteriorStreamProvider:
    """One physical stream carrying appraisal plus an expression episode.

    This adapter is process-local to one inbound InnerTurn.  Its first call
    starts the provider stream through the existing cancellation coordinator
    and returns only after a complete appraisal and first visible expression
    frame are available.  Later bytes remain attached to that same request.
    Any bounded structural correction is delegated to the same provider as a
    normal completion; it cannot silently create a second character lane.
    """

    def __init__(
        self,
        *,
        request: ModelInput,
        provider: ChatCompletionModel,
        stream_adapter: _ExpressionDraftWire,
        temperature: float,
        model_version: str,
    ) -> None:
        self.model = str(getattr(provider, "model", "character-interior-stream"))
        self.reports_exact_request_emission = bool(
            getattr(provider, "reports_exact_request_emission", False)
        )
        self.supports_required_tool_choice = bool(
            getattr(provider, "supports_required_tool_choice", False)
        )
        self.supports_strict_tool_choice = bool(
            getattr(provider, "supports_strict_tool_choice", False)
        )
        self._request = request
        self._provider = provider
        self._stream_adapter = stream_adapter
        self._temperature = temperature
        self._model_version = model_version
        self._generation = stream_adapter._stream_generation(request)  # noqa: SLF001
        self._provider_identity: _ProviderInvocationIdentity | None = None
        self._messages: list[dict[str, str]] | None = None
        self._head_raw: str | None = None
        self._head_requested = False
        self._retirement: PhysicalProviderInvocationAudit | None = None

    @property
    def provider_identity(self) -> _ProviderInvocationIdentity:
        if self._provider_identity is None:
            raise RuntimeError("character interior stream has no provider identity")
        return self._provider_identity

    @property
    def head_raw(self) -> str:
        if self._head_raw is None:
            raise RuntimeError("character interior stream has no head")
        return self._head_raw

    @property
    def retirement(self) -> PhysicalProviderInvocationAudit | None:
        return self._retirement

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
        tool_contract_identity: dict[str, str] | None = None,
    ) -> str:
        if self._head_requested:
            self._retire_predecessor()
            return await self._delegate_completion(
                messages,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
            )
        self._head_requested = True
        identity = _provider_invocation_identity(
            parent_call_id=self._request.call_id,
            purpose="paired_cognition_initial",
            messages=messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            tool_contract_identity=tool_contract_identity,
        )
        self._provider_identity = identity
        self._messages = messages
        raw, _usage, returned_identity, _complete = (
            await self._stream_adapter._unit_stream_result(  # noqa: SLF001
                request=self._request,
                messages=messages,
                temperature=temperature,
                part="head",
                provider_identity=identity,
                stream_generation=self._generation,
                tools=tools,
                tool_choice=tool_choice,
            )
        )
        if returned_identity != identity:
            raise RuntimeError("character interior stream changed provider identity")
        self._head_raw = raw
        return raw

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, object]:
        """Retire the physical stream before every metered correction lane."""

        self._retire_predecessor()
        return await self._delegate_metered_completion(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            json_mode=True,
        )

    async def complete_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        """Retire the physical stream before a legacy metered correction."""

        self._retire_predecessor()
        return await self._delegate_metered_completion(
            messages,
            temperature=temperature,
            json_mode=False,
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
        tool_contract_identity: dict[str, str] | None = None,
    ) -> str:
        return await self.complete_json(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            tool_contract_identity=tool_contract_identity,
        )

    async def tail(
        self,
        request: ModelInput,
    ) -> tuple[str, ModelUsageProvenance | None, str]:
        messages = self._messages
        if messages is None:
            raise RuntimeError("character interior stream tail preceded its head")
        raw, usage_raw, identity, complete_raw = (
            await self._stream_adapter._unit_stream_result(  # noqa: SLF001
                request=request,
                messages=messages,
                temperature=self._temperature,
                part="tail",
                provider_identity=self.provider_identity,
                stream_generation=self._generation,
            )
        )
        if identity != self.provider_identity or complete_raw is None:
            raise RuntimeError("character interior stream tail lost physical identity")
        usage = (
            ModelUsageProvenance.model_validate(usage_raw)
            if usage_raw is not None
            else None
        )
        return raw, usage, complete_raw

    def cancel(self) -> None:
        self._stream_adapter._cancel_unit_stream_for(self._request)  # noqa: SLF001

    def _retire_predecessor(self) -> None:
        if self._head_requested and self._retirement is None:
            self._retirement = self.retirement_audit(
                model_id=str(getattr(self._provider, "model", self.model)),
                model_version=self._model_version,
            )

    def retirement_audit(
        self,
        *,
        model_id: str,
        model_version: str,
    ) -> PhysicalProviderInvocationAudit:
        """Truthfully settle a stream replaced before head authorization."""

        key = self._stream_adapter._unit_stream_key(self._request)  # noqa: SLF001
        session = self._stream_adapter._unit_stream_sessions.get(key)  # noqa: SLF001
        complete_raw: str | None = None
        usage: ModelUsageProvenance | None = None
        cancellation_confirmed = bool(
            session is not None and session.completed.cancelled()
        )
        if session is not None and session.completed.done() and not session.completed.cancelled():
            try:
                _head, _tail, usage_raw, complete_raw = session.completed.result()
            except BaseException:
                complete_raw = None
            else:
                if usage_raw is not None:
                    usage = ModelUsageProvenance.model_validate(usage_raw)
        provider_identity = self.provider_identity
        head_identity = _stream_unit_identity(provider_identity, "head")
        tail_identity = _stream_unit_identity(provider_identity, "tail")
        self.cancel()
        completed = complete_raw is not None
        outcome = (
            "completed"
            if completed
            else "cancelled"
            if cancellation_confirmed
            else "unresolved"
        )
        return PhysicalProviderInvocationAudit(
            model_call_id=provider_identity.model_call_id,
            request_hash=provider_identity.request_hash,
            model_id=model_id,
            model_version=model_version,
            outcome=outcome,
            failure_code=(
                None
                if completed
                else "stream_reselected"
                if cancellation_confirmed
                else "stream_reselection_unresolved"
            ),
            response_hash=(
                sha256(complete_raw.encode("utf-8")).hexdigest()
                if complete_raw is not None
                else None
            ),
            usage_status=(
                "provider_reported"
                if usage is not None
                else "unresolved"
                if completed or not cancellation_confirmed
                else "cancelled"
            ),
            usage=usage,
            semantic_model_call_ids=(
                head_identity.model_call_id,
                tail_identity.model_call_id,
            ),
        )

    async def _delegate_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> str:
        operation = (
            getattr(self._provider, "complete_json", None)
            if tools is not None
            else None
        )
        if not callable(operation):
            operation = getattr(self._provider, "complete", None)
        if not callable(operation):
            raise RuntimeError("character interior correction provider is unavailable")
        return await operation(
            messages,
            temperature=temperature,
            **(
                {"tools": tools, "tool_choice": tool_choice}
                if tools is not None
                else {}
            ),
        )

    async def _delegate_metered_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
        json_mode: bool,
    ) -> tuple[str, object]:
        operation = (
            getattr(self._provider, "complete_json_with_usage", None)
            if json_mode
            else getattr(self._provider, "complete_with_usage", None)
        )
        if not callable(operation):
            operation = getattr(self._provider, "complete_with_usage", None)
        if not callable(operation):
            raise RuntimeError("metered character interior correction is unavailable")
        result = await operation(
            messages,
            temperature=temperature,
            **(
                {"tools": tools, "tool_choice": tool_choice}
                if tools is not None
                else {}
            ),
        )
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], str)
        ):
            raise ValueError("metered character correction must return (text, usage)")
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self._provider, name)


class _FailedExpressionDetail:
    """The exact provider conversation and violation of one structural reject.

    Retained so the post-acceptance expression pass can spend one corrective
    retry that names the concrete violation before the bounded role-model
    recovery.  This is attempt-bound evidence for a retry, never accepted state.
    """

    __slots__ = (
        "messages",
        "raw",
        "violation",
        "usage",
        "private_state_context_json",
        "source_ref_aliases",
        "origin_call_id",
        "origin_request_hash",
        "origin_world_revision",
        "origin_deliberation_revision",
        "origin_ledger_sequence",
    )

    def __init__(
        self,
        *,
        messages: list[dict[str, str]],
        raw: str,
        violation: str,
        usage: ModelUsageProvenance | None,
        private_state_context_json: str,
        source_ref_aliases: SourceRefAliasTable,
        origin_call_id: str,
        origin_request_hash: str,
        origin_world_revision: int,
        origin_deliberation_revision: int,
        origin_ledger_sequence: int,
    ) -> None:
        self.messages = messages
        self.raw = raw
        self.violation = violation
        self.usage = usage
        self.private_state_context_json = private_state_context_json
        self.source_ref_aliases = source_ref_aliases
        self.origin_call_id = origin_call_id
        self.origin_request_hash = origin_request_hash
        self.origin_world_revision = origin_world_revision
        self.origin_deliberation_revision = origin_deliberation_revision
        self.origin_ledger_sequence = origin_ledger_sequence


class _BoundedKeySet:
    """Small insertion-ordered set for same-trigger recovery markers."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: OrderedDict[tuple[str, ...], None] = OrderedDict()

    def add(self, key: tuple[str, ...]) -> None:
        self._items.pop(key, None)
        self._items[key] = None
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def discard(self, key: tuple[str, ...]) -> None:
        self._items.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._items


def _merge_evidence(
    *groups: tuple[ProposalEvidenceRef, ...],
) -> tuple[ProposalEvidenceRef, ...]:
    by_ref: dict[str, ProposalEvidenceRef] = {}
    for group in groups:
        for evidence in group:
            previous = by_ref.get(evidence.ref_id)
            if previous is not None and previous != evidence:
                raise ValueError("inbound cognition changed one evidence binding")
            by_ref[evidence.ref_id] = evidence
    return tuple(by_ref.values())


def _merge_cognition_outputs(
    *, appraisal: ModelOutput | None, expression: ModelOutput
) -> ModelOutput:
    """Merge same-call typed state changes without changing expression choice."""

    expression_proposal = validate_proposal_envelope(expression.raw_proposal)
    if appraisal is None:
        return expression
    appraisal_proposal = validate_proposal_envelope(appraisal.raw_proposal)
    if not isinstance(appraisal_proposal, DecisionProposal):
        raise ValueError("inbound appraisal did not return a DecisionProposal")
    state_changes = tuple(
        change
        for change in appraisal_proposal.proposed_changes
        if change.kind
        in {"appraisal_transition", "affect_transition", "relationship_signal"}
    )
    if not state_changes:
        return expression
    evidence = _merge_evidence(
        appraisal_proposal.evidence_refs,
        expression_proposal.evidence_refs,
    )
    expression_changes = expression_proposal.proposed_changes
    if isinstance(expression_proposal, DecisionProposal):
        merged = expression_proposal.model_copy(
            update={
                "evidence_refs": evidence,
                "proposed_changes": (*state_changes, *expression_changes),
                "appraisals": appraisal_proposal.appraisals,
                "affect_tendencies": appraisal_proposal.affect_tendencies,
                "affect_decision": appraisal_proposal.affect_decision,
            }
        )
        merged = DecisionProposal.model_validate(merged.model_dump(mode="python"))
    elif isinstance(expression_proposal, MinimalProposal):
        merged = DecisionProposal(
            proposal_id=expression_proposal.proposal_id,
            trigger_ref=expression_proposal.trigger_ref,
            evaluated_world_revision=expression_proposal.evaluated_world_revision,
            evidence_refs=evidence,
            proposed_changes=(*state_changes, *expression_changes),
            action_intents=expression_proposal.action_intents,
            confidence=expression_proposal.confidence,
            brief_rationale=expression_proposal.brief_rationale,
            private_turn_state=expression_proposal.private_turn_state,
            appraisals=appraisal_proposal.appraisals,
            affect_tendencies=appraisal_proposal.affect_tendencies,
            affect_decision=appraisal_proposal.affect_decision,
            behavior_tendency=appraisal_proposal.behavior_tendency,
            stance=expression_proposal.stance,
            display_strategy=appraisal_proposal.display_strategy,
            timing_choice=(
                "silent"
                if not expression_proposal.proposed_changes
                and not expression_proposal.action_intents
                else "later"
                if len(expression_proposal.action_intents) == 1
                and expression_proposal.action_intents[0].kind == "followup"
                else "now"
            ),
            response_expectation_assessment=(
                expression_proposal.response_expectation_assessment
            ),
        )
    else:
        raise ValueError("inbound expression returned an unsupported proposal kind")
    return expression.model_copy(update={"raw_proposal": merged.model_dump(mode="json")})


class _PairedAppraisalMaterializer:
    """Private Appraisal/Affect materializer for the unified engine."""

    supports_immediate_emotion = True

    def __init__(self, owner: "_InboundCharacterAuthor") -> None:
        self._owner = owner

    def has_hedge_provider(self, _request: ModelInput) -> bool:
        """Keep the installed role recovery model out of speculative races.

        This provider is the independent author of last resort after an actual
        primary failure.  Starting it merely because the ordinary author is
        still working and then cancelling it can leave the provider generating
        server-side.  Its conservative capacity gate must then cool down, which
        would make the next real recovery unavailable.
        """

        return False

    def source_closure_review_enabled(self) -> bool:
        """Appraisal never authorizes the paired expression bytes.

        Source closure belongs to the later expression authority seam.  Telling
        Deliberation that the appraisal candidate performs this review both
        reserves the wrong deadline and pays to review bytes that are normally
        discarded after Appraisal acceptance advances the pinned request.
        """

        return False

    def shadow_observer_provider_available(self, _request: ModelInput) -> bool:
        """A diagnostic shadow requires its explicitly isolated provider."""

        return self._owner._expression_episode_observer is not None

    async def propose_shadow_observer(self, request: ModelInput) -> ModelOutput:
        return await self._owner._propose_shadow_episode_candidate(request)

    def accept_candidate(self, request: ModelInput) -> None:
        self._owner._accept_candidate_pending(request)

    def discard_candidate(self, request: ModelInput) -> None:
        self._owner._discard_candidate_pending(request)

    async def propose(self, request: ModelInput) -> ModelOutput:
        return await self._owner._propose_appraisal(request)


class _PairedExpressionMaterializer:
    """Private Expression materializer for the unified engine."""

    def __init__(self, owner: "_InboundCharacterAuthor") -> None:
        self._owner = owner

    def has_hedge_provider(self, _request: ModelInput) -> bool:
        """Reserve formal expression recovery for an observed author failure."""

        return False

    def source_closure_review_enabled(self) -> bool:
        """Reserve the alternate author slot for the factual truth boundary."""

        return self._owner._source_closure_reviewer is not None

    def stream_provider_available(self, _request: ModelInput) -> bool:
        adapter = self._owner._routed_expression
        available = getattr(adapter, "stream_provider_available", None)
        if callable(available):
            return bool(available(_request))
        available = getattr(adapter, "expression_unit_stream_available", None)
        return bool(callable(available) and available())

    def shadow_observer_provider_available(self, _request: ModelInput) -> bool:
        """Never infer observation capacity from the formal recovery lane."""

        return self._owner._expression_episode_observer is not None

    def install_recall_coordinator(self, coordinator: RecallCoordinator) -> None:
        self._owner.install_recall_coordinator(coordinator)

    def delegate_recall_to_character_interior(self) -> None:
        """Retire this adapter's Recall lifecycle for the Interior main path."""

        self._owner.delegate_recall_to_character_interior()

    def character_interior_owns_recall(self) -> bool:
        return (
            self._owner._character_interior_recall_delegate
            and self._owner._recall is None
        )

    async def propose_stream_head(self, request: ModelInput) -> ModelOutput:
        return await self._owner._routed_expression.propose_stream_head(request)

    async def propose_stream_tail(self, request: ModelInput) -> ModelOutput:
        return await self._owner._routed_expression.propose_stream_tail(request)

    def advance_expression_attention(self, attention_ref: str) -> None:
        self._owner._routed_expression.advance_expression_attention(attention_ref)

    async def propose_shadow_observer(self, request: ModelInput) -> ModelOutput:
        """Observe one candidate through the explicitly isolated client."""

        return await self._owner._propose_shadow_episode_candidate(request)

    def accept_candidate(self, request: ModelInput) -> None:
        self._owner._routed_expression.accept_candidate(request)

    def discard_candidate(self, request: ModelInput) -> None:
        self._owner._routed_expression.discard_candidate(request)

    def bind_same_call_paired_request(self, request: ModelInput) -> ModelInput:
        """Return the exact request image retained by the combined call.

        CharacterInterior now consumes Appraisal and Expression inside one
        purpose Faculty before the outer Deliberation can accept a candidate.
        Promote that process-local candidate internally and preserve the exact
        compact Context used by the provider.  Selective Recall is owned by
        CharacterInterior and is never installed on this adapter. This method has
        no provider or ledger side effect; it exists only so the second typed
        materializer cannot mistake formatting compaction for a new semantic
        opportunity and launch another role call.
        """

        key = _cache_key(request)
        candidate = self._owner._candidate_pending.pop((key, request.call_id), None)
        if candidate is not None:
            self._owner._pending[key] = candidate
            self._owner._pending.move_to_end(key)
            while len(self._owner._pending) > _MAX_PENDING_DRAFTS:
                self._owner._pending.popitem(last=False)
            self._owner._discard_other_candidate_pending(key)
        pending = self._owner._pending.get(key)
        if pending is None:
            return request
        # Selective recall is owned by CharacterInterior. A paired request may
        # therefore carry only the already-pinned provider Context here; no
        # second recall trace is appended by the expression materializer.
        if pending.recall_trace is not None or pending.prefetch_trace is not None:
            raise RuntimeError(
                "paired inbound recall must be owned by CharacterInterior"
            )
        return request.model_copy(
            update={"model_content_json": pending.private_state_context_json}
        )

    async def propose(self, request: ModelInput) -> ModelOutput:
        key = _cache_key(request)
        pending = self._owner._pending.pop(key, None)
        if pending is None:
            failed_key = _failed_cache_key(request)
            if key in self._owner._terminal_authored_expression_combined:
                raise ValidationTechnicalFailure("authored_expression_reselection_invalid")
            if key in self._owner._terminal_failed_combined:
                raise ValidationTechnicalFailure("recall_choice_reselection_invalid")
            if failed_key in self._owner._failed_combined:
                self._owner._failed_combined.discard(failed_key)
                repaired = await self._owner._retry_failed_expression_before_failsafe(
                    request,
                    failed_key,
                )
                if repaired is not None:
                    return repaired
                raise ValidationTechnicalFailure(
                    "paired_expression_reselection_invalid",
                    attempted_model_id=self._owner._model_id_for(request),
                    attempted_model_version=self._owner.VERSION,
                )
            raise ValidationTechnicalFailure(
                "paired_expression_missing",
                attempted_model_id=self._owner._model_id_for(request),
                attempted_model_version=self._owner.VERSION,
            )
        carried_recall_trace = pending.recall_trace
        carried_prefetch_trace = pending.prefetch_trace
        carried_presented_prefetch_traces = pending.presented_prefetch_traces
        target_cursor = RecallCursor(
            world_revision=request.evaluated_world_revision,
            deliberation_revision=request.evaluated_deliberation_revision,
            ledger_sequence=request.evaluated_ledger_sequence,
        )
        if carried_recall_trace is not None:
            if self._owner._recall is None:
                raise ValueError("paired recall runtime is unavailable")
            carried_recall_trace = self._owner._recall.carry_forward(
                carried_recall_trace,
                evaluated_cursor=target_cursor,
                trigger_ref=request.trigger_ref,
            )
        if carried_prefetch_trace is not None:
            if self._owner._recall is None:
                raise ValueError("paired prefetch runtime is unavailable")
            carried_prefetch_trace = self._owner._recall.carry_forward(
                carried_prefetch_trace,
                evaluated_cursor=target_cursor,
                trigger_ref=request.trigger_ref,
            )
        if carried_presented_prefetch_traces:
            if self._owner._recall is None:
                raise ValueError("paired prefetch presentation runtime is unavailable")
            carried_presented_prefetch_traces = tuple(
                presentation.model_copy(
                    update={
                        "trace": self._owner._recall.carry_forward(
                            presentation.trace,
                            evaluated_cursor=target_cursor,
                            trigger_ref=request.trigger_ref,
                        )
                    }
                )
                for presentation in carried_presented_prefetch_traces
            )
        # The cached draft is bound to the compact provider Context retained by
        # the simultaneous author call. Direct parser/materializer tests may
        # omit ``bind_same_call_paired_request``; using the retained Context
        # here preserves the same identity without opening another author path.
        model_content_json = pending.private_state_context_json
        for trace in (carried_prefetch_trace, carried_recall_trace):
            if trace is not None:
                model_content_json = augment_model_content_with_recall(
                    model_content_json,
                    verify_trusted_recall_trace(trace),
                )
        if carried_prefetch_trace is not None or carried_recall_trace is not None:
            model_content_json = mark_recall_budget_consumed(model_content_json)
        expression_request = request.model_copy(update={"model_content_json": model_content_json})
        expression_request_hash = _model_input_request_hash(expression_request)
        origin_identity_changed = (
            pending.origin_call_id != expression_request.call_id
            or pending.origin_request_hash != expression_request_hash
        )
        if origin_identity_changed:
            # A cached expression belongs only to the simultaneous author call
            # that produced its Appraisal. Re-authoring Expression alone at a
            # later identity would recreate the retired split-author path.
            raise ValidationTechnicalFailure(
                "paired_expression_origin_changed",
                model_call_id=pending.winning_model_call_id,
                request_hash=pending.winning_request_hash,
                attempted_model_id=pending.model_id,
                attempted_model_version=self._owner.VERSION,
                usage=pending.usage,
            )
        usage = pending.usage
        winning_model_call_id = pending.winning_model_call_id
        winning_request_hash = pending.winning_request_hash
        winning_model_id = pending.model_id
        winning_episode_disposition = pending.episode_disposition
        try:
            proposal = materialize_expression_draft(
                raw=pending.raw,
                request=expression_request,
                capabilities=self._owner._capabilities,
                quick_recovery=False,
                stable_identity_source_refs=self._owner._stable_identity_source_refs,
                private_state_context_json=pending.private_state_context_json,
                source_ref_aliases=pending.source_ref_aliases,
                require_explicit_authored_decision_fields=(
                    self._owner._require_explicit_authored_decision_fields
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValidationTechnicalFailure(
                "paired_expression_materialization_changed",
                model_call_id=pending.winning_model_call_id,
                request_hash=pending.winning_request_hash,
                attempted_model_id=pending.model_id,
                attempted_model_version=self._owner.VERSION,
                usage=pending.usage,
            ) from exc
        reviewer = self._owner._source_closure_reviewer
        if reviewer is not None:
            report_relative_adjudication_used = False
            review_result = await review_expression_with_candidate_external_coverage(
                reviewer=reviewer,
                inventory_model=self._owner._candidate_external_proposition_inventory_model,
                report_relative_reviewer=self._owner._report_relative_reviewer,
                request=expression_request,
                raw=pending.raw,
                identity_frame=self._owner._identity_frame,
                model_visible_context_json=pending.private_state_context_json,
                source_ref_aliases=pending.source_ref_aliases,
            )
            report_relative_adjudication_used = review_result.report_relative_adjudication_used
            if review_result.usage is not None:
                usage = _combine_usage(
                    usage,
                    review_result.usage,
                    expression_request.call_id,
                )
            review = review_result.review
            if review is not None and review.decision == "unsupported":
                _trace_source_closure_rejection(
                    stage="initial_rejection",
                    raw=pending.raw,
                    review=review,
                )
            if review is not None and review.decision == "unsupported":
                violation = source_closure_violation(review)
                if pending.corrective_spent:
                    _trace_source_closure_rejection(
                        stage="reselection_not_attempted",
                        raw=pending.raw,
                        review=review,
                    )
                    raise ValidationTechnicalFailure(
                        "authored_expression_reselection_invalid",
                        model_call_id=winning_model_call_id,
                        request_hash=winning_request_hash,
                        attempted_model_id=winning_model_id,
                        attempted_model_version=self._owner.VERSION,
                        usage=usage,
                    ) from ValueError(violation)
                if not begin_validation_reselection_recovery():
                    _trace_source_closure_rejection(
                        stage="reselection_not_attempted",
                        raw=pending.raw,
                        review=review,
                    )
                    raise ValueError(violation)
                repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
                if repair_timeout is None:
                    _trace_source_closure_rejection(
                        stage="reselection_not_attempted",
                        raw=pending.raw,
                        review=review,
                    )
                    raise ValueError(violation)
                repair_request = expression_request.model_copy(
                    update={"model_content_json": pending.private_state_context_json}
                )
                repair_messages = self._owner._selected_expression(expression_request)._messages(  # noqa: SLF001 - paired cognition owns this internal seam
                    request=repair_request,
                    quick_recovery=False,
                    provisional=False,
                    failure_code=None,
                    source_ref_aliases=pending.source_ref_aliases,
                )
                repaired_result = await self._owner._repair_expression_claims(
                    request=expression_request,
                    provider=pending.author_provider,
                    messages=repair_messages,
                    raw=pending.raw,
                    violation=violation,
                    source_closure_review=review,
                    combined=False,
                    timeout_seconds=repair_timeout,
                    private_state_context_json=pending.private_state_context_json,
                    source_ref_aliases=pending.source_ref_aliases,
                )
                if repaired_result is None:
                    raise ValueError(violation)
                usage = _combine_usage(
                    usage,
                    repaired_result.usage,
                    expression_request.call_id,
                )
                reselection_lane = self._owner._source_closure_reselection_lane
                corrected_reviewer = reviewer
                corrected_inventory = (
                    self._owner._candidate_external_proposition_inventory_model
                )
                corrected_report_relative_reviewer = self._owner._report_relative_reviewer
                if repaired_result.source_closure_lane_used:
                    if reselection_lane is None:
                        raise ValueError("paired source correction route was not retained")
                    corrected_reviewer = reselection_lane.reviewer
                    corrected_inventory = reselection_lane.inventory_model
                    corrected_report_relative_reviewer = (
                        reselection_lane.report_relative_reviewer
                    )
                corrected_review_result = await review_expression_with_candidate_external_coverage(
                    reviewer=corrected_reviewer,
                    inventory_model=corrected_inventory,
                    report_relative_reviewer=corrected_report_relative_reviewer,
                    request=expression_request,
                    raw=repaired_result.raw,
                    identity_frame=self._owner._identity_frame,
                    model_visible_context_json=pending.private_state_context_json,
                    source_ref_aliases=pending.source_ref_aliases,
                    # The repaired raw has a distinct author invocation and
                    # therefore its own single narrow-review allowance.
                    allow_report_relative_adjudication=True,
                )
                report_relative_adjudication_used = (
                    report_relative_adjudication_used
                    or corrected_review_result.report_relative_adjudication_used
                )
                if corrected_review_result.usage is not None:
                    usage = _combine_usage(
                        usage,
                        corrected_review_result.usage,
                        expression_request.call_id,
                    )
                corrected_review = corrected_review_result.review
                if corrected_review is not None and corrected_review.decision == "unsupported":
                    _trace_source_closure_rejection(
                        stage="corrected_rejection",
                        raw=repaired_result.raw,
                        review=corrected_review,
                    )
                    raise ValidationTechnicalFailure(
                        "authored_expression_reselection_invalid",
                        model_call_id=repaired_result.winning_model_call_id,
                        request_hash=repaired_result.winning_request_hash,
                        attempted_model_id=(
                            repaired_result.winning_model_id or pending.model_id
                        ),
                        attempted_model_version=self._owner.VERSION,
                        usage=usage,
                    ) from ValueError(source_closure_violation(corrected_review))
                proposal = materialize_expression_draft(
                    raw=repaired_result.raw,
                    request=expression_request,
                    capabilities=self._owner._capabilities,
                    quick_recovery=False,
                    stable_identity_source_refs=self._owner._stable_identity_source_refs,
                    private_state_context_json=pending.private_state_context_json,
                    source_ref_aliases=pending.source_ref_aliases,
                    require_explicit_authored_decision_fields=(
                        self._owner._require_explicit_authored_decision_fields
                    ),
                )
                if (
                    repaired_result.winning_model_call_id is None
                    or repaired_result.winning_request_hash is None
                ):
                    raise ValueError("paired source correction omitted provider identity")
                winning_model_call_id = repaired_result.winning_model_call_id
                winning_request_hash = repaired_result.winning_request_hash
                winning_model_id = repaired_result.winning_model_id or pending.model_id
                winning_episode_disposition = repaired_result.episode_disposition
        if winning_episode_disposition is not None:
            proposal = {
                **proposal,
                "episode_disposition": winning_episode_disposition,
            }
        return ModelOutput(
            model_id=winning_model_id,
            model_version=self._owner.VERSION,
            raw_proposal=proposal,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            usage=usage,
            winning_model_call_id=winning_model_call_id,
            winning_request_hash=winning_request_hash,
            episode_disposition=winning_episode_disposition,
            recall_trace=carried_recall_trace,
            prefetch_trace=carried_prefetch_trace,
            presented_prefetch_traces=carried_presented_prefetch_traces,
        )



class _InboundCharacterAuthor:
    """Author one source-bound inbound decision behind CharacterInterior.

    A normal text turn performs one provider call and returns one inert merged
    proposal.  Appraisal/Affect and Expression cannot be injected into
    production as independent protagonist authors.
    """

    VERSION = "character-interior-inbound-author.1"

    @property
    def author_identity(self) -> Mapping[str, object]:
        """The same logical character identity used by every purpose Faculty."""

        return {
            **self._semantic_author_identity,
            "name": "inbound-turn-faculty",
            "version": self.VERSION,
        }

    def __init__(
        self,
        *,
        flash_model: ChatCompletionModel,
        thinking_model: ChatCompletionModel | None = None,
        source_closure_model: ChatCompletionModel | None = None,
        report_relative_source_closure_model: ChatCompletionModel | None = None,
        candidate_external_proposition_inventory_model: ChatCompletionModel | None = None,
        source_closure_reselection_lane: SourceClosureReselectionLane | None = None,
        expression_episode_observer_model: ChatCompletionModel | None = None,
        contextual_failsafe_model: ChatCompletionModel | None = None,
        contextual_failsafe_reviewer_model: ChatCompletionModel | None = None,
        contextual_failsafe_enabled: bool = False,
        flash_model_id: str | None = None,
        thinking_model_id: str | None = None,
        temperature: float = 0.7,
        expression_capabilities: ExpressionDraftCapabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES,
        identity_frame: CompanionIdentityFrame | None = None,
        require_explicit_authored_decision_fields: bool = False,
    ) -> None:
        self._flash_model = flash_model
        self._thinking_model = thinking_model
        self._source_closure_reselection_lane = source_closure_reselection_lane
        if expression_episode_observer_model is not None and any(
            expression_episode_observer_model is provider
            for provider in (flash_model, thinking_model)
            if provider is not None
        ):
            raise ValueError("expression episode observer must use an independent provider client")
        observer_resources = _provider_runtime_resource_ids(expression_episode_observer_model)
        formal_resources = frozenset().union(
            *(
                _provider_runtime_resource_ids(provider)
                for provider in (flash_model, thinking_model)
            )
        )
        if observer_resources & formal_resources:
            raise ValueError(
                "expression episode observer must not share client, capacity, or circuit state"
            )
        self._expression_episode_observer_model = expression_episode_observer_model
        self._flash_id = (
            flash_model_id or str(getattr(flash_model, "model", "single-call-flash"))
        )[:256]
        self._thinking_id = thinking_model_id or (
            str(getattr(thinking_model, "model", "single-call-thinking"))
            if thinking_model
            else None
        )
        self._semantic_author_identity = character_semantic_author_identity(
            model_id=self._flash_id,
            model_version=str(getattr(flash_model, "model_version", self._flash_id)),
        )
        self._temperature = temperature
        self._capabilities = expression_capabilities
        self._require_explicit_authored_decision_fields = require_explicit_authored_decision_fields
        self._identity_frame = identity_frame
        # Semantic source-closure review is an explicit deployment capability,
        # not an implicit consequence of installing a local appraisal model.
        # A synchronous reviewer on every ordinary turn doubled provider
        # latency and converted otherwise valid replies into deadline misses.
        # The normal source-token/materializer boundary remains active; an
        # explicitly installed reviewer can still run the bounded correction
        # path for experiments or higher-risk lanes.
        resolved_source_closure_model = source_closure_model
        self._source_closure_reviewer = resolved_source_closure_model
        self._report_relative_reviewer = report_relative_source_closure_model
        self._candidate_external_proposition_inventory_model = (
            candidate_external_proposition_inventory_model
        )
        self._recall: RecallCoordinator | None = None
        self._character_interior_recall_delegate = False
        recovery_contexts = _ExpressionRecoveryContextStore()
        self._flash_expression = _ExpressionDraftWire(
            model=flash_model,
            model_id=self._flash_id,
            temperature=temperature,
            expression_capabilities=expression_capabilities,
            identity_frame=identity_frame,
            semantic_boundary_reviewer=flash_model,
            source_closure_reviewer=resolved_source_closure_model,
            report_relative_reviewer=report_relative_source_closure_model,
            candidate_external_proposition_inventory_model=(
                candidate_external_proposition_inventory_model
            ),
            source_closure_reselection_lane=source_closure_reselection_lane,
            recovery_context_store=recovery_contexts,
            require_explicit_authored_decision_fields=(require_explicit_authored_decision_fields),
        )
        self._thinking_expression = (
            _ExpressionDraftWire(
                model=thinking_model,
                model_id=self._thinking_id,
                temperature=temperature,
                expression_capabilities=expression_capabilities,
                identity_frame=identity_frame,
                semantic_boundary_reviewer=flash_model,
                source_closure_reviewer=resolved_source_closure_model,
                report_relative_reviewer=report_relative_source_closure_model,
                candidate_external_proposition_inventory_model=(
                    candidate_external_proposition_inventory_model
                ),
                source_closure_reselection_lane=source_closure_reselection_lane,
                recovery_context_store=recovery_contexts,
                require_explicit_authored_decision_fields=(
                    require_explicit_authored_decision_fields
                ),
            )
            if thinking_model is not None
            else None
        )
        self._routed_expression = _RoutedExpressionDraftWire(
            flash_model=flash_model,
            thinking_model=thinking_model,
            flash_model_id=self._flash_id,
            thinking_model_id=self._thinking_id,
            temperature=temperature,
            expression_capabilities=expression_capabilities,
            identity_frame=identity_frame,
            source_closure_reviewer=resolved_source_closure_model,
            report_relative_reviewer=report_relative_source_closure_model,
            candidate_external_proposition_inventory_model=(
                candidate_external_proposition_inventory_model
            ),
            source_closure_reselection_lane=source_closure_reselection_lane,
            recovery_context_store=recovery_contexts,
            require_explicit_authored_decision_fields=(require_explicit_authored_decision_fields),
        )
        self._expression_episode_observer = (
            _ExpressionDraftWire(
                model=expression_episode_observer_model,
                model_id=str(
                    getattr(
                        expression_episode_observer_model,
                        "model",
                        "expression-episode-observer",
                    )
                ),
                temperature=temperature,
                expression_capabilities=expression_capabilities,
                identity_frame=identity_frame,
                semantic_boundary_reviewer=None,
                # A provisional observer never receives Recall or semantic
                # review authority. Its draft still crosses the same
                # deterministic source-ref/materializer boundary, while all
                # provider-backed review capacity stays with the formal lane.
                source_closure_reviewer=None,
                report_relative_reviewer=None,
                candidate_external_proposition_inventory_model=None,
                # Observer failures and retries must never publish transient
                # recovery material into the authoritative recovery lane.
                recovery_context_store=_ExpressionRecoveryContextStore(),
                require_explicit_authored_decision_fields=(
                    require_explicit_authored_decision_fields
                ),
            )
            if expression_episode_observer_model is not None
            else None
        )
        if contextual_failsafe_enabled and (
            contextual_failsafe_model is None or contextual_failsafe_reviewer_model is None
        ):
            raise ValueError("contextual failsafe requires separate generation and reviewer models")
        if (
            contextual_failsafe_enabled
            and contextual_failsafe_model is contextual_failsafe_reviewer_model
        ):
            raise ValueError("contextual failsafe generation and reviewer must be independent")
        if contextual_failsafe_enabled:
            generator_identity = str(getattr(contextual_failsafe_model, "model", "")).strip()
            reviewer_identity = str(
                getattr(contextual_failsafe_reviewer_model, "model", "")
            ).strip()
            if generator_identity and generator_identity == reviewer_identity:
                raise ValueError("contextual failsafe reviewer must use a distinct model identity")
        self._contextual_failsafe_expression = (
            _ExpressionDraftWire(
                model=contextual_failsafe_model,
                model_id=(
                    "contextual-failure-recovery:"
                    + str(
                        getattr(
                            contextual_failsafe_model,
                            "model",
                            type(contextual_failsafe_model).__name__,
                        )
                    )
                )[:256],
                temperature=temperature,
                expression_capabilities=expression_capabilities,
                identity_frame=identity_frame,
                semantic_boundary_reviewer=None,
                recovery_prompt_mode="contextual_failure",
                contextual_grounding_reviewer=contextual_failsafe_reviewer_model,
                recovery_context_store=recovery_contexts,
                require_explicit_authored_decision_fields=(
                    require_explicit_authored_decision_fields
                ),
            )
            if contextual_failsafe_enabled
            else None
        )
        self._pending: OrderedDict[tuple[str, ...], _PendingExpression] = OrderedDict()
        self._candidate_pending: OrderedDict[tuple[tuple[str, ...], str], _PendingExpression] = (
            OrderedDict()
        )
        self._failed_combined = _BoundedKeySet(_MAX_PENDING_DRAFTS)
        self._terminal_failed_combined = _BoundedKeySet(_MAX_PENDING_DRAFTS)
        self._terminal_authored_expression_combined = _BoundedKeySet(_MAX_PENDING_DRAFTS)
        self._failed_details: OrderedDict[tuple[str, ...], _FailedExpressionDetail] = OrderedDict()
        self._interior_streams: OrderedDict[
            tuple[str, ...], _CombinedInteriorStreamProvider
        ] = OrderedDict()
        # These are wire materializers, not independently composable semantic
        # authors.  Production never receives either reference; direct access
        # remains private for the parser/source-closure contract corpus only.
        self._appraisal_materializer = _PairedAppraisalMaterializer(self)
        self._expression_materializer = _PairedExpressionMaterializer(self)

    @property
    def _stable_identity_source_refs(self) -> frozenset[str]:
        if self._identity_frame is None:
            return frozenset()
        return frozenset(companion_identity_source_refs(self._identity_frame).values())

    def install_recall_coordinator(self, coordinator: RecallCoordinator) -> None:
        if self._character_interior_recall_delegate:
            raise ValueError(
                "CharacterInterior-owned inbound cognition cannot install a second Recall lifecycle"
            )
        if (
            self._recall is not None
            and self._recall is not coordinator
            and not self._recall.is_closed
        ):
            raise ValueError("paired cognition recall coordinator is already installed")
        self._recall = coordinator
        self._flash_expression.install_recall_coordinator(coordinator)
        if self._thinking_expression is not None:
            self._thinking_expression.install_recall_coordinator(coordinator)
        self._routed_expression.install_recall_coordinator(coordinator)

    def delegate_recall_to_character_interior(self) -> None:
        """Enable Recall choices while prohibiting local retrieval execution."""

        if self._recall is not None:
            raise ValueError(
                "paired cognition already owns Recall and cannot join CharacterInterior"
            )
        self._character_interior_recall_delegate = True

    def character_interior_owns_recall(self) -> bool:
        """Report that selective retrieval execution belongs to the outer core."""

        return self._character_interior_recall_delegate and self._recall is None

    async def propose(self, request: ModelInput) -> ModelOutput:
        """Return one merged Expression+Appraisal+optional Affect decision."""

        appraisal_output = await self._appraisal_materializer.propose(request)
        expression_input = self._expression_materializer.bind_same_call_paired_request(request)
        expression_output = await self._expression_materializer.propose(expression_input)
        return _merge_cognition_outputs(
            appraisal=appraisal_output,
            expression=expression_output,
        )

    async def correct_role_result(
        self,
        request: ModelInput,
        failure_code: str | None,
    ) -> ModelOutput:
        """Let this exact author replace one malformed outer role result.

        Ordinary Expression parsing already performs its own bounded repair.
        This outer guard is a fail-closed defense for a result that somehow
        crossed that parser without a complete final private state. It never
        selects another provider or changes the pinned World context.
        """

        if not failure_code:
            raise ValueError("character interior correction failure code is missing")
        return await self.propose(request)

    def stream_provider_available(self, request: ModelInput) -> bool:
        """Report only the selected provider's real streaming transport."""

        return self._routed_expression.stream_provider_available(request)

    async def propose_stream_head(self, request: ModelInput) -> ModelOutput:
        """Resolve one simultaneous appraisal plus the first expression unit."""

        if not self.stream_provider_available(request):
            raise RuntimeError("character interior stream provider is unavailable")
        route = self._routed_expression._route(request)  # noqa: SLF001
        stream = _CombinedInteriorStreamProvider(
            request=request,
            provider=self._selected_provider(request),
            stream_adapter=route,
            temperature=self._temperature,
            model_version=self.VERSION,
        )
        key = _cache_key(request)
        previous = self._interior_streams.pop(key, None)
        if previous is not None:
            previous.cancel()
        self._interior_streams[key] = stream
        self._interior_streams.move_to_end(key)
        while len(self._interior_streams) > 32:
            self._interior_streams.popitem(last=False)

        try:
            appraisal_output = await self._propose_appraisal(
                request,
                transport_provider=stream,
            )
            expression_input = self._expression_materializer.bind_same_call_paired_request(request)
            expression_output = await self._expression_materializer.propose(expression_input)
        except asyncio.CancelledError:
            raise
        except ValidationTechnicalFailure as exc:
            if stream.retirement is not None:
                exc.physical_provider_audits = (
                    *exc.physical_provider_audits,
                    stream.retirement,
                )
            raise
        except Exception as exc:
            if stream.retirement is None:
                raise
            raise ValidationTechnicalFailure(
                (
                    "authored_subcall_timeout"
                    if isinstance(exc, TimeoutError)
                    else "authored_subcall_exception"
                ),
                attempted_model_id=str(
                    getattr(self._selected_provider(request), "model", self._model_id_for(request))
                ),
                attempted_model_version=self.VERSION,
                physical_provider_audits=(stream.retirement,),
            ) from exc
        merged = _merge_cognition_outputs(
            appraisal=appraisal_output,
            expression=expression_output,
        )
        provider_identity = stream.provider_identity
        if any(
            output.winning_model_call_id != provider_identity.model_call_id
            or output.winning_request_hash != provider_identity.request_hash
            for output in (appraisal_output, expression_output)
        ):
            # The same character's bounded correction replaced the rejected
            # stream before any head was authorized.  Return that complete
            # corrected decision as a normal result and make the speculative
            # original continuation permanently unavailable. The corrected
            # candidate is an independent author call: it carries no stream
            # physical terminal, so the strict audit's tail binding does not
            # apply (2026-08-08; the retired stream's own failure is already
            # recorded on the rejected attempt).
            self._interior_streams.pop(key, None)
            return merged.model_copy(
                update={
                    "physical_provider_audits": (
                        (stream.retirement,) if stream.retirement is not None else ()
                    ),
                }
            )
        unit_identity = _stream_unit_identity(provider_identity, "head")
        return merged.model_copy(
            update={
                "usage": None,
                "input_tokens": None,
                "output_tokens": None,
                "winning_model_call_id": unit_identity.model_call_id,
                "winning_request_hash": unit_identity.request_hash,
                "provider_parent_model_call_id": provider_identity.model_call_id,
                "semantic_stream_part": "head",
                "physical_provider_audits": (),
            }
        )

    async def propose_stream_tail(self, request: ModelInput) -> ModelOutput:
        """Materialize later bytes from the already-authored InnerTurn."""

        key = _cache_key(request)
        stream = self._interior_streams.get(key)
        if stream is None:
            raise RuntimeError("character interior stream continuation is unavailable")
        current_task = asyncio.current_task()
        if current_task is not None:
            def retire_stream(_task: asyncio.Task[object]) -> None:
                if self._interior_streams.get(key) is stream:
                    self._interior_streams.pop(key, None)
                stream.cancel()

            current_task.add_done_callback(retire_stream)
        raw, usage, complete_raw = await stream.tail(request)
        combined = _parse_combined(raw)
        head = _parse_combined(stream.head_raw)
        if combined["appraisal_draft"] != head["appraisal_draft"]:
            raise ValueError("character interior stream changed its frozen appraisal")

        private_state_context_json = compact_chat_model_facing_context(
            request.model_content_json
        )
        source_ref_aliases = build_source_ref_alias_table(
            request=request,
            stable_identity_source_refs=self._stable_identity_source_refs,
            model_visible_context_json=private_state_context_json,
        )
        expression_raw = json.dumps(
            combined["expression_draft"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        expression_raw, episode_disposition = _split_expression_episode_disposition(
            expression_raw,
            provisional=False,
        )
        proposal = materialize_expression_draft(
            raw=expression_raw,
            request=request,
            capabilities=self._capabilities,
            quick_recovery=False,
            stable_identity_source_refs=self._stable_identity_source_refs,
            private_state_context_json=private_state_context_json,
            source_ref_aliases=source_ref_aliases,
            require_explicit_authored_decision_fields=(
                self._require_explicit_authored_decision_fields
            ),
        )
        provider_identity = stream.provider_identity
        head_identity = _stream_unit_identity(provider_identity, "head")
        tail_identity = _stream_unit_identity(provider_identity, "tail")
        provider_subcall_audits = ()
        if self._source_closure_reviewer is not None:
            with _ProviderSubcallAuditCapture(
                owner_model_call_id=tail_identity.model_call_id,
                owner_request_hash=tail_identity.request_hash,
                owner_raw=expression_raw,
                owner_model_id=self._model_id_for(request),
                owner_model_version=self.VERSION,
                purpose="expression_stream_tail",
            ) as review_capture:
                try:
                    review_result = (
                        await review_expression_with_candidate_external_coverage(
                            reviewer=self._source_closure_reviewer,
                            inventory_model=(
                                self._candidate_external_proposition_inventory_model
                            ),
                            report_relative_reviewer=self._report_relative_reviewer,
                            request=request,
                            raw=expression_raw,
                            identity_frame=self._identity_frame,
                            model_visible_context_json=private_state_context_json,
                            source_ref_aliases=source_ref_aliases,
                        )
                    )
                except ValidationTechnicalFailure as exc:
                    provider_subcall_audits = review_capture.finalize(
                        additional_attempts=exc.provider_subcall_audits,
                    )
                    raise ValidationTechnicalFailure(
                        exc.failure_code,
                        model_call_id=tail_identity.model_call_id,
                        request_hash=tail_identity.request_hash,
                        attempted_model_id=self._model_id_for(request),
                        attempted_model_version=self.VERSION,
                        # Physical author usage and reviewer usage retain
                        # separate immutable owners. The latter lives only on
                        # the provider-subcall audits finalized above.
                        usage=usage,
                        provider_subcall_audits=provider_subcall_audits,
                        authored_candidate_audits=exc.authored_candidate_audits,
                    ) from exc
                provider_subcall_audits = review_capture.finalize()
            review = review_result.review
            if review is not None and review.decision == "unsupported":
                raise ValidationTechnicalFailure(
                    "authored_expression_reselection_invalid",
                    model_call_id=tail_identity.model_call_id,
                    request_hash=tail_identity.request_hash,
                    attempted_model_id=self._model_id_for(request),
                    attempted_model_version=self.VERSION,
                    usage=usage,
                    provider_subcall_audits=provider_subcall_audits,
                ) from ValueError(source_closure_violation(review))

        physical = PhysicalProviderInvocationAudit(
            model_call_id=provider_identity.model_call_id,
            request_hash=provider_identity.request_hash,
            model_id=self._model_id_for(request),
            model_version=self.VERSION,
            outcome="completed",
            response_hash=sha256(complete_raw.encode("utf-8")).hexdigest(),
            usage_status=("provider_reported" if usage is not None else "unresolved"),
            usage=usage,
            semantic_model_call_ids=(
                head_identity.model_call_id,
                tail_identity.model_call_id,
            ),
        )
        if episode_disposition is not None:
            proposal = {**proposal, "episode_disposition": episode_disposition}
        return ModelOutput(
            model_id=self._model_id_for(request),
            model_version=self.VERSION,
            raw_proposal=proposal,
            winning_model_call_id=tail_identity.model_call_id,
            winning_request_hash=tail_identity.request_hash,
            provider_parent_model_call_id=provider_identity.model_call_id,
            semantic_stream_part="tail",
            physical_provider_audits=(physical,),
            provider_subcall_audits=provider_subcall_audits,
            episode_disposition=episode_disposition,
        )

    def advance_expression_attention(self, attention_ref: str) -> None:
        """Cancel every unfinished continuation before a newer Observation."""

        self._routed_expression.advance_expression_attention(attention_ref)
        self._interior_streams.clear()

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        """Expose only the explicit, default-off contextual failsafe.

        A provider/transport failure is not permission to ask a backup role to
        make a new character decision. Structural and source-closure
        reselection happen inside the original selected-author attempt. Once
        that attempt is technically unavailable, the normal result is a typed
        failure for durable retry. Deployments may separately enable the
        audited contextual failsafe described by ADR 0010.
        """

        self._pending.pop(_cache_key(request), None)
        contextual = self._contextual_failsafe_expression
        if contextual is None:
            raise ValidationTechnicalFailure(
                "inbound_character_author_unavailable",
                attempted_model_id=self._model_id_for(request),
                attempted_model_version=self.VERSION,
            )
        try:
            async with asyncio.timeout(_CONTEXTUAL_FAILSAFE_TIMEOUT_SECONDS):
                output = await contextual.recover(
                    request,
                    f"ordinary_routes_exhausted:{failure_code}"[:64],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "contextual failure recovery failed error_type=%s",
                type(exc).__name__,
            )
            if has_provider_slot_coordinator():
                failure_kind = (
                    "timeout"
                    if isinstance(exc, TimeoutError)
                    else "invalid"
                    if isinstance(exc, (TypeError, ValueError))
                    else "exception"
                )
                raise RecoveryCandidateFailure(failure_kind) from exc
            raise ValidationTechnicalFailure(
                "contextual_failsafe_unavailable",
                attempted_model_id=self._model_id_for(request),
                attempted_model_version=self.VERSION,
            ) from exc
        record_failsafe()
        return output.model_copy(update={"model_version": _CONTEXTUAL_FAILSAFE_VERSION})

    def _recall_available(self, request: ModelInput) -> bool:
        if not model_content_allows_recall(request.model_content_json):
            return False
        if self._character_interior_recall_delegate:
            return True
        return self._recall is not None and self._recall.is_available(
            RecallCursor(
                world_revision=request.evaluated_world_revision,
                deliberation_revision=request.evaluated_deliberation_revision,
                ledger_sequence=request.evaluated_ledger_sequence,
            ),
            trigger_ref=request.trigger_ref,
        )

    def _final_tool_reselection_kwargs(
        self,
        *,
        request: ModelInput,
        provider: ChatCompletionModel,
    ) -> dict[str, object]:
        """Return the decision-only transport for an already-consumed choice.

        Internal shape/source corrections and Recall follow-ups are final: the
        transport must not advertise another Recall branch. Offline fixtures
        without required-tool support retain the historical plain JSON seam.
        """

        if not bool(getattr(provider, "supports_required_tool_choice", False)):
            return {}
        contract = InboundToolContracts().contract_for(
            phase="final",
            transport="atomic",
            capabilities=self._capabilities,
            recall_allowed=False,
            require_turn_posture=(
                request.trigger_message is not None
                and request.trigger_message.turn_attention_advisory is not None
            ),
            schema_dialect=(
                "deepseek-strict"
                if bool(getattr(provider, "supports_strict_tool_choice", False))
                else "standard"
            ),
        )
        return {
            "tools": list(contract.provider_tools),
            "tool_choice": contract.provider_tool_choice,
            "tool_contract_identity": contract.identity.request_identity_material(),
            "unwrap_tool_result": contract.unwrap,
        }

    async def _propose_shadow_episode_candidate(self, request: ModelInput) -> ModelOutput:
        adapter = self._expression_episode_observer
        if adapter is None:
            raise RuntimeError("expression episode shadow observer is not configured")
        return await adapter.propose_provisional(request)

    def _accept_candidate_pending(self, request: ModelInput) -> None:
        key = _cache_key(request)
        pending = self._candidate_pending.pop((key, request.call_id), None)
        if pending is None:
            return
        self._pending[key] = pending
        self._pending.move_to_end(key)
        while len(self._pending) > _MAX_PENDING_DRAFTS:
            self._pending.popitem(last=False)
        self._discard_other_candidate_pending(key)

    def _discard_candidate_pending(self, request: ModelInput) -> None:
        key = _cache_key(request)
        self._candidate_pending.pop((key, request.call_id), None)

    def _discard_other_candidate_pending(self, key: tuple[str, ...]) -> None:
        for candidate_key in tuple(self._candidate_pending):
            if candidate_key[0] == key:
                self._candidate_pending.pop(candidate_key, None)

    def _selected_expression(self, request: ModelInput) -> _ExpressionDraftWire:
        if request.route.tier == "thinking":
            if self._thinking_expression is None:
                raise RuntimeError("thinking deliberation route is not configured")
            return self._thinking_expression
        return self._flash_expression

    def _selected_provider(self, request: ModelInput) -> ChatCompletionModel:
        if request.route.tier == "thinking":
            if self._thinking_model is None:
                raise RuntimeError("thinking deliberation route is not configured")
            return self._thinking_model
        return self._flash_model

    def _model_id_for(self, request: ModelInput) -> str:
        if request.route.tier == "thinking":
            if self._thinking_id is None:
                raise RuntimeError("thinking deliberation route is not configured")
            return self._thinking_id[:256]
        return self._flash_id

    def _model_id_for_provider(self, request: ModelInput, provider: ChatCompletionModel) -> str:
        inferred = str(getattr(provider, "model", "")).strip()
        return (inferred or self._model_id_for(request))[:256]

    async def _repair_expression_claims(
        self,
        *,
        request: ModelInput,
        provider: ChatCompletionModel,
        messages: list[dict[str, str]],
        raw: str,
        violation: object,
        source_closure_review: Any | None = None,
        combined: bool = True,
        timeout_seconds: float = _CLAIM_REPAIR_TIMEOUT_SECONDS,
        private_state_context_json: str | None = None,
        source_ref_aliases: SourceRefAliasTable | None = None,
    ) -> ValidationReselectionResult | None:
        """Spend one corrective call naming the exact bounded violation.

        Handles semantic source closure, claim-bookkeeping near-misses, and
        non-claim draft-shape rejects. Returns validated expression bytes, or
        ``None`` when the correction itself fails. This never loosens any
        boundary: the corrected draft still passes the full materializer, and
        only one role-model attempt is made.
        """

        if source_closure_review is not None and combined:
            raise ValidationTechnicalFailure(
                "authored_expression_reselection_invalid",
                attempted_model_id=self._model_id_for_provider(request, provider),
                attempted_model_version=self.VERSION,
            )
        is_private_state = is_private_turn_state_violation(violation)
        violation_text = str(violation)
        canonical_appraisal = (
            _canonical_appraisal_reselection_context(raw=raw, request=request)
            if combined and is_private_state
            else None
        )
        effective_source_ref_aliases = source_ref_aliases
        if source_closure_review is not None and effective_source_ref_aliases is None:
            effective_source_ref_aliases = build_source_ref_alias_table(
                request=request,
                stable_identity_source_refs=self._stable_identity_source_refs,
                model_visible_context_json=private_state_context_json,
            )
        if combined and is_private_state:
            if canonical_appraisal is None:
                shape = (
                    "one complete replacement JSON object with appraisal_draft and "
                    "expression_draft: select appraisal_draft from the pinned Context and "
                    "completely reselect expression_draft"
                )
            else:
                shape = (
                    "one complete replacement JSON object with appraisal_draft and "
                    "expression_draft: copy the immediately preceding canonical "
                    "appraisal_draft unchanged and completely reselect expression_draft"
                )
        elif combined and source_closure_review is not None:
            shape = (
                "one complete replacement JSON object with appraisal_draft and "
                "expression_draft: copy appraisal_draft unchanged and completely reselect "
                "expression_draft"
            )
        else:
            shape = (
                "the same JSON object shape (appraisal_draft and expression_draft)"
                if combined
                else "one complete replacement ExpressionDraft JSON object only"
            )
        is_claim = _is_world_claim_violation(violation_text)
        instruction = (
            _source_closure_reselection_envelope(
                raw=raw,
                review=source_closure_review,
                shape_line=shape,
                companion_life_authority_availability=(
                    _life_authority_availability_from_messages(messages)
                ),
                output_contract=expression_reselection_output_contract(
                    capabilities=self._capabilities,
                    allowed_source_ref_aliases=tuple(
                        sorted(
                            effective_source_ref_aliases.alias_for(source_ref) or source_ref
                            for source_ref in effective_source_ref_aliases.canonical_refs
                        )
                    )
                    if effective_source_ref_aliases is not None
                    else (),
                    world_claim_source_ref_aliases_by_scope=(
                        world_claim_source_ref_aliases_by_scope(
                            request=request,
                            stable_identity_source_refs=self._stable_identity_source_refs,
                            source_ref_aliases=effective_source_ref_aliases,
                            model_visible_context_json=private_state_context_json,
                        )
                        if effective_source_ref_aliases is not None
                        else {
                            scope: ()
                            for scope in (
                                "current_world",
                                "past_world",
                                "counterpart_history",
                                "shared_history",
                                "stable_identity",
                            )
                        }
                    ),
                    response_expectation_assessment_required=(
                        request_requires_response_expectation_assessment(request)
                    ),
                    provider_message_bound=bool(
                        request.trigger_message is not None
                        and request.trigger_message.platform_message_id
                    ),
                    combined=False,
                ),
            )
            if source_closure_review is not None
            else (
                private_turn_state_reselection_instruction(
                    violation_text,
                    shape_line=shape,
                )
                if is_private_state
                else (
                    claim_repair_instruction(violation_text, shape_line=shape)
                    if is_claim
                    else shape_repair_instruction(
                        violation_text,
                        shape_line=shape,
                        companion_life_authority_availability=(
                            _life_authority_availability_from_messages(messages)
                        ),
                    )
                )
            )
        )
        reselection_messages = [*messages]
        if canonical_appraisal is not None:
            reselection_messages.append({"role": "assistant", "content": canonical_appraisal})
        reselection_lane = (
            self._source_closure_reselection_lane
            if source_closure_review is not None
            else None
        )
        reselection_provider = (
            reselection_lane.author if reselection_lane is not None else provider
        )
        reselection_model_id = (
            reselection_lane.model_id
            if reselection_lane is not None
            else self._model_id_for_provider(request, provider)
        )
        expression_tool_kwargs = (
            _expression_tool_reselection_kwargs(
                request=request,
                provider=reselection_provider,
                capabilities=self._capabilities,
                stable_identity_source_refs=self._stable_identity_source_refs,
                source_ref_aliases=effective_source_ref_aliases,
            )
            if not combined
            else {}
        )
        try:
            reselection = await complete_bounded_validation_reselection(
                model=reselection_provider,
                messages=reselection_messages,
                raw=raw,
                instruction=instruction,
                temperature=(0.0 if source_closure_review is not None else 0.2),
                timeout_seconds=timeout_seconds,
                parent_call_id=request.call_id,
                include_invalid_raw=(source_closure_review is None and not is_private_state),
                model_id=reselection_model_id,
                source_closure_lane_used=reselection_lane is not None,
                **(
                    self._final_tool_reselection_kwargs(
                        request=request,
                        provider=reselection_provider,
                    )
                    if combined and reselection_lane is None
                    else expression_tool_kwargs
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if source_closure_review is not None:
                _trace_source_closure_rejection(
                    stage="reselection_provider_failed",
                    raw=raw,
                    review=source_closure_review,
                )
            correction_kind = (
                "source-closure"
                if source_closure_review is not None
                else ("world-claim" if is_claim else "draft-shape")
            )
            logger.warning(
                "%s corrective retry failed error_type=%s",
                correction_kind,
                type(exc).__name__,
            )
            if source_closure_review is not None:
                raise ValidationTechnicalFailure(
                    "authored_expression_reselection_invalid",
                    attempted_model_id=reselection_model_id,
                    attempted_model_version=self.VERSION,
                ) from exc
            return None

        corrected_raw = reselection.raw
        episode_disposition: str | None = None
        try:
            if source_closure_review is not None or expression_tool_kwargs:
                corrected_raw = normalize_realtime_expression_reselection_output(corrected_raw)
            if combined:
                corrected = _parse_combined(corrected_raw)
                expression_raw = json.dumps(
                    corrected["expression_draft"], ensure_ascii=False, separators=(",", ":")
                )
            else:
                expression_raw = corrected_raw
            expression_raw, episode_disposition = _split_expression_episode_disposition(
                expression_raw,
                provisional=False,
                allow_source_reselection_envelope=(
                    source_closure_review is not None and not combined
                ),
            )
            materialize_expression_draft(
                raw=expression_raw,
                request=request,
                capabilities=self._capabilities,
                quick_recovery=False,
                stable_identity_source_refs=self._stable_identity_source_refs,
                private_state_context_json=private_state_context_json,
                source_ref_aliases=source_ref_aliases,
                require_explicit_authored_decision_fields=(
                    self._require_explicit_authored_decision_fields
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if source_closure_review is not None:
                _trace_source_reselection_materialization_failure(
                    raw=corrected_raw,
                    error=exc,
                    stage="pre_final_source_review",
                )
                _trace_source_closure_rejection(
                    stage="reselection_output_invalid_before_review",
                    raw=corrected_raw,
                    review=source_closure_review,
                )
            correction_kind = (
                "source-closure"
                if source_closure_review is not None
                else ("world-claim" if is_claim else "draft-shape")
            )
            logger.warning(
                "%s corrective retry failed error_type=%s",
                correction_kind,
                type(exc).__name__,
            )
            if source_closure_review is not None:
                raise ValidationTechnicalFailure(
                    "authored_expression_reselection_invalid",
                    model_call_id=reselection.winning_model_call_id,
                    request_hash=reselection.winning_request_hash,
                    attempted_model_id=reselection_model_id,
                    attempted_model_version=self.VERSION,
                    usage=reselection.usage,
                ) from exc
            return None
        if source_closure_review is not None:
            logger.warning("source-closure corrective retry repaired the expression draft")
            record_source_closure_reselection()
        elif is_claim:
            logger.warning("world-claim corrective retry repaired the expression draft")
            record_claim_repair()
        else:
            logger.warning("draft-shape corrective retry repaired the expression draft")
            record_shape_repair()
        return ValidationReselectionResult(
            raw=expression_raw,
            usage=reselection.usage,
            corrective_used=True,
            winning_model_call_id=reselection.winning_model_call_id,
            winning_request_hash=reselection.winning_request_hash,
            winning_model_id=reselection_model_id,
            source_closure_lane_used=reselection_lane is not None,
            episode_disposition=episode_disposition,
        )

    async def _reselect_invalid_private_recall_choice(
        self,
        *,
        request: ModelInput,
        provider: ChatCompletionModel,
        messages: list[dict[str, str]],
        raw: str,
        violation: object,
        timeout_seconds: float = _CLAIM_REPAIR_TIMEOUT_SECONDS,
        private_state_context_json: str | None = None,
        source_ref_aliases: SourceRefAliasTable | None = None,
    ) -> ValidationReselectionResult | None:
        """Replace a malformed Recall choice with one final paired cognition.

        The corrective call consumes the turn's only secondary provider slot,
        so its result must be the final appraisal/expression envelope rather
        than another recall request.
        """

        canonical_appraisal = _canonical_appraisal_reselection_context(
            raw=raw,
            request=request,
        )
        appraisal_clause = (
            "copy the immediately preceding canonical appraisal_draft unchanged and "
            if canonical_appraisal is not None
            else "select appraisal_draft from the pinned Context and "
        )
        shape_line = (
            "one complete replacement JSON object with appraisal_draft and "
            f"expression_draft; {appraisal_clause}choose the final expression now "
            "without requesting another recall"
        )
        instruction = (
            recall_choice_reselection_instruction(
                violation,
                shape_line=shape_line,
            )
            if isinstance(violation, RecallChoiceValidationError)
            else private_turn_state_reselection_instruction(
                str(violation),
                shape_line=shape_line,
            )
        )
        reselection_messages = [*messages]
        if canonical_appraisal is not None:
            reselection_messages.append({"role": "assistant", "content": canonical_appraisal})
        try:
            reselection = await complete_bounded_validation_reselection(
                model=provider,
                messages=reselection_messages,
                raw=raw,
                instruction=instruction,
                temperature=self._temperature,
                timeout_seconds=timeout_seconds,
                parent_call_id=request.call_id,
                include_invalid_raw=False,
                **self._final_tool_reselection_kwargs(
                    request=request,
                    provider=provider,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "private recall-choice reselection failed error_type=%s",
                type(exc).__name__,
            )
            return None

        corrected_raw = reselection.raw
        episode_disposition: str | None = None
        try:
            corrected = _parse_combined(corrected_raw)
            expression_raw = json.dumps(
                corrected["expression_draft"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            expression_raw, episode_disposition = _split_expression_episode_disposition(
                expression_raw,
                provisional=False,
            )
            materialize_expression_draft(
                raw=expression_raw,
                request=request,
                capabilities=self._capabilities,
                quick_recovery=False,
                stable_identity_source_refs=self._stable_identity_source_refs,
                private_state_context_json=private_state_context_json,
                source_ref_aliases=source_ref_aliases,
                require_explicit_authored_decision_fields=(
                    self._require_explicit_authored_decision_fields
                ),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "private recall-choice reselection returned another invalid result error_type=%s",
                type(exc).__name__,
            )
            raise ValidationTechnicalFailure(
                "recall_choice_reselection_invalid",
                model_call_id=reselection.winning_model_call_id,
                request_hash=reselection.winning_request_hash,
                attempted_model_id=self._model_id_for_provider(request, provider),
                attempted_model_version=self.VERSION,
                usage=reselection.usage,
            ) from exc
        logger.warning("private recall-choice reselection produced a final paired cognition")
        record_shape_repair()
        return ValidationReselectionResult(
            raw=corrected_raw,
            usage=reselection.usage,
            corrective_used=True,
            winning_model_call_id=reselection.winning_model_call_id,
            winning_request_hash=reselection.winning_request_hash,
            episode_disposition=episode_disposition,
        )

    async def _retry_failed_expression_before_failsafe(
        self, request: ModelInput, key: tuple[str, ...]
    ) -> ModelOutput | None:
        """One violation-quoting main-provider retry before model-owned recovery.

        The paired pass failed structurally and its bounded in-attempt repair
        either did not fit the appraisal-lane budget or itself failed once.
        The person is now already waiting on the failure path, so spending a
        few more seconds on one corrective completion lets the same character
        model make a valid choice without introducing host-authored speech.
        Timeout-class failures never reach here: they leave no remembered
        violation, so this method returns ``None`` immediately for them.
        """

        detail = self._failed_details.pop(key, None)
        if detail is None:
            return None
        origin_identity_changed = (
            detail.origin_call_id != request.call_id
            or detail.origin_request_hash != _model_input_request_hash(request)
            or detail.origin_world_revision != request.evaluated_world_revision
            or detail.origin_deliberation_revision != request.evaluated_deliberation_revision
            or detail.origin_ledger_sequence != request.evaluated_ledger_sequence
        )
        if origin_identity_changed:
            # The retained messages and invalid bytes belong to the old
            # Pinned Turn.  They are not a recovery recipe for the same
            # Observation at a later cursor: author a new Expression from the
            # current Context and give that provider invocation its own audit.
            return await self._selected_expression(request).propose(request)
        repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
        if repair_timeout is None:
            return None
        provider = self._selected_provider(request)
        repaired_result = await self._repair_expression_claims(
            request=request,
            provider=provider,
            messages=detail.messages,
            raw=detail.raw,
            violation=detail.violation,
            combined=True,
            timeout_seconds=repair_timeout,
            private_state_context_json=detail.private_state_context_json,
            source_ref_aliases=detail.source_ref_aliases,
        )
        if repaired_result is None:
            return None
        usage = _combine_usage(detail.usage, repaired_result.usage, request.call_id)
        reviewer = self._source_closure_reviewer
        if reviewer is not None:
            review_result = await review_expression_with_candidate_external_coverage(
                reviewer=reviewer,
                inventory_model=self._candidate_external_proposition_inventory_model,
                report_relative_reviewer=self._report_relative_reviewer,
                request=request,
                raw=repaired_result.raw,
                identity_frame=self._identity_frame,
                model_visible_context_json=detail.private_state_context_json,
                source_ref_aliases=detail.source_ref_aliases,
            )
            if review_result.usage is not None:
                usage = _combine_usage(
                    usage,
                    review_result.usage,
                    request.call_id,
                )
            review = review_result.review
            if review is not None and review.decision == "unsupported":
                _trace_source_closure_rejection(
                    stage="corrected_rejection",
                    raw=repaired_result.raw,
                    review=review,
                )
                raise ValueError(source_closure_violation(review))
        if (
            repaired_result.winning_model_call_id is None
            or repaired_result.winning_request_hash is None
        ):
            raise ValueError("paired delayed shape correction omitted provider identity")
        logger.warning(
            "pre-failsafe corrective retry recovered a genuine expression trigger=%s",
            request.trigger_message.observation_ref
            if request.trigger_message is not None
            else request.trigger_ref,
        )
        return ModelOutput(
            model_id=self._model_id_for_provider(request, provider),
            model_version=self.VERSION,
            raw_proposal=materialize_expression_draft(
                raw=repaired_result.raw,
                request=request,
                capabilities=self._capabilities,
                quick_recovery=False,
                stable_identity_source_refs=self._stable_identity_source_refs,
                private_state_context_json=detail.private_state_context_json,
                source_ref_aliases=detail.source_ref_aliases,
                require_explicit_authored_decision_fields=(
                    self._require_explicit_authored_decision_fields
                ),
            ),
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            usage=usage,
            winning_model_call_id=repaired_result.winning_model_call_id,
            winning_request_hash=repaired_result.winning_request_hash,
        )

    async def _propose_appraisal(
        self,
        request: ModelInput,
        *,
        transport_provider: ChatCompletionModel | None = None,
    ) -> ModelOutput:
        trigger = request.trigger_message
        if trigger is None:
            raise ValidationTechnicalFailure(
                "inbound_character_turn_requires_verified_observation",
                attempted_model_id=self._model_id_for(request),
                attempted_model_version=self.VERSION,
            )

        expression_adapter = self._selected_expression(request)
        expected_cursor = RecallCursor(
            world_revision=request.evaluated_world_revision,
            deliberation_revision=request.evaluated_deliberation_revision,
            ledger_sequence=request.evaluated_ledger_sequence,
        )
        recall_trace: TrustedRecallTrace | None = None
        prefetch_trace: TrustedRecallTrace | None = None
        presented_prefetch_traces: tuple[PresentedPrefetchTrace, ...] = ()
        prefetch_job_token = (
            self._recall.scheduled_prefetch_token(
                expected_cursor=expected_cursor,
                trigger_ref=request.trigger_ref,
            )
            if self._recall is not None
            and self._recall.is_available(
                expected_cursor,
                trigger_ref=request.trigger_ref,
            )
            else None
        )
        if prefetch_trace is not None:
            initial_prefetch_trace = prefetch_trace
            request = request.model_copy(
                update={
                    "model_content_json": augment_model_content_with_recall(
                        request.model_content_json,
                        verify_trusted_recall_trace(initial_prefetch_trace),
                    )
                }
            )
            if (
                self._recall_available(request)
                and self._recall is not None
                and prefetch_job_token is not None
            ):
                ready_prefetch = self._recall.take_ready_scheduled_prefetch(
                    expected_cursor=expected_cursor,
                    trigger_ref=request.trigger_ref,
                    job_token=prefetch_job_token,
                )
                if (
                    ready_prefetch is not None
                    and ready_prefetch.audit.result_hash != initial_prefetch_trace.audit.result_hash
                ):
                    request = request.model_copy(
                        update={
                            "model_content_json": augment_model_content_with_recall(
                                request.model_content_json,
                                verify_trusted_recall_trace(ready_prefetch),
                            )
                        }
                    )
                    prefetch_trace = ready_prefetch
        elif (
            self._recall_available(request)
            and self._recall is not None
            and prefetch_job_token is not None
        ):
            prefetch_trace = await self._recall.await_scheduled_prefetch(
                expected_cursor=expected_cursor,
                trigger_ref=request.trigger_ref,
                timeout_seconds=fit_pre_provider_wait_timeout(
                    PREFETCH_FIRST_PASS_JOIN_SECONDS
                ),
                job_token=prefetch_job_token,
            )
            if prefetch_trace is not None:
                request = request.model_copy(
                    update={
                        "model_content_json": augment_model_content_with_recall(
                            request.model_content_json,
                            verify_trusted_recall_trace(prefetch_trace),
                        )
                    }
                )
        provider_request = request.model_copy(
            update={
                "model_content_json": compact_chat_model_facing_context(request.model_content_json)
            }
        )
        source_ref_aliases = build_source_ref_alias_table(
            request=provider_request,
            stable_identity_source_refs=self._stable_identity_source_refs,
            model_visible_context_json=provider_request.model_content_json,
        )
        appraisal_messages = _appraisal_draft_messages(provider_request)
        expression_messages = expression_adapter._messages(  # noqa: SLF001 - paired internal seam
            request=provider_request,
            quick_recovery=False,
            provisional=False,
            failure_code=None,
            stream_part=("head" if transport_provider is not None else None),
            source_ref_aliases=source_ref_aliases,
        )
        expression_user_material = json.loads(expression_messages[1]["content"])
        if not isinstance(expression_user_material, dict):
            raise ValueError("paired expression provider material must be an object")
        expression_user_material["appraisal_affect_hard_boundaries"] = {
            "active_affect_heads": _active_affect_heads(request),
        }
        expression_messages[1] = {
            "role": "user",
            "content": json.dumps(
                expression_user_material,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        recall_context_available = model_content_allows_recall(request.model_content_json)
        recall_available = self._recall_available(request)
        recall_choice_envelope = (
            '{"private_turn_state":{...},"recall_request":{...}}'
            if self._capabilities.private_turn_state_mode == "required"
            else '{"recall_request":{...}}'
        )
        messages = [
            {
                "role": "system",
                "content": (
                    (
                        (
                            "Return either one JSON object with exactly two keys, appraisal_draft "
                            "and expression_draft, or a recall choice with exactly the keys "
                            "private_turn_state and recall_request in either serialization order "
                            "when you choose to remember more first. "
                            if self._capabilities.private_turn_state_mode == "required"
                            else "Return either one JSON object with exactly two keys, "
                            "appraisal_draft and expression_draft, or the single recall_request "
                            "object described below when you choose to remember more first. "
                        )
                        if recall_available
                        else "Return exactly one JSON object with exactly two keys: "
                        "appraisal_draft and expression_draft. "
                    )
                    + "Both draft values must be JSON objects. This is one simultaneous "
                    "cognition pass. Treat appraisal, affect, attention, relationship, memory and "
                    "World context as evidence and advisory material, not behavior instructions. "
                    "The role model owns timing, motive, stance, expression and silence; neither "
                    "draft is accepted authority until the application validates its hard boundaries."
                    "\n\nAPPRAISAL DRAFT CONTRACT:\n"
                    + appraisal_messages[0]["content"]
                    + "\n\nEXPRESSION DRAFT CONTRACT:\n"
                    + expression_messages[0]["content"]
                    + "\n\nCOMBINED OUTPUT ENVELOPE:\n"
                    "The standalone return-format sentences embedded in the two contracts above "
                    "describe each inner object. "
                    + (
                        "Choose exactly one complete top-level envelope: return "
                        '{"appraisal_draft":{...},"expression_draft":{...}} when you can '
                        "decide now; alternatively return exactly "
                        + recall_choice_envelope
                        + " when you choose to remember more before deciding. The recall-first "
                        "envelope contains no appraisal_draft or expression_draft; after the "
                        "bounded result is supplied, return the final two-draft envelope. "
                        if recall_available
                        else "For this simultaneous call, return exactly "
                        '{"appraisal_draft":{...},"expression_draft":{...}} and no standalone '
                        "draft. "
                    )
                ),
            },
            expression_messages[1],
        ]
        inner_snapshot = json.loads(provider_request.model_content_json).get(
            "inner_life_snapshot"
        )
        correction = (
            inner_snapshot.get("role_result_correction")
            if isinstance(inner_snapshot, dict)
            else None
        )
        if isinstance(correction, dict):
            failure_code = correction.get("failure_code")
            if not isinstance(failure_code, str) or not failure_code:
                raise ValueError("character interior role correction is malformed")
            messages[0]["content"] += (
                "\n\nROLE RESULT HARD-BOUNDARY CORRECTION: your preceding result failed "
                f"the exact wire with failure_code={failure_code}. Return one fresh, complete "
                "choice from the same pinned Context and capabilities. This failure says nothing "
                "about whether to speak, what to feel, or what to say; those remain your decision."
            )
        if transport_provider is not None:
            messages[0]["content"] += (
                "\n\nCHARACTER INTERIOR STREAM TRANSPORT (overrides only the return "
                "envelope wording above, never either semantic contract): return one raw "
                "JSON object with protocol, appraisal_draft, and events; JSON member order is "
                "irrelevant. protocol must equal character-interior-events.1. appraisal_draft "
                "is the complete AppraisalDraft object chosen in this same cognition pass. "
                "events is an append-only expression array: first one head event, then zero "
                "or more beat events, then exactly {\"type\":\"end\"}. A head event has "
                "type=head, all complete ExpressionDraft fields except beats and "
                "episode_disposition, and either one visible beat field or a beats array. "
                "Each continuation is exactly type=beat, beat=<one authored beat>, "
                "world_claims=<claims for that beat>. The application releases no visible unit "
                "until both the complete appraisal and a complete visible head are available. "
                "Return no Markdown and no other top-level fields. If you "
                "instead choose the available recall-first option, return that exact recall "
                "object normally; it has no expression continuation."
            )
        provider = transport_provider or self._selected_provider(request)
        model_id = self._model_id_for_provider(request, provider)
        cognition_contract = InboundToolContracts().contract_for(
            phase=("initial" if recall_context_available else "after_recall"),
            transport=("stream" if transport_provider is not None else "atomic"),
            capabilities=self._capabilities,
            recall_allowed=recall_available,
            require_turn_posture=(
                provider_request.trigger_message is not None
                and provider_request.trigger_message.turn_attention_advisory is not None
            ),
            schema_dialect=(
                "deepseek-strict"
                if bool(getattr(provider, "supports_strict_tool_choice", False))
                else "standard"
            ),
        )
        cognition_tools = list(cognition_contract.provider_tools)
        cognition_tool_choice = cognition_contract.provider_tool_choice
        metered = (
            None
            if transport_provider is not None
            else getattr(provider, "complete_json_with_usage", None)
        )
        if transport_provider is None and not callable(metered):
            metered = getattr(provider, "complete_with_usage", None)
        use_forced_tool = (callable(metered) or transport_provider is not None) and bool(
            getattr(provider, "supports_required_tool_choice", False)
        )
        if use_forced_tool:
            decision_transport = (
                "For result_kind=decision include result_kind, protocol, appraisal_draft, "
                "and events in any valid JSON member order; protocol and events "
                "are the append-only CHARACTER INTERIOR STREAM TRANSPORT above. "
                if transport_provider is not None
                else "For result_kind=decision include appraisal_draft and "
                "expression_draft exactly as specified above. "
            )
            messages[0]["content"] += (
                "\n\nFORCED TOOL TRANSPORT (overrides only the outer JSON envelope above): "
                "call the required function exactly once. Its arguments must include "
                "result_kind. "
                + decision_transport
                + (
                    "For result_kind=recall include recall_request"
                    + (
                        " and private_turn_state"
                        if self._capabilities.private_turn_state_mode == "required"
                        else " (private_turn_state may also be included)"
                    )
                    + "."
                    if cognition_contract.recall_allowed
                    else "Recall is unavailable on this call; use result_kind=decision."
                )
                + " result_kind is transport-only and does not choose your appraisal, affect, "
                "timing, expression, or silence."
            )
        winning_provider_identity = _provider_invocation_identity(
            parent_call_id=provider_request.call_id,
            purpose="paired_cognition_initial",
            messages=messages,
            temperature=self._temperature,
            tools=(cognition_tools if use_forced_tool else None),
            tool_choice=(cognition_tool_choice if use_forced_tool else None),
            tool_contract_identity=(
                cognition_contract.identity.request_identity_material()
                if use_forced_tool
                else None
            ),
        )
        usage: ModelUsageProvenance | None = None
        forced_transport_error: ValueError | None = None
        exact_request_emission = bool(
            getattr(provider, "reports_exact_request_emission", False)
        )
        if not exact_request_emission:
            # Offline/fake providers have no HTTP boundary. Production
            # providers emit from immediately before their ``client.post``.
            mark_first_role_provider_entry(winning_provider_identity.model_call_id)
        try:
            with model_request_emission_scope(
                provider_call_id=winning_provider_identity.model_call_id,
                entry_marker=mark_first_role_provider_entry,
                completion_marker=mark_first_role_provider_completion,
            ):
                if transport_provider is not None:
                    raw = await provider.complete_json(
                        messages,
                        temperature=self._temperature,
                        **(
                            {
                                "tools": cognition_tools,
                                "tool_choice": cognition_tool_choice,
                                "tool_contract_identity": (
                                    cognition_contract.identity.request_identity_material()
                                ),
                            }
                            if use_forced_tool
                            else {}
                        ),
                    )
                elif callable(metered):
                    # Main character call uses the forced combined_cognition
                    # tool: the envelope structure becomes a server-side
                    # guarantee. Providers without tool support (fixtures)
                    # fall back to the plain JSON envelope path.
                    tool_kwargs: dict[str, object] = (
                        {
                            "tools": cognition_tools,
                            "tool_choice": cognition_tool_choice,
                        }
                        if use_forced_tool
                        else {}
                    )
                    result = await metered(
                        messages,
                        temperature=self._temperature,
                        **tool_kwargs,
                    )
                    if (
                        not isinstance(result, tuple)
                        or len(result) != 2
                        or not isinstance(result[0], str)
                    ):
                        raise ValueError(
                            "metered combined provider result must be (text, usage)"
                        )
                    raw, usage_raw = result
                    usage = ModelUsageProvenance.model_validate(usage_raw)
                else:
                    complete_json = getattr(provider, "complete_json", None)
                    raw = await (
                        complete_json(messages, temperature=self._temperature)
                        if callable(complete_json)
                        else provider.complete(messages, temperature=self._temperature)
                    )
            if use_forced_tool and transport_provider is None:
                try:
                    raw = cognition_contract.unwrap(raw)
                except ValueError as exc:
                    # Preserve the candidate for the existing bounded
                    # same-role envelope correction below; a transport error
                    # must not become an immediate visible-turn failure.
                    forced_transport_error = exc
            if not exact_request_emission:
                mark_first_role_provider_completion(
                    winning_provider_identity.model_call_id
                )
        except asyncio.CancelledError:
            # Deliberation cancels the paired provider task when its deadline
            # expires.  Preserve the same-trigger marker so the later
            # expression pass does not launch a duplicate provider call.
            self._failed_combined.add(_failed_cache_key(request))
            raise
        except Exception:
            self._failed_combined.add(_failed_cache_key(request))
            raise
        expression_request = request
        repair_messages = messages
        recall_allowed = model_content_allows_recall(request.model_content_json)
        recall_choice_corrective_spent = False
        try:
            parsed_recall_request = parse_character_recall_request(
                raw,
                request=provider_request,
                capabilities=self._capabilities,
                stable_identity_source_refs=self._stable_identity_source_refs,
                model_visible_context_json=provider_request.model_content_json,
                source_ref_aliases=source_ref_aliases,
            )
        except (TypeError, ValueError) as exc:
            if expression_episode_provider_slots_active() or not (
                is_private_turn_state_violation(exc) or is_recall_choice_violation(exc)
            ):
                raise
            repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
            if repair_timeout is None:
                raise
            try:
                corrected_result = await self._reselect_invalid_private_recall_choice(
                    request=provider_request,
                    provider=provider,
                    messages=messages,
                    raw=raw,
                    violation=exc,
                    timeout_seconds=repair_timeout,
                    private_state_context_json=provider_request.model_content_json,
                    source_ref_aliases=source_ref_aliases,
                )
            except ValidationTechnicalFailure as terminal:
                self._terminal_failed_combined.add(_cache_key(request))
                raise ValidationTechnicalFailure(
                    "recall_choice_reselection_invalid",
                    model_call_id=terminal.model_call_id,
                    request_hash=terminal.request_hash,
                    attempted_model_id=model_id,
                    attempted_model_version=self.VERSION,
                    usage=_combine_usage(usage, terminal.usage, request.call_id),
                ) from terminal
            if corrected_result is None:
                raise
            usage = _combine_usage(usage, corrected_result.usage, request.call_id)
            raw = corrected_result.raw
            forced_transport_error = None
            if (
                corrected_result.winning_model_call_id is None
                or corrected_result.winning_request_hash is None
            ):
                raise ValueError("paired recall correction omitted provider identity")
            winning_provider_identity = _ProviderInvocationIdentity(
                model_call_id=corrected_result.winning_model_call_id,
                request_hash=corrected_result.winning_request_hash,
            )
            recall_choice_corrective_spent = True
            parsed_recall_request = parse_character_recall_request(
                raw,
                request=provider_request,
                capabilities=self._capabilities,
                stable_identity_source_refs=self._stable_identity_source_refs,
                model_visible_context_json=provider_request.model_content_json,
                source_ref_aliases=source_ref_aliases,
            )
            if parsed_recall_request is not None:
                raise ValueError(
                    "a private-state recall-choice correction must return final "
                    "appraisal_draft and expression_draft"
                )
        if not recall_allowed and parsed_recall_request is not None:
            raise ValueError("paired character recall budget is already consumed")
        if (
            parsed_recall_request is not None
            and recall_allowed
            and self._character_interior_recall_delegate
            and not expression_episode_provider_slots_active()
        ):
            # Do not execute the retired embedded Recall path.  The private
            # Faculty turns this exact, valid role choice into a
            # ``recall_request`` result; CharacterInterior retrieves once and
            # invokes this same author again with the recalled sources inside
            # the canonical eight-facet snapshot.
            recall_choice_value = _parse_json_object(raw)
            private_turn_state = validate_expression_private_turn_state(
                value=recall_choice_value,
                request=provider_request,
                capabilities=self._capabilities,
                stable_identity_source_refs=self._stable_identity_source_refs,
                model_visible_context_json=provider_request.model_content_json,
                source_ref_aliases=source_ref_aliases,
            )
            raise _InboundRecallRequested(
                query=parsed_recall_request.query_text,
                model_id=model_id,
                model_version=self.VERSION,
                model_call_id=winning_provider_identity.model_call_id,
                request_hash=winning_provider_identity.request_hash,
                response_hash=sha256(raw.encode("utf-8")).hexdigest(),
                usage=usage,
                private_turn_state=private_turn_state,
            )
        recall_request = (
            parsed_recall_request
            if recall_allowed
            and self._recall_available(request)
            and not expression_episode_provider_slots_active()
            else None
        )
        prior_presentation_count = len(presented_prefetch_traces)
        presented_prefetch_traces = append_presented_prefetch(
            presented_prefetch_traces,
            phase="initial",
            model_call_id=winning_provider_identity.model_call_id,
            trace=prefetch_trace,
        )
        if self._recall is not None and len(presented_prefetch_traces) > prior_presentation_count:
            self._recall.record_prefetch_presentation(presented_prefetch_traces[-1])
        if recall_request is None and self._recall is not None and prefetch_job_token is not None:
            self._recall.discard_scheduled_prefetch(
                expected_cursor,
                trigger_ref=request.trigger_ref,
                job_token=prefetch_job_token,
            )
        if recall_request is not None:
            recall_timeout = fit_secondary_call_timeout(8.0)
            if recall_timeout is None:
                raise TimeoutError("paired character recall budget exhausted")
            if not claim_secondary_provider_slot("recall"):
                raise TimeoutError("paired character recall slot is unavailable")
            # The first paired author call is latency-bounded to the local
            # prefetch fallback.  If the semantic job finished while that model
            # was thinking, the already-required recall follow-up may consume
            # it without waiting or adding another character-model call.
            ready_prefetch = (
                self._recall.take_ready_scheduled_prefetch(
                    expected_cursor=expected_cursor,
                    trigger_ref=request.trigger_ref,
                    job_token=prefetch_job_token,
                )
                if prefetch_job_token is not None
                else None
            )
            if ready_prefetch is not None:
                prefetch_trace = ready_prefetch
            accessibility_seed = (
                f"paired-character-recall:{request.call_id}:" + _cache_key(request)[1]
            )
            if prefetch_trace is None:
                prefetch_trace, recall_trace = await perform_character_recall_with_prefetch(
                    self._recall,
                    request=recall_request,
                    accessibility_seed=accessibility_seed,
                    expected_cursor=expected_cursor,
                    trigger_ref=request.trigger_ref,
                    timeout_seconds=recall_timeout,
                    prefetch_job_token=prefetch_job_token,
                )
            else:
                recall_trace = await perform_character_recall(
                    self._recall,
                    request=recall_request,
                    accessibility_seed=accessibility_seed,
                    expected_cursor=expected_cursor,
                    trigger_ref=request.trigger_ref,
                    timeout_seconds=recall_timeout,
                )
            audit_trace = verify_trusted_recall_trace(recall_trace)
            prefetch_audit = (
                verify_trusted_recall_trace(prefetch_trace) if prefetch_trace is not None else None
            )
            model_content_json = request.model_content_json
            if prefetch_audit is not None:
                model_content_json = augment_model_content_with_recall(
                    model_content_json,
                    prefetch_audit,
                )
            expression_request = request.model_copy(
                update={
                    "model_content_json": augment_model_content_with_recall(
                        model_content_json,
                        audit_trace,
                    )
                }
            )
            provider_expression_request = expression_request.model_copy(
                update={
                    "model_content_json": compact_chat_model_facing_context(
                        expression_request.model_content_json
                    )
                }
            )
            source_ref_aliases = build_source_ref_alias_table(
                request=provider_expression_request,
                stable_identity_source_refs=self._stable_identity_source_refs,
                model_visible_context_json=provider_expression_request.model_content_json,
                existing=source_ref_aliases,
            )
            followup = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "Here is the bounded read-only recall result you chose. It is reference "
                        "material, not a behavior instruction. Now return exactly one JSON object "
                        "with exactly appraisal_draft and expression_draft; no further recall is "
                        "available. Form expression_draft's final private_turn_state again from "
                        "the augmented Context and include it in the complete final draft; the "
                        "earlier state explained the recall choice but cannot justify the final "
                        "expression after the fact. "
                        "Copy source_refs only when a factual clause is supported.\n"
                        "For this augmented final Context, use this frozen source_ref_aliases "
                        "mapping (it extends and supersedes the earlier displayed mapping):\n"
                        + json.dumps(
                            source_ref_aliases.prompt_value(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                        + recall_followup_evidence_json(
                            prefetch=prefetch_audit,
                            character_pull=audit_trace,
                        )
                    ),
                },
            ]
            repair_messages = followup
            followup_tool = self._final_tool_reselection_kwargs(
                request=provider_expression_request,
                provider=provider,
            )
            followup_tools = followup_tool.get("tools")
            followup_tool_choice = followup_tool.get("tool_choice")
            followup_contract_identity = followup_tool.get("tool_contract_identity")
            followup_unwrap = followup_tool.get("unwrap_tool_result")
            followup_identity = _provider_invocation_identity(
                parent_call_id=provider_expression_request.call_id,
                purpose="paired_recall_followup",
                messages=followup,
                temperature=self._temperature,
                tools=(followup_tools if isinstance(followup_tools, list) else None),
                tool_choice=followup_tool_choice,
                tool_contract_identity=(
                    followup_contract_identity
                    if isinstance(followup_contract_identity, dict)
                    else None
                ),
            )
            second_usage: ModelUsageProvenance | None = None
            recall_timeout = fit_secondary_call_timeout(8.0)
            if recall_timeout is None:
                raise TimeoutError("paired character recall follow-up budget exhausted")
            async with asyncio.timeout(recall_timeout):
                if callable(metered):
                    result = await metered(
                        followup,
                        temperature=self._temperature,
                        **(
                            {
                                "tools": followup_tools,
                                "tool_choice": followup_tool_choice,
                            }
                            if isinstance(followup_tools, list)
                            else {}
                        ),
                    )
                    if (
                        not isinstance(result, tuple)
                        or len(result) != 2
                        or not isinstance(result[0], str)
                    ):
                        raise ValueError("metered paired recall result must be (text, usage)")
                    raw, usage_raw = result
                    if callable(followup_unwrap):
                        raw = followup_unwrap(raw)
                    second_usage = ModelUsageProvenance.model_validate(usage_raw)
                else:
                    complete_json = getattr(provider, "complete_json", None)
                    raw = await (
                        complete_json(
                            followup,
                            temperature=self._temperature,
                            **(
                                {
                                    "tools": followup_tools,
                                    "tool_choice": followup_tool_choice,
                                }
                                if isinstance(followup_tools, list)
                                else {}
                            ),
                        )
                        if callable(complete_json)
                        else provider.complete(followup, temperature=self._temperature)
                    )
                    if callable(followup_unwrap):
                        raw = followup_unwrap(raw)
            usage = _combine_usage(usage, second_usage, request.call_id)
            prior_presentation_count = len(presented_prefetch_traces)
            presented_prefetch_traces = append_presented_prefetch(
                presented_prefetch_traces,
                phase="recall_followup",
                model_call_id=followup_identity.model_call_id,
                trace=prefetch_trace,
            )
            if (
                self._recall is not None
                and len(presented_prefetch_traces) > prior_presentation_count
            ):
                self._recall.record_prefetch_presentation(presented_prefetch_traces[-1])
            winning_provider_identity = followup_identity
        else:
            provider_expression_request = provider_request
        envelope_corrective_spent = False
        try:
            if forced_transport_error is not None:
                raise forced_transport_error
            value = _parse_combined(raw)
        except (TypeError, ValueError) as exc:
            failed_key = _failed_cache_key(expression_request)
            self._failed_combined.add(failed_key)
            self._remember_failed_expression(
                failed_key,
                messages=repair_messages,
                raw=raw,
                violation=str(exc),
                usage=usage,
                private_state_context_json=(provider_expression_request.model_content_json),
                source_ref_aliases=source_ref_aliases,
                origin_request=expression_request,
            )
            # Envelope repair (2026-08-08): a wrong outer shape (missing or
            # extra top-level keys) used to fail closed with no recovery. Echo
            # the exact violation back at low temperature so the model emits
            # the canonical two-object envelope instead of going silent.
            repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
            if repair_timeout is None:
                raise
            envelope_instruction = (
                "Your previous response was not one combined cognition object. "
                f"Exact violation: {str(exc)[:300]}. Return exactly one complete JSON "
                "object with exactly the two keys appraisal_draft and expression_draft, "
                "each an object. Re-decide both together from the same pinned context."
            )
            corrected = await complete_bounded_validation_reselection(
                model=provider,
                messages=messages,
                raw=raw,
                instruction=envelope_instruction,
                temperature=0.2,
                timeout_seconds=repair_timeout,
                parent_call_id=provider_request.call_id,
                **self._final_tool_reselection_kwargs(
                    request=provider_request,
                    provider=provider,
                ),
            )
            usage = _combine_usage(usage, corrected.usage, request.call_id)
            try:
                value = _parse_combined(corrected.raw)
            except (TypeError, ValueError):
                raise
            envelope_corrective_spent = True
        key = _cache_key(request)

        def materialize_live_appraisal(raw_value: dict[str, object]) -> dict[str, object]:
            if use_forced_tool and "affect" not in raw_value:
                # Historical/plain appraisal bytes retain the omitted
                # no_change compatibility default. Every candidate descended
                # from a live forced result must state this role-owned
                # lifecycle choice, including its one bounded correction.
                raise ValueError("forced AppraisalDraft must explicitly include affect")
            return materialize_appraisal_draft(
                raw=json.dumps(raw_value, ensure_ascii=False, separators=(",", ":")),
                request=request,
            )

        appraisal_proposal: dict[str, object] | None = None
        corrective_spent = recall_choice_corrective_spent or envelope_corrective_spent
        try:
            appraisal_proposal = materialize_live_appraisal(value["appraisal_draft"])
        except AffectTargetBelowMinimumError as target_error:
            if corrective_spent:
                raise ValidationTechnicalFailure(
                    "affect_target_reselection_invalid",
                    model_call_id=winning_provider_identity.model_call_id,
                    request_hash=winning_provider_identity.request_hash,
                    attempted_model_id=model_id,
                    attempted_model_version=self.VERSION,
                    usage=usage,
                ) from target_error
            repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
            if repair_timeout is None:
                raise TimeoutError("paired Affect target reselection budget exhausted")
            instruction = (
                target_reselection_instruction(target_error)
                + " For this simultaneous cognition call, return exactly one complete "
                "replacement object with appraisal_draft and expression_draft. Re-decide "
                "both drafts together from the original pinned context; the system does "
                "not select an emotion, target, timing, stance, or expression for you."
            )
            corrected = await complete_bounded_validation_reselection(
                model=provider,
                messages=messages,
                raw=raw,
                instruction=instruction,
                temperature=0.2,
                timeout_seconds=repair_timeout,
                parent_call_id=provider_request.call_id,
                **self._final_tool_reselection_kwargs(
                    request=provider_request,
                    provider=provider,
                ),
            )
            corrected_usage = _combine_usage(
                usage,
                corrected.usage,
                request.call_id,
            )
            try:
                value = _parse_combined(corrected.raw)
                appraisal_proposal = materialize_live_appraisal(value["appraisal_draft"])
            except (TypeError, ValueError) as second_error:
                raise ValidationTechnicalFailure(
                    "affect_target_reselection_invalid",
                    model_call_id=corrected.winning_model_call_id,
                    request_hash=corrected.winning_request_hash,
                    attempted_model_id=model_id,
                    attempted_model_version=self.VERSION,
                    usage=corrected_usage,
                ) from second_error
            usage = corrected_usage
            raw = corrected.raw
            repair_messages = messages
            corrective_spent = True
            if corrected.winning_model_call_id is None or corrected.winning_request_hash is None:
                raise ValueError("paired Affect target correction omitted provider identity")
            winning_provider_identity = _ProviderInvocationIdentity(
                model_call_id=corrected.winning_model_call_id,
                request_hash=corrected.winning_request_hash,
            )
        except (TypeError, ValueError) as appraisal_error:
            if corrective_spent:
                raise ValidationTechnicalFailure(
                    "appraisal_reselection_invalid",
                    model_call_id=winning_provider_identity.model_call_id,
                    request_hash=winning_provider_identity.request_hash,
                    attempted_model_id=model_id,
                    attempted_model_version=self.VERSION,
                    usage=usage,
                ) from appraisal_error
            repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
            if repair_timeout is None:
                raise ValidationTechnicalFailure(
                    "appraisal_reselection_unavailable",
                    model_call_id=winning_provider_identity.model_call_id,
                    request_hash=winning_provider_identity.request_hash,
                    attempted_model_id=model_id,
                    attempted_model_version=self.VERSION,
                    usage=usage,
                ) from appraisal_error
            instruction = (
                "The simultaneous result failed the AppraisalDraft hard contract: "
                + f"{type(appraisal_error).__name__}: {str(appraisal_error)[:512]}. "
                "Using only the original pinned Context and capabilities, return one "
                "complete replacement object with appraisal_draft and expression_draft. "
                "This is a structural correction, not an instruction to choose any "
                "particular appraisal, affect, relationship meaning, timing, stance, "
                "speech, or silence."
            )
            corrected = await complete_bounded_validation_reselection(
                model=provider,
                messages=messages,
                raw=raw,
                instruction=instruction,
                temperature=0.2,
                timeout_seconds=repair_timeout,
                parent_call_id=provider_request.call_id,
                **self._final_tool_reselection_kwargs(
                    request=provider_request,
                    provider=provider,
                ),
            )
            corrected_usage = _combine_usage(usage, corrected.usage, request.call_id)
            try:
                value = _parse_combined(corrected.raw)
                appraisal_proposal = materialize_live_appraisal(value["appraisal_draft"])
            except (TypeError, ValueError) as second_error:
                raise ValidationTechnicalFailure(
                    "appraisal_reselection_invalid",
                    model_call_id=corrected.winning_model_call_id,
                    request_hash=corrected.winning_request_hash,
                    attempted_model_id=model_id,
                    attempted_model_version=self.VERSION,
                    usage=corrected_usage,
                ) from second_error
            if (
                corrected.winning_model_call_id is None
                or corrected.winning_request_hash is None
            ):
                raise ValueError("paired appraisal correction omitted provider identity")
            usage = corrected_usage
            raw = corrected.raw
            repair_messages = messages
            corrective_spent = True
            winning_provider_identity = _ProviderInvocationIdentity(
                model_call_id=corrected.winning_model_call_id,
                request_hash=corrected.winning_request_hash,
            )
        expression_value = dict(value["expression_draft"])
        expression_raw = json.dumps(expression_value, ensure_ascii=False, separators=(",", ":"))
        expression_raw, episode_disposition = _split_expression_episode_disposition(
            expression_raw,
            provisional=False,
        )
        expression_value = json.loads(expression_raw)
        # The provider creates two fallible drafts in one transport response,
        # but they remain independent proposal candidates.  A malformed inner
        # appraisal must not erase a valid, separately auditable expression.
        # Conversely, never cache expression bytes that cannot pass the normal
        # ExpressionDraft materializer even at the source cursor.
        violation: str | None = None
        violation_object: object | None = None
        authored_field_violation = False
        try:
            materialize_expression_draft(
                raw=expression_raw,
                request=expression_request,
                capabilities=self._capabilities,
                quick_recovery=False,
                stable_identity_source_refs=self._stable_identity_source_refs,
                private_state_context_json=(provider_expression_request.model_content_json),
                source_ref_aliases=source_ref_aliases,
                require_explicit_authored_decision_fields=(
                    self._require_explicit_authored_decision_fields
                ),
            )
        except (TypeError, ValueError) as exc:
            violation = str(exc)
            violation_object = exc
            authored_field_violation = (
                self._require_explicit_authored_decision_fields
                and is_authored_expression_draft_shape_violation(exc)
            )
            logger.warning(
                "combined expression failed its exact contract: shape=%s error_type=%s detail=%s",
                _visible_expression_shape(expression_value),
                type(exc).__name__,
                " | ".join(
                    f"{'.'.join(str(part) for part in item.get('loc', ()))}:{item.get('type', '')}:{str(item.get('msg', ''))[:120]}"
                    for item in getattr(exc, "errors", lambda: ())()[:6]
                ),
            )
            expression_valid = False
        else:
            expression_valid = True
        if (
            not expression_valid
            and violation is not None
            and not corrective_spent
            and not expression_episode_provider_slots_active()
        ):
            # A structural near-miss (claim bookkeeping, beat shape, later
            # contract) regularly arrives attached to a perfectly good visible
            # reply. Spend one corrective call on the same selected role
            # author that names the exact violation. The retry
            # is deadline-aware: when the Deliberation attempt budget cannot
            # fit another completion, defer the correction to the
            # post-acceptance expression pass instead of timing out the whole
            # attempt after the repair already succeeded.
            repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
            if repair_timeout is None:
                logger.warning(
                    "paired corrective retry deferred: attempt budget exhausted violation=%s",
                    violation[:200],
                )
            else:
                corrective_spent = True
                repaired_result = await self._repair_expression_claims(
                    request=expression_request,
                    provider=provider,
                    messages=repair_messages,
                    raw=raw,
                    violation=(violation_object if violation_object is not None else violation),
                    timeout_seconds=repair_timeout,
                    private_state_context_json=(provider_expression_request.model_content_json),
                    source_ref_aliases=source_ref_aliases,
                )
                if repaired_result is not None:
                    usage = _combine_usage(
                        usage,
                        repaired_result.usage,
                        request.call_id,
                    )
                    expression_raw = repaired_result.raw
                    episode_disposition = repaired_result.episode_disposition
                    if (
                        repaired_result.winning_model_call_id is None
                        or repaired_result.winning_request_hash is None
                    ):
                        raise ValueError("paired shape correction omitted provider identity")
                    winning_provider_identity = _ProviderInvocationIdentity(
                        model_call_id=repaired_result.winning_model_call_id,
                        request_hash=repaired_result.winning_request_hash,
                    )
                    expression_valid = True
                elif authored_field_violation:
                    # The paired transport already spent this authored
                    # candidate's only full structural reselection. A second
                    # omission is a terminal technical validation failure,
                    # not permission to materialize replay defaults or ask a
                    # second character author to make a third semantic choice.
                    self._terminal_authored_expression_combined.add(_cache_key(expression_request))
        if appraisal_proposal is None:
            raise ValidationTechnicalFailure(
                "appraisal_result_missing",
                model_call_id=winning_provider_identity.model_call_id,
                request_hash=winning_provider_identity.request_hash,
                attempted_model_id=model_id,
                attempted_model_version=self.VERSION,
                usage=usage,
            )
        if expression_valid:
            self._terminal_failed_combined.discard(_cache_key(expression_request))
            self._terminal_authored_expression_combined.discard(_cache_key(expression_request))
            pending_expression = _PendingExpression(
                raw=expression_raw,
                model_id=model_id,
                route_tier=request.route.tier,
                usage=usage,
                private_state_context_json=(provider_expression_request.model_content_json),
                source_ref_aliases=source_ref_aliases,
                origin_call_id=provider_expression_request.call_id,
                origin_request_hash=_model_input_request_hash(provider_expression_request),
                origin_world_revision=provider_expression_request.evaluated_world_revision,
                origin_deliberation_revision=(
                    provider_expression_request.evaluated_deliberation_revision
                ),
                origin_ledger_sequence=provider_expression_request.evaluated_ledger_sequence,
                winning_model_call_id=winning_provider_identity.model_call_id,
                winning_request_hash=winning_provider_identity.request_hash,
                author_provider=provider,
                corrective_spent=corrective_spent,
                episode_disposition=episode_disposition,
                recall_trace=recall_trace,
                prefetch_trace=prefetch_trace,
                presented_prefetch_traces=presented_prefetch_traces,
            )
            if has_provider_slot_coordinator():
                self._candidate_pending[(key, request.call_id)] = pending_expression
                self._candidate_pending.move_to_end((key, request.call_id))
                while len(self._candidate_pending) > _MAX_PENDING_DRAFTS * 2:
                    self._candidate_pending.popitem(last=False)
            else:
                self._pending[key] = pending_expression
                self._pending.move_to_end(key)
                while len(self._pending) > _MAX_PENDING_DRAFTS:
                    self._pending.popitem(last=False)
        else:
            self._pending.pop(key, None)
            # The appraisal bytes may still be valid even when the paired
            # expression draft is not.  Preserve a same-trigger marker plus
            # the exact violation so the post-acceptance expression lane can
            # spend one corrective retry that names the concrete problem
            # before it falls back to the bounded role-model recovery. When the
            # in-attempt corrective was already spent (and failed once), do
            # not queue the same correction again: repeating an identical
            # failed repair only delays the bounded model recovery.
            failed_key = _failed_cache_key(expression_request)
            self._failed_combined.add(failed_key)
            if violation is not None and not corrective_spent:
                self._remember_failed_expression(
                    failed_key,
                    messages=repair_messages,
                    raw=raw,
                    violation=violation,
                    usage=usage,
                    private_state_context_json=(provider_expression_request.model_content_json),
                    source_ref_aliases=source_ref_aliases,
                    origin_request=expression_request,
                )
        return ModelOutput(
            model_id=model_id,
            model_version=self.VERSION,
            raw_proposal=appraisal_proposal,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            usage=usage,
            winning_model_call_id=winning_provider_identity.model_call_id,
            winning_request_hash=winning_provider_identity.request_hash,
            recall_trace=recall_trace,
            prefetch_trace=prefetch_trace,
            presented_prefetch_traces=presented_prefetch_traces,
        )

    def _remember_failed_expression(
        self,
        key: tuple[str, ...],
        *,
        messages: list[dict[str, str]],
        raw: str,
        violation: str,
        usage: ModelUsageProvenance | None,
        private_state_context_json: str,
        source_ref_aliases: SourceRefAliasTable,
        origin_request: ModelInput,
    ) -> None:
        self._failed_details.pop(key, None)
        self._failed_details[key] = _FailedExpressionDetail(
            messages=messages,
            raw=raw,
            violation=violation,
            usage=usage,
            private_state_context_json=private_state_context_json,
            source_ref_aliases=source_ref_aliases,
            origin_call_id=origin_request.call_id,
            origin_request_hash=_model_input_request_hash(origin_request),
            origin_world_revision=origin_request.evaluated_world_revision,
            origin_deliberation_revision=origin_request.evaluated_deliberation_revision,
            origin_ledger_sequence=origin_request.evaluated_ledger_sequence,
        )
        while len(self._failed_details) > _MAX_PENDING_DRAFTS:
            self._failed_details.popitem(last=False)


def _visible_expression_shape(value: dict[str, Any]) -> str:
    """Return bounded structural diagnostics without logging proposed prose."""

    parts: list[str] = []
    for key in sorted(value)[:16]:
        item = value[key]
        if isinstance(item, list):
            item_types = ",".join(type(child).__name__ for child in item[:8])
            kind = f"list[{item_types}]"
        else:
            kind = type(item).__name__
        parts.append(f"{key}:{kind}")
    if len(value) > 16:
        parts.append(f"+{len(value) - 16}-keys")
    return ";".join(parts)


def _canonical_appraisal_reselection_context(
    *,
    raw: str,
    request: ModelInput,
) -> str | None:
    """Keep only a validated appraisal when a paired expression is discarded.

    A private-state failure invalidates the causal path to the complete
    expression.  Returning that expression to the role model would make its
    old visible prose an accidental anchor.  A separately valid appraisal may
    still be retained, but only as a compact canonical subobject whose fields
    are understood by the AppraisalDraft materializer.
    """

    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            return None
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    appraisal = value.get("appraisal_draft", value.get("AppraisalDraft"))
    if not isinstance(appraisal, dict):
        return None

    fields = set(_APPRAISAL_COMMON_FIELDS)
    if appraisal.get("appraise") is True:
        fields.update(_APPRAISAL_EVENT_FIELDS)
        affect_operation = appraisal.get("affect")
        if isinstance(affect_operation, str):
            fields.update(_APPRAISAL_AFFECT_FIELDS.get(affect_operation, ()))
    canonical_appraisal = {key: appraisal[key] for key in sorted(fields) if key in appraisal}
    appraisal_raw = json.dumps(
        canonical_appraisal,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        materialize_appraisal_draft(raw=appraisal_raw, request=request)
    except (TypeError, ValueError):
        return None
    return json.dumps(
        {"appraisal_draft": canonical_appraisal},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_combined(raw: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, str):
        raise ValueError("combined cognition model did not return text")
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ValueError("combined cognition model returned an unclosed JSON fence")
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("combined cognition model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("combined cognition model must return an object")
    if len(value) == 2:
        aliases: dict[str, object] = {}
        for key, item in value.items():
            normalized = "".join(character for character in key.lower() if character.isalpha())
            if normalized in {"appraisal", "appraisaldraft"}:
                canonical = "appraisal_draft"
            elif normalized in {"expression", "expressiondraft"}:
                canonical = "expression_draft"
            else:
                break
            if canonical in aliases:
                break
            aliases[canonical] = item
        if set(aliases) == {"appraisal_draft", "expression_draft"}:
            value = aliases
    if set(value) != {"appraisal_draft", "expression_draft"}:
        raise ValueError(
            "combined cognition must contain exactly appraisal_draft and expression_draft"
        )
    if not all(isinstance(value[key], dict) for key in value):
        raise ValueError("combined cognition drafts must be objects")
    return value  # type: ignore[return-value]


__all__: list[str] = []
