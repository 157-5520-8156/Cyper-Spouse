"""Explicit reachability status for expression Action kinds.

The matrix catalog intentionally contains the *vocabulary* a companion may
reason about.  It is not a statement that every vocabulary item is executable
on every production platform.  This module is the much narrower composition
fact consumed by the production proposal grammar: a kind becomes reachable
only after its payload contract, acceptance lane, concrete transport and
receipt/recovery path are all installed together.

Keeping this distinct from the matrix prevents a tempting but incorrect rule
such as "reaction is always unavailable" from leaking into deliberation.  The
LLM can still decide that a reaction would fit; production simply cannot turn
that judgement into an external effect until the corresponding capability is
closed end-to-end.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


EXPRESSION_ACTION_CAPABILITY_VERSION = "expression-action-capabilities.1"
ExpressionActionAvailability = Literal["production", "adapter_only", "planned"]


class ExpressionActionCapability(FrozenModel):
    """One expression form and the evidence required before it is reachable."""

    action_kind: str = Field(min_length=1, max_length=128)
    content_type: str = Field(min_length=1, max_length=128)
    availability: ExpressionActionAvailability
    required_closure: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def availability_has_a_complete_or_missing_closure(self) -> "ExpressionActionCapability":
        if self.availability == "production" and set(self.required_closure) != {
            "immutable_payload",
            "acceptance",
            "transport",
            "receipt_recovery",
        }:
            raise ValueError("production expression action must close every external-effect seam")
        return self


# These are composition facts, not a behavioural policy.  In particular,
# ``adapter_only`` does *not* tell a model whether a reaction would be
# socially appropriate; it tells the grammar that the current deployment has
# no producer-to-provider path it can truthfully authorize.
EXPRESSION_ACTION_CAPABILITIES: tuple[ExpressionActionCapability, ...] = (
    ExpressionActionCapability(
        action_kind="reply",
        content_type="text/plain",
        availability="production",
        required_closure=("immutable_payload", "acceptance", "transport", "receipt_recovery"),
    ),
    ExpressionActionCapability(
        action_kind="followup",
        content_type="text/plain",
        availability="production",
        required_closure=("immutable_payload", "acceptance", "transport", "receipt_recovery"),
    ),
    ExpressionActionCapability(
        action_kind="proactive_message",
        content_type="text/plain",
        availability="production",
        required_closure=("immutable_payload", "acceptance", "transport", "receipt_recovery"),
    ),
    ExpressionActionCapability(
        action_kind="reaction",
        content_type="application/vnd.world-v2.reaction+json",
        availability="production",
        required_closure=("immutable_payload", "acceptance", "transport", "receipt_recovery"),
    ),
    ExpressionActionCapability(
        action_kind="typing",
        content_type="application/vnd.world-v2.typing+json",
        availability="production",
        required_closure=("immutable_payload", "acceptance", "transport", "receipt_recovery"),
    ),
    ExpressionActionCapability(
        action_kind="sticker",
        content_type="application/vnd.world-v2.sticker+json",
        availability="production",
        required_closure=("immutable_payload", "acceptance", "transport", "receipt_recovery"),
    ),
)


def production_expression_action_kinds() -> frozenset[str]:
    """Return the only expression kinds the current production grammar may accept."""

    return frozenset(
        item.action_kind
        for item in EXPRESSION_ACTION_CAPABILITIES
        if item.availability == "production"
    )


def expression_action_capability(action_kind: str) -> ExpressionActionCapability:
    """Read an explicit status instead of inferring it from the matrix vocabulary."""

    for item in EXPRESSION_ACTION_CAPABILITIES:
        if item.action_kind == action_kind:
            return item
    raise ValueError(f"unknown expression action capability: {action_kind}")


__all__ = [
    "EXPRESSION_ACTION_CAPABILITIES",
    "EXPRESSION_ACTION_CAPABILITY_VERSION",
    "ExpressionActionAvailability",
    "ExpressionActionCapability",
    "expression_action_capability",
    "production_expression_action_kinds",
]
