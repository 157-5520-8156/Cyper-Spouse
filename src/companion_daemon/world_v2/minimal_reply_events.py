"""Closed event payloads and identities for the minimal-reply acceptance lane.

The models here are deliberately narrower than a general expression engine.
They represent one already accepted reply beat; they do not decide what to say
or dispatch it to a platform.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .minimal_reply_acceptance import ExpressionBeatMaterial, MessagePayloadMaterial
from .schema_core import FrozenModel


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def minimal_reply_event_id(*, manifest_hash: str, role: str, stable_id: str) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": "minimal-reply-event-id.1",
                "manifest_hash": manifest_hash,
                "role": role,
                "stable_id": stable_id,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"event:minimal-reply:{role}:{digest}"


def minimal_reply_idempotency_key(
    *, world_id: str, manifest_hash: str, role: str, stable_id: str
) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": "minimal-reply-idempotency.1",
                "world_id": world_id,
                "manifest_hash": manifest_hash,
                "role": role,
                "stable_id": stable_id,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"world-v2:minimal-reply:{role}:{digest}"


class MessagePayloadStoredPayload(FrozenModel):
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    message: MessagePayloadMaterial

    @model_validator(mode="after")
    def message_is_inline(self) -> "MessagePayloadStoredPayload":
        if self.message.storage_kind != "inline_text" or self.message.text is None:
            raise ValueError("message payload event cannot store a sidecar body")
        return self


class ExpressionPlanAcceptedPayload(FrozenModel):
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    expression_change_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=512)


class ExpressionBeatAuthorizedPayload(FrozenModel):
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    expression_change_id: str = Field(min_length=1, max_length=256)
    beat: ExpressionBeatMaterial


class ExpressionBeatSettledPayload(FrozenModel):
    """A terminal provider receipt has settled one authorized expression beat."""

    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=512)
    beat_id: str = Field(min_length=1, max_length=512)
    action_id: str = Field(min_length=1, max_length=512)
    receipt_id: str = Field(min_length=1, max_length=512)
    receipt_event_ref: str = Field(min_length=1, max_length=512)
    receipt_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_action_state: Literal["delivered", "failed", "unknown", "cancelled", "expired"]


class ExpressionPlanCompletedPayload(FrozenModel):
    """The current minimal-reply plan has no remaining authorized beats."""

    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=512)
    terminal_beat_id: str = Field(min_length=1, max_length=512)
    receipt_id: str = Field(min_length=1, max_length=512)
    receipt_event_ref: str = Field(min_length=1, max_length=512)
    receipt_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_action_state: Literal["delivered", "failed", "unknown", "cancelled", "expired"]


MINIMAL_REPLY_EVENT_PAYLOAD_MODELS = {
    "MessagePayloadStored": MessagePayloadStoredPayload,
    "ExpressionPlanAccepted": ExpressionPlanAcceptedPayload,
    "ExpressionBeatAuthorized": ExpressionBeatAuthorizedPayload,
    "ExpressionBeatSettled": ExpressionBeatSettledPayload,
    "ExpressionPlanCompleted": ExpressionPlanCompletedPayload,
}


__all__ = [
    "ExpressionBeatAuthorizedPayload",
    "ExpressionBeatSettledPayload",
    "ExpressionPlanCompletedPayload",
    "ExpressionPlanAcceptedPayload",
    "MINIMAL_REPLY_EVENT_PAYLOAD_MODELS",
    "MessagePayloadStoredPayload",
    "minimal_reply_event_id",
    "minimal_reply_idempotency_key",
]
