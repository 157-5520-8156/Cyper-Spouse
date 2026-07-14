"""Typed proposal-authorized CharacterCore C1 mutation contracts."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import (
    CharacterCoreAuthorityLane,
    CharacterCoreEvidenceBinding,
    CharacterCoreEvidenceWindow,
    CharacterCoreFieldClass,
    CharacterCoreOperatorAuthorityBinding,
    CharacterCoreProjection,
    EvidenceRef,
    FrozenModel,
)


class CharacterCoreCompensationTarget(FrozenModel):
    transition_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_world_revision: int = Field(ge=1)
    accepted_payload_hash: str = Field(min_length=64, max_length=64)


class CharacterCoreChangedPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)
    operation: Literal["initialize", "revise", "compensate"]
    authority_lane: CharacterCoreAuthorityLane
    changed_field_classes: tuple[CharacterCoreFieldClass, ...] = Field(min_length=1)
    core_before: CharacterCoreProjection | None = None
    core_after: CharacterCoreProjection
    evidence_window: CharacterCoreEvidenceWindow | None = None
    operator_authority: CharacterCoreOperatorAuthorityBinding | None = None
    compensation_target: CharacterCoreCompensationTarget | None = None
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def mutation_is_canonical(self) -> CharacterCoreChangedPayload:
        if self.accepted_change_hash != character_core_mutation_hash(self):
            raise ValueError("character core accepted change hash is invalid")
        if self.change_id != self.core_after.origin.change_id or (
            self.transition_id != self.core_after.origin.transition_id
        ):
            raise ValueError("character core mutation ids do not match after origin")
        if self.policy_refs != self.core_after.origin.policy_refs:
            raise ValueError("character core policy refs do not match after origin")
        if tuple(sorted(self.changed_field_classes)) != self.changed_field_classes or len(
            self.changed_field_classes
        ) != len(set(self.changed_field_classes)):
            raise ValueError("character core changed field classes must be sorted and unique")
        if self.operation == "initialize":
            if self.expected_entity_revision != 0 or self.core_before is not None:
                raise ValueError("character core initialize must create revision one")
            if self.authority_lane != "operator_initialize":
                raise ValueError("character core initialize requires operator lane")
        elif self.core_before is None or self.expected_entity_revision < 1:
            raise ValueError("character core transition requires a before image")
        if self.operation == "compensate":
            if self.authority_lane != "compensation" or self.compensation_target is None:
                raise ValueError("character core compensation requires exact target authority")
        elif self.compensation_target is not None:
            raise ValueError("non-compensation cannot name a compensation target")
        if self.authority_lane == "longitudinal_evolution":
            if self.evidence_window is None or self.operator_authority is not None:
                raise ValueError("longitudinal core revision requires only an evidence window")
        elif self.authority_lane in {"operator_initialize", "operator_revision"}:
            if self.operator_authority is None or self.evidence_window is not None:
                raise ValueError("operator core mutation requires only actor authority")
        elif self.authority_lane == "compensation" and self.evidence_window is not None:
            raise ValueError("compensation reverts lineage and cannot claim new evolution evidence")
        if self.evidence_refs != character_core_evidence_refs(self):
            raise ValueError("character core EvidenceRefs are not exact mutation authority")
        return self


CHARACTER_CORE_PAYLOAD_MODELS = {
    "CharacterCoreInitialized": CharacterCoreChangedPayload,
    "CharacterCoreRevised": CharacterCoreChangedPayload,
    "CharacterCoreRevisionCompensated": CharacterCoreChangedPayload,
}


def character_core_source_evidence(binding: CharacterCoreEvidenceBinding) -> EvidenceRef:
    return EvidenceRef(
        ref_id=binding.authority_event_ref,
        evidence_type=("committed_fact" if binding.source_kind == "fact" else "committed_experience"),
        claim_purpose=("current_fact" if binding.source_kind == "fact" else "past_experience"),
        source_world_revision=binding.authority_world_revision,
        immutable_hash=binding.source_values_hash,
    )


def character_core_operator_evidence(
    binding: CharacterCoreOperatorAuthorityBinding,
) -> EvidenceRef:
    return EvidenceRef(
        ref_id=binding.authority_event_ref,
        evidence_type="committed_world_event",
        claim_purpose="action_authorization",
        source_world_revision=binding.authority_world_revision,
        immutable_hash=binding.authority_payload_hash,
    )


def character_core_evidence_refs(
    payload: CharacterCoreChangedPayload | Mapping[str, Any],
) -> tuple[EvidenceRef, ...]:
    value = (
        payload
        if isinstance(payload, CharacterCoreChangedPayload)
        else CharacterCoreChangedPayload.model_construct(**dict(payload))
    )
    refs: list[EvidenceRef] = []
    if value.evidence_window is not None:
        refs.extend(character_core_source_evidence(item) for item in value.evidence_window.source_bindings)
    if value.operator_authority is not None:
        refs.append(character_core_operator_evidence(value.operator_authority))
    if value.compensation_target is not None:
        refs.append(
            EvidenceRef(
                ref_id=value.compensation_target.accepted_event_ref,
                evidence_type="committed_world_event",
                claim_purpose="conversation_continuity",
                source_world_revision=value.compensation_target.accepted_world_revision,
                immutable_hash=value.compensation_target.accepted_payload_hash,
            )
        )
    return tuple(refs)


def character_core_mutation_hash(
    payload: CharacterCoreChangedPayload | Mapping[str, Any],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, CharacterCoreChangedPayload)
        else to_jsonable_python(dict(payload))
    )
    material.pop("accepted_change_hash", None)
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
