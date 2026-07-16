"""Authoritative production status for World v2 external capability verticals.

The situation matrix deliberately names actions a companion can *consider*.
That vocabulary must not become an accidental claim that every platform can
execute them.  This catalogue is the composition fact for the remaining
external capability verticals: it records which ones have a complete
source-bound path today and, more importantly, which ones must stay outside
production proposal grammar until their missing authority seams exist.

``adapter_only`` is intentionally not a social-policy outcome.  It means a
low-level adapter may understand a frozen request shape, but no World v2
producer-to-provider lifecycle owns that operation.  ``planned`` means even
that adapter seam is not installed.  Neither status is executable through a
production deliberation lane.
"""

from __future__ import annotations

from typing import Literal

from .expression_action_capabilities import (
    EXPRESSION_ACTION_CAPABILITIES,
    production_expression_action_kinds,
)
from .schema_core import FrozenModel


EXTERNAL_CAPABILITY_CATALOG_VERSION = "world-v2-external-capability-catalog.1"
ExternalCapabilityAvailability = Literal["production", "adapter_only", "planned", "prohibited"]
ExternalCapabilityFamily = Literal[
    "expression", "perception", "read_only_tool", "creative_media"
]


class ExternalCapability(FrozenModel):
    """One capability and the exact seams its current status relies on."""

    capability_id: str
    action_kind: str
    family: ExternalCapabilityFamily
    availability: ExternalCapabilityAvailability
    installed_closure: tuple[str, ...] = ()
    missing_closure: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Pydantic frozen models normally use validators; this small invariant
        # is kept in the catalogue verifier below so the public model remains
        # a plain, reusable value object.
        return None


# This is a status catalogue, not an action whitelist.  Production admission
# remains in specialized proposal grammars, because a generic action kind is
# never enough to authorize a side effect.
EXTERNAL_CAPABILITIES: tuple[ExternalCapability, ...] = (
    ExternalCapability(
        capability_id="expression.reply",
        action_kind="reply",
        family="expression",
        availability="production",
        installed_closure=(
            "immutable_payload",
            "specialized_acceptance",
            "concrete_transport",
            "receipt_recovery",
        ),
    ),
    ExternalCapability(
        capability_id="expression.followup",
        action_kind="followup",
        family="expression",
        availability="production",
        installed_closure=(
            "immutable_payload",
            "specialized_acceptance",
            "concrete_transport",
            "receipt_recovery",
        ),
    ),
    ExternalCapability(
        capability_id="expression.proactive_message",
        action_kind="proactive_message",
        family="expression",
        availability="production",
        installed_closure=(
            "immutable_payload",
            "specialized_acceptance",
            "concrete_transport",
            "receipt_recovery",
        ),
    ),
    ExternalCapability(
        capability_id="expression.reaction",
        action_kind="reaction",
        family="expression",
        availability="adapter_only",
        installed_closure=("immutable_payload", "receipt_binding_adapter"),
        missing_closure=("proposal_materializer", "concrete_transport", "receipt_recovery"),
    ),
    ExternalCapability(
        capability_id="expression.typing",
        action_kind="typing",
        family="expression",
        availability="adapter_only",
        installed_closure=("immutable_payload", "receipt_binding_adapter"),
        missing_closure=("proposal_materializer", "concrete_transport", "receipt_recovery"),
    ),
    ExternalCapability(
        capability_id="expression.sticker",
        action_kind="sticker",
        family="expression",
        availability="adapter_only",
        installed_closure=("immutable_payload", "receipt_binding_adapter"),
        missing_closure=("proposal_materializer", "concrete_transport", "receipt_recovery"),
    ),
    ExternalCapability(
        capability_id="perception.vision",
        action_kind="vision",
        family="perception",
        availability="planned",
        missing_closure=(
            "source_bound_request",
            "acceptance_budget",
            "provider_adapter",
            "vision_result_projection",
            "deterministic_result_trigger",
            "receipt_recovery",
        ),
    ),
    ExternalCapability(
        capability_id="perception.transcription",
        action_kind="transcription",
        family="perception",
        availability="planned",
        missing_closure=(
            "source_bound_request",
            "acceptance_budget",
            "provider_adapter",
            "transcription_result_projection",
            "deterministic_result_trigger",
            "receipt_recovery",
        ),
    ),
    ExternalCapability(
        capability_id="tool.read_only",
        action_kind="read_only_tool",
        family="read_only_tool",
        availability="adapter_only",
        installed_closure=(
            "source_bound_request",
            "acceptance_budget",
            "provider_adapter",
            "tool_result_projection",
            "deterministic_result_trigger",
            "receipt_recovery",
        ),
        missing_closure=(
            "production_request_deliberation",
            "deployment_provider_composition",
            "result_response_lane",
        ),
    ),
    ExternalCapability(
        capability_id="creative_media.user_request",
        action_kind="creative_media_request",
        family="creative_media",
        availability="planned",
        missing_closure=(
            "creative_request_projection",
            "source_bound_request",
            "acceptance_budget",
            "provider_adapter",
            "delivery_receipt_recovery",
        ),
    ),
)


def external_capability(action_kind: str) -> ExternalCapability:
    """Return an explicit World v2 status; unknown kinds are never implicit."""

    for capability in EXTERNAL_CAPABILITIES:
        if capability.action_kind == action_kind:
            return capability
    raise ValueError(f"unknown World v2 external capability {action_kind!r}")


def production_expression_capabilities() -> frozenset[str]:
    """The expression kinds a production proposal grammar may potentially admit."""

    return frozenset(
        capability.action_kind
        for capability in EXTERNAL_CAPABILITIES
        if capability.family == "expression" and capability.availability == "production"
    )


def assert_external_capability_catalog_coverage() -> None:
    """Fail closed if status claims drift away from executable expression grammar.

    This is deliberately narrow: perception, tools, and creative-media have
    no closed production grammar yet, so the only correct assertion is that
    none of them can accidentally be labelled production.  When one is
    implemented, its specialized request/acceptance/result-trigger chain must
    extend this verifier in the same change.
    """

    ids = tuple(capability.capability_id for capability in EXTERNAL_CAPABILITIES)
    kinds = tuple(capability.action_kind for capability in EXTERNAL_CAPABILITIES)
    if len(ids) != len(set(ids)) or len(kinds) != len(set(kinds)):
        raise RuntimeError("external capability catalogue contains duplicate identity")
    for capability in EXTERNAL_CAPABILITIES:
        if capability.availability == "production":
            if capability.missing_closure or not capability.installed_closure:
                raise RuntimeError("production external capability has incomplete closure")
        elif not capability.missing_closure:
            raise RuntimeError("non-production external capability lacks a fail-closed reason")
        if capability.family != "expression" and capability.availability == "production":
            raise RuntimeError("unclosed non-expression vertical was marked production")

    expression_by_kind = {
        capability.action_kind: capability for capability in EXTERNAL_CAPABILITIES
        if capability.family == "expression"
    }
    legacy_expression_by_kind = {
        capability.action_kind: capability for capability in EXPRESSION_ACTION_CAPABILITIES
    }
    if set(expression_by_kind) != set(legacy_expression_by_kind):
        raise RuntimeError("expression capability catalogues drifted")
    for action_kind, expression in legacy_expression_by_kind.items():
        if expression_by_kind[action_kind].availability != expression.availability:
            raise RuntimeError("expression capability availability drifted")
    if production_expression_capabilities() != production_expression_action_kinds():
        raise RuntimeError("production expression grammar capability drifted")


__all__ = [
    "EXTERNAL_CAPABILITIES",
    "EXTERNAL_CAPABILITY_CATALOG_VERSION",
    "ExternalCapability",
    "ExternalCapabilityAvailability",
    "ExternalCapabilityFamily",
    "assert_external_capability_catalog_coverage",
    "external_capability",
    "production_expression_capabilities",
]
