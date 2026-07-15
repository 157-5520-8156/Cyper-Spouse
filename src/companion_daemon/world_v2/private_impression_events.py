"""Source-bound authority events for a character's private impressions.

An impression is deliberately an *internal, revisable hypothesis*, not a fact
about the user.  Its readable content is a set of accepted appraisal-hypothesis
references; free-form model prose is never persisted on this authority path.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import AppraisalMeaningRef, EvidenceRef, FrozenModel, PrivateImpressionProjection


PRIVATE_IMPRESSION_POLICY_REFS = ("policy:private-impression.1",)
_ALLOWED_EVIDENCE_TYPES = {
    "committed_fact",
    "committed_experience",
    "committed_world_event",
    "settled_world_event",
    "settled_external_result",
    "observed_message",
    "active_plan",
    "clock_observation",
}


class PrivateImpressionAuthorizedPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    appraisal_refs: tuple[AppraisalMeaningRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def evidence_and_policy_are_narrow(self) -> PrivateImpressionAuthorizedPayload:
        if self.policy_refs != PRIVATE_IMPRESSION_POLICY_REFS:
            raise ValueError("private impression references an uninstalled policy")
        if len(self.evidence_refs) != len({item.ref_id for item in self.evidence_refs}):
            raise ValueError("private impression evidence refs must be unique")
        if any(
            item.evidence_type not in _ALLOWED_EVIDENCE_TYPES
            or item.claim_purpose != "private_hypothesis"
            for item in self.evidence_refs
        ):
            raise ValueError("private impression requires sourced private-hypothesis evidence")
        if len(self.appraisal_refs) != len(
            {(item.appraisal_id, item.hypothesis_id) for item in self.appraisal_refs}
        ):
            raise ValueError("private impression appraisal refs must be unique")
        return self


class PrivateImpressionAcceptedPayload(PrivateImpressionAuthorizedPayload):
    impression: PrivateImpressionProjection

    @model_validator(mode="after")
    def opens_a_sourced_private_hypothesis(self) -> PrivateImpressionAcceptedPayload:
        if self.expected_entity_revision != 0:
            raise ValueError("private impression acceptance must create revision one")
        if self.impression.entity_revision != 1 or self.impression.status != "active":
            raise ValueError("private impression acceptance must create an active impression")
        if self.impression.origin is None:
            raise ValueError("private impression acceptance requires an origin")
        if (
            self.impression.origin.change_id != self.change_id
            or self.impression.origin.transition_id != self.transition_id
            or self.impression.origin.policy_refs != self.policy_refs
        ):
            raise ValueError("private impression origin does not match authority")
        if len(self.impression.source_refs) != len(self.evidence_refs):
            raise ValueError("private impression must retain one committed source per evidence ref")
        expected_interpretations = tuple(
            f"appraisal:{item.appraisal_id}:{item.hypothesis_id}" for item in self.appraisal_refs
        )
        if self.impression.interpretation_refs != expected_interpretations:
            raise ValueError("private impression interpretations must be appraisal references")
        if self.impression.first_seen != self.impression.last_supported:
            raise ValueError("new private impression must have one authoritative support time")
        if self.accepted_change_hash != private_impression_mutation_hash(self):
            raise ValueError("accepted change hash does not match private impression transition")
        return self


PRIVATE_IMPRESSION_PAYLOAD_MODELS = {
    "PrivateImpressionAccepted": PrivateImpressionAcceptedPayload,
}


def private_impression_mutation_hash(
    payload: PrivateImpressionAuthorizedPayload | Mapping[str, Any],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, PrivateImpressionAuthorizedPayload)
        else to_jsonable_python(dict(payload))
    )
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
