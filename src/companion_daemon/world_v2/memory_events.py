"""Typed authority events for source-bound retrieval memory candidates."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import (
    EvidenceRef,
    FrozenModel,
    MemoryCandidateProjection,
    MemoryRetentionRationale,
    MemorySourceBinding,
)


MemoryOperation = Literal[
    "open", "accept", "reject", "revise", "reinforce", "forget"
]


class MemoryClockForgetAuthority(FrozenModel):
    authority_kind: Literal["clock"] = "clock"
    reason: Literal["scheduled_decay", "obsolete_review"]
    clock_event_ref: str = Field(min_length=1)
    clock_world_revision: int = Field(ge=1)
    clock_payload_hash: str = Field(min_length=64, max_length=64)


class MemoryEvidenceForgetAuthority(FrozenModel):
    authority_kind: Literal["evidence"] = "evidence"
    reason: Literal["privacy_request", "explicit_suppression"]
    decision_evidence_ref: EvidenceRef
    target_candidate_id: str = Field(min_length=1)
    decision_subject_ref: str = Field(min_length=1)
    decision_scope_hash: str = Field(min_length=64, max_length=64)
    decision_content_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def evidence_kind_matches_reason(self) -> MemoryEvidenceForgetAuthority:
        expected = {
            "privacy_request": "observed_message",
            "explicit_suppression": "operator_observation",
        }[self.reason]
        if self.decision_evidence_ref.evidence_type != expected:
            raise ValueError("memory forget evidence kind does not match reason")
        if self.decision_scope_hash != memory_forget_scope_hash(
            reason=self.reason,
            target_candidate_id=self.target_candidate_id,
            decision_subject_ref=self.decision_subject_ref,
            decision_evidence_ref=self.decision_evidence_ref,
            decision_content_hash=self.decision_content_hash,
        ):
            raise ValueError("memory forget decision scope hash is invalid")
        return self


class MemorySourceIdentityRef(FrozenModel):
    source_kind: Literal["fact", "experience", "terminal_thread"]
    source_id: str = Field(min_length=1)
    source_entity_revision: int = Field(ge=1)
    source_authority_id: str = Field(min_length=64, max_length=64)


class MemorySourceInvalidationForgetAuthority(FrozenModel):
    authority_kind: Literal["source_invalidation"] = "source_invalidation"
    reason: Literal["source_invalidated"] = "source_invalidated"
    sources: tuple[MemorySourceIdentityRef, ...] = Field(min_length=1)


class MemoryCompressionForgetAuthority(FrozenModel):
    authority_kind: Literal["compression"] = "compression"
    reason: Literal["compressed_into"] = "compressed_into"
    target_candidate_id: str = Field(min_length=1)
    target_entity_revision: int = Field(ge=1)
    target_event_ref: str = Field(min_length=1)
    target_world_revision: int = Field(ge=1)
    target_payload_hash: str = Field(min_length=64, max_length=64)


class MemoryDeliberativeForgetAuthority(FrozenModel):
    authority_kind: Literal["accepted_deliberation"] = "accepted_deliberation"
    reason: Literal["low_future_utility"] = "low_future_utility"


MemoryForgetAuthority = Annotated[
    MemoryClockForgetAuthority
    | MemoryEvidenceForgetAuthority
    | MemorySourceInvalidationForgetAuthority
    | MemoryCompressionForgetAuthority
    | MemoryDeliberativeForgetAuthority,
    Field(discriminator="authority_kind"),
]


def memory_forget_scope_hash(
    *,
    reason: str,
    target_candidate_id: str,
    decision_subject_ref: str,
    decision_evidence_ref: EvidenceRef,
    decision_content_hash: str,
) -> str:
    encoded = json.dumps(
        {
            "reason": reason,
            "target_candidate_id": target_candidate_id,
            "decision_subject_ref": decision_subject_ref,
            "decision_evidence_ref": decision_evidence_ref.model_dump(mode="json"),
            "decision_content_hash": decision_content_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def memory_source_evidence(binding: MemorySourceBinding) -> EvidenceRef:
    if binding.source_kind == "fact":
        return EvidenceRef(
            ref_id=binding.authority_event_ref,
            evidence_type="committed_fact",
            claim_purpose="conversation_continuity",
            source_world_revision=binding.authority_world_revision,
            immutable_hash=binding.source_values_hash,
        )
    if binding.source_kind == "experience":
        return EvidenceRef(
            ref_id=binding.authority_event_ref,
            evidence_type="committed_experience",
            claim_purpose="conversation_continuity",
            source_world_revision=binding.authority_world_revision,
            immutable_hash=binding.source_values_hash,
        )
    return EvidenceRef(
        ref_id=binding.authority_event_ref,
        evidence_type="committed_world_event",
        claim_purpose="conversation_continuity",
        source_world_revision=binding.authority_world_revision,
        immutable_hash=binding.authority_payload_hash,
    )


class MemoryCandidateAuthorizedMutationPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)


class MemoryCandidateChangedPayload(MemoryCandidateAuthorizedMutationPayload):
    operation: MemoryOperation
    candidate_before: MemoryCandidateProjection | None
    candidate_after: MemoryCandidateProjection
    revise_kind: Literal["pending_edit", "compress", "clarify", "correct"] | None = None
    reinforcement_reason: MemoryRetentionRationale | None = None
    rejection_reason: Literal[
        "duplicate",
        "insufficient_future_utility",
        "operator_decision",
    ] | None = None
    forget_authority: MemoryForgetAuthority | None = None
    strength_before_bp: int | None = Field(default=None, ge=0, le=10_000)
    strength_after_bp: int | None = Field(default=None, ge=0, le=10_000)
    reinforcement_count_before: int | None = Field(default=None, ge=0)
    reinforcement_count_after: int | None = Field(default=None, ge=0)
    policy_version: str | None = None
    policy_digest: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def mutation_is_complete(self) -> MemoryCandidateChangedPayload:
        after = self.candidate_after
        if self.accepted_change_hash != memory_candidate_mutation_hash(self):
            raise ValueError("accepted change hash does not match memory candidate transition")
        if (
            after.origin.change_id != self.change_id
            or after.origin.transition_id != self.transition_id
            or after.origin.policy_refs != self.policy_refs
        ):
            raise ValueError("memory candidate origin does not match mutation authority")
        expected_evidence = tuple(
            memory_source_evidence(binding) for binding in after.values.source_bindings
        )
        if self.evidence_refs != expected_evidence:
            raise ValueError("memory evidence must be derived only from source bindings")
        if self.operation == "open":
            if (
                self.candidate_before is not None
                or self.expected_entity_revision != 0
                or after.entity_revision != 1
            ):
                raise ValueError("memory open must create revision one from zero")
        else:
            before = self.candidate_before
            if (
                before is None
                or before.candidate_id != after.candidate_id
                or self.expected_entity_revision != before.entity_revision
                or after.entity_revision != before.entity_revision + 1
            ):
                raise ValueError("memory transition requires exact consecutive images")
        lifecycle_fields = (
            self.strength_before_bp,
            self.strength_after_bp,
            self.reinforcement_count_before,
            self.reinforcement_count_after,
        )
        if self.operation in {"reinforce", "forget"}:
            before = self.candidate_before
            if (
                before is None
                or self.strength_before_bp != before.values.retrieval_strength_bp
                or self.strength_after_bp != after.values.retrieval_strength_bp
                or self.reinforcement_count_before
                != before.values.reinforcement_count
                or self.reinforcement_count_after
                != after.values.reinforcement_count
                or not self.policy_version
                or not self.policy_digest
            ):
                raise ValueError("memory reinforce/forget requires explicit before/after authority")
        elif any(item is not None for item in lifecycle_fields) or any(
            item is not None for item in (self.policy_version, self.policy_digest)
        ):
            raise ValueError("ordinary memory transition cannot carry settlement authority")
        if self.operation == "reinforce" and (
            self.reinforcement_reason is None
            or self.rejection_reason is not None
            or self.forget_authority is not None
        ):
            raise ValueError("memory reinforcement requires only its rationale class")
        if self.operation == "reject" and (
            self.rejection_reason is None
            or self.reinforcement_reason is not None
            or self.forget_authority is not None
        ):
            raise ValueError("memory rejection requires only its rejection reason")
        if self.operation == "forget" and (
            self.forget_authority is None
            or self.reinforcement_reason is not None
            or self.rejection_reason is not None
        ):
            raise ValueError("memory forget requires typed decision authority")
        if self.operation not in {"reinforce", "reject", "forget"} and any(
            item is not None
            for item in (
                self.reinforcement_reason,
                self.rejection_reason,
                self.forget_authority,
            )
        ):
            raise ValueError("ordinary memory transition cannot carry decision reasons")
        if self.operation == "revise" and self.revise_kind is None:
            raise ValueError("memory revision requires a typed revision kind")
        if self.operation != "revise" and self.revise_kind is not None:
            raise ValueError("only memory revision may carry a revision kind")
        return self


MEMORY_CANDIDATE_PAYLOAD_MODELS = {
    "MemoryCandidateOpened": MemoryCandidateChangedPayload,
    "MemoryCandidateAccepted": MemoryCandidateChangedPayload,
    "MemoryCandidateRejected": MemoryCandidateChangedPayload,
    "MemoryCandidateRevised": MemoryCandidateChangedPayload,
    "MemoryCandidateReinforced": MemoryCandidateChangedPayload,
    "MemoryCandidateForgotten": MemoryCandidateChangedPayload,
}


def memory_candidate_mutation_hash(
    payload: MemoryCandidateAuthorizedMutationPayload | Mapping[str, Any],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, MemoryCandidateAuthorizedMutationPayload)
        else to_jsonable_python(dict(payload))
    )
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    encoded = json.dumps(
        _canonicalize(material),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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
