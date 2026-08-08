"""Source-bound proactive and pulse deliberation with durable terminal outcomes.

This lane turns an *opportunity* into a model decision; it never turns a timer
into prose.  The eligible sources are accepted world settlements and explicitly
due conversation Threads/Commitments.  The model may choose an immediate
proactive message, a delayed follow-up, or silence.  Acceptance remains the
normal ExpressionPlan -> Budget -> Action chain.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Literal

from pydantic import Field

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .character_interior import CharacterInterior, InteriorOpportunity
from .character_interior.audit import recorded_character_interior_lineage
from .character_interior.contracts import _InteriorCapabilityManifest
from .character_interior.inbound_wire import (
    review_candidate_external_proposition_coverage,
    review_expression_with_candidate_external_coverage,
)
from .companion_identity import CompanionIdentityFrame
from .context_capsule import ContextCapsuleCompiler, InnerAdvisoryCandidate, InnerAdvisoryProjection
from .context_resolver import query_from_projection
from .deliberation import (
    ModelInput,
    ModelOutput,
    ModelRouterAdapter,
    ValidationTechnicalFailure,
)
from .production_proposal_grammar import compose_production_deliberation
from .errors import ConcurrencyConflict, IdempotencyConflict
from .event_identity import domain_idempotency_key
from .expression_plan_acceptance import (
    ExpressionPlanAcceptanceError,
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from .expression_draft import (
    ExpressionDraft,
    ExpressionDraftCapabilities,
    materialize_expression_plan_beats,
    normalize_expression_draft_wire,
    validate_expression_draft_capabilities,
    validate_expression_private_turn_state,
    world_claim_source_refs_by_scope,
)
from .interactive_turn_budget import InteractiveTurnBudgetPolicy
from .ledger import LedgerPort
from .model_facing_context import compact_chat_model_facing_context
from .proposal_audit import ProposalAuditCommit, ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    EventShareClaimBinding,
    EventSharePlanClaimBindingV2,
    ProactiveExpressionSourceBinding,
    ProactiveExpressionPlanSourceBindingV2,
    ProactiveOpportunityDecision,
    ProposalEvidenceRef,
    TypedChange,
    validate_proposal_envelope,
)
from .schema_core import FrozenModel
from .schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent
from .shared_private_invitation import pending_shared_private_invitation_advisories
from .social_initiative import (
    SITUATION_STIMULUS_EVENT_TYPES,
    SocialInitiativeCompiler,
    situation_stimulus_is_observable,
    technical_failure_point,
)

_LOG = logging.getLogger(__name__)


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class ProactiveDraft(ExpressionDraft):
    """The ordinary ExpressionDraft grammar, with proactive impulse audit.

    This intentionally does not have a one-message ``response_text`` escape
    hatch.  A proactive turn chooses the same zero/one/many beat plan as an
    inbound turn; only the causal source binding differs.
    """

    impulse_summary: str = Field(min_length=1, max_length=240)


class _ProactiveGroundingViolation(ValueError):
    """Stable hard-boundary coordinate without rejected candidate prose."""

    def __init__(self, *, code: str, path: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path


class _ProactiveSourceAuthorityUnavailable(RuntimeError):
    pass


class _UnavailableProactiveSourceAuthority:
    """Never substitutes the role author for an independent truth authority."""

    async def complete_json(self, _messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
        del temperature
        raise _ProactiveSourceAuthorityUnavailable


def _caused_by(exc: BaseException, error_type: type[BaseException]) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, error_type):
            return True
        current = current.__cause__ or current.__context__
    return False


def _validate_proactive_grounding(*, draft: ProactiveDraft, request: ModelInput) -> None:
    if not draft.beats:
        return
    try:
        context = json.loads(request.model_content_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _ProactiveGroundingViolation(
            code="proactive_grounding_context_invalid",
            path="model_content_json",
        ) from exc
    if not isinstance(context, dict):
        raise _ProactiveGroundingViolation(
            code="proactive_grounding_context_invalid",
            path="model_content_json",
        )
    allowed = world_claim_source_refs_by_scope(context=context)
    for claim in draft.world_claims:
        if claim.scope == "subjective_or_hypothetical":
            raise _ProactiveGroundingViolation(
                code="proactive_world_claim_scope_not_authoritative",
                path="world_claims[].scope",
            )
        permitted = allowed.get(claim.scope)
        if permitted is not None and not set(claim.source_refs).issubset(permitted):
            raise _ProactiveGroundingViolation(
                code="proactive_world_claim_source_lane_mismatch",
                path="world_claims[].source_refs",
            )


def _proactive_source_review_raw(draft: ProactiveDraft) -> str:
    """Adapt one factual proactive draft to the shared effect-review seam."""

    if not draft.beats:
        raise ValueError("proactive source review requires visible beats")
    value: dict[str, object] = {
        "timing_choice": draft.timing_choice,
        "beats": [item.model_dump(mode="json") for item in draft.beats],
        "stance": draft.stance,
        "brief_rationale": draft.brief_rationale,
        "impulse_summary": draft.impulse_summary,
        "confidence": draft.confidence,
        "world_claims": [claim.model_dump(mode="json") for claim in draft.world_claims],
    }
    if draft.timing_choice == "later":
        value.update(
            {
                "delay_seconds": draft.delay_seconds,
                "expires_after_seconds": draft.expires_after_seconds,
            }
        )
    return _canonical(value)


class _ProactiveSourceBindingError(ValueError):
    """A claimed proactive opportunity no longer binds committed authority."""


class _CharacterInteriorProactiveTransport:
    """Translate one pinned proactive capability through CharacterInterior.

    The existing durable proactive worker still owns source eligibility,
    claims, Proposal audit, budget and Action acceptance.  This transport owns no
    role prompt or provider: its only semantic crossing is
    :meth:`CharacterInterior.consider`.
    """

    VERSION = "character-interior-proactive.1"

    def __init__(
        self,
        *,
        character_interior: CharacterInterior,
        world_id: str,
        actor_ref: str,
        target: str,
        expression_capabilities: ExpressionDraftCapabilities,
        identity_frame: CompanionIdentityFrame | None = None,
        source_closure_reviewer=None,
        report_relative_reviewer=None,
        candidate_external_proposition_inventory_model=None,
    ) -> None:
        if not world_id or not actor_ref or not target:
            raise ValueError("Interior proactive transport binding is incomplete")
        self._interior = character_interior
        self._world_id = world_id
        self._actor_ref = actor_ref
        self._target = target
        self._capabilities = expression_capabilities
        self._identity_frame = identity_frame
        self._source_closure_reviewer = source_closure_reviewer
        self._report_relative_reviewer = report_relative_reviewer
        self._inventory_model = candidate_external_proposition_inventory_model

    def has_hedge_provider(self, _request: ModelInput) -> bool:
        return False

    def source_closure_review_enabled(self) -> bool:
        return self._source_closure_reviewer is not None or self._inventory_model is not None

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        # Deliberation may invoke this only as the same-author bounded
        # correction. CharacterInterior owns that correction internally and
        # caches the terminal InnerTurn, so a second semantic author call is
        # never made here.
        del failure_code
        return await self.propose(request)

    async def propose(self, request: ModelInput) -> ModelOutput:
        try:
            context = json.loads(request.model_content_json)
            logical_time = datetime.fromisoformat(context["logical_time"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("proactive Interior opportunity lacks pinned logical time") from exc
        if logical_time.tzinfo is None or logical_time.utcoffset() is None:
            raise ValueError("proactive Interior logical time must be timezone-aware")
        source_refs = tuple(dict.fromkeys(item.ref_id for item in request.trigger_evidence))
        if not source_refs or request.trigger_ref not in source_refs:
            raise ValueError("proactive Interior opportunity lacks trigger evidence")
        capability = self._capability(request=request, source_refs=source_refs)
        decision = await self._interior.consider(
            InteriorOpportunity(
                opportunity_ref=f"opportunity:proactive:{request.attempt_id}",
                inner_turn_ref=request.attempt_id,
                world_id=self._world_id,
                actor_ref=self._actor_ref,
                trigger_ref=request.trigger_ref,
                cursor=ProjectionCursor(
                    world_revision=request.evaluated_world_revision,
                    deliberation_revision=request.evaluated_deliberation_revision,
                    ledger_sequence=request.evaluated_ledger_sequence,
                ),
                logical_time=logical_time,
                purpose="proactive_contact",
                source_refs=source_refs,
                capability_manifest=capability,
                context_note=(
                    "A source-bound proactive contact opportunity is due; the character "
                    "freely owns now, later, silent, wording and message count."
                ),
            )
        )
        if decision.status == "technical_failure":
            failure = decision.failure_code or "unknown"
            raise ValidationTechnicalFailure(
                "authored_subcall_timeout"
                if "timeout" in failure
                else "authored_expression_reselection_invalid"
                if "invalid" in failure
                else "authored_subcall_exception"
            ) from RuntimeError("character Interior proactive failure: " + failure)
        if decision.status != "decided" or decision.decision is None:
            # Proactive silence is the explicit timing_choice=silent payload;
            # generic model_silent would discard the capability binding.
            raise ValueError("proactive Interior result lacks an explicit decision payload")
        draft = self._draft(decision=decision)
        try:
            _validate_proactive_grounding(draft=draft, request=request)
        except _ProactiveGroundingViolation as violation:
            if violation.code == "proactive_grounding_context_invalid":
                raise
            # The character completed her one semantic choice.  A false or
            # lane-mismatched factual declaration is an authority rejection,
            # not a second author path and not provider downtime.
            grounding = "rejected"
        else:
            grounding = await self._grounding_outcome(draft=draft, request=request)
        proposal = _materialize_interior_proactive_draft(
            draft=draft,
            request=request,
            target=self._target,
            expression_capabilities=self._capabilities,
            grounding_outcome=grounding,
        )
        lineage = decision.author_lineage
        if lineage is None:
            raise ValueError("proactive Interior decision lacks author lineage")
        return ModelOutput(
            model_id=lineage.model_id,
            model_version=lineage.model_version,
            raw_proposal=proposal.model_dump(mode="json"),
            winning_model_call_id=lineage.model_call_id,
            winning_request_hash=lineage.request_hash.removeprefix("sha256:"),
            character_interior_lineage=recorded_character_interior_lineage(
                decision,
                purpose="proactive_contact",
                subject_ref=decision.opportunity_ref,
                capability_ref=capability.capability_ref,
            ),
        )

    def _capability(
        self,
        *,
        request: ModelInput,
        source_refs: tuple[str, ...],
    ) -> _InteriorCapabilityManifest:
        payload = {
            "contract": "character-interior-proactive-capability.1",
            "expression_capabilities": self._capabilities.prompt_value(),
            "source_opportunity": _proactive_source_frame(request.model_content_json),
            "target_ref": self._target,
        }
        payload_json = _canonical(payload)
        return _InteriorCapabilityManifest(
            capability_ref=f"capability:proactive:{request.attempt_id}",
            capability_kind="proactive_contact",
            payload_json=payload_json,
            payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
            source_refs=source_refs,
        )

    def _draft(self, *, decision) -> ProactiveDraft:  # type: ignore[no-untyped-def]
        outer = decision.decision
        if (
            not isinstance(outer, dict)
            or outer.get("contract") != "character-interior-purpose-decision.1"
            or outer.get("purpose") != "proactive_contact"
        ):
            raise ValueError("proactive Interior decision envelope is invalid")
        raw = outer.get("payload")
        if not isinstance(raw, dict) or raw.get("contract") != (
            "character-interior-proactive-contact-decision.1"
        ):
            raise ValueError("proactive Interior decision payload is invalid")
        if len(decision.attended_source_refs) > 8 or not decision.summary:
            raise ValueError("proactive Interior private turn state is out of bounds")
        value = dict(raw)
        value.pop("contract")
        value["private_turn_state"] = {
            "inner_state_summary": decision.summary,
            "attended_source_refs": list(decision.attended_source_refs),
        }
        normalized = normalize_expression_draft_wire(value)
        draft = ProactiveDraft.model_validate_json(_canonical(normalized), strict=True)
        validate_expression_draft_capabilities(
            draft=draft,
            capabilities=self._capabilities,
            provider_message_id=None,
        )
        return draft

    async def _grounding_outcome(
        self,
        *,
        draft: ProactiveDraft,
        request: ModelInput,
    ) -> Literal["not_required", "accepted", "rejected"]:
        if not draft.beats:
            return "not_required"
        if self._source_closure_reviewer is None:
            if self._inventory_model is None:
                return "not_required"
            try:
                inventory_only = await review_candidate_external_proposition_coverage(
                    inventory_model=self._inventory_model,
                    authority_reviewer=_UnavailableProactiveSourceAuthority(),
                    request=request,
                    raw=_proactive_source_review_raw(draft),
                    identity_frame=self._identity_frame,
                )
            except ValidationTechnicalFailure as exc:
                if _caused_by(exc, _ProactiveSourceAuthorityUnavailable):
                    raise ValidationTechnicalFailure(
                        "proactive_source_closure_reviewer_unavailable"
                    ) from exc
                raise
            if inventory_only.review is None:
                return "not_required"
            raise ValidationTechnicalFailure("proactive_source_closure_reviewer_unavailable")
        result = await review_expression_with_candidate_external_coverage(
            reviewer=self._source_closure_reviewer,
            inventory_model=self._inventory_model,
            report_relative_reviewer=self._report_relative_reviewer,
            request=request,
            raw=_proactive_source_review_raw(draft),
            identity_frame=self._identity_frame,
            model_visible_context_json=request.model_content_json,
            allow_report_relative_adjudication=True,
        )
        if result.review is None:
            return "not_required"
        return "accepted" if result.review.decision == "supported" else "rejected"


def _materialize_interior_proactive_draft(
    *,
    draft: ProactiveDraft,
    request: ModelInput,
    target: str,
    expression_capabilities: ExpressionDraftCapabilities,
    grounding_outcome: Literal["not_required", "accepted", "rejected"],
) -> DecisionProposal:
    """Compile an already-authored Interior expression into typed authority."""

    validate_expression_private_turn_state(
        value=draft.model_dump(mode="json"),
        request=request,
        capabilities=expression_capabilities,
    )
    identity_draft = draft.model_dump(mode="json")
    identity_draft.pop("private_turn_state", None)
    identity = _digest(
        {
            "contract": "character-interior-proactive-materialization.1",
            "capability_profile": expression_capabilities.profile_id,
            "call_id": request.call_id,
            "trigger_ref": request.trigger_ref,
            "world_revision": request.evaluated_world_revision,
            "draft": identity_draft,
        }
    )
    source_kind = _proactive_source_kind(request.model_content_json)
    source_evidence = next(
        (
            item
            for item in request.trigger_evidence
            if item.ref_id == request.trigger_ref and item.source_world_revision is not None
        ),
        None,
    )
    if source_kind is None or source_evidence is None:
        raise ValueError("proactive Interior draft lacks verified source authority")
    decision = ProactiveOpportunityDecision(
        source_kind=source_kind,
        source_event_ref=source_evidence.ref_id,
        source_payload_hash=source_evidence.immutable_hash,
        source_world_revision=source_evidence.source_world_revision,
        disposition=(
            "grounding_rejected"
            if grounding_outcome == "rejected"
            else {
                "now": "engage_now",
                "later": "engage_later",
                "silent": "silent_after_consideration",
            }[draft.timing_choice]
        ),
        decision_origin=("grounding_gate" if grounding_outcome == "rejected" else "model"),
    )
    common = dict(
        proposal_id=f"proposal:proactive:{identity}",
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=request.trigger_evidence,
        confidence=draft.confidence,
        brief_rationale=draft.brief_rationale,
        private_turn_state=draft.private_turn_state,
        behavior_tendency=(
            "remain_silent"
            if draft.timing_choice == "silent"
            else "respond"
            if draft.timing_choice == "now"
            else "defer"
        ),
        stance=draft.stance,
        display_strategy=(
            "withhold_for_now" if draft.timing_choice == "silent" else "model_selected_expression"
        ),
        timing_choice=draft.timing_choice,
        proactive_opportunity_decision=decision,
        impulse_summary=draft.impulse_summary,
        proactive_grounding_outcome=grounding_outcome,
    )
    if draft.timing_choice == "silent" or grounding_outcome == "rejected":
        return DecisionProposal(**common)
    due_window = None
    if draft.timing_choice == "later":
        assert draft.delay_seconds is not None and draft.expires_after_seconds is not None
        logical_time = datetime.fromisoformat(
            json.loads(request.model_content_json)["logical_time"]
        )
        due_window = (
            logical_time + timedelta(seconds=draft.delay_seconds),
            logical_time + timedelta(seconds=draft.expires_after_seconds),
        )
    change_id = f"change:proactive:{identity}"
    beat_drafts, intents = materialize_expression_plan_beats(
        draft=draft,
        identity=identity,
        namespace="proactive",
        change_id=change_id,
        target=target,
        provider_message_id=None,
        effective_windows=tuple(due_window for _ in draft.beats),
        text_now_action_kind="proactive_message",
    )
    plan_id = f"plan:proactive:{identity}"
    expression_payload: dict[str, object] = {
        "plan_id": plan_id,
        "overall_intent": "followup" if due_window else "proactive_message",
        "ordering_policy": "dependencies",
        "terminal_policy": "settle",
        "beat_drafts": beat_drafts,
        "proactive_source_plan_binding_v2": ProactiveExpressionPlanSourceBindingV2(
            source_kind=source_kind,
            source_event_ref=source_evidence.ref_id,
            source_payload_hash=source_evidence.immutable_hash,
            source_world_revision=source_evidence.source_world_revision,
            plan_id=plan_id,
            beat_payload_hashes=[intent.payload_hash for intent in intents],
            target_ref=target,
        ).model_dump(mode="json"),
        "world_claims": [item.model_dump(mode="json") for item in draft.world_claims],
    }
    if len(intents) == 1:
        expression_payload["proactive_source_binding"] = ProactiveExpressionSourceBinding(
            source_kind=source_kind,
            source_event_ref=source_evidence.ref_id,
            source_payload_hash=source_evidence.immutable_hash,
            source_world_revision=source_evidence.source_world_revision,
            response_payload_hash=intents[0].payload_hash,
            target_ref=target,
        ).model_dump(mode="json")
    change = TypedChange(
        change_id=change_id,
        kind="expression_plan_transition",
        target_id=plan_id,
        transition="accept",
        evidence_refs=(),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="expression_plan_transition.v1",
            value=expression_payload,
        ),
    )
    return DecisionProposal(
        **common,
        proposed_changes=(change,),
        action_intents=tuple(intents),
    )


def _proactive_lived_context(
    model_content_json: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Separate the role's lived view from ledger and validation transport.

    The complete pinned Capsule stays on ``ModelInput`` for source closure and
    Acceptance.  The role sees the same compact semantic view as interactive
    cognition, with its source-bound working self promoted beside the current
    opportunity instead of being buried inside serialized audit material.
    """

    compacted = compact_chat_model_facing_context(model_content_json)
    try:
        context = json.loads(compacted)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("proactive lived context requires Context JSON") from exc
    if not isinstance(context, dict):
        raise ValueError("proactive lived context requires a Context object")
    inner_life_snapshot = context.pop("inner_life_snapshot", None)
    if not isinstance(inner_life_snapshot, dict):
        raise ValueError("proactive lived context requires inner_life_snapshot")
    return context, inner_life_snapshot


def _proactive_binding_request(
    *,
    request: ModelInput,
    lived_context: dict[str, object],
) -> ModelInput:
    """Retain exact authority only for semantic items visible to the role.

    The role's compact view intentionally omits cryptographic and event-store
    transport.  The binder and reviewer need that transport, but must not gain
    facts the role never saw.  Selection is therefore joined back to the full
    Capsule by the compact item's stable semantic source ref.
    """

    try:
        full = json.loads(request.model_content_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("proactive binding requires Context JSON") from exc
    full_slices = full.get("slices") if isinstance(full, dict) else None
    visible_slices = lived_context.get("slices")
    if not isinstance(full, dict) or not isinstance(full_slices, dict):
        raise ValueError("proactive binding requires Context slices")
    if not isinstance(visible_slices, dict):
        raise ValueError("proactive binding requires lived Context slices")

    selected_slices: dict[str, object] = {}
    for name, visible_lane in visible_slices.items():
        full_lane = full_slices.get(name)
        if not isinstance(visible_lane, dict):
            continue
        if not isinstance(full_lane, dict):
            full_lane = {}
        visible_items = visible_lane.get("items")
        full_items = full_lane.get("items")
        if not isinstance(visible_items, list) or not isinstance(full_items, list):
            continue
        visible_refs = {
            item.get("source_ref")
            for item in visible_items
            if isinstance(item, dict) and isinstance(item.get("source_ref"), str)
        }
        retained = [
            item
            for item in full_items
            if isinstance(item, dict)
            and (item.get("item_ref") in visible_refs or item.get("source_ref") in visible_refs)
        ]
        retained_refs = {
            (
                item.get("item_ref")
                if isinstance(item.get("item_ref"), str)
                else item.get("source_ref")
            )
            for item in retained
        }
        semantic_only = [
            {
                **item,
                "authority_transport_availability": "semantic_only_no_world_claim_authority",
            }
            for item in visible_items
            if isinstance(item, dict) and item.get("source_ref") not in retained_refs
        ]
        if retained or semantic_only:
            selected_slices[name] = {
                "availability": "available",
                "items": [*retained, *semantic_only],
            }
    selected = {key: value for key, value in full.items() if key != "slices"}
    selected["slices"] = selected_slices
    pinned_time = lived_context.get("pinned_time")
    if isinstance(pinned_time, dict):
        selected["pinned_time"] = {
            **pinned_time,
            "authority_transport_availability": "semantic_only_no_world_claim_authority",
        }
    return request.model_copy(update={"model_content_json": _canonical(selected)})


def _proactive_source_kind(model_content_json: str) -> str | None:
    """Read the compiler-owned opportunity kind from its verified advisory."""

    frame = _proactive_source_frame(model_content_json)
    kind = frame.get("source_kind") if frame is not None else None
    return kind if isinstance(kind, str) else None


def _proactive_source_frame(model_content_json: str) -> dict[str, object] | None:
    """Promote one verified opportunity above the larger capsule for attention."""

    try:
        context = json.loads(model_content_json)
        items = context["slices"]["advisories"]["items"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(items, list):
        return None
    frames: list[dict[str, object]] = []
    for item in items:
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, dict) or value.get("kind") != "proactive_opportunity":
            continue
        refs = value.get("candidate_refs")
        if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], str):
            return None
        kind = refs[0].split(":", 1)[0]
        if kind in {
            "settled_world_event",
            "thread",
            "commitment",
            "spontaneous_contact",
            "ambient_presence",
            "situation_change",
        }:
            candidates = value.get("candidates")
            candidate = (
                candidates[0]
                if isinstance(candidates, list)
                and len(candidates) == 1
                and isinstance(candidates[0], dict)
                else None
            )
            guidance = candidate.get("value") if candidate is not None else None
            source_refs = value.get("source_refs")
            if not isinstance(guidance, str) or not isinstance(source_refs, list):
                return None
            frames.append(
                {
                    "source_kind": kind,
                    "candidate_ref": refs[0],
                    "guidance": guidance,
                    "source_refs": source_refs,
                }
            )
    return frames[0] if len(frames) == 1 else None


def _proactive_memory_cue(model_content_json: str) -> str:
    """Build one non-behavioral semantic cue from the present situation.

    The cue intentionally excludes recalled memories, user facts and dialogue
    so old material cannot query itself into the result. Current situation,
    Affect, appraisals, life and the verified opportunity are attention
    conditions only; the character still decides whether the association
    matters and what, if anything, to do with it.
    """

    try:
        context = json.loads(model_content_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("proactive memory cue requires Context JSON") from exc
    slices = context.get("slices") if isinstance(context, dict) else None
    if not isinstance(slices, dict):
        raise ValueError("proactive memory cue requires Context slices")
    attention: dict[str, object] = {}
    for name in (
        "current_situation",
        "affect_episodes",
        "appraisals",
        "world_life",
        "open_threads",
        "advisories",
    ):
        lane = slices.get(name)
        if not isinstance(lane, dict) or lane.get("availability") == "unavailable":
            continue
        items = lane.get("items")
        if isinstance(items, list) and items:
            attention[name] = items[:4]
    cue = _canonical(
        {
            "logical_time": context.get("logical_time"),
            "present_attention": attention,
        }
    )
    return cue[:1_024]


class ProactiveOpportunity(FrozenModel):
    source_kind: Literal[
        "settled_world_event",
        "thread",
        "commitment",
        "spontaneous_contact",
        "ambient_presence",
        "situation_change",
    ]
    source_id: str
    source_event_ref: str
    source_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_world_revision: int = Field(ge=1)
    trace_id: str
    correlation_id: str
    created_at: datetime
    consideration_id: str | None = None
    consideration_epoch: int = Field(default=0, ge=0)
    scheduled_for: datetime | None = None
    cadence_reason_codes: tuple[str, ...] = ()
    retry_ordinal: int = Field(default=0, ge=0)
    stimulus_event_refs: tuple[str, ...] = ()


def _unique_committed_stimulus_refs(
    projection: object,
    stimulus_event_refs: tuple[str, ...],
) -> tuple[object, ...]:
    """Resolve a stimulus list into stable, unique committed authorities.

    A situation window is assembled from durable projection rows, and older
    projections can contain the same event authority more than once after a
    recovery or migration.  Deliberation's evidence contract is intentionally
    strict about uniqueness; normalizing the already-declared event ids here
    is structural hygiene, not a semantic choice about what the character
    should say.
    """

    unique_ids = tuple(dict.fromkeys(stimulus_event_refs))
    # Deliberation deliberately accepts at most eight trigger authorities.
    # Keep the window's anchor (the source trigger) and the newest seven
    # authorities when a busy ten-minute window contains more rows.  The
    # complete pinned projection remains available to the role/context
    # resolver; this is only the bounded causal-evidence transport.
    wanted = unique_ids if len(unique_ids) <= 8 else (unique_ids[0], *unique_ids[-7:])
    by_event_id: dict[str, object] = {}
    for ref in getattr(projection, "committed_world_event_refs", ()):
        event_id = getattr(ref, "event_id", None)
        if isinstance(event_id, str) and event_id in wanted:
            by_event_id.setdefault(event_id, ref)
    return tuple(by_event_id[event_id] for event_id in wanted if event_id in by_event_id)


class ProactiveDeliberationTurn:
    """Compile one non-message proactive opportunity at an exact cursor."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        character_interior: CharacterInterior,
        router: ModelRouterAdapter,
        target: str,
        expression_capabilities: ExpressionDraftCapabilities,
        identity_frame: CompanionIdentityFrame | None = None,
        source_closure_reviewer=None,
        report_relative_reviewer=None,
        candidate_external_proposition_inventory_model=None,
        companion_actor_ref: str,
        budget_policy: InteractiveTurnBudgetPolicy | None = None,
    ) -> None:
        transport = _CharacterInteriorProactiveTransport(
            character_interior=character_interior,
            world_id=ledger.world_id,
            actor_ref=companion_actor_ref,
            target=target,
            expression_capabilities=expression_capabilities,
            identity_frame=identity_frame,
            source_closure_reviewer=source_closure_reviewer,
            report_relative_reviewer=report_relative_reviewer,
            candidate_external_proposition_inventory_model=(
                candidate_external_proposition_inventory_model
            ),
        )
        deliberation = compose_production_deliberation(
            lane_id="proactive",
            router=router,
            main_model=transport,
        )
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._actor = companion_actor_ref
        self._budget_policy = budget_policy
        self._recorder = ProposalAuditRecorder(ledger=ledger)

    async def audit(
        self,
        *,
        opportunity: ProactiveOpportunity,
        cursor: ProjectionCursor,
        attempt_id: str | None = None,
    ) -> ProposalAuditCommit:
        stored = await self._lookup(opportunity.source_event_ref)
        committed_ref = await self._resolve_source_ref(
            event_id=opportunity.source_event_ref,
            at_world_revision=cursor.world_revision,
        )
        if (
            stored is None
            or committed_ref is None
            or committed_ref.payload_hash != opportunity.source_event_hash
            or committed_ref.world_revision != opportunity.source_world_revision
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise _ProactiveSourceBindingError("proactive source is not exact committed authority")
        projection = await self._project_at(cursor)
        event = stored[0]
        head = None
        if opportunity.source_kind == "settled_world_event":
            head = next(
                (
                    item
                    for item in projection.world_occurrences
                    if item.occurrence_id == opportunity.source_id
                ),
                None,
            )
            valid_source = (
                event.event_type == "WorldOccurrenceSettled"
                and head is not None
                and head.status == "settled"
                and head.visibility in {"public", "shareable"}
                and self._actor in head.participant_refs
                and head.settlement_event_ref == event.event_id
                and head.settlement_payload_hash == event.payload_hash
                and head.settlement_world_revision == opportunity.source_world_revision
            )
        elif opportunity.source_kind == "thread":
            head = next(
                (item for item in projection.threads if item.thread_id == opportunity.source_id),
                None,
            )
            transition = next(
                (
                    item
                    for item in reversed(projection.thread_transitions)
                    if head is not None
                    and item.thread_id == head.thread_id
                    and item.entity_revision == head.entity_revision
                    and item.values_after == head.values
                ),
                None,
            )
            valid_source = (
                event.event_type in {"ThreadOpened", "ThreadUpdated"}
                and head is not None
                and head.values.status == "open"
                and transition is not None
                and transition.accepted_event_ref == event.event_id
            )
        elif opportunity.source_kind == "commitment":
            head = next(
                (
                    item
                    for item in projection.commitments
                    if item.commitment_id == opportunity.source_id
                ),
                None,
            )
            transition = next(
                (
                    item
                    for item in reversed(projection.commitment_transitions)
                    if head is not None
                    and item.commitment_id == head.commitment_id
                    and item.entity_revision == head.entity_revision
                    and item.values_after == head.values
                ),
                None,
            )
            valid_source = (
                event.event_type in {"PrivateCommitmentOpened", "PrivateCommitmentDue"}
                and head is not None
                and head.values.status in {"open", "due"}
                and transition is not None
                and transition.accepted_event_ref == event.event_id
            )
        elif opportunity.source_kind == "spontaneous_contact":
            message = next(
                (
                    item
                    for item in projection.message_observations
                    if item.observation_id == opportunity.source_id
                ),
                None,
            )
            valid_source = (
                event.event_type == "ObservationRecorded"
                and message is not None
                and message.world_revision == opportunity.source_world_revision
                and projection.message_observations[-1] == message
            )
        elif opportunity.source_kind == "ambient_presence":
            valid_source = event.event_type == "ClockAdvanced"
        elif opportunity.source_kind == "situation_change":
            stimulus_refs = tuple(
                next(
                    (
                        ref
                        for ref in projection.committed_world_event_refs
                        if ref.event_id == stimulus_ref
                    ),
                    None,
                )
                for stimulus_ref in opportunity.stimulus_event_refs
            )
            stimulus_events: list[WorldEvent | None] = []
            for ref in stimulus_refs:
                located = await self._lookup(ref.event_id) if ref is not None else None
                stimulus_events.append(located[0] if located is not None else None)
            valid_source = (
                event.event_type in SITUATION_STIMULUS_EVENT_TYPES
                and opportunity.stimulus_event_refs
                and opportunity.stimulus_event_refs[0] == event.event_id
                and len(set(opportunity.stimulus_event_refs))
                == len(opportunity.stimulus_event_refs)
                and all(ref is not None for ref in stimulus_refs)
                and all(item is not None for item in stimulus_events)
                and all(
                    left.world_revision < right.world_revision
                    for left, right in zip(
                        stimulus_refs,
                        stimulus_refs[1:],
                        strict=False,
                    )
                    if left is not None and right is not None
                )
                and all(
                    ref.event_type in SITUATION_STIMULUS_EVENT_TYPES
                    and ref.world_revision <= cursor.world_revision
                    and event.logical_time
                    <= ref.logical_time
                    < event.logical_time + timedelta(minutes=10)
                    for ref in stimulus_refs
                    if ref is not None
                )
                and all(
                    item is not None
                    and situation_stimulus_is_observable(
                        projection=projection,
                        event=item,
                        actor_ref=self._actor,
                    )
                    for item in stimulus_events
                )
            )
        else:
            valid_source = False
        if not valid_source:
            raise _ProactiveSourceBindingError(
                "proactive source does not bind the current domain head"
            )
        opportunity_context = (
            "Verified shareable settled occurrence: "
            + _canonical(
                {
                    "occurrence_id": head.occurrence_id,
                    "result_id": head.result_id,
                    "result_payload_ref": head.result_payload_ref,
                    "result_payload_hash": head.result_payload_hash,
                    "participant_refs": head.participant_refs,
                    "location_ref": head.location_ref,
                    "settled_at": head.settled_at.isoformat() if head.settled_at else None,
                    "visibility": head.visibility,
                    "source_event_ref": event.event_id,
                    "source_payload_hash": event.payload_hash,
                }
            )
            if opportunity.source_kind == "settled_world_event" and head is not None
            else (
                "Verified latest inbound message before the idle gap: "
                + str(event.payload().get("text") or "[content unavailable]")[:1_024]
                if opportunity.source_kind == "spontaneous_contact"
                else (
                    "A durable ambient-presence consideration is due. The Clock is timing authority only; "
                    "relationship, current life, affect, commitments, and remembered context remain available "
                    "as non-directive context."
                    if opportunity.source_kind == "ambient_presence"
                    else (
                        (
                            "A bounded set of committed situation changes is available. "
                            "It is timing and attention evidence only; derive any motive "
                            "freely from the verified relationship, affect, current "
                            "situation, life, memory, threads and commitments. Stimulus refs: "
                            + _canonical(opportunity.stimulus_event_refs)
                        )
                        if opportunity.source_kind == "situation_change"
                        else "A verified proactive opportunity exists."
                    )
                )
            )
        )
        query = query_from_projection(
            projection, actor_ref=self._actor, trigger_ref=opportunity.source_event_ref
        )
        if opportunity.source_kind == "situation_change":
            evidence_refs = _unique_committed_stimulus_refs(
                projection, opportunity.stimulus_event_refs
            )
            bounded_stimulus_event_refs = tuple(ref.event_id for ref in evidence_refs)
        else:
            evidence_refs = (committed_ref,)
            bounded_stimulus_event_refs = ()
        advisory = InnerAdvisoryProjection(
            advisory_id="advisory:proactive:" + _digest(opportunity.model_dump(mode="json")),
            kind="proactive_opportunity",
            source_refs=(
                bounded_stimulus_event_refs
                if opportunity.source_kind == "situation_change"
                else (opportunity.source_event_ref,)
            ),
            candidate_refs=(f"{opportunity.source_kind}:{opportunity.source_id}",),
            candidates=(
                InnerAdvisoryCandidate(
                    candidate_ref=f"{opportunity.source_kind}:{opportunity.source_id}",
                    value=(
                        opportunity_context[:190]
                        + " Choose freely: now, later, or silent; kind="
                        + opportunity.source_kind
                        + "."
                    ),
                    weight_bp=10_000,
                    confidence_bp=10_000,
                ),
            ),
            confidence_bp=10_000,
            expiry=(projection.logical_time or stored[0].logical_time) + timedelta(days=1),
            producer_version="proactive-opportunity-matrix.1",
        )
        # A pending shared_private invitation plan rides along as read-only
        # texture: the proactive expression is exactly where "问出口" happens.
        try:
            invitation_advisories = pending_shared_private_invitation_advisories(projection)
        except (TypeError, ValueError):
            invitation_advisories = ()
        capsule = await asyncio.to_thread(
            self._capsules.compile_for_deliberation_with_advisories,
            query,
            (advisory, *invitation_advisories),
            model_content_profile="proactive_decision",
        )
        result = await self._deliberation.deliberate(
            capsule,
            attempt_id=attempt_id
            or "attempt:proactive:"
            + _digest(
                {
                    "trigger": opportunity.source_event_ref,
                    "cursor": cursor.model_dump(mode="json"),
                }
            ),
            trigger_evidence=tuple(
                ProposalEvidenceRef(
                    ref_id=ref.event_id,
                    evidence_kind="committed_world_event",
                    source_world_revision=ref.world_revision,
                    immutable_hash="sha256:" + ref.payload_hash,
                )
                for ref in evidence_refs
            ),
            budget=(self._budget_policy.start() if self._budget_policy is not None else None),
        )
        projection_time = projection.logical_time or stored[0].logical_time
        context = ProposalAuditContext(
            world_id=self._ledger.world_id,
            trigger_ref=opportunity.source_event_ref,
            logical_time=projection_time,
            created_at=projection_time,
            actor=self._actor,
            source="world-runtime:proactive-turn",
            trace_id=opportunity.trace_id,
            causation_id=opportunity.source_event_ref,
            correlation_id=opportunity.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            expected_ledger_sequence=cursor.ledger_sequence,
        )
        try:
            if self._ledger.blocks_event_loop:
                return await asyncio.to_thread(self._recorder.record, result, context)
            return self._recorder.record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict):
            raise

    async def _lookup(self, event_id: str):
        return (
            await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
            if self._ledger.blocks_event_loop
            else self._ledger.lookup_event_commit(event_id)
        )

    async def _project_at(self, cursor: ProjectionCursor):
        return (
            await asyncio.to_thread(self._ledger.project_at, cursor)
            if self._ledger.blocks_event_loop
            else self._ledger.project_at(cursor)
        )

    async def _resolve_source_ref(self, *, event_id: str, at_world_revision: int):
        if self._ledger.blocks_event_loop:
            resolved = await asyncio.to_thread(
                self._ledger.resolve_committed_event_refs,
                (event_id,),
                at_world_revision=at_world_revision,
            )
        else:
            resolved = self._ledger.resolve_committed_event_refs(
                (event_id,), at_world_revision=at_world_revision
            )
        return resolved.get(event_id)


class ProactiveActionRunResult(FrozenModel):
    status: Literal[
        "idle",
        "opened",
        "owned_elsewhere",
        "silent",
        "grounding_rejected",
        "authorized",
        "failed_safe",
        "budget_exhausted",
        "stale",
        "completed_existing",
        "retry_wait",
    ]
    source_ref: str | None = None
    proposal_id: str | None = None
    action_id: str | None = None
    reason_code: str | None = None
    next_retry_at: datetime | None = None
    retry_ordinal: int = Field(default=0, ge=0)


class ProactiveTechnicalRetryState(FrozenModel):
    """Read-only retry authority derived from committed proactive attempts."""

    consideration_id: str
    trigger_ref: str
    source_evidence_ref: str
    retry_ordinal: int = Field(ge=1)
    consecutive_technical_failures: int = Field(ge=1)
    last_failed_at: datetime
    next_retry_at: datetime
    last_failure_code: str | None = None
    last_failure_world_revision: int = Field(ge=1)
    retry_process_state: Literal["pending", "open", "claimed"] = "pending"


class ProactiveActionRuntime:
    """Recovery-safe opportunity -> deliberation -> accepted Action worker."""

    PROCESS_KIND = "proactive_action_deliberation"
    FAILURE_BACKOFF_SECONDS = (600, 1_800, 7_200)
    # Adapter-v1 instances emitted these codes through the retired production
    # post-authorship binder. Keeping their old 10/30/120 delay after the
    # dependency is removed would leave the exact outage waiting for hours.
    # Only the exact retired v1 audit receives one immediately due attempt;
    # compatibility injection under later adapters uses ordinary backoff.
    _RETIRED_BINDER_FAILURE_CODES = frozenset(
        {
            "proactive_claim_binding_invalid",
            "backup_proactive_claim_binding_invalid",
        }
    )
    _RETIRED_BINDER_MODEL_VERSIONS = frozenset({"proactive-draft-adapter.1"})
    _TERMINAL_VALIDATION_FAILURE_CODES = frozenset(
        {
            "source_review_timeout",
            "source_review_exception",
            "recall_choice_reselection_invalid",
            "authored_expression_reselection_invalid",
            "proactive_claim_binding_invalid",
            "affect_target_reselection_invalid",
            "inventory_invalid",
            "coverage_invalid",
        }
    )
    _SEMANTIC_TERMINAL_OUTCOMES = frozenset(
        {
            "proactive:silent",
            "proactive:grounding-rejected",
        }
    )

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        turn: ProactiveDeliberationTurn,
        batch_issuer: AcceptedLedgerBatchIssuer,
        policy: ExpressionPlanBudgetPolicy,
        owner_id: str,
        lease_seconds: int = 120,
        social_initiative: SocialInitiativeCompiler | None = None,
    ) -> None:
        if not owner_id or lease_seconds <= 0 or policy.category != "proactive":
            raise ValueError("proactive runtime requires owner, lease, and proactive budget policy")
        self.ledger = ledger
        self._turn = turn
        self._recorder = ExpressionPlanAtomicRecorder(batch_issuer=batch_issuer)
        self._policy = policy
        self._owner = owner_id
        self._lease_seconds = lease_seconds
        self._social_initiative = social_initiative

    async def drain_one(self) -> ProactiveActionRunResult:
        projection = await self._project()
        opportunity = await self._next_opportunity(projection)
        if opportunity is None:
            return ProactiveActionRunResult(status="idle")
        # Social eligibility may record a replayable RandomAuthority decision.
        # Re-pin the cursor before opening the lifecycle against that new head.
        projection = await self._project()
        checked_considerations: set[str] = set()
        while True:
            consideration_id = self._consideration_id(opportunity)
            retry_ordinal, next_retry_at = self._retry_state(
                projection=projection,
                opportunity=opportunity,
                consideration_id=consideration_id,
            )
            logical_time = projection.logical_time or opportunity.created_at
            if next_retry_at is not None and logical_time < next_retry_at:
                checked_considerations.add(consideration_id)
                alternate = await self._next_opportunity(
                    projection,
                    excluded_consideration_ids=frozenset(checked_considerations),
                )
                if alternate is not None:
                    opportunity = alternate
                    continue
                return ProactiveActionRunResult(
                    status="retry_wait",
                    source_ref=opportunity.source_event_ref,
                    reason_code="proactive.technical_failure_backoff",
                    next_retry_at=next_retry_at,
                    retry_ordinal=retry_ordinal,
                )
            break
        opportunity = opportunity.model_copy(update={"retry_ordinal": retry_ordinal})
        model_attempt_id = self._model_attempt_id(
            consideration_id=consideration_id,
            retry_ordinal=retry_ordinal,
        )
        trigger_id = self._trigger_id(
            consideration_id=consideration_id,
            retry_ordinal=retry_ordinal,
        )
        process = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id), None
        )
        if process is None:
            await self._open(
                opportunity=opportunity, trigger_id=trigger_id, cursor=self._cursor(projection)
            )
            return ProactiveActionRunResult(
                status="opened", source_ref=opportunity.source_event_ref
            )
        if process.state == "terminal":
            return ProactiveActionRunResult(
                status="completed_existing", source_ref=opportunity.source_event_ref
            )
        active = await self._claim(process=process, opportunity=opportunity, projection=projection)
        if active is None:
            return ProactiveActionRunResult(
                status="owned_elsewhere", source_ref=opportunity.source_event_ref
            )
        current = await self._project()
        commit: ProposalAuditCommit | None = None
        audit = next(
            (
                item
                for item in current.proposal_audits
                if item.attempt_id == model_attempt_id
                and item.proposal_kind == "decision"
                and item.proposal_id.startswith("proposal:proactive:")
            ),
            None,
        )
        durable_failure_ref = self._durable_failure_ref(
            projection=current, attempt_id=model_attempt_id
        )
        if audit is None and durable_failure_ref is not None:
            await self._complete(
                process=active,
                opportunity=opportunity,
                outcome="deliberation-failed:" + durable_failure_ref,
            )
            return ProactiveActionRunResult(
                status="failed_safe",
                source_ref=opportunity.source_event_ref,
                reason_code="proactive.deliberation_failed",
            )
        if audit is None:
            try:
                commit = await self._turn.audit(
                    opportunity=opportunity,
                    cursor=self._cursor(current),
                    attempt_id=model_attempt_id,
                )
            except _ProactiveSourceBindingError:
                await self._complete(
                    process=active,
                    opportunity=opportunity,
                    outcome="source-binding-invalid",
                )
                return ProactiveActionRunResult(
                    status="failed_safe",
                    source_ref=opportunity.source_event_ref,
                    reason_code="proactive.source_binding_invalid",
                )
            except ConcurrencyConflict:
                return ProactiveActionRunResult(
                    status="stale", source_ref=opportunity.source_event_ref
                )
            current = await self._project()
            if commit.proposal_id is None:
                durable_failure = next(
                    (
                        item
                        for item in current.model_result_audits
                        if item.model_result_ref == commit.model_result_ref
                        and item.attempt_id == model_attempt_id
                        and item.proposal_hash is None
                    ),
                    None,
                )
                if durable_failure is None:
                    raise RuntimeError("proactive deliberation failure lacks durable model audit")
                await self._complete(
                    process=active,
                    opportunity=opportunity,
                    outcome="deliberation-failed:" + commit.model_result_ref,
                )
                return ProactiveActionRunResult(
                    status="failed_safe",
                    source_ref=opportunity.source_event_ref,
                    reason_code="proactive.deliberation_failed",
                )
            audit = next(
                (
                    item
                    for item in current.proposal_audits
                    if item.proposal_id == commit.proposal_id
                    and item.proposal_id.startswith("proposal:proactive:")
                ),
                None,
            )
        if audit is None:
            raise RuntimeError("proactive deliberation produced no durable audit")
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        if (
            not isinstance(proposal, DecisionProposal)
            or proposal.trigger_ref != opportunity.source_event_ref
        ):
            raise ValueError("proactive audit has the wrong proposal family")
        self._validate_opportunity_decision(
            opportunity=opportunity,
            proposal=proposal,
        )
        self._validate_source_acceptance(
            opportunity=opportunity,
            proposal=proposal,
        )
        existing = next(
            (
                item
                for item in current.actions
                if item.intent_ref.startswith(proposal.proposal_id + ":")
            ),
            None,
        )
        if existing is not None:
            await self._complete(
                process=active, opportunity=opportunity, outcome=f"authorized:{existing.action_id}"
            )
            return ProactiveActionRunResult(
                status="completed_existing",
                source_ref=opportunity.source_event_ref,
                proposal_id=proposal.proposal_id,
                action_id=existing.action_id,
            )
        if proposal.proactive_grounding_outcome == "rejected":
            await self._complete(
                process=active,
                opportunity=opportunity,
                outcome="grounding-rejected",
            )
            return ProactiveActionRunResult(
                status="grounding_rejected",
                source_ref=opportunity.source_event_ref,
                proposal_id=proposal.proposal_id,
                reason_code="proactive.grounding_rejected",
            )
        if proposal.timing_choice == "silent" or not proposal.action_intents:
            await self._complete(process=active, opportunity=opportunity, outcome="silent")
            return ProactiveActionRunResult(
                status="silent",
                source_ref=opportunity.source_event_ref,
                proposal_id=proposal.proposal_id,
            )
        account = next(
            (
                item
                for item in current.budget_accounts
                if item.account_id == self._policy.account_id
            ),
            None,
        )
        if account is None:
            await self._complete(
                process=active,
                opportunity=opportunity,
                outcome="budget-exhausted:account-unavailable",
            )
            return ProactiveActionRunResult(
                status="budget_exhausted",
                source_ref=opportunity.source_event_ref,
                proposal_id=proposal.proposal_id,
                reason_code="proactive.budget_account_unavailable",
            )
        cursor = self._cursor(current)
        projection_time = current.logical_time or opportunity.created_at
        try:
            material = derive_expression_plan_material(
                audit=audit,
                cursor=cursor,
                world_id=self.ledger.world_id,
                policy=self._policy,
                account=account,
                logical_time=projection_time,
                created_at=projection_time,
                trace_id=opportunity.trace_id,
                correlation_id=opportunity.correlation_id,
            )
        except ExpressionPlanAcceptanceError as exc:
            if exc.code in {
                "expression_plan_acceptance.budget_unavailable",
                "expression_plan_acceptance.budget_account_unavailable",
            }:
                await self._complete(
                    process=active, opportunity=opportunity, outcome="budget-exhausted:abandoned"
                )
                return ProactiveActionRunResult(
                    status="budget_exhausted",
                    source_ref=opportunity.source_event_ref,
                    proposal_id=proposal.proposal_id,
                    reason_code=exc.code,
                )
            raise
        handle = self._recorder.prepare_batch(
            acceptance_id="acceptance:proactive:" + _digest(proposal.proposal_id),
            material=material,
            actor=self._policy.actor,
            source="world-v2:proactive-action-runtime",
        )
        try:
            if self.ledger.blocks_event_loop:
                await asyncio.to_thread(self.ledger.commit_accepted, handle, expected_cursor=cursor)
            else:
                self.ledger.commit_accepted(handle, expected_cursor=cursor)
        except ConcurrencyConflict:
            raced = await self._project()
            existing = next(
                (
                    item
                    for item in raced.actions
                    if item.intent_ref.startswith(proposal.proposal_id + ":")
                ),
                None,
            )
            if existing is None:
                return ProactiveActionRunResult(
                    status="stale",
                    source_ref=opportunity.source_event_ref,
                    proposal_id=proposal.proposal_id,
                )
        action_id = (
            existing.action_id if existing is not None else material.beats[0].action.action_id
        )
        await self._complete(
            process=active, opportunity=opportunity, outcome=f"authorized:{action_id}"
        )
        return ProactiveActionRunResult(
            status="authorized",
            source_ref=opportunity.source_event_ref,
            proposal_id=proposal.proposal_id,
            action_id=action_id,
        )

    def _validate_event_share_acceptance(
        self,
        *,
        opportunity: ProactiveOpportunity,
        proposal: DecisionProposal,
    ) -> None:
        """Fail closed unless the whole event-share prose binds source and recipient."""

        if opportunity.source_kind != "settled_world_event":
            return
        if proposal.timing_choice == "silent" or not proposal.action_intents:
            return
        if len(proposal.proposed_changes) != 1 or not proposal.action_intents:
            raise ValueError("proactive event share requires one source-bound expression plan")
        change = proposal.proposed_changes[0]
        payload = change.payload.value()
        raw_plan_claim = payload.get("event_share_plan_claim_v2")
        if raw_plan_claim is not None:
            try:
                plan_claim = EventSharePlanClaimBindingV2.model_validate(raw_plan_claim)
            except Exception as exc:
                raise ValueError("proactive event share lacks a source-bound plan claim") from exc
            drafts = payload.get("beat_drafts")
            if not isinstance(drafts, list) or len(drafts) != len(proposal.action_intents):
                raise ValueError("proactive event share plan does not bind each beat")
            expected_hash = "sha256:" + opportunity.source_event_hash
            if (
                change.evidence_refs != (opportunity.source_event_ref,)
                or plan_claim.source_event_ref != opportunity.source_event_ref
                or plan_claim.source_payload_hash != expected_hash
                or plan_claim.source_world_revision != opportunity.source_world_revision
                or plan_claim.recipient_ref not in self._policy.allowed_targets
                or len(plan_claim.beats) != len(drafts)
            ):
                raise ValueError(
                    "proactive event share plan claim does not match source and recipient"
                )
            for claim_beat, draft, intent in zip(
                plan_claim.beats, drafts, proposal.action_intents, strict=True
            ):
                if (
                    not isinstance(draft, dict)
                    or claim_beat.beat_id != draft.get("beat_id")
                    or claim_beat.claim_text != draft.get("inline_text")
                    or claim_beat.payload_hash != intent.payload_hash
                    or plan_claim.recipient_ref != intent.target
                ):
                    raise ValueError("proactive event share plan claim does not bind each effect")
            return
        if len(proposal.action_intents) != 1:
            raise ValueError("legacy proactive event share requires one expression")
        try:
            claim = EventShareClaimBinding.model_validate(payload.get("event_share_claim"))
        except Exception as exc:
            raise ValueError("proactive event share lacks a source-bound claim") from exc
        drafts = payload.get("beat_drafts")
        draft = drafts[0] if isinstance(drafts, list) and len(drafts) == 1 else None
        text = draft.get("inline_text") if isinstance(draft, dict) else None
        intent = proposal.action_intents[0]
        expected_hash = "sha256:" + opportunity.source_event_hash
        if (
            change.evidence_refs != (opportunity.source_event_ref,)
            or claim.claim_text != text
            or claim.source_event_ref != opportunity.source_event_ref
            or claim.source_payload_hash != expected_hash
            or claim.source_world_revision != opportunity.source_world_revision
            or claim.recipient_ref != intent.target
            or claim.recipient_ref not in self._policy.allowed_targets
        ):
            raise ValueError("proactive event share claim does not match source and recipient")

    def _validate_source_acceptance(
        self,
        *,
        opportunity: ProactiveOpportunity,
        proposal: DecisionProposal,
    ) -> None:
        """Every visible proactive expression binds its source, bytes, and target."""

        if proposal.timing_choice == "silent" or not proposal.action_intents:
            return
        if len(proposal.proposed_changes) != 1 or not proposal.action_intents:
            raise ValueError("visible proactive choice requires one source-bound expression plan")
        payload = proposal.proposed_changes[0].payload.value()
        raw_plan_binding = payload.get("proactive_source_plan_binding_v2")
        if raw_plan_binding is not None:
            try:
                binding_v2 = ProactiveExpressionPlanSourceBindingV2.model_validate(raw_plan_binding)
            except Exception as exc:
                raise ValueError("proactive expression lacks an exact plan source binding") from exc
            plan_id = payload.get("plan_id")
            if (
                binding_v2.source_kind != opportunity.source_kind
                or binding_v2.source_event_ref != opportunity.source_event_ref
                or binding_v2.source_payload_hash != "sha256:" + opportunity.source_event_hash
                or binding_v2.source_world_revision != opportunity.source_world_revision
                or binding_v2.plan_id != plan_id
                or tuple(binding_v2.beat_payload_hashes)
                != tuple(intent.payload_hash for intent in proposal.action_intents)
                or binding_v2.target_ref not in self._policy.allowed_targets
                or any(intent.target != binding_v2.target_ref for intent in proposal.action_intents)
            ):
                raise ValueError(
                    "proactive expression plan binding does not match source and recipient"
                )
            return
        if len(proposal.action_intents) != 1:
            raise ValueError("legacy visible proactive choice requires one expression")
        try:
            binding = ProactiveExpressionSourceBinding.model_validate(
                payload.get("proactive_source_binding")
            )
        except Exception as exc:
            raise ValueError("proactive expression lacks an exact source binding") from exc
        intent = proposal.action_intents[0]
        if (
            binding.source_kind != opportunity.source_kind
            or binding.source_event_ref != opportunity.source_event_ref
            or binding.source_payload_hash != "sha256:" + opportunity.source_event_hash
            or binding.source_world_revision != opportunity.source_world_revision
            or binding.response_payload_hash != intent.payload_hash
            or binding.target_ref != intent.target
            or binding.target_ref not in self._policy.allowed_targets
        ):
            raise ValueError("proactive expression binding does not match source and recipient")

    @staticmethod
    def _validate_opportunity_decision(
        *, opportunity: ProactiveOpportunity, proposal: DecisionProposal
    ) -> None:
        decision = proposal.proactive_opportunity_decision
        expected_disposition = (
            "grounding_rejected"
            if proposal.proactive_grounding_outcome == "rejected"
            else {
                "now": "engage_now",
                "later": "engage_later",
                "silent": "silent_after_consideration",
            }[proposal.timing_choice]
        )
        if (
            decision is None
            or decision.source_kind != opportunity.source_kind
            or decision.source_event_ref != opportunity.source_event_ref
            or decision.source_payload_hash != "sha256:" + opportunity.source_event_hash
            or decision.source_world_revision != opportunity.source_world_revision
            or decision.disposition != expected_disposition
        ):
            raise ValueError("proactive decision does not bind the considered opportunity")

    @classmethod
    def _durable_failure_ref(cls, *, projection, attempt_id: str) -> str | None:  # type: ignore[no-untyped-def]
        for item in reversed(projection.model_result_audits):
            if item.attempt_id != attempt_id or item.proposal_hash is not None:
                continue
            if cls._is_durable_technical_failure(item):
                return item.model_result_ref
        return None

    @classmethod
    def _is_durable_technical_failure(cls, item) -> bool:  # type: ignore[no-untyped-def]
        # Nested reviewer calls are evidence for their parent validation lane,
        # not independent retry boundaries.  The parent terminal audit either
        # records exhausted provider recovery or one of Deliberation's bounded
        # validation failures.
        if item.parent_model_call_id is not None:
            return False
        try:
            audit = json.loads(item.audit_json)
        except (AttributeError, json.JSONDecodeError):
            return False
        return (
            audit.get("status") == "recovery_failed"
            or audit.get("failure_code") in cls._TERMINAL_VALIDATION_FAILURE_CODES
        )

    @staticmethod
    def _consideration_id(opportunity: ProactiveOpportunity) -> str:
        return opportunity.consideration_id or "consideration:proactive:" + _digest(
            {
                "source": opportunity.source_event_ref,
                "kind": opportunity.source_kind,
            }
        )

    @staticmethod
    def _model_attempt_id(*, consideration_id: str, retry_ordinal: int) -> str:
        return "attempt:proactive:" + _digest(
            {
                "consideration": consideration_id,
                "retry_ordinal": retry_ordinal,
            }
        )

    def _trigger_id(self, *, consideration_id: str, retry_ordinal: int) -> str:
        return self._trigger_id_for_world(
            world_id=self.ledger.world_id,
            consideration_id=consideration_id,
            retry_ordinal=retry_ordinal,
        )

    @staticmethod
    def _trigger_id_for_world(*, world_id: str, consideration_id: str, retry_ordinal: int) -> str:
        return "trigger:proactive:" + _digest(
            {
                "world": world_id,
                "consideration": consideration_id,
                "retry_ordinal": retry_ordinal,
            }
        )

    @classmethod
    def _technical_retry_state(
        cls,
        *,
        projection,
        consideration_id: str,
        fallback_failed_at: datetime | None = None,
    ) -> ProactiveTechnicalRetryState | None:  # type: ignore[no-untyped-def]
        """Validate one contiguous, stable proactive retry lineage.

        A terminal process alone is not retry authority.  Every ordinal must
        bind the deterministic trigger and model-attempt identities to the
        exact top-level durable technical-failure audit.  A newer user
        Observation supersedes the lineage, while an opened next ordinal or a
        semantic terminal outcome means there is no pending retry deadline.
        """

        trigger_ref = "proactive-consideration:" + consideration_id
        refs = {item.event_id: item for item in projection.committed_world_event_refs}
        processes_by_id = {item.trigger_id: item for item in projection.trigger_processes}
        completion_positions = {
            trigger_id: index for index, trigger_id in enumerate(projection.completed_trigger_ids)
        }
        durable_failures = {
            (item.attempt_id, item.model_result_ref): item
            for item in projection.model_result_audits
            if item.proposal_hash is None and cls._is_durable_technical_failure(item)
        }
        failures: list[tuple[datetime, int, str | None, str | None, str]] = []
        retry_ordinal = 0
        last_failure_completion_position: int | None = None
        source_evidence_ref: str | None = None
        retry_process_state: Literal["pending", "open", "claimed"] = "pending"
        while True:
            trigger_id = cls._trigger_id_for_world(
                world_id=projection.world_id,
                consideration_id=consideration_id,
                retry_ordinal=retry_ordinal,
            )
            process = processes_by_id.get(trigger_id)
            if process is None:
                break
            if (
                process.process_kind == cls.PROCESS_KIND
                and process.trigger_ref == trigger_ref
                and process.state in {"open", "claimed"}
            ):
                retry_process_state = process.state
                break
            if (
                process.process_kind != cls.PROCESS_KIND
                or process.trigger_ref != trigger_ref
                or process.state != "terminal"
                or not str(process.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
            ):
                # The next stable attempt is already in flight or settled by
                # a model decision/effect; neither state owns another timer.
                return None
            attempt_id = cls._model_attempt_id(
                consideration_id=consideration_id,
                retry_ordinal=retry_ordinal,
            )
            result_ref = str(process.runtime_outcome_ref).removeprefix(
                "proactive:deliberation-failed:"
            )
            failure = durable_failures.get((attempt_id, result_ref))
            if failure is None:
                return None
            event_ref = refs.get(failure.event_ref)
            failed_at = (
                event_ref.logical_time
                if event_ref is not None
                else (
                    process.claim_lease.acquired_at
                    if process.claim_lease is not None
                    else fallback_failed_at
                )
            )
            if failed_at is None:
                return None
            failure_revision = (
                event_ref.world_revision
                if event_ref is not None
                else failure.evaluated_world_revision
            )
            try:
                audit_value = json.loads(failure.audit_json)
            except (TypeError, json.JSONDecodeError):
                audit_value = {}
            failure_code = audit_value.get("failure_code")
            attempted_model_version = audit_value.get("attempted_model_version")
            failures.append(
                (
                    failed_at,
                    failure_revision,
                    failure_code if isinstance(failure_code, str) else None,
                    (attempted_model_version if isinstance(attempted_model_version, str) else None),
                    process.source_evidence_ref or "",
                )
            )
            completion_position = completion_positions.get(process.trigger_id)
            if completion_position is not None:
                last_failure_completion_position = completion_position
            source_evidence_ref = process.source_evidence_ref
            retry_ordinal += 1
        if not failures or not source_evidence_ref:
            return None
        # A later completed role decision proves that the proactive model lane
        # recovered. Technical failures belong to attempts, not permanently
        # to the social scheduler: carrying an older retry past a newer
        # now/later/silent/grounding decision would misreport recovery as
        # continued outage and could replay stale context hours later.
        for process in projection.trigger_processes:
            outcome = str(process.runtime_outcome_ref)
            completion_position = completion_positions.get(process.trigger_id)
            if (
                last_failure_completion_position is not None
                and completion_position is not None
                and completion_position > last_failure_completion_position
                and process.process_kind == cls.PROCESS_KIND
                and process.state == "terminal"
                and (
                    outcome in cls._SEMANTIC_TERMINAL_OUTCOMES
                    or outcome.startswith("proactive:authorized:")
                )
            ):
                return None
        (
            last_failed_at,
            last_failure_revision,
            last_failure_code,
            last_failure_model_version,
            _,
        ) = failures[-1]
        latest_message_revision = (
            projection.message_observations[-1].world_revision
            if projection.message_observations
            else 0
        )
        if latest_message_revision > last_failure_revision:
            return None
        delay = (
            0
            if cls._is_retired_binder_failure(
                failure_code=last_failure_code,
                attempted_model_version=last_failure_model_version,
            )
            else cls.FAILURE_BACKOFF_SECONDS[
                min(retry_ordinal - 1, len(cls.FAILURE_BACKOFF_SECONDS) - 1)
            ]
        )
        return ProactiveTechnicalRetryState(
            consideration_id=consideration_id,
            trigger_ref=trigger_ref,
            source_evidence_ref=source_evidence_ref,
            retry_ordinal=retry_ordinal,
            consecutive_technical_failures=retry_ordinal,
            last_failed_at=last_failed_at,
            next_retry_at=last_failed_at + timedelta(seconds=delay),
            last_failure_code=last_failure_code,
            last_failure_world_revision=last_failure_revision,
            retry_process_state=retry_process_state,
        )

    def _retry_state(
        self,
        *,
        projection,
        opportunity: ProactiveOpportunity,
        consideration_id: str,
    ) -> tuple[int, datetime | None]:  # type: ignore[no-untyped-def]
        state = self._technical_retry_state(
            projection=projection,
            consideration_id=consideration_id,
            fallback_failed_at=opportunity.created_at,
        )
        if state is None:
            return 0, None
        return state.retry_ordinal, state.next_retry_at

    @classmethod
    def _is_retired_binder_failure(
        cls,
        *,
        failure_code: str | None,
        attempted_model_version: str | None,
    ) -> bool:
        return (
            failure_code in cls._RETIRED_BINDER_FAILURE_CODES
            and attempted_model_version in cls._RETIRED_BINDER_MODEL_VERSIONS
        )

    @staticmethod
    def _audit_status(audit_json: str) -> str | None:
        try:
            value = json.loads(audit_json)
        except (TypeError, json.JSONDecodeError):
            return None
        status = value.get("status")
        return status if isinstance(status, str) else None

    async def _next_opportunity(
        self,
        projection,
        *,
        excluded_consideration_ids: frozenset[str] = frozenset(),
    ) -> ProactiveOpportunity | None:
        active_retry_sources = {
            item.source_evidence_ref for item in proactive_technical_retry_states(projection)
        }
        terminal_sources = {
            item.source_evidence_ref
            for item in projection.trigger_processes
            if item.process_kind == self.PROCESS_KIND
            and item.state == "terminal"
            and (
                not str(item.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
                or item.source_evidence_ref not in active_retry_sources
            )
        }
        failed_source_revisions = {}
        for item in projection.trigger_processes:
            if (
                item.process_kind != self.PROCESS_KIND
                or item.state != "terminal"
                or not str(item.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
                or item.source_evidence_ref is None
            ):
                continue
            failure_revision, _failed_at = technical_failure_point(
                projection=projection, process=item
            )
            if failure_revision is not None:
                failed_source_revisions[item.source_evidence_ref] = max(
                    failure_revision,
                    failed_source_revisions.get(item.source_evidence_ref, 0),
                )
        latest_message_revision = (
            projection.message_observations[-1].world_revision
            if projection.message_observations
            else 0
        )
        logical_time = projection.logical_time
        if self._social_initiative is not None:
            social_exclusions = set(excluded_consideration_ids)
            while True:
                if social_exclusions:
                    social = await self._social_initiative.next_opportunity(
                        projection,
                        excluded_consideration_ids=frozenset(social_exclusions),
                    )
                else:
                    social = await self._social_initiative.next_opportunity(projection)
                if social is None:
                    break
                opportunity = ProactiveOpportunity.model_validate(social.model_dump())
                consideration_id = self._consideration_id(opportunity)
                if consideration_id in social_exclusions:
                    break
                has_terminal_process = any(
                    item.process_kind == self.PROCESS_KIND
                    and item.trigger_ref == "proactive-consideration:" + consideration_id
                    and item.state == "terminal"
                    for item in projection.trigger_processes
                )
                if (
                    has_terminal_process
                    and self._technical_retry_state(
                        projection=projection,
                        consideration_id=consideration_id,
                        fallback_failed_at=opportunity.created_at,
                    )
                    is None
                ):
                    # The social compiler can reconstruct an old failed
                    # stimulus from immutable history after a later semantic
                    # decision has already proven the lane recovered. The
                    # retry projection is the authority for whether that
                    # consideration still owns work; skip stale reconstructions
                    # instead of returning ``completed_existing`` forever.
                    social_exclusions.add(consideration_id)
                    continue
                return opportunity
        candidates: list[tuple[datetime, str, str, str]] = []
        # ``settled_world_event`` was the pre-V3 direct production interface.
        # Keep enough reconstruction authority to finish a process that was
        # already open when V3 was deployed, but never derive a fresh legacy
        # opportunity. New settlements are considered only by
        # SocialInitiative as coalesced, jittered ``situation_change`` stimuli.
        legacy_settlement_recovery_refs = {
            item.source_evidence_ref
            for item in projection.trigger_processes
            if item.process_kind == self.PROCESS_KIND
            and item.state != "terminal"
            and item.source_evidence_ref is not None
        }
        for occurrence in projection.world_occurrences:
            if (
                occurrence.status == "settled"
                and occurrence.settlement_event_ref
                and occurrence.visibility in {"public", "shareable"}
                and occurrence.settlement_event_ref in legacy_settlement_recovery_refs
                and self._policy.actor
                in getattr(occurrence, "participant_refs", ())
            ):
                candidates.append(
                    (
                        occurrence.settled_at or logical_time,
                        "settled_world_event",
                        occurrence.occurrence_id,
                        occurrence.settlement_event_ref,
                    )
                )
        if logical_time is not None:
            for thread in projection.threads:
                values = thread.values
                if (
                    values.status == "open"
                    and values.due_window is not None
                    and values.due_window.opens_at <= logical_time < values.due_window.closes_at
                ):
                    # A shared main-turn defer already owns one Commitment and
                    # one scheduled followup Action.  Its Thread is continuity
                    # evidence, not permission for a second proactive send.
                    already_materialized = any(
                        commitment.values.status in {"open", "due"}
                        and commitment.values.subject_ref == values.subject_ref
                        and commitment.values.due_window == values.due_window
                        and commitment.values.anchor_evidence_refs == values.anchor_evidence_refs
                        and any(
                            action.action_id
                            == commitment.values.fulfillment_contract.expected_action_id
                            for action in projection.actions
                        )
                        for commitment in projection.commitments
                    )
                    if already_materialized:
                        continue
                    latest = next(
                        (
                            item
                            for item in reversed(projection.thread_transitions)
                            if item.thread_id == thread.thread_id
                            and item.entity_revision == thread.entity_revision
                            and item.values_after == thread.values
                        ),
                        None,
                    )
                    if latest is None:
                        raise ValueError("proactive thread head lacks its exact latest transition")
                    candidates.append(
                        (
                            values.due_window.opens_at,
                            "thread",
                            thread.thread_id,
                            latest.accepted_event_ref,
                        )
                    )
            for commitment in projection.commitments:
                values = commitment.values
                bound_action = next(
                    (
                        item
                        for item in projection.actions
                        if item.action_id == values.fulfillment_contract.expected_action_id
                    ),
                    None,
                )
                if (
                    values.status in {"open", "due"}
                    and values.due_window.opens_at <= logical_time < values.due_window.closes_at
                    and bound_action is None
                ):
                    latest = next(
                        (
                            item
                            for item in reversed(projection.commitment_transitions)
                            if item.commitment_id == commitment.commitment_id
                            and item.entity_revision == commitment.entity_revision
                            and item.values_after == commitment.values
                        ),
                        None,
                    )
                    if latest is None:
                        raise ValueError(
                            "proactive commitment head lacks its exact latest transition"
                        )
                    candidates.append(
                        (
                            values.due_window.opens_at,
                            "commitment",
                            commitment.commitment_id,
                            latest.accepted_event_ref,
                        )
                    )
        for _at, source_kind, source_id, event_ref in sorted(
            candidates, key=lambda item: (item[0], item[3])
        ):
            if event_ref in terminal_sources:
                continue
            located = await self._lookup(event_ref)
            if located is None:
                continue
            event, _commit = located
            committed_ref = next(
                (
                    item
                    for item in projection.committed_world_event_refs
                    if item.event_id == event_ref
                ),
                None,
            )
            if committed_ref is None or committed_ref.payload_hash != event.payload_hash:
                raise ValueError("proactive projection source lacks exact committed authority")
            if (
                event_ref in failed_source_revisions
                and latest_message_revision > failed_source_revisions[event_ref]
            ):
                # A technical retry is scoped to the context it failed in.
                # Fresh user interaction supersedes it; it must not revive an
                # old proactive draft after the conversation has moved on.
                continue
            allowed = (
                source_kind == "settled_world_event"
                and event.event_type == "WorldOccurrenceSettled"
                or source_kind == "thread"
                and event.event_type in {"ThreadOpened", "ThreadUpdated"}
                or source_kind == "commitment"
                and event.event_type in {"PrivateCommitmentOpened", "PrivateCommitmentDue"}
            )
            if not allowed:
                raise ValueError("proactive projection source has an invalid authority event")
            opportunity = ProactiveOpportunity(
                source_kind=source_kind,
                source_id=source_id,
                source_event_ref=event_ref,
                source_event_hash=event.payload_hash,
                source_world_revision=committed_ref.world_revision,
                trace_id=event.trace_id,
                correlation_id=event.correlation_id,
                created_at=event.created_at,
            )
            if self._consideration_id(opportunity) in excluded_consideration_ids:
                continue
            return opportunity
        return None

    async def _open(
        self, *, opportunity: ProactiveOpportunity, trigger_id: str, cursor: ProjectionCursor
    ) -> None:
        process = TriggerProcess(
            trigger_id=trigger_id,
            trigger_ref="proactive-consideration:" + self._consideration_id(opportunity),
            process_kind=self.PROCESS_KIND,
            source_evidence_ref=opportunity.source_event_ref,
            state="open",
        )
        payload = {"process": process.model_dump(mode="json")}
        await self._commit_event(
            event_type="TriggerProcessOpened",
            payload=payload,
            event_id="event:proactive:opened:" + _digest(payload),
            idempotency_key=domain_idempotency_key(
                event_type="TriggerProcessOpened", world_id=self.ledger.world_id, payload=payload
            ),
            opportunity=opportunity,
            cursor=cursor,
            commit_id="commit:proactive:opened:" + _digest(payload),
        )

    async def _claim(
        self, *, process: TriggerProcess, opportunity: ProactiveOpportunity, projection
    ) -> TriggerProcess | None:
        at = projection.logical_time or opportunity.created_at
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:proactive-worker:" + _digest(
            {"trigger": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": ClaimLease(
                    owner_id=self._owner,
                    attempt_id=attempt_id,
                    acquired_at=at,
                    expires_at=at + timedelta(seconds=self._lease_seconds),
                ),
                "attempt_ids": (*process.attempt_ids, attempt_id),
            }
        )
        event_type = (
            "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        )
        payload = {"process": claimed.model_dump(mode="json")}
        try:
            await self._commit_event(
                event_type=event_type,
                payload=payload,
                event_id="event:proactive:claim:" + _digest(payload),
                idempotency_key=(
                    domain_idempotency_key(
                        event_type=event_type, world_id=self.ledger.world_id, payload=payload
                    )
                    or "world-v2:proactive-claim:"
                    + _digest(
                        {
                            "world": self.ledger.world_id,
                            "event_type": event_type,
                            "payload": payload,
                        }
                    )
                ),
                opportunity=opportunity,
                cursor=self._cursor(projection),
                commit_id="commit:proactive:claim:" + _digest(payload),
            )
        except ConcurrencyConflict:
            return None
        return claimed

    async def _complete(
        self, *, process: TriggerProcess, opportunity: ProactiveOpportunity, outcome: str
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("proactive completion requires a claimed process")
        projection = await self._project()
        current = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == process.trigger_id
            ),
            None,
        )
        if current is not None and current.state == "terminal":
            return
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": (projection.logical_time or opportunity.created_at).isoformat(),
            "runtime_outcome_ref": f"proactive:{outcome}",
        }
        await self._commit_event(
            event_type="TriggerProcessCompleted",
            payload=payload,
            event_id="event:proactive:completed:" + _digest(payload),
            idempotency_key="world-v2:proactive-completed:"
            + _digest({"world": self.ledger.world_id, "payload": payload}),
            opportunity=opportunity,
            cursor=self._cursor(projection),
            commit_id="commit:proactive:completed:" + _digest(payload),
        )

    async def _commit_event(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        event_id: str,
        idempotency_key: str | None,
        opportunity: ProactiveOpportunity,
        cursor: ProjectionCursor,
        commit_id: str,
    ) -> None:
        if idempotency_key is None:
            raise ValueError("proactive lifecycle event lacks identity")
        projection_time = (await self._project()).logical_time or opportunity.created_at
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self.ledger.world_id,
            event_type=event_type,
            logical_time=projection_time,
            created_at=projection_time,
            actor=self._owner,
            source="world-v2:proactive-action-runtime",
            trace_id=opportunity.trace_id,
            causation_id=opportunity.source_event_ref,
            correlation_id=opportunity.correlation_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        kwargs = dict(events=(event,), expected_cursor=cursor, commit_id=commit_id)
        if self.ledger.blocks_event_loop:
            await asyncio.to_thread(self.ledger.commit_at_cursor, **kwargs)
        else:
            self.ledger.commit_at_cursor(**kwargs)

    async def _project(self):
        return (
            await asyncio.to_thread(self.ledger.project)
            if self.ledger.blocks_event_loop
            else self.ledger.project()
        )

    async def _lookup(self, event_id: str):
        return (
            await asyncio.to_thread(self.ledger.lookup_event_commit, event_id)
            if self.ledger.blocks_event_loop
            else self.ledger.lookup_event_commit(event_id)
        )

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


def proactive_technical_retry_states(projection) -> tuple[ProactiveTechnicalRetryState, ...]:  # type: ignore[no-untyped-def]
    """Return active durable proactive retries in committed failure order."""

    process_order = {
        item.trigger_ref: index
        for index, item in enumerate(projection.trigger_processes)
        if item.process_kind == ProactiveActionRuntime.PROCESS_KIND
        and item.state == "terminal"
        and str(item.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
    }
    consideration_ids = {
        item.trigger_ref.removeprefix("proactive-consideration:")
        for item in projection.trigger_processes
        if item.process_kind == ProactiveActionRuntime.PROCESS_KIND
        and item.trigger_ref.startswith("proactive-consideration:")
        and item.state == "terminal"
        and str(item.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
    }
    states = tuple(
        state
        for consideration_id in consideration_ids
        if (
            state := ProactiveActionRuntime._technical_retry_state(
                projection=projection,
                consideration_id=consideration_id,
            )
        )
        is not None
    )
    return tuple(
        sorted(
            states,
            key=lambda item: (
                process_order.get(item.trigger_ref, -1),
                item.trigger_ref,
            ),
        )
    )


def next_proactive_retry_due(projection) -> datetime | None:  # type: ignore[no-untyped-def]
    """Return the deadline for the newest unresolved proactive consideration.

    Historical situation failures can coexist in an immutable ledger.  The
    social compiler intentionally resumes only the newest unresolved context;
    choosing the global minimum here would let an old, already-overdue failure
    mask every newer retry forever.
    """

    states = proactive_technical_retry_states(projection)
    current = states[-1] if states else None
    return (
        current.next_retry_at
        if current is not None and current.retry_process_state == "pending"
        else None
    )


__all__ = [
    "ProactiveActionRunResult",
    "ProactiveActionRuntime",
    "ProactiveDeliberationTurn",
    "ProactiveDraft",
    "ProactiveOpportunity",
    "ProactiveTechnicalRetryState",
    "next_proactive_retry_due",
    "proactive_technical_retry_states",
]
