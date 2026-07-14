"""Typed authority events for unfinished conversation matters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import EvidenceRef, FrozenModel, ThreadProjection


class ThreadAuthorizedMutationPayload(FrozenModel):
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
    def authority_inputs_are_unique(self) -> ThreadAuthorizedMutationPayload:
        if len(self.policy_refs) != len(set(self.policy_refs)):
            raise ValueError("thread policy refs must be unique")
        if len(self.evidence_refs) != len({item.ref_id for item in self.evidence_refs}):
            raise ValueError("thread evidence refs must be unique")
        return self


class ThreadChangedPayload(ThreadAuthorizedMutationPayload):
    operation: Literal["open", "update", "resolve", "cancel", "supersede", "compensate"]
    thread_before: ThreadProjection | None
    thread_after: ThreadProjection
    compensates_transition_id: str | None = None

    @model_validator(mode="after")
    def mutation_is_complete(self) -> ThreadChangedPayload:
        if self.accepted_change_hash != thread_mutation_hash(self):
            raise ValueError("accepted change hash does not match thread transition")
        if self.thread_after.origin.change_id != self.change_id:
            raise ValueError("thread origin change does not match authority")
        if self.thread_after.origin.transition_id != self.transition_id:
            raise ValueError("thread origin transition does not match authority")
        if self.thread_after.origin.policy_refs != self.policy_refs:
            raise ValueError("thread origin policy does not match authority")
        if self.thread_after.values.source_evidence_refs != self.evidence_refs:
            raise ValueError("thread evidence does not match authority")
        if self.operation == "open":
            if self.thread_before is not None or self.expected_entity_revision != 0:
                raise ValueError("thread open must create from revision zero")
            if self.thread_after.entity_revision != 1:
                raise ValueError("thread open must create revision one")
        else:
            if self.thread_before is None or self.expected_entity_revision < 1:
                raise ValueError("thread transition requires an existing before image")
            if self.thread_after.thread_id != self.thread_before.thread_id:
                raise ValueError("thread transition cannot change thread id")
            if self.thread_after.entity_revision != self.expected_entity_revision + 1:
                raise ValueError("thread transition must advance one entity revision")
        if self.operation == "compensate" and self.compensates_transition_id is None:
            raise ValueError("thread compensation requires its target transition")
        if self.operation != "compensate" and self.compensates_transition_id is not None:
            raise ValueError("ordinary thread transition cannot compensate")
        return self


THREAD_PAYLOAD_MODELS = {
    "ThreadOpened": ThreadChangedPayload,
    "ThreadUpdated": ThreadChangedPayload,
    "ThreadResolved": ThreadChangedPayload,
    "ThreadCancelled": ThreadChangedPayload,
    "ThreadSuperseded": ThreadChangedPayload,
    "ThreadCompensated": ThreadChangedPayload,
}


class ThreadExpiredPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=1)
    thread_before: ThreadProjection
    thread_after: ThreadProjection
    clock_evidence_ref: EvidenceRef
    clock_event_ref: str = Field(min_length=1)
    clock_event_payload_hash: str = Field(min_length=64, max_length=64)
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    expires_at: datetime

    @model_validator(mode="after")
    def expiry_is_mechanical(self) -> ThreadExpiredPayload:
        if self.clock_evidence_ref.evidence_type != "clock_observation":
            raise ValueError("thread expiry requires clock evidence")
        if self.clock_evidence_ref.claim_purpose != "conversation_continuity":
            raise ValueError("thread expiry clock evidence has wrong purpose")
        if self.thread_after.thread_id != self.thread_before.thread_id:
            raise ValueError("thread expiry cannot change thread id")
        if self.thread_after.entity_revision != self.expected_entity_revision + 1:
            raise ValueError("thread expiry must advance one entity revision")
        return self


THREAD_MECHANICAL_PAYLOAD_MODELS = {"ThreadExpired": ThreadExpiredPayload}


def thread_mutation_hash(payload: ThreadAuthorizedMutationPayload | Mapping[str, Any]) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, ThreadAuthorizedMutationPayload)
        else to_jsonable_python(dict(payload))
    )
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    material = _canonicalize_typed_value(material)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonicalize_typed_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonicalize_typed_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize_typed_value(item) for item in value]
    if isinstance(value, datetime):
        value = value.astimezone(UTC)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and (value.endswith("Z") or value.endswith("+00:00")):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value
