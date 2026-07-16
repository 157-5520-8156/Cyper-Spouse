"""Injection-only deliberation factory for the read-only tool request lane."""

from __future__ import annotations

from .deliberation import Deliberation
from .production_proposal_grammar import ProductionProposalGrammar, SpecializedProposalCapability


_TOOL_CAPABILITY = SpecializedProposalCapability(
    change_kind="read_only_tool_request",
    transition="request",
    compiler_ref="read-only-tool-proposal-compiler.1",
    manifest_ref="read-only-tool-acceptance.1",
    reverse_verifier_ref="read-only-tool-authorization.1",
    allows_actions=True,
    action_kinds=frozenset({"read_only_tool"}),
)


def compose_injected_read_only_tool_deliberation(*, router: object, model: object) -> Deliberation:
    """Build a grammar only when a deployment explicitly injects this lane.

    It intentionally is not added to the global default grammar catalogue:
    construction must be coupled to a real tool transport and the enforcement
    resolver by the application composition root.
    """

    return Deliberation(
        router=router,  # type: ignore[arg-type]
        main_model=model,  # type: ignore[arg-type]
        quick_recovery=model,  # type: ignore[arg-type]
        proposal_grammar=ProductionProposalGrammar(
            lane_id="read_only_tool" ,  # runtime-local lane; not a default catalogue member
            capabilities=(_TOOL_CAPABILITY,),
            allows_no_change_decision=True,
        ),
    )


__all__ = ["compose_injected_read_only_tool_deliberation"]
