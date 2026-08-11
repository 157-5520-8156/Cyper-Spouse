"""Materialize typed role output into source-bound interaction-act mutations.

This module contains no dialogue classifier.  It only checks that exact text
spans selected by the role occur unambiguously in verified source bytes, then
binds the structured output to those source coordinates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .interaction_act_identity import (
    interaction_act_conversation_ref,
    interaction_act_id,
    interaction_act_object_ref,
    interaction_act_overlapping_occurrence_count,
    interaction_act_role_output_hash,
    interaction_act_transition_id,
    normalize_interaction_act_source_text,
)
from .interaction_act_schemas import (
    DeliveredExpressionProof,
    InteractionActContextItem,
    InteractionActMutation,
    InteractionActObjectDescriptor,
    InteractionActParticipantStatus,
    InteractionActProjection,
    InteractionActRoleOutput,
    InteractionActSourceRef,
    InteractionActTransitionProjection,
)
from .schema_core import FrozenModel


class ObservedInteractionActSource(FrozenModel):
    world_id: str = Field(min_length=1)
    conversation_ref: str = Field(min_length=1)
    source_event_ref: str = Field(min_length=1)
    source_world_revision: int = Field(ge=1)
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_actor_ref: str = Field(min_length=1)
    source_text: str = Field(min_length=1, max_length=12_000, exclude=True)

    def as_source_ref(self) -> InteractionActSourceRef:
        return InteractionActSourceRef(
            authority_kind="observed_message",
            source_event_ref=self.source_event_ref,
            source_world_revision=self.source_world_revision,
            source_payload_hash=self.source_payload_hash,
            source_actor_ref=self.source_actor_ref,
        )


class DeliveredExpressionInteractionActSource(FrozenModel):
    """A companion expression joined to its exact delivered receipt."""

    world_id: str = Field(min_length=1)
    conversation_ref: str = Field(min_length=1)
    source_event_ref: str = Field(min_length=1)
    source_world_revision: int = Field(ge=1)
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_actor_ref: str = Field(min_length=1)
    source_text: str = Field(min_length=1, max_length=4_096, exclude=True)
    expression_plan_id: str = Field(min_length=1)
    expression_plan_event_ref: str = Field(min_length=1)
    expression_plan_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expression_beat_id: str = Field(min_length=1)
    expression_beat_event_ref: str = Field(min_length=1)
    expression_beat_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    stored_payload_event_ref: str = Field(min_length=1)
    stored_payload_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_id: str = Field(min_length=1)
    action_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action_target_ref: str = Field(min_length=1)
    action_event_ref: str = Field(min_length=1)
    action_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_id: str = Field(min_length=1)
    receipt_event_ref: str = Field(min_length=1)
    receipt_world_revision: int = Field(ge=1)
    receipt_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_status: Literal["delivered"]

    @model_validator(mode="after")
    def receipt_is_exact_delivered_proof(self) -> DeliveredExpressionInteractionActSource:
        if self.receipt_status != "delivered":
            raise ValueError("interaction act expression source requires delivered receipt")
        if self.receipt_world_revision < self.source_world_revision:
            raise ValueError("interaction act receipt cannot precede its expression source")
        if self.receipt_event_ref == self.source_event_ref:
            raise ValueError("interaction act receipt must be distinct from expression source")
        if (
            self.stored_payload_event_ref != self.source_event_ref
            or self.stored_payload_event_payload_hash != self.source_payload_hash
        ):
            raise ValueError("interaction act stored payload proof must be its source")
        return self

    def as_source_ref(self) -> InteractionActSourceRef:
        return InteractionActSourceRef(
            authority_kind="delivered_expression",
            source_event_ref=self.source_event_ref,
            source_world_revision=self.source_world_revision,
            source_payload_hash=self.source_payload_hash,
            source_actor_ref=self.source_actor_ref,
            delivery_proof=DeliveredExpressionProof(
                expression_plan_id=self.expression_plan_id,
                expression_plan_event_ref=self.expression_plan_event_ref,
                expression_plan_event_payload_hash=(
                    self.expression_plan_event_payload_hash
                ),
                expression_beat_id=self.expression_beat_id,
                expression_beat_event_ref=self.expression_beat_event_ref,
                expression_beat_event_payload_hash=(
                    self.expression_beat_event_payload_hash
                ),
                stored_payload_event_ref=self.stored_payload_event_ref,
                stored_payload_event_payload_hash=(
                    self.stored_payload_event_payload_hash
                ),
                action_id=self.action_id,
                action_payload_hash=self.action_payload_hash,
                action_target_ref=self.action_target_ref,
                action_event_ref=self.action_event_ref,
                action_event_payload_hash=self.action_event_payload_hash,
                receipt_id=self.receipt_id,
                receipt_event_ref=self.receipt_event_ref,
                receipt_world_revision=self.receipt_world_revision,
                receipt_payload_hash=self.receipt_payload_hash,
                receipt_status="delivered",
            ),
        )


InteractionActMaterializationSource = (
    ObservedInteractionActSource | DeliveredExpressionInteractionActSource
)


def interaction_act_context_items(
    current: tuple[InteractionActProjection, ...],
    *,
    history: tuple[InteractionActTransitionProjection, ...],
    conversation_ref: str,
    participant_ref: str,
    limit: int = 8,
) -> tuple[InteractionActContextItem, ...]:
    """Return recent exact coordinates without deriving a semantic outcome."""

    if not conversation_ref or not participant_ref or limit <= 0:
        raise ValueError("interaction act context query is incomplete")
    matches = sorted(
        (
            item
            for item in current
            if item.conversation_ref == conversation_ref
            and (
                item.subject_ref == participant_ref
                or participant_ref in item.counterparty_refs
            )
        ),
        key=lambda item: (item.updated_at, item.interaction_act_id),
        reverse=True,
    )[:limit]
    result = []
    for item in matches:
        transition_chain = tuple(
            transition
            for transition in history
            if transition.interaction_act_id == item.interaction_act_id
        )
        transition_chain = tuple(
            sorted(transition_chain, key=lambda transition: transition.entity_revision)
        )
        expected_revisions = tuple(range(1, item.entity_revision + 1))
        actual_revisions = tuple(
            transition.entity_revision for transition in transition_chain
        )
        participant_refs = {item.subject_ref, *item.counterparty_refs}
        latest_status_by_actor: dict[str, InteractionActParticipantStatus] = {}
        status_actor_order: list[str] = []
        chain_is_exact = True
        previous_accepted_at: datetime | None = None
        for index, transition in enumerate(transition_chain):
            actor_ref = transition.performed_by_ref
            previous_status = latest_status_by_actor.get(actor_ref)
            expected_before = (
                previous_status.status_code if previous_status is not None else None
            )
            expected_operation = "declare" if index == 0 else "revise"
            if (
                transition.operation != expected_operation
                or actor_ref not in participant_refs
                or (index == 0 and actor_ref != item.subject_ref)
                or transition.status_before != expected_before
                or transition.source_ref != item.source_refs[index]
                or actor_ref != transition.source_ref.source_actor_ref
                or (
                    previous_accepted_at is not None
                    and transition.accepted_at < previous_accepted_at
                )
            ):
                chain_is_exact = False
                break
            if actor_ref not in latest_status_by_actor:
                status_actor_order.append(actor_ref)
            latest_status_by_actor[actor_ref] = InteractionActParticipantStatus(
                actor_ref=actor_ref,
                status_code=transition.status_after,
                source_ref=transition.source_ref,
                source_text_span=transition.source_text_span,
                updated_at=transition.accepted_at,
            )
            previous_accepted_at = transition.accepted_at
        rebuilt_statuses = tuple(
            latest_status_by_actor[actor_ref] for actor_ref in status_actor_order
        )
        if (
            len(item.source_refs) != item.entity_revision
            or actual_revisions != expected_revisions
            or not chain_is_exact
            or rebuilt_statuses != item.participant_statuses
            or transition_chain[0].transition_id != item.origin_transition_id
            or transition_chain[0].accepted_event_ref
            != item.origin_accepted_event_ref
            or transition_chain[0].status_before is not None
            or transition_chain[0].accepted_at != item.opened_at
            or transition_chain[-1].accepted_at != item.updated_at
            or len(
                {transition.accepted_event_ref for transition in transition_chain}
            )
            != len(transition_chain)
        ):
            raise ValueError("interaction act context transition chain is not exact")
        latest_transition = transition_chain[-1]
        result.append(
            InteractionActContextItem(
                interaction_act_ref=item.interaction_act_id,
                conversation_ref=item.conversation_ref,
                act_kind=item.act_kind,
                subject_ref=item.subject_ref,
                counterparty_refs=item.counterparty_refs,
                object_descriptor=item.object_descriptor,
                participant_statuses=item.participant_statuses,
                external_outcome=item.external_outcome,
                source_refs=item.source_refs,
                source_text_span=latest_transition.source_text_span,
                accepted_event_ref=latest_transition.accepted_event_ref,
                privacy_class=item.privacy_class,
                updated_at=item.updated_at,
            )
        )
    return tuple(result)


def _object_descriptor(
    *, authored: InteractionActRoleOutput, source: InteractionActMaterializationSource
) -> InteractionActObjectDescriptor | None:
    if authored.object_label is None:
        return None
    source_text = normalize_interaction_act_source_text(source.source_text)
    occurrences = interaction_act_overlapping_occurrence_count(
        source_text=source_text,
        selected_text=authored.object_label,
    )
    if occurrences == 0:
        raise ValueError("interaction act object label is not bound to exact source text")
    if occurrences != 1:
        raise ValueError("interaction act object label is not exact-once in source text")
    return InteractionActObjectDescriptor(
        object_ref=interaction_act_object_ref(
            conversation_ref=source.conversation_ref,
            object_label=authored.object_label,
            opening_source_event_ref=source.source_event_ref,
            opening_source_payload_hash=source.source_payload_hash,
        ),
        object_label=authored.object_label,
    )


def materialize_interaction_act_mutation(
    *,
    authored: InteractionActRoleOutput,
    source: InteractionActMaterializationSource,
    current: tuple[InteractionActProjection, ...],
    logical_time: datetime,
) -> InteractionActMutation:
    """Bind one already-authored semantic choice to exact source authority."""

    normalized_source_text = normalize_interaction_act_source_text(source.source_text)
    if (
        interaction_act_overlapping_occurrence_count(
            source_text=normalized_source_text,
            selected_text=authored.source_text_span,
        )
        != 1
    ):
        raise ValueError("interaction act source text span is not exact-once")
    source_ref = source.as_source_ref()
    role_output_hash = interaction_act_role_output_hash(authored)
    if authored.operation == "revise":
        return _materialize_existing_transition(
            authored=authored,
            source=source,
            source_ref=source_ref,
            role_output_hash=role_output_hash,
            current=current,
            logical_time=logical_time,
        )
    if authored.subject_ref != source.source_actor_ref:
        raise ValueError("interaction act declaration subject does not match source actor")
    object_descriptor = _object_descriptor(authored=authored, source=source)
    act_id = interaction_act_id(
        world_id=source.world_id,
        conversation_ref=source.conversation_ref,
        source_ref=source_ref,
        role_output_hash=role_output_hash,
    )
    transition_id = interaction_act_transition_id(
        interaction_act_ref=act_id,
        operation=authored.operation,
        source_ref=source_ref,
        role_output_hash=role_output_hash,
    )
    participant_status = InteractionActParticipantStatus(
        actor_ref=authored.subject_ref,
        status_code=authored.status_code,
        source_ref=source_ref,
        source_text_span=authored.source_text_span,
        updated_at=logical_time,
    )
    projected = InteractionActProjection(
        interaction_act_id=act_id,
        entity_revision=1,
        conversation_ref=source.conversation_ref,
        subject_ref=authored.subject_ref,
        counterparty_refs=authored.counterparty_refs,
        act_kind=authored.act_kind,
        object_descriptor=object_descriptor,
        participant_statuses=(participant_status,),
        source_refs=(source_ref,),
        opened_at=logical_time,
        updated_at=logical_time,
        origin_transition_id=transition_id,
    )
    return InteractionActMutation(
        transition_id=projected.origin_transition_id,
        operation=authored.operation,
        expected_entity_revision=0,
        act_before=None,
        act_after=projected,
        source_ref=source_ref,
        source_text_span=authored.source_text_span,
        role_output_hash=role_output_hash,
    )


def _materialize_existing_transition(
    *,
    authored: InteractionActRoleOutput,
    source: InteractionActMaterializationSource,
    source_ref: InteractionActSourceRef,
    role_output_hash: str,
    current: tuple[InteractionActProjection, ...],
    logical_time: datetime,
) -> InteractionActMutation:
    matches = tuple(
        item
        for item in current
        if item.interaction_act_id == authored.interaction_act_ref
    )
    if len(matches) != 1:
        raise ValueError("interaction act transition target does not resolve exactly once")
    before = matches[0]
    expected_object_ref = (
        before.object_descriptor.object_ref
        if before.object_descriptor is not None
        else None
    )
    authored_coordinates = (
        authored.act_kind,
        authored.subject_ref,
        authored.counterparty_refs,
        authored.object_ref,
    )
    projected_coordinates = (
        before.act_kind,
        before.subject_ref,
        before.counterparty_refs,
        expected_object_ref,
    )
    if authored_coordinates != projected_coordinates:
        raise ValueError("interaction act transition changed semantic coordinates")
    if source.conversation_ref != before.conversation_ref:
        raise ValueError("interaction act transition changed conversation")
    if source.source_event_ref in {
        item.source_event_ref for item in before.source_refs
    }:
        raise ValueError("interaction act source event was already consumed")
    participant_refs = {before.subject_ref, *before.counterparty_refs}
    if source.source_actor_ref not in participant_refs:
        raise ValueError("interaction act revision requires a bound participant")
    if logical_time < before.updated_at:
        raise ValueError("interaction act revision precedes current state")
    transition_id = interaction_act_transition_id(
        interaction_act_ref=before.interaction_act_id,
        operation=authored.operation,
        source_ref=source_ref,
        role_output_hash=role_output_hash,
    )
    authored_status = InteractionActParticipantStatus(
        actor_ref=source.source_actor_ref,
        status_code=authored.status_code,
        source_ref=source_ref,
        source_text_span=authored.source_text_span,
        updated_at=logical_time,
    )
    replaced = False
    participant_statuses = []
    for item in before.participant_statuses:
        if item.actor_ref == authored_status.actor_ref:
            participant_statuses.append(authored_status)
            replaced = True
        else:
            participant_statuses.append(item)
    if not replaced:
        participant_statuses.append(authored_status)
    after = before.model_copy(
        update={
            "entity_revision": before.entity_revision + 1,
            "participant_statuses": tuple(participant_statuses),
            "source_refs": (*before.source_refs, source_ref),
            "updated_at": logical_time,
        }
    )
    after = InteractionActProjection.model_validate(after.model_dump(mode="python"))
    return InteractionActMutation(
        transition_id=transition_id,
        operation=authored.operation,
        expected_entity_revision=before.entity_revision,
        act_before=before,
        act_after=after,
        source_ref=source_ref,
        source_text_span=authored.source_text_span,
        role_output_hash=role_output_hash,
    )


__all__ = [
    "DeliveredExpressionInteractionActSource",
    "InteractionActRoleOutput",
    "ObservedInteractionActSource",
    "interaction_act_conversation_ref",
    "interaction_act_context_items",
    "materialize_interaction_act_mutation",
]
