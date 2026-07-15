"""Source-bound reconsideration gates for un-dispatched expression beats.

The event is deliberately a *gate*, not a hidden policy decision.  A new user
observation makes an old, not-yet-dispatched beat ineligible for dispatch until
the dedicated worker has recorded an explicit continuation decision.  The
worker may later continue, cancel, merge or supersede, but this first vertical
does not silently reuse the old payload while that decision is absent.
"""

from __future__ import annotations

import hashlib
import json

from .event_identity import domain_idempotency_key
from .schemas import LedgerProjection, Observation, TriggerProcess, WorldEvent


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def expression_reconsideration_trigger_id(
    *, world_id: str, plan_id: str, beat_id: str, observation_id: str
) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": "expression-reconsideration-trigger.1",
                "world_id": world_id,
                "plan_id": plan_id,
                "beat_id": beat_id,
                "observation_id": observation_id,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"trigger:expression-reconsideration:{digest}"


def expression_reconsideration_trigger_ref(
    *, plan_id: str, beat_id: str, observation_id: str
) -> str:
    """Opaque, displayable lineage; identity itself remains the digest above."""

    return "expression-reconsideration:" + _canonical_json(
        {"plan_id": plan_id, "beat_id": beat_id, "observation_id": observation_id}
    )


def expression_reconsideration_trigger_event(
    *,
    world_id: str,
    source_event: WorldEvent,
    observation: Observation,
    plan_id: str,
    beat_id: str,
) -> WorldEvent:
    """Open one exact gate for an old beat after a user observation.

    The caller must place this event immediately after ``source_event`` in the
    same CAS commit.  Reducers verify both source event and current beat/action
    eligibility; this factory never grants dispatch authority.
    """

    if source_event.event_type != "ObservationRecorded":
        raise ValueError("expression reconsideration requires an observation event")
    if source_event.world_id != world_id or observation.world_id != world_id:
        raise ValueError("expression reconsideration source belongs to another world")
    if source_event.payload() != observation.model_dump(mode="json"):
        raise ValueError("expression reconsideration source does not bind its observation")
    trigger_id = expression_reconsideration_trigger_id(
        world_id=world_id,
        plan_id=plan_id,
        beat_id=beat_id,
        observation_id=observation.observation_id,
    )
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=expression_reconsideration_trigger_ref(
            plan_id=plan_id, beat_id=beat_id, observation_id=observation.observation_id
        ),
        process_kind="expression_reconsideration",
        source_evidence_ref=source_event.event_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessOpened", world_id=world_id, payload=payload
    )
    if identity is None:
        raise ValueError("expression reconsideration trigger lacks a domain identity")
    return WorldEvent.from_payload(
        schema_version=source_event.schema_version,
        event_id="event:expression-reconsideration-trigger-opened:"
        + trigger_id.removeprefix("trigger:"),
        world_id=world_id,
        event_type="TriggerProcessOpened",
        logical_time=source_event.logical_time,
        created_at=source_event.created_at,
        actor="system:expression-reconsideration-trigger",
        source="world-runtime",
        trace_id=source_event.trace_id,
        causation_id=source_event.event_id,
        correlation_id=source_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


def expression_reconsideration_events_for_observation(
    *, projection: LedgerProjection, observation: Observation, source_event: WorldEvent
) -> tuple[WorldEvent, ...]:
    """Return gates only for currently un-dispatched, interruptible beats.

    Events are ordered by beat ID to make a same-observation multi-beat batch
    stable across replay.  Beats already handed to a provider are deliberately
    omitted: their payload is immutable and they settle through receipts.
    """

    active_plans = {item.plan_id for item in projection.expression_plans if item.state == "authorized"}
    by_action = {item.action_id: item for item in projection.actions}
    candidates = sorted(
        (
            beat
            for beat in projection.expression_beats
            if beat.state == "authorized"
            and beat.plan_id in active_plans
            and beat.action_id is not None
            and beat.reconsider_policy != "never"
            and (action := by_action.get(beat.action_id)) is not None
            and action.state in {"authorized", "scheduled", "claimed"}
        ),
        key=lambda item: item.beat_id,
    )
    return tuple(
        expression_reconsideration_trigger_event(
            world_id=projection.world_id,
            source_event=source_event,
            observation=observation,
            plan_id=beat.plan_id,
            beat_id=beat.beat_id,
        )
        for beat in candidates
    )


def expression_beat_is_gated(*, projection: LedgerProjection, plan_id: str, beat_id: str) -> bool:
    """Whether an open/claimed interjection gate prevents old-payload dispatch."""

    for process in projection.trigger_processes:
        if process.process_kind != "expression_reconsideration":
            continue
        prefix = "expression-reconsideration:"
        if not process.trigger_ref.startswith(prefix):
            continue  # reducer rejects this, but dispatch must fail closed too.
        try:
            lineage = json.loads(process.trigger_ref.removeprefix(prefix))
        except json.JSONDecodeError:
            return True
        if not isinstance(lineage, dict):
            return True
        if lineage.get("plan_id") != plan_id or lineage.get("beat_id") != beat_id:
            continue
        if process.state != "terminal":
            return True
        # A recorded defer is a decision, not an accidental unlock.  It holds
        # the frozen payload until later user/world evidence opens a *new*
        # source-bound gate.  Other terminal decisions deliberately release
        # this gate: continue permits the old immutable payload; replacement
        # dispositions have cancelled the old Action atomically.
        outcome = process.runtime_outcome_ref or ""
        prefix = "expression-reconsideration-decision:"
        if not outcome.startswith(prefix):
            continue
        try:
            decision = json.loads(outcome.removeprefix(prefix)).get("decision")
        except json.JSONDecodeError:
            return True
        if not isinstance(decision, dict):
            return True
        if decision.get("disposition") == "defer":
            return True
    return False


__all__ = [
    "expression_beat_is_gated",
    "expression_reconsideration_events_for_observation",
    "expression_reconsideration_trigger_event",
    "expression_reconsideration_trigger_id",
    "expression_reconsideration_trigger_ref",
]
