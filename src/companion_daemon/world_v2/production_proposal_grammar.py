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
from .relationship_acceptance_manifest import RelationshipAcceptanceManifest
from .relationship_acceptance_runtime import RelationshipAcceptanceRuntime
from .relationship_proposal_compiler import RelationshipProposalCompiler
from .appraisal_acceptance_manifest import AppraisalAcceptanceManifest
from .appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from .appraisal_proposal_compiler import AppraisalProposalCompiler
from .expression_plan_acceptance import derive_expression_plan_material
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from .expression_plan_manifest import ExpressionPlanAcceptanceManifest
from .external_capability_catalog import assert_external_capability_catalog_coverage
from .expression_action_capabilities import production_expression_action_kinds
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
    "silence_appraisal",
    "plan_disruption_appraisal",
    "affect",
    "relationship",
    "outcome",
    "interaction_bid",
    "proactive",
    "quick_reaction",
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
        if self.lane_id == "interaction_appraisal":
            self._validate_interaction_appraisal(proposal)
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

    @staticmethod
    def _validate_interaction_appraisal(proposal: DecisionProposal) -> None:
        """Close the only production composite to appraisal plus its derived affect."""

        appraisals = []
        affects = []
        for change in proposal.proposed_changes:
            if change.kind == "appraisal_transition" and change.transition == "activate":
                appraisals.append(change)
            elif change.kind == "affect_transition" and change.transition == "open":
                affects.append(change)
            else:
                raise ProductionProposalGrammarError("interaction_change_not_reachable")

        if len(appraisals) != 1:
            raise ProductionProposalGrammarError("interaction_appraisal_count_invalid")
        if len(affects) > 1:
            raise ProductionProposalGrammarError("interaction_affect_count_invalid")
        if proposal.action_intents:
            raise ProductionProposalGrammarError("action_not_reachable")

        if not affects:
            if proposal.affect_decision != "no_change":
                raise ProductionProposalGrammarError("interaction_affect_decision_invalid")
            return

        if proposal.affect_decision != "propose":
            raise ProductionProposalGrammarError("interaction_affect_decision_invalid")
        appraisal_refs = affects[0].payload.value().get("appraisal_change_refs")
        if appraisal_refs != [appraisals[0].change_id]:
            raise ProductionProposalGrammarError("interaction_affect_appraisal_binding_invalid")


_EXPRESSION = SpecializedProposalCapability(
    change_kind="expression_plan_transition",
    transition="accept",
    compiler_ref="derive_expression_plan_material.1",
    manifest_ref="expression-plan-manifest.1",
    reverse_verifier_ref="expression-plan-acceptance.1",
    allows_actions=True,
    # Do not infer reachability from the deliberation matrix or from a
    # platform adapter's low-level request type.  This closes only the action
    # kinds whose payload, acceptance, concrete transport and recovery are
    # installed in the current production composition.
    action_kinds=production_expression_action_kinds(),
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
_RELATIONSHIP_SIGNAL = SpecializedProposalCapability(
    change_kind="relationship_signal",
    transition="suggest",
    compiler_ref="relationship-proposal-compiler.1",
    manifest_ref="relationship-acceptance-manifest.1",
    reverse_verifier_ref="relationship-acceptance-runtime.1",
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
            (
                _RELATIONSHIP_SIGNAL.compiler_ref,
                _RELATIONSHIP_SIGNAL.manifest_ref,
                _RELATIONSHIP_SIGNAL.reverse_verifier_ref,
            ): (
                SpecializedAuthoritySeam(
                    compiler=RelationshipProposalCompiler,
                    manifest=RelationshipAcceptanceManifest,
                    reverse_verifier=RelationshipAcceptanceRuntime.accept_runtime_owned,
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
            capabilities=(_APPRAISAL, _AFFECT),
            allows_no_change_decision=True,
        ),
        "settled_world_appraisal": ProductionProposalGrammar(
            lane_id="settled_world_appraisal",
            capabilities=(_APPRAISAL,),
            allows_no_change_decision=True,
        ),
        # A silence appraisal shares the settled-world discipline: one typed
        # appraisal at most, never an action or a direct affect authoring.
        "silence_appraisal": ProductionProposalGrammar(
            lane_id="silence_appraisal",
            capabilities=(_APPRAISAL,),
            allows_no_change_decision=True,
        ),
        # A plan-disruption appraisal shares the same discipline: one typed
        # appraisal at most, never an action or a direct affect authoring.
        "plan_disruption_appraisal": ProductionProposalGrammar(
            lane_id="plan_disruption_appraisal",
            capabilities=(_APPRAISAL,),
            allows_no_change_decision=True,
        ),
        "affect": ProductionProposalGrammar(
            lane_id="affect", capabilities=(_AFFECT,), allows_no_change_decision=True
        ),
        "relationship": ProductionProposalGrammar(
            lane_id="relationship",
            capabilities=(_RELATIONSHIP_SIGNAL,),
            allows_no_change_decision=True,
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
        "proactive": ProductionProposalGrammar(
            lane_id="proactive",
            capabilities=(
                SpecializedProposalCapability(
                    change_kind=_EXPRESSION.change_kind,
                    transition=_EXPRESSION.transition,
                    compiler_ref=_EXPRESSION.compiler_ref,
                    manifest_ref=_EXPRESSION.manifest_ref,
                    reverse_verifier_ref=_EXPRESSION.reverse_verifier_ref,
                    allows_actions=True,
                    action_kinds=frozenset({"proactive_message", "followup"}),
                ),
            ),
            allows_no_change_decision=True,
        ),
        # The same-turn quick reaction lane may authorize exactly one platform
        # ``reaction`` on the triggering message and nothing else.  Its
        # no-change form is the ordinary outcome: the recorded act/hold draw
        # or the local semantic gate declining leaves the lane inert.
        "quick_reaction": ProductionProposalGrammar(
            lane_id="quick_reaction",
            capabilities=(
                SpecializedProposalCapability(
                    change_kind=_EXPRESSION.change_kind,
                    transition=_EXPRESSION.transition,
                    compiler_ref=_EXPRESSION.compiler_ref,
                    manifest_ref=_EXPRESSION.manifest_ref,
                    reverse_verifier_ref=_EXPRESSION.reverse_verifier_ref,
                    allows_actions=True,
                    action_kinds=frozenset({"reaction"}),
                ),
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

    assert_external_capability_catalog_coverage()
    if PRODUCTION_PROPOSAL_GRAMMARS is not _EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS:
        raise RuntimeError("production proposal grammar public view was replaced")
    expected = _EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS
    if set(expected) != {
        "chat_reply",
        "interaction_appraisal",
        "settled_world_appraisal",
        "silence_appraisal",
        "plan_disruption_appraisal",
        "affect",
        "relationship",
        "outcome",
        "interaction_bid",
        "proactive",
        "quick_reaction",
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


def production_proposal_grammar(
    lane_id: ProductionProposalLaneId,
    *,
    expression_action_kinds: frozenset[str] | None = None,
) -> ProductionProposalGrammar:
    assert_production_proposal_grammar_coverage()
    grammar = _EXPECTED_PRODUCTION_PROPOSAL_GRAMMARS[lane_id]
    if lane_id != "chat_reply" or expression_action_kinds is None:
        return grammar
    installed = production_expression_action_kinds()
    if not expression_action_kinds or not expression_action_kinds.issubset(installed):
        raise ValueError("chat expression actions exceed installed production capabilities")
    capability = SpecializedProposalCapability(
        change_kind=_EXPRESSION.change_kind,
        transition=_EXPRESSION.transition,
        compiler_ref=_EXPRESSION.compiler_ref,
        manifest_ref=_EXPRESSION.manifest_ref,
        reverse_verifier_ref=_EXPRESSION.reverse_verifier_ref,
        allows_actions=True,
        action_kinds=expression_action_kinds,
    )
    return ProductionProposalGrammar(
        lane_id="chat_reply",
        capabilities=(capability,),
        allows_no_change_decision=True,
        allows_minimal_reply=True,
    )


def compose_production_deliberation(
    *,
    lane_id: ProductionProposalLaneId,
    router: object,
    main_model: object,
    quick_recovery: object,
    expression_action_kinds: frozenset[str] | None = None,
    main_timeout_seconds: float = 6.0,
    quick_timeout_seconds: float = 2.5,
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
        main_timeout_seconds=main_timeout_seconds,
        quick_timeout_seconds=quick_timeout_seconds,
        proposal_grammar=production_proposal_grammar(
            lane_id, expression_action_kinds=expression_action_kinds
        ),
        # Chat recovery may legitimately return the typed ``silent``
        # DecisionProposal after both provider attempts fail.  The grammar
        # already allows an empty no-change decision and keeps it inert.
        # Every appraisal lane's recovery likewise fails closed with a typed
        # no-change appraisal proposal — never a visible MinimalReply — so it
        # must be validated by its lane grammar; under ``minimal_only`` that
        # honest recovery was rejected on shape alone and the whole appraisal
        # turn failed even though nothing needed to change.
        recovery_mode=(
            "proposal_grammar"
            if lane_id
            in {
                "proactive",
                "chat_reply",
                "interaction_appraisal",
                "settled_world_appraisal",
                "silence_appraisal",
                "plan_disruption_appraisal",
            }
            else "minimal_only"
        ),
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
