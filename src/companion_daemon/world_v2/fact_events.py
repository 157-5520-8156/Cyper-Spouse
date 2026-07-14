"""Typed authority events for replay-safe facts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import EvidenceRef, FactProjection, FrozenModel


class FactAuthorizedMutationPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)


class FactChangedPayload(FactAuthorizedMutationPayload):
    operation: Literal["commit", "correct", "withdraw", "compensate"]
    fact_before: FactProjection | None
    fact_after: FactProjection
    compensates_transition_id: str | None = None

    @model_validator(mode="after")
    def mutation_is_complete(self) -> FactChangedPayload:
        if self.accepted_change_hash != fact_mutation_hash(self):
            raise ValueError("accepted change hash does not match fact transition")
        after = self.fact_after
        if (
            after.origin.change_id != self.change_id
            or after.origin.transition_id != self.transition_id
            or after.origin.policy_refs != self.policy_refs
            or after.values.source_evidence_refs != self.evidence_refs
        ):
            raise ValueError("fact after image does not match proposal authority")
        if self.operation == "commit":
            if self.fact_before is not None or self.expected_entity_revision != 0:
                raise ValueError("fact commit must create from revision zero")
            if after.entity_revision != 1:
                raise ValueError("fact commit must create revision one")
        else:
            before = self.fact_before
            if before is None or self.expected_entity_revision < 1:
                raise ValueError("fact transition requires a before image")
            if after.fact_id != before.fact_id:
                raise ValueError("fact transition cannot change identity")
            if after.entity_revision != self.expected_entity_revision + 1:
                raise ValueError("fact transition must advance one revision")
        if self.operation == "compensate" and not self.compensates_transition_id:
            raise ValueError("fact compensation requires its correction target")
        if self.operation != "compensate" and self.compensates_transition_id is not None:
            raise ValueError("ordinary fact transition cannot compensate")
        return self


FACT_PAYLOAD_MODELS = {
    "FactCommitted": FactChangedPayload,
    "FactCorrected": FactChangedPayload,
    "FactWithdrawn": FactChangedPayload,
    "FactCorrectionCompensated": FactChangedPayload,
}


def fact_mutation_hash(payload: FactAuthorizedMutationPayload | Mapping[str, Any]) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, FactAuthorizedMutationPayload)
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
