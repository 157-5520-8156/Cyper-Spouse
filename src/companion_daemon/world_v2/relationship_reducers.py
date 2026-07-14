"""Pure reducers for relationship slow variables, signals, and boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from .relationship_events import (
    BoundaryChangedPayload,
    RelationshipSignalAcceptedPayload,
    RelationshipSlowVariableAdjustedPayload,
)
from .schemas import (
    BoundaryProjection,
    RelationshipAdjustmentProjection,
    RelationshipHysteresisProjection,
    RelationshipSignalProjection,
    RelationshipStateProjection,
    RelationshipVariableDeltas,
    RelationshipVariablesProjection,
)


_VARIABLE_NAMES = (
    "trust_bp",
    "closeness_bp",
    "respect_bp",
    "reliability_bp",
    "mutuality_bp",
    "repair_confidence_bp",
)
_STAGES = ("stranger", "acquaintance", "friend", "close_friend")
_POLICY = {
    "policy_version": "relationship-policy.1",
    "delta_cap_bp": 500,
    "stage_order": _STAGES,
    "enter_bp": {"acquaintance": 2_000, "friend": 4_500, "close_friend": 7_000},
    "exit_bp": {"acquaintance": 1_500, "friend": 3_800, "close_friend": 6_200},
    "required_confirmations": 2,
    "minimum_dwell_seconds": 86_400,
    "stage_step_limit": 1,
    "aggregation": "mean-six-variables-floor",
}


def relationship_policy_digest() -> str:
    encoded = json.dumps(_POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


RELATIONSHIP_POLICY_DIGEST = relationship_policy_digest()


def accept_relationship_signal(
    signals: tuple[RelationshipSignalProjection, ...],
    payload: RelationshipSignalAcceptedPayload,
    *,
    logical_time: datetime,
) -> tuple[RelationshipSignalProjection, ...]:
    _require_aware(logical_time)
    signal = payload.signal
    if signal.accepted_at != logical_time:
        raise ValueError("relationship signal must use authoritative logical time")
    if any(item.signal_id == signal.signal_id for item in signals):
        raise ValueError("relationship signal already exists")
    if any(item.semantic_fingerprint == signal.semantic_fingerprint for item in signals):
        raise ValueError("relationship signal semantic evidence already exists")
    return (*signals, signal)


def adjust_relationship_slow_variables(
    states: tuple[RelationshipStateProjection, ...],
    history: tuple[RelationshipAdjustmentProjection, ...],
    signals: tuple[RelationshipSignalProjection, ...],
    payload: RelationshipSlowVariableAdjustedPayload,
    *,
    logical_time: datetime,
) -> tuple[
    tuple[RelationshipStateProjection, ...],
    tuple[RelationshipAdjustmentProjection, ...],
]:
    _require_aware(logical_time)
    if payload.adjusted_at != logical_time:
        raise ValueError("relationship adjustment must use authoritative logical time")
    if payload.policy_version != "relationship-policy.1":
        raise ValueError("uninstalled relationship policy")
    if payload.policy_digest != RELATIONSHIP_POLICY_DIGEST:
        raise ValueError("relationship policy digest is not installed")
    if any(item.adjustment_id == payload.adjustment_id for item in history):
        raise ValueError("relationship adjustment already exists")
    resolved_signals = []
    for signal_ref in payload.signal_refs:
        matches = [item for item in signals if item.signal_id == signal_ref]
        if len(matches) != 1 or matches[0].subject_ref != payload.subject_ref:
            raise ValueError("relationship adjustment signal does not resolve")
        resolved_signals.append(matches[0])
    if payload.operation == "adjust":
        consumed_refs = {
            signal_ref
            for item in history
            if item.operation == "adjust"
            for signal_ref in item.signal_refs
        }
        if any(signal_ref in consumed_refs for signal_ref in payload.signal_refs):
            raise ValueError("relationship adjustment requires all signals to be unconsumed")
    if payload.contradiction_group_ref is not None and not any(
        item.contradiction_group_ref == payload.contradiction_group_ref
        for item in resolved_signals
    ):
        raise ValueError("relationship contradiction group has no supporting signal")
    for name in _VARIABLE_NAMES:
        proposed = getattr(payload.proposed_deltas, name)
        accepted = getattr(payload.accepted_deltas, name)
        if abs(accepted) > _POLICY["delta_cap_bp"]:
            raise ValueError("relationship delta exceeds policy cap")
        if accepted and (
            not proposed
            or (accepted > 0) != (proposed > 0)
            or abs(accepted) > abs(proposed)
        ):
            raise ValueError("accepted relationship delta does not refine proposal")
    matches = [
        (index, item)
        for index, item in enumerate(states)
        if item.relationship_id == payload.relationship_id
    ]
    if len(matches) > 1:
        raise ValueError("duplicate relationship state authority")
    if not matches and states:
        raise ValueError("world v2.1 permits one primary relationship state")
    if matches:
        index, current = matches[0]
        revision = current.entity_revision
        if current.subject_ref != payload.subject_ref:
            raise ValueError("relationship identity changed subject")
        before = current.variables
        stage = current.stage
        hysteresis = current.hysteresis
        commitment_refs = current.commitment_refs
        temperature = current.temperature
        if (
            current.policy_version != _POLICY["policy_version"]
            or current.policy_digest != RELATIONSHIP_POLICY_DIGEST
        ):
            raise ValueError("relationship state references an uninstalled policy")
        if current.last_adjusted_at is not None and logical_time < current.last_adjusted_at:
            raise ValueError("relationship adjustment precedes current state")
    else:
        index, revision, before, stage = None, 0, RelationshipVariablesProjection(), "stranger"
        hysteresis = RelationshipHysteresisProjection()
        commitment_refs = ()
        temperature = "ordinary"
    if revision != payload.expected_entity_revision or before != payload.variables_before:
        raise ValueError("stale relationship adjustment")
    if stage != payload.stage_before:
        raise ValueError("relationship stage before is stale")
    if hysteresis != payload.hysteresis_before:
        raise ValueError("relationship hysteresis before is stale")
    if hysteresis.candidate_since is not None and hysteresis.candidate_since > logical_time:
        raise ValueError("relationship hysteresis candidate starts in the future")
    if commitment_refs != payload.commitment_refs:
        raise ValueError("relationship commitment lineage is stale")
    if stage in {"ambiguous", "lover"}:
        raise ValueError("relationship stage requires an installed commitment protocol")
    calculated = _apply_deltas(before, payload.accepted_deltas)
    if calculated != payload.variables_after:
        raise ValueError("relationship variables do not match accepted deltas")
    target = None
    if payload.operation == "compensate":
        target = next(
            (item for item in history if item.adjustment_id == payload.compensates_adjustment_id),
            None,
        )
        if target is None or target.subject_ref != payload.subject_ref:
            raise ValueError("relationship compensation target does not resolve")
        if not history or history[-1] != target or target.relationship_revision != revision:
            raise ValueError("relationship compensation target must be the latest adjustment")
        if payload.signal_refs != target.signal_refs:
            raise ValueError("relationship compensation must preserve target signal lineage")
        if (
            stage != target.stage_after
            or hysteresis != target.hysteresis_after
            or calculated != target.variables_before
        ):
            raise ValueError("relationship compensation cannot restore target state")
        if any(item.compensates_adjustment_id == target.adjustment_id for item in history):
            raise ValueError("relationship adjustment is already compensated")
        for name in _VARIABLE_NAMES:
            effective_delta = getattr(target.variables_after, name) - getattr(
                target.variables_before, name
            )
            if getattr(payload.accepted_deltas, name) != -effective_delta:
                raise ValueError("relationship compensation must invert effective deltas")
    if target is not None:
        next_stage, next_hysteresis = target.stage_before, target.hysteresis_before
    else:
        next_stage, next_hysteresis = _derive_stage(stage, calculated, hysteresis, logical_time)
    if next_stage != payload.stage_after:
        raise ValueError("relationship stage does not match hysteresis policy")
    if next_hysteresis != payload.hysteresis_after:
        raise ValueError("relationship hysteresis accumulator does not match policy")
    if calculated == before and next_stage == stage and next_hysteresis == hysteresis:
        raise ValueError("relationship adjustment is a semantic no-op")
    updated = RelationshipStateProjection(
        relationship_id=payload.relationship_id,
        subject_ref=payload.subject_ref,
        entity_revision=revision + 1,
        stage=next_stage,
        variables=calculated,
        temperature=temperature,
        policy_version=payload.policy_version,
        policy_digest=payload.policy_digest,
        hysteresis=next_hysteresis,
        commitment_refs=payload.commitment_refs,
        last_adjusted_at=logical_time,
    )
    adjustment = RelationshipAdjustmentProjection(
        adjustment_id=payload.adjustment_id,
        subject_ref=payload.subject_ref,
        relationship_revision=revision + 1,
        operation=payload.operation,
        signal_refs=payload.signal_refs,
        proposed_deltas=payload.proposed_deltas,
        accepted_deltas=payload.accepted_deltas,
        variables_before=payload.variables_before,
        variables_after=payload.variables_after,
        stage_before=payload.stage_before,
        stage_after=payload.stage_after,
        hysteresis_before=payload.hysteresis_before,
        hysteresis_after=payload.hysteresis_after,
        commitment_refs=payload.commitment_refs,
        confidence_bp=payload.confidence_bp,
        persistence=payload.persistence,
        contradiction_group_ref=payload.contradiction_group_ref,
        rationale_code=payload.rationale_code,
        policy_version=payload.policy_version,
        policy_digest=payload.policy_digest,
        adjusted_at=payload.adjusted_at,
        compensates_adjustment_id=payload.compensates_adjustment_id,
    )
    updated_states = (*states, updated) if index is None else (*states[:index], updated, *states[index + 1 :])
    return updated_states, (*history, adjustment)


def change_boundary(
    boundaries: tuple[BoundaryProjection, ...],
    payload: BoundaryChangedPayload,
    *,
    logical_time: datetime,
) -> tuple[BoundaryProjection, ...]:
    _require_aware(logical_time)
    candidate = payload.boundary
    if candidate.policy_version != "boundary-policy.1":
        raise ValueError("boundary references an uninstalled policy")
    if candidate.updated_at != logical_time:
        raise ValueError("boundary transition must use authoritative logical time")
    matches = [(index, item) for index, item in enumerate(boundaries) if item.boundary_id == candidate.boundary_id]
    if payload.operation == "open":
        if matches:
            raise ValueError("boundary already exists")
        if any(
            item.status == "active"
            and item.subject_ref == candidate.subject_ref
            and item.scope_ref == candidate.scope_ref
            for item in boundaries
        ):
            raise ValueError("active boundary authority already exists for subject scope")
        if candidate.opened_at != logical_time:
            raise ValueError("boundary open must use authoritative logical time")
        return (*boundaries, candidate)
    if len(matches) != 1:
        raise ValueError("boundary transition target does not resolve")
    index, current = matches[0]
    if current.entity_revision != payload.expected_entity_revision:
        raise ValueError("stale boundary transition")
    if current.status != "active":
        raise ValueError("closed boundary cannot transition")
    if (
        candidate.entity_revision != current.entity_revision + 1
        or candidate.subject_ref != current.subject_ref
        or candidate.scope_ref != current.scope_ref
        or candidate.opened_at != current.opened_at
    ):
        raise ValueError("boundary transition changed immutable identity")
    return (*boundaries[:index], candidate, *boundaries[index + 1 :])


def _apply_deltas(
    before: RelationshipVariablesProjection,
    deltas: RelationshipVariableDeltas,
) -> RelationshipVariablesProjection:
    return RelationshipVariablesProjection(
        **{
            name: min(10_000, max(0, getattr(before, name) + getattr(deltas, name)))
            for name in _VARIABLE_NAMES
        }
    )


def _derive_stage(
    current: str,
    variables: RelationshipVariablesProjection,
    hysteresis: RelationshipHysteresisProjection,
    logical_time: datetime,
) -> tuple[str, RelationshipHysteresisProjection]:
    if current not in _STAGES:
        raise ValueError("relationship stage requires an installed commitment protocol")
    score = sum(getattr(variables, name) for name in _VARIABLE_NAMES) // len(_VARIABLE_NAMES)
    index = _STAGES.index(current)
    candidate = None
    direction = None
    if index < len(_STAGES) - 1 and score >= _POLICY["enter_bp"][_STAGES[index + 1]]:
        candidate, direction = _STAGES[index + 1], "promote"
    elif index > 0 and score < _POLICY["exit_bp"][_STAGES[index]]:
        candidate, direction = _STAGES[index - 1], "demote"
    if candidate is None:
        return current, RelationshipHysteresisProjection()
    if hysteresis.candidate_stage == candidate and hysteresis.direction == direction:
        assert hysteresis.candidate_since is not None
        next_hysteresis = hysteresis.model_copy(
            update={"confirming_adjustment_count": hysteresis.confirming_adjustment_count + 1}
        )
    else:
        next_hysteresis = RelationshipHysteresisProjection(
            candidate_stage=candidate,
            direction=direction,
            candidate_since=logical_time,
            confirming_adjustment_count=1,
        )
    assert next_hysteresis.candidate_since is not None
    if (
        next_hysteresis.confirming_adjustment_count >= _POLICY["required_confirmations"]
        and logical_time - next_hysteresis.candidate_since
        >= timedelta(seconds=_POLICY["minimum_dwell_seconds"])
    ):
        return candidate, RelationshipHysteresisProjection()
    return current, next_hysteresis


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("relationship logical time must be timezone-aware")
