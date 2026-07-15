from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.acceptance_manifest import (
    AcceptanceManifestV2,
    canonical_acceptance_manifest_hash,
)
from companion_daemon.world_v2.accepted_effect_contracts import (
    ACCEPTANCE_MANIFEST_V3_VERSION,
    EFFECT_AUTHORITY_VERSION,
    MAX_COMPILER_AUTHORITY_MATERIAL_BYTES,
    MAX_EFFECT_AUTHORITY_REFS,
    MAX_MANIFEST_V3_EFFECTS,
    MAX_MANIFEST_V3_PROPOSALS,
    MAX_TYPED_COMPILER_DEPENDENCIES,
    AcceptanceAuthorizedEffectV3,
    AcceptedEffectContractError,
    AcceptanceChangeAuthorityV3,
    AcceptanceManifestProposalV3,
    AcceptanceManifestV3,
    DurableDomainCompilerKeyV1,
    DurableEffectCompilerAuthorityV1,
    EffectAuthorityRefV3,
    TypedCompilerDependencyV1,
    canonical_acceptance_manifest_v3_hash,
    canonical_compiler_authority_hash,
    parse_acceptance_manifest_v3,
    rehydrate_acceptance_manifest_v3,
    rehydrate_acceptance_manifest_v3_json,
)


def _digest(character: str) -> str:
    return character * 64


def _dependency(
    kind: str = "payload_schema", ref: str = "schema:fact-commit.2", digest: str | None = None
) -> dict[str, object]:
    return {
        "dependency_kind": kind,
        "dependency_ref": ref,
        "dependency_digest": digest or _digest("a"),
    }


def _compiler_authority_raw() -> dict[str, object]:
    raw: dict[str, object] = {
        "authority_version": "effect-compiler-authority.1",
        "install_descriptor_ref": "compiler-install:fact-commit.2",
        "install_descriptor_digest": _digest("1"),
        "registry_version": "acceptance-domain-compilers.2",
        "registry_ref": "compiler-registry:production.2",
        "registry_digest": _digest("2"),
        "compiler_key": {
            "proposal_schema_registry": "world-v2-proposals.2",
            "change_kind": "fact_transition",
            "transition": "commit",
            "payload_schema": "fact_commit_intent.v2",
            "payload_version": 2,
        },
        "compiler_ref": "compiler:fact-commit.2",
        "compiler_digest": _digest("3"),
        "reverse_verifier_ref": "verifier:fact-commit.2",
        "reverse_verifier_digest": _digest("4"),
        "canonical_codec_ref": "codec:fact-commit.2",
        "canonical_codec_digest": _digest("5"),
        "output_contract_ref": "contract:fact-commit-materialized.2",
        "output_contract_digest": _digest("6"),
        "resolver_ref": "resolver:fact-authority.2",
        "resolver_digest": _digest("7"),
        "predicate_matrix_ref": "matrix:fact-predicate.2",
        "predicate_matrix_digest": _digest("8"),
        "evidence_use_matrix_ref": "matrix:fact-evidence-use.2",
        "evidence_use_matrix_digest": _digest("9"),
        "privacy_matrix_ref": "matrix:fact-privacy.2",
        "privacy_matrix_digest": _digest("a"),
        "observation_authority_contract_ref": "contract:observation-authority.2",
        "observation_authority_contract_digest": _digest("b"),
        "event_catalog_ref": "event-catalog:world-v2.18",
        "event_catalog_digest": _digest("c"),
        "domain_identity_contract_ref": "domain-identity:fact.2",
        "domain_identity_contract_digest": _digest("d"),
        "reducer_bundle_ref": "reducer-bundle:world-v2.18",
        "reducer_bundle_digest": _digest("e"),
        "typed_dependencies": (
            _dependency("payload_schema", "schema:fact-commit.2", _digest("f")),
            _dependency("policy_contract", "policy:fact-conflict.2", _digest("0")),
        ),
        "proposal_event_ref": "event:proposal:1",
        "proposal_event_payload_hash": _digest("1"),
        "proposal_hash": "sha256:" + _digest("2"),
    }
    return raw


def _proposal_raw() -> dict[str, object]:
    return {
        "proposal_id": "proposal:1",
        "proposal_kind": "decision",
        "proposal_schema_registry": "world-v2-proposals.2",
        "audit_contract": "proposal-envelope-audit.1",
        "proposal_event_ref": "event:proposal:1",
        "proposal_event_payload_hash": _digest("1"),
        "proposal_hash": "sha256:" + _digest("2"),
        "evaluated_world_revision": 12,
        "changes": (
            {
                "change_id": "change:1",
                "kind": "fact_transition",
                "target_id": "fact:1",
                "transition": "commit",
                "expected_entity_revision": 0,
                "evidence_refs": ("evidence:1",),
                "preconditions": (),
                "policy_refs": ("policy:fact-conflict.2",),
                "payload_schema": "fact_commit_intent.v2",
                "payload_version": 2,
                "payload_hash": "sha256:" + _digest("3"),
                "full_change_authority_hash": _digest("4"),
            },
        ),
        "action_intents": (),
    }


def _effect_raw() -> dict[str, object]:
    return {
        "effect_authority_version": EFFECT_AUTHORITY_VERSION,
        "ordinal": 0,
        "role": "domain_mutation",
        "event_id": "event:fact-committed:1",
        "event_type": "FactCommitted",
        "payload_hash": _digest("5"),
        "authority_refs": (
            {
                "proposal_id": "proposal:1",
                "authority_kind": "change",
                "authority_id": "change:1",
                "authority_hash": _digest("4"),
            },
        ),
        "domain_compiler_authority": _compiler_authority_raw(),
    }


def _manifest_raw() -> dict[str, object]:
    raw: dict[str, object] = {
        "manifest_version": ACCEPTANCE_MANIFEST_V3_VERSION,
        "acceptance_id": "acceptance:1",
        "status": "accepted",
        "evaluated_world_revision": 12,
        "proposals": (_proposal_raw(),),
        "authorized_effects": (_effect_raw(),),
    }
    raw["manifest_hash"] = canonical_acceptance_manifest_v3_hash(raw)
    return raw


def test_compiler_authority_roundtrip_is_strict_and_hash_is_byte_stable() -> None:
    authority = DurableEffectCompilerAuthorityV1.model_validate(
        _compiler_authority_raw(), strict=True
    )
    reloaded = DurableEffectCompilerAuthorityV1.model_validate_json(
        authority.model_dump_json(), strict=True
    )

    assert reloaded == authority
    assert canonical_compiler_authority_hash(reloaded) == canonical_compiler_authority_hash(
        authority
    )
    assert len(canonical_compiler_authority_hash(authority)) == 64


def test_durable_fact_key_uses_the_approved_intent_contract_only() -> None:
    key_raw = dict(_compiler_authority_raw()["compiler_key"])  # type: ignore[arg-type]
    DurableDomainCompilerKeyV1.model_validate(key_raw, strict=True)

    key_raw["payload_schema"] = "fact_transition.v2"
    with pytest.raises(ValidationError):
        DurableDomainCompilerKeyV1.model_validate(key_raw, strict=True)


def test_v3_manifest_proposal_explicitly_supports_the_fact_v2_audit_contract() -> None:
    raw = _proposal_raw()
    raw["audit_contract"] = "fact-commit-proposal-audit.2"

    proposal = AcceptanceManifestProposalV3.model_validate(raw, strict=True)

    assert proposal.audit_contract == "fact-commit-proposal-audit.2"


@pytest.mark.parametrize(
    "field",
    [
        "install_descriptor_digest",
        "registry_digest",
        "compiler_digest",
        "reverse_verifier_digest",
        "canonical_codec_digest",
        "output_contract_digest",
        "resolver_digest",
        "predicate_matrix_digest",
        "evidence_use_matrix_digest",
        "privacy_matrix_digest",
        "observation_authority_contract_digest",
        "event_catalog_digest",
        "domain_identity_contract_digest",
        "reducer_bundle_digest",
        "proposal_event_payload_hash",
    ],
)
@pytest.mark.parametrize("invalid", ["A" * 64, "sha256:" + "a" * 64, "a" * 63])
def test_every_explicit_artifact_digest_is_exact_lowercase_sha256(
    field: str, invalid: str
) -> None:
    raw = _compiler_authority_raw()
    raw[field] = invalid

    with pytest.raises(ValidationError):
        DurableEffectCompilerAuthorityV1.model_validate(raw, strict=True)


def test_typed_dependencies_must_be_sorted_unique_and_known() -> None:
    raw = _compiler_authority_raw()
    dependencies = tuple(raw["typed_dependencies"])  # type: ignore[arg-type]
    raw["typed_dependencies"] = tuple(reversed(dependencies))
    with pytest.raises(ValidationError, match="sorted"):
        DurableEffectCompilerAuthorityV1.model_validate(raw, strict=True)

    raw = _compiler_authority_raw()
    raw["typed_dependencies"] = (dependencies[0], dependencies[0])
    with pytest.raises(ValidationError, match="unique"):
        DurableEffectCompilerAuthorityV1.model_validate(raw, strict=True)

    with pytest.raises(ValidationError):
        TypedCompilerDependencyV1(
            dependency_kind="other",  # type: ignore[arg-type]
            dependency_ref="dependency:other",
            dependency_digest=_digest("a"),
        )


def test_untyped_dependency_fallback_and_unknown_fields_are_forbidden() -> None:
    raw = _compiler_authority_raw()
    raw["dependency_digests"] = ({"name": "escape", "digest": _digest("a")},)

    with pytest.raises(ValidationError):
        DurableEffectCompilerAuthorityV1.model_validate(raw, strict=True)


def test_dependency_count_and_total_metadata_bytes_are_bounded() -> None:
    raw = _compiler_authority_raw()
    raw["typed_dependencies"] = tuple(
        _dependency("payload_schema", f"schema:{index:03d}")
        for index in range(MAX_TYPED_COMPILER_DEPENDENCIES + 1)
    )
    with pytest.raises(ValidationError):
        DurableEffectCompilerAuthorityV1.model_validate(raw, strict=True)

    raw = _compiler_authority_raw()
    for name in tuple(raw):
        if name.endswith("_ref"):
            raw[name] = "x" * 512
    raw["typed_dependencies"] = tuple(
        _dependency("payload_schema", f"schema:{index:03d}:" + "x" * 500)
        for index in range(MAX_TYPED_COMPILER_DEPENDENCIES)
    )
    with pytest.raises(ValidationError):
        DurableEffectCompilerAuthorityV1.model_validate(raw, strict=True)

    assert MAX_COMPILER_AUTHORITY_MATERIAL_BYTES < 64_000


def test_manifest_effect_and_authority_ref_counts_are_bounded() -> None:
    raw = _manifest_raw()
    raw["proposals"] = tuple(_proposal_raw() for _ in range(MAX_MANIFEST_V3_PROPOSALS + 1))
    with pytest.raises(ValueError, match="proposal limit"):
        parse_acceptance_manifest_v3(raw)

    raw = _manifest_raw()
    raw["authorized_effects"] = tuple(
        _effect_raw() for _ in range(MAX_MANIFEST_V3_EFFECTS + 1)
    )
    with pytest.raises(ValueError, match="effect limit"):
        parse_acceptance_manifest_v3(raw)

    effect = _effect_raw()
    effect["authority_refs"] = tuple(
        effect["authority_refs"][0] for _ in range(MAX_EFFECT_AUTHORITY_REFS + 1)  # type: ignore[index]
    )
    with pytest.raises(ValidationError):
        AcceptanceAuthorizedEffectV3.model_validate(effect, strict=True)


def test_domain_effect_requires_exact_compiler_authority_and_change_ref() -> None:
    effect = AcceptanceAuthorizedEffectV3.model_validate(_effect_raw(), strict=True)
    assert effect.domain_compiler_authority is not None

    raw = _effect_raw()
    raw["domain_compiler_authority"] = None
    with pytest.raises(ValidationError, match="compiler authority"):
        AcceptanceAuthorizedEffectV3.model_validate(raw, strict=True)

    raw = _effect_raw()
    raw["authority_refs"] = ()
    with pytest.raises(ValidationError):
        AcceptanceAuthorizedEffectV3.model_validate(raw, strict=True)


def test_non_domain_effect_cannot_smuggle_domain_compiler_authority() -> None:
    raw = _effect_raw()
    raw.update(
        role="action_authorization",
        event_type="ActionAuthorized",
        authority_refs=(
            {
                "proposal_id": "proposal:1",
                "authority_kind": "action_intent",
                "authority_id": "intent:1",
                "authority_hash": _digest("4"),
            },
        ),
    )

    with pytest.raises(ValidationError, match="must not carry domain compiler authority"):
        AcceptanceAuthorizedEffectV3.model_validate(raw, strict=True)


def test_manifest_v3_hash_binds_complete_compiler_metadata() -> None:
    manifest = rehydrate_acceptance_manifest_v3(_manifest_raw())
    assert manifest.manifest_version == "acceptance-manifest.3"

    tampered = deepcopy(_manifest_raw())
    effects = list(tampered["authorized_effects"])  # type: ignore[arg-type]
    authority = effects[0]["domain_compiler_authority"]  # type: ignore[index]
    authority["resolver_digest"] = _digest("f")  # type: ignore[index]
    with pytest.raises(ValidationError, match="manifest hash"):
        rehydrate_acceptance_manifest_v3(tampered)


def test_public_parser_keeps_accepted_v3_disabled_by_default() -> None:
    with pytest.raises(AcceptedEffectContractError) as captured:
        parse_acceptance_manifest_v3(_manifest_raw())
    assert captured.value.code == "accepted_effect_contract.accepted_not_enabled"

    manifest = parse_acceptance_manifest_v3(
        _manifest_raw(), accepted_integration_enabled=True
    )
    assert manifest.status == "accepted"

    with pytest.raises(AcceptedEffectContractError) as invalid_gate:
        parse_acceptance_manifest_v3(  # type: ignore[arg-type]
            _manifest_raw(), accepted_integration_enabled="true"
        )
    assert invalid_gate.value.code == "accepted_effect_contract.invalid_gate"


def test_manifest_rejects_compiler_proposal_binding_mismatch() -> None:
    raw = _manifest_raw()
    effects = list(raw["authorized_effects"])  # type: ignore[arg-type]
    authority = effects[0]["domain_compiler_authority"]  # type: ignore[index]
    authority["proposal_event_ref"] = "event:proposal:other"  # type: ignore[index]
    raw["manifest_hash"] = canonical_acceptance_manifest_v3_hash(raw)

    with pytest.raises(ValidationError, match="proposal summary"):
        rehydrate_acceptance_manifest_v3(raw)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("proposals", 0, "proposal_schema_registry"), "world-v2-proposals.1"),
        (("proposals", 0, "changes", 0, "payload_version"), 1),
    ],
)
def test_proposal_registry_and_payload_version_are_closed(
    path: tuple[object, ...], value: object
) -> None:
    raw = _manifest_raw()
    current: object = raw
    for component in path[:-1]:
        current = current[component]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]
    raw["manifest_hash"] = canonical_acceptance_manifest_v3_hash(raw)

    with pytest.raises(ValidationError):
        rehydrate_acceptance_manifest_v3(raw)


def test_compiler_key_registry_and_payload_version_exactly_bind_proposal_summary() -> None:
    raw = _manifest_raw()
    proposal = raw["proposals"][0]  # type: ignore[index]
    proposal["proposal_schema_registry"] = "world-v2-proposals.2"  # type: ignore[index]
    change = proposal["changes"][0]  # type: ignore[index]
    change["payload_version"] = 2  # type: ignore[index]
    rehydrate_acceptance_manifest_v3(raw)


def _manifest_with_due_window(start: datetime, end: datetime) -> dict[str, object]:
    raw = _manifest_raw()
    proposal = raw["proposals"][0]  # type: ignore[index]
    proposal["action_intents"] = (  # type: ignore[index]
        {
            "intent_id": "intent:1",
            "kind": "reply",
            "layer": "external_action",
            "target": "user:1",
            "causal_change_id": "change:1",
            "beat_ref": None,
            "dependencies": (),
            "due_window": (start, end),
            "payload_ref": "payload:reply:1",
            "payload_hash": "sha256:" + _digest("6"),
            "full_action_authority_hash": _digest("7"),
        },
    )
    raw["manifest_hash"] = canonical_acceptance_manifest_v3_hash(raw)
    return raw


def test_datetime_canonicalization_uses_utc_z_and_survives_json_roundtrip() -> None:
    utc_raw = _manifest_with_due_window(
        datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 5, 0, tzinfo=UTC),
    )
    china = timezone(timedelta(hours=8))
    offset_raw = _manifest_with_due_window(
        datetime(2026, 7, 15, 12, 0, tzinfo=china),
        datetime(2026, 7, 15, 13, 0, tzinfo=china),
    )

    assert canonical_acceptance_manifest_v3_hash(utc_raw) == (
        canonical_acceptance_manifest_v3_hash(offset_raw)
    )
    manifest = rehydrate_acceptance_manifest_v3(offset_raw)
    dumped = manifest.model_dump_json()
    assert canonical_acceptance_manifest_v3_hash(manifest.model_dump(mode="json")) == (
        canonical_acceptance_manifest_v3_hash(manifest)
    )
    assert rehydrate_acceptance_manifest_v3_json(dumped) == manifest
    assert canonical_acceptance_manifest_v3_hash(
        rehydrate_acceptance_manifest_v3_json(dumped)
    ) == canonical_acceptance_manifest_v3_hash(manifest)


@pytest.mark.parametrize("field", ["acceptance_id", "target_id", "proposal_event_ref"])
def test_iso_shaped_identity_and_ref_strings_remain_byte_distinct(field: str) -> None:
    utc_spelling = "2026-07-15T04:00:00Z"
    offset_spelling = "2026-07-15T12:00:00+08:00"

    def with_spelling(spelling: str) -> dict[str, object]:
        raw = _manifest_raw()
        proposal = raw["proposals"][0]  # type: ignore[index]
        if field == "acceptance_id":
            raw["acceptance_id"] = spelling
        elif field == "target_id":
            proposal["changes"][0]["target_id"] = spelling  # type: ignore[index]
        else:
            proposal["proposal_event_ref"] = spelling  # type: ignore[index]
            effect = raw["authorized_effects"][0]  # type: ignore[index]
            effect["domain_compiler_authority"]["proposal_event_ref"] = spelling  # type: ignore[index]
        raw["manifest_hash"] = canonical_acceptance_manifest_v3_hash(raw)
        return raw

    utc_raw = with_spelling(utc_spelling)
    offset_raw = with_spelling(offset_spelling)

    assert utc_raw["manifest_hash"] != offset_raw["manifest_hash"]
    assert rehydrate_acceptance_manifest_v3(utc_raw).manifest_hash != (
        rehydrate_acceptance_manifest_v3(offset_raw).manifest_hash
    )


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _manifest_with_due_window(
            datetime(2026, 7, 15, 4, 0),
            datetime(2026, 7, 15, 5, 0),
        )


def test_model_construct_does_not_bypass_parse_revalidation() -> None:
    forged = AcceptanceManifestV3.model_construct(**_manifest_raw())
    forged.__dict__["manifest_version"] = "acceptance-manifest.2"

    with pytest.raises(ValidationError):
        rehydrate_acceptance_manifest_v3(forged)


def test_nested_model_construct_is_strictly_rehydrated() -> None:
    forged_dependency = TypedCompilerDependencyV1.model_construct(
        dependency_kind="other",
        dependency_ref="dependency:forged",
        dependency_digest=_digest("a"),
    )
    raw = _compiler_authority_raw()
    raw["typed_dependencies"] = (forged_dependency,)

    with pytest.raises(ValidationError):
        DurableEffectCompilerAuthorityV1.model_validate(raw, strict=True)


def test_v3_is_wire_isolated_from_v2_and_uses_a_distinct_hash_domain() -> None:
    assert "domain_compiler_authority" not in AcceptanceManifestV2.model_json_schema()[
        "$defs"
    ]["AcceptanceAuthorizedEffectV2"]["properties"]

    v2_raw: dict[str, object] = {
        "manifest_version": "acceptance-manifest.2",
        "acceptance_id": "acceptance:v2:golden",
        "status": "rejected",
        "evaluated_world_revision": 3,
        "proposals": (
            {
                "proposal_id": "proposal:v2",
                "proposal_kind": "decision",
                "audit_contract": "proposal-envelope-audit.1",
                "proposal_event_ref": "event:proposal:v2",
                "proposal_event_payload_hash": _digest("a"),
                "proposal_hash": "sha256:" + _digest("b"),
                "evaluated_world_revision": 3,
                "changes": (),
                "action_intents": (),
            },
        ),
        "authorized_effects": (),
    }
    assert canonical_acceptance_manifest_hash(v2_raw) == (
        "c842592f91aa7d265565e678df2be710ac3583b4aa5b8e85b425f6ab5920f6b8"
    )
    assert canonical_acceptance_manifest_v3_hash(v2_raw) != canonical_acceptance_manifest_hash(
        v2_raw
    )
    with pytest.raises(ValidationError):
        parse_acceptance_manifest_v3(v2_raw)


def test_contracts_are_inert_and_do_not_materialize_world_events() -> None:
    for contract in (
        DurableEffectCompilerAuthorityV1,
        AcceptanceAuthorizedEffectV3,
        AcceptanceManifestV3,
    ):
        assert not hasattr(contract, "to_world_event")
        assert not hasattr(contract, "commit")


def test_public_models_are_frozen_and_strict() -> None:
    key = DurableDomainCompilerKeyV1.model_validate(
        _compiler_authority_raw()["compiler_key"], strict=True
    )
    with pytest.raises(ValidationError):
        key.payload_version = 3  # type: ignore[misc]

    with pytest.raises(ValidationError):
        AcceptanceChangeAuthorityV3.model_validate(
            {**_proposal_raw()["changes"][0], "expected_entity_revision": "0"},  # type: ignore[index]
            strict=True,
        )
    AcceptanceManifestProposalV3.model_validate(_proposal_raw(), strict=True)
    EffectAuthorityRefV3.model_validate(_effect_raw()["authority_refs"][0], strict=True)  # type: ignore[index]
