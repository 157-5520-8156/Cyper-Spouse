"""Pure, inert Fact commit v2 intent and materialized payload contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .schema_core import FrozenModel, PrivacyClass
from .schemas import fact_conflict_key


MAX_FACT_INTENT_BYTES = 65_536
MAX_FACT_MATERIALIZED_BYTES = 65_536
MAX_FACT_CONTRACT_NODES = 8_192
MAX_FACT_CONTRACT_DEPTH = 16
MAX_FACT_CONTRACT_INTEGER_BITS = 128
MAX_FACT_EVIDENCE = 64
MAX_REF_LENGTH = 512
MAX_ID_LENGTH = 256

_HEX_PATTERN = r"^[0-9a-f]{64}$"
_PREFIXED_HEX_PATTERN = r"^sha256:[0-9a-f]{64}$"
_HEX_CHARS = frozenset("0123456789abcdef")
_INTENT_FIELDS = frozenset(
    {
        "subject_ref",
        "predicate_code",
        "value_ref",
        "value_hash",
        "assertion_source_ref",
        "evidence_uses",
        "confidence_bp",
        "privacy_class",
    }
)
_MATERIAL_FIELDS = frozenset(
    {
        "payload_contract",
        "change_id",
        "transition_id",
        "fact_id",
        "expected_entity_revision",
        "evidence_refs",
        "policy_refs",
        "acceptance_id",
        "proposal_id",
        "evaluated_world_revision",
        "full_change_authority_hash",
        "values",
    }
)
_OUTPUT_FIELDS = _MATERIAL_FIELDS | {"materialized_change_hash"}
_EVIDENCE_USE_FIELDS = frozenset({"evidence_ref", "purpose", "anchor"})
_FACT_VALUE_FIELDS = frozenset(
    {
        "subject_ref",
        "predicate_code",
        "cardinality",
        "conflict_key",
        "value_ref",
        "value_hash",
        "assertion_binding",
        "anchor_evidence_refs",
        "source_evidence_refs",
        "confidence_bp",
        "privacy_class",
        "status",
        "withdrawal_reason_code",
        "withdrawal_evidence_ref",
    }
)
_ASSERTION_FIELDS = frozenset(
    {
        "source_kind",
        "source_ref",
        "asserted_subject_ref",
        "actor_ref",
        "channel",
        "payload_ref",
        "content_payload_hash",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "ref_id",
        "evidence_type",
        "claim_purpose",
        "source_world_revision",
        "immutable_hash",
    }
)
_RESOLVED_EVIDENCE_TYPES = frozenset(
    {
        "observed_message",
        "operator_observation",
        "committed_world_event",
        "settled_world_event",
        "settled_external_result",
        "committed_fact",
        "committed_experience",
    }
)
_FACT_PURPOSES = frozenset(
    {
        "current_fact",
        "past_experience",
        "future_plan",
        "private_hypothesis",
        "conversation_continuity",
    }
)


def _is_hex(value: object) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _HEX_CHARS


class FactEvidenceUseV2(FrozenModel):
    evidence_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    purpose: Literal[
        "current_fact",
        "past_experience",
        "future_plan",
        "private_hypothesis",
        "conversation_continuity",
    ]
    anchor: bool


class FactCommitIntentV2(FrozenModel):
    subject_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    predicate_code: str = Field(min_length=1, max_length=128)
    value_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    value_hash: str = Field(pattern=_PREFIXED_HEX_PATTERN)
    assertion_source_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    evidence_uses: tuple[FactEvidenceUseV2, ...] = Field(
        min_length=1, max_length=MAX_FACT_EVIDENCE
    )
    confidence_bp: int = Field(ge=1, le=10_000)
    privacy_class: PrivacyClass

    @field_validator("evidence_uses", mode="before")
    @classmethod
    def evidence_uses_are_rebuilt(
        cls, value: object
    ) -> tuple[FactEvidenceUseV2, ...]:
        material = _safe_material(value, max_bytes=MAX_FACT_INTENT_BYTES)
        if type(material) not in {tuple, list}:
            raise ValueError("Fact intent evidence uses must be an array")
        return tuple(
            FactEvidenceUseV2.model_validate(
                _require_exact_fields(
                    item, _EVIDENCE_USE_FIELDS, label="FactEvidenceUseV2"
                ),
                strict=True,
            )
            for item in material
        )

    @model_validator(mode="after")
    def evidence_contract_is_closed(self) -> FactCommitIntentV2:
        refs = tuple(item.evidence_ref for item in self.evidence_uses)
        if refs != tuple(sorted(refs)):
            raise ValueError("Fact intent evidence refs must be sorted")
        if len(refs) != len(set(refs)):
            raise ValueError("Fact intent evidence refs must be globally unique")
        assertion = next(
            (item for item in self.evidence_uses if item.evidence_ref == self.assertion_source_ref),
            None,
        )
        if assertion is None or assertion.purpose != "current_fact" or not assertion.anchor:
            raise ValueError("Fact intent assertion source must be a current_fact anchor")
        return self


class ResolvedFactEvidenceV2(FrozenModel):
    """Closed, revision-pinned evidence admitted by a Fact commit v2 reader."""

    ref_id: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    evidence_type: Literal[
        "observed_message",
        "operator_observation",
        "committed_world_event",
        "settled_world_event",
        "settled_external_result",
        "committed_fact",
        "committed_experience",
    ]
    claim_purpose: Literal[
        "current_fact",
        "past_experience",
        "future_plan",
        "private_hypothesis",
        "conversation_continuity",
    ]
    source_world_revision: int = Field(ge=1)
    immutable_hash: str = Field(pattern=_HEX_PATTERN)


class FactAssertionBindingV2(FrozenModel):
    source_kind: Literal["observed_message", "operator_observation"]
    source_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    asserted_subject_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    actor_ref: str | None = Field(max_length=MAX_REF_LENGTH)
    channel: str | None = Field(max_length=MAX_REF_LENGTH)
    payload_ref: str | None = Field(max_length=MAX_REF_LENGTH)
    content_payload_hash: str = Field(pattern=_HEX_PATTERN)

    @model_validator(mode="after")
    def source_shape_is_closed(self) -> FactAssertionBindingV2:
        retained = (self.actor_ref, self.channel, self.payload_ref)
        if self.source_kind == "observed_message" and any(
            item is None or item == "" for item in retained
        ):
            raise ValueError("message fact assertion requires its retained envelope")
        if self.source_kind == "operator_observation" and any(
            item is not None for item in retained
        ):
            raise ValueError("operator fact assertion cannot claim message envelope fields")
        return self


class FactCommitValuesV2(FrozenModel):
    """Exact active Fact value image produced by the trusted v2 adapter."""

    subject_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    predicate_code: str = Field(min_length=1, max_length=128)
    cardinality: Literal["single", "set"]
    conflict_key: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    value_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    value_hash: str = Field(pattern=_HEX_PATTERN)
    assertion_binding: FactAssertionBindingV2
    anchor_evidence_refs: tuple[ResolvedFactEvidenceV2, ...] = Field(
        min_length=1, max_length=MAX_FACT_EVIDENCE
    )
    source_evidence_refs: tuple[ResolvedFactEvidenceV2, ...] = Field(
        min_length=1, max_length=MAX_FACT_EVIDENCE
    )
    confidence_bp: int = Field(ge=1, le=10_000)
    privacy_class: PrivacyClass
    status: Literal["active"]
    withdrawal_reason_code: None
    withdrawal_evidence_ref: None

    @field_validator("assertion_binding", mode="before")
    @classmethod
    def assertion_is_rebuilt(cls, value: object) -> FactAssertionBindingV2:
        return _strict_assertion(value)

    @field_validator("anchor_evidence_refs", "source_evidence_refs", mode="before")
    @classmethod
    def evidence_is_rebuilt(
        cls, value: object
    ) -> tuple[ResolvedFactEvidenceV2, ...]:
        material = _safe_material(value, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
        if type(material) not in {tuple, list}:
            raise ValueError("Fact values evidence must be an array")
        return tuple(_strict_evidence(item) for item in material)

    @model_validator(mode="after")
    def fact_value_authority_is_closed(self) -> FactCommitValuesV2:
        _validate_fact_values(self)
        return self


class _FactCommitMaterializedHashMaterialV2(FrozenModel):
    payload_contract: Literal["fact-commit-materialized.2"]
    change_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    transition_id: str = Field(pattern=r"^fact-transition:[0-9a-f]{64}$")
    fact_id: str = Field(pattern=r"^fact:[0-9a-f]{64}$")
    expected_entity_revision: Literal[0]
    evidence_refs: tuple[ResolvedFactEvidenceV2, ...] = Field(
        min_length=1, max_length=MAX_FACT_EVIDENCE
    )
    policy_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    acceptance_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    proposal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    evaluated_world_revision: int = Field(ge=0)
    full_change_authority_hash: str = Field(pattern=_HEX_PATTERN)
    values: FactCommitValuesV2

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def evidence_wire_fields_are_explicit(cls, value: object) -> object:
        material = _safe_material(value, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
        if type(material) not in {tuple, list}:
            raise ValueError("materialized evidence refs must be an array")
        for item in material:
            _require_exact_fields(item, _EVIDENCE_FIELDS, label="resolved evidence")
        return tuple(_strict_evidence(item) for item in material)

    @field_validator("values", mode="before")
    @classmethod
    def value_wire_fields_are_explicit(cls, value: object) -> object:
        material = _safe_material(value, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
        values = _require_exact_fields(material, _FACT_VALUE_FIELDS, label="FactValues")
        _require_exact_fields(
            values.get("assertion_binding"),
            _ASSERTION_FIELDS,
            label="Fact assertion binding",
        )
        for name in ("anchor_evidence_refs", "source_evidence_refs"):
            items = values.get(name)
            if type(items) not in {tuple, list}:
                raise ValueError(f"FactValues {name} must be an array")
            for item in items:
                _require_exact_fields(item, _EVIDENCE_FIELDS, label="resolved evidence")
        return _strict_values(value)

    @model_validator(mode="after")
    def resolved_authority_is_closed(self) -> _FactCommitMaterializedHashMaterialV2:
        _validate_materialized_components(self)
        return self


class FactCommitMaterializedPayloadV2(_FactCommitMaterializedHashMaterialV2):
    materialized_change_hash: str = Field(pattern=_HEX_PATTERN)

    @model_validator(mode="after")
    def self_hash_is_exact(self) -> FactCommitMaterializedPayloadV2:
        if self.materialized_change_hash != canonical_fact_commit_materialized_hash(self):
            raise ValueError("materialized change hash does not match canonical payload")
        return self


class _MaterialBudget:
    __slots__ = ("bytes", "max_bytes", "nodes", "visiting")

    def __init__(self, max_bytes: int) -> None:
        self.bytes = 0
        self.nodes = 0
        self.max_bytes = max_bytes
        self.visiting: set[int] = set()

    def consume(self, byte_count: int) -> None:
        self.bytes += byte_count
        self.nodes += 1
        # This walk bounds structure before encoding. The authoritative byte
        # budget is checked against the completed canonical UTF-8 document.
        if (
            self.bytes > (self.max_bytes * 4) + 4_096
            or self.nodes > MAX_FACT_CONTRACT_NODES
        ):
            raise ValueError("Fact contract material budget exceeded")


def _model_storage(value: BaseModel) -> dict[str, object]:
    stored = object.__getattribute__(value, "__dict__")
    if type(stored) is not dict:
        raise ValueError("Fact contract model storage must be a plain object")
    output = dict(stored)
    extra = object.__getattribute__(value, "__pydantic_extra__")
    if extra is not None:
        if type(extra) is not dict:
            raise ValueError("Fact contract extras must be a plain object")
        output.update(extra)
    return output


def _walk_material(
    value: object, *, budget: _MaterialBudget, depth: int = 0
) -> object:
    if depth > MAX_FACT_CONTRACT_DEPTH:
        raise ValueError("Fact contract material is too deep")
    if value is None or type(value) is bool:
        budget.consume(8)
        return value
    if type(value) is int:
        if value.bit_length() > MAX_FACT_CONTRACT_INTEGER_BITS:
            raise ValueError("Fact contract integer exceeds its budget")
        budget.consume(max(2, len(str(value))))
        return value
    if type(value) is str:
        budget.consume(len(json.dumps(value, ensure_ascii=False).encode("utf-8")))
        return value
    source: object = _model_storage(value) if isinstance(value, BaseModel) else value
    if type(source) not in {dict, tuple, list}:
        raise ValueError("Fact contract contains unsupported material")
    identity = id(source)
    if identity in budget.visiting:
        raise ValueError("Fact contract material is cyclic")
    budget.visiting.add(identity)
    budget.consume(2)
    try:
        if type(source) is dict:
            output: dict[str, object] = {}
            for key, child in source.items():
                if type(key) is not str:
                    raise ValueError("Fact contract object keys must be strings")
                budget.consume(len(json.dumps(key, ensure_ascii=False).encode("utf-8")) + 1)
                output[key] = _walk_material(child, budget=budget, depth=depth + 1)
            return output
        return tuple(
            _walk_material(child, budget=budget, depth=depth + 1) for child in source
        )
    finally:
        budget.visiting.remove(identity)


def _safe_material(value: object, *, max_bytes: int) -> object:
    material = _walk_material(value, budget=_MaterialBudget(max_bytes))
    _enforce_canonical_byte_budget(material, max_bytes=max_bytes)
    return material


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _enforce_canonical_byte_budget(value: object, *, max_bytes: int) -> None:
    if len(_canonical_json(value).encode("utf-8")) > max_bytes:
        raise ValueError("Fact contract canonical UTF-8 byte budget exceeded")


def _require_exact_fields(
    raw: object, expected: frozenset[str], *, label: str
) -> dict[str, object]:
    if type(raw) is not dict:
        raise ValueError(f"{label} must be an object")
    if set(raw) != expected:
        raise ValueError(f"{label} fields must be explicit and exact")
    return raw


def _strict_evidence(value: object) -> ResolvedFactEvidenceV2:
    raw = _safe_material(value, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
    raw = _require_exact_fields(raw, _EVIDENCE_FIELDS, label="resolved evidence")
    return ResolvedFactEvidenceV2.model_validate(raw, strict=True)


def _strict_assertion(value: object) -> FactAssertionBindingV2:
    raw = _safe_material(value, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
    raw = _require_exact_fields(raw, _ASSERTION_FIELDS, label="Fact assertion binding")
    return FactAssertionBindingV2.model_validate(raw, strict=True)


def _strict_values(value: object) -> FactCommitValuesV2:
    raw = _safe_material(value, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
    raw = _require_exact_fields(raw, _FACT_VALUE_FIELDS, label="FactValues")
    assertion = _strict_assertion(raw["assertion_binding"])
    anchors_raw = raw["anchor_evidence_refs"]
    sources_raw = raw["source_evidence_refs"]
    if type(anchors_raw) not in {tuple, list} or type(sources_raw) not in {tuple, list}:
        raise ValueError("FactValues evidence must be arrays")
    raw["assertion_binding"] = assertion
    raw["anchor_evidence_refs"] = tuple(_strict_evidence(item) for item in anchors_raw)
    raw["source_evidence_refs"] = tuple(_strict_evidence(item) for item in sources_raw)
    return FactCommitValuesV2.model_validate(raw, strict=True)


def _validate_fact_values(values: FactCommitValuesV2) -> None:
    if values.conflict_key != fact_conflict_key(
        subject_ref=values.subject_ref, predicate_code=values.predicate_code
    ):
        raise ValueError("Fact conflict key must derive from its semantic slot")
    sources = tuple(item.ref_id for item in values.source_evidence_refs)
    anchors = tuple(item.ref_id for item in values.anchor_evidence_refs)
    if sources != tuple(sorted(sources)) or len(sources) != len(set(sources)):
        raise ValueError("Fact source evidence refs must be sorted and globally unique")
    if anchors != tuple(sorted(anchors)) or len(anchors) != len(set(anchors)):
        raise ValueError("Fact anchor evidence refs must be sorted and globally unique")
    if any(anchor not in values.source_evidence_refs for anchor in values.anchor_evidence_refs):
        raise ValueError("Fact anchor evidence must exactly exist in source evidence")
    assertion_source = values.assertion_binding.source_ref
    if (
        values.assertion_binding.asserted_subject_ref != values.subject_ref
        or assertion_source not in anchors
        or assertion_source not in sources
    ):
        raise ValueError("Fact assertion must bind subject and source evidence")
    assertion_evidence = next(item for item in values.source_evidence_refs if item.ref_id == assertion_source)
    expected_type = (
        "observed_message"
        if values.assertion_binding.source_kind == "observed_message"
        else "operator_observation"
    )
    if assertion_evidence.evidence_type != expected_type:
        raise ValueError("Fact assertion source kind does not match resolved evidence")
    if assertion_evidence.claim_purpose != "current_fact":
        raise ValueError("Fact assertion source evidence must serve current_fact")


def _validate_materialized_components(
    value: _FactCommitMaterializedHashMaterialV2,
) -> None:
    strict_evidence = tuple(_strict_evidence(item) for item in value.evidence_refs)
    strict_values = _strict_values(value.values)
    refs = tuple(item.ref_id for item in strict_evidence)
    if refs != tuple(sorted(refs)):
        raise ValueError("materialized evidence refs must be sorted")
    if len(refs) != len(set(refs)):
        raise ValueError("materialized evidence refs must be globally unique")
    if strict_evidence != strict_values.source_evidence_refs:
        raise ValueError("materialized evidence must equal FactValues source evidence")
    if value.policy_refs != tuple(sorted(value.policy_refs)):
        raise ValueError("materialized policy refs must be sorted")
    if len(value.policy_refs) != len(set(value.policy_refs)) or any(
        not item or len(item) > MAX_REF_LENGTH for item in value.policy_refs
    ):
        raise ValueError("materialized policy refs must be unique bounded refs")


def _rehydrate_intent_material(value: object) -> FactCommitIntentV2:
    raw = _safe_material(value, max_bytes=MAX_FACT_INTENT_BYTES)
    raw = _require_exact_fields(raw, _INTENT_FIELDS, label="FactCommitIntentV2")
    uses = raw["evidence_uses"]
    if type(uses) not in {tuple, list}:
        raise ValueError("Fact intent evidence uses must be an array")
    raw["evidence_uses"] = tuple(
        FactEvidenceUseV2.model_validate(item, strict=True) for item in uses
    )
    return FactCommitIntentV2.model_validate(raw, strict=True)


def rehydrate_fact_commit_intent_v2(value: object) -> FactCommitIntentV2:
    return _rehydrate_intent_material(value)


def canonical_fact_commit_intent_json(value: object) -> str:
    intent = _rehydrate_intent_material(value)
    canonical = _canonical_json(intent.model_dump(mode="json"))
    _enforce_canonical_byte_budget(
        intent.model_dump(mode="json"), max_bytes=MAX_FACT_INTENT_BYTES
    )
    return canonical


def canonical_fact_commit_intent_hash(value: object) -> str:
    encoded = canonical_fact_commit_intent_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def rehydrate_fact_commit_intent_v2_json(payload_json: str) -> FactCommitIntentV2:
    decoded = _decode_canonical_json(payload_json, max_bytes=MAX_FACT_INTENT_BYTES)
    intent = _rehydrate_intent_material(decoded)
    if payload_json != _canonical_json(intent.model_dump(mode="json")):
        raise ValueError("Fact intent JSON must be canonical")
    return intent


def _rehydrate_hash_material(value: object) -> _FactCommitMaterializedHashMaterialV2:
    raw = _safe_material(value, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
    if type(raw) is not dict:
        raise ValueError("Fact materialized payload must be an object")
    raw.pop("materialized_change_hash", None)
    raw = _require_exact_fields(raw, _MATERIAL_FIELDS, label="Fact materialized hash material")
    _require_exact_nested_wire_fields(raw)
    return _FactCommitMaterializedHashMaterialV2.model_validate_json(
        _canonical_json(raw), strict=True
    )


def _require_exact_nested_wire_fields(raw: dict[str, object]) -> None:
    values = _require_exact_fields(raw.get("values"), _FACT_VALUE_FIELDS, label="FactValues")
    _require_exact_fields(
        values.get("assertion_binding"), _ASSERTION_FIELDS, label="Fact assertion binding"
    )
    for name in ("anchor_evidence_refs", "source_evidence_refs"):
        items = values.get(name)
        if type(items) not in {tuple, list}:
            raise ValueError(f"FactValues {name} must be an array")
        for item in items:
            _require_exact_fields(item, _EVIDENCE_FIELDS, label="resolved evidence")
    evidence = raw.get("evidence_refs")
    if type(evidence) not in {tuple, list}:
        raise ValueError("materialized evidence refs must be an array")
    for item in evidence:
        _require_exact_fields(item, _EVIDENCE_FIELDS, label="resolved evidence")


def canonical_fact_commit_materialized_hash(value: object) -> str:
    material = _rehydrate_hash_material(value)
    encoded = _canonical_json(
        {
            "contract": "fact-commit-materialized-hash.2",
            "payload": material.model_dump(mode="json"),
        }
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def rehydrate_fact_commit_materialized_v2(
    value: object,
) -> FactCommitMaterializedPayloadV2:
    raw = _safe_material(value, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
    raw = _require_exact_fields(raw, _OUTPUT_FIELDS, label="FactCommitMaterializedPayloadV2")
    _require_exact_nested_wire_fields(raw)
    return FactCommitMaterializedPayloadV2.model_validate_json(
        _canonical_json(raw), strict=True
    )


def canonical_fact_commit_materialized_json(value: object) -> str:
    payload = rehydrate_fact_commit_materialized_v2(value)
    material = payload.model_dump(mode="json")
    _enforce_canonical_byte_budget(material, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
    return _canonical_json(material)


def rehydrate_fact_commit_materialized_v2_json(
    payload_json: str,
) -> FactCommitMaterializedPayloadV2:
    decoded = _decode_canonical_json(payload_json, max_bytes=MAX_FACT_MATERIALIZED_BYTES)
    payload = rehydrate_fact_commit_materialized_v2(decoded)
    if payload_json != _canonical_json(payload.model_dump(mode="json")):
        raise ValueError("Fact materialized JSON must be canonical")
    return payload


def fact_commit_event_payload_hash(value: object) -> str:
    return hashlib.sha256(
        canonical_fact_commit_materialized_json(value).encode("utf-8")
    ).hexdigest()


def fact_commit_transition_id_v2(
    *,
    world_id: str,
    proposal_id: str,
    change_id: str,
    full_change_authority_hash: str,
    fact_id: str,
) -> str:
    for label, item in (
        ("world_id", world_id),
        ("proposal_id", proposal_id),
        ("change_id", change_id),
        ("fact_id", fact_id),
    ):
        if not item or len(item) > MAX_REF_LENGTH:
            raise ValueError(f"{label} is not a bounded ref")
    if not _is_hex(full_change_authority_hash):
        raise ValueError("full change authority hash must be lowercase SHA-256")
    encoded = _canonical_json(
        {
            "contract": "fact-commit-transition-id.2",
            "world_id": world_id,
            "proposal_id": proposal_id,
            "change_id": change_id,
            "full_change_authority_hash": full_change_authority_hash,
            "fact_id": fact_id,
            "expected_entity_revision": 0,
        }
    ).encode("utf-8")
    return f"fact-transition:{hashlib.sha256(encoded).hexdigest()}"


def _decode_canonical_json(payload_json: str, *, max_bytes: int) -> object:
    if type(payload_json) is not str:
        raise TypeError("Fact contract JSON must be a string")
    encoded = payload_json.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError("Fact contract JSON exceeds byte budget")
    try:
        return json.loads(payload_json)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Fact contract JSON is invalid") from exc


__all__ = [
    "MAX_FACT_INTENT_BYTES",
    "MAX_FACT_MATERIALIZED_BYTES",
    "FactCommitIntentV2",
    "FactCommitMaterializedPayloadV2",
    "FactCommitValuesV2",
    "FactEvidenceUseV2",
    "ResolvedFactEvidenceV2",
    "canonical_fact_commit_intent_hash",
    "canonical_fact_commit_intent_json",
    "canonical_fact_commit_materialized_hash",
    "canonical_fact_commit_materialized_json",
    "fact_commit_event_payload_hash",
    "fact_commit_transition_id_v2",
    "rehydrate_fact_commit_intent_v2",
    "rehydrate_fact_commit_intent_v2_json",
    "rehydrate_fact_commit_materialized_v2",
    "rehydrate_fact_commit_materialized_v2_json",
]
