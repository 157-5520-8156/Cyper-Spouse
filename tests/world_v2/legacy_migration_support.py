"""Helpers for constructing honest pre-.16 state fixtures from current models."""

from __future__ import annotations

import json


V16_ONLY_STATE_FIELDS = (
    "clock_transition_history",
    "goals",
    "goal_transitions",
    "goal_proposals",
    "goal_proposal_ids",
    "locations",
    "location_transitions",
    "location_proposals",
    "location_proposal_ids",
    "resources",
    "resource_transitions",
    "resource_proposals",
    "resource_proposal_ids",
    "attentions",
    "attention_transitions",
    "attention_proposals",
    "attention_proposal_ids",
)


def strip_v16_state_fields(raw: dict[str, object]) -> dict[str, object]:
    for field in V16_ONLY_STATE_FIELDS:
        raw.pop(field, None)
    actor_transitions = raw.get("actor_authority_transitions")
    if isinstance(actor_transitions, list):
        for transition in actor_transitions:
            if isinstance(transition, dict):
                transition.pop("accepted_event_ref", None)
                transition.pop("accepted_world_revision", None)
                transition.pop("accepted_payload_hash", None)
    plans = raw.get("plans")
    if isinstance(plans, list):
        for plan in plans:
            if isinstance(plan, dict):
                plan.pop("owner_actor_ref", None)
                plan.pop("authority_origin", None)
    occurrences = raw.get("world_occurrences")
    if isinstance(occurrences, list):
        for occurrence in occurrences:
            if isinstance(occurrence, dict):
                occurrence.pop("settled_outcome_ref", None)
    return raw


def legacy_state_json(value: str | dict[str, object]) -> str:
    raw = json.loads(value) if isinstance(value, str) else value
    return json.dumps(
        strip_v16_state_fields(raw),
        ensure_ascii=False,
        separators=(",", ":"),
    )
