"""Authority payloads for private commitments; never schedules behavior."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import CommitmentProjection, EvidenceRef, FrozenModel


class CommitmentAuthorizedMutationPayload(FrozenModel):
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
    def authority_is_unique(self) -> CommitmentAuthorizedMutationPayload:
        if len(self.policy_refs) != len(set(self.policy_refs)):
            raise ValueError("commitment policy refs must be unique")
        if len(self.evidence_refs) != len(
            {(item.evidence_type, item.ref_id) for item in self.evidence_refs}
        ):
            raise ValueError("commitment evidence refs must be unique")
        return self


class CommitmentChangedPayload(CommitmentAuthorizedMutationPayload):
    operation: Literal["open", "fulfill", "break", "release"]
    commitment_before: CommitmentProjection | None
    commitment_after: CommitmentProjection

    @model_validator(mode="after")
    def mutation_is_complete(self) -> CommitmentChangedPayload:
        if self.accepted_change_hash != commitment_mutation_hash(self):
            raise ValueError("accepted change hash does not match commitment transition")
        after = self.commitment_after
        if (
            after.origin.authority_mode != "accepted_proposal"
            or after.origin.change_id != self.change_id
            or after.origin.transition_id != self.transition_id
            or after.origin.policy_refs != self.policy_refs
            or after.values.source_evidence_refs != self.evidence_refs
        ):
            raise ValueError("commitment after image does not match proposal authority")
        if self.operation == "open":
            if self.commitment_before is not None or self.expected_entity_revision != 0:
                raise ValueError("commitment open must create from revision zero")
            if after.entity_revision != 1:
                raise ValueError("commitment open must create revision one")
        else:
            before = self.commitment_before
            if before is None or self.expected_entity_revision < 1:
                raise ValueError("commitment transition requires a before image")
            if after.commitment_id != before.commitment_id:
                raise ValueError("commitment transition cannot change identity")
            if after.entity_revision != self.expected_entity_revision + 1:
                raise ValueError("commitment transition must advance one revision")
        return self


class CommitmentClockTransitionPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    operation: Literal["due", "break"]
    expected_entity_revision: int = Field(ge=1)
    commitment_before: CommitmentProjection
    commitment_after: CommitmentProjection
    clock_evidence_ref: EvidenceRef
    clock_event_ref: str = Field(min_length=1)
    clock_event_payload_hash: str = Field(min_length=64, max_length=64)
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def transition_is_mechanical(self) -> CommitmentClockTransitionPayload:
        if (
            self.clock_evidence_ref.evidence_type != "clock_observation"
            or self.clock_evidence_ref.claim_purpose != "conversation_continuity"
        ):
            raise ValueError("commitment clock transition requires continuity clock evidence")
        if self.commitment_after.commitment_id != self.commitment_before.commitment_id:
            raise ValueError("commitment clock transition cannot change identity")
        if self.commitment_after.entity_revision != self.expected_entity_revision + 1:
            raise ValueError("commitment clock transition must advance one revision")
        return self


COMMITMENT_ACCEPTED_PAYLOAD_MODELS = {
    "PrivateCommitmentOpened": CommitmentChangedPayload,
    "PrivateCommitmentFulfilled": CommitmentChangedPayload,
    "PrivateCommitmentReleased": CommitmentChangedPayload,
    "PrivateCommitmentBroken": CommitmentChangedPayload,
}

COMMITMENT_MECHANICAL_PAYLOAD_MODELS = {
    "PrivateCommitmentDue": CommitmentClockTransitionPayload,
    "PrivateCommitmentDeadlineBroken": CommitmentClockTransitionPayload,
}
COMMITMENT_PAYLOAD_MODELS = {
    **COMMITMENT_ACCEPTED_PAYLOAD_MODELS,
    **COMMITMENT_MECHANICAL_PAYLOAD_MODELS,
}


def commitment_mutation_hash(
    payload: CommitmentAuthorizedMutationPayload | Mapping[str, Any],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, CommitmentAuthorizedMutationPayload)
        else to_jsonable_python(dict(payload))
    )
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    encoded = json.dumps(
        _canonicalize(material), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and (value.endswith("Z") or value.endswith("+00:00")):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value
