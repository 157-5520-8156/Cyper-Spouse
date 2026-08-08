"""Closed attachment-perception grammar composed through CharacterInterior."""

from __future__ import annotations

from .character_interior.core import CharacterInterior
from .character_interior.qq_attachment_perception import (
    CharacterInteriorQQAttachmentPerceptionPort,
)
from .deliberation import Deliberation
from .perception_executor import PerceptionTransport
from .perception_input_source import PerceptionInputSource
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


def compose_character_interior_perception_deliberation(
    *,
    router: object,
    character_interior: CharacterInterior,
    input_source: PerceptionInputSource,
    dispatch_evidence: PerceptionTransport,
    budget_account_id: str,
    budget_limit: int,
    daily_limit: int,
    local_timezone: str,
) -> Deliberation:
    """Build the lane without exposing or constructing another character author."""

    interior_port = CharacterInteriorQQAttachmentPerceptionPort(
        character_interior=character_interior,
        input_source=input_source,
        dispatch_evidence=dispatch_evidence,
        budget_account_id=budget_account_id,
        budget_limit=budget_limit,
        daily_limit=daily_limit,
        local_timezone=local_timezone,
    )

    return Deliberation(
        router=router,  # type: ignore[arg-type]
        main_model=interior_port,
        quick_recovery=None,
        # CharacterInterior already owns one bounded same-author structural
        # correction. Provider failure remains technical and the durable
        # perception trigger retries later; Deliberation must not re-enter the
        # role through its historical recovery port.
        technical_recovery_enabled=False,
        main_timeout_seconds=12.0,
        proposal_grammar=ProductionProposalGrammar(
            lane_id="perception",
            capabilities=(_CAPABILITY,),
            allows_no_change_decision=True,
        ),
    )


__all__ = ["compose_character_interior_perception_deliberation"]
