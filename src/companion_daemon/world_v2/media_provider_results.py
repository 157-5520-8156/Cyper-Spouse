"""Immutable result contract for one Media v2 provider Action.

The generic Action receipt proves that a provider RPC reached a terminal
state, but it deliberately contains no opaque image/inspection bytes.  This
module closes that gap without making the provider a ledger writer: the
provider persists an idempotency-keyed result, the host reads it back through
this contract, and a v2 worker hashes and materializes it only after checking
the already-recorded terminal receipt.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from pydantic import Field, model_validator

from .media_v2 import StoredMediaPayload, media_payload_hash
from .schema_core import FrozenModel


class MediaProviderArtifactResult(FrozenModel):
    action_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_payload_ref: str = Field(min_length=1)
    artifact_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_content_type: str = Field(min_length=1)
    artifact_body: str = Field(min_length=1)

    @model_validator(mode="after")
    def binds_exact_artifact_bytes(self) -> "MediaProviderArtifactResult":
        if self.artifact_payload_hash != media_payload_hash(self.artifact_body):
            raise ValueError("media provider artifact result hash does not bind exact bytes")
        return self

    def artifact_payload(self) -> StoredMediaPayload:
        return StoredMediaPayload(
            payload_ref=self.artifact_payload_ref,
            payload_hash=self.artifact_payload_hash,
            content_type=self.artifact_content_type,
            body=self.artifact_body,
        )


class MediaProviderInspectionResult(FrozenModel):
    action_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    passed: bool
    reason_code: str = Field(min_length=1, max_length=256)
    observed_summary: str | None = Field(default=None, max_length=4_000)
    inspection_payload_ref: str = Field(min_length=1)
    inspection_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    inspection_content_type: str = Field(min_length=1)
    inspection_body: str = Field(min_length=1)
    repairable: bool = False
    repair_scope: tuple[str, ...] = ()

    @model_validator(mode="after")
    def binds_exact_inspection_bytes(self) -> "MediaProviderInspectionResult":
        if self.inspection_payload_hash != media_payload_hash(self.inspection_body):
            raise ValueError("media provider inspection result hash does not bind exact bytes")
        if self.repair_scope != tuple(sorted(set(self.repair_scope))):
            raise ValueError("media provider inspection repair scope must be sorted and unique")
        if self.passed and (self.repairable or self.repair_scope):
            raise ValueError("passed media inspection cannot authorize repair")
        if self.repairable != bool(self.repair_scope):
            raise ValueError("repairability must exactly match a non-empty defect scope")
        return self

    def inspection_payload(self) -> StoredMediaPayload:
        return StoredMediaPayload(
            payload_ref=self.inspection_payload_ref,
            payload_hash=self.inspection_payload_hash,
            content_type=self.inspection_content_type,
            body=self.inspection_body,
        )


MediaProviderExecutionResult = MediaProviderArtifactResult | MediaProviderInspectionResult


def media_provider_result_hash(result: MediaProviderExecutionResult) -> str:
    """Stable hash that must equal the generic provider receipt raw hash."""

    payload = {
        "result_type": type(result).__name__,
        "result": result.model_dump(mode="json"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MediaProviderResultTransport(Protocol):
    """Read-only recovery seam for durable provider results.

    Implementations must make this lookup idempotent and available after a
    process restart.  Returning ``None`` is a pending/reconciliation state,
    not permission to render again.
    """

    async def lookup_execution_result(
        self,
        *,
        action_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> MediaProviderExecutionResult | None: ...


__all__ = [
    "MediaProviderArtifactResult",
    "MediaProviderExecutionResult",
    "MediaProviderInspectionResult",
    "MediaProviderResultTransport",
    "media_provider_result_hash",
]
