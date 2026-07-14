from __future__ import annotations

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.acceptance_manifest import (
    ACCEPTANCE_MANIFEST_ERROR_PREFIX,
    ACCEPTANCE_MANIFEST_VERSION,
    MAX_EFFECT_PROPOSAL_REFS,
    MAX_MANIFEST_EFFECTS,
    MAX_MANIFEST_MATERIAL_BYTES,
    MAX_MANIFEST_MATERIAL_DEPTH,
    MAX_MANIFEST_MATERIAL_NODES,
    MAX_MANIFEST_PROPOSALS,
    AcceptanceAuthorizedEffectV2,
    AcceptanceManifestError,
    AcceptanceManifestProposalV2,
    AcceptanceManifestRefV2,
    AcceptanceManifestV2,
    canonical_acceptance_manifest_hash,
    parse_acceptance_manifest_v2,
)


def _proposal(index: int = 1) -> dict[str, object]:
    return {
        "proposal_id": f"proposal:{index}",
        "proposal_kind": "decision",
        "audit_contract": "proposal-envelope-audit.1",
        "proposal_event_ref": f"event:proposal:{index}",
        "proposal_event_payload_hash": format(index % 16, "x") * 64,
        "proposal_hash": "sha256:" + format(index % 16, "x") * 64,
        "evaluated_world_revision": 12,
        "changes": (
            {
                "change_id": f"change:{index}",
                "kind": "fact_transition",
                "target_id": f"fact:{index}",
                "transition": "commit",
                "expected_entity_revision": 0,
                "evidence_refs": ("evidence:1",),
                "preconditions": ("precondition:1",),
                "policy_refs": ("policy:1",),
                "payload_schema": "fact_transition.v1",
                "payload_hash": "sha256:" + "a" * 64,
                "full_change_authority_hash": format(index % 16, "x") * 64,
            },
        ),
        "action_intents": (
            {
                "intent_id": f"intent:{index}",
                "kind": "reply",
                "layer": "external_action",
                "target": "user:1",
                "causal_change_id": f"change:{index}",
                "beat_ref": None,
                "dependencies": (),
                "due_window": None,
                "payload_ref": "payload:1",
                "payload_hash": "sha256:" + "b" * 64,
                "full_action_authority_hash": format(index % 16, "x") * 64,
            },
        ),
    }


def _effect(
    index: int = 0,
    *,
    role: str = "domain_mutation",
    event_type: str = "FactCommitted",
    proposal_refs: tuple[str, ...] = ("proposal:1",),
    change_id: str = "change:1",
    change_hash: str = "1" * 64,
) -> dict[str, object]:
    value: dict[str, object] = {
        "ordinal": index,
        "role": role,
        "event_id": f"event:effect:{index}",
        "event_type": event_type,
        "payload_hash": "e" * 64,
    }
    authority_kind = "action_intent" if role == "action_authorization" else "change"
    authority_id = (
        proposal_refs[0].replace("proposal:", "intent:")
        if authority_kind == "action_intent"
        else change_id
    )
    value["authority_refs"] = (
        {
            "proposal_id": proposal_refs[0],
            "authority_kind": authority_kind,
            "authority_id": authority_id,
            "authority_hash": change_hash,
        },
    )
    return value


def _manifest_raw(
    *,
    status: str = "rejected",
    proposals: tuple[dict[str, object], ...] | None = None,
    effects: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    raw: dict[str, object] = {
        "manifest_version": ACCEPTANCE_MANIFEST_VERSION,
        "acceptance_id": "acceptance:multi:1",
        "status": status,
        "evaluated_world_revision": 12,
        "proposals": proposals or (_proposal(),),
        "authorized_effects": effects,
    }
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    return raw


def _error_code(exc: BaseException) -> str:
    assert isinstance(exc, AcceptanceManifestError)
    assert str(exc).startswith(ACCEPTANCE_MANIFEST_ERROR_PREFIX)
    return exc.code


@pytest.mark.parametrize("status", ["rejected", "stale"])
def test_nonaccepted_manifest_is_canonical_hash_bound_and_referenceable(status: str) -> None:
    raw = _manifest_raw(status=status, proposals=(_proposal(1), _proposal(2)))

    manifest = parse_acceptance_manifest_v2(raw)
    reference = AcceptanceManifestRefV2.from_manifest(
        manifest,
        acceptance_event_ref="event:acceptance:" + "x" * 300,
        acceptance_event_payload_hash="a" * 64,
        recorded_at_world_revision=13,
    )

    assert manifest.status == status
    assert manifest.authorized_effects == ()
    assert reference.manifest_hash == manifest.manifest_hash
    assert reference.proposals == manifest.proposals


def test_accepted_shape_is_valid_but_public_integration_gate_is_closed() -> None:
    raw = _manifest_raw(status="accepted", effects=(_effect(),))

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)
    assert _error_code(captured.value) == "acceptance_manifest.accepted_not_enabled"

    manifest = parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)
    assert manifest.status == "accepted"
    assert manifest.authorized_effects[0].role == "domain_mutation"


def test_accepted_shape_binds_effect_event_ref_longer_than_256() -> None:
    raw = _manifest_raw(status="accepted", effects=(_effect(),))
    effect = dict(raw["authorized_effects"][0])  # type: ignore[index]
    long_event_ref = "event:" + "x" * 300
    effect["event_id"] = long_event_ref
    raw["authorized_effects"] = (effect,)
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    manifest = parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)
    assert manifest.authorized_effects[0].event_id == long_event_ref

    effect["event_id"] = long_event_ref + ":tampered"
    raw["authorized_effects"] = (effect,)
    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)
    assert _error_code(captured.value) == "acceptance_manifest.hash_mismatch"


@pytest.mark.parametrize("forged_gate", ["false", 1, 0, None, object()])
def test_accepted_integration_gate_rejects_truthiness_and_non_bool_values(
    forged_gate: object,
) -> None:
    raw = _manifest_raw(status="accepted", effects=(_effect(),))

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(  # type: ignore[arg-type]
            raw, accepted_integration_enabled=forged_gate
        )

    assert _error_code(captured.value) == "acceptance_manifest.invalid_gate"


@pytest.mark.parametrize(
    ("role", "event_type"),
    [
        ("budget_reservation", "BudgetReserved"),
        ("action_authorization", "ActionAuthorized"),
    ],
)
def test_non_domain_effect_roles_are_closed(role: str, event_type: str) -> None:
    raw = _manifest_raw(
        status="accepted",
        effects=(_effect(), _effect(1, role=role, event_type=event_type)),
    )

    manifest = parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)

    assert manifest.authorized_effects[1].event_type == event_type

    wrong = dict(raw)
    wrong_effect = dict(raw["authorized_effects"][1])  # type: ignore[index]
    wrong_effect["event_type"] = "FactCommitted"
    wrong["authorized_effects"] = (raw["authorized_effects"][0], wrong_effect)  # type: ignore[index]
    wrong["manifest_hash"] = canonical_acceptance_manifest_hash(wrong)
    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(wrong, accepted_integration_enabled=True)
    assert _error_code(captured.value) == "acceptance_manifest.invalid_role_shape"


def test_domain_effect_requires_one_proposal_and_complete_change_authority() -> None:
    raw = _manifest_raw(status="accepted", effects=(_effect(),))
    effect = dict(raw["authorized_effects"][0])  # type: ignore[index]
    effect["authority_refs"] = (
        effect["authority_refs"][0],
        {
            "proposal_id": "proposal:2",
            "authority_kind": "change",
            "authority_id": "change:2",
            "authority_hash": "2" * 64,
        },
    )
    raw["proposals"] = (_proposal(1), _proposal(2))
    raw["authorized_effects"] = (effect,)
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)
    assert _error_code(captured.value) == "acceptance_manifest.invalid_role_shape"


def test_accepted_manifest_can_cover_multiple_proposals_once_each() -> None:
    raw = _manifest_raw(
        status="accepted",
        proposals=(_proposal(1), _proposal(2)),
        effects=(
            _effect(),
            _effect(
                1,
                proposal_refs=("proposal:2",),
                change_id="change:2",
                change_hash="2" * 64,
            ),
        ),
    )

    manifest = parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)

    assert len(manifest.proposals) == len(manifest.authorized_effects) == 2


def test_proposal_event_refs_are_hash_bound() -> None:
    second = _proposal(2)
    second["proposal_event_ref"] = "event:proposal:1"
    raw = _manifest_raw(proposals=(_proposal(1), second))
    manifest = parse_acceptance_manifest_v2(raw)
    assert manifest.proposals[1].proposal_event_ref == "event:proposal:1"


def test_rejected_and_stale_cannot_smuggle_authorized_effects() -> None:
    raw = _manifest_raw(status="rejected", effects=(_effect(),))

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)

    assert _error_code(captured.value) == "acceptance_manifest.effects_for_nonaccepted"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.update(status="stale"),
        lambda raw: raw["proposals"][0].update(proposal_hash="sha256:" + "0" * 64),
        lambda raw: raw.update(acceptance_id="acceptance:tampered"),
    ],
)
def test_manifest_hash_rejects_every_tampered_authority(mutation) -> None:
    raw = _manifest_raw()
    mutation(raw)

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)

    assert _error_code(captured.value) == "acceptance_manifest.hash_mismatch"


def test_parser_revalidates_model_construct_and_manifest_reference() -> None:
    raw = _manifest_raw()
    valid = AcceptanceManifestV2.model_validate(raw)
    forged_values = valid.model_dump(mode="python")
    forged_values["manifest_hash"] = "f" * 64
    forged = AcceptanceManifestV2.model_construct(**forged_values)

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(forged)
    assert _error_code(captured.value) == "acceptance_manifest.hash_mismatch"

    reference_values = forged.model_dump(
        mode="python", exclude={"manifest_version"}, warnings=False
    )
    reference_values.update(
        acceptance_event_ref="event:acceptance:1",
        acceptance_event_payload_hash="a" * 64,
        recorded_at_world_revision=13,
    )
    forged_reference = AcceptanceManifestRefV2.model_construct(**reference_values)
    with pytest.raises(ValidationError, match="hash_mismatch"):
        AcceptanceManifestRefV2.model_validate(
            forged_reference.model_dump(mode="python", warnings=False)
        )


def test_proposals_effects_and_refs_must_be_sorted_unique_and_contiguous() -> None:
    unsorted = _manifest_raw(proposals=(_proposal(2), _proposal(1)))
    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(unsorted)
    assert _error_code(captured.value) == "acceptance_manifest.noncanonical_proposals"

    raw = _manifest_raw(
        status="accepted",
        effects=(_effect(1),),
    )
    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)
    assert _error_code(captured.value) == "acceptance_manifest.effect_order_invalid"

    unsorted_refs = _effect(role="budget_reservation", event_type="BudgetReserved")
    unsorted_refs["authority_refs"] = (
        {"proposal_id": "proposal:2", "authority_kind": "change", "authority_id": "change:2", "authority_hash": "2" * 64},
        {"proposal_id": "proposal:1", "authority_kind": "change", "authority_id": "change:1", "authority_hash": "1" * 64},
    )
    with pytest.raises(ValidationError, match="noncanonical_proposal_refs"):
        AcceptanceAuthorizedEffectV2(**unsorted_refs)


def test_effect_cannot_reference_a_proposal_outside_manifest() -> None:
    raw = _manifest_raw(
        status="accepted",
        effects=(_effect(proposal_refs=("proposal:missing",)),),
    )

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)
    assert _error_code(captured.value) == "acceptance_manifest.mutation_binding_mismatch"


@pytest.mark.parametrize("target", ["proposals", "effects", "refs"])
def test_parser_preflights_dos_limits_before_model_validation(target: str) -> None:
    raw = _manifest_raw()
    if target == "proposals":
        raw["proposals"] = tuple(
            _proposal(index + 1) for index in range(MAX_MANIFEST_PROPOSALS + 1)
        )
    elif target == "effects":
        raw["authorized_effects"] = tuple(
            _effect(index) for index in range(MAX_MANIFEST_EFFECTS + 1)
        )
    else:
        raw = _manifest_raw(status="accepted", effects=(_effect(),))
        effect = dict(raw["authorized_effects"][0])  # type: ignore[index]
        effect["authority_refs"] = tuple(
            {
                "proposal_id": f"proposal:{index}",
                "authority_kind": "change",
                "authority_id": f"change:{index}",
                "authority_hash": "a" * 64,
            }
            for index in range(MAX_EFFECT_PROPOSAL_REFS + 1)
        )
        raw["authorized_effects"] = (effect,)

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)

    assert _error_code(captured.value) == "acceptance_manifest.limit_exceeded"


class _DumpBombManifest(AcceptanceManifestV2):
    def model_dump(self, *args, **kwargs):
        raise AssertionError("untrusted model_dump executed before preflight")


def test_model_construct_oversize_is_rejected_without_calling_model_dump() -> None:
    raw = _manifest_raw()
    raw["proposals"] = tuple(_proposal(index + 1) for index in range(MAX_MANIFEST_PROPOSALS + 1))
    bomb = _DumpBombManifest.model_construct(**raw)

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(bomb)

    assert _error_code(captured.value) == "acceptance_manifest.limit_exceeded"


@pytest.mark.parametrize("attack", ["cycle", "depth", "nodes", "bytes"])
def test_full_material_budget_rejects_graph_attacks_before_serialization(attack: str) -> None:
    raw = _manifest_raw()
    if attack == "cycle":
        cycle: list[object] = []
        cycle.append(cycle)
        raw["unexpected"] = cycle
        expected = "acceptance_manifest.cyclic_material"
    elif attack == "depth":
        nested: object = "leaf"
        for _ in range(MAX_MANIFEST_MATERIAL_DEPTH + 2):
            nested = [nested]
        raw["unexpected"] = nested
        expected = "acceptance_manifest.material_limit_exceeded"
    elif attack == "nodes":
        raw["unexpected"] = [None] * (MAX_MANIFEST_MATERIAL_NODES + 1)
        expected = "acceptance_manifest.material_limit_exceeded"
    else:
        raw["acceptance_id"] = "x" * (MAX_MANIFEST_MATERIAL_BYTES + 1)
        expected = "acceptance_manifest.material_limit_exceeded"

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw, accepted_integration_enabled=True)

    assert _error_code(captured.value) == expected


def test_huge_integer_is_budgeted_before_canonical_json() -> None:
    raw = _manifest_raw()
    raw["evaluated_world_revision"] = 1 << 1_000_000

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)

    assert _error_code(captured.value) == "acceptance_manifest.material_limit_exceeded"


@pytest.mark.parametrize("location", ["value", "key"])
def test_lone_surrogate_is_wrapped_as_stable_invalid_shape(location: str) -> None:
    raw = _manifest_raw()
    if location == "value":
        raw["acceptance_id"] = "\ud800"
    else:
        raw["\ud800"] = "value"

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)

    assert _error_code(captured.value) == "acceptance_manifest.invalid_shape"

    with pytest.raises(AcceptanceManifestError) as hash_error:
        canonical_acceptance_manifest_hash(raw)
    assert _error_code(hash_error.value) == "acceptance_manifest.invalid_shape"


def test_untrusted_validation_text_cannot_inject_a_stable_error_code() -> None:
    raw = _manifest_raw()
    raw["status"] = "acceptance_manifest.hash_mismatch: injected"
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)

    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)

    assert _error_code(captured.value) == "acceptance_manifest.invalid_shape"

    extra_hash = _manifest_raw()
    extra_hash["payload_hash"] = "bad"
    extra_hash["manifest_hash"] = canonical_acceptance_manifest_hash(extra_hash)
    with pytest.raises(AcceptanceManifestError) as extra_captured:
        parse_acceptance_manifest_v2(extra_hash)
    assert _error_code(extra_captured.value) == "acceptance_manifest.invalid_shape"


def test_digest_and_extra_fields_fail_closed_with_stable_prefix() -> None:
    raw = _manifest_raw()
    raw["manifest_hash"] = "not-a-digest"
    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)
    assert _error_code(captured.value) == "acceptance_manifest.invalid_digest"

    raw = _manifest_raw()
    raw["unexpected"] = True
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)
    assert _error_code(captured.value) == "acceptance_manifest.invalid_shape"


def test_proposal_value_object_rejects_non_hex_authority_hash() -> None:
    with pytest.raises(ValidationError, match="invalid_digest"):
        AcceptanceManifestProposalV2(
            **{**_proposal(), "proposal_event_payload_hash": "z" * 64}
        )


@pytest.mark.parametrize("layer", ["change", "action", "effect_ref"])
def test_nested_authority_digest_errors_are_stable(layer: str) -> None:
    raw = _manifest_raw(
        status="accepted" if layer == "effect_ref" else "rejected",
        effects=(_effect(),) if layer == "effect_ref" else (),
    )
    if layer == "effect_ref":
        effect = dict(raw["authorized_effects"][0])  # type: ignore[index]
        ref = dict(effect["authority_refs"][0])
        ref["authority_hash"] = "z" * 64
        effect["authority_refs"] = (ref,)
        raw["authorized_effects"] = (effect,)
    else:
        proposal = dict(raw["proposals"][0])  # type: ignore[index]
        field = "changes" if layer == "change" else "action_intents"
        authority = dict(proposal[field][0])
        authority[
            "full_change_authority_hash"
            if layer == "change"
            else "full_action_authority_hash"
        ] = "z" * 64
        proposal[field] = (authority,)
        raw["proposals"] = (proposal,)
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(
            raw, accepted_integration_enabled=layer == "effect_ref"
        )
    assert _error_code(captured.value) == "acceptance_manifest.invalid_digest"


def test_action_authority_rejects_reversed_due_window() -> None:
    raw = _manifest_raw()
    proposal = dict(raw["proposals"][0])  # type: ignore[index]
    action = dict(proposal["action_intents"][0])
    action["due_window"] = (
        "2026-07-15T10:01:00+00:00",
        "2026-07-15T10:00:00+00:00",
    )
    proposal["action_intents"] = (action,)
    raw["proposals"] = (proposal,)
    raw["manifest_hash"] = canonical_acceptance_manifest_hash(raw)
    with pytest.raises(AcceptanceManifestError) as captured:
        parse_acceptance_manifest_v2(raw)
    assert _error_code(captured.value) == "acceptance_manifest.invalid_role_shape"
