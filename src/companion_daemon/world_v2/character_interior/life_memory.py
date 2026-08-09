"""Private wire helpers for CharacterInterior memory purposes.

The three memory runtimes own their durable workflow and call the sole public
``CharacterInterior.consider`` seam themselves.  This module deliberately
contains no port/facade object and no model fallback; it only constructs and
validates the source-bound capability wire shared by those runtimes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from ..fact_memory_draft import (
    FactMemoryDraftTechnicalFailure,
    FactMemoryRetentionDraft,
    materialize_fact_memory_draft,
)
from ..schema_core import canonicalize_json_value
from .contracts import InnerDecision, InteriorOpportunity, _InteriorCapabilityManifest
from .purpose_context import InteriorPurposeContext
from .run_result import CausalOpportunityIdentity


_FACT_MEMORY_PURPOSE = "fact_memory_retention"
_EXPERIENCE_MEMORY_PURPOSE = "experience_memory_retention"
_WITHDRAWAL_PURPOSE = "memory_withdrawal_review"

_PAYLOAD_CONTRACTS = {
    _FACT_MEMORY_PURPOSE: "character-interior-fact-memory-retention.1",
    _EXPERIENCE_MEMORY_PURPOSE: "character-interior-experience-memory-retention.1",
    _WITHDRAWAL_PURPOSE: "character-interior-memory-withdrawal-review.1",
}
_DECISION_CONTRACT = "character-interior-purpose-decision.1"


def _canonical(value: object) -> str:
    return json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _memory_opportunity(
    *,
    world_id: str,
    actor_ref: str,
    purpose: str,
    context: InteriorPurposeContext,
    capability_payload: Mapping[str, object],
) -> InteriorOpportunity:
    """Build one replay-stable, source-bound memory opportunity."""

    if purpose not in _PAYLOAD_CONTRACTS:
        raise ValueError("unknown CharacterInterior memory purpose")
    material = {
        "purpose": purpose,
        "inner_turn_ref": context.inner_turn_ref,
        "trigger_ref": context.trigger_ref,
        "cursor": context.cursor.model_dump(mode="json"),
        "logical_time": context.logical_time,
        "source_refs": context.source_refs,
        "payload": dict(capability_payload),
    }
    suffix = _digest(material)
    payload_json = _canonical(dict(capability_payload))
    manifest = _InteriorCapabilityManifest(
        capability_ref=f"capability:{purpose}:sha256:{suffix}",
        capability_kind=purpose,
        payload_json=payload_json,
        payload_hash="sha256:"
        + hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        source_refs=context.source_refs,
    )
    opportunity_identity = CausalOpportunityIdentity.from_source_refs(
        world_id=world_id,
        actor_ref=actor_ref,
        purpose=purpose,
        source_refs=context.source_refs,
        epoch=context.trigger_ref,
    )
    return InteriorOpportunity(
        opportunity_ref=opportunity_identity.opportunity_ref,
        inner_turn_ref=context.inner_turn_ref,
        world_id=world_id,
        actor_ref=actor_ref,
        trigger_ref=context.trigger_ref,
        cursor=context.cursor,
        logical_time=context.logical_time,
        purpose=purpose,
        source_refs=context.source_refs,
        capability_manifest=manifest,
        context_note=(
            "A committed, source-bound memory capability is available. The "
            "character owns the semantic choice; the system validates only "
            "the offered capability, evidence binding, and wire contract."
        ),
    )


def _decision_payload(
    *,
    result: InnerDecision,
    opportunity: InteriorOpportunity,
    purpose: str,
    technical_failure: type[RuntimeError],
) -> dict[str, object]:
    """Validate an Interior result without manufacturing a character choice."""

    if result.status == "technical_failure":
        raise technical_failure(
            result.failure_code or "character_interior_technical_failure"
        )
    if result.status == "model_silent":
        raise technical_failure("character_interior_model_silent")
    if result.status != "decided" or not isinstance(result.decision, dict):
        raise technical_failure("character_interior_invalid_decision")
    manifest = opportunity.capability_manifest
    if manifest is None:
        raise technical_failure("character_interior_capability_missing")
    decision = result.decision
    bindings = {
        "contract": decision.get("contract") == _DECISION_CONTRACT,
        "purpose": decision.get("purpose") == purpose,
        "capability_ref": decision.get("capability_ref") == manifest.capability_ref,
        "capability_hash": (
            decision.get("capability_payload_hash") == manifest.payload_hash
        ),
        "source_refs": tuple(decision.get("source_refs", ()))
        == opportunity.source_refs,
    }
    invalid = next((name for name, valid in bindings.items() if not valid), None)
    if invalid is not None:
        raise technical_failure(
            f"character_interior_decision_binding_invalid:{invalid}"
        )
    payload = decision.get("payload")
    if not isinstance(payload, dict):
        raise technical_failure("character_interior_decision_payload_invalid")
    if payload.get("contract") != _PAYLOAD_CONTRACTS[purpose]:
        raise technical_failure("character_interior_decision_contract_invalid")
    return {key: value for key, value in payload.items() if key != "contract"}


def _memory_retention_capability(
    *,
    source_kind: str,
    predicate_code: str,
    source_text: str,
) -> dict[str, object]:
    text_field = (
        "verified_experience_text"
        if source_kind == "companion_lived_experience"
        else "verified_source_text"
    )
    return {
        "source_kind": source_kind,
        "predicate_code": predicate_code,
        text_field: source_text,
        "decision_contract": {
            "no_change": {"retain": False},
            "retain": {
                "retain": True,
                "cue_kind": "one exact token from: identity, relationship, boundary, unfinished_business, repeated_pattern, future_utility, emotional_residue, world_continuity",
                "retention_rationales": [
                    "one or more distinct exact tokens from: identity_relevance, relationship_continuity, boundary_relevance, unfinished_business, repeated_pattern, future_utility, emotional_salience, world_continuity"
                ],
                "salience": {
                    "autobiographical_relevance_bp": "integer 0-10000",
                    "relationship_relevance_bp": "integer 0-10000",
                    "emotional_residue_bp": "integer 0-10000",
                    "unfinished_business_bp": "integer 0-10000",
                    "recurrence_bp": "integer 0-10000",
                    "novelty_bp": "integer 0-10000",
                    "future_utility_bp": "integer 0-10000",
                    "world_continuity_bp": "integer 0-10000",
                },
            },
        },
    }


def _materialize_memory_retention(
    *,
    result: InnerDecision,
    opportunity: InteriorOpportunity,
    purpose: str,
) -> FactMemoryRetentionDraft | None:
    payload = _decision_payload(
        result=result,
        opportunity=opportunity,
        purpose=purpose,
        technical_failure=FactMemoryDraftTechnicalFailure,
    )
    try:
        return materialize_fact_memory_draft(_canonical(payload))
    except (TypeError, ValueError) as exc:
        raise FactMemoryDraftTechnicalFailure(
            "character_interior_memory_payload_invalid"
        ) from exc


def _withdrawal_capability(
    *,
    candidate: Any,
    withdrawal: Any,
    withdrawal_payload_hash: str,
    can_revise: bool,
    technical_failure: type[RuntimeError],
) -> dict[str, object]:
    offered = ["retain", "forget"]
    if can_revise:
        offered.append("revise")
    try:
        values = candidate.values
        fact_values = withdrawal.fact_after.values
        candidate_payload = {
            "candidate_id": candidate.candidate_id,
            "entity_revision": candidate.entity_revision,
            "cue_kind": values.cue_kind,
            "retention_rationales": list(values.retention_rationales),
            "privacy_ceiling": values.privacy_ceiling,
            "salience": values.salience.model_dump(mode="json"),
            "source_count": len(values.source_bindings),
        }
        withdrawal_payload = {
            "predicate_code": fact_values.predicate_code,
            "reason_code": fact_values.withdrawal_reason_code,
            "source_hash": withdrawal_payload_hash,
        }
    except (AttributeError, TypeError, ValueError) as exc:
        raise technical_failure("memory_withdrawal_context_invalid") from exc
    return {
        "candidate": candidate_payload,
        "withdrawal": withdrawal_payload,
        "offered_tokens": offered,
        "selection_contract": "select exactly one offered disposition token",
    }


def _materialize_withdrawal_decision(
    *,
    result: InnerDecision,
    opportunity: InteriorOpportunity,
    technical_failure: type[RuntimeError],
) -> str:
    payload = _decision_payload(
        result=result,
        opportunity=opportunity,
        purpose=_WITHDRAWAL_PURPOSE,
        technical_failure=technical_failure,
    )
    offered = opportunity.capability_manifest.payload.get("offered_tokens", ())
    token = payload.get("selected_token")
    if not isinstance(offered, list) or token not in offered:
        raise technical_failure(
            "character_interior_withdrawal_payload_invalid"
        )
    return str(token)


def _decision_model_id(result: InnerDecision) -> str:
    if result.author_lineage is None:
        raise FactMemoryDraftTechnicalFailure(
            "character_interior_author_lineage_missing"
        )
    return result.author_lineage.model_id


__all__: list[str] = []
