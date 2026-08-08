"""Durable model-owned retention choice for one lived Experience."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .proposal_audit_schemas import ModelResultRecordedPayload
from .schema_core import FrozenModel


_HASH = r"^[0-9a-f]{64}$"
_MAX_DECISION_BYTES = 65_536


def canonical_experience_memory_decision_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def experience_memory_decision_hash(decision_json: str) -> str:
    return hashlib.sha256(decision_json.encode("utf-8")).hexdigest()


def experience_memory_decision_event_id(
    *, experience_authority_event_ref: str
) -> str:
    if not experience_authority_event_ref:
        raise ValueError("Experience-memory decision requires Experience authority")
    digest = hashlib.sha256(
        canonical_experience_memory_decision_json(
            {"experience_authority_event_ref": experience_authority_event_ref}
        ).encode()
    ).hexdigest()
    return "event:experience-memory:decision:" + digest


def experience_memory_decision_identity(
    *, experience_authority_event_ref: str
) -> str:
    if not experience_authority_event_ref:
        raise ValueError("Experience-memory decision requires Experience authority")
    digest = hashlib.sha256(
        canonical_experience_memory_decision_json(
            {"experience_authority_event_ref": experience_authority_event_ref}
        ).encode()
    ).hexdigest()
    return "experience-memory-decision:" + digest


class ExperienceMemoryDecisionRecordedPayload(FrozenModel):
    """Retain/no-change choice bound to exact immutable lived history."""

    decision_id: str = Field(min_length=1, max_length=256)
    experience_id: str = Field(min_length=1, max_length=256)
    experience_entity_revision: int = Field(ge=1)
    experience_authority_event_ref: str = Field(min_length=1, max_length=512)
    experience_authority_world_revision: int = Field(ge=1)
    experience_authority_payload_hash: str = Field(pattern=_HASH)
    evaluated_world_revision: int = Field(ge=0)
    adapter_version: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    request_hash: str = Field(pattern=_HASH)
    decision_kind: Literal["retain", "no_change"]
    decision_json: str = Field(min_length=2, max_length=_MAX_DECISION_BYTES)
    decision_hash: str = Field(pattern=_HASH)
    recorded_at: datetime
    character_interior_model_result: ModelResultRecordedPayload | None = None

    @model_validator(mode="after")
    def decision_bytes_are_canonical_and_bound(
        self,
    ) -> ExperienceMemoryDecisionRecordedPayload:
        if len(self.decision_json.encode("utf-8")) > _MAX_DECISION_BYTES:
            raise ValueError("Experience-memory decision exceeds byte limit")
        try:
            value = json.loads(self.decision_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Experience-memory decision must be JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Experience-memory decision must be an object")
        canonical = canonical_experience_memory_decision_json(value)
        if canonical != self.decision_json:
            raise ValueError("Experience-memory decision bytes are not canonical")
        if experience_memory_decision_hash(canonical) != self.decision_hash:
            raise ValueError("Experience-memory decision hash is invalid")
        if (
            self.decision_id
            != experience_memory_decision_identity(
                experience_authority_event_ref=self.experience_authority_event_ref
            )
        ):
            raise ValueError("Experience-memory decision identity is invalid")
        if self.decision_kind == "no_change" and value != {"decision": "no_change"}:
            raise ValueError("Experience-memory no-change decision has invalid bytes")
        return self


__all__ = [
    "ExperienceMemoryDecisionRecordedPayload",
    "canonical_experience_memory_decision_json",
    "experience_memory_decision_event_id",
    "experience_memory_decision_hash",
    "experience_memory_decision_identity",
]
