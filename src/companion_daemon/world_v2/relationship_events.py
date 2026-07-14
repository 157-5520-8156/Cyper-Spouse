"""Typed, behavior-neutral relationship authority events."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import (
    BoundaryProjection,
    EvidenceRef,
    FrozenModel,
    RelationshipSignalProjection,
    RelationshipHysteresisProjection,
    RelationshipStage,
    RelationshipVariableDeltas,
    RelationshipVariablesProjection,
)


class RelationshipAuthorizedMutationPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def authority_inputs_are_unique(self) -> RelationshipAuthorizedMutationPayload:
        if len(self.policy_refs) != len(set(self.policy_refs)):
            raise ValueError("relationship policy refs must be unique")
        if len(self.evidence_refs) != len({item.ref_id for item in self.evidence_refs}):
            raise ValueError("relationship evidence refs must be unique")
        return self


class RelationshipSignalAcceptedPayload(RelationshipAuthorizedMutationPayload):
    signal: RelationshipSignalProjection

    @model_validator(mode="after")
    def signal_matches_authority(self) -> RelationshipSignalAcceptedPayload:
        _validate_hash(self)
        if self.expected_entity_revision != 0 or self.signal.entity_revision != 1:
            raise ValueError("relationship signal must create revision one")
        if self.signal.evidence_refs != self.evidence_refs:
            raise ValueError("relationship signal evidence does not match payload")
        if (
            self.signal.origin.change_id != self.change_id
            or self.signal.origin.transition_id != self.transition_id
            or self.signal.origin.policy_refs != self.policy_refs
        ):
            raise ValueError("relationship signal origin does not match payload")
        return self


class RelationshipSlowVariableAdjustedPayload(RelationshipAuthorizedMutationPayload):
    relationship_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    adjustment_id: str = Field(min_length=1)
    operation: Literal["adjust", "compensate"]
    signal_refs: tuple[str, ...] = Field(min_length=1)
    proposed_deltas: RelationshipVariableDeltas
    accepted_deltas: RelationshipVariableDeltas
    variables_before: RelationshipVariablesProjection
    variables_after: RelationshipVariablesProjection
    stage_before: RelationshipStage
    stage_after: RelationshipStage
    hysteresis_before: RelationshipHysteresisProjection
    hysteresis_after: RelationshipHysteresisProjection
    commitment_refs: tuple[str, ...] = ()
    confidence_bp: int = Field(ge=1, le=10_000)
    persistence: Literal["session", "durable"]
    contradiction_group_ref: str | None = None
    rationale_code: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    adjusted_at: datetime
    compensates_adjustment_id: str | None = None

    @model_validator(mode="after")
    def adjustment_is_explicit(self) -> RelationshipSlowVariableAdjustedPayload:
        _validate_hash(self)
        if self.operation == "adjust" and self.compensates_adjustment_id is not None:
            raise ValueError("ordinary relationship adjustment cannot compensate")
        if self.operation == "compensate" and self.compensates_adjustment_id is None:
            raise ValueError("relationship compensation requires its target")
        if len(self.signal_refs) != len(set(self.signal_refs)):
            raise ValueError("relationship adjustment signal refs must be unique")
        if not any(self.accepted_deltas.model_dump().values()):
            raise ValueError("relationship adjustment cannot be a no-op")
        return self


class BoundaryChangedPayload(RelationshipAuthorizedMutationPayload):
    operation: Literal["open", "revise", "close"]
    boundary: BoundaryProjection

    @model_validator(mode="after")
    def boundary_matches_authority(self) -> BoundaryChangedPayload:
        _validate_hash(self)
        if self.evidence_refs != self.boundary.evidence_refs:
            raise ValueError("boundary evidence does not match payload")
        if (
            self.boundary.origin.change_id != self.change_id
            or self.boundary.origin.transition_id != self.transition_id
            or self.boundary.origin.policy_refs != self.policy_refs
        ):
            raise ValueError("boundary origin does not match payload")
        if self.operation == "open" and (
            self.expected_entity_revision != 0
            or self.boundary.entity_revision != 1
            or self.boundary.status != "active"
        ):
            raise ValueError("boundary open must create active revision one")
        if self.operation != "open" and self.expected_entity_revision < 1:
            raise ValueError("boundary transition requires an existing entity")
        if self.operation == "revise" and self.boundary.status != "active":
            raise ValueError("boundary revision must remain active")
        if self.operation == "close" and self.boundary.status != "closed":
            raise ValueError("boundary close requires closed projection")
        return self


RELATIONSHIP_PAYLOAD_MODELS = {
    "RelationshipSignalAccepted": RelationshipSignalAcceptedPayload,
    "RelationshipSlowVariableAdjusted": RelationshipSlowVariableAdjustedPayload,
    "BoundaryChanged": BoundaryChangedPayload,
}


def relationship_mutation_hash(
    payload: RelationshipAuthorizedMutationPayload | Mapping[str, Any],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, RelationshipAuthorizedMutationPayload)
        else to_jsonable_python(dict(payload))
    )
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_hash(payload: RelationshipAuthorizedMutationPayload) -> None:
    if payload.accepted_change_hash != relationship_mutation_hash(payload):
        raise ValueError("accepted change hash does not match relationship transition")
