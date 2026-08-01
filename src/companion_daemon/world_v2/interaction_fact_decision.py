"""Durable, replayable result of one interaction-fact model decision.

The event is deliberation audit only: it grants no Fact authority.  It closes
the crash window between a validated model answer and the later Fact,
correction, withdrawal, or no-change branch by making every branch rejoin the
same immutable semantic result.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from .schema_core import FrozenModel


_HASH = r"^[0-9a-f]{64}$"
_MAX_DECISION_BYTES = 262_144


def canonical_interaction_fact_decision_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def interaction_fact_decision_hash(decision_json: str) -> str:
    return hashlib.sha256(decision_json.encode("utf-8")).hexdigest()


class InteractionFactDecisionRecordedPayload(FrozenModel):
    """One validated model-owned choice, bound to its exact trigger input."""

    decision_id: str = Field(min_length=1, max_length=256)
    trigger_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    source_event_ref: str = Field(min_length=1, max_length=512)
    source_observation_ref: str = Field(min_length=1, max_length=512)
    evaluated_world_revision: int = Field(ge=0)
    evaluated_deliberation_revision: int = Field(ge=0)
    evaluated_ledger_sequence: int = Field(ge=0)
    adapter_version: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    request_hash: str = Field(pattern=_HASH)
    batch_size: int = Field(default=1, ge=1, le=8)
    fact_context_hash: str = Field(pattern=_HASH)
    decision_kind: Literal["retain", "withdraw", "no_change"]
    decision_json: str = Field(min_length=2, max_length=_MAX_DECISION_BYTES)
    decision_hash: str = Field(pattern=_HASH)
    recorded_at: datetime

    @model_validator(mode="after")
    def decision_bytes_are_canonical_and_bound(self) -> Self:
        if len(self.decision_json.encode("utf-8")) > _MAX_DECISION_BYTES:
            raise ValueError("interaction Fact decision exceeds byte limit")
        try:
            value = json.loads(self.decision_json)
        except json.JSONDecodeError as exc:
            raise ValueError("interaction Fact decision must be JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("interaction Fact decision must be an object")
        canonical = canonical_interaction_fact_decision_json(value)
        if canonical != self.decision_json:
            raise ValueError("interaction Fact decision bytes are not canonical")
        if interaction_fact_decision_hash(canonical) != self.decision_hash:
            raise ValueError("interaction Fact decision hash is invalid")
        if self.decision_kind == "no_change" and value != {"decision": "no_change"}:
            raise ValueError("no-change interaction Fact decision has invalid bytes")
        return self


__all__ = [
    "InteractionFactDecisionRecordedPayload",
    "canonical_interaction_fact_decision_json",
    "interaction_fact_decision_hash",
]
