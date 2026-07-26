"""Durable lifecycle identity for one visible Expression Episode.

The lifecycle reuses TriggerProcess plus the existing ModelResult/Proposal/
Action lineage.  It never authorizes delivery: opening and claiming only make
crash recovery and the afterthought mutex projection-visible.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from .event_identity import domain_idempotency_key
from .minimal_reply_events import (
    ExpressionBeatTerminatedPayload,
    ExpressionPlanTerminatedPayload,
)
from .schemas import ClaimLease, Observation, TriggerProcess, WorldEvent


PROCESS_KIND = "expression_episode"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def expression_episode_trigger_id(world_id: str, observation_id: str) -> str:
    if not world_id or not observation_id:
        raise ValueError("expression episode identity requires world and observation")
    return "trigger:expression-episode:" + _digest(
        {"world_id": world_id, "observation_id": observation_id}
    )


def expression_episode_open_event(
    *, observation: Observation, observation_event: WorldEvent
) -> WorldEvent:
    if (
        observation_event.event_type != "ObservationRecorded"
        or observation_event.world_id != observation.world_id
    ):
        raise ValueError("expression episode requires its ObservationRecorded authority")
    process = TriggerProcess(
        trigger_id=expression_episode_trigger_id(
            observation.world_id, observation.observation_id
        ),
        trigger_ref=f"expression-episode:{observation.observation_id}",
        process_kind=PROCESS_KIND,
        source_evidence_ref=observation.observation_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:expression-episode:opened:" + _digest(payload),
        world_id=observation.world_id,
        event_type="TriggerProcessOpened",
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor=observation.actor,
        source="world-runtime:expression-episode",
        trace_id=observation.trace_id,
        causation_id=observation_event.event_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="TriggerProcessOpened",
            world_id=observation.world_id,
            payload=payload,
        )
        or "expression-episode:opened:" + process.trigger_id,
        payload=payload,
    )


def expression_episode_claim_event(
    *,
    world_id: str,
    process: TriggerProcess,
    owner_id: str,
    at: datetime,
    trace_id: str,
    correlation_id: str,
    lease_seconds: int = 300,
) -> tuple[WorldEvent, TriggerProcess]:
    if process.process_kind != PROCESS_KIND or process.state != "open":
        raise ValueError("only an open expression episode can be claimed")
    attempt_id = "attempt:expression-episode:" + _digest(
        {"trigger_id": process.trigger_id, "attempt": 1}
    )
    claimed = process.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id=owner_id,
                attempt_id=attempt_id,
                acquired_at=at,
                expires_at=at + timedelta(seconds=lease_seconds),
            ),
            "attempt_ids": (attempt_id,),
        }
    )
    payload = {"process": claimed.model_dump(mode="json")}
    return (
        WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:expression-episode:claimed:" + _digest(payload),
            world_id=world_id,
            event_type="TriggerProcessClaimed",
            logical_time=at,
            created_at=at,
            actor=owner_id,
            source="world-runtime:expression-episode",
            trace_id=trace_id,
            causation_id=process.trigger_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="TriggerProcessClaimed", world_id=world_id, payload=payload
            )
            or "expression-episode:claimed:" + attempt_id,
            payload=payload,
        ),
        claimed,
    )


def expression_episode_complete_event(
    *,
    world_id: str,
    process: TriggerProcess,
    at: datetime,
    trace_id: str,
    correlation_id: str,
    outcome_ref: str,
) -> WorldEvent:
    if (
        process.process_kind != PROCESS_KIND
        or process.state != "claimed"
        or process.claim_lease is None
    ):
        raise ValueError("expression episode completion requires its claimed lifecycle")
    payload = {
        "trigger_id": process.trigger_id,
        "owner_id": process.claim_lease.owner_id,
        "attempt_id": process.claim_lease.attempt_id,
        "completed_at": at.isoformat(),
        "runtime_outcome_ref": outcome_ref,
    }
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:expression-episode:completed:" + _digest(payload),
        world_id=world_id,
        event_type="TriggerProcessCompleted",
        logical_time=at,
        created_at=at,
        actor=process.claim_lease.owner_id,
        source="world-runtime:expression-episode",
        trace_id=trace_id,
        causation_id=process.trigger_id,
        correlation_id=correlation_id,
        idempotency_key="expression-episode:completed:" + _digest(
            {"world_id": world_id, "payload": payload}
        ),
        payload=payload,
    )


def expression_episode_cancel_events(
    *,
    world_id: str,
    projection,
    process: TriggerProcess,
    observation: Observation,
    observation_event_ref: str,
    superseded: bool = False,
) -> tuple[WorldEvent, ...]:
    """Atomically cancel every undispatched beat and release its reservation."""

    if process.process_kind != PROCESS_KIND or process.state != "claimed":
        raise ValueError("episode cancellation requires a claimed lifecycle")
    actions = tuple(
        item
        for item in projection.actions
        if item.state in {"authorized", "scheduled", "claimed"}
        and item.expression_plan_id is not None
        and any(
            audit.trigger_ref == observation_event_ref
            and manifest.proposal_id == audit.proposal_id
            and manifest.plan_id == item.expression_plan_id
            for manifest in projection.expression_plan_manifests
            for audit in projection.proposal_audits
        )
    )
    if not actions:
        return ()
    plan_ids = {item.expression_plan_id for item in actions}
    if len(plan_ids) != 1:
        raise ValueError("episode cancellation must bind one accepted plan")
    plan_id = next(iter(plan_ids))
    plan = next(
        (item for item in projection.expression_plans if item.plan_id == plan_id),
        None,
    )
    if plan is None or plan.state != "authorized":
        raise ValueError("episode cancellation requires an active plan")
    at = projection.logical_time or observation.logical_time
    common = {
        "schema_version": "world-v2.1",
        "world_id": world_id,
        "logical_time": at,
        "created_at": observation.created_at,
        "actor": process.claim_lease.owner_id if process.claim_lease else observation.actor,
        "source": "world-runtime:expression-episode-cancel",
        "trace_id": observation.trace_id,
        "causation_id": process.trigger_id,
        "correlation_id": observation.correlation_id,
    }
    events: list[WorldEvent] = []
    disposition = "superseded" if superseded else "cancelled"
    terminal_beat_id = ""
    for action in actions:
        beat = next(
            (
                item
                for item in projection.expression_beats
                if item.beat_id == action.expression_beat_id
            ),
            None,
        )
        reservation = next(
            (
                item
                for item in projection.budget_reservations
                if item.reservation_id == action.budget_reservation_id
            ),
            None,
        )
        if (
            beat is None
            or beat.state != "authorized"
            or reservation is None
            or reservation.state != "reserved"
        ):
            raise ValueError("episode cancellation lacks beat or budget authority")
        cancellation_id = "cancellation:expression-episode:" + _digest(
            {"trigger_id": process.trigger_id, "action_id": action.action_id}
        )
        cancel_payload = {"action_id": action.action_id}
        cancelled = WorldEvent.from_payload(
            **common,
            event_id="event:expression-episode:action-cancelled:"
            + _digest(cancel_payload | {"cancellation_id": cancellation_id}),
            event_type="ActionCancelled",
            idempotency_key="expression-episode:cancel:" + cancellation_id,
            payload=cancel_payload,
        )
        beat_payload = ExpressionBeatTerminatedPayload(
            acceptance_id=beat.acceptance_id,
            proposal_id=beat.proposal_id,
            plan_id=beat.plan_id,
            beat_id=beat.beat_id,
            action_id=action.action_id,
            disposition=disposition,
            source_event_ref=cancelled.event_id,
            source_event_payload_hash=cancelled.payload_hash,
        ).model_dump(mode="json")
        beat_event = WorldEvent.from_payload(
            **{**common, "causation_id": cancelled.event_id},
            event_id="event:expression-episode:beat-terminated:"
            + _digest(beat_payload),
            event_type="ExpressionBeatTerminated",
            idempotency_key=domain_idempotency_key(
                event_type="ExpressionBeatTerminated",
                world_id=world_id,
                payload=beat_payload,
            )
            or "expression-episode:beat-terminated:" + beat.beat_id,
            payload=beat_payload,
        )
        result_id = "result:expression-episode:cancel:" + _digest(
            {"trigger_id": process.trigger_id, "action_id": action.action_id}
        )
        settlement = {
            "settlement_id": "settlement:expression-episode:" + _digest(result_id),
            "reservation_id": reservation.reservation_id,
            "action_id": action.action_id,
            "result_id": result_id,
            "state": "released",
            "settlement_kind": "terminal",
            "previous_cost": reservation.settled_cost,
            "cost_actual": 0,
            "cost_delta": -reservation.settled_cost,
        }
        released = WorldEvent.from_payload(
            **{**common, "causation_id": beat_event.event_id},
            event_id="event:expression-episode:budget-released:"
            + _digest(settlement),
            event_type="BudgetReleased",
            idempotency_key="expression-episode:release:"
            + _digest({"world_id": world_id, "settlement": settlement}),
            payload={"settlement": settlement},
        )
        events.extend((cancelled, beat_event, released))
        terminal_beat_id = beat.beat_id
    plan_payload = ExpressionPlanTerminatedPayload(
        acceptance_id=plan.acceptance_id,
        proposal_id=plan.proposal_id,
        plan_id=plan.plan_id,
        terminal_beat_id=terminal_beat_id,
        disposition=disposition,
        source_event_ref=events[0].event_id,
        source_event_payload_hash=events[0].payload_hash,
    ).model_dump(mode="json")
    events.append(
        WorldEvent.from_payload(
            **{**common, "causation_id": events[-1].event_id},
            event_id="event:expression-episode:plan-terminated:"
            + _digest(plan_payload),
            event_type="ExpressionPlanTerminated",
            idempotency_key=domain_idempotency_key(
                event_type="ExpressionPlanTerminated",
                world_id=world_id,
                payload=plan_payload,
            )
            or "expression-episode:plan-terminated:" + plan.plan_id,
            payload=plan_payload,
        )
    )
    return tuple(events)


__all__ = [
    "PROCESS_KIND",
    "expression_episode_claim_event",
    "expression_episode_cancel_events",
    "expression_episode_complete_event",
    "expression_episode_open_event",
    "expression_episode_trigger_id",
]
