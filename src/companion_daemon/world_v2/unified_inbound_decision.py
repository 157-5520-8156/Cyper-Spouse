"""Closed structural view of one CharacterInterior inbound decision.

The broad ``DecisionProposal`` envelope is not generic mutation authority.
Ordinary inbound cognition may carry at most one ExpressionPlan, one
Appraisal, the Affect exactly derived from that Appraisal, and one optional
candidate for each installed role-authored semantic authority. This helper is
shared by proposal grammar, specialized acceptance, and replay validation so
those boundaries cannot silently disagree about the unified shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from .proposal_envelope import DecisionProposal, TypedChange


class UnifiedInboundDecisionError(ValueError):
    """The proposal is valid JSON/schema but not the closed inbound shape."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class UnifiedInboundDecisionShape:
    expression: TypedChange | None
    appraisal: TypedChange | None
    affect: TypedChange | None
    relationship: TypedChange | None
    relationship_commitment: TypedChange | None
    interaction_act: TypedChange | None


def inspect_unified_inbound_decision(
    proposal: DecisionProposal,
) -> UnifiedInboundDecisionShape:
    """Return exact typed components or fail closed on every other mutation."""

    expressions: list[TypedChange] = []
    appraisals: list[TypedChange] = []
    affects: list[TypedChange] = []
    relationships: list[TypedChange] = []
    relationship_commitments: list[TypedChange] = []
    interaction_acts: list[TypedChange] = []
    for change in proposal.proposed_changes:
        if change.kind == "expression_plan_transition" and change.transition == "accept":
            expressions.append(change)
        elif change.kind == "appraisal_transition" and change.transition == "activate":
            appraisals.append(change)
        elif change.kind == "affect_transition" and change.transition in {
            "open",
            "update",
            "resolve",
            "supersede",
        }:
            affects.append(change)
        elif change.kind == "relationship_signal" and change.transition == "suggest":
            relationships.append(change)
        elif (
            change.kind == "relationship_commitment"
            and change.transition == "commit"
        ):
            relationship_commitments.append(change)
        elif change.kind == "interaction_act" and change.transition in {
            "declare",
            "revise",
        }:
            interaction_acts.append(change)
        else:
            raise UnifiedInboundDecisionError("change_not_reachable")

    if len(expressions) > 1:
        raise UnifiedInboundDecisionError("expression_count_invalid")
    if len(appraisals) > 1:
        raise UnifiedInboundDecisionError("appraisal_count_invalid")
    if len(affects) > 1:
        raise UnifiedInboundDecisionError("affect_count_invalid")
    if len(relationships) > 1:
        raise UnifiedInboundDecisionError("relationship_count_invalid")
    if len(relationship_commitments) > 1:
        raise UnifiedInboundDecisionError("relationship_commitment_count_invalid")
    if len(interaction_acts) > 1:
        raise UnifiedInboundDecisionError("interaction_act_count_invalid")

    expression = expressions[0] if expressions else None
    appraisal = appraisals[0] if appraisals else None
    affect = affects[0] if affects else None
    relationship = relationships[0] if relationships else None
    relationship_commitment = (
        relationship_commitments[0] if relationship_commitments else None
    )
    interaction_act = interaction_acts[0] if interaction_acts else None
    if affect is not None and appraisal is None:
        raise UnifiedInboundDecisionError("affect_without_appraisal")
    if affect is not None:
        appraisal_refs = affect.payload.value().get("appraisal_change_refs")
        if proposal.affect_decision != "propose":
            raise UnifiedInboundDecisionError("affect_decision_invalid")
        if appraisal_refs != [appraisal.change_id]:
            raise UnifiedInboundDecisionError("affect_appraisal_binding_invalid")
    elif proposal.affect_decision != "no_change":
        raise UnifiedInboundDecisionError("affect_decision_invalid")

    if relationship_commitment is not None and expression is None:
        raise UnifiedInboundDecisionError("relationship_commitment_without_expression")
    if interaction_act is not None:
        act_payload = interaction_act.payload.value()
        status_code = act_payload.get("status_code")
        if (
            not isinstance(status_code, str)
            or not status_code
            or len(status_code) > 128
            or status_code != status_code.strip()
        ):
            raise UnifiedInboundDecisionError("interaction_act_status_invalid")
        if interaction_act.transition == "declare":
            if (
                act_payload.get("interaction_act_ref") is not None
                or act_payload.get("object_ref") is not None
            ):
                raise UnifiedInboundDecisionError("interaction_act_declare_refs_invalid")
        elif (
            act_payload.get("interaction_act_ref") is None
            or act_payload.get("object_label") is not None
        ):
            raise UnifiedInboundDecisionError("interaction_act_revise_refs_invalid")
        if (
            act_payload.get("source_scope") == "delivered_expression"
            and expression is None
        ):
            raise UnifiedInboundDecisionError("interaction_act_without_expression")

    if expression is None:
        if proposal.action_intents:
            raise UnifiedInboundDecisionError("action_without_expression")
        if proposal.timing_choice != "silent":
            raise UnifiedInboundDecisionError("silent_timing_invalid")
    elif any(
        intent.causal_change_id != expression.change_id
        for intent in proposal.action_intents
    ):
        raise UnifiedInboundDecisionError("action_expression_binding_invalid")

    return UnifiedInboundDecisionShape(
        expression=expression,
        appraisal=appraisal,
        affect=affect,
        relationship=relationship,
        relationship_commitment=relationship_commitment,
        interaction_act=interaction_act,
    )


__all__ = [
    "UnifiedInboundDecisionError",
    "UnifiedInboundDecisionShape",
    "inspect_unified_inbound_decision",
]
