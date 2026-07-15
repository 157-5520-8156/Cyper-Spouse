"""Deterministic clock continuation for committed lived-world occurrences.

Clock continuation is deliberately narrow: it can only activate an occurrence
whose declared preconditions resolve against the current ledger projection, or
expire one whose committed window has closed.  It never selects an outcome or
creates narrative content; that remains an evidence-backed deliberation and
acceptance concern.
"""

from __future__ import annotations

import hashlib
import json

from .event_identity import domain_idempotency_key
from .schemas import (
    ClockObservation,
    ClockTransitionProjection,
    EvidenceRef,
    LedgerProjection,
    PlanStateProjection,
    WorldEvent,
    WorldOccurrenceProjection,
)


OCCURRENCE_CLOCK_POLICY_REFS = ("policy:world-occurrence-clock.1",)


def build_occurrence_clock_events(
    *,
    world_id: str,
    projection: LedgerProjection,
    clock: ClockObservation,
    clock_transition: ClockTransitionProjection,
) -> list[WorldEvent]:
    """Build canonical lifecycle events for one authoritative clock tick.

    The returned events are intended to follow the clock event in its same
    atomic ledger batch, so their clock evidence resolves against the reducer
    state created by that event.  A non-plan precondition is intentionally not
    guessed: until another bounded authority can prove it, the occurrence stays
    committed and will eventually expire.
    """

    if clock.world_id != world_id:
        raise ValueError("occurrence continuation clock belongs to another world")
    committed_occurrences = tuple(
        item for item in projection.world_occurrences if item.status == "committed"
    )
    if not committed_occurrences:
        return []
    if projection.logical_time != clock.logical_time_from:
        raise ValueError("occurrence continuation clock does not extend the projection")
    if (
        clock.logical_time_from != clock_transition.logical_time_from
        or clock.logical_time_to != clock_transition.logical_time_to
    ):
        raise ValueError("occurrence continuation clock authority does not match its observation")

    events: list[WorldEvent] = []
    for occurrence in sorted(committed_occurrences, key=lambda item: item.occurrence_id):
        if occurrence.time_window.closes_at <= clock.logical_time_to:
            events.append(
                _expiry_event(
                    world_id=world_id,
                    occurrence=occurrence,
                    clock=clock,
                    clock_transition=clock_transition,
                )
            )
            continue
        if occurrence.time_window.opens_at > clock.logical_time_to:
            continue
        satisfied = _satisfied_precondition_evidence(
            occurrence=occurrence,
            plans=projection.plans,
        )
        if satisfied is None:
            continue
        events.append(
            _activation_event(
                world_id=world_id,
                occurrence=occurrence,
                clock=clock,
                clock_transition=clock_transition,
                plan_evidence=satisfied,
            )
        )
    return events


def _activation_event(
    *,
    world_id: str,
    occurrence: WorldOccurrenceProjection,
    clock: ClockObservation,
    clock_transition: ClockTransitionProjection,
    plan_evidence: tuple[EvidenceRef, ...],
) -> WorldEvent:
    continuation_id = _continuation_id(
        operation="activate",
        world_id=world_id,
        occurrence=occurrence,
        clock_transition=clock_transition,
    )
    payload = {
        "change_id": f"change:{continuation_id}",
        "transition_id": f"transition:{continuation_id}",
        "expected_entity_revision": occurrence.entity_revision,
        "evidence_refs": [
            _clock_evidence(clock_transition).model_dump(mode="json"),
            *(item.model_dump(mode="json") for item in plan_evidence),
        ],
        "policy_refs": OCCURRENCE_CLOCK_POLICY_REFS,
        "occurrence_id": occurrence.occurrence_id,
        "activated_at": clock.logical_time_to.isoformat(),
        "satisfied_precondition_refs": tuple(sorted(occurrence.precondition_refs)),
    }
    return _event(
        world_id=world_id,
        event_type="WorldOccurrenceActivated",
        event_id=f"event:{continuation_id}",
        payload=payload,
        clock=clock,
        clock_transition=clock_transition,
    )


def _expiry_event(
    *,
    world_id: str,
    occurrence: WorldOccurrenceProjection,
    clock: ClockObservation,
    clock_transition: ClockTransitionProjection,
) -> WorldEvent:
    continuation_id = _continuation_id(
        operation="expire",
        world_id=world_id,
        occurrence=occurrence,
        clock_transition=clock_transition,
    )
    payload = {
        "change_id": f"change:{continuation_id}",
        "transition_id": f"transition:{continuation_id}",
        "expected_entity_revision": occurrence.entity_revision,
        "evidence_refs": [_clock_evidence(clock_transition).model_dump(mode="json")],
        "policy_refs": OCCURRENCE_CLOCK_POLICY_REFS,
        "occurrence_id": occurrence.occurrence_id,
        "effective_at": clock.logical_time_to.isoformat(),
        "reason_ref": f"clock-expired:{clock_transition.clock_event_ref}",
    }
    return _event(
        world_id=world_id,
        event_type="WorldOccurrenceExpired",
        event_id=f"event:{continuation_id}",
        payload=payload,
        clock=clock,
        clock_transition=clock_transition,
    )


def _event(
    *,
    world_id: str,
    event_type: str,
    event_id: str,
    payload: dict[str, object],
    clock: ClockObservation,
    clock_transition: ClockTransitionProjection,
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version=clock.schema_version,
        event_id=event_id,
        world_id=world_id,
        event_type=event_type,
        logical_time=clock.logical_time_to,
        created_at=clock.created_at,
        actor="system:occurrence-clock",
        source="scheduler",
        trace_id=clock.trace_id,
        causation_id=clock_transition.clock_event_ref,
        correlation_id=clock.correlation_id,
        idempotency_key=(
            domain_idempotency_key(
                event_type=event_type,
                world_id=world_id,
                payload=payload,
            )
            or event_id.removeprefix("event:")
        ),
        payload=payload,
    )


def _satisfied_precondition_evidence(
    *,
    occurrence: WorldOccurrenceProjection,
    plans: tuple[PlanStateProjection, ...],
) -> tuple[EvidenceRef, ...] | None:
    plans_by_id = {item.plan_id: item for item in plans}
    evidence: list[EvidenceRef] = []
    for precondition_ref in occurrence.precondition_refs:
        if not precondition_ref.startswith("plan:"):
            return None
        plan = plans_by_id.get(precondition_ref.removeprefix("plan:"))
        if plan is None or plan.status not in {"planned", "active", "paused"}:
            return None
        evidence.append(
            EvidenceRef(
                ref_id=plan.plan_id,
                evidence_type="active_plan",
                claim_purpose="future_plan",
                immutable_hash=_canonical_plan_evidence_hash(plan),
            )
        )
    return tuple(evidence)


def _clock_evidence(clock_transition: ClockTransitionProjection) -> EvidenceRef:
    return EvidenceRef(
        ref_id=clock_transition.clock_event_ref,
        evidence_type="committed_world_event",
        claim_purpose="current_fact",
        source_world_revision=clock_transition.computed_world_revision,
        immutable_hash=clock_transition.payload_hash,
    )


def _continuation_id(
    *,
    operation: str,
    world_id: str,
    occurrence: WorldOccurrenceProjection,
    clock_transition: ClockTransitionProjection,
) -> str:
    encoded = json.dumps(
        {
            "clock_event_ref": clock_transition.clock_event_ref,
            "clock_payload_hash": clock_transition.payload_hash,
            "expected_entity_revision": occurrence.entity_revision,
            "occurrence_id": occurrence.occurrence_id,
            "operation": operation,
            "world_id": world_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"occurrence-clock:{operation}:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_plan_evidence_hash(plan: PlanStateProjection) -> str:
    fields_to_exclude: set[str] = set()
    if plan.owner_actor_ref == "legacy:unknown-owner":
        fields_to_exclude = {"owner_actor_ref", "authority_origin"}
    encoded = json.dumps(
        plan.model_dump(mode="json", exclude=fields_to_exclude),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
