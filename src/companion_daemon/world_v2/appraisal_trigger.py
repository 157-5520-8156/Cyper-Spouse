"""Deterministic ingress lifecycle for source-bound interaction appraisal.

This module does not interpret a message and does not mutate a ledger.  It
only derives the open/claim events for the one appraisal opportunity owned by
an already committed incoming Observation.  Runtime remains the only caller
that commits those events under its per-world orchestration lock.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json

from .batch_invariants import interaction_appraisal_trigger_identity
from .event_identity import domain_idempotency_key
from .schemas import ClaimLease, Observation, TriggerProcess, WorldEvent


INTERACTION_APPRAISAL_TRIGGER_VERSION = "interaction-appraisal-trigger.2"
CHARACTER_INTERIOR_INBOUND_ATTEMPT_PREFIX = "attempt:character-interior-inbound:"
DEFAULT_INTERACTION_APPRAISAL_LEASE_SECONDS = 120
INTERACTION_APPRAISAL_PROPOSAL_ID_PREFIXES = (
    "proposal:appraisal-draft:",
    "proposal:interaction-appraisal:",
)
INTERACTION_APPRAISAL_FOLDED_OUTCOME = (
    "interaction-appraisal:folded-into-newer-inbound"
)


class InteractionAppraisalTriggerError(ValueError):
    """Stable invalid-authority failure for interaction trigger derivation."""


def is_interaction_appraisal_audit(
    audit: object,
    *,
    trigger_ref: str | None = None,
) -> bool:
    """Classify the installed appraisal proposal family at one source event."""

    return (
        getattr(audit, "proposal_kind", None) == "decision"
        and (
            trigger_ref is None
            or getattr(audit, "trigger_ref", None) == trigger_ref
        )
        and isinstance((proposal_id := getattr(audit, "proposal_id", None)), str)
        and proposal_id.startswith(INTERACTION_APPRAISAL_PROPOSAL_ID_PREFIXES)
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _event_id(*, role: str, world_id: str, trigger_id: str, attempt_id: str) -> str:
    return f"event:interaction-appraisal:{role}:{_digest((world_id, trigger_id, attempt_id, role))}"


def _attempt_id(*, world_id: str, trigger_id: str, observation_event: WorldEvent) -> str:
    return CHARACTER_INTERIOR_INBOUND_ATTEMPT_PREFIX + _digest(
        {
            "version": INTERACTION_APPRAISAL_TRIGGER_VERSION,
            "world_id": world_id,
            "trigger_id": trigger_id,
            "observation_event_id": observation_event.event_id,
            "observation_payload_hash": observation_event.payload_hash,
        }
    )


def interaction_appraisal_trigger_events(
    *,
    observation: Observation,
    observation_event: WorldEvent,
    owner_id: str,
    lease_seconds: int = DEFAULT_INTERACTION_APPRAISAL_LEASE_SECONDS,
) -> tuple[WorldEvent, WorldEvent]:
    """Derive the exact open then claim events for one observed user message.

    The pair intentionally has no model output and no domain mutation.  A
    caller may safely retry it because both event identities and idempotency
    keys bind the committed Observation envelope.
    """

    if not owner_id:
        raise InteractionAppraisalTriggerError("interaction appraisal owner is required")
    if lease_seconds <= 0:
        raise InteractionAppraisalTriggerError("interaction appraisal lease must be positive")
    if (
        observation_event.world_id != observation.world_id
        or observation_event.event_type != "ObservationRecorded"
        or observation_event.payload() != observation.model_dump(mode="json")
        or observation_event.logical_time != observation.logical_time
        or observation_event.created_at != observation.created_at
    ):
        raise InteractionAppraisalTriggerError("observation does not match committed authority")

    trigger_id = interaction_appraisal_trigger_identity(
        observation.world_id, observation.observation_id
    )
    attempt_id = _attempt_id(
        world_id=observation.world_id, trigger_id=trigger_id, observation_event=observation_event
    )
    opened = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"interaction:{observation.observation_id}",
        process_kind="interaction_appraisal",
        source_evidence_ref=observation.observation_id,
        state="open",
    )
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id=owner_id,
                attempt_id=attempt_id,
                acquired_at=observation.logical_time,
                expires_at=observation.logical_time + timedelta(seconds=lease_seconds),
            ),
            "attempt_ids": (attempt_id,),
        }
    )
    common = {
        "schema_version": observation.schema_version,
        "world_id": observation.world_id,
        "logical_time": observation.logical_time,
        "created_at": observation.created_at,
        "actor": owner_id,
        "source": "world-runtime:interaction-appraisal",
        "trace_id": observation.trace_id,
        "correlation_id": observation.correlation_id,
    }
    payloads = ({"process": opened.model_dump(mode="json")}, {"process": claimed.model_dump(mode="json")})
    types = ("TriggerProcessOpened", "TriggerProcessClaimed")
    event_ids = (
        _event_id(role="opened", world_id=observation.world_id, trigger_id=trigger_id, attempt_id=attempt_id),
        _event_id(role="claimed", world_id=observation.world_id, trigger_id=trigger_id, attempt_id=attempt_id),
    )
    events: list[WorldEvent] = []
    for event_type, event_id, payload in zip(types, event_ids, payloads, strict=True):
        identity = domain_idempotency_key(
            event_type=event_type, world_id=observation.world_id, payload=payload
        )
        if identity is None:
            raise InteractionAppraisalTriggerError("trigger event has no domain identity")
        events.append(
            WorldEvent.from_payload(
                **common,
                event_id=event_id,
                event_type=event_type,
                causation_id=observation_event.event_id if not events else events[-1].event_id,
                idempotency_key=identity,
                payload=payload,
            )
        )
    return events[0], events[1]


def interaction_appraisal_folded_event(
    *,
    process: TriggerProcess,
    superseding_observation: Observation,
    superseding_observation_event: WorldEvent,
) -> WorldEvent:
    """Fold an unanswered burst fragment into the newer conversational moment.

    The newer Observation is durable authority for the fold.  Original
    messages remain immutable World evidence; only the redundant appraisal
    opportunity is terminalized, so the newest appraisal can read the whole
    recent-dialogue packet once.
    """

    lease = process.claim_lease
    if (
        process.process_kind != "interaction_appraisal"
        or process.state != "claimed"
        or lease is None
        or not process.source_evidence_ref
    ):
        raise InteractionAppraisalTriggerError(
            "appraisal fold requires one claimed interaction process"
        )
    if (
        superseding_observation_event.world_id != superseding_observation.world_id
        or superseding_observation_event.event_type != "ObservationRecorded"
        or superseding_observation_event.payload()
        != superseding_observation.model_dump(mode="json")
    ):
        raise InteractionAppraisalTriggerError(
            "appraisal fold requires its exact superseding Observation"
        )
    completed_at = max(
        superseding_observation.logical_time,
        lease.acquired_at,
    )
    payload = {
        "trigger_id": process.trigger_id,
        "owner_id": lease.owner_id,
        "attempt_id": lease.attempt_id,
        "completed_at": completed_at.isoformat(),
        "runtime_outcome_ref": INTERACTION_APPRAISAL_FOLDED_OUTCOME,
        "superseding_observation_event_ref": superseding_observation_event.event_id,
    }
    # Generic TriggerProcessCompleted events intentionally have no installed
    # domain codec.  Bind this completion to the claimed attempt exactly as
    # the other trigger runtimes do.
    identity = "world-v2:interaction-appraisal:completion:" + _digest(
        [
            superseding_observation.world_id,
            process.trigger_id,
            lease.attempt_id,
        ]
    )
    return WorldEvent.from_payload(
        schema_version=superseding_observation.schema_version,
        event_id="event:interaction-appraisal:trigger:completed:"
        + _digest([process.trigger_id, lease.attempt_id]),
        world_id=superseding_observation.world_id,
        event_type="TriggerProcessCompleted",
        logical_time=completed_at,
        created_at=max(superseding_observation.created_at, completed_at),
        actor=lease.owner_id,
        source="world-runtime:interaction-appraisal-fold",
        trace_id=superseding_observation.trace_id,
        causation_id=superseding_observation_event.event_id,
        correlation_id=superseding_observation.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


__all__ = [
    "DEFAULT_INTERACTION_APPRAISAL_LEASE_SECONDS",
    "INTERACTION_APPRAISAL_FOLDED_OUTCOME",
    "INTERACTION_APPRAISAL_PROPOSAL_ID_PREFIXES",
    "INTERACTION_APPRAISAL_TRIGGER_VERSION",
    "CHARACTER_INTERIOR_INBOUND_ATTEMPT_PREFIX",
    "InteractionAppraisalTriggerError",
    "interaction_appraisal_folded_event",
    "interaction_appraisal_trigger_events",
    "is_interaction_appraisal_audit",
]
