"""Dedicated authority records for Threads opened or updated after delivery.

The generic ``ThreadProposalRecorded`` family is deliberately not used here.
This lane has a narrower source contract: one exact committed
``MediaDeliveryShared`` event and its claimed interaction trigger.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schema_core import FrozenModel
from .schemas import EvidenceRef, MediaDeliveryThreadProposalProjection, ThreadProjection


class MediaDeliveryThreadProposalRecordedPayload(MediaDeliveryThreadProposalProjection):
    @model_validator(mode="after")
    def exact_hash_and_source(self) -> "MediaDeliveryThreadProposalRecordedPayload":
        if self.proposed_change_hash != media_thread_mutation_hash(self):
            raise ValueError("media delivery thread proposed change hash does not match fields")
        if not any(item.ref_id == self.delivery_event_ref for item in self.evidence_refs):
            raise ValueError("media delivery thread evidence must bind delivery")
        return self


class MediaDeliveryThreadChangedPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    operation: Literal["open", "update"]
    expected_entity_revision: int = Field(ge=0)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    thread_before: ThreadProjection | None
    thread_after: ThreadProjection
    compensates_transition_id: str | None = None

    @model_validator(mode="after")
    def accepted_payload_is_complete(self) -> "MediaDeliveryThreadChangedPayload":
        if self.accepted_change_hash != media_thread_mutation_hash(self):
            raise ValueError("media delivery thread accepted change hash does not match fields")
        if (
            self.thread_after.origin.change_id != self.change_id
            or self.thread_after.origin.transition_id != self.transition_id
        ):
            raise ValueError("media delivery thread origin does not match authority")
        if self.thread_after.origin.policy_refs != self.policy_refs:
            raise ValueError("media delivery thread origin policy does not match authority")
        if self.thread_after.values.source_evidence_refs != self.evidence_refs:
            raise ValueError("media delivery thread evidence does not match authority")
        if self.operation == "open":
            if (
                self.thread_before is not None
                or self.expected_entity_revision != 0
                or self.thread_after.entity_revision != 1
            ):
                raise ValueError("media delivery thread open must create revision one")
        elif (
            self.thread_before is None
            or self.thread_after.entity_revision != self.expected_entity_revision + 1
        ):
            raise ValueError("media delivery thread update must advance one entity revision")
        return self


def media_thread_mutation_hash(
    payload: MediaDeliveryThreadProposalRecordedPayload
    | MediaDeliveryThreadChangedPayload
    | Mapping[str, Any],
) -> str:
    raw = (
        payload.model_dump(mode="json")
        if isinstance(payload, FrozenModel)
        else to_jsonable_python(dict(payload))
    )
    # Source/decision metadata is bound separately by the manifest.  This
    # digest binds exactly the mutation which must survive proposal→acceptance.
    raw = {
        field: raw.get(field)
        for field in (
            "change_id",
            "transition_id",
            "operation",
            "expected_entity_revision",
            "evaluated_world_revision",
            "evidence_refs",
            "policy_refs",
            "thread_before",
            "thread_after",
        )
    }
    encoded = json.dumps(
        raw, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


MEDIA_DELIVERY_THREAD_PAYLOAD_MODELS = {
    "MediaDeliveryThreadProposalRecorded": MediaDeliveryThreadProposalRecordedPayload,
    "MediaDeliveryThreadOpened": MediaDeliveryThreadChangedPayload,
    "MediaDeliveryThreadUpdated": MediaDeliveryThreadChangedPayload,
}


__all__ = [
    "MEDIA_DELIVERY_THREAD_PAYLOAD_MODELS",
    "MediaDeliveryThreadChangedPayload",
    "MediaDeliveryThreadProposalRecordedPayload",
    "media_thread_mutation_hash",
]
