"""Typed, immutable Experience authority events."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .life_events import DomainMutationPayload
from .schemas import (
    EvidenceRef,
    ExperienceExecutionReceiptBinding,
    ExperienceOccurrenceSettlementBinding,
    ExperienceProjection,
    LegacyExperienceEvidenceRef,
    LegacyExperienceProjection,
)


def experience_binding_evidence(
    binding: ExperienceOccurrenceSettlementBinding | ExperienceExecutionReceiptBinding,
) -> EvidenceRef:
    if isinstance(binding, ExperienceOccurrenceSettlementBinding):
        return EvidenceRef(
            ref_id=binding.authority_event_ref,
            evidence_type="settled_world_event",
            claim_purpose="past_experience",
            source_world_revision=binding.authority_world_revision,
            immutable_hash=binding.authority_payload_hash,
        )
    return EvidenceRef(
        ref_id=binding.receipt_id,
        evidence_type="settled_external_result",
        claim_purpose="past_experience",
        immutable_hash=binding.receipt_hash,
    )


class ExperienceAuthorizedMutationPayload(DomainMutationPayload):
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)


class ExperienceCommittedPayload(ExperienceAuthorizedMutationPayload):
    experience: ExperienceProjection

    @model_validator(mode="after")
    def creates_exact_revision_one_authority(self) -> ExperienceCommittedPayload:
        if self.expected_entity_revision != 0 or self.experience.entity_revision != 1:
            raise ValueError("ExperienceCommitted must create entity revision one")
        if (
            self.experience.origin.change_id != self.change_id
            or self.experience.origin.transition_id != self.transition_id
            or self.experience.origin.policy_refs != self.policy_refs
        ):
            raise ValueError("experience origin does not match mutation authority")
        if self.accepted_change_hash != experience_mutation_hash(self):
            raise ValueError("accepted change hash does not match experience mutation")
        return self


class LegacyExperienceCommittedPayload(DomainMutationPayload):
    """Migration-only payload; never accepted by a live ledger append."""

    evidence_refs: tuple[LegacyExperienceEvidenceRef, ...] = Field(min_length=1)
    experience: LegacyExperienceProjection


EXPERIENCE_PAYLOAD_MODELS = {"ExperienceCommitted": ExperienceCommittedPayload}


def experience_mutation_hash(
    payload: ExperienceAuthorizedMutationPayload | Mapping[str, Any],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, ExperienceAuthorizedMutationPayload)
        else to_jsonable_python(dict(payload))
    )
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    encoded = json.dumps(
        _canonicalize(material),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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
