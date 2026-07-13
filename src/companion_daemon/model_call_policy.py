"""Pure per-turn policy for bounded model calls and grounding audits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from companion_daemon.conversation_cadence import FrozenTurnContext


ModelCallPurpose = Literal[
    "reply",
    "interaction_appraisal",
    "reply_audit",
    "reply_fallback_audit",
    "proactive",
    "proactive_audit",
    "afterthought",
    "afterthought_audit",
]
ModelComplexity = Literal[
    "routine",
    "high_pragmatic_ambiguity",
    "cross_turn_relation_repair",
    "complex_grounding_conflict",
]
GroundingAuditReason = Literal[
    "fact_free_candidate",
    "claims",
    "mentioned_events",
    "proposed_actions",
    "factual_language",
]
ModelCallDecisionReason = Literal[
    "hot_fact_free_reply",
    "fact_free_reply_within_budget",
    "grounded_reply_generation_within_budget",
    "grounding_audit_within_budget",
    "ambiguous_turn_appraisal_within_budget",
    "turn_model_call_budget_exhausted",
    "independent_audit_not_required",
    "provider_circuit_open_use_local_fallback",
    "provider_recovery_probe_required",
    "provider_recovery_probe",
    "monetary_budget_exhausted_use_local_fallback",
    "appraisal_not_required",
    "unsupported_model_call_purpose",
]


_TURN_MODEL_CALL_PURPOSES = frozenset(
    {
        "reply",
        "interaction_appraisal",
        "reply_audit",
        "reply_fallback_audit",
        "proactive",
        "proactive_audit",
        "afterthought",
        "afterthought_audit",
    }
)


@dataclass(frozen=True)
class CandidateGroundingSignals:
    """Provenance-bearing features of one proposed reply."""

    reply_text: str
    claims: tuple[str, ...] = ()
    mentioned_event_ids: tuple[str, ...] = ()
    proposed_action_ids: tuple[str, ...] = ()
    has_factual_language: bool = False


@dataclass(frozen=True)
class GroundingAuditDecision:
    requires_independent_audit: bool
    risk: Literal["low", "elevated"]
    reasons: tuple[GroundingAuditReason, ...]
    hard_invariants_required: bool = True


class GroundingAuditRisk:
    """Classify whether a candidate needs a second, independent grounding pass."""

    def assess(self, candidate: CandidateGroundingSignals) -> GroundingAuditDecision:
        reasons: list[GroundingAuditReason] = []
        if candidate.claims:
            reasons.append("claims")
        if candidate.mentioned_event_ids:
            reasons.append("mentioned_events")
        if candidate.proposed_action_ids:
            reasons.append("proposed_actions")
        if candidate.has_factual_language:
            reasons.append("factual_language")
        if reasons:
            return GroundingAuditDecision(True, "elevated", tuple(reasons))
        return GroundingAuditDecision(False, "low", ("fact_free_candidate",))


@dataclass(frozen=True)
class ModelCallRequest:
    purpose: ModelCallPurpose
    calls_used: int
    ambiguous: bool = False
    recovery_probe: bool = False
    remaining_budget_cny: float | None = None
    estimated_call_cost_cny: float = 0.0
    complexity: ModelComplexity = "routine"


@dataclass(frozen=True)
class ProviderCircuitState:
    status: Literal["closed", "open", "half_open"]

    @classmethod
    def closed(cls) -> ProviderCircuitState:
        return cls("closed")

    @classmethod
    def open(cls) -> ProviderCircuitState:
        return cls("open")

    @classmethod
    def half_open(cls) -> ProviderCircuitState:
        return cls("half_open")


@dataclass(frozen=True)
class ModelCallDecision:
    allowed: bool
    max_calls: int
    soft_timeout_seconds: float
    requires_independent_audit: bool
    hard_invariants_required: bool
    reason: ModelCallDecisionReason
    model_tier: Literal["flash", "strong"] = "flash"
    thinking: bool = False


@dataclass(frozen=True)
class ResolvedModelRoute:
    """The capability actually selected for one model call.

    Policy states what would be valuable.  Runtime capability determines what
    can honestly be claimed in traces and cost/latency baselines.
    """

    model_tier: Literal["flash", "strong"]
    thinking: bool
    degraded: bool = False
    degradation_reason: str | None = None


def resolve_model_route(
    decision: ModelCallDecision,
    *,
    expressive_available: bool,
    expressive_thinking_enabled: bool,
) -> ResolvedModelRoute:
    if decision.model_tier != "strong":
        return ResolvedModelRoute("flash", False)
    if not expressive_available:
        return ResolvedModelRoute(
            "flash",
            False,
            degraded=True,
            degradation_reason="expressive_model_unavailable",
        )
    return ResolvedModelRoute(
        "strong",
        bool(decision.thinking and expressive_thinking_enabled),
        degraded=bool(decision.thinking and not expressive_thinking_enabled),
        degradation_reason=(
            "expressive_thinking_unavailable"
            if decision.thinking and not expressive_thinking_enabled
            else None
        ),
    )


class TurnModelCallBudget:
    """Choose a bounded call envelope from immutable turn observations."""

    def decide(
        self,
        *,
        turn: FrozenTurnContext,
        request: ModelCallRequest,
        grounding: GroundingAuditDecision,
        circuit: ProviderCircuitState,
    ) -> ModelCallDecision:
        # Reserve roughly one second of the five-second user-visible hot-turn
        # budget for Guard, Action staging and transport dispatch.  A slower
        # provider result is less valuable than a prompt local convergence.
        timeout = {"hot": 3.5, "warm": 10.0, "cold": 15.0}.get(
            turn.cadence.heat, 15.0
        )
        max_calls = min(
            3,
            1 + int(request.ambiguous) + int(grounding.requires_independent_audit),
        )
        if request.purpose not in _TURN_MODEL_CALL_PURPOSES:
            return ModelCallDecision(
                allowed=False,
                max_calls=max_calls,
                soft_timeout_seconds=0.0,
                requires_independent_audit=grounding.requires_independent_audit,
                hard_invariants_required=True,
                reason="unsupported_model_call_purpose",
            )
        if request.purpose == "interaction_appraisal" and not request.ambiguous:
            return ModelCallDecision(
                allowed=False,
                max_calls=max_calls,
                soft_timeout_seconds=0.0,
                requires_independent_audit=grounding.requires_independent_audit,
                hard_invariants_required=True,
                reason="appraisal_not_required",
            )
        if circuit.status == "open":
            return ModelCallDecision(
                allowed=False,
                max_calls=max_calls,
                soft_timeout_seconds=0.0,
                requires_independent_audit=grounding.requires_independent_audit,
                hard_invariants_required=True,
                reason="provider_circuit_open_use_local_fallback",
            )
        if (
            request.remaining_budget_cny is not None
            and max(0.0, request.estimated_call_cost_cny)
            > max(0.0, request.remaining_budget_cny)
        ):
            return ModelCallDecision(
                allowed=False,
                max_calls=max_calls,
                soft_timeout_seconds=0.0,
                requires_independent_audit=grounding.requires_independent_audit,
                hard_invariants_required=True,
                reason="monetary_budget_exhausted_use_local_fallback",
            )
        if circuit.status == "half_open" and not request.recovery_probe:
            return ModelCallDecision(
                allowed=False,
                max_calls=max_calls,
                soft_timeout_seconds=0.0,
                requires_independent_audit=grounding.requires_independent_audit,
                hard_invariants_required=True,
                reason="provider_recovery_probe_required",
            )
        if circuit.status == "half_open":
            return ModelCallDecision(
                allowed=request.calls_used == 0,
                max_calls=1,
                soft_timeout_seconds=timeout,
                requires_independent_audit=grounding.requires_independent_audit,
                hard_invariants_required=True,
                reason=(
                    "provider_recovery_probe"
                    if request.calls_used == 0
                    else "turn_model_call_budget_exhausted"
                ),
            )
        is_audit = request.purpose.endswith("audit")
        if is_audit and not grounding.requires_independent_audit:
            allowed = False
            reason = "independent_audit_not_required"
        elif request.calls_used >= max_calls:
            allowed = False
            reason = "turn_model_call_budget_exhausted"
        elif is_audit:
            allowed = True
            reason = "grounding_audit_within_budget"
        elif request.purpose == "interaction_appraisal":
            allowed = True
            reason = "ambiguous_turn_appraisal_within_budget"
        elif grounding.requires_independent_audit:
            allowed = True
            reason = "grounded_reply_generation_within_budget"
        else:
            allowed = True
            reason = (
                "hot_fact_free_reply"
                if turn.cadence.heat == "hot"
                else "fact_free_reply_within_budget"
            )
        use_strong_reasoning = (
            request.complexity != "routine"
            and turn.cadence.heat != "hot"
            and request.purpose
            in {"reply", "interaction_appraisal", "reply_audit"}
        )
        return ModelCallDecision(
            allowed=allowed,
            max_calls=max_calls,
            soft_timeout_seconds=timeout,
            requires_independent_audit=grounding.requires_independent_audit,
            hard_invariants_required=True,
            reason=reason,
            model_tier="strong" if use_strong_reasoning else "flash",
            thinking=use_strong_reasoning,
        )
