"""Machine-enforced domain idempotency identities for typed event families."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .schemas import WorldEvent
from .typed_proposal_families import family_for_mutation, family_for_record


def domain_idempotency_key(
    *, event_type: str, world_id: str, payload: dict[str, Any]
) -> str | None:
    """Derive the installed event identity; return None for legacy families."""

    components = _life_identity_components(event_type, world_id, payload)
    if components is None:
        return None
    encoded = json.dumps(
        [event_type, *components],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"world-v2:{event_type}:{digest}"


def validate_event_identity(event: WorldEvent) -> None:
    if event.event_type == "LegacyAcceptanceAuditRecorded":
        raise ValueError("legacy acceptance audit events are migration-only")
    expected = domain_idempotency_key(
        event_type=event.event_type,
        world_id=event.world_id,
        payload=event.payload(),
    )
    if expected is not None and event.idempotency_key != expected:
        raise ValueError(f"{event.event_type} idempotency key does not match its domain identity")


def _life_identity_components(
    event_type: str, world_id: str, payload: dict[str, Any]
) -> tuple[object, ...] | None:
    proposal_family = family_for_record(event_type, payload)
    if proposal_family is not None:
        return proposal_family.codec.record_identity(
            world_id=world_id,
            event_type=event_type,
            payload=payload,
        )
    mutation_family = family_for_mutation(event_type)
    if mutation_family is not None:
        return mutation_family.codec.mutation_identity(
            world_id=world_id,
            event_type=event_type,
            payload=payload,
        )
    if event_type == "NpcRegistered":
        return world_id, _nested(payload, "npc", "npc_id")
    if (
        event_type == "ObservationRecorded"
        and payload.get("observation_kind") == "message"
        and isinstance(payload.get("source"), str)
        and isinstance(payload.get("source_event_id"), str)
    ):
        return payload.get("source"), payload.get("source_event_id")
    if event_type == "OperatorObservationRecorded":
        return world_id, payload.get("observation_id")
    if event_type == "ActivityPlanned":
        return _nested(payload, "plan", "plan_id"), payload.get("transition_id")
    if event_type in {
        "ActivityStarted",
        "ActivityPaused",
        "ActivityResumed",
        "ActivityCompleted",
        "ActivityAbandoned",
    }:
        return payload.get("plan_id"), payload.get("transition_id")
    if event_type == "WorldOccurrenceCommitted":
        return (
            _nested(payload, "occurrence", "occurrence_id"),
            payload.get("transition_id"),
        )
    if event_type == "WorldOccurrenceActivated":
        return payload.get("occurrence_id"), payload.get("transition_id")
    if event_type == "OutcomeObservationRecorded":
        return world_id, _nested(payload, "observation", "observation_id")
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("proposal_id") is not None
        and payload.get("evaluated_world_revision") is not None
    ):
        return world_id, payload.get("proposal_id"), payload.get("evaluated_world_revision")
    if event_type == "ExperienceCommitted":
        return world_id, _nested(payload, "experience", "experience_id")
    if event_type in {"WorldOccurrenceCancelled", "WorldOccurrenceExpired"}:
        return payload.get("occurrence_id"), payload.get("transition_id")
    if event_type == "AppraisalExpired":
        return payload.get("appraisal_id"), payload.get("transition_id")
    if event_type == "AffectEpisodeDecayed":
        results = payload.get("component_results")
        config_digests = (
            tuple(item.get("config_digest") for item in results if isinstance(item, dict))
            if isinstance(results, list)
            else ()
        )
        return (
            payload.get("episode_id"),
            payload.get("expected_entity_revision"),
            payload.get("to_logical_time"),
            config_digests,
        )
    if event_type == "TriggerProcessOpened":
        return world_id, _nested(payload, "process", "trigger_id"), "opened"
    if event_type in {"TriggerProcessClaimed", "TriggerProcessReclaimed"}:
        process = payload.get("process")
        if isinstance(process, dict) and process.get("process_kind") in {
            "npc_world_appraisal",
            "interaction_appraisal",
        }:
            attempts = process.get("attempt_ids")
            attempt_id = attempts[-1] if isinstance(attempts, list) and attempts else None
            return world_id, process.get("trigger_id"), attempt_id, event_type
    return None


def _nested(payload: dict[str, Any], parent: str, child: str) -> object:
    value = payload.get(parent)
    if not isinstance(value, dict):
        return None
    return value.get(child)
