"""Typed, source-bound continuity for role-authored interaction acts.

An interaction act records who performed a conversational move, which
counterparties it addresses, and its role-authored kind.  It is not an
external ``Action`` and never establishes that a represented World outcome
occurred.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
import unicodedata

from pydantic import Field, field_validator, model_validator

from .schema_core import FrozenModel, PrivacyClass


InteractionActOperation = Literal["declare", "revise"]
InteractionActSourceKind = Literal["observed_message", "delivered_expression"]


def _normalize_role_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


class DeliveredExpressionProof(FrozenModel):
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


class InteractionActSourceRef(FrozenModel):
    authority_kind: InteractionActSourceKind
    source_event_ref: str = Field(min_length=1)
    source_world_revision: int = Field(ge=1)
    source_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_actor_ref: str = Field(min_length=1)
    delivery_proof: DeliveredExpressionProof | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def delivery_proof_matches_source_kind(self) -> InteractionActSourceRef:
        if (self.authority_kind == "delivered_expression") != (
            self.delivery_proof is not None
        ):
            raise ValueError("interaction act delivery proof does not match source kind")
        if self.delivery_proof is not None and (
            self.delivery_proof.stored_payload_event_ref != self.source_event_ref
            or self.delivery_proof.stored_payload_event_payload_hash
            != self.source_payload_hash
        ):
            raise ValueError("interaction act stored payload proof must be its source")
        return self


class InteractionActParticipantStatus(FrozenModel):
    """Latest source-bound status authored by one participant."""

    actor_ref: str = Field(min_length=1)
    status_code: str = Field(min_length=1, max_length=128)
    source_ref: InteractionActSourceRef
    source_text_span: str = Field(min_length=1, max_length=4_096)
    updated_at: datetime

    @field_validator("status_code", mode="before")
    @classmethod
    def normalize_status_code(cls, value: object) -> object:
        return _normalize_role_text(value) if isinstance(value, str) else value

    @field_validator("source_text_span", mode="before")
    @classmethod
    def normalize_source_text_span(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFC", value)
        if not normalized.strip():
            raise ValueError(
                "interaction act source text span must contain visible text"
            )
        return normalized

    @model_validator(mode="after")
    def source_actor_is_status_actor(self) -> InteractionActParticipantStatus:
        if self.source_ref.source_actor_ref != self.actor_ref:
            raise ValueError("interaction act status actor must match its source actor")
        return self


class InteractionActObjectDescriptor(FrozenModel):
    """Conversation-scoped described object; never a committed World entity."""

    object_ref: str = Field(pattern=r"^interaction-object:sha256:[0-9a-f]{64}$")
    object_label: str = Field(min_length=1, max_length=512)
    epistemic_scope: Literal["report_only"] = "report_only"

    @field_validator("object_label", mode="before")
    @classmethod
    def normalize_object_label(cls, value: object) -> object:
        return _normalize_role_text(value) if isinstance(value, str) else value


class InteractionActProjection(FrozenModel):
    interaction_act_id: str = Field(pattern=r"^interaction-act:sha256:[0-9a-f]{64}$")
    entity_revision: int = Field(ge=1)
    conversation_ref: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    counterparty_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    act_kind: str = Field(min_length=1, max_length=128)
    object_descriptor: InteractionActObjectDescriptor | None = None
    participant_statuses: tuple[InteractionActParticipantStatus, ...] = Field(
        min_length=1,
        max_length=9,
    )
    external_outcome: Literal["not_established"] = "not_established"
    source_refs: tuple[InteractionActSourceRef, ...] = Field(min_length=1, max_length=32)
    privacy_class: PrivacyClass = "private"
    opened_at: datetime
    updated_at: datetime
    origin_transition_id: str = Field(min_length=1)
    # Pre-acceptance mutations leave this unset.  The reducer installs the
    # committed mutation event ref supplied by the ledger handler.
    origin_accepted_event_ref: str | None = Field(
        default=None,
        min_length=1,
        exclude_if=lambda value: value is None,
    )

    @field_validator("act_kind", mode="before")
    @classmethod
    def normalize_act_kind(cls, value: object) -> object:
        return _normalize_role_text(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def participants_and_sources_are_stable(self) -> InteractionActProjection:
        if len(self.counterparty_refs) != len(set(self.counterparty_refs)):
            raise ValueError("interaction act counterparties must be unique")
        if self.subject_ref in self.counterparty_refs:
            raise ValueError("interaction act subject cannot be its own counterparty")
        participant_refs = {self.subject_ref, *self.counterparty_refs}
        status_actor_refs = tuple(item.actor_ref for item in self.participant_statuses)
        if len(status_actor_refs) != len(set(status_actor_refs)):
            raise ValueError("interaction act participant statuses must be unique")
        if self.subject_ref not in status_actor_refs:
            raise ValueError("interaction act subject status is required")
        if any(item not in participant_refs for item in status_actor_refs):
            raise ValueError("interaction act status actor is not a participant")
        source_ids = tuple(item.source_event_ref for item in self.source_refs)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("interaction act source events must be unique")
        if self.updated_at < self.opened_at:
            raise ValueError("interaction act update cannot precede its opening")
        if any(
            item.source_ref not in self.source_refs
            or item.updated_at < self.opened_at
            or item.updated_at > self.updated_at
            for item in self.participant_statuses
        ):
            raise ValueError("interaction act participant status is not source-bound")
        return self


class InteractionActRoleOutput(FrozenModel):
    """One semantic choice authored by the role model, not by host text rules."""

    contract: Literal["interaction-act-role-output.2"]
    source_text_span: str = Field(min_length=1, max_length=4_096)
    operation: InteractionActOperation
    status_code: str = Field(min_length=1, max_length=128)
    interaction_act_ref: str | None = Field(default=None, min_length=1)
    act_kind: str = Field(min_length=1, max_length=128)
    subject_ref: str = Field(min_length=1)
    counterparty_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    object_ref: str | None = Field(
        default=None,
        pattern=r"^interaction-object:sha256:[0-9a-f]{64}$",
    )
    object_label: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("act_kind", "object_label", "status_code", mode="before")
    @classmethod
    def normalize_authored_text(cls, value: object) -> object:
        return _normalize_role_text(value) if isinstance(value, str) else value

    @field_validator("source_text_span", mode="before")
    @classmethod
    def normalize_source_text_span(cls, value: object) -> object:
        # Preserve the selected span boundary.  Only Unicode representation is
        # normalized; unlike authored labels/kinds, whitespace is not trimmed.
        if not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFC", value)
        if not normalized.strip():
            raise ValueError("interaction act source text span must contain visible text")
        return normalized

    @model_validator(mode="after")
    def declared_operation_is_exact(self) -> InteractionActRoleOutput:
        if len(self.counterparty_refs) != len(set(self.counterparty_refs)):
            raise ValueError("interaction act counterparties must be unique")
        if self.subject_ref in self.counterparty_refs:
            raise ValueError("interaction act subject cannot be its own counterparty")
        if self.operation == "declare":
            if self.interaction_act_ref is not None or self.object_ref is not None:
                raise ValueError("interaction act declaration cannot invent authority refs")
        elif self.interaction_act_ref is None or self.object_label is not None:
            raise ValueError("interaction act revision must select existing authority")
        return self


class InteractionActMutation(FrozenModel):
    transition_id: str = Field(pattern=r"^interaction-act-transition:sha256:[0-9a-f]{64}$")
    operation: InteractionActOperation
    expected_entity_revision: int = Field(ge=0)
    act_before: InteractionActProjection | None
    act_after: InteractionActProjection
    source_ref: InteractionActSourceRef
    source_text_span: str = Field(min_length=1, max_length=4_096)
    role_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def images_match_operation(self) -> InteractionActMutation:
        source_actor = self.source_ref.source_actor_ref
        authored_statuses = tuple(
            item
            for item in self.act_after.participant_statuses
            if item.actor_ref == source_actor
        )
        if len(authored_statuses) != 1:
            raise ValueError("interaction act mutation must bind its source actor status")
        authored_status = authored_statuses[0]
        if (
            authored_status.source_ref != self.source_ref
            or authored_status.source_text_span != self.source_text_span
            or authored_status.updated_at != self.act_after.updated_at
        ):
            raise ValueError("interaction act mutation status source is not exact")
        if self.operation == "declare":
            if self.act_before is not None or self.expected_entity_revision != 0:
                raise ValueError("interaction act declaration must create from revision zero")
            if self.act_after.entity_revision != 1:
                raise ValueError("interaction act declaration must create revision one")
            if self.act_after.origin_transition_id != self.transition_id:
                raise ValueError(
                    "interaction act origin transition does not match mutation"
                )
            if (
                source_actor != self.act_after.subject_ref
                or self.act_after.participant_statuses != (authored_status,)
                or self.act_after.source_refs != (self.source_ref,)
            ):
                raise ValueError(
                    "interaction act declaration must create its subject status"
                )
        else:
            if self.act_before is None or self.expected_entity_revision < 1:
                raise ValueError("interaction act revision requires a before image")
            if self.act_after.interaction_act_id != self.act_before.interaction_act_id:
                raise ValueError("interaction act revision cannot change identity")
            if self.act_after.entity_revision != self.expected_entity_revision + 1:
                raise ValueError("interaction act revision must advance one revision")
            immutable_before = (
                self.act_before.conversation_ref,
                self.act_before.subject_ref,
                self.act_before.counterparty_refs,
                self.act_before.act_kind,
                self.act_before.object_descriptor,
                self.act_before.external_outcome,
                self.act_before.privacy_class,
                self.act_before.opened_at,
                self.act_before.origin_transition_id,
                self.act_before.origin_accepted_event_ref,
            )
            immutable_after = (
                self.act_after.conversation_ref,
                self.act_after.subject_ref,
                self.act_after.counterparty_refs,
                self.act_after.act_kind,
                self.act_after.object_descriptor,
                self.act_after.external_outcome,
                self.act_after.privacy_class,
                self.act_after.opened_at,
                self.act_after.origin_transition_id,
                self.act_after.origin_accepted_event_ref,
            )
            if immutable_after != immutable_before:
                raise ValueError("interaction act semantic coordinates are immutable")
        return self


class InteractionActTransitionProjection(FrozenModel):
    transition_id: str = Field(pattern=r"^interaction-act-transition:sha256:[0-9a-f]{64}$")
    interaction_act_id: str = Field(pattern=r"^interaction-act:sha256:[0-9a-f]{64}$")
    entity_revision: int = Field(ge=1)
    operation: InteractionActOperation
    performed_by_ref: str = Field(min_length=1)
    status_before: str | None = Field(default=None, min_length=1, max_length=128)
    status_after: str = Field(min_length=1, max_length=128)
    source_ref: InteractionActSourceRef
    source_text_span: str = Field(min_length=1, max_length=4_096)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime

    @field_validator("status_before", "status_after", mode="before")
    @classmethod
    def normalize_status_codes(cls, value: object) -> object:
        return _normalize_role_text(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def performed_actor_matches_source(self) -> InteractionActTransitionProjection:
        if self.performed_by_ref != self.source_ref.source_actor_ref:
            raise ValueError("interaction act history actor must match its source actor")
        return self


class InteractionActContextItem(FrozenModel):
    """Compact public query contract for CharacterInterior continuity."""

    interaction_act_ref: str = Field(pattern=r"^interaction-act:sha256:[0-9a-f]{64}$")
    conversation_ref: str = Field(min_length=1)
    act_kind: str = Field(min_length=1, max_length=128)
    subject_ref: str = Field(min_length=1)
    counterparty_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    object_descriptor: InteractionActObjectDescriptor | None = None
    participant_statuses: tuple[InteractionActParticipantStatus, ...] = Field(
        min_length=1,
        max_length=9,
    )
    external_outcome: Literal["not_established"] = "not_established"
    source_refs: tuple[InteractionActSourceRef, ...] = Field(min_length=1, max_length=32)
    source_text_span: str = Field(min_length=1, max_length=4_096)
    accepted_event_ref: str = Field(min_length=1)
    privacy_class: PrivacyClass
    updated_at: datetime


__all__ = [
    "DeliveredExpressionProof",
    "InteractionActContextItem",
    "InteractionActMutation",
    "InteractionActObjectDescriptor",
    "InteractionActOperation",
    "InteractionActParticipantStatus",
    "InteractionActProjection",
    "InteractionActRoleOutput",
    "InteractionActSourceKind",
    "InteractionActSourceRef",
    "InteractionActTransitionProjection",
]
