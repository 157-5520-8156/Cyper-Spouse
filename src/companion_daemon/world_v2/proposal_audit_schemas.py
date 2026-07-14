"""Immutable Phase-4A model/proposal audit contracts.

These records are deliberation authority only.  They never authorize a domain
mutation or Action and intentionally have no dependency on Acceptance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from .proposal_envelope import ProposalInput
from .schema_core import FrozenModel


_HASH = r"^[0-9a-f]{64}$"
_PROPOSAL_HASH = r"^sha256:[0-9a-f]{64}$"
_MAX_PROPOSAL_BYTES = 262_144
_MAX_AUDIT_BYTES = 32_768
_PROPOSAL_ADAPTER = TypeAdapter(ProposalInput)


class RecordedModelRoute(FrozenModel):
    tier: Literal["flash", "thinking"]
    reason_code: str = Field(min_length=1, max_length=128)
    router_version: str = Field(min_length=1, max_length=128)


class RecordedModelResultAudit(FrozenModel):
    model_call_id: str = Field(min_length=1, max_length=256)
    model_result_ref: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    route: RecordedModelRoute
    model_id: str | None = Field(default=None, max_length=256)
    model_version: str | None = Field(default=None, max_length=256)
    request_hash: str = Field(pattern=_HASH)
    response_hash: str | None = Field(default=None, pattern=_HASH)
    status: Literal[
        "proposal_validated",
        "main_timeout",
        "main_invalid",
        "main_exception",
        "main_timeout_recovered",
        "main_invalid_recovered",
        "main_exception_recovered",
        "recovery_failed",
    ]
    failure_code: str | None = Field(default=None, max_length=64)
    input_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=10_000_000)

    @model_validator(mode="after")
    def output_and_failure_are_consistent(self) -> Self:
        encoded = canonical_json(
            {"model_call_id": self.model_call_id, "response_hash": self.response_hash}
        )
        expected_ref = f"model-result:{sha256(encoded)}"
        if self.model_result_ref != expected_ref:
            raise ValueError("model result ref is not bound to its call")
        identity = (self.model_id, self.model_version, self.response_hash)
        has_output = all(value is not None for value in identity)
        if not has_output and any(value is not None for value in identity):
            raise ValueError("model output audit identity is partial")
        if not has_output and (self.input_tokens is not None or self.output_tokens is not None):
            raise ValueError("model token counts require an output identity")
        required = {
            "main_timeout": "main_timeout",
            "main_invalid": "main_invalid_output",
            "main_exception": "main_exception",
            "main_timeout_recovered": "main_timeout",
            "main_invalid_recovered": "main_invalid_output",
            "main_exception_recovered": "main_exception",
        }.get(self.status)
        if self.status == "proposal_validated":
            if not has_output or self.failure_code is not None:
                raise ValueError("validated audit requires output and no failure")
        elif self.status in {"main_timeout", "main_exception"}:
            if has_output or self.failure_code != required:
                raise ValueError("terminal main audit has invalid lineage")
        elif self.status == "main_invalid":
            if self.failure_code != required:
                raise ValueError("invalid main audit has invalid lineage")
        elif self.status == "recovery_failed":
            if not (self.failure_code or "").startswith("quick_"):
                raise ValueError("failed recovery audit has invalid lineage")
        elif not has_output or self.failure_code != required:
            raise ValueError("recovered audit has invalid lineage")
        return self


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ModelResultRecordedPayload(FrozenModel):
    audit_contract: Literal["model-result-audit.1"] = "model-result-audit.1"
    model_result_ref: str = Field(min_length=1, max_length=256)
    deliberation_result_id: str = Field(min_length=1, max_length=256)
    proposal_hash: str | None = Field(default=None, pattern=_PROPOSAL_HASH)
    model_call_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    capsule_id: str = Field(pattern=_HASH)
    trigger_ref: str = Field(min_length=1, max_length=512)
    evaluated_world_revision: int = Field(ge=0)
    attempt_index: int = Field(ge=0, le=1)
    attempt_count: int = Field(ge=1, le=2)
    audit_json: str = Field(min_length=2, max_length=_MAX_AUDIT_BYTES)
    audit_hash: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def audit_bytes_are_canonical_and_bound(self) -> Self:
        if self.attempt_index >= self.attempt_count:
            raise ValueError("model attempt index is out of bounds")
        if len(self.audit_json.encode("utf-8")) > _MAX_AUDIT_BYTES:
            raise ValueError("model result audit exceeds byte limit")
        audit = RecordedModelResultAudit.model_validate_json(self.audit_json)
        canonical = canonical_json(audit.model_dump(mode="json"))
        if canonical != self.audit_json or sha256(canonical) != self.audit_hash:
            raise ValueError("model result audit bytes/hash are not canonical")
        if (
            audit.model_call_id != self.model_call_id
            or audit.model_result_ref != self.model_result_ref
            or audit.attempt_id != self.attempt_id
        ):
            raise ValueError("model result lineage does not match its audit bytes")
        return self


def validate_recorded_attempt_lineage(
    audits: tuple[RecordedModelResultAudit, ...],
    *,
    capsule_id: str,
    proposal_hash: str | None,
    deliberation_result_id: str,
) -> None:
    if not 1 <= len(audits) <= 2:
        raise ValueError("model attempt audit count is out of bounds")
    if len({audit.model_call_id for audit in audits}) != len(audits):
        raise ValueError("model attempts require distinct call identities")
    if len(audits) == 1:
        if audits[0].status != "proposal_validated" or proposal_hash is None:
            raise ValueError("single attempt must produce a validated proposal")
    else:
        main, quick = audits
        expected = {
            "main_timeout": ("main_timeout", "main_timeout_recovered"),
            "main_invalid": ("main_invalid_output", "main_invalid_recovered"),
            "main_exception": ("main_exception", "main_exception_recovered"),
        }.get(main.status)
        if expected is None or main.failure_code != expected[0]:
            raise ValueError("recovery lineage has an invalid main audit")
        if quick.status == "recovery_failed":
            if proposal_hash is not None or not (quick.failure_code or "").startswith("quick_"):
                raise ValueError("failed recovery cannot claim a proposal")
        elif (
            quick.status != expected[1]
            or quick.failure_code != expected[0]
            or proposal_hash is None
        ):
            raise ValueError("successful recovery lineage is invalid")
        if main.attempt_id != quick.attempt_id or main.route != quick.route:
            raise ValueError("model attempt lineage changed identity or route")
    identity = {
        "capsule_id": capsule_id,
        "proposal_hash": proposal_hash,
        "attempt_audits": [audit.model_dump(mode="json") for audit in audits],
    }
    if deliberation_result_id != f"deliberation:{sha256(canonical_json(identity))}":
        raise ValueError("deliberation result identity is invalid")


class ProposalRecordedV2Payload(FrozenModel):
    audit_contract: Literal["proposal-envelope-audit.1"] = "proposal-envelope-audit.1"
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_kind: Literal["decision", "continuation", "minimal"]
    model_result_ref: str = Field(min_length=1, max_length=256)
    deliberation_result_id: str = Field(min_length=1, max_length=256)
    model_call_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    capsule_id: str = Field(pattern=_HASH)
    trigger_ref: str = Field(min_length=1, max_length=512)
    evaluated_world_revision: int = Field(ge=0)
    proposal_json: str = Field(min_length=2, max_length=_MAX_PROPOSAL_BYTES)
    proposal_hash: str = Field(pattern=_PROPOSAL_HASH)

    @model_validator(mode="after")
    def proposal_bytes_are_canonical_and_bound(self) -> Self:
        if len(self.proposal_json.encode("utf-8")) > _MAX_PROPOSAL_BYTES:
            raise ValueError("proposal audit exceeds byte limit")
        try:
            proposal = _PROPOSAL_ADAPTER.validate_json(self.proposal_json, strict=True)
        except (ValueError, RecursionError) as exc:
            raise ValueError("proposal audit must contain a valid ProposalEnvelope") from exc
        canonical = canonical_json(proposal.model_dump(mode="json"))
        if canonical != self.proposal_json or proposal.proposal_hash != self.proposal_hash:
            raise ValueError("proposal audit bytes/hash are not canonical")
        if (
            proposal.proposal_id != self.proposal_id
            or proposal.proposal_kind != self.proposal_kind
            or proposal.trigger_ref != self.trigger_ref
            or proposal.evaluated_world_revision != self.evaluated_world_revision
        ):
            raise ValueError("proposal audit lineage does not match its envelope")
        if (
            proposal.proposal_kind == "minimal"
            and proposal.source_model_result != self.model_result_ref
        ):
            raise ValueError("minimal proposal is not bound to the final model result")
        return self


class ModelResultAuditProjection(ModelResultRecordedPayload):
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=_HASH)


class ProposalAuditProjection(ProposalRecordedV2Payload):
    event_ref: str = Field(min_length=1)
    event_payload_hash: str = Field(pattern=_HASH)


__all__ = [
    "ModelResultAuditProjection",
    "ModelResultRecordedPayload",
    "ProposalAuditProjection",
    "ProposalRecordedV2Payload",
    "canonical_json",
    "sha256",
    "validate_recorded_attempt_lineage",
]
