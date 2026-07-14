"""Pure deterministic reducers for the appraisal lifecycle."""

from __future__ import annotations

from datetime import datetime

from .appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalExpiredPayload,
    AppraisalProjection,
    AppraisalSupersededPayload,
)


def accept_appraisal(
    appraisals: tuple[AppraisalProjection, ...],
    payload: AppraisalAcceptedPayload,
    *,
    logical_time: datetime,
) -> tuple[AppraisalProjection, ...]:
    """Accept one sourced interpretation without deriving an affect delta."""
    _aware_logical_time(logical_time)
    if any(item.appraisal_id == payload.appraisal.appraisal_id for item in appraisals):
        raise ValueError(f"appraisal {payload.appraisal.appraisal_id!r} already exists")
    if payload.appraisal.accepted_at != logical_time:
        raise ValueError("appraisal acceptance must equal authoritative logical time")
    return (*appraisals, payload.appraisal)


def contradict_appraisal(
    appraisals: tuple[AppraisalProjection, ...],
    payload: AppraisalContradictedPayload,
    *,
    logical_time: datetime,
) -> tuple[AppraisalProjection, ...]:
    """Close an active appraisal when later sourced evidence contradicts it."""
    _aware_logical_time(logical_time)
    index, appraisal = _active_appraisal(
        appraisals,
        payload.appraisal_id,
        payload.expected_entity_revision,
    )
    _same_logical_time(payload.contradicted_at, logical_time)
    if logical_time >= appraisal.expires_at:
        raise ValueError("expired appraisal must use AppraisalExpired")
    updated = _evolve(
        appraisal,
        entity_revision=appraisal.entity_revision + 1,
        status="contradicted",
        closed_at=logical_time,
        contradiction_refs=payload.contradiction_refs,
    )
    return _replace(appraisals, index, updated)


def expire_appraisal(
    appraisals: tuple[AppraisalProjection, ...],
    payload: AppraisalExpiredPayload,
    *,
    logical_time: datetime,
) -> tuple[AppraisalProjection, ...]:
    """Expire only after recorded Logical Time reaches the appraisal deadline."""
    _aware_logical_time(logical_time)
    index, appraisal = _active_appraisal(
        appraisals,
        payload.appraisal_id,
        payload.expected_entity_revision,
    )
    if logical_time < appraisal.expires_at:
        raise ValueError("appraisal logical expiry has not reached its deadline")
    _same_logical_time(payload.expired_at, logical_time)
    updated = _evolve(
        appraisal,
        entity_revision=appraisal.entity_revision + 1,
        status="expired",
        closed_at=logical_time,
    )
    return _replace(appraisals, index, updated)


def supersede_appraisal(
    appraisals: tuple[AppraisalProjection, ...],
    payload: AppraisalSupersededPayload,
    *,
    logical_time: datetime,
) -> tuple[AppraisalProjection, ...]:
    """Atomically close an inferior interpretation and install its successor."""
    _aware_logical_time(logical_time)
    index, predecessor = _active_appraisal(
        appraisals,
        payload.appraisal_id,
        payload.expected_entity_revision,
    )
    _same_logical_time(payload.superseded_at, logical_time)
    if logical_time >= predecessor.expires_at:
        raise ValueError("expired appraisal cannot be superseded")
    successor = payload.successor
    if successor.accepted_at != logical_time:
        raise ValueError("successor acceptance must equal authoritative logical time")
    if successor.subject_ref != predecessor.subject_ref:
        raise ValueError("successor must interpret the same appraisal subject")
    if any(item.appraisal_id == successor.appraisal_id for item in appraisals):
        raise ValueError(f"appraisal {successor.appraisal_id!r} already exists")
    closed = _evolve(
        predecessor,
        entity_revision=predecessor.entity_revision + 1,
        status="superseded",
        closed_at=logical_time,
        superseded_by_appraisal_id=successor.appraisal_id,
    )
    return (*_replace(appraisals, index, closed), successor)


def _active_appraisal(
    appraisals: tuple[AppraisalProjection, ...],
    appraisal_id: str,
    expected_revision: int,
) -> tuple[int, AppraisalProjection]:
    matches = [
        (index, item) for index, item in enumerate(appraisals) if item.appraisal_id == appraisal_id
    ]
    if not matches:
        raise ValueError(f"unknown appraisal {appraisal_id!r}")
    if len(matches) != 1:
        raise ValueError(f"duplicate appraisal identity {appraisal_id!r}")
    index, appraisal = matches[0]
    if appraisal.entity_revision != expected_revision:
        raise ValueError(
            f"stale appraisal revision: expected {expected_revision}, "
            f"found {appraisal.entity_revision}"
        )
    if appraisal.status != "active":
        raise ValueError("only an active appraisal can transition")
    return index, appraisal


def _same_logical_time(recorded_at: datetime, logical_time: datetime) -> None:
    if recorded_at != logical_time:
        raise ValueError("transition time must equal authoritative logical time")


def _aware_logical_time(logical_time: datetime) -> None:
    if logical_time.tzinfo is None or logical_time.utcoffset() is None:
        raise ValueError("authoritative logical time must be timezone-aware")


def _evolve(
    appraisal: AppraisalProjection,
    **updates: object,
) -> AppraisalProjection:
    values = appraisal.model_dump()
    values.update(updates)
    return AppraisalProjection.model_validate(values)


def _replace(
    appraisals: tuple[AppraisalProjection, ...],
    index: int,
    appraisal: AppraisalProjection,
) -> tuple[AppraisalProjection, ...]:
    values = list(appraisals)
    values[index] = appraisal
    return tuple(values)
