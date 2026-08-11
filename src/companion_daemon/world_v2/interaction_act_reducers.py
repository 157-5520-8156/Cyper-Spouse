"""Pure reducer for typed interaction-act continuity."""

from __future__ import annotations

from datetime import datetime

from .interaction_act_schemas import (
    InteractionActMutation,
    InteractionActProjection,
    InteractionActTransitionProjection,
)


def reduce_interaction_act(
    current: tuple[InteractionActProjection, ...],
    history: tuple[InteractionActTransitionProjection, ...],
    mutation: InteractionActMutation,
    *,
    logical_time: datetime,
    accepted_event_ref: str,
) -> tuple[
    tuple[InteractionActProjection, ...],
    tuple[InteractionActTransitionProjection, ...],
]:
    """Apply one authorized mutation with effect-once identity checks."""

    if not accepted_event_ref:
        raise ValueError("interaction act accepted event ref is required")
    after = mutation.act_after
    if after.external_outcome != "not_established":
        raise ValueError("interaction act text cannot establish an external outcome")
    if any(item.transition_id == mutation.transition_id for item in history):
        raise ValueError("interaction act transition identity already exists")
    if any(item.accepted_event_ref == accepted_event_ref for item in history):
        raise ValueError("interaction act accepted event was already consumed")
    authored_statuses = tuple(
        item
        for item in after.participant_statuses
        if item.actor_ref == mutation.source_ref.source_actor_ref
    )
    if len(authored_statuses) != 1:
        raise ValueError("interaction act mutation must bind its source actor status")
    authored_status = authored_statuses[0]
    before_status = None
    if mutation.operation == "declare":
        if after.origin_transition_id != mutation.transition_id:
            raise ValueError("interaction act origin transition does not match mutation")
        if after.opened_at != logical_time or after.updated_at != logical_time:
            raise ValueError("interaction act timestamps must use authoritative logical time")
        if after.source_refs != (mutation.source_ref,):
            raise ValueError("interaction act declaration must bind its exact source")
        if mutation.source_ref.source_actor_ref != after.subject_ref:
            raise ValueError("interaction act declaration requires its subject source")
        if after.participant_statuses != (authored_status,):
            raise ValueError(
                "interaction act declaration must create its subject status"
            )
        if any(item.interaction_act_id == after.interaction_act_id for item in current):
            raise ValueError("interaction act identity already exists")
        if after.origin_accepted_event_ref is not None:
            raise ValueError(
                "interaction act declaration cannot provide accepted event authority"
            )
        after = after.model_copy(
            update={"origin_accepted_event_ref": accepted_event_ref}
        )
        after = InteractionActProjection.model_validate(after.model_dump(mode="python"))
        updated = (*current, after)
    else:
        matches = tuple(
            (index, item)
            for index, item in enumerate(current)
            if item.interaction_act_id == after.interaction_act_id
        )
        if len(matches) != 1:
            raise ValueError("interaction act revision target does not resolve exactly once")
        index, before = matches[0]
        if before.entity_revision != mutation.expected_entity_revision:
            raise ValueError("interaction act entity revision compare-and-swap failed")
        if mutation.act_before != before:
            raise ValueError("interaction act before image does not match current entity")
        immutable_before = (
            before.interaction_act_id,
            before.conversation_ref,
            before.subject_ref,
            before.counterparty_refs,
            before.act_kind,
            before.object_descriptor,
            before.external_outcome,
            before.privacy_class,
            before.opened_at,
            before.origin_transition_id,
            before.origin_accepted_event_ref,
        )
        immutable_after = (
            after.interaction_act_id,
            after.conversation_ref,
            after.subject_ref,
            after.counterparty_refs,
            after.act_kind,
            after.object_descriptor,
            after.external_outcome,
            after.privacy_class,
            after.opened_at,
            after.origin_transition_id,
            after.origin_accepted_event_ref,
        )
        if immutable_after != immutable_before:
            raise ValueError("interaction act semantic coordinates are immutable")
        if after.entity_revision != before.entity_revision + 1:
            raise ValueError("interaction act revision must advance one revision")
        if after.updated_at != logical_time or logical_time < before.updated_at:
            raise ValueError("interaction act update time is not authoritative")
        if after.source_refs != (*before.source_refs, mutation.source_ref):
            raise ValueError("interaction act sources must append exact revision evidence")
        participant_refs = {before.subject_ref, *before.counterparty_refs}
        source_actor = mutation.source_ref.source_actor_ref
        if source_actor not in participant_refs:
            raise ValueError("interaction act revision requires a bound participant")
        before_statuses = {
            item.actor_ref: item for item in before.participant_statuses
        }
        after_statuses = {item.actor_ref: item for item in after.participant_statuses}
        if set(after_statuses) not in (
            set(before_statuses),
            {*before_statuses, source_actor},
        ) or any(
            after_statuses[actor_ref] != item
            for actor_ref, item in before_statuses.items()
            if actor_ref != source_actor
        ):
            raise ValueError("interaction act revision may only change its source actor")
        if after_statuses.get(source_actor) != authored_status:
            raise ValueError("interaction act revision status source is not exact")
        before_mark = before_statuses.get(source_actor)
        before_status = (
            before_mark.status_code if before_mark is not None else None
        )
        updated = tuple(
            after if position == index else item
            for position, item in enumerate(current)
        )
    transition = InteractionActTransitionProjection(
        transition_id=mutation.transition_id,
        interaction_act_id=after.interaction_act_id,
        entity_revision=after.entity_revision,
        operation=mutation.operation,
        performed_by_ref=mutation.source_ref.source_actor_ref,
        status_before=before_status,
        status_after=authored_status.status_code,
        source_ref=mutation.source_ref,
        source_text_span=mutation.source_text_span,
        accepted_event_ref=accepted_event_ref,
        accepted_at=logical_time,
    )
    return updated, (*history, transition)


__all__ = ["reduce_interaction_act"]
