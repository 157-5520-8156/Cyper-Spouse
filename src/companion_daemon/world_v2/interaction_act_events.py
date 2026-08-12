"""Typed proposal and accepted-effect payloads for interaction-act authority."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .interaction_act_identity import (
    interaction_act_overlapping_occurrence_count,
    normalize_interaction_act_source_text,
)
from .interaction_act_schemas import InteractionActMutation
from .proposal_envelope import TypedChange
from .schema_core import FrozenModel, canonicalize_json_value
from .schemas import WorldEvent


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_interaction_act_mutation_hash(
    mutation: InteractionActMutation | dict[str, object],
) -> str:
    material = (
        mutation.model_dump(mode="json")
        if isinstance(mutation, InteractionActMutation)
        else dict(mutation)
    )
    return _canonical_hash(material)


def canonical_interaction_act_change_hash(
    change: TypedChange | dict[str, object],
) -> str:
    """Bind the full generic change envelope, not only its semantic payload."""

    material = (
        change.model_dump(mode="json")
        if isinstance(change, TypedChange)
        else dict(change)
    )
    return _canonical_hash(material)


def interaction_act_accepted_event_id(
    *,
    world_id: str,
    proposal_id: str,
    change_id: str,
    mutation_payload_hash: str,
) -> str:
    if not world_id or not proposal_id or not change_id:
        raise ValueError("interaction act accepted event identity is incomplete")
    digest = _canonical_hash(
        {
            "contract": "interaction-act-accepted-event-id.1",
            "world_id": world_id,
            "proposal_id": proposal_id,
            "change_id": change_id,
            "mutation_payload_hash": mutation_payload_hash,
        }
    )
    return f"event:interaction-act-transition-accepted:{digest}"


def interaction_act_typed_proposal_id(
    *,
    source_audit_event_ref: str,
    source_audit_event_payload_hash: str,
    source_change_id: str,
    evaluated_world_revision: int,
    mutation_payload_hash: str,
) -> str:
    if (
        not source_audit_event_ref
        or not source_change_id
        or evaluated_world_revision < 0
    ):
        raise ValueError("interaction act typed proposal identity is incomplete")
    digest = _canonical_hash(
        {
            "contract": "interaction-act-typed-proposal-id.1",
            "source_audit_event_ref": source_audit_event_ref,
            "source_audit_event_payload_hash": source_audit_event_payload_hash,
            "source_change_id": source_change_id,
            "evaluated_world_revision": evaluated_world_revision,
            "mutation_payload_hash": mutation_payload_hash,
        }
    )
    return f"proposal:interaction-act-compiled:{digest}"


class InteractionActProposalRecordedPayload(FrozenModel):
    """Compiled proposal authority before any interaction-act World mutation."""

    contract: Literal["interaction-act-proposal.1"]
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    mutation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutation: InteractionActMutation
    observed_source_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=12_000,
        exclude_if=lambda value: value is None,
    )

    @field_validator("observed_source_text", mode="before")
    @classmethod
    def normalize_observed_source_text(cls, value: object) -> object:
        return (
            normalize_interaction_act_source_text(value)
            if isinstance(value, str)
            else value
        )

    @model_validator(mode="after")
    def mutation_hash_is_exact(self) -> InteractionActProposalRecordedPayload:
        if self.mutation_payload_hash != canonical_interaction_act_mutation_hash(
            self.mutation
        ):
            raise ValueError("interaction act proposal mutation hash is invalid")
        if self.mutation.act_after.external_outcome != "not_established":
            raise ValueError("interaction act proposal cannot establish external outcome")
        is_observed = self.mutation.source_ref.authority_kind == "observed_message"
        if is_observed != (self.observed_source_text is not None):
            raise ValueError(
                "interaction act observed source text does not match source scope"
            )
        if self.observed_source_text is not None:
            if (
                interaction_act_overlapping_occurrence_count(
                    source_text=self.observed_source_text,
                    selected_text=self.mutation.source_text_span,
                )
                != 1
            ):
                raise ValueError(
                    "interaction act observed source span is not exact-once"
                )
            descriptor = self.mutation.act_after.object_descriptor
            if (
                self.mutation.operation == "declare"
                and descriptor is not None
                and interaction_act_overlapping_occurrence_count(
                    source_text=self.observed_source_text,
                    selected_text=descriptor.object_label,
                )
                != 1
            ):
                raise ValueError(
                    "interaction act observed object label is not exact-once"
                )
        return self


class InteractionActAcceptedPayload(FrozenModel):
    """Accepted effect payload binding audit, proposal, change, and mutation."""

    contract: Literal["interaction-act-accepted.1"] = "interaction-act-accepted.1"
    world_id: str = Field(min_length=1, max_length=256)
    acceptance_id: str = Field(min_length=1, max_length=256)
    accepted_event_ref: str = Field(
        pattern=r"^event:interaction-act-transition-accepted:[0-9a-f]{64}$"
    )
    source_proposal_event_ref: str = Field(min_length=1, max_length=512)
    source_proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    mutation_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutation: InteractionActMutation

    @model_validator(mode="after")
    def accepted_mutation_is_exact(self) -> InteractionActAcceptedPayload:
        if self.mutation_payload_hash != canonical_interaction_act_mutation_hash(
            self.mutation
        ):
            raise ValueError("interaction act accepted mutation hash is invalid")
        if self.mutation.act_after.external_outcome != "not_established":
            raise ValueError("interaction act acceptance cannot establish external outcome")
        expected_event_ref = interaction_act_accepted_event_id(
            world_id=self.world_id,
            proposal_id=self.proposal_id,
            change_id=self.change_id,
            mutation_payload_hash=self.mutation_payload_hash,
        )
        if self.accepted_event_ref != expected_event_ref:
            raise ValueError("interaction act accepted event identity is invalid")
        return self


def canonical_interaction_act_accepted_payload_hash(
    payload: InteractionActAcceptedPayload | dict[str, object],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, InteractionActAcceptedPayload)
        else dict(payload)
    )
    return _canonical_hash(material)


def build_interaction_act_accepted_payload(
    *,
    acceptance_id: str,
    source_proposal_event: WorldEvent,
) -> InteractionActAcceptedPayload:
    if source_proposal_event.event_type != "InteractionActProposalRecorded":
        raise ValueError("interaction act acceptance source is not a typed proposal")
    proposal = InteractionActProposalRecordedPayload.model_validate_json(
        source_proposal_event.payload_json,
        strict=True,
    )
    if source_proposal_event.payload() != proposal.model_dump(mode="json"):
        raise ValueError("interaction act proposal event payload is not canonical")
    return InteractionActAcceptedPayload(
        world_id=source_proposal_event.world_id,
        acceptance_id=acceptance_id,
        accepted_event_ref=interaction_act_accepted_event_id(
            world_id=source_proposal_event.world_id,
            proposal_id=proposal.proposal_id,
            change_id=proposal.change_id,
            mutation_payload_hash=proposal.mutation_payload_hash,
        ),
        source_proposal_event_ref=source_proposal_event.event_id,
        source_proposal_event_payload_hash=source_proposal_event.payload_hash,
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal.proposal_hash,
        change_id=proposal.change_id,
        accepted_change_hash=proposal.accepted_change_hash,
        evaluated_world_revision=proposal.evaluated_world_revision,
        mutation_payload_hash=proposal.mutation_payload_hash,
        mutation=proposal.mutation,
    )


__all__ = [
    "InteractionActAcceptedPayload",
    "InteractionActProposalRecordedPayload",
    "build_interaction_act_accepted_payload",
    "canonical_interaction_act_change_hash",
    "canonical_interaction_act_accepted_payload_hash",
    "canonical_interaction_act_mutation_hash",
    "interaction_act_accepted_event_id",
    "interaction_act_typed_proposal_id",
]
