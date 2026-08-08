"""Directional relationship material for the canonical Character Interior.

The protagonist's own relationship heads are private protagonist state.  The
reverse direction is deliberately narrower: only an NPC's already-committed,
shareable action involving the protagonist is visible.  NPC subjective state,
goals and Affect are not inputs to this join.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Mapping

from ..life_events import WorldOccurrenceSettledPayload
from ..relationship_events import RelationshipSlowVariableAdjustedPayload
from ..schemas import LedgerProjection, ProjectionCursor, validate_plan_authority_state


@dataclass(frozen=True, slots=True)
class RelationshipContextJoin:
    protagonist_npc_items: tuple[dict[str, object], ...]
    npc_observable_items: tuple[dict[str, object], ...]
    source_envelopes: Mapping[str, Mapping[str, object]]


async def _lookup(ledger: object, event_ref: str):
    operation = getattr(ledger, "lookup_event_commit", None)
    if not callable(operation):
        raise ValueError("relationship context authority lookup is unavailable")
    if bool(getattr(ledger, "blocks_event_loop", False)):
        return await asyncio.to_thread(operation, event_ref)
    return operation(event_ref)


async def _pinned_event(
    *,
    ledger: object,
    projection: LedgerProjection,
    cursor: ProjectionCursor,
    event_ref: str,
):
    located = await _lookup(ledger, event_ref)
    committed = next(
        (item for item in projection.committed_world_event_refs if item.event_id == event_ref),
        None,
    )
    if located is None or committed is None:
        raise ValueError("relationship context authority is not committed")
    event, commit = located
    if (
        event.event_id != event_ref
        or event.world_id != projection.world_id
        or event.event_id not in commit.event_ids
        or committed.event_type != event.event_type
        or committed.payload_hash != event.payload_hash
        or committed.world_revision != commit.world_revision
        or commit.world_revision > cursor.world_revision
        or commit.deliberation_revision > cursor.deliberation_revision
        or commit.ledger_sequence > cursor.ledger_sequence
    ):
        raise ValueError("relationship context authority mismatches pinned prefix")
    return event, committed


def _envelope(
    *,
    item_ref: str,
    value: Mapping[str, object],
    privacy_class: str,
    event: object,
    committed: object,
) -> dict[str, object]:
    return {
        "item_ref": item_ref,
        "privacy_class": privacy_class,
        "value": dict(value),
        "source_bindings": [
            {
                "source_kind": "committed_event",
                "authority_type": event.event_type,
                "ref": event.event_id,
                "source_world_revision": committed.world_revision,
                "immutable_hash": event.payload_hash,
            }
        ],
    }


async def build_relationship_context_join(
    *,
    ledger: object,
    projection: LedgerProjection,
    actor_ref: str,
    cursor: ProjectionCursor,
) -> RelationshipContextJoin:
    """Build exact directional relationship views at one pinned ledger prefix."""

    registered_npcs = {f"npc:{item.npc_id}" for item in projection.npcs}
    protagonist_items: list[dict[str, object]] = []
    observable_items: list[dict[str, object]] = []
    envelopes: dict[str, Mapping[str, object]] = {}

    for state in sorted(projection.relationship_states, key=lambda item: item.relationship_id):
        if state.subject_ref not in registered_npcs or state.origin is None:
            continue
        event, committed = await _pinned_event(
            ledger=ledger,
            projection=projection,
            cursor=cursor,
            event_ref=state.origin.accepted_event_ref,
        )
        if event.event_type != "RelationshipSlowVariableAdjusted":
            raise ValueError("NPC relationship head has the wrong authority type")
        payload = RelationshipSlowVariableAdjustedPayload.model_validate_json(event.payload_json)
        if (
            payload.relationship_id != state.relationship_id
            or payload.subject_ref != state.subject_ref
            or payload.expected_entity_revision + 1 != state.entity_revision
            or payload.variables_after != state.variables
            or payload.stage_after != state.stage
            or payload.hysteresis_after != state.hysteresis
            or payload.commitment_refs != state.commitment_refs
            or payload.policy_version != state.policy_version
            or payload.policy_digest != state.policy_digest
            or payload.adjusted_at != state.last_adjusted_at
            or payload.change_id != state.origin.change_id
            or payload.transition_id != state.origin.transition_id
            or payload.policy_refs != state.origin.policy_refs
        ):
            raise ValueError("NPC relationship head changed accepted meaning")
        item_ref = f"interior:relationship:protagonist:{state.relationship_id}"
        value = {
            "relationship_id": state.relationship_id,
            "entity_revision": state.entity_revision,
            "direction": "protagonist_to_npc",
            "subject_ref": state.subject_ref,
            "stage": state.stage,
            "variables": state.variables.model_dump(mode="json"),
            "temperature": state.temperature,
            "hysteresis": state.hysteresis.model_dump(mode="json"),
            "commitment_refs": list(state.commitment_refs),
            "last_adjusted_at": (
                state.last_adjusted_at.isoformat() if state.last_adjusted_at else None
            ),
            "accepted_event_ref": event.event_id,
        }
        protagonist_items.append(
            {"item_ref": item_ref, "privacy_class": "private", "value": value}
        )
        envelopes[item_ref] = _envelope(
            item_ref=item_ref,
            value=value,
            privacy_class="private",
            event=event,
            committed=committed,
        )

    observable_plans = sorted(
        (
            item
            for item in projection.plans
            if item.owner_actor_ref in registered_npcs
            and actor_ref in item.participant_refs
            and item.status in {"active", "completed"}
            and item.privacy_class in {"public", "shareable"}
            and item.authority_origin is not None
        ),
        key=lambda item: (item.last_transitioned_at, item.plan_id),
        reverse=True,
    )[:8]
    for plan in observable_plans:
        validate_plan_authority_state(
            (plan,),
            projection.committed_world_event_refs,
            logical_time=projection.logical_time,
        )
        origin = plan.authority_origin
        assert origin is not None
        event, committed = await _pinned_event(
            ledger=ledger,
            projection=projection,
            cursor=cursor,
            event_ref=origin.accepted_event_ref,
        )
        item_ref = f"interior:relationship:{plan.owner_actor_ref}:{plan.plan_id}"
        value = {
            "entity_revision": plan.entity_revision,
            "direction": "npc_to_protagonist",
            "npc_ref": plan.owner_actor_ref,
            "toward_actor_ref": actor_ref,
            "epistemic_scope": "observable_action_only",
            "observable_act": {
                "plan_ref": plan.plan_id,
                "activity_kind": plan.activity_kind,
                "status": plan.status,
                "participant_refs": list(plan.participant_refs),
                "location_ref": plan.location_ref,
                "observed_at": (
                    plan.last_transitioned_at.isoformat()
                    if plan.last_transitioned_at is not None
                    else event.logical_time.isoformat()
                ),
            },
            "accepted_event_ref": event.event_id,
        }
        observable_items.append(
            {"item_ref": item_ref, "privacy_class": plan.privacy_class, "value": value}
        )
        envelopes[item_ref] = _envelope(
            item_ref=item_ref,
            value=value,
            privacy_class=plan.privacy_class,
            event=event,
            committed=committed,
        )

    return RelationshipContextJoin(
        protagonist_npc_items=tuple(protagonist_items),
        npc_observable_items=tuple(observable_items),
        source_envelopes=envelopes,
    )


def install_relationship_context(
    context: Mapping[str, object], join: RelationshipContextJoin
) -> dict[str, object]:
    result = dict(context)
    slices = dict(context.get("slices") or {})
    if join.protagonist_npc_items:
        slices["protagonist_npc_relationships"] = {
            "availability": "available",
            "items": list(join.protagonist_npc_items),
        }
    if join.npc_observable_items:
        slices["npc_observable_attitudes"] = {
            "availability": "available",
            "items": list(join.npc_observable_items),
        }
    result["slices"] = slices
    return result


def relationship_transition_subject_refs(
    *, projection: object, source_event: object
) -> tuple[str, ...]:
    """Subjects the existing typed relationship signal seam may address.

    A new NPC direction is offered only when that exact settled occurrence
    names the registered NPC as a participant.  The helper exposes permission,
    never a requirement or a relationship verdict.
    """

    subjects = {item.subject_ref for item in projection.relationship_states}
    if source_event.event_type != "WorldOccurrenceSettled":
        return tuple(sorted(subjects))
    payload = WorldOccurrenceSettledPayload.model_validate_json(source_event.payload_json)
    occurrences = [
        item
        for item in projection.world_occurrences
        if item.occurrence_id == payload.occurrence_id
        and item.status == "settled"
        and item.settlement_event_ref == source_event.event_id
    ]
    if len(occurrences) != 1:
        raise ValueError("relationship transition occurrence is not cursor exact")
    registered = {f"npc:{item.npc_id}" for item in projection.npcs}
    subjects.update(set(occurrences[0].participant_refs) & registered)
    return tuple(sorted(subjects))


__all__ = [
    "RelationshipContextJoin",
    "build_relationship_context_join",
    "install_relationship_context",
    "relationship_transition_subject_refs",
]
