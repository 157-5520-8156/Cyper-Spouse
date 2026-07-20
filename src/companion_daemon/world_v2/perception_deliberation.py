"""Opt-in closed proposal grammar for attachment perception decisions."""

from __future__ import annotations

from .deliberation import Deliberation
from .production_proposal_grammar import ProductionProposalGrammar, SpecializedProposalCapability


_CAPABILITY = SpecializedProposalCapability(
    change_kind="perception_request",
    transition="request",
    compiler_ref="perception-proposal-compiler.2",
    manifest_ref="perception-acceptance.1",
    reverse_verifier_ref="perception-authorization.1",
    allows_actions=True,
    action_kinds=frozenset({"vision", "transcription"}),
)


def compose_injected_perception_deliberation(*, router: object, model: object) -> Deliberation:
    """Build the lane only when a deployment injects model, source and provider."""

    return Deliberation(
        router=router,  # type: ignore[arg-type]
        main_model=model,  # type: ignore[arg-type]
        quick_recovery=model,  # type: ignore[arg-type]
        # This decision is background work that never extends a visible
        # reply, so it may absorb one provider-route failover (primary
        # failure → fallback through the proxy) instead of spending its one
        # audited attempt on the tight interactive deadline.
        main_timeout_seconds=12.0,
        proposal_grammar=ProductionProposalGrammar(
            lane_id="perception",
            capabilities=(_CAPABILITY,),
            allows_no_change_decision=True,
        ),
    )


__all__ = ["compose_injected_perception_deliberation"]
