"""Structured-proposal adapter for the existing chat-model seam.

The adapter is deliberately small at its public seam (``propose`` and
``recover``) while it owns prompt framing, response extraction, route metadata
and model identity.  It lets World v2 use the configured Flash/Thinking model
without importing ``CompanionEngine`` or inheriting its legacy turn logic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Literal, Protocol

from pydantic import Field

from companion_daemon.llm import model_call_scope

from .deliberation import (
    ModelInput,
    ModelOutput,
    ModelUsageProvenance,
    claim_secondary_provider_slot,
    expression_episode_provider_slots_active,
    fit_secondary_call_timeout,
)
from .expression_draft import (
    ExpressionDraft,
    ExpressionDraftCapabilities,
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
    is_world_claim_violation,
    materialize_expression_draft,
)
from .expression_episode import validate_provisional_proposal
from .model_facing_context import (
    compact_model_facing_context,
    compact_recovery_model_facing_context,
)
from .production_reliability_metrics import record_claim_repair, record_shape_repair
from .proposal_envelope import (
    CanonicalTypedPayload,
    MinimalProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from .schema_core import FrozenModel
from .recall_index import RecallCursor
from .recall_runtime import (
    CharacterRecallRequest,
    RecallCoordinator,
    TrustedRecallTrace,
    augment_model_content_with_recall,
    model_content_allows_recall,
    perform_character_recall,
    perform_character_recall_with_prefetch,
    recall_followup_evidence_json,
    verify_trusted_recall_trace,
)


logger = logging.getLogger(__name__)

_SEMANTIC_REVIEW_TIMEOUT_SECONDS = 3.5
# One corrective completion for a claim-bookkeeping near-miss: a repaired
# genuine reply a few seconds late reads far more human than an instant
# canned acknowledgement, but the wait stays bounded.
_WORLD_CLAIM_REPAIR_TIMEOUT_SECONDS = 8.0


def expression_draft_shape_contract() -> str:
    """Describe executable JSON fields without prescribing character behavior."""

    return (
        "Exact ExpressionDraft JSON field contract: timing_choice is the string now, later, "
        "or silent; cadence, when present, is rapid, conversational, hesitant, or escalating; "
        'beats is an array of objects. A text beat uses exactly modality="text" and '
        "text=<non-empty string>, with optional role set to opening, substantive, challenge, "
        "self_correction, or afterthought; never use content or put cadence inside a beat. "
        "Non-text beats must use only the installed expression_capabilities and their matching "
        "reaction_id or sticker_id field; typing has no value field. stance and "
        "brief_rationale are non-empty strings. confidence is an integer from 0 through 10000, "
        "never a decimal fraction. world_claims is an array; each item uses claim_text, scope, "
        "and source_refs. scope is exactly one of current_world, past_world, "
        "counterpart_history, shared_history, stable_identity, or "
        "subjective_or_hypothetical; never use conversation or user_fact. "
        "response_expectation, when chosen, uses hoped_response, pressure_bp, "
        "importance_bp, wait_seconds, and expires_after_seconds. "
        "response_expectation_assessment, when required by Context, uses status "
        "(fulfilled, superseded, still_pending, or uncertain) and reason. Do not add fields "
        "from a different chat or response schema."
    )


def claim_repair_instruction(violation: str, *, shape_line: str | None = None) -> str:
    """Corrective prompt for a world-claim bookkeeping near-miss.

    The exact violation is quoted so the model fixes the offending clause
    instead of guessing which part of the reply was classified as an
    occurrence.  ``shape_line`` lets the paired cognition pass request its
    two-key wrapper without duplicating the claim contract text.
    """

    shape = shape_line or "one corrected JSON object of the same shape"
    return (
        "Your draft failed world-claim validation with this exact violation: "
        f"{violation[:640]}\n"
        f"Return {shape} with the visible reply "
        "preserved as closely as honesty allows, fixing only the problem: the claim "
        "field is named source_refs; grounded scopes (current_world, past_world, "
        "counterpart_history, shared_history, factual stable_identity) require source_refs "
        "copied verbatim from a matching Context item. counterpart_history claims about the "
        "other person cite recent_dialogue or relevant_facts item refs. shared_history claims cite recent_dialogue or "
        "recent_experiences item refs; current_world/past_world cite "
        "current_situation, world_life, or recent_experiences item refs; "
        "stable_identity cites character_core item refs. If no Context item backs an "
        "asserted occurrence, rephrase that exact offending clause so it no longer "
        "asserts the occurrence, or mark truly subjective inner-life statements as "
        "scope=subjective_or_hypothetical with empty source_refs. Do not invent refs."
    )


def shape_repair_instruction(violation: str, *, shape_line: str | None = None) -> str:
    """Corrective prompt for a non-claim structural draft violation.

    Covers the measured rejection classes that arrive attached to an
    otherwise sound reply: ExpressionDraft field/beat shape, the one-beat
    later contract, timing_choice values, and malformed JSON wrappers.
    """

    shape = shape_line or "one corrected JSON object of the same shape"
    return (
        "Your draft failed structural validation with this exact violation: "
        f"{violation[:640]}\n"
        f"Return {shape} that fixes only this problem while preserving the visible "
        "reply text as closely as possible. "
        + expression_draft_shape_contract()
        + " later carries exactly one text beat plus delay_seconds and "
        "expires_after_seconds; silent carries an empty beats array; world_claims is always "
        "present (an empty array when there are none). "
        "Return raw JSON only, never Markdown fences or commentary."
    )


async def _bounded_review_call(
    reviewer: ChatCompletionModel,
    messages: list[dict[str, str]],
    *,
    temperature: float,
) -> str:
    """Keep secondary semantic reviews from becoming a hidden second turn."""

    complete_json = getattr(reviewer, "complete_json", None)
    call = (
        complete_json(messages, temperature=temperature)
        if callable(complete_json)
        else reviewer.complete(messages, temperature=temperature)
    )
    return await asyncio.wait_for(call, timeout=_SEMANTIC_REVIEW_TIMEOUT_SECONDS)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ChatCompletionModel(Protocol):
    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str: ...


class CompanionIdentityFrame(FrozenModel):
    """Bounded deployment identity shared by main and recovery reply lanes."""

    companion_name: str = Field(min_length=1, max_length=128)
    companion_aliases: tuple[str, ...] = Field(default=(), max_length=8)
    counterpart_name: str = Field(min_length=1, max_length=128)
    relationship_frame: str = Field(min_length=1, max_length=512)
    stable_identity_facts: tuple[str, ...] = Field(default=(), max_length=16)
    personality_frame: str | None = Field(default=None, max_length=2_048)
    values: tuple[str, ...] = Field(default=(), max_length=16)
    speech_frame: str | None = Field(default=None, max_length=2_048)
    style_rules: tuple[str, ...] = Field(default=(), max_length=16)
    boundaries: tuple[str, ...] = Field(default=(), max_length=16)
    role: str = "virtual_companion"
    not_an_assistant: bool = True


def companion_identity_source_ref(identity: CompanionIdentityFrame) -> str:
    """Return the immutable token for the exact deployment identity frame."""

    return "identity-frame:sha256:" + _digest(
        identity.model_dump(mode="json", exclude={"counterpart_name"})
    )


class _IdentityAndCounterpartReview(FrozenModel):
    """Semantic review of a first-contact reply's two identity boundaries."""

    decision: Literal["accept", "replace"]
    replacement_text: str | None = Field(default=None, min_length=1, max_length=4_096)
    addresses_counterpart_as_companion_name: bool
    contains_counterpart_fact_premise: bool
    premise_source_refs: tuple[str, ...] = Field(default=(), max_length=8)
    brief_reason: str = Field(min_length=1, max_length=240)


class _ContextualClaimSupportReview(FrozenModel):
    """Independent semantic closure for one emergency factual explanation."""

    decision: Literal["supported", "unsupported"]
    unsupported_claim_indexes: tuple[int, ...] = Field(default=(), max_length=8)
    undeclared_fact_fragments: tuple[str, ...] = Field(default=(), max_length=8)
    brief_reason: str = Field(min_length=1, max_length=240)


async def review_expression_source_closure(
    *,
    reviewer: ChatCompletionModel,
    request: ModelInput,
    raw: str,
    identity_frame: CompanionIdentityFrame | None,
) -> _ContextualClaimSupportReview | None:
    """Semantically audit declared and omitted factual claims.

    Deterministic validation can prove that a declared source ref exists, but
    it cannot detect a model silently omitting the declaration or swapping the
    companion and counterpart subjects.  This bounded reviewer enforces only
    that truth boundary; it does not judge tone, motive, questions, or whether
    the character should speak.
    """

    value = _parse_json_object(raw)
    wrapped = value.get("expression_draft")
    if set(value) == {"expression_draft"} and isinstance(wrapped, dict):
        value = wrapped
    draft = ExpressionDraft.model_validate_json(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        strict=True,
    )
    visible_text = "\n".join(
        beat.text for beat in draft.beats if beat.text is not None
    )
    if not visible_text:
        return None
    identity_material = None
    if identity_frame is not None:
        identity_material = {
            "scope": "facts about the companion only, never the counterpart",
            "identity_source_ref": companion_identity_source_ref(identity_frame),
            "value": identity_frame.model_dump(mode="json"),
        }
    messages = [
        {
            "role": "system",
            "content": (
                "Audit only factual source closure for one proposed private-chat reply. "
                "Do not judge style, motive, warmth, questions, silence, or conversational "
                "quality. Every externally checkable assertion or presupposition about current "
                "life, a past event, shared history, the counterpart, or stable identity must "
                "be declared in world_claims and be directly supported by the cited pinned "
                "source. Judge each clause against evidence for its actual grammatical subject: "
                "companion identity evidence supports companion claims, while counterpart "
                "evidence supports counterpart claims. Neither subject's evidence supports a "
                "premise about the other. Subjective "
                "feelings, uncertainty, imagination, genuinely open questions, and freely "
                "chosen future intentions need no source. A source token alone is insufficient "
                "when its semantic content or subject does not support the clause. Return one "
                "JSON object with decision (supported or unsupported), zero-based "
                "unsupported_claim_indexes, undeclared_fact_fragments copied exactly from "
                "visible_text, and brief_reason. Do not rewrite the reply."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "visible_text": visible_text,
                    "world_claims": tuple(
                        {
                            "claim_index": index,
                            **claim.model_dump(mode="json"),
                        }
                        for index, claim in enumerate(draft.world_claims)
                    ),
                    "current_trigger": (
                        request.trigger_message.model_dump(mode="json")
                        if request.trigger_message is not None
                        else None
                    ),
                    "pinned_context": json.loads(
                        compact_model_facing_context(request.model_content_json)
                    ),
                    "private_companion_identity": identity_material,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
    with model_call_scope("world_v2_expression_source_closure"):
        reviewed_raw = await _bounded_review_call(
            reviewer,
            messages,
            temperature=0.0,
        )
    review = _ContextualClaimSupportReview.model_validate_json(
        json.dumps(
            _parse_json_object(reviewed_raw),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        strict=True,
    )
    indexes = review.unsupported_claim_indexes
    fragments = review.undeclared_fact_fragments
    if any(
        isinstance(index, bool) or index < 0 or index >= len(draft.world_claims)
        for index in indexes
    ) or len(indexes) != len(set(indexes)):
        raise ValueError("source-closure review returned invalid claim indexes")
    if any(
        not fragment.strip() or fragment not in visible_text for fragment in fragments
    ) or len(fragments) != len(set(fragments)):
        raise ValueError("source-closure review returned invalid undeclared fragments")
    if review.decision == "supported" and (indexes or fragments):
        raise ValueError("supported source-closure review cannot reject reply content")
    if review.decision == "unsupported" and not (indexes or fragments):
        raise ValueError(
            "unsupported source-closure review must identify a claim or visible fragment"
        )
    return review


def source_closure_violation(review: _ContextualClaimSupportReview) -> str:
    fragment_hashes = tuple(
        hashlib.sha256(fragment.encode()).hexdigest()[:16]
        for fragment in review.undeclared_fact_fragments
    )
    return (
        "world claim semantic source closure rejected: "
        f"unsupported_claim_indexes={list(review.unsupported_claim_indexes)}; "
        f"undeclared_fact_fragment_count={len(fragment_hashes)}; "
        f"undeclared_fact_fragment_hashes={list(fragment_hashes)}; "
        f"reason_hash={hashlib.sha256(review.brief_reason.encode()).hexdigest()[:16]}"
    )


class MeteredChatCompletionModel(ChatCompletionModel, Protocol):
    """Optional provider seam for a response plus immutable usage evidence.

    Existing string-only providers remain valid for conversation handling, but
    produce audit.1 records which Phase-8 cost gates reject.  A production
    provider opts in by returning the exact response text and the provider
    usage object from the same request, never by filling a later metrics map.
    """

    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance | dict[str, Any]]: ...


class ChatModelDeliberationAdapter:
    """Turn an ordinary chat completion into one inert World v2 proposal.

    The model receives a bounded, already-authoritative context capsule and
    returns JSON only.  This adapter neither validates the proposal semantics
    nor writes it: ``Deliberation`` does both at its existing authority seam.
    The same adapter can run a normal route and a constrained quick-recovery
    route without introducing another world-state path.
    """

    VERSION = "world-v2-chat-proposal-adapter.1"

    def __init__(
        self,
        *,
        model: ChatCompletionModel,
        model_id: str | None = None,
        temperature: float = 0.7,
        expression_capabilities: ExpressionDraftCapabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES,
        identity_frame: CompanionIdentityFrame | None = None,
        semantic_boundary_reviewer: ChatCompletionModel | None = None,
        source_closure_reviewer: ChatCompletionModel | None = None,
        recovery_prompt_mode: Literal["ordinary", "contextual_failure"] = "ordinary",
        contextual_grounding_reviewer: ChatCompletionModel | None = None,
        recall_coordinator: RecallCoordinator | None = None,
    ) -> None:
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("proposal adapter temperature must be between 0 and 2")
        if recovery_prompt_mode not in {"ordinary", "contextual_failure"}:
            raise ValueError("proposal adapter recovery prompt mode is invalid")
        if recovery_prompt_mode == "contextual_failure" and contextual_grounding_reviewer is None:
            raise ValueError("contextual failure recovery requires an independent grounding review")
        inferred = str(getattr(model, "model", "")).strip()
        self._model = model
        self._model_id = (model_id or inferred or type(model).__name__)[:256]
        self._temperature = temperature
        self._expression_capabilities = expression_capabilities
        self._identity_frame = identity_frame
        self._semantic_boundary_reviewer = semantic_boundary_reviewer
        self._source_closure_reviewer = source_closure_reviewer
        self._recovery_prompt_mode = recovery_prompt_mode
        self._contextual_grounding_reviewer = contextual_grounding_reviewer
        self._recall = recall_coordinator

    def install_recall_coordinator(self, coordinator: RecallCoordinator) -> None:
        if (
            self._recall is not None
            and self._recall is not coordinator
            and not self._recall.is_closed
        ):
            raise ValueError("proposal adapter recall coordinator is already installed")
        self._recall = coordinator

    def _recall_available(self, request: ModelInput) -> bool:
        return (
            model_content_allows_recall(request.model_content_json)
            and self._recall is not None
            and self._recall.is_available(
                RecallCursor(
                    world_revision=request.evaluated_world_revision,
                    deliberation_revision=request.evaluated_deliberation_revision,
                    ledger_sequence=request.evaluated_ledger_sequence,
                ),
                trigger_ref=request.trigger_ref,
            )
        )

    async def propose(self, request: ModelInput) -> ModelOutput:
        return await self._complete(
            request=request,
            quick_recovery=False,
            provisional=False,
            failure_code=None,
        )

    async def propose_provisional(self, request: ModelInput) -> ModelOutput:
        """Author one independently useful text beat with no hidden retry."""

        return await self._complete(
            request=request,
            quick_recovery=False,
            provisional=True,
            failure_code=None,
        )

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        if not failure_code:
            raise ValueError("quick recovery requires a failure code")
        return await self._complete(
            request=request,
            quick_recovery=True,
            provisional=False,
            failure_code=failure_code[:64],
        )

    async def _complete(
        self,
        *,
        request: ModelInput,
        quick_recovery: bool,
        failure_code: str | None,
        provisional: bool = False,
    ) -> ModelOutput:
        expected_cursor = RecallCursor(
            world_revision=request.evaluated_world_revision,
            deliberation_revision=request.evaluated_deliberation_revision,
            ledger_sequence=request.evaluated_ledger_sequence,
        )
        recall_trace: TrustedRecallTrace | None = None
        prefetch_trace: TrustedRecallTrace | None = None
        if (
            self._recall_available(request)
            and self._recall is not None
            and not quick_recovery
            and not provisional
        ):
            prefetch_trace = await self._recall.await_scheduled_prefetch(
                expected_cursor=expected_cursor,
                trigger_ref=request.trigger_ref,
            )
            if prefetch_trace is not None:
                try:
                    request = request.model_copy(
                        update={
                            "model_content_json": augment_model_content_with_recall(
                                request.model_content_json,
                                verify_trusted_recall_trace(prefetch_trace),
                            )
                        }
                    )
                except ValueError:
                    logger.warning(
                        "recall prefetch could not augment model Context "
                        "trigger=%s",
                        request.trigger_ref,
                        exc_info=True,
                    )
                    prefetch_trace = None
        messages = self._messages(
            request=request,
            quick_recovery=quick_recovery,
            provisional=provisional,
            failure_code=failure_code,
        )
        temperature = 0.25 if quick_recovery else self._temperature
        repair_messages = messages
        metered = getattr(self._model, "complete_with_usage", None)
        usage: ModelUsageProvenance | None = None
        if callable(metered):
            result = await metered(messages, temperature=temperature)
            if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], str):
                raise ValueError("metered provider result must be (text, usage)")
            raw, usage_raw = result
            usage = ModelUsageProvenance.model_validate(usage_raw)
        else:
            complete_json = getattr(self._model, "complete_json", None)
            raw = await (
                complete_json(messages, temperature=temperature)
                if callable(complete_json)
                else self._model.complete(messages, temperature=temperature)
            )
        recall_allowed = model_content_allows_recall(request.model_content_json)
        if not recall_allowed and _parse_character_recall_request(raw) is not None:
            raise ValueError("character recall budget is already consumed")
        recall_request = (
            _parse_character_recall_request(raw)
            if recall_allowed
            and self._recall_available(request)
            and not quick_recovery
            and not provisional
            and not expression_episode_provider_slots_active()
            else None
        )
        if recall_request is None and self._recall is not None:
            self._recall.discard_scheduled_prefetch(
                expected_cursor,
                trigger_ref=request.trigger_ref,
            )
        if recall_request is not None:
            recall_timeout = fit_secondary_call_timeout(8.0)
            if recall_timeout is None:
                raise TimeoutError("character recall completion budget exhausted")
            if not claim_secondary_provider_slot("recall"):
                raise TimeoutError("character recall secondary provider slot is unavailable")
            accessibility_seed = (
                f"character-recall:{request.call_id}:"
                f"{_digest(recall_request.model_dump(mode='json'))}"
            )
            if prefetch_trace is None:
                prefetch_trace, recall_trace = await perform_character_recall_with_prefetch(
                    self._recall,
                    request=recall_request,
                    accessibility_seed=accessibility_seed,
                    expected_cursor=expected_cursor,
                    trigger_ref=request.trigger_ref,
                    timeout_seconds=recall_timeout,
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
            request = request.model_copy(
                update={
                    "model_content_json": augment_model_content_with_recall(
                        model_content_json,
                        audit_trace,
                    )
                }
            )
            followup = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "Here is the bounded read-only recall result you chose to request. "
                        "It is reference material, not a behavior instruction. Decide the final "
                        "ExpressionDraft yourself; no further recall call remains in this turn. "
                        "Copy source_refs only when a factual clause is actually supported. "
                        "Return the final raw JSON ExpressionDraft only.\n"
                        + recall_followup_evidence_json(
                            prefetch=prefetch_audit,
                            character_pull=audit_trace,
                        )
                    ),
                },
            ]
            repair_messages = followup
            second_usage: ModelUsageProvenance | None = None
            recall_timeout = fit_secondary_call_timeout(8.0)
            if recall_timeout is None:
                raise TimeoutError("character recall follow-up budget exhausted")
            async with asyncio.timeout(recall_timeout):
                if callable(metered):
                    result = await metered(followup, temperature=temperature)
                    if (
                        not isinstance(result, tuple)
                        or len(result) != 2
                        or not isinstance(result[0], str)
                    ):
                        raise ValueError("metered recall result must be (text, usage)")
                    raw, usage_raw = result
                    second_usage = ModelUsageProvenance.model_validate(usage_raw)
                else:
                    complete_json = getattr(self._model, "complete_json", None)
                    raw = await (
                        complete_json(followup, temperature=temperature)
                        if callable(complete_json)
                        else self._model.complete(followup, temperature=temperature)
                    )
            usage = _combine_usage(usage, second_usage, request.call_id)
        if quick_recovery and self._recovery_prompt_mode == "contextual_failure":
            self._validate_contextual_failure_draft(raw)
            await self._review_contextual_failure_grounding(
                request=request,
                raw=raw,
            )
        episode_disposition = None
        try:
            episode_value = _parse_json_object(raw)
        except ValueError:
            episode_value = None
        if isinstance(episode_value, dict) and "episode_disposition" in episode_value:
            raw_disposition = episode_value.pop("episode_disposition")
            if raw_disposition not in {
                "complete_without_more",
                "append",
                "cancel_pending",
                "supersede_pending",
            }:
                raise ValueError("invalid expression episode disposition")
            if provisional:
                raise ValueError("provisional author cannot settle the episode")
            episode_disposition = raw_disposition
            raw = json.dumps(episode_value, ensure_ascii=False, separators=(",", ":"))
        # A provisional slot is the turn's second and final provider call.
        # It therefore uses only deterministic parsing/claim/epistemic gates;
        # semantic review or corrective completion would be a forbidden third
        # call. Full expression keeps its established reviewers.
        # The general source-closure reviewer subsumes the old first-contact
        # identity/counterpart check. Running both made a grounded continuation
        # pay twice and allowed the narrower review to erase context-supported
        # material before the complete review could see it.
        if (
            not provisional
            and not expression_episode_provider_slots_active()
            and self._source_closure_reviewer is None
        ):
            raw = await self._review_identity_and_counterpart_if_needed(request=request, raw=raw)
        source_corrective_spent = False
        if (
            not provisional
            and not expression_episode_provider_slots_active()
            and self._source_closure_reviewer is not None
        ):
            review = await review_expression_source_closure(
                reviewer=self._source_closure_reviewer,
                request=request,
                raw=raw,
                identity_frame=self._identity_frame,
            )
            if review is not None and review.decision == "unsupported":
                violation = source_closure_violation(review)
                repair_timeout = fit_secondary_call_timeout(
                    _WORLD_CLAIM_REPAIR_TIMEOUT_SECONDS
                )
                if repair_timeout is None:
                    raise ValueError(violation)
                raw = await self._repair_structural_violation(
                    messages=repair_messages,
                    raw=raw,
                    violation=violation,
                    timeout_seconds=repair_timeout,
                )
                source_corrective_spent = True
                corrected_review = await review_expression_source_closure(
                    reviewer=self._source_closure_reviewer,
                    request=request,
                    raw=raw,
                    identity_frame=self._identity_frame,
                )
                if (
                    corrected_review is not None
                    and corrected_review.decision == "unsupported"
                ):
                    raise ValueError(source_closure_violation(corrected_review))
        try:
            raw_proposal = _proposal_from_model_text(
                raw=raw,
                request=request,
                capabilities=self._expression_capabilities,
                quick_recovery=quick_recovery,
                stable_identity_source_refs=self._stable_identity_source_refs,
            )
            if episode_disposition is not None:
                raw_proposal = {
                    **raw_proposal,
                    "episode_disposition": episode_disposition,
                }
            if provisional:
                validate_provisional_proposal(raw_proposal)
        except (TypeError, ValueError) as exc:
            violation = str(exc)
            if (
                quick_recovery
                or provisional
                or expression_episode_provider_slots_active()
                or source_corrective_spent
            ):
                raise
            # A structural near-miss (claim bookkeeping, beat shape, later
            # contract) regularly rides on a perfectly good visible reply.
            # One corrective call naming the exact violation preserves the
            # honest answer; the corrected draft still passes the full
            # materializer, so no validation gate is loosened.  The retry is
            # deadline-aware: when the Deliberation attempt budget cannot fit
            # another completion, skip it so the recovery lane (which the
            # host will actually deliver) gets the remaining time instead.
            repair_timeout = fit_secondary_call_timeout(_WORLD_CLAIM_REPAIR_TIMEOUT_SECONDS)
            if repair_timeout is None:
                logger.warning(
                    "structural corrective retry skipped: attempt budget exhausted violation=%s",
                    violation[:200],
                )
                raise
            raw = await self._repair_structural_violation(
                messages=repair_messages,
                raw=raw,
                violation=violation,
                timeout_seconds=repair_timeout,
            )
            raw_proposal = _proposal_from_model_text(
                raw=raw,
                request=request,
                capabilities=self._expression_capabilities,
                quick_recovery=quick_recovery,
                stable_identity_source_refs=self._stable_identity_source_refs,
            )
        return ModelOutput(
            model_id=self._model_id,
            model_version=self.VERSION,
            raw_proposal=raw_proposal,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            usage=usage,
            episode_disposition=episode_disposition,
            recall_trace=recall_trace,
            prefetch_trace=prefetch_trace,
        )

    @staticmethod
    def _validate_contextual_failure_draft(raw: str) -> None:
        """Require one actual World-grounded reason before emergency delivery."""

        value = _parse_json_object(raw)
        wrapped = value.get("expression_draft")
        if set(value) == {"expression_draft"} and isinstance(wrapped, dict):
            value = wrapped
        draft = ExpressionDraft.model_validate_json(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            strict=True,
        )
        if not any(claim.scope in {"current_world", "past_world"} for claim in draft.world_claims):
            raise ValueError("contextual failure recovery requires a current/past World claim")

    async def _review_contextual_failure_grounding(
        self,
        *,
        request: ModelInput,
        raw: str,
    ) -> None:
        """Reject plausible-but-unsupported excuses even when their refs exist."""

        reviewer = self._contextual_grounding_reviewer
        if reviewer is None:  # Constructor keeps this fail-closed.
            raise ValueError("contextual grounding reviewer is unavailable")
        value = _parse_json_object(raw)
        wrapped = value.get("expression_draft")
        if set(value) == {"expression_draft"} and isinstance(wrapped, dict):
            value = wrapped
        draft = ExpressionDraft.model_validate_json(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            strict=True,
        )
        claims = draft.world_claims
        visible_text = "\n".join(beat.text for beat in draft.beats if beat.text is not None)
        messages = [
            {
                "role": "system",
                "content": (
                    "Independently review the complete visible reply and every supplied factual "
                    "claim against the cited pinned Context content. Every externally checkable "
                    "statement about current life, past events, shared history, the counterpart, "
                    "or stable identity must both be declared as a claim and be directly "
                    "supported by its cited Context source. Subjective feelings and connective "
                    "wording do not require claims. A valid source ref alone is not enough: "
                    "plausible elaboration, an unstated occurrence, changed activity, or invented "
                    "timing is unsupported. Return exactly one JSON object with decision "
                    "(supported or unsupported), unsupported_claim_indexes (zero-based), "
                    "undeclared_fact_fragments (short exact excerpts from visible_text), and "
                    "brief_reason. Do not rewrite the message."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "claims": tuple(
                            {
                                "claim_index": index,
                                **claim.model_dump(mode="json"),
                            }
                            for index, claim in enumerate(claims)
                        ),
                        "visible_text": visible_text,
                        "pinned_context": json.loads(
                            compact_recovery_model_facing_context(request.model_content_json)
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        reviewed_raw = await _bounded_review_call(
            reviewer,
            messages,
            temperature=0.0,
        )
        review = _ContextualClaimSupportReview.model_validate_json(
            json.dumps(
                _parse_json_object(reviewed_raw),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            strict=True,
        )
        indexes = review.unsupported_claim_indexes
        undeclared = review.undeclared_fact_fragments
        if any(
            isinstance(index, bool) or index < 0 or index >= len(claims) for index in indexes
        ) or len(indexes) != len(set(indexes)):
            raise ValueError("contextual grounding review returned invalid claim indexes")
        if any(
            not fragment.strip() or fragment not in visible_text for fragment in undeclared
        ) or len(undeclared) != len(set(undeclared)):
            raise ValueError("contextual grounding review returned invalid undeclared fragments")
        if review.decision == "supported" and (indexes or undeclared):
            raise ValueError("supported contextual review cannot reject reply content")
        if review.decision == "unsupported" and not (indexes or undeclared):
            raise ValueError(
                "unsupported contextual review must identify a claim or visible fragment"
            )
        if review.decision != "supported":
            raise ValueError(
                "contextual failure recovery claim is not supported by pinned World context"
            )

    async def _repair_structural_violation(
        self,
        *,
        messages: list[dict[str, str]],
        raw: str,
        violation: str,
        timeout_seconds: float = _WORLD_CLAIM_REPAIR_TIMEOUT_SECONDS,
    ) -> str:
        is_claim = is_world_claim_violation(violation)
        corrective = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    claim_repair_instruction(violation)
                    if is_claim
                    else shape_repair_instruction(violation)
                ),
            },
        ]
        async with asyncio.timeout(timeout_seconds):
            complete_json = getattr(self._model, "complete_json", None)
            corrected = await (
                complete_json(corrective, temperature=0.25)
                if callable(complete_json)
                else self._model.complete(corrective, temperature=0.25)
            )
        if is_claim:
            logger.warning("world-claim corrective retry produced a corrected draft")
            record_claim_repair()
        else:
            logger.warning("draft-shape corrective retry produced a corrected draft")
            record_shape_repair()
        return corrected

    async def _review_identity_and_counterpart_if_needed(
        self, *, request: ModelInput, raw: str
    ) -> str:
        """Fail closed on first-contact identity swaps and invented user facts.

        This is deliberately a bounded review seam rather than a growing list
        of location, occupation, group-membership, and history regexes.  It is
        entered for question-bearing first contact (where an invented premise
        is both common and especially damaging), or whenever the deterministic
        speaker-name invariant sees a possible self-name address.  Established
        conversation therefore does not pay a second model round trip on every
        ordinary question.
        """

        reviewer = self._semantic_boundary_reviewer
        identity = self._identity_frame
        trigger = request.trigger_message
        if identity is None or trigger is None:
            return raw
        draft = _parse_json_object(raw)
        wrapped = draft.get("expression_draft")
        if set(draft) == {"expression_draft"} and isinstance(wrapped, dict):
            draft = wrapped
        texts = _draft_texts(draft)
        if not texts:
            return raw
        combined = "\n".join(texts)
        possible_name_swap = _addresses_counterpart_as_companion_name(
            combined, companion_name=identity.companion_name
        )
        if reviewer is None:
            if possible_name_swap:
                raise ValueError("reply uses companion name as counterpart address")
            return raw
        context = _parse_context_object(request.model_content_json)
        if not possible_name_swap and not (
            _is_first_contact_context(context) and ("?" in combined or "？" in combined)
        ):
            return raw
        evidence = _counterpart_evidence_material(context)
        allowed_refs = _counterpart_evidence_source_refs(evidence) | {trigger.observation_ref}
        messages = [
            {
                "role": "system",
                "content": (
                    "Review one first-contact reply for two hard semantic boundaries. Return exactly "
                    "one JSON object with decision, replacement_text, "
                    "addresses_counterpart_as_companion_name, contains_counterpart_fact_premise, "
                    "premise_source_refs, and brief_reason. The decision is accept or replace. "
                    "First, the speaker is companion_name and the other person is counterpart_name: "
                    "the reply must not greet, address, or identify the other person using companion_name. "
                    "Second, a question contains a counterpart fact premise when it assumes rather than "
                    "asks for a location, membership, occupation, relationship, personal history, or prior "
                    "occurrence. A genuinely open question that asks the counterpart to supply the unknown "
                    "fact is not a premise. A factual premise is supported only by explicit semantic content "
                    "in current_trigger or counterpart_evidence, and must copy its exact allowed source ref. "
                    "Names and plausible stereotypes are never evidence. Replace any identity swap or "
                    "unsupported premise with one natural reply that preserves the conversational intent "
                    "without either problem. Do not mention review, evidence, configuration, or source refs "
                    "in replacement_text."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "companion_name": identity.companion_name,
                        "counterpart_name": identity.counterpart_name,
                        "current_trigger": {
                            "text": trigger.text,
                            "source_ref": trigger.observation_ref,
                        },
                        "proposed_texts": texts,
                        "counterpart_evidence": evidence,
                        "allowed_source_refs": sorted(allowed_refs),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        with model_call_scope("world_v2_identity_counterpart_review"):
            reviewed_raw = await _bounded_review_call(reviewer, messages, temperature=0.1)
        review = _parse_identity_and_counterpart_review(reviewed_raw)
        _validate_identity_and_counterpart_review(
            review=review,
            allowed_refs=allowed_refs,
        )
        if review.decision == "accept":
            if possible_name_swap:
                raise ValueError("reply uses companion name as counterpart address")
            return raw
        if review.replacement_text is None:
            raise ValueError("identity/counterpart replacement omitted replacement text")
        if _addresses_counterpart_as_companion_name(
            review.replacement_text, companion_name=identity.companion_name
        ):
            raise ValueError("replacement uses companion name as counterpart address")
        claims = draft.get("world_claims")
        return _replace_draft_text(
            draft,
            text=review.replacement_text,
            world_claims=list(claims) if isinstance(claims, list) else [],
        )

    def _messages(
        self,
        *,
        request: ModelInput,
        quick_recovery: bool,
        failure_code: str | None,
        provisional: bool = False,
    ) -> list[dict[str, str]]:
        return self._model_led_messages(
            request=request,
            quick_recovery=quick_recovery,
            failure_code=failure_code,
            provisional=provisional,
        )

    def _model_led_messages(
        self,
        *,
        request: ModelInput,
        quick_recovery: bool,
        failure_code: str | None,
        provisional: bool,
    ) -> list[dict[str, str]]:
        """Expose capability and truth boundaries without directing behavior."""

        schema = (
            "This is a provisional first beat: timing_choice must be now and beats must "
            "contain exactly one independently useful text beat."
            if provisional
            else (
                "This is a recovery attempt: timing_choice must be now and beats must "
                "contain exactly one useful text beat."
                if quick_recovery
                else (
                    "Choose timing_choice now, later, or silent. Choose the number, modalities, "
                    "cadence, stance, and content of beats yourself. later needs delay_seconds "
                    "and expires_after_seconds; silent has no beats. You may optionally set "
                    "episode_disposition to complete_without_more, append, cancel_pending, or "
                    "supersede_pending."
                )
            )
        )
        system = (
            "Decide the next expression as the independent person in the private identity frame. "
            "The supplied World context contains authoritative facts and non-authoritative advisory "
            "signals; use both as reference, never as a script. You own the motive, tone, timing, "
            "warmth, distance, questions, silence, message count, and wording. Do not follow a canned "
            "social rule or optimize for being agreeable. "
            + self._identity_instruction()
            + "Return one raw JSON ExpressionDraft with timing_choice, beats, stance, "
            "brief_rationale, confidence, and world_claims. "
            + expression_draft_shape_contract()
            + " "
            + schema
            + " Use only the supplied expression_capabilities. Do not return host IDs, hashes, "
            "Actions, receipts, deliveries, consent, capabilities, or World mutations. "
            "When you choose to assert an externally checkable current/past/shared/user or stable "
            "fact, declare it in world_claims and copy exact matching source_refs from Context. "
            "This includes first-person biography, relatives, past experiences, and enduring "
            "preferences; before returning JSON, silently verify that every such clause in the "
            "visible beats is covered by a declaration and pinned evidence. If evidence is absent, "
            "express uncertainty, ask an open question, or omit that factual clause. Never add an "
            "unlisted person, event, or past occurrence to your own biography merely to create "
            "rapport or an analogy. "
            "Subjective feelings, uncertainty, imagination, questions, and freely chosen future "
            "intentions are not committed occurrences and need no factual source. Never use a "
            "companion reply as evidence for a user fact. Attachment metadata proves only that an "
            "attachment exists unless perception_results describes it. "
            "You may create response_expectation only when you genuinely expect a reply. If Context "
            "contains a pending response_expectation advisory, assess it in this same cognition with "
            "fulfilled, superseded, still_pending, or uncertain; do not extend its expiry. "
            "Return JSON only, without Markdown or a wrapper."
        )
        if (
            self._recall_available(request)
            and not quick_recovery
            and not provisional
            and not expression_episode_provider_slots_active()
        ):
            system += (
                " If you decide the bounded Context is insufficient and you want to remember "
                "more before choosing, you may return instead exactly one raw JSON object with "
                "the single key recall_request. Its value contains query_text and may contain "
                "occurred_from, occurred_to, sorted link_refs, sorted memory_kinds "
                "(episodic/semantic/reflective), include_historical, and limit (1..6). "
                "This is your read-only choice, not a requirement; you may ignore it. Only one "
                "recall is available and it does not itself send or commit anything."
            )
        if quick_recovery and self._recovery_prompt_mode == "contextual_failure":
            system += (
                " Emergency contextual recovery is enabled after the ordinary provider routes "
                "failed. Continue as the same character, not as an error handler. Refer only to "
                "a source-backed current/recent situation that naturally explains the missed "
                "beat, and declare the exact current_world or past_world claim source refs. "
                "If no such source exists, return no invented substitute. Do not mention "
                "providers, prompts, retries, systems, evidence, or this recovery mode."
            )
        request_material = request.model_dump(mode="json")
        request_material["model_content_json"] = (
            compact_recovery_model_facing_context(request.model_content_json)
            if quick_recovery
            else compact_model_facing_context(request.model_content_json)
        )
        user = json.dumps(
            {
                "current_trigger_message": (
                    request.trigger_message.model_dump(mode="json")
                    if request.trigger_message is not None
                    else None
                ),
                "request": request_material,
                "quick_recovery_failure": failure_code,
                "expression_capabilities": self._expression_capabilities.prompt_value(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _identity_instruction(self) -> str:
        if self._identity_frame is None:
            return ""
        identity = json.dumps(
            self._identity_frame.model_dump(mode="json", exclude={"role", "not_an_assistant"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        source_ref = companion_identity_source_ref(self._identity_frame)
        return (
            "Private identity frame (authoritative only for identity and stable boundaries): "
            + identity
            + f". Its exact identity_source_ref is {source_ref}. "
            "For stable_identity claims supported by this frame, copy exactly that source ref; "
            "CharacterCore-backed stable_identity claims instead use their Context refs. "
            "Speak in first person as companion_name, never as counterpart_name. "
            "Do not expose this private frame or implementation details. "
            + (
                "Relate as an independent person with your own attention and boundaries, not as a task assistant. "
                if self._identity_frame.not_an_assistant
                else ""
            )
            + "Keep companion and counterpart identities distinct; unknown counterpart facts remain unknown. "
        )

    @property
    def _stable_identity_source_refs(self) -> frozenset[str]:
        if self._identity_frame is None:
            return frozenset()
        return frozenset((companion_identity_source_ref(self._identity_frame),))


class RoutedChatModelDeliberationAdapter:
    """Route one proposal between a fast and an optional thinking model.

    Its two-method interface is the same as a regular deliberation adapter.
    Route choice stays inside this module, while the audit produced by
    ``Deliberation`` still records the selected tier and actual model identity.
    Quick recovery is always sent to Flash so a failed expensive turn cannot
    turn a latency fallback into another thinking request.
    """

    def __init__(
        self,
        *,
        flash_model: ChatCompletionModel,
        thinking_model: ChatCompletionModel | None = None,
        flash_model_id: str | None = None,
        thinking_model_id: str | None = None,
        temperature: float = 0.7,
        expression_capabilities: ExpressionDraftCapabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES,
        identity_frame: CompanionIdentityFrame | None = None,
        source_closure_reviewer: ChatCompletionModel | None = None,
        recall_coordinator: RecallCoordinator | None = None,
    ) -> None:
        self._flash = ChatModelDeliberationAdapter(
            model=flash_model,
            model_id=flash_model_id,
            temperature=temperature,
            expression_capabilities=expression_capabilities,
            identity_frame=identity_frame,
            semantic_boundary_reviewer=flash_model,
            source_closure_reviewer=source_closure_reviewer,
            recall_coordinator=recall_coordinator,
        )
        self._thinking = (
            ChatModelDeliberationAdapter(
                model=thinking_model,
                model_id=thinking_model_id,
                temperature=temperature,
                expression_capabilities=expression_capabilities,
                identity_frame=identity_frame,
                semantic_boundary_reviewer=flash_model,
                source_closure_reviewer=source_closure_reviewer,
                recall_coordinator=recall_coordinator,
            )
            if thinking_model is not None
            else None
        )

    async def propose(self, request: ModelInput) -> ModelOutput:
        if request.route.tier == "thinking":
            if self._thinking is None:
                raise RuntimeError("thinking deliberation route is not configured")
            return await self._thinking.propose(request)
        return await self._flash.propose(request)

    def install_recall_coordinator(self, coordinator: RecallCoordinator) -> None:
        self._flash.install_recall_coordinator(coordinator)
        if self._thinking is not None:
            self._thinking.install_recall_coordinator(coordinator)

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        return await self._flash.recover(request, failure_code)


def _parse_json_object(raw: str) -> dict[str, object]:
    """Accept one object, including a provider's accidental fenced JSON wrapper."""

    if not isinstance(raw, str):
        raise ValueError("chat model did not return text")
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ValueError("chat model returned an unclosed JSON fence")
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("chat model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("chat model did not return one JSON object")
    return value


def _parse_character_recall_request(raw: str) -> CharacterRecallRequest | None:
    try:
        value = _parse_json_object(raw)
    except ValueError:
        return None
    if "recall_request" not in value:
        return None
    if set(value) != {"recall_request"} or not isinstance(value["recall_request"], dict):
        raise ValueError("recall choice must contain only one recall_request object")
    return CharacterRecallRequest.model_validate_json(
        json.dumps(
            value["recall_request"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _combine_usage(
    first: ModelUsageProvenance | None,
    second: ModelUsageProvenance | None,
    call_id: str,
) -> ModelUsageProvenance | None:
    if first is None and second is None:
        return None
    if first is None or second is None:
        raise ValueError("recall provider metering is partial")
    identity = (
        first.route_class,
        first.token_provenance,
        first.transport,
        first.provider,
    )
    if identity != (
        second.route_class,
        second.token_provenance,
        second.transport,
        second.provider,
    ):
        raise ValueError("recall provider usage identities do not match")
    material = {
        "usage_contract": "model-usage.1",
        "route_class": first.route_class,
        "input_tokens": first.input_tokens + second.input_tokens,
        "output_tokens": first.output_tokens + second.output_tokens,
        "thinking_tokens": first.thinking_tokens + second.thinking_tokens,
        "token_provenance": first.token_provenance,
        "transport": first.transport,
        "provider": first.provider,
        "provider_usage_ref": (
            f"provider-usage:recall-pair:{_digest((call_id, first.provider_usage_ref, second.provider_usage_ref))}"
        ),
    }
    return ModelUsageProvenance(
        **material,
        provider_usage_hash=_digest(material),
    )


def _parse_identity_and_counterpart_review(raw: str) -> _IdentityAndCounterpartReview:
    value = _parse_json_object(raw)
    wrapped = value.get("identity_counterpart_review")
    if set(value) == {"identity_counterpart_review"} and isinstance(wrapped, dict):
        value = wrapped
    refs = value.get("premise_source_refs", ())
    if isinstance(refs, list):
        refs = tuple(refs)
    replacement = value.get("replacement_text")
    if isinstance(replacement, str):
        replacement = replacement.strip() or None
    reason = value.get("brief_reason") or "First-contact identity review."
    return _IdentityAndCounterpartReview.model_validate(
        {
            **value,
            "replacement_text": replacement,
            "premise_source_refs": refs,
            "brief_reason": str(reason)[:240],
        }
    )


def _validate_identity_and_counterpart_review(
    *, review: _IdentityAndCounterpartReview, allowed_refs: set[str]
) -> None:
    if not set(review.premise_source_refs).issubset(allowed_refs):
        raise ValueError("identity reviewer cited unavailable counterpart authority")
    if review.contains_counterpart_fact_premise and not review.premise_source_refs:
        if review.decision == "accept":
            raise ValueError("identity reviewer accepted an unsupported counterpart premise")
    elif not review.contains_counterpart_fact_premise and review.premise_source_refs:
        raise ValueError("premise-free identity review cannot cite counterpart authority")
    if review.addresses_counterpart_as_companion_name and review.decision == "accept":
        raise ValueError("identity reviewer accepted companion name as counterpart address")
    if review.decision == "accept" and review.replacement_text is not None:
        raise ValueError("accepted identity review cannot replace text")
    if review.decision == "replace" and review.replacement_text is None:
        raise ValueError("identity/counterpart replacement omitted replacement text")


def _parse_context_object(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_first_contact_context(context: dict[str, object]) -> bool:
    """No prior companion-authored dialogue means the relationship is still opening."""

    slices = context.get("slices")
    if not isinstance(slices, dict):
        return True
    dialogue = slices.get("recent_dialogue")
    if not isinstance(dialogue, dict):
        return True
    items = dialogue.get("items")
    if not isinstance(items, list):
        return True
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        speaker = str(value.get("speaker") or value.get("actor") or "").lower()
        if any(label in speaker for label in ("companion", "assistant", "character")):
            return False
    return True


def _counterpart_evidence_material(context: dict[str, object]) -> dict[str, object]:
    """Keep only lanes that can contain claims about the other person."""

    slices = context.get("slices")
    if not isinstance(slices, dict):
        return {}
    return {
        name: slices[name]
        for name in (
            "recent_dialogue",
            "relevant_facts",
            "active_memory_candidates",
            "relationship_slice",
        )
        if name in slices
    }


def _counterpart_evidence_source_refs(evidence: dict[str, object]) -> set[str]:
    """Read provenance tokens from item envelopes, never semantic values."""

    refs: set[str] = set()
    for lane in evidence.values():
        if not isinstance(lane, dict):
            continue
        items = lane.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("item_ref", "source_ref"):
                ref = item.get(key)
                if isinstance(ref, str):
                    refs.add(ref)
            bindings = item.get("source_bindings")
            if isinstance(bindings, list):
                for binding in bindings:
                    if isinstance(binding, dict) and isinstance(binding.get("ref"), str):
                        refs.add(binding["ref"])
    return refs


def _addresses_counterpart_as_companion_name(text: str, *, companion_name: str) -> bool:
    """Detect the narrow identity swap, without banning self-introduction."""

    escaped = re.escape(companion_name.strip())
    if not escaped:
        return False
    return bool(
        re.search(
            rf"(?:^|[。！？!?]\s*)"
            rf"(?:(?:嗨|你好|嘿|哈喽|hello)\s*[,，:：]?\s*)?"
            rf"{escaped}\s*[,，:：。！？!?]",
            text,
            flags=re.IGNORECASE,
        )
    )


def _draft_texts(draft: dict[str, object]) -> tuple[str, ...]:
    response = draft.get("response_text")
    if isinstance(response, str) and response:
        return (response,)
    beats = draft.get("beats")
    if not isinstance(beats, list):
        return ()
    return tuple(
        text
        for beat in beats
        if isinstance(beat, dict) and isinstance((text := beat.get("text")), str) and text
    )


def _merge_overflowing_later_beats(
    value: dict[str, object], *, capabilities: ExpressionDraftCapabilities
) -> dict[str, object]:
    """Join an all-text later expression into the installed one-beat contract.

    A deferred reply arrives as one message when she comes back to the phone,
    so several drafted bubbles legitimately collapse into one text.  This is
    a structural normalization only — it never changes prose, timing, claims,
    or any other field, and anything but the exact overflow shape (later plus
    purely-text beats) is left for the ordinary validators to judge.
    """

    if value.get("timing_choice") != "later":
        return value
    beats = value.get("beats")
    if not isinstance(beats, list) or len(beats) <= capabilities.max_later_beats:
        return value
    texts: list[str] = []
    for beat in beats:
        if (
            not isinstance(beat, dict)
            or beat.get("modality") != "text"
            or not isinstance(beat.get("text"), str)
            or not beat["text"]
        ):
            return value
        texts.append(beat["text"])
    merged = "\n".join(texts)
    if len(merged) > 4_096:
        return value
    logger.warning(
        "later expression merged %d drafted text beats into the one-beat deferred contract",
        len(texts),
    )
    return {**value, "beats": [{"modality": "text", "text": merged}]}


def _replace_draft_text(
    draft: dict[str, object], *, text: str, world_claims: list[dict[str, object]]
) -> str:
    wrapped = draft.get("expression_draft")
    if set(draft) == {"expression_draft"} and isinstance(wrapped, dict):
        draft = dict(wrapped)
    else:
        draft = dict(draft)
    if isinstance(draft.get("response_text"), str):
        draft["response_text"] = text
    else:
        beats = draft.get("beats")
        retained: list[object] = []
        replaced = False
        if isinstance(beats, list):
            for beat in beats:
                if isinstance(beat, dict) and isinstance(beat.get("text"), str):
                    if not replaced:
                        retained.append({**beat, "text": text})
                        replaced = True
                    continue
                retained.append(beat)
        if not replaced:
            retained.append({"modality": "text", "text": text})
        draft["beats"] = retained
    draft["world_claims"] = world_claims
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))


def _proposal_from_model_text(
    *,
    raw: str,
    request: ModelInput,
    capabilities: ExpressionDraftCapabilities,
    quick_recovery: bool,
    stable_identity_source_refs: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Materialize one ordinary reply from an LLM-owned expression draft.

    Computing hashes, target bindings and effect identifiers is authority work,
    not linguistic work.  Accepting a small draft therefore keeps the model
    free to decide *what* it says while making the actual Action replayable and
    impossible to redirect by a malformed completion.  Full proposal envelopes
    remain accepted for non-chat adapters that intentionally produce them.
    """

    value = _parse_json_object(raw)
    # Some OpenAI-compatible providers follow the semantic type name in the
    # prompt and wrap an otherwise valid draft.  Accept only the exact,
    # single-key wrapper so unrelated metadata cannot bypass draft validation.
    wrapped = value.get("expression_draft")
    was_wrapped = False
    if set(value) == {"expression_draft"} and isinstance(wrapped, dict):
        value = wrapped
        was_wrapped = True
    if was_wrapped and "proposal_id" in value:
        raise ValueError("wrapped expression draft cannot contain a complete proposal")
    if "proposal_id" in value:
        return value
    beats = value.get("beats")
    if isinstance(beats, list):
        normalized_beats: list[object] = []
        for beat in beats:
            if (
                isinstance(beat, dict)
                and set(beat) == {"text"}
                and isinstance(beat.get("text"), str)
                and beat["text"]
            ):
                normalized_beats.append({"modality": "text", "text": beat["text"]})
            else:
                normalized_beats.append(beat)
        value = {**value, "beats": normalized_beats}
    if "beats" in value or "timing_choice" in value:
        # ``stance`` and ``brief_rationale`` are bounded audit metadata, not
        # visible prose, World evidence, or Action authority.  Live providers
        # occasionally return a complete, otherwise valid ExpressionDraft
        # while omitting one or both fields.  Re-running the provider merely
        # to regenerate metadata discards good text and can consume the whole
        # interactive deadline.  Complete only absent metadata locally;
        # malformed supplied values, claims, beats, timing and capabilities
        # still pass through the ordinary strict validators unchanged.
        value = {
            "stance": "compiler_default_unspecified",
            "brief_rationale": (
                "Model omitted draft metadata; compiler preserved separately "
                "validated visible content."
            ),
            **value,
        }
    value = _merge_overflowing_later_beats(value, capabilities=capabilities)
    if not quick_recovery and ("beats" in value or "timing_choice" in value):
        return materialize_expression_draft(
            value=value,
            request=request,
            capabilities=capabilities,
            stable_identity_source_refs=stable_identity_source_refs,
        ).model_dump(mode="json")
    if quick_recovery and ("beats" in value or "timing_choice" in value):
        draft = ExpressionDraft.model_validate_json(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            strict=True,
        )
        if (
            draft.timing_choice != "now"
            or len(draft.beats) != 1
            or draft.beats[0].modality != "text"
            or draft.beats[0].text is None
            or draft.response_expectation is not None
        ):
            raise ValueError("quick recovery ExpressionDraft must be one immediate text beat")
        if draft.world_claims:
            # A claim-bearing recovery cannot use the legacy MinimalReply
            # compatibility envelope: that envelope has no place to retain
            # claim declarations and historically discarded them before
            # source closure.  Materialize the full proposal so the same
            # Context-token and capability checks as an ordinary expression
            # remain authoritative.
            return materialize_expression_draft(
                value=value,
                request=request,
                capabilities=capabilities,
                stable_identity_source_refs=stable_identity_source_refs,
            ).model_dump(mode="json")
        value = {
            "response_text": draft.beats[0].text,
            # Expression stance is deliberately open vocabulary.  The legacy
            # MinimalReply envelope has only three compatibility labels, so a
            # valid recovery must narrow that descriptive label instead of
            # dropping the user's reply because the wording was novel.
            "stance": "answer_without_world_claims",
            "brief_rationale": draft.brief_rationale,
            "confidence": draft.confidence,
            "response_expectation_assessment": draft.response_expectation_assessment,
        }
    trigger = request.trigger_message
    if trigger is None:
        raise ValueError("ReplyDraft requires a verified current message")
    text = value.get("response_text")
    stance = value.get("stance")
    rationale = value.get("brief_rationale")
    confidence = value.get("confidence", 5_000)
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= 4_096
        or not isinstance(stance, str)
        or stance not in {"defer", "acknowledge_briefly", "answer_without_world_claims"}
        or not isinstance(rationale, str)
        or not 1 <= len(rationale) <= 1_024
        or isinstance(confidence, bool)
        or not isinstance(confidence, int)
        or not 0 <= confidence <= 10_000
    ):
        raise ValueError(
            "ReplyDraft has an invalid response_text, stance, rationale, or confidence"
        )
    identity = _digest(
        {
            "contract": "chat-reply-draft-materialization.1",
            "call_id": request.call_id,
            "trigger_ref": request.trigger_ref,
            "world_revision": request.evaluated_world_revision,
            "reply_target": trigger.reply_target,
            "text": text,
            "stance": stance,
        }
    )
    proposal_id = f"proposal:chat-reply:{identity}"
    payload_ref = f"payload:chat-reply:{identity}"
    payload_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    change_id = f"change:chat-reply:{identity}"
    plan_id = f"plan:chat-reply:{identity}"
    beat_id = f"beat:chat-reply:{identity}"
    intent_id = f"intent:chat-reply:{identity}"
    proposal = MinimalProposal(
        proposal_id=proposal_id,
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=trigger.observation_ref,
                evidence_kind="observed_message",
                source_world_revision=trigger.source_world_revision,
                immutable_hash=trigger.event_payload_hash,
            ),
        ),
        proposed_changes=(
            TypedChange(
                change_id=change_id,
                kind="expression_plan_transition",
                target_id=plan_id,
                transition="accept",
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="expression_plan_transition.v1",
                    value={
                        "plan_id": plan_id,
                        "overall_intent": "reply",
                        "ordering_policy": "dependencies",
                        "terminal_policy": "settle",
                        "beat_drafts": [
                            {
                                "beat_id": beat_id,
                                "inline_text": text,
                                "materialized_payload_ref": payload_ref,
                                "payload_hash": payload_hash,
                                "content_type": "text/plain",
                                "dependency_beat_ids": [],
                                "delay_window": None,
                                "cancel_policy": "cancel-before-dispatch",
                                "reconsider_policy": "reconsider-on-new-observation",
                                "merge_policy": "never",
                            }
                        ],
                    },
                ),
            ),
        ),
        action_intents=(
            ProposalActionIntent(
                intent_id=intent_id,
                kind="reply",
                layer="external_action",
                target=trigger.reply_target,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                causal_change_id=change_id,
                beat_ref=beat_id,
            ),
        ),
        confidence=confidence,
        brief_rationale=rationale,
        source_model_result="model-result:adapter-placeholder",
        response_text=text,
        stance=stance,
        response_expectation_assessment=value.get("response_expectation_assessment"),
    )
    return proposal.model_dump(mode="json")


__all__ = [
    "ChatCompletionModel",
    "ChatModelDeliberationAdapter",
    "CompanionIdentityFrame",
    "companion_identity_source_ref",
    "RoutedChatModelDeliberationAdapter",
    "claim_repair_instruction",
    "shape_repair_instruction",
]
