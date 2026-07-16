"""Source resolution and event construction for observed world outcomes.

The platform or a sensor may report a typed observation, but never supplies
ledger evidence.  This module binds its source references to the current
authoritative projection before a runtime can append ``OutcomeObservationRecorded``.
"""

from __future__ import annotations

import hashlib
import json

from .event_identity import domain_idempotency_key
from .plan_evidence import canonical_plan_evidence_hash
from .schemas import (
    EvidenceRef,
    LedgerProjection,
    OutcomeObservation,
    WorldEvent,
)


OUTCOME_OBSERVATION_POLICY_REFS = ("policy:outcome-observation.1",)


def build_outcome_observation_event(
    *,
    world_id: str,
    projection: LedgerProjection,
    observation: OutcomeObservation,
) -> WorldEvent:
    """Build one source-bound lifecycle event from a host observation."""

    if observation.world_id != world_id:
        raise ValueError("outcome observation belongs to another world")
    if projection.logical_time != observation.logical_time:
        raise ValueError("outcome observation must be pinned to current logical time")
    occurrence = next(
        (
            item
            for item in projection.world_occurrences
            if item.occurrence_id == observation.occurrence_id
        ),
        None,
    )
    if occurrence is None:
        raise ValueError("outcome observation references an unknown occurrence")
    if occurrence.status != "active":
        raise ValueError("outcome observation requires an active occurrence")
    evidence = _source_evidence(
        projection=projection,
        observation=observation,
        occurrence_preconditions=occurrence.precondition_refs,
    )
    payload = {
        "change_id": f"change:outcome-observation:{observation.observation_id}",
        "transition_id": f"transition:outcome-observation:{observation.observation_id}",
        "expected_entity_revision": occurrence.entity_revision,
        "evidence_refs": [item.model_dump(mode="json") for item in evidence],
        "policy_refs": OUTCOME_OBSERVATION_POLICY_REFS,
        "observation": observation.as_projection().model_dump(mode="json"),
    }
    event_type = "OutcomeObservationRecorded"
    event_id = f"event:outcome-observation:{observation.observation_id}"
    return WorldEvent.from_payload(
        schema_version=observation.schema_version,
        event_id=event_id,
        world_id=world_id,
        event_type=event_type,
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor="system:outcome-observation",
        source="world-runtime",
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=(
            domain_idempotency_key(
                event_type=event_type,
                world_id=world_id,
                payload=payload,
            )
            or f"outcome-observation:{observation.observation_id}"
        ),
        payload=payload,
    )


def _source_evidence(
    *,
    projection: LedgerProjection,
    observation: OutcomeObservation,
    occurrence_preconditions: tuple[str, ...],
) -> tuple[EvidenceRef, ...]:
    source_refs = _canonical_source_refs(observation.source_refs)
    if observation.source_kind == "clock_plan_precondition":
        required_plan_refs = {
            ref.removeprefix("plan:")
            for ref in occurrence_preconditions
            if ref.startswith("plan:")
        }
        if set(source_refs) - required_plan_refs:
            raise ValueError("outcome observation plan source is not an occurrence precondition")
        plans = {item.plan_id: item for item in projection.plans}
        result: list[EvidenceRef] = []
        for source_ref in source_refs:
            plan = plans.get(source_ref)
            if plan is None or plan.status not in {"planned", "active", "paused"}:
                raise ValueError("outcome observation plan source is not currently active")
            result.append(
                EvidenceRef(
                    ref_id=plan.plan_id,
                    evidence_type="active_plan",
                    claim_purpose="current_fact",
                    immutable_hash=canonical_plan_evidence_hash(plan),
                )
            )
        return tuple(result)
    if observation.source_kind == "committed_world_event":
        committed = {item.event_id: item for item in projection.committed_world_event_refs}
        result = []
        for source_ref in source_refs:
            authority = committed.get(source_ref)
            if authority is None:
                raise ValueError("outcome observation world source is not committed")
            result.append(
                EvidenceRef(
                    ref_id=authority.event_id,
                    evidence_type="committed_world_event",
                    claim_purpose="current_fact",
                    source_world_revision=authority.world_revision,
                    immutable_hash=authority.payload_hash,
                )
            )
        return tuple(result)
    if observation.source_kind == "operator_observation":
        authorities = {
            item.observation_id: item for item in projection.operator_observations
        }
        result = []
        for source_ref in source_refs:
            authority = authorities.get(source_ref)
            if authority is None:
                raise ValueError("outcome observation operator source is not committed")
            result.append(
                EvidenceRef(
                    ref_id=authority.observation_id,
                    evidence_type="operator_observation",
                    claim_purpose="current_fact",
                    immutable_hash=authority.observation_hash,
                )
            )
        return tuple(result)
    receipts = {
        key: item
        for item in projection.execution_receipts
        if item.is_terminal
        for key in (item.receipt_id, item.result_id, item.source_event_id)
    }
    result = []
    for source_ref in source_refs:
        receipt = receipts.get(source_ref)
        if receipt is None:
            raise ValueError("outcome observation external source is not settled")
        result.append(
            EvidenceRef(
                ref_id=receipt.receipt_id,
                evidence_type="settled_external_result",
                claim_purpose="current_fact",
                immutable_hash=_canonical_model_hash(receipt),
            )
        )
    return tuple(result)


def _canonical_source_refs(source_refs: tuple[str, ...]) -> tuple[str, ...]:
    canonical = tuple(sorted(set(source_refs)))
    if source_refs != canonical:
        raise ValueError("outcome observation source references must be sorted and unique")
    return canonical


def _canonical_model_hash(value: object, *, exclude: set[str] | None = None) -> str:
    model_dump = getattr(value, "model_dump")
    encoded = json.dumps(
        model_dump(mode="json", exclude=exclude or set()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["OUTCOME_OBSERVATION_POLICY_REFS", "build_outcome_observation_event"]
