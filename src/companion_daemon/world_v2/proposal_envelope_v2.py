"""Pure Fact commit ProposalEnvelope v2 normalization contracts.

This module can turn inert normalization claims plus semantic Fact intents
into an inert proposal.  It deliberately has no acceptance, ledger, event,
reducer, compiler, or materialization dependency.

Normalization proves only structure, deterministic identity, and internal
consistency.  A later accepted compiler must independently re-prove ledger
evidence and installed policy authority.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .fact_accepted_contracts import (
    FactCommitIntentV2,
    canonical_fact_commit_intent_hash,
    canonical_fact_commit_intent_json,
    rehydrate_fact_commit_intent_v2,
    rehydrate_fact_commit_intent_v2_json,
)


MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES = 65_536
MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES = 262_144
MAX_FACT_COMMIT_PROPOSAL_NODES = 20_000
MAX_FACT_COMMIT_PROPOSAL_DEPTH = 32
MAX_FACT_COMMIT_INTENTS = 64
MAX_REF_LENGTH = 512
MAX_ID_LENGTH = 256

_PREFIXED_HASH = r"^sha256:[0-9a-f]{64}$"


class _V2Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class FactCommitProposalRegistryKeyV2(_V2Model):
    proposal_schema_registry: Literal["world-v2-proposals.2"]
    change_kind: Literal["fact_transition"]
    transition: Literal["commit"]
    payload_schema: Literal["fact_commit_intent.v2"]
    payload_version: Literal[2]


FACT_COMMIT_PROPOSAL_REGISTRY_V2 = (
    FactCommitProposalRegistryKeyV2(
        proposal_schema_registry="world-v2-proposals.2",
        change_kind="fact_transition",
        transition="commit",
        payload_schema="fact_commit_intent.v2",
        payload_version=2,
    ),
)


class FactCommitProposalEvidenceV2(_V2Model):
    ref_id: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    evidence_kind: Literal[
        "observed_message",
        "operator_observation",
        "committed_world_event",
        "settled_world_event",
        "settled_external_result",
        "committed_fact",
        "committed_experience",
    ]
    source_world_revision: int = Field(ge=1)
    immutable_hash: str = Field(pattern=_PREFIXED_HASH)


class FactCommitProposalNormalizationContextV2(_V2Model):
    world_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    proposal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    trigger_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    evaluated_world_revision: int = Field(ge=0)
    evidence_refs: tuple[FactCommitProposalEvidenceV2, ...] = Field(max_length=128)
    policy_refs: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def inert_claims_are_canonical(self) -> FactCommitProposalNormalizationContextV2:
        evidence_ids = tuple(item.ref_id for item in self.evidence_refs)
        if evidence_ids != tuple(sorted(set(evidence_ids))):
            raise ValueError("context evidence refs must be canonical and unique")
        if any(
            item.source_world_revision > self.evaluated_world_revision
            for item in self.evidence_refs
        ):
            raise ValueError("context evidence cannot come from a future revision")
        if self.policy_refs != tuple(sorted(set(self.policy_refs))) or any(
            not item or len(item) > MAX_REF_LENGTH for item in self.policy_refs
        ):
            raise ValueError("context policy claims must be canonical nonempty refs")
        return self


class FactCommitProposalDraftV2(_V2Model):
    fact_commit_intents: tuple[FactCommitIntentV2, ...] = Field(
        min_length=1, max_length=MAX_FACT_COMMIT_INTENTS
    )
    confidence: int = Field(ge=0, le=10_000)
    brief_rationale: str = Field(min_length=1, max_length=240)

    @field_validator("fact_commit_intents", mode="before")
    @classmethod
    def intents_are_rebuilt(cls, value: object) -> tuple[FactCommitIntentV2, ...]:
        material = _safe_material(
            value, max_bytes=MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES
        )
        if type(material) not in {tuple, list}:
            raise ValueError("fact commit intents must be an array")
        return tuple(rehydrate_fact_commit_intent_v2(item) for item in material)


class FactCommitCanonicalTypedPayloadV2(_V2Model):
    payload_schema: Literal["fact_commit_intent.v2"]
    payload_version: Literal[2]
    canonical_json: str = Field(min_length=2, max_length=MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def payload_is_exact(self) -> FactCommitCanonicalTypedPayloadV2:
        if len(self.canonical_json.encode("utf-8")) > MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES:
            raise ValueError("Fact commit payload exceeds UTF-8 byte budget")
        rehydrate_fact_commit_intent_v2_json(self.canonical_json)
        return self

    @property
    def payload_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


class FactCommitTypedChangeV2(_V2Model):
    change_id: str = Field(pattern=r"^change:[0-9a-f]{64}$")
    kind: Literal["fact_transition"]
    target_id: str = Field(pattern=r"^fact:[0-9a-f]{64}$")
    expected_entity_revision: Literal[0]
    transition: Literal["commit"]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    preconditions: tuple[()] = Field(min_length=0, max_length=0)
    policy_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    payload: FactCommitCanonicalTypedPayloadV2

    @model_validator(mode="after")
    def refs_are_canonical(self) -> FactCommitTypedChangeV2:
        for label, refs in (
            ("evidence", self.evidence_refs),
            ("policy", self.policy_refs),
        ):
            if refs != tuple(sorted(set(refs))) or any(
                not item or len(item) > MAX_REF_LENGTH for item in refs
            ):
                raise ValueError(f"Fact commit {label} refs must be canonical and unique")
        return self


class FactCommitProposalEnvelopeV2(_V2Model):
    proposal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    proposal_kind: Literal["decision"]
    trigger_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    evaluated_world_revision: int = Field(ge=0)
    schema_registry_version: Literal["world-v2-proposals.2"]
    evidence_refs: tuple[FactCommitProposalEvidenceV2, ...] = Field(
        min_length=1, max_length=128
    )
    proposed_changes: tuple[FactCommitTypedChangeV2, ...] = Field(
        min_length=1, max_length=MAX_FACT_COMMIT_INTENTS
    )
    action_intents: tuple[()] = Field(min_length=0, max_length=0)
    confidence: int = Field(ge=0, le=10_000)
    brief_rationale: str = Field(min_length=1, max_length=240)


class _GraphBudget:
    __slots__ = ("bytes", "max_bytes", "nodes", "visiting")

    def __init__(self, *, max_bytes: int) -> None:
        self.bytes = 0
        self.max_bytes = max_bytes
        self.nodes = 0
        self.visiting: set[int] = set()

    def consume_string(self, value: str) -> None:
        # Raw UTF-8 is a lower bound for its JSON spelling.  Reject an obvious
        # oversized field/key before asking the JSON encoder to allocate it.
        remaining = self.max_bytes - self.bytes
        if len(value) > remaining:
            raise ValueError("Fact commit proposal exceeds preflight UTF-8 byte budget")
        if len(value.encode("utf-8")) > remaining:
            raise ValueError("Fact commit proposal exceeds preflight UTF-8 byte budget")
        self.bytes += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if self.bytes > self.max_bytes:
            raise ValueError("Fact commit proposal exceeds preflight UTF-8 byte budget")


def _walk(value: object, *, budget: _GraphBudget, depth: int = 0) -> object:
    if depth > MAX_FACT_COMMIT_PROPOSAL_DEPTH:
        raise ValueError("Fact commit proposal is too deep")
    budget.nodes += 1
    if budget.nodes > MAX_FACT_COMMIT_PROPOSAL_NODES:
        raise ValueError("Fact commit proposal exceeds node budget")
    if type(value) is str:
        budget.consume_string(value)
        return value
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if value.bit_length() > 128:
            raise ValueError("Fact commit proposal integer is oversized")
        return value
    mappings: tuple[dict[object, object], ...] | None = None
    if isinstance(value, BaseModel):
        raw = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        if type(raw) is not dict or (extra is not None and type(extra) is not dict):
            raise ValueError("proposal model storage must use plain mappings")
        mappings = (raw,) if extra is None else (raw, extra)
        source: object = value
    else:
        source = value
        if type(source) is dict:
            mappings = (source,)
    if mappings is None and type(source) not in {tuple, list}:
        raise ValueError("Fact commit proposal contains unsupported material")
    identity = id(source)
    if identity in budget.visiting:
        raise ValueError("Fact commit proposal is cyclic")
    budget.visiting.add(identity)
    try:
        if mappings is not None:
            item_count = sum(len(mapping) for mapping in mappings)
            if item_count > MAX_FACT_COMMIT_PROPOSAL_NODES - budget.nodes:
                raise ValueError("Fact commit proposal exceeds node budget")
            output: dict[str, object] = {}
            for mapping in mappings:
                for key, item in mapping.items():
                    if type(key) is not str:
                        raise ValueError("Fact commit proposal object keys must be strings")
                    if key in output:
                        raise ValueError("proposal model field and extra key collision")
                    budget.consume_string(key)
                    output[key] = _walk(item, budget=budget, depth=depth + 1)
            return output
        if len(source) > MAX_FACT_COMMIT_PROPOSAL_NODES - budget.nodes:  # type: ignore[arg-type]
            raise ValueError("Fact commit proposal exceeds node budget")
        return tuple(_walk(item, budget=budget, depth=depth + 1) for item in source)
    finally:
        budget.visiting.remove(identity)


def _safe_material(value: object, *, max_bytes: int) -> object:
    return _walk(value, budget=_GraphBudget(max_bytes=max_bytes))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_model(value: object, model: type[_V2Model]) -> _V2Model:
    material = _safe_material(
        value, max_bytes=MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES
    )
    return model.model_validate_json(_canonical_json(material), strict=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _change_id(*, world_id: str, proposal_id: str, intent_hash: str) -> str:
    return "change:" + _digest(
        {
            "contract": "fact-commit-change-id.2",
            "world_id": world_id,
            "proposal_id": proposal_id,
            "intent_hash": intent_hash,
        }
    )


def _fact_id(
    *, world_id: str, proposal_id: str, change_id: str, intent_hash: str
) -> str:
    return "fact:" + _digest(
        {
            "contract": "fact-id.2",
            "world_id": world_id,
            "proposal_id": proposal_id,
            "change_id": change_id,
            "intent_hash": intent_hash,
        }
    )


def normalize_fact_commit_proposal_v2(
    *, draft: object, context: object
) -> FactCommitProposalEnvelopeV2:
    strict_draft = _strict_model(draft, FactCommitProposalDraftV2)
    strict_context = _strict_model(context, FactCommitProposalNormalizationContextV2)
    assert isinstance(strict_draft, FactCommitProposalDraftV2)
    assert isinstance(strict_context, FactCommitProposalNormalizationContextV2)

    ordered = sorted(
        (
            canonical_fact_commit_intent_hash(intent),
            rehydrate_fact_commit_intent_v2(intent),
        )
        for intent in strict_draft.fact_commit_intents
    )
    intent_hashes = tuple(item[0] for item in ordered)
    if len(intent_hashes) != len(set(intent_hashes)):
        raise ValueError("Fact commit proposal contains duplicate intent hash")

    evidence_by_ref = {item.ref_id: item for item in strict_context.evidence_refs}
    used_refs = tuple(
        sorted(
            {
                use.evidence_ref
                for _, intent in ordered
                for use in intent.evidence_uses
            }
        )
    )
    if any(ref not in evidence_by_ref for ref in used_refs):
        raise ValueError("Fact commit intent evidence does not uniquely resolve in context")
    for _, intent in ordered:
        assertion = evidence_by_ref[intent.assertion_source_ref]
        if assertion.evidence_kind not in {"observed_message", "operator_observation"}:
            raise ValueError("Fact assertion source is not an observation claim")

    changes: list[FactCommitTypedChangeV2] = []
    for intent_hash, intent in ordered:
        change_id = _change_id(
            world_id=strict_context.world_id,
            proposal_id=strict_context.proposal_id,
            intent_hash=intent_hash,
        )
        payload_json = canonical_fact_commit_intent_json(intent)
        evidence_refs = tuple(use.evidence_ref for use in intent.evidence_uses)
        changes.append(
            FactCommitTypedChangeV2(
                change_id=change_id,
                kind="fact_transition",
                target_id=_fact_id(
                    world_id=strict_context.world_id,
                    proposal_id=strict_context.proposal_id,
                    change_id=change_id,
                    intent_hash=intent_hash,
                ),
                expected_entity_revision=0,
                transition="commit",
                evidence_refs=evidence_refs,
                preconditions=(),
                policy_refs=strict_context.policy_refs,
                payload=FactCommitCanonicalTypedPayloadV2(
                    payload_schema="fact_commit_intent.v2",
                    payload_version=2,
                    canonical_json=payload_json,
                ),
            )
        )
    changes.sort(key=lambda item: item.change_id)
    proposal = FactCommitProposalEnvelopeV2(
        proposal_id=strict_context.proposal_id,
        proposal_kind="decision",
        trigger_ref=strict_context.trigger_ref,
        evaluated_world_revision=strict_context.evaluated_world_revision,
        schema_registry_version="world-v2-proposals.2",
        evidence_refs=tuple(evidence_by_ref[ref] for ref in used_refs),
        proposed_changes=tuple(changes),
        action_intents=(),
        confidence=strict_draft.confidence,
        brief_rationale=strict_draft.brief_rationale,
    )
    return validate_fact_commit_proposal_v2(proposal, world_id=strict_context.world_id)


def validate_fact_commit_proposal_v2(
    value: object, *, world_id: str
) -> FactCommitProposalEnvelopeV2:
    if type(world_id) is not str or not world_id or len(world_id) > MAX_ID_LENGTH:
        raise ValueError("Fact commit proposal world id is invalid")
    material = _safe_material(
        value, max_bytes=MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES
    )
    canonical = _canonical_json(material)
    if len(canonical.encode("utf-8")) > MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES:
        raise ValueError("Fact commit proposal envelope exceeds UTF-8 byte budget")
    proposal = FactCommitProposalEnvelopeV2.model_validate_json(canonical, strict=True)
    evidence_ids = tuple(item.ref_id for item in proposal.evidence_refs)
    if evidence_ids != tuple(sorted(set(evidence_ids))):
        raise ValueError("Fact commit proposal evidence must be canonical and unique")
    if any(
        item.source_world_revision > proposal.evaluated_world_revision
        for item in proposal.evidence_refs
    ):
        raise ValueError("Fact commit proposal evidence comes from a future revision")
    evidence_by_ref = {item.ref_id: item for item in proposal.evidence_refs}
    change_ids = tuple(change.change_id for change in proposal.proposed_changes)
    if change_ids != tuple(sorted(set(change_ids))):
        raise ValueError("Fact commit proposal changes must be sorted by unique change id")
    payload_hashes = tuple(change.payload.payload_hash for change in proposal.proposed_changes)
    if len(payload_hashes) != len(set(payload_hashes)):
        raise ValueError("Fact commit proposal payload hashes must be unique")
    policy_claims = {change.policy_refs for change in proposal.proposed_changes}
    if len(policy_claims) != 1:
        raise ValueError("Fact commit changes must claim the same canonical policy refs")
    used_refs: set[str] = set()
    for change in proposal.proposed_changes:
        if len(change.payload.canonical_json.encode("utf-8")) > MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES:
            raise ValueError("Fact commit payload exceeds UTF-8 byte budget")
        intent = rehydrate_fact_commit_intent_v2_json(change.payload.canonical_json)
        assertion = evidence_by_ref.get(intent.assertion_source_ref)
        if assertion is None or assertion.evidence_kind not in {
            "observed_message",
            "operator_observation",
        }:
            raise ValueError("Fact assertion source is not an observation claim")
        intent_hash = canonical_fact_commit_intent_hash(intent)
        expected_refs = tuple(item.evidence_ref for item in intent.evidence_uses)
        if change.evidence_refs != expected_refs:
            raise ValueError("Fact commit change evidence does not exactly match intent")
        expected_change_id = _change_id(
            world_id=world_id,
            proposal_id=proposal.proposal_id,
            intent_hash=intent_hash,
        )
        if change.change_id != expected_change_id or change.target_id != _fact_id(
            world_id=world_id,
            proposal_id=proposal.proposal_id,
            change_id=expected_change_id,
            intent_hash=intent_hash,
        ):
            raise ValueError("Fact commit change identity does not match the supplied world")
        used_refs.update(expected_refs)
    if evidence_ids != tuple(sorted(used_refs)):
        raise ValueError("Fact commit envelope evidence must exactly equal the used union")
    return proposal


def canonical_fact_commit_proposal_v2_json(value: object, *, world_id: str) -> str:
    proposal = validate_fact_commit_proposal_v2(value, world_id=world_id)
    canonical = _canonical_json(proposal.model_dump(mode="json"))
    if len(canonical.encode("utf-8")) > MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES:
        raise ValueError("Fact commit proposal envelope exceeds UTF-8 byte budget")
    return canonical


def canonical_fact_commit_proposal_v2_hash(value: object, *, world_id: str) -> str:
    encoded = canonical_fact_commit_proposal_v2_json(value, world_id=world_id).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def canonical_full_change_authority_hash_v2(value: object) -> str:
    strict = _strict_model(value, FactCommitTypedChangeV2)
    assert isinstance(strict, FactCommitTypedChangeV2)
    return _digest(
        {
            "contract": "manifest-change-authority.1",
            "change": strict.model_dump(mode="json"),
        }
    )


__all__ = [
    "FACT_COMMIT_PROPOSAL_REGISTRY_V2",
    "MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES",
    "MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES",
    "FactCommitProposalDraftV2",
    "FactCommitProposalEnvelopeV2",
    "FactCommitProposalNormalizationContextV2",
    "canonical_fact_commit_proposal_v2_hash",
    "canonical_fact_commit_proposal_v2_json",
    "canonical_full_change_authority_hash_v2",
    "normalize_fact_commit_proposal_v2",
    "validate_fact_commit_proposal_v2",
]
