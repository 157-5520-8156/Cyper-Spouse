"""Ledger-backed Character Interior context for generic interaction acts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Mapping

from .interaction_act_events import InteractionActAcceptedPayload
from .interaction_act_runtime import interaction_act_context_items
from .schemas import LedgerProjection, ProjectionCursor


@dataclass(frozen=True, slots=True)
class InteractionActContextJoin:
    items: tuple[dict[str, object], ...]
    source_envelopes: Mapping[str, Mapping[str, object]]


async def _lookup(ledger: object, event_ref: str):
    operation = getattr(ledger, "lookup_event_commit", None)
    if not callable(operation):
        raise ValueError("interaction act context authority lookup is unavailable")
    if bool(getattr(ledger, "blocks_event_loop", False)):
        return await asyncio.to_thread(operation, event_ref)
    return operation(event_ref)


class InteractionActContextBuilder:
    """Join current typed heads to their latest committed mutation event."""

    __slots__ = ("_ledger",)

    def __init__(self, *, ledger: object) -> None:
        self._ledger = ledger

    async def build(
        self,
        *,
        projection: LedgerProjection,
        actor_ref: str,
        cursor: ProjectionCursor,
        limit: int = 8,
    ) -> InteractionActContextJoin:
        if not actor_ref or limit <= 0:
            raise ValueError("interaction act context query is incomplete")
        candidates = tuple(
            sorted(
                (
                    item
                    for item in projection.interaction_acts
                    if item.subject_ref == actor_ref
                    or actor_ref in item.counterparty_refs
                ),
                key=lambda item: (item.updated_at, item.interaction_act_id),
                reverse=True,
            )[:limit]
        )
        items: list[dict[str, object]] = []
        envelopes: dict[str, Mapping[str, object]] = {}
        committed_by_ref = {
            item.event_id: item for item in projection.committed_world_event_refs
        }
        for current in candidates:
            context_items = interaction_act_context_items(
                (current,),
                history=projection.interaction_act_transitions,
                conversation_ref=current.conversation_ref,
                participant_ref=actor_ref,
                limit=1,
            )
            if len(context_items) != 1:
                raise ValueError("interaction act context head does not resolve exactly")
            item = context_items[0]
            transitions = tuple(
                transition
                for transition in projection.interaction_act_transitions
                if transition.interaction_act_id == current.interaction_act_id
                and transition.entity_revision == current.entity_revision
            )
            if len(transitions) != 1:
                raise ValueError("interaction act latest transition is not exact")
            transition = transitions[0]
            located = await _lookup(self._ledger, item.accepted_event_ref)
            committed = committed_by_ref.get(item.accepted_event_ref)
            if located is None or committed is None:
                raise ValueError("interaction act context authority is not committed")
            event, commit = located
            if (
                event.event_id != item.accepted_event_ref
                or event.world_id != projection.world_id
                or event.event_type != "InteractionActTransitionAccepted"
                or event.event_id not in commit.event_ids
                or committed.event_type != event.event_type
                or committed.payload_hash != event.payload_hash
                or committed.logical_time != event.logical_time
                or committed.world_revision > commit.world_revision
                or commit.world_revision > cursor.world_revision
                or commit.deliberation_revision > cursor.deliberation_revision
                or commit.ledger_sequence > cursor.ledger_sequence
            ):
                raise ValueError(
                    "interaction act context authority mismatches pinned prefix"
                )
            try:
                accepted = InteractionActAcceptedPayload.model_validate_json(
                    event.payload_json,
                    strict=True,
                )
            except ValueError as exc:
                raise ValueError(
                    "interaction act context accepted payload is invalid"
                ) from exc
            projected_after = accepted.mutation.act_after.model_copy(
                update={
                    "origin_accepted_event_ref": current.origin_accepted_event_ref
                }
            )
            if (
                event.payload() != accepted.model_dump(mode="json")
                or accepted.world_id != projection.world_id
                or accepted.accepted_event_ref != event.event_id
                or accepted.mutation.transition_id != transition.transition_id
                or accepted.mutation.operation != transition.operation
                or accepted.mutation.source_ref != transition.source_ref
                or accepted.mutation.source_text_span != transition.source_text_span
                or accepted.mutation.act_after.updated_at != transition.accepted_at
                or projected_after != current
            ):
                raise ValueError(
                    "interaction act context head changed accepted mutation"
                )
            participant_statuses = [
                {
                    "actor_ref": status.actor_ref,
                    "status_code": status.status_code,
                    "source_ref": {
                        "authority_kind": status.source_ref.authority_kind,
                        "source_event_ref": status.source_ref.source_event_ref,
                        "source_actor_ref": status.source_ref.source_actor_ref,
                    },
                    "source_text_span": status.source_text_span,
                    "updated_at": status.updated_at.isoformat(),
                }
                for status in item.participant_statuses
            ]
            value: dict[str, object] = {
                "frame": {
                    "subject_ref": item.subject_ref,
                    "counterparty_refs": list(item.counterparty_refs),
                    "act_kind": item.act_kind,
                    "object": (
                        item.object_descriptor.model_dump(mode="json")
                        if item.object_descriptor is not None
                        else None
                    ),
                },
                "participant_statuses": participant_statuses,
                "external_outcome": item.external_outcome,
            }
            item_ref = f"interior:interaction-act:{item.interaction_act_ref}"
            context_item = {
                "item_ref": item_ref,
                "privacy_class": item.privacy_class,
                "value": value,
            }
            items.append(context_item)
            envelopes[item_ref] = {
                **context_item,
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
        return InteractionActContextJoin(
            items=tuple(items),
            source_envelopes=envelopes,
        )


def install_interaction_act_context(
    context: Mapping[str, object],
    join: InteractionActContextJoin,
) -> dict[str, object]:
    result = dict(context)
    slices = dict(context.get("slices") or {})
    if join.items:
        slices["interaction_acts"] = {
            "availability": "available",
            "items": list(join.items),
        }
    result["slices"] = slices
    return result


__all__ = [
    "InteractionActContextBuilder",
    "InteractionActContextJoin",
    "install_interaction_act_context",
]
