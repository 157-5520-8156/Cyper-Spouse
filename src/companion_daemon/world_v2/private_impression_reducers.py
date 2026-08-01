"""Pure reducer for source-bound, non-public private impressions."""

from __future__ import annotations

from datetime import datetime

from .private_impression_events import PrivateImpressionAcceptedPayload
from .schemas import AppraisalMeaningRef, AppraisalProjection, PrivateImpressionProjection


def validate_private_impression_appraisals(
    appraisals: tuple[AppraisalProjection, ...],
    refs: tuple[AppraisalMeaningRef, ...],
    *,
    subject_ref: str,
) -> None:
    """Require each readable interpretation to resolve to an active appraisal."""
    for ref in refs:
        appraisal = next((item for item in appraisals if item.appraisal_id == ref.appraisal_id), None)
        if (
            appraisal is None
            or appraisal.status != "active"
            or appraisal.subject_ref != subject_ref
            or appraisal.source_cluster_ref != ref.source_cluster_ref
            or appraisal.origin.change_id != ref.accepted_change_id
            or appraisal.origin.transition_id != ref.accepted_transition_id
            or not any(item.hypothesis_id == ref.hypothesis_id for item in appraisal.hypotheses)
        ):
            raise ValueError("private impression appraisal reference does not resolve")


def accept_private_impression(
    impressions: tuple[PrivateImpressionProjection, ...],
    payload: PrivateImpressionAcceptedPayload,
    *,
    logical_time: datetime,
    appraisals: tuple[AppraisalProjection, ...],
) -> tuple[PrivateImpressionProjection, ...]:
    if logical_time.tzinfo is None or logical_time.utcoffset() is None:
        raise ValueError("private impression logical time must be timezone-aware")
    impression = payload.impression
    if impression.last_supported != logical_time:
        raise ValueError("private impression timestamps must equal authoritative logical time")
    if payload.transition_kind != "consolidate" and impression.first_seen != logical_time:
        raise ValueError("private impression timestamps must equal authoritative logical time")
    if any(item.impression_id == impression.impression_id for item in impressions):
        raise ValueError("private impression already exists")
    predecessors: list[PrivateImpressionProjection] = []
    for ref in payload.predecessor_refs:
        predecessor = next(
            (item for item in impressions if item.impression_id == ref.impression_id),
            None,
        )
        if (
            predecessor is None
            or predecessor.status != "active"
            or predecessor.subject_ref != impression.subject_ref
            or predecessor.entity_revision != ref.expected_entity_revision
        ):
            raise ValueError("private impression predecessor does not resolve as active")
        predecessors.append(predecessor)
    if payload.transition_kind == "consolidate":
        if impression.first_seen != min(item.first_seen for item in predecessors):
            raise ValueError("consolidated impression must preserve its earliest first-seen time")
    if any(
        item.origin is None
        or item.origin.accepted_event_ref not in impression.source_refs
        for item in predecessors
    ):
        raise ValueError("private impression replacement dropped predecessor event lineage")
    if any(
        item.status == "active"
        and item not in predecessors
        and item.subject_ref == impression.subject_ref
        and item.interpretation_refs == impression.interpretation_refs
        for item in impressions
    ):
        raise ValueError("active private impression duplicates the same appraisal meaning")
    validate_private_impression_appraisals(
        appraisals, payload.appraisal_refs, subject_ref=impression.subject_ref
    )
    superseded = tuple(
        item.model_copy(
            update={
                "entity_revision": item.entity_revision + 1,
                "status": "superseded",
            }
        )
        if item in predecessors
        else item
        for item in impressions
    )
    return (*superseded, impression)
