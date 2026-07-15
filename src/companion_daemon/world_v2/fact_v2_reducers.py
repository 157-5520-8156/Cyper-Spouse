"""FactCommittedV2 projection adapter, independent of legacy proposal authority.

The materialized.2 payload already contains a sealed before/after authority for
the first Fact commit.  This module converts it into the shared Fact projection
shape solely to reuse deterministic domain-invariant reduction; it never
queries a legacy FactProposalProjection or legacy AcceptanceDecisionRef.
"""

from __future__ import annotations

from datetime import datetime

from .fact_accepted_contracts import FactCommitMaterializedPayloadV2
from .fact_events import FactChangedPayload, fact_mutation_hash
from .schemas import FactOrigin, FactProjection, FactValues, fact_semantic_fingerprint


class FactV2ReducerError(ValueError):
    """Stable failure while converting a sealed Fact-v2 commit for projection."""


def materialized_fact_v2_as_projection_change(
    *,
    payload: FactCommitMaterializedPayloadV2,
    event_id: str,
    logical_time: datetime,
) -> FactChangedPayload:
    """Derive the shared Fact projection change from exact materialized.2 bytes.

    The caller remains responsible for manifest-v3 ordinal/authority checks.
    This function deliberately has no state or ledger dependency.
    """

    if type(payload) is not FactCommitMaterializedPayloadV2:
        raise FactV2ReducerError("Fact v2 payload must use its exact materialized contract")
    if type(event_id) is not str or not event_id:
        raise FactV2ReducerError("Fact v2 event id is invalid")
    try:
        values = FactValues.model_validate(payload.values.model_dump(mode="python"), strict=True)
        origin = FactOrigin(
            change_id=payload.change_id,
            transition_id=payload.transition_id,
            policy_refs=payload.policy_refs,
            accepted_event_ref=event_id,
        )
        after = FactProjection(
            fact_id=payload.fact_id,
            entity_revision=1,
            semantic_fingerprint=fact_semantic_fingerprint(
                subject_ref=values.subject_ref,
                predicate_code=values.predicate_code,
                cardinality=values.cardinality,
                conflict_key=values.conflict_key,
                value_hash=values.value_hash,
                assertion_binding=values.assertion_binding,
                anchor_evidence_refs=values.anchor_evidence_refs,
                policy_refs=payload.policy_refs,
            ),
            values=values,
            origin=origin,
            committed_at=logical_time,
            updated_at=logical_time,
        )
        raw: dict[str, object] = {
            "change_id": payload.change_id,
            "transition_id": payload.transition_id,
            "expected_entity_revision": payload.expected_entity_revision,
            "evidence_refs": tuple(item.model_dump(mode="python") for item in payload.evidence_refs),
            "policy_refs": payload.policy_refs,
            "acceptance_id": payload.acceptance_id,
            "proposal_id": payload.proposal_id,
            "evaluated_world_revision": payload.evaluated_world_revision,
            "operation": "commit",
            "fact_before": None,
            "fact_after": after.model_dump(mode="python"),
            "compensates_transition_id": None,
        }
        raw["accepted_change_hash"] = fact_mutation_hash(raw)
        return FactChangedPayload.model_validate(raw, strict=True)
    except Exception as exc:
        raise FactV2ReducerError("Fact v2 payload cannot form a valid Fact projection") from exc


__all__ = ["FactV2ReducerError", "materialized_fact_v2_as_projection_change"]
