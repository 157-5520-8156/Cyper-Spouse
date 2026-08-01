"""Source-bound authority events for a character's private impressions.

An impression is deliberately an *internal, revisable hypothesis*, not a fact
about the user.  Its model-authored reading remains closed over exact accepted
appraisal sources and never becomes World fact authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal

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


@dataclass(frozen=True, slots=True)
class PrivateImpressionReflectionBinding:
    source_ref: str
    source_kind: str
    authority_event_ref: str


class PrivateImpressionPredecessorRef(FrozenModel):
    """Exact active impression revision selected by the role model to replace."""

    impression_id: str = Field(min_length=1, max_length=512)
    expected_entity_revision: int = Field(ge=1)


def offered_private_impression_reflection_bindings(
    projection: object,
    *,
    appraisal: object,
) -> tuple[PrivateImpressionReflectionBinding, ...]:
    """Derive the exact bounded source manifest shown to reflection."""

    sources: list[PrivateImpressionReflectionBinding] = []

    def add(source_ref: str, source_kind: str, authority_event_ref: str) -> None:
        if not authority_event_ref or any(item.source_ref == source_ref for item in sources):
            return
        sources.append(
            PrivateImpressionReflectionBinding(
                source_ref=source_ref,
                source_kind=source_kind,
                authority_event_ref=authority_event_ref,
            )
        )

    subject_ref = getattr(appraisal, "subject_ref")
    related_appraisals = [
        item
        for item in getattr(projection, "appraisals")
        if item.status == "active" and item.subject_ref == subject_ref
    ]
    ordered_appraisals = [
        appraisal,
        *(
            item
            for item in reversed(related_appraisals)
            if item.appraisal_id != getattr(appraisal, "appraisal_id")
        ),
    ][:8]
    for item in ordered_appraisals:
        for hypothesis in item.hypotheses:
            add(
                f"appraisal:{item.appraisal_id}:{hypothesis.hypothesis_id}",
                "appraisal",
                item.origin.accepted_event_ref,
            )

    core = getattr(projection, "character_core")
    if core is not None and core.origin is not None:
        add(
            f"character-core:{core.core_id}:{core.entity_revision}",
            "character_core",
            core.origin.accepted_event_ref,
        )

    for relationship in reversed(getattr(projection, "relationship_states")):
        if relationship.subject_ref != subject_ref or relationship.origin is None:
            continue
        add(
            f"relationship:{relationship.relationship_id}:{relationship.entity_revision}",
            "relationship",
            relationship.origin.accepted_event_ref,
        )
        break

    appraisal_ids = {item.appraisal_id for item in related_appraisals}
    affects = [
        item
        for item in getattr(projection, "affect_episodes")
        if item.status == "active"
        and any(
            ref.appraisal_id in appraisal_ids
            for component in item.components
            for ref in component.appraisal_refs
        )
    ][-4:]
    for episode in reversed(affects):
        add(
            f"affect:{episode.episode_id}:{episode.entity_revision}",
            "affect",
            episode.origin.accepted_event_ref,
        )

    experiences = [
        item
        for item in getattr(projection, "experiences")
        if getattr(item, "status", None) == "committed"
        and subject_ref in item.values.participant_refs
    ][-4:]
    for experience in reversed(experiences):
        add(
            f"experience:{experience.experience_id}",
            "experience",
            experience.origin.accepted_event_ref,
        )

    impressions = [
        item
        for item in getattr(projection, "private_impressions")
        if item.status == "active"
        and item.subject_ref == subject_ref
        and item.origin is not None
    ][-6:]
    for impression in reversed(impressions):
        add(
            f"private-impression:{impression.impression_id}",
            "existing_impression",
            impression.origin.accepted_event_ref,
        )
    return tuple(sources)


class PrivateImpressionAuthorizedPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    transition_kind: Literal["open", "consolidate", "supersede"] = "open"
    expected_entity_revision: int = Field(ge=0)
    predecessor_refs: tuple[PrivateImpressionPredecessorRef, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    appraisal_refs: tuple[AppraisalMeaningRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)
    # Absent only on immutable legacy events. New commits are rejected by the
    # commit-batch boundary unless this complete role-reflection lineage is
    # present.
    reflection_contract: Literal[
        "private-impression-draft.3",
        "private-impression-draft.4",
    ] | None = None
    reflection_decision: Literal["retain", "consolidate", "supersede"] | None = None
    reflection_source_refs: tuple[str, ...] = ()
    source_model_result: str | None = Field(default=None, min_length=1, max_length=256)
    source_capsule_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

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
        reflection_lineage = (
            self.reflection_contract,
            self.reflection_source_refs,
            self.source_model_result,
            self.source_capsule_id,
        )
        if any(reflection_lineage) and not all(reflection_lineage):
            raise ValueError("private impression reflection lineage is incomplete")
        if (
            self.reflection_decision is not None
            and self.reflection_contract != "private-impression-draft.4"
        ):
            raise ValueError("private impression decision lacks its v4 reflection contract")
        if self.reflection_source_refs != tuple(dict.fromkeys(self.reflection_source_refs)):
            raise ValueError("private impression reflection source refs must be unique")
        predecessor_ids = tuple(item.impression_id for item in self.predecessor_refs)
        if predecessor_ids != tuple(dict.fromkeys(predecessor_ids)):
            raise ValueError("private impression predecessor refs must be unique")
        if self.reflection_contract == "private-impression-draft.3":
            if (
                self.transition_kind != "open"
                or self.predecessor_refs
                or self.reflection_decision is not None
            ):
                raise ValueError("private impression v3 reflection can only open an impression")
        elif self.reflection_contract == "private-impression-draft.4":
            if self.reflection_decision != (
                "retain" if self.transition_kind == "open" else self.transition_kind
            ):
                raise ValueError(
                    "private impression transition does not match the role-model decision"
                )
            if self.transition_kind == "open" and self.predecessor_refs:
                raise ValueError("new private impression cannot replace predecessors")
            if self.transition_kind != "open" and not self.predecessor_refs:
                raise ValueError("private impression replacement requires predecessors")
            selected_predecessors = {
                ref.removeprefix("private-impression:")
                for ref in self.reflection_source_refs
                if ref.startswith("private-impression:")
            }
            if any(item not in selected_predecessors for item in predecessor_ids):
                raise ValueError(
                    "private impression predecessors were not selected by the role model"
                )
        elif self.transition_kind != "open" or self.predecessor_refs:
            raise ValueError("legacy private impression cannot replace predecessors")
        return self


class PrivateImpressionAcceptedPayload(PrivateImpressionAuthorizedPayload):
    impression: PrivateImpressionProjection

    @model_validator(mode="after")
    def accepts_a_sourced_private_hypothesis(self) -> PrivateImpressionAcceptedPayload:
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
        if (
            self.transition_kind != "consolidate"
            and self.impression.first_seen != self.impression.last_supported
        ):
            raise ValueError("new private impression must have one authoritative support time")
        if self.reflection_contract is not None and self.impression.reflection_summary is None:
            raise ValueError("role-reflected private impression requires authored prose")
        if self.accepted_change_hash != private_impression_mutation_hash(self):
            raise ValueError("accepted change hash does not match private impression transition")
        return self


PRIVATE_IMPRESSION_PAYLOAD_MODELS = {
    "PrivateImpressionAccepted": PrivateImpressionAcceptedPayload,
}


def private_impression_mutation_hash(
    payload: PrivateImpressionAuthorizedPayload | Mapping[str, Any],
) -> str:
    material = private_impression_payload_material(payload)
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def private_impression_reflection_digest(
    payload: PrivateImpressionAcceptedPayload | Mapping[str, Any],
) -> str:
    """Bind the exact role-authored reflection carried by a typed mutation."""

    value = (
        payload
        if isinstance(payload, PrivateImpressionAcceptedPayload)
        else PrivateImpressionAcceptedPayload.model_validate(payload)
    )
    return private_impression_reflection_value_digest(
        decision=(
            value.reflection_decision
            if value.reflection_contract == "private-impression-draft.4"
            else None
        ),
        predecessor_refs=tuple(
            f"private-impression:{item.impression_id}" for item in value.predecessor_refs
        ),
        source_refs=value.reflection_source_refs,
        reflection_summary=value.impression.reflection_summary,
        confidence_bp=value.impression.confidence_bp,
        expiry_condition=value.impression.expiry_condition,
    )


def private_impression_reflection_value_digest(
    *,
    decision: str | None = None,
    predecessor_refs: tuple[str, ...] = (),
    source_refs: tuple[str, ...],
    reflection_summary: str | None,
    confidence_bp: int,
    expiry_condition: str,
) -> str:
    material = (
        {
            "decision": decision,
            "predecessor_refs": list(predecessor_refs),
            "source_refs": list(source_refs),
            "reflection_summary": reflection_summary,
            "confidence": confidence_bp,
            "expiry_condition": expiry_condition,
        }
        if decision is not None
        else {
            "retain": True,
            "source_refs": list(source_refs),
            "reflection_summary": reflection_summary,
            "confidence": confidence_bp,
            "expiry_condition": expiry_condition,
        }
    )
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def private_impression_payload_material(
    payload: PrivateImpressionAuthorizedPayload | Mapping[str, Any],
) -> dict[str, Any]:
    """Preserve pre-reflection event bytes while validating them with the new schema."""

    if not isinstance(payload, PrivateImpressionAuthorizedPayload):
        return to_jsonable_python(dict(payload))
    material = payload.model_dump(mode="json")
    if isinstance(payload, PrivateImpressionAcceptedPayload):
        impression = material.get("impression")
        if (
            isinstance(impression, dict)
            and payload.impression.reflection_summary is None
            and "reflection_summary" not in payload.impression.model_fields_set
        ):
            impression.pop("reflection_summary", None)
    for field in (
        "transition_kind",
        "predecessor_refs",
        "reflection_contract",
        "reflection_decision",
        "reflection_source_refs",
        "source_model_result",
        "source_capsule_id",
    ):
        if field not in payload.model_fields_set:
            material.pop(field, None)
    return material
