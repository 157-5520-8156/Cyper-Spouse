"""Closed proposal grammar for every production ``Deliberation`` lane.

``DecisionProposal`` is deliberately a broad, inert interchange envelope.  It
must not, however, become a broad *production* mutation grammar merely because
one model adapter can serialize it.  This module is the composition-level
allow-list: every LLM-backed lane names the one specialized compiler/manifest
chain that may consume its proposal.  All other registered typed changes stay
inert and are rejected before a ``ProposalAudit`` can make them reachable.

Mechanical workflows (Fact draft, ActivityPlan, Media continuation and
reply-later) do not use ``DecisionProposal`` and are intentionally absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Literal, Mapping

from .affect_acceptance_manifest import AffectAcceptanceManifest
from .affect_acceptance_runtime import AffectAcceptanceRuntime
from .affect_proposal_compiler import AffectProposalCompiler
from .appraisal_acceptance_manifest import AppraisalAcceptanceManifest
from .appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from .appraisal_proposal_compiler import AppraisalProposalCompiler
from .expression_plan_acceptance import derive_expression_plan_material
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from .expression_plan_manifest import ExpressionPlanAcceptanceManifest
from .outcome_acceptance_manifest import OutcomeAcceptanceManifest
from .outcome_acceptance_runtime import OutcomeAcceptanceRuntime
from .outcome_proposal_compiler import OutcomeProposalCompiler
from .interaction_bid_acceptance_manifest import InteractionBidAcceptanceManifest
from .interaction_bid_acceptance_runtime import InteractionBidAcceptanceRuntime
from .interaction_bid_proposal_compiler import InteractionBidProposalCompiler
from .media_thread_acceptance_manifest import MediaDeliveryThreadAcceptanceManifest
from .media_thread_acceptance_runtime import MediaDeliveryThreadAcceptanceRuntime
from .media_thread_proposal_compiler import MediaDeliveryThreadProposalCompiler

from .proposal_envelope import (
    CHANGE_TRANSITION_REGISTRY,
    DecisionProposal,
    MinimalProposal,
    ProposalInput,
)


ProductionProposalLaneId = Literal[
    "chat_reply",
    "interaction_appraisal",
    "settled_world_appraisal",
    "affect",
    "outcome",
    "interaction_bid",
]


class ProductionProposalGrammarError(ValueError):
    """A model output is structurally valid but unreachable in production."""

    def __init__(self, code: str) -> None:
        self.code = f"production_proposal_grammar.{code}"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SpecializedProposalCapability:
    """One concrete accepted-effect lane, including its reverse-check seam."""

    change_kind: str
    transition: str
    compiler_ref: str
    manifest_ref: str
    reverse_verifier_ref: str
    allows_actions: bool = False
    action_kinds: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SpecializedAuthoritySeam:
    """Resolved implementation objects for one accepted proposal capability."""

    compiler: Callable[..., object] | type
    manifest: type
    reverse_verifier: Callable[..., object]


@dataclass(frozen=True, slots=True)
class ProductionProposalGrammar:
    """Small immutable allow-list applied before an audit is persisted."""

    lane_id: ProductionProposalLaneId
    capabilities: tuple[SpecializedProposalCapability, ...]
    allows_no_change_decision: bool
    allows_minimal_reply: bool = False

    def validate(self, proposal: ProposalInput) -> None:
        if isinstance(proposal, MinimalProposal):
            if not self.allows_minimal_reply:
                raise ProductionProposalGrammarError("minimal_proposal_not_reachable")
            # MinimalProposal's own validator already fixes this to one
            # expression reply/followup.  Keep this assertion here so a future
            # envelope expansion cannot silently escape the chat manifest.
            if any(intent.kind not in {"reply", "followup"} for intent in proposal.action_intents):
                raise ProductionProposalGrammarError("minimal_action_not_reachable")
            return
        if not isinstance(proposal, DecisionProposal):
            raise ProductionProposalGrammarError("proposal_kind_not_reachable")

        if not proposal.proposed_changes:
            if not self.allows_no_change_decision or proposal.action_intents:
                raise ProductionProposalGrammarError("no_change_not_reachable")
            return
        if len(proposal.proposed_changes) != 1:
            raise ProductionProposalGrammarError("change_count_not_reachable")

        change = proposal.proposed_changes[0]
        capability = next(
            (
                item
                for item in self.capabilities
                if item.change_kind == change.kind and item.transition == change.transition
            ),
            None,
        )
        if capability is None:
            raise ProductionProposalGrammarError("change_not_reachable")
        if capability.allows_actions:
            if not proposal.action_intents:
                raise ProductionProposalGrammarError("action_required")
            if any(
                intent.kind not in capability.action_kinds
                or intent.causal_change_id != change.change_id
                for intent in proposal.action_intents
            ):
                raise ProductionProposalGrammarError("action_not_reachable")
        elif proposal.action_intents:
            raise ProductionProposalGrammarError("action_not_reachable")


_EXPRESSION = SpecializedProposalCapability(
    change_kind="expression_plan_transition",
    transition="accept",
    compiler_ref="derive_expression_plan_material.1",
    manifest_ref="expression-plan-manifest.1",
    reverse_verifier_ref="expression-plan-acceptance.1",
    allows_actions=True,
    action_kinds=frozenset({"reply", "followup", "proactive_message"}),
)
_APPRAISAL = SpecializedProposalCapability(
    change_kind="appraisal_transition",
    transition="activate",
    compiler_ref="appraisal-proposal-compiler.1",
    manifest_ref="appraisal-acceptance-manifest.1",
    reverse_verifier_ref="appraisal-acceptance-runtime.1",
)
_AFFECT = SpecializedProposalCapability(
    change_kind="affect_transition",
    transition="open",
    compiler_ref="affect-proposal-compiler.1",
    manifest_ref="affect-acceptance-manifest.1",
    reverse_verifier_ref="affect-acceptance-runtime.1",
)
_OUTCOME = SpecializedProposalCapability(
    change_kind="outcome_settlement",
    transition="settle",
    compiler_ref="outcome-proposal-compiler.1",
    manifest_ref="outcome-acceptance-manifest.1",
    reverse_verifier_ref="outcome-acceptance-runtime.1",
)
_INTERACTION_BID = SpecializedProposalCapability(
    change_kind="interaction_bid_transition",
    transition="open",
    compiler_ref="interaction-bid-proposal-compiler.1",
    manifest_ref="interaction-bid-acceptance-manifest.1",
    reverse_verifier_ref="interaction-bid-acceptance-runtime.1",
)
_MEDIA_DELIVERY_THREAD = SpecializedProposalCapability(
    change_kind="media_delivery_thread_transition",
    transition="open",
    compiler_ref="media-delivery-thread-proposal-compiler.1",
    manifest_ref="media-delivery-thread-acceptance-manifest.1",
    reverse_verifier_ref="media-delivery-thread-acceptance-runtime.1",
)
_MEDIA_DELIVERY_THREAD_UPDATE = SpecializedProposalCapability(
    change_kind="media_delivery_thread_transition",
    transition="update",
    compiler_ref="media-delivery-thread-proposal-compiler.1",
    manifest_ref="media-delivery-thread-acceptance-manifest.1",
    reverse_verifier_ref="media-delivery-thread-acceptance-runtime.1",
)


_SPECIALIZED_AUTHORITY_SEAMS: Mapping[tuple[str, str, str], SpecializedAuthoritySeam] = (
    MappingProxyType(
        {
            (
                _EXPRESSION.compiler_ref,
                _EXPRESSION.manifest_ref,
                _EXPRESSION.reverse_verifier_ref,
            ): (
                SpecializedAuthoritySeam(
                    compiler=derive_expression_plan_material,
                    manifest=ExpressionPlanAcceptanceManifest,
                    reverse_verifier=ExpressionPlanAtomicRecorder.prepare_batch,
                )
            ),
            (_APPRAISAL.compiler_ref, _APPRAISAL.manifest_ref, _APPRAISAL.reverse_verifier_ref): (
                SpecializedAuthoritySeam(
                    compiler=AppraisalProposalCompiler,
                    manifest=AppraisalAcceptanceManifest,
                    reverse_verifier=AppraisalAcceptanceRuntime.accept_runtime_owned,
                )
            ),
            (_AFFECT.compiler_ref, _AFFECT.manifest_ref, _AFFECT.reverse_verifier_ref): (
                SpecializedAuthoritySeam(
                    compiler=AffectProposalCompiler,
                    manifest=AffectAcceptanceManifest,
                    reverse_verifier=AffectAcceptanceRuntime.accept_runtime_owned,
                )
            ),
            (_OUTCOME.compiler_ref, _OUTCOME.manifest_ref, _OUTCOME.reverse_verifier_ref): (
                SpecializedAuthoritySeam(
                    compiler=OutcomeProposalCompiler,
                    manifest=OutcomeAcceptanceManifest,
                    reverse_verifier=OutcomeAcceptanceRuntime.accept_runtime_owned,
                )
            ),
            (
                _INTERACTION_BID.compiler_ref,
                _INTERACTION_BID.manifest_ref,
                _INTERACTION_BID.reverse_verifier_ref,
            ): (
                SpecializedAuthoritySeam(
                    compiler=InteractionBidProposalCompiler,
                    manifest=InteractionBidAcceptanceManifest,
                    reverse_verifier=InteractionBidAcceptanceRuntime.accept_runtime_owned,
                )
            ),
            (
                _MEDIA_DELIVERY_THREAD.compiler_ref,
                _MEDIA_DELIVERY_THREAD.manifest_ref,
                _MEDIA_DELIVERY_THREAD.reverse_verifier_ref,
            ): (
                SpecializedAuthoritySeam(
                    compiler=MediaDeliveryThreadProposalCompiler,
                    manifest=MediaDeliveryThreadAcceptanceManifest,
                    reverse_verifier=MediaDeliveryThreadAcceptanceRuntime.accept_runtime_owned,
                )
            ),
        }
    )
)

_EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS: Mapping[
    ProductionProposalLaneId, ProductionProposalGrammar
] = MappingProxyType(
    {
        "chat_reply": ProductionProposalGrammar(
            lane_id="chat_reply",
            capabilities=(_EXPRESSION,),
            allows_no_change_decision=False,
            allows_minimal_reply=True,
        ),
        "interaction_appraisal": ProductionProposalGrammar(
            lane_id="interaction_appraisal",
            capabilities=(_APPRAISAL,),
            allows_no_change_decision=True,
        ),
        "settled_world_appraisal": ProductionProposalGrammar(
            lane_id="settled_world_appraisal",
            capabilities=(_APPRAISAL,),
            allows_no_change_decision=True,
        ),
        "affect": ProductionProposalGrammar(
            lane_id="affect", capabilities=(_AFFECT,), allows_no_change_decision=True
        ),
        "outcome": ProductionProposalGrammar(
            lane_id="outcome", capabilities=(_OUTCOME,), allows_no_change_decision=False
        ),
        "interaction_bid": ProductionProposalGrammar(
            lane_id="interaction_bid",
            # A delivered artifact can lead to exactly one bounded private
            # continuation: a bid, a durable follow-up thread, or no change.
            # Both mutations have independent compiler/manifest/recorder
            # chains; sharing this deliberation trigger never grants a
            # generic Thread authority.
            capabilities=(
                _INTERACTION_BID,
                _MEDIA_DELIVERY_THREAD,
                _MEDIA_DELIVERY_THREAD_UPDATE,
            ),
            allows_no_change_decision=True,
        ),
    }
)
# A public read-only view is useful to architecture tests and diagnostics, but
# production construction uses the private immutable catalogue below.
PRODUCTION_PROPOSAL_GRAMMARS = _EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS


def assert_production_proposal_grammar_coverage() -> None:
    """Executable gate for the closed grammar catalogue.

    The check deliberately validates descriptors in addition to kind/transition
    membership.  A new reachable change therefore needs a named compiler,
    accepted-manifest and reverse verifier rather than merely a parser entry.
    """

    if PRODUCTION_PROPOSAL_GRAMMARS is not _EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS:
        raise RuntimeError("production proposal grammar public view was replaced")
    expected = _EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS
    if set(expected) != {
        "chat_reply",
        "interaction_appraisal",
        "settled_world_appraisal",
        "affect",
        "outcome",
        "interaction_bid",
    }:
        raise RuntimeError("production proposal grammar lane coverage changed")
    for lane_id, grammar in expected.items():
        if grammar != _EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS[lane_id] or grammar.lane_id != lane_id:
            raise RuntimeError("production proposal grammar lane identity changed")
        if not grammar.capabilities:
            raise RuntimeError("production proposal grammar has no specialized capability")
        pairs: set[tuple[str, str]] = set()
        for capability in grammar.capabilities:
            pair = (capability.change_kind, capability.transition)
            if pair in pairs or capability.transition not in CHANGE_TRANSITION_REGISTRY.get(
                capability.change_kind, frozenset()
            ):
                raise RuntimeError("production proposal grammar has an invalid change capability")
            pairs.add(pair)
            seam = _SPECIALIZED_AUTHORITY_SEAMS.get(
                (
                    capability.compiler_ref,
                    capability.manifest_ref,
                    capability.reverse_verifier_ref,
                )
            )
            if (
                seam is None
                or not callable(seam.compiler)
                or not isinstance(seam.manifest, type)
                or not callable(seam.reverse_verifier)
            ):
                raise RuntimeError("production proposal grammar authority seam is not installed")
            if capability.allows_actions != bool(capability.action_kinds):
                raise RuntimeError("production proposal grammar action capability is incomplete")
        if grammar.allows_minimal_reply and lane_id != "chat_reply":
            raise RuntimeError("minimal reply is only reachable in chat")


def production_proposal_grammar(lane_id: ProductionProposalLaneId) -> ProductionProposalGrammar:
    assert_production_proposal_grammar_coverage()
    return _EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS[lane_id]


def compose_production_deliberation(
    *, lane_id: ProductionProposalLaneId, router: object, main_model: object, quick_recovery: object
):
    """Create the only Deliberation shape permitted by production composition.

    The local import intentionally keeps the generic deliberation module free
    of a production-composition dependency while ensuring call sites cannot
    forget the grammar argument.
    """

    from .deliberation import Deliberation

    return Deliberation(
        router=router,  # type: ignore[arg-type]
        main_model=main_model,  # type: ignore[arg-type]
        quick_recovery=quick_recovery,  # type: ignore[arg-type]
        proposal_grammar=production_proposal_grammar(lane_id),
    )


__all__ = [
    "PRODUCTION_PROPOSAL_GRAMMARS",
    "ProductionProposalGrammar",
    "ProductionProposalGrammarError",
    "ProductionProposalLaneId",
    "SpecializedProposalCapability",
    "assert_production_proposal_grammar_coverage",
    "compose_production_deliberation",
    "production_proposal_grammar",
]
