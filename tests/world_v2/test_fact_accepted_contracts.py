from __future__ import annotations

from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.fact_accepted_contracts import (
    MAX_FACT_INTENT_BYTES,
    MAX_FACT_MATERIALIZED_BYTES,
    FactCommitIntentV2,
    FactCommitMaterializedPayloadV2,
    FactCommitValuesV2,
    FactEvidenceUseV2,
    ResolvedFactEvidenceV2,
    canonical_fact_commit_intent_hash,
    canonical_fact_commit_intent_json,
    canonical_fact_commit_materialized_hash,
    canonical_fact_commit_materialized_json,
    fact_commit_event_payload_hash,
    fact_commit_transition_id_v2,
    rehydrate_fact_commit_intent_v2,
    rehydrate_fact_commit_intent_v2_json,
    rehydrate_fact_commit_materialized_v2,
    rehydrate_fact_commit_materialized_v2_json,
)
from companion_daemon.world_v2.schemas import fact_conflict_key


def _hex(character: str) -> str:
    return character * 64


def _evidence(
    ref_id: str = "observation:message:1",
    *,
    evidence_type: str = "observed_message",
    purpose: str = "current_fact",
    revision: int | None = 12,
    immutable_hash: str | None = None,
) -> dict[str, object]:
    return {
        "ref_id": ref_id,
        "evidence_type": evidence_type,
        "claim_purpose": purpose,
        "source_world_revision": revision,
        "immutable_hash": immutable_hash or _hex("a"),
    }


def _intent_raw() -> dict[str, object]:
    return {
        "subject_ref": "user:1",
        "predicate_code": "profile.display_name",
        "value_ref": "value:display-name:alice",
        "value_hash": "sha256:" + _hex("b"),
        "assertion_source_ref": "observation:message:1",
        "evidence_uses": (
            {
                "evidence_ref": "observation:message:1",
                "purpose": "current_fact",
                "anchor": True,
            },
        ),
        "confidence_bp": 8_500,
        "privacy_class": "personal",
    }


def _values_raw() -> dict[str, object]:
    evidence = _evidence()
    return {
        "subject_ref": "user:1",
        "predicate_code": "profile.display_name",
        "cardinality": "single",
        "conflict_key": fact_conflict_key(
            subject_ref="user:1", predicate_code="profile.display_name"
        ),
        "value_ref": "value:display-name:alice",
        "value_hash": _hex("b"),
        "assertion_binding": {
            "source_kind": "observed_message",
            "source_ref": "observation:message:1",
            "asserted_subject_ref": "user:1",
            "actor_ref": "user:1",
            "channel": "qq",
            "payload_ref": "message-payload:1",
            "content_payload_hash": _hex("c"),
        },
        "anchor_evidence_refs": (evidence,),
        "source_evidence_refs": (evidence,),
        "confidence_bp": 8_500,
        "privacy_class": "personal",
        "status": "active",
        "withdrawal_reason_code": None,
        "withdrawal_evidence_ref": None,
    }


def _materialized_raw() -> dict[str, object]:
    raw: dict[str, object] = {
        "payload_contract": "fact-commit-materialized.2",
        "change_id": "change:1",
        "transition_id": fact_commit_transition_id_v2(
            world_id="world:1",
            proposal_id="proposal:1",
            change_id="change:1",
            full_change_authority_hash=_hex("d"),
            fact_id="fact:" + _hex("e"),
        ),
        "fact_id": "fact:" + _hex("e"),
        "expected_entity_revision": 0,
        "evidence_refs": (_evidence(),),
        "policy_refs": ("policy:fact-commit.2",),
        "acceptance_id": "acceptance:1",
        "proposal_id": "proposal:1",
        "evaluated_world_revision": 12,
        "full_change_authority_hash": _hex("d"),
        "values": _values_raw(),
    }
    raw["materialized_change_hash"] = canonical_fact_commit_materialized_hash(raw)
    return raw


def test_intent_is_closed_canonical_and_hash_stable() -> None:
    intent = rehydrate_fact_commit_intent_v2(_intent_raw())
    canonical = canonical_fact_commit_intent_json(intent)

    assert isinstance(intent, FactCommitIntentV2)
    assert rehydrate_fact_commit_intent_v2_json(canonical) == intent
    assert canonical_fact_commit_intent_hash(intent) == canonical_fact_commit_intent_hash(
        json.loads(canonical)
    )


@pytest.mark.parametrize("field", tuple(_intent_raw()))
def test_intent_requires_every_field(field: str) -> None:
    raw = _intent_raw()
    del raw[field]
    with pytest.raises((ValidationError, ValueError)):
        rehydrate_fact_commit_intent_v2(raw)


@pytest.mark.parametrize(
    "field",
    [
        "cardinality",
        "conflict_key",
        "evidence_type",
        "source_world_revision",
        "immutable_hash",
        "assertion_binding",
        "after_image",
        "fact_projection",
        "accepted_event_ref",
        "committed_at",
    ],
)
def test_intent_rejects_system_derived_or_projection_fields(field: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        rehydrate_fact_commit_intent_v2({**_intent_raw(), field: "forged"})


def test_evidence_uses_are_sorted_and_globally_unique_by_ref() -> None:
    first = {
        "evidence_ref": "evidence:a",
        "purpose": "current_fact",
        "anchor": True,
    }
    second = {
        "evidence_ref": "evidence:b",
        "purpose": "conversation_continuity",
        "anchor": False,
    }
    raw = _intent_raw()
    raw["assertion_source_ref"] = "evidence:a"
    raw["evidence_uses"] = (second, first)
    with pytest.raises(ValidationError, match="sorted"):
        rehydrate_fact_commit_intent_v2(raw)

    raw["evidence_uses"] = (first, {**first, "purpose": "past_experience"})
    with pytest.raises(ValidationError, match="unique"):
        rehydrate_fact_commit_intent_v2(raw)


def test_intent_public_constructor_revalidates_hostile_nonassertion_use() -> None:
    raw = _intent_raw()
    raw["evidence_uses"] = (
        raw["evidence_uses"][0],  # type: ignore[index]
        FactEvidenceUseV2.model_construct(
            evidence_ref="zz:evidence",
            purpose="evil",
            anchor=False,
        ),
    )

    with pytest.raises((ValidationError, ValueError)):
        FactCommitIntentV2(**raw)


@pytest.mark.parametrize(
    "update",
    [
        {"assertion_source_ref": "evidence:missing"},
        {
            "evidence_uses": (
                {
                    "evidence_ref": "observation:message:1",
                    "purpose": "past_experience",
                    "anchor": True,
                },
            )
        },
        {
            "evidence_uses": (
                {
                    "evidence_ref": "observation:message:1",
                    "purpose": "current_fact",
                    "anchor": False,
                },
            )
        },
    ],
)
def test_assertion_source_use_is_current_fact_anchor(update: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="assertion source"):
        rehydrate_fact_commit_intent_v2({**_intent_raw(), **update})


@pytest.mark.parametrize("value", ["b" * 64, "sha256:" + "B" * 64, "sha256:b"])
def test_intent_value_hash_is_exact_prefixed_lowercase_sha256(value: str) -> None:
    with pytest.raises(ValidationError):
        rehydrate_fact_commit_intent_v2({**_intent_raw(), "value_hash": value})


def test_intent_enforces_confidence_count_and_canonical_json() -> None:
    for confidence in (0, 10_001):
        with pytest.raises(ValidationError):
            rehydrate_fact_commit_intent_v2(
                {**_intent_raw(), "confidence_bp": confidence}
            )
    raw = _intent_raw()
    raw["evidence_uses"] = tuple(raw["evidence_uses"]) * 65  # type: ignore[arg-type]
    with pytest.raises((ValidationError, ValueError)):
        rehydrate_fact_commit_intent_v2(raw)
    with pytest.raises(ValueError, match="canonical"):
        rehydrate_fact_commit_intent_v2_json(
            json.dumps(_intent_raw(), default=list, indent=2)
        )


def test_materialized_payload_roundtrips_with_exact_v2_fact_values() -> None:
    payload = rehydrate_fact_commit_materialized_v2(_materialized_raw())
    canonical = canonical_fact_commit_materialized_json(payload)

    assert isinstance(payload, FactCommitMaterializedPayloadV2)
    assert type(payload.values) is FactCommitValuesV2
    assert rehydrate_fact_commit_materialized_v2_json(canonical) == payload
    assert fact_commit_event_payload_hash(payload) == fact_commit_event_payload_hash(
        rehydrate_fact_commit_materialized_v2_json(canonical)
    )
    for forbidden in (
        "operation",
        "fact_before",
        "fact_after",
        "origin",
        "accepted_event_ref",
        "entity_revision",
        "semantic_fingerprint",
        "committed_at",
        "updated_at",
    ):
        assert forbidden not in type(payload).model_fields


@pytest.mark.parametrize(
    "path",
    [
        ("values", "status"),
        ("values", "withdrawal_reason_code"),
        ("values", "withdrawal_evidence_ref"),
        ("values", "source_evidence_refs", 0, "source_world_revision"),
        ("values", "source_evidence_refs", 0, "immutable_hash"),
    ],
)
def test_materialized_wire_requires_explicit_nested_default_fields(
    path: tuple[object, ...],
) -> None:
    raw = _materialized_raw()
    current: object = raw
    for component in path[:-1]:
        current = current[component]  # type: ignore[index]
    del current[path[-1]]  # type: ignore[index]
    with pytest.raises((ValidationError, ValueError)):
        rehydrate_fact_commit_materialized_v2(raw)
    with pytest.raises((ValidationError, ValueError)):
        FactCommitMaterializedPayloadV2.model_validate(raw, strict=True)


@pytest.mark.parametrize(
    "update",
    [
        {"evidence_type": "active_plan"},
        {"evidence_type": "clock_observation"},
        {"source_world_revision": None},
        {"immutable_hash": None},
        {"immutable_hash": "A" * 64},
    ],
)
def test_materialized_evidence_type_revision_and_hash_are_closed(
    update: dict[str, object],
) -> None:
    raw = _materialized_raw()
    evidence = {**_evidence(), **update}
    raw["evidence_refs"] = (evidence,)
    raw["values"]["source_evidence_refs"] = (evidence,)  # type: ignore[index]
    raw["values"]["anchor_evidence_refs"] = (evidence,)  # type: ignore[index]
    with pytest.raises((ValidationError, ValueError)):
        canonical_fact_commit_materialized_hash(raw)


def test_materialized_hash_excludes_only_its_self_field() -> None:
    raw = _materialized_raw()
    original_hash = raw["materialized_change_hash"]
    assert canonical_fact_commit_materialized_hash(raw) == original_hash

    changed_self = {**raw, "materialized_change_hash": _hex("f")}
    assert canonical_fact_commit_materialized_hash(changed_self) == original_hash
    with pytest.raises(ValidationError, match="materialized change hash"):
        rehydrate_fact_commit_materialized_v2(changed_self)

    changed_payload = deepcopy(raw)
    changed_payload["acceptance_id"] = "acceptance:other"
    with pytest.raises(ValidationError, match="materialized change hash"):
        rehydrate_fact_commit_materialized_v2(changed_payload)

    extra = {**raw, "origin": {"accepted_event_ref": "event:forged"}}
    with pytest.raises((ValidationError, ValueError)):
        rehydrate_fact_commit_materialized_v2(extra)


def test_self_hash_is_distinct_from_full_event_payload_hash() -> None:
    payload = rehydrate_fact_commit_materialized_v2(_materialized_raw())
    assert payload.materialized_change_hash != fact_commit_event_payload_hash(payload)


def test_materialized_sequences_and_cross_bindings_are_closed() -> None:
    raw = _materialized_raw()
    second = _evidence(
        "fact:source:2", evidence_type="committed_fact", purpose="current_fact"
    )
    raw["evidence_refs"] = (_evidence(), second)
    raw["values"]["source_evidence_refs"] = (_evidence(), second)  # type: ignore[index]
    with pytest.raises((ValidationError, ValueError), match="sorted"):
        canonical_fact_commit_materialized_hash(raw)

    raw = _materialized_raw()
    raw["values"]["assertion_binding"]["asserted_subject_ref"] = "user:other"  # type: ignore[index]
    with pytest.raises(ValidationError, match="assertion"):
        rehydrate_fact_commit_materialized_v2(raw)

    raw = _materialized_raw()
    raw["evidence_refs"] = (_evidence("observation:other"),)
    with pytest.raises(ValidationError, match="source evidence"):
        rehydrate_fact_commit_materialized_v2(raw)

    raw = _materialized_raw()
    assertion_evidence = _evidence(purpose="past_experience")
    raw["evidence_refs"] = (assertion_evidence,)
    raw["values"]["source_evidence_refs"] = (assertion_evidence,)  # type: ignore[index]
    raw["values"]["anchor_evidence_refs"] = (assertion_evidence,)  # type: ignore[index]
    with pytest.raises((ValidationError, ValueError), match="current_fact"):
        canonical_fact_commit_materialized_hash(raw)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("evidence_type", "committed_world_event"),
        ("claim_purpose", "current_fact"),
        ("source_world_revision", 13),
        ("immutable_hash", _hex("d")),
    ],
)
def test_fact_anchor_must_exactly_exist_in_source_evidence(
    field: str, replacement: object
) -> None:
    raw = _materialized_raw()
    source = _evidence(
        "zz:secondary",
        evidence_type="committed_fact",
        purpose="conversation_continuity",
    )
    forged_anchor = {**source, field: replacement}
    assertion = _evidence()
    raw["evidence_refs"] = (assertion, source)
    raw["values"]["source_evidence_refs"] = (assertion, source)  # type: ignore[index]
    raw["values"]["anchor_evidence_refs"] = (assertion, forged_anchor)  # type: ignore[index]

    with pytest.raises((ValidationError, ValueError), match="anchor"):
        canonical_fact_commit_materialized_hash(raw)


def test_hostile_nested_constructs_are_strictly_rehydrated() -> None:
    valid_values = FactCommitValuesV2.model_validate(_values_raw(), strict=True)
    hostile_assertion = type(valid_values.assertion_binding).model_construct(
        **{**valid_values.assertion_binding.__dict__, "content_payload_hash": "not-a-hash"}
    )
    hostile_evidence = ResolvedFactEvidenceV2.model_construct(
        **{**valid_values.source_evidence_refs[0].__dict__, "immutable_hash": "bad"}
    )
    hostile_values = FactCommitValuesV2.model_construct(
        **{
            **valid_values.__dict__,
            "assertion_binding": hostile_assertion,
            "source_evidence_refs": (hostile_evidence,),
            "anchor_evidence_refs": (hostile_evidence,),
        }
    )
    raw = _materialized_raw()
    raw["values"] = hostile_values
    raw["evidence_refs"] = (hostile_evidence,)

    with pytest.raises((ValidationError, ValueError)):
        rehydrate_fact_commit_materialized_v2(raw)


def test_public_constructor_revalidates_hostile_nested_evidence_instances() -> None:
    raw = _materialized_raw()
    hostile = ResolvedFactEvidenceV2.model_construct(
        **{**_evidence(), "immutable_hash": "forged"}
    )
    raw["evidence_refs"] = (hostile,)
    raw["values"]["anchor_evidence_refs"] = (hostile,)  # type: ignore[index]
    raw["values"]["source_evidence_refs"] = (hostile,)  # type: ignore[index]

    with pytest.raises((ValidationError, ValueError)):
        FactCommitMaterializedPayloadV2(**raw)


def test_public_v2_json_schema_has_only_exact_required_nonnullable_provenance() -> None:
    evidence_schema = ResolvedFactEvidenceV2.model_json_schema()
    assert set(evidence_schema["required"]) == {
        "ref_id",
        "evidence_type",
        "claim_purpose",
        "source_world_revision",
        "immutable_hash",
    }
    assert evidence_schema["properties"]["evidence_type"]["enum"] == [
        "observed_message",
        "operator_observation",
        "committed_world_event",
        "settled_world_event",
        "settled_external_result",
        "committed_fact",
        "committed_experience",
    ]
    assert evidence_schema["properties"]["claim_purpose"]["enum"] == [
        "current_fact",
        "past_experience",
        "future_plan",
        "private_hypothesis",
        "conversation_continuity",
    ]
    assert evidence_schema["properties"]["source_world_revision"]["type"] == "integer"
    assert evidence_schema["properties"]["immutable_hash"]["type"] == "string"

    values_schema = FactCommitValuesV2.model_json_schema()
    assert set(values_schema["required"]) == {
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
    public_schema = json.dumps(
        FactCommitMaterializedPayloadV2.model_json_schema(), sort_keys=True
    )
    assert '"EvidenceRef"' not in public_schema
    assert '"FactValues"' not in public_schema


def test_intent_hash_is_prefixed_sha256_of_exact_canonical_bytes() -> None:
    intent = rehydrate_fact_commit_intent_v2(_intent_raw())
    assert canonical_fact_commit_intent_json(intent) == (
        '{"assertion_source_ref":"observation:message:1","confidence_bp":8500,'
        '"evidence_uses":[{"anchor":true,"evidence_ref":"observation:message:1",'
        '"purpose":"current_fact"}],"predicate_code":"profile.display_name",'
        '"privacy_class":"personal","subject_ref":"user:1",'
        '"value_hash":"sha256:' + _hex("b") + '",'
        '"value_ref":"value:display-name:alice"}'
    )
    assert canonical_fact_commit_intent_hash(intent) == (
        "sha256:e5bf1b9ad64e965d3afe1c6c7a4c15bc4a3a43567e53529d45950be3d974616a"
    )


@pytest.mark.parametrize("contract", ["intent", "materialized"])
def test_contract_budget_counts_exact_utf8_bytes_and_all_json_separators(
    monkeypatch: pytest.MonkeyPatch, contract: str
) -> None:
    import companion_daemon.world_v2.fact_accepted_contracts as contracts

    if contract == "intent":
        raw = _intent_raw()
        raw["subject_ref"] = "人" * 100
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=list)
        monkeypatch.setattr(contracts, "MAX_FACT_INTENT_BYTES", len(canonical.encode("utf-8")))
        assert canonical_fact_commit_intent_json(raw) == canonical
        monkeypatch.setattr(contracts, "MAX_FACT_INTENT_BYTES", len(canonical.encode("utf-8")) - 1)
        with pytest.raises(ValueError, match="byte budget"):
            canonical_fact_commit_intent_json(raw)
    else:
        raw = _materialized_raw()
        raw["acceptance_id"] = "验" * 80
        raw["materialized_change_hash"] = canonical_fact_commit_materialized_hash(raw)
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=list)
        monkeypatch.setattr(
            contracts, "MAX_FACT_MATERIALIZED_BYTES", len(canonical.encode("utf-8"))
        )
        assert canonical_fact_commit_materialized_json(raw) == canonical
        monkeypatch.setattr(
            contracts, "MAX_FACT_MATERIALIZED_BYTES", len(canonical.encode("utf-8")) - 1
        )
        with pytest.raises(ValueError, match="byte budget"):
            canonical_fact_commit_materialized_json(raw)


def test_transition_id_is_stable_and_binds_every_authority_input() -> None:
    kwargs: dict[str, object] = {
        "world_id": "world:1",
        "proposal_id": "proposal:1",
        "change_id": "change:1",
        "full_change_authority_hash": _hex("d"),
        "fact_id": "fact:" + _hex("e"),
    }
    expected = fact_commit_transition_id_v2(**kwargs)  # type: ignore[arg-type]
    assert fact_commit_transition_id_v2(**kwargs) == expected  # type: ignore[arg-type]
    for field, replacement in (
        ("world_id", "world:2"),
        ("proposal_id", "proposal:2"),
        ("change_id", "change:2"),
        ("full_change_authority_hash", _hex("f")),
        ("fact_id", "fact:" + _hex("0")),
    ):
        assert fact_commit_transition_id_v2(  # type: ignore[arg-type]
            **{**kwargs, field: replacement}
        ) != expected


def test_contracts_enforce_material_budgets_and_are_inert() -> None:
    with pytest.raises((ValidationError, ValueError)):
        rehydrate_fact_commit_intent_v2(
            {**_intent_raw(), "subject_ref": "x" * MAX_FACT_INTENT_BYTES}
        )
    with pytest.raises((ValidationError, ValueError)):
        rehydrate_fact_commit_materialized_v2(
            {**_materialized_raw(), "acceptance_id": "x" * MAX_FACT_MATERIALIZED_BYTES}
        )
    for contract in (FactEvidenceUseV2, FactCommitIntentV2, FactCommitMaterializedPayloadV2):
        assert not hasattr(contract, "to_world_event")
        assert not hasattr(contract, "commit")
