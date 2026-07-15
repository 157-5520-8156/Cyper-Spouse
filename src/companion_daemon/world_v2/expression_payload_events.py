"""Ledger descriptor for an opaque expression payload sidecar read."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .schema_core import FrozenModel, PrivacyClass


class ExpressionPayloadDescriptorRecordedPayload(FrozenModel):
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    payload_ref: str = Field(min_length=1, max_length=512)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=128)
    privacy_class: PrivacyClass
    payload_kind: Literal["referenced", "inline_encrypted"]


EXPRESSION_PAYLOAD_EVENT_MODELS = {
    "ExpressionPayloadDescriptorRecorded": ExpressionPayloadDescriptorRecordedPayload,
}


__all__ = ["EXPRESSION_PAYLOAD_EVENT_MODELS", "ExpressionPayloadDescriptorRecordedPayload"]
