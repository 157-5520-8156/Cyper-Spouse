"""Typed, affect-neutral event payloads for accepted event meaning.

Appraisals are fallible interpretations.  They retain alternative hypotheses and
their evidence, but deliberately contain no affect component or behavioural
instruction.  Only a later, independently accepted affect transition may turn an
appraisal into an affect change.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import (
    AppraisalHypothesis as AppraisalHypothesis,
    AppraisalProjection,
    EvidenceRef,
    FrozenModel,
)

_APPRAISAL_EVIDENCE_TYPES = {
    "committed_fact",
    "committed_experience",
    "committed_world_event",
    "settled_world_event",
    "settled_external_result",
    "observed_message",
    "active_plan",
    "clock_observation",
}


class AppraisalMutationPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def sourced_by_hypothesis_evidence(self) -> AppraisalMutationPayload:
        _validate_appraisal_evidence(self.evidence_refs)
        if len(self.policy_refs) != len(set(self.policy_refs)):
            raise ValueError("appraisal policy refs must be unique")
        return self


class AppraisalAuthorizedMutationPayload(AppraisalMutationPayload):
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)
    trigger_id: str = Field(min_length=1)


class AppraisalAcceptedPayload(AppraisalAuthorizedMutationPayload):
    appraisal: AppraisalProjection

    @model_validator(mode="after")
    def creates_active_revision_one(self) -> AppraisalAcceptedPayload:
        _validate_authorized_hash(self)
        if self.expected_entity_revision != 0:
            raise ValueError("AppraisalAccepted must create a new entity")
        if self.appraisal.entity_revision != 1 or self.appraisal.status != "active":
            raise ValueError("AppraisalAccepted requires active entity revision one")
        if self.appraisal.supersedes_appraisal_id is not None:
            raise ValueError("replacement appraisal requires AppraisalSuperseded")
        if self.evidence_refs != self.appraisal.evidence_refs:
            raise ValueError("payload evidence must equal appraisal evidence")
        if any(ref.claim_purpose != "private_hypothesis" for ref in self.evidence_refs):
            raise ValueError("accepted appraisal evidence must support a private hypothesis")
        _validate_appraisal_origin(self.appraisal, self)
        return self


class AppraisalContradictedPayload(AppraisalAuthorizedMutationPayload):
    expected_entity_revision: int = Field(ge=1)
    appraisal_id: str = Field(min_length=1)
    contradicted_at: datetime
    contradiction_refs: tuple[EvidenceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def contradiction_is_the_transition_evidence(self) -> AppraisalContradictedPayload:
        _validate_authorized_hash(self)
        _require_aware(self.contradicted_at, "contradicted_at")
        _validate_appraisal_evidence(self.contradiction_refs)
        if self.evidence_refs != self.contradiction_refs:
            raise ValueError("contradiction refs must equal transition evidence")
        if any(ref.claim_purpose != "private_hypothesis" for ref in self.evidence_refs):
            raise ValueError("contradiction evidence must support a private hypothesis")
        return self


class AppraisalExpiredPayload(AppraisalMutationPayload):
    expected_entity_revision: int = Field(ge=1)
    appraisal_id: str = Field(min_length=1)
    expired_at: datetime

    @model_validator(mode="after")
    def expiry_is_timezone_aware(self) -> AppraisalExpiredPayload:
        _require_aware(self.expired_at, "expired_at")
        if not all(
            ref.evidence_type == "clock_observation" and ref.claim_purpose == "current_fact"
            for ref in self.evidence_refs
        ):
            raise ValueError("appraisal expiry requires clock evidence")
        return self


class AppraisalSupersededPayload(AppraisalAuthorizedMutationPayload):
    expected_entity_revision: int = Field(ge=1)
    appraisal_id: str = Field(min_length=1)
    superseded_at: datetime
    successor: AppraisalProjection

    @model_validator(mode="after")
    def successor_is_linked_and_new(self) -> AppraisalSupersededPayload:
        _validate_authorized_hash(self)
        _require_aware(self.superseded_at, "superseded_at")
        if self.successor.appraisal_id == self.appraisal_id:
            raise ValueError("successor appraisal must have a new identity")
        if self.successor.entity_revision != 1 or self.successor.status != "active":
            raise ValueError("successor must be active entity revision one")
        if self.successor.supersedes_appraisal_id != self.appraisal_id:
            raise ValueError("successor must link to the superseded appraisal")
        if self.evidence_refs != self.successor.evidence_refs:
            raise ValueError("payload evidence must equal successor evidence")
        if any(ref.claim_purpose != "private_hypothesis" for ref in self.evidence_refs):
            raise ValueError("successor evidence must support a private hypothesis")
        _validate_appraisal_origin(self.successor, self)
        return self


APPRAISAL_PAYLOAD_MODELS = {
    "AppraisalAccepted": AppraisalAcceptedPayload,
    "AppraisalContradicted": AppraisalContradictedPayload,
    "AppraisalExpired": AppraisalExpiredPayload,
    "AppraisalSuperseded": AppraisalSupersededPayload,
}


def appraisal_mutation_hash(
    payload: AppraisalAuthorizedMutationPayload | Mapping[str, Any],
) -> str:
    """Hash the complete proposed transition, excluding its later authorization IDs."""

    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, AppraisalAuthorizedMutationPayload)
        else to_jsonable_python(dict(payload))
    )
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_authorized_hash(payload: AppraisalAuthorizedMutationPayload) -> None:
    if payload.accepted_change_hash != appraisal_mutation_hash(payload):
        raise ValueError("accepted change hash does not match appraisal transition")


def _validate_appraisal_origin(
    appraisal: AppraisalProjection,
    payload: AppraisalAuthorizedMutationPayload,
) -> None:
    if (
        appraisal.origin.change_id != payload.change_id
        or appraisal.origin.transition_id != payload.transition_id
        or appraisal.origin.policy_refs != payload.policy_refs
    ):
        raise ValueError("appraisal origin does not match its accepted transition")


def _validate_appraisal_evidence(refs: tuple[EvidenceRef, ...]) -> None:
    identities = [ref.ref_id for ref in refs]
    if len(identities) != len(set(identities)):
        raise ValueError("appraisal evidence refs must be unique")
    for ref in refs:
        if ref.evidence_type not in _APPRAISAL_EVIDENCE_TYPES:
            raise ValueError("unsupported appraisal evidence type")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
