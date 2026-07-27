"""Durable model-owned retention choice for one exact accepted Fact."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal, Self

from pydantic import Field, model_validator

from .schema_core import FrozenModel


_HASH = r"^[0-9a-f]{64}$"
_MAX_DECISION_BYTES = 65_536


def canonical_fact_memory_decision_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def fact_memory_decision_hash(decision_json: str) -> str:
    return hashlib.sha256(decision_json.encode("utf-8")).hexdigest()


class FactMemoryDecisionRecordedPayload(FrozenModel):
    """Validated retain/no-change choice bound to immutable Fact authority."""

    decision_id: str = Field(min_length=1, max_length=256)
    trigger_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    source_observation_ref: str = Field(min_length=1, max_length=512)
    fact_id: str = Field(min_length=1, max_length=256)
    fact_entity_revision: int = Field(ge=1)
    fact_authority_event_ref: str = Field(min_length=1, max_length=512)
    fact_authority_world_revision: int = Field(ge=1)
    fact_authority_payload_hash: str = Field(pattern=_HASH)
    evaluated_world_revision: int = Field(ge=0)
    evaluated_deliberation_revision: int = Field(ge=0)
    evaluated_ledger_sequence: int = Field(ge=0)
    adapter_version: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    request_hash: str = Field(pattern=_HASH)
    decision_kind: Literal["retain", "no_change"]
    decision_json: str = Field(min_length=2, max_length=_MAX_DECISION_BYTES)
    decision_hash: str = Field(pattern=_HASH)
    recorded_at: datetime

    @model_validator(mode="after")
    def decision_bytes_are_canonical_and_bound(self) -> Self:
        if len(self.decision_json.encode("utf-8")) > _MAX_DECISION_BYTES:
            raise ValueError("Fact-memory decision exceeds byte limit")
        try:
            value = json.loads(self.decision_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Fact-memory decision must be JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Fact-memory decision must be an object")
        canonical = canonical_fact_memory_decision_json(value)
        if canonical != self.decision_json:
            raise ValueError("Fact-memory decision bytes are not canonical")
        if fact_memory_decision_hash(canonical) != self.decision_hash:
            raise ValueError("Fact-memory decision hash is invalid")
        if self.decision_kind == "no_change" and value != {"decision": "no_change"}:
            raise ValueError("Fact-memory no-change decision has invalid bytes")
        return self


__all__ = [
    "FactMemoryDecisionRecordedPayload",
    "canonical_fact_memory_decision_json",
    "fact_memory_decision_hash",
]
